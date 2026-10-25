#!/usr/bin/env python3
"""CRISIS ENGINE v3.0 — сборщик. Запускается по расписанию (GitHub Actions), пишет data/*.json."""
import json, os, sys
import datetime as dt
from engine import *  # noqa

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
PRIOR = {'liq': 'Ликвидность', 'credit': 'Кредит', 'market': 'Рынок', 'macro': 'Макро'}


def r1(x):
    return None if x is None else round(x, 1)


def bdays(a, b):
    return [a + TD(days=k) for k in range((b - a).days + 1) if (a + TD(days=k)).weekday() < 5]


def near(dct, d):
    for _ in range(10):
        if d in dct:
            return d
        d -= TD(days=1)
    return None


def nl_info(data, asof):
    s = data.B.get('NETLIQ')
    if s is None:
        return None
    i = s.idx(asof)
    if i < 13:
        return None
    v = s.v
    f = lambda n: dict(usd=round(v[i - 1] - v[i - 1 - n], 1), pct=round((v[i - 1] / v[i - 1 - n] - 1) * 100, 2))  # noqa
    age = (asof - s.d[i - 1]).days
    out = dict(value=round(v[i - 1], 1), unit='млрд $', obs=s.d[i - 1].isoformat(), pub=s.a[i - 1].isoformat(),
               stale=age > 15, d1=f(1), d4=f(4), d12=f(12))
    w = last(data.B.get('WALCL'), asof, 15)
    t = last(data.B.get(data.tga_key), asof, 15)
    r = last(data.B.get('RRPONTSYD'), asof, 15)
    if w is not None and t is not None:
        out.update(walcl=round(w / 1000), tga=round(t / 1000), rrp=round(r) if r is not None else 0)
    return out


def m2_info(data, asof):
    s = data.B.get('M2SL')
    if s is None:
        return None
    i = s.idx(asof)
    if i < 13:
        return None
    v = s.v
    return dict(value=round(v[i - 1]), unit='млрд $', obs=s.d[i - 1].isoformat(),
                chg3_usd=round(v[i - 1] - v[i - 4]), chg3_pct=round((v[i - 1] / v[i - 4] - 1) * 100, 2),
                ann3=round(((v[i - 1] / v[i - 4]) ** 4 - 1) * 100, 2), yoy=round((v[i - 1] / v[i - 13] - 1) * 100, 2))


def run(raw, err, live, today):
    data = Data(raw, err)
    start = today - TD(days=365 + 28)
    light = {d: compute(data, d, light=True) for d in bdays(start, today)}
    hist = {}
    for d, rs in light.items():
        p = light.get(near(light, d - TD(days=28)) or d)
        hist[d] = finalize(rs, p['base'] if p and p['valid'] else None)

    rs = compute(data, today)
    rs28 = light[near(light, today - TD(days=28))]
    rs182 = compute(data, today - TD(days=182), light=True)
    fin = finalize(rs, rs28['base'] if rs28['valid'] else None)
    score = fin['value'] if fin else None
    prev_fin = hist.get(near(hist, today - TD(days=28)))
    delta = (score - prev_fin['value']) if (score is not None and prev_fin) else None
    trend = None if delta is None else 'up' if delta >= 5 else 'down' if delta <= -5 else 'flat'

    cyc0 = cycle(rs182, None)
    cyc = cycle(rs, cyc0['state'])
    reg = regime(rs, cyc) if rs['valid'] else dict(state=None, label='Недостаточно данных', type=None, why=rs['reason'])
    cr = crypto(data, today, live)

    blocks = {}
    for b in BW:
        cur, old = rs['blocks'][b], rs28['blocks'][b]
        blocks[b] = dict(name=BNAME[b], ru=PRIOR[b], weight=BW[b], score=r1(cur['score']), cov=round(cur['cov'], 2),
                         ok=cur['ok'], n=cur['n'], stale=cur['stale'],
                         delta=r1(cur['score'] - old['score']) if cur['score'] is not None and old['score'] is not None else None,
                         why=explain(rs, rs28, b) if cur['score'] is not None else 'недостаточно данных')

    # --- инвестиционный блок ---
    inv = None
    if rs['valid'] and cyc['state'] and reg['state']:
        m = modes(rs, score, cyc, reg, cr, trend)
        eq = m['eq']
        btc = eth = None
        if cr['lvl_btc'] is not None:
            btc = max(min(3, eq + (1 if m['liq_hi'] else 0)), cr['lvl_btc'])
            eth = btc
            e2 = cr.get('e200')
            if e2 is not None:
                eth = max(eth, 2 if e2 < -10 else 1 if e2 < 0 else 0)
        mode = lambda lv, why: None if lv is None else dict(level=lv, label=LVL[lv], why=why)  # noqa
        crypto_why = '; '.join(f'{i["name"]} {i["disp"]}' for i in cr['items'][:4] if i['ok'])
        modes_out = dict(
            equity=mode(eq, m['eq_why']),
            btc=mode(btc, f'акции → {LVL[eq]}' + ('; ликвидность ≥ 60 (+1 уровень)' if m['liq_hi'] else '') + '; ' + crypto_why),
            eth=mode(eth, 'как BTC' + (f'; ETH к 200-дн.: {fmt("chg", cr["e200"])}' if cr.get('e200') is not None else '')))
        scen = []
        for name, b1, b2 in (('1 мес', 'market', 'liq'), ('3 мес', 'credit', 'liq'), ('6 мес', 'macro', 'credit'), ('12 мес', 'macro', 'liq')):
            s = (rs['blocks'][b1]['score'] + rs['blocks'][b2]['score']) / 2
            dl = [blocks[b]['delta'] for b in (b1, b2) if blocks[b]['delta'] is not None]
            d = sum(dl) / len(dl) if dl else 0
            lean = ('ухудшение' if (s >= 55 and d >= 0) or d >= 8 else
                    'улучшение' if (s < 40 and d <= 0) or d <= -8 else 'без существенных изменений')
            top = sorted([x for x in rs['ind'] if x['blk'] in (b1, b2) and x['status'] == 'ok'], key=lambda x: -x['stress'])[:2]
            scen.append(dict(h=name, lean=lean, drivers=f'{PRIOR[b1]} {rs["blocks"][b1]["score"]:.0f}, {PRIOR[b2]} {rs["blocks"][b2]["score"]:.0f}',
                             watch=', '.join(f'{x["name"]} ({x["stress"]:.0f})' for x in top)))
        by = rs['by']
        bs = {b: rs['blocks'][b]['score'] for b in BW}
        gx = lambda k: by[k]['x'] if by[k]['status'] == 'ok' else None  # noqa
        a = rs['aux']
        trans = [
            dict(to='Risk-off', cond='VIX ≥ 25 или S&P 500 ниже 200-дн. средней на 3%+',
                 now=f'VIX {fmt("num", gx("vix"))}, S&P к 200-дн. {fmt("chg", a["s200"])}',
                 met=(gx('vix') or 0) >= 25 or (a['s200'] is not None and a['s200'] < -3)),
            dict(to='Кризис', cond='Credit ≥ 65 и (Liquidity ≥ 60 или Market ≥ 65)',
                 now=f'Credit {bs["credit"]:.0f}, Liquidity {bs["liq"]:.0f}, Market {bs["market"]:.0f}',
                 met=bs['credit'] >= 65 and (bs['liq'] >= 60 or bs['market'] >= 65)),
            dict(to='Высокий системный риск', cond='Индекс ≥ 70 при ≥ 2 блоках ≥ 60',
                 now=f'индекс {score:.0f}, блоков ≥ 60: {rs["n_conf"]}', met=score >= 70 and rs['n_conf'] >= 2),
            dict(to='Рецессия (цикл)', cond='≥ 3 из 5 макро-подтверждений',
                 now=f'подтверждено {cyc["n"]}', met=cyc['n'] is not None and cyc['n'] >= 3),
            dict(to='Смягчение', cond='Net Liquidity 4 нед. > 0, HY OAS 4 нед. < 0, индекс не растёт',
                 now=f'NetLiq {fmt("chg", gx("netliq4"))}, HY {fmt("bp", gx("hy4w"))}',
                 met=(gx('netliq4') or -1) > 0 and (gx('hy4w') or 1) < 0 and trend != 'up')]
        contrib = []
        for x in rs['ind']:
            if x['status'] == 'ok':
                wsum = sum(y['w'] for y in rs['ind'] if y['blk'] == x['blk'] and y['status'] == 'ok')
                contrib.append((BW[x['blk']] * x['w'] / wsum * (x['stress'] - 50), x))
        contrib.sort(key=lambda t: -abs(t[0]))
        factors = [dict(name=x['name'], disp=x['disp'], stress=x['stress'], blk=PRIOR[x['blk']], up=c > 0,
                        text=f'{x["name"]}: {x["disp"]} · стресс {x["stress"]:.0f}/100 относительно собственной истории')
                   for c, x in contrib[:3]]
        risk = band(score, rs['n_conf'])
        chk = []
        if score >= 70 and rs['n_conf'] >= 2 and min(v['level'] for v in modes_out.values() if v) < 2:
            chk.append('высокий риск, но режим не защитный')
        if reg['state'] == 'riskon' and score >= 55:
            chk.append('Risk-on при индексе ≥ 55')
        if cyc['state'] == 'recession' and eq < 2:
            chk.append('рецессия, но акции не сокращены')
        inv = dict(risk=risk, trend=trend, scenarios=scen, transitions=trans, modes=modes_out, factors=factors, consistency=chk)

    ok = rs['n_ok']
    headline = None
    if inv:
        tr = {'up': 'растёт', 'down': 'снижается', 'flat': 'без изменений'}[trend or 'flat']
        headline = (f'Экономика: {cyc["label"].lower()}. Рынок: {reg["label"]}. Стресс {score:.0f}/100 ({inv["risk"].lower()}, {tr}). '
                    f'Акции — {inv["modes"]["equity"]["label"]}' +
                    (f', BTC — {inv["modes"]["btc"]["label"]}' if inv['modes']['btc'] else '') + '.')
    quality = dict(ok=ok, stale=rs['n_stale'], missing=rs['n'] - ok - rs['n_stale'], n=rs['n'],
                   coverage=round((ok + rs['n_stale']) / rs['n'], 2), freshness=round(ok / max(1, ok + rs['n_stale']), 2),
                   problems=[dict(name=x['name'], status=x['status'], why=x.get('why') or f'возраст {x.get("age")} дн. > допуска {x.get("maxage")}')
                             for x in rs['ind'] if x['status'] != 'ok'],
                   errors=data.err)
    latest = dict(schema=3, generated_at=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), asof=today.isoformat(),
                  valid=rs['valid'], reason=rs['reason'], headline=headline,
                  score=None if not fin else dict(value=r1(score), band=inv['risk'] if inv else band(score, rs['n_conf']), delta=r1(delta), trend=trend,
                                                   n_conf=rs['n_conf'], base=r1(fin['base']), bonus_conf=fin['bonus_conf'], bonus_speed=fin['bonus_speed']),
                  blocks=blocks, netliq=nl_info(data, today), m2=m2_info(data, today),
                  cycle=cyc, regime=reg, invest=inv, crypto=cr, quality=quality,
                  indicators=[{k: x.get(k) for k in ('id', 'name', 'blk', 'status', 'disp', 'stress', 'pctl', 'obs', 'pub', 'age', 'maxage', 'src', 'url', 'series', 'why', 'hint')}
                              for x in rs['ind']])
    ds = [d for d in sorted(hist) if d >= today - TD(days=365)]
    nls = data.B.get('NETLIQ')

    def nlv(d):
        if nls is None:
            return None
        i = nls.idx(d)
        return round(nls.v[i - 1], 1) if i else None
    history = dict(dates=[d.isoformat() for d in ds],
                   score=[r1(hist[d]['value']) if hist[d] else None for d in ds],
                   **{b: [r1(light[d]['blocks'][b]['score']) for d in ds] for b in BW},
                   netliq=[nlv(d) for d in ds],
                   note='Ряд пересчитан по данным, опубликованным к каждой дате (оценка лага публикации). Ревизии не учтены.')
    return latest, history


def main():
    raw, err = fetch_all()
    if len(raw) < len(SRC) * 0.5:
        print('Слишком мало источников:', err, file=sys.stderr)
        sys.exit(1)
    live = live_crypto()
    latest, history = run(raw, err, live, dt.datetime.now(dt.timezone.utc).date())
    os.makedirs(OUT, exist_ok=True)
    json.dump(latest, open(os.path.join(OUT, 'latest.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(history, open(os.path.join(OUT, 'history.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('ok', latest['asof'], 'score', (latest['score'] or {}).get('value'), 'valid', latest['valid'], 'errors', len(err))
    for k, v in err.items():
        print('  ✗', k, v)


if __name__ == '__main__':
    main()
