#!/usr/bin/env python3
"""CRISIS ENGINE v3.4 — сборщик. Запускается по расписанию (GitHub Actions), пишет data/*.json."""
import json, os, sys, time, csv, io, math, random, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import engine as eng
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


def _calendar_change(s, end, *, days):
    """Measured at the intended date rather than a fixed number of rows."""
    j = eng.calendar_prior_index(s.d, end, days=days, tolerance=3)
    if j is None or s.v[j] == 0:
        return dict(usd=None, pct=None)
    return dict(usd=round(s.v[end]-s.v[j],1),
                pct=round((s.v[end]/s.v[j]-1)*100,2))


def nl_info(data, asof):
    s = data.B.get('NETLIQ')
    if s is None:
        return None
    i = s.idx(asof)
    if not i:
        return None
    k = i-1
    age = (asof-s.d[k]).days
    out = dict(value=round(s.v[k],1), unit='млрд $',
               obs=s.d[k].isoformat(), pub=s.a[k].isoformat(), stale=age>15,
               d1=_calendar_change(s,k,days=7),
               d4=_calendar_change(s,k,days=28),
               d12=_calendar_change(s,k,days=84))
    w = last(data.B.get('WALCL'),asof,15)
    t = last(data.B.get(data.tga_key),asof,15)
    r = last(data.B.get('RRPONTSYD'),asof,15)
    if w is not None and t is not None:
        out.update(walcl=round(w/1000),tga=round(t/1000),
                   rrp=round(r) if r is not None else None)
    return out


def m2_info(data, asof):
    s = data.B.get('M2SL')
    if s is None:
        return None
    i = s.idx(asof)
    if not i:
        return None
    k = i-1
    j3 = eng.calendar_prior_index(s.d,k,months=3)
    j12 = eng.calendar_prior_index(s.d,k,months=12)
    def change(j):
        if j is None or s.v[j] <= 0:
            return None,None
        return round(s.v[k]-s.v[j]),round((s.v[k]/s.v[j]-1)*100,2)
    chg3, pct3 = change(j3)
    _, yoy = change(j12)
    return dict(value=round(s.v[k]),unit='млрд $',obs=s.d[k].isoformat(),
                chg3_usd=chg3,chg3_pct=pct3,
                ann3=round(((s.v[k]/s.v[j3])**4-1)*100,2) if j3 is not None and s.v[j3]>0 else None,
                yoy=yoy)


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
        for name, b1, b2 in (('Рынок и деньги', 'market', 'liq'), ('Кредит и деньги', 'credit', 'liq'), ('Работа и кредиты', 'macro', 'credit'), ('Экономика и деньги', 'macro', 'liq')):
            s = (rs['blocks'][b1]['score'] + rs['blocks'][b2]['score']) / 2
            dl = [blocks[b]['delta'] for b in (b1, b2) if blocks[b]['delta'] is not None]
            d = sum(dl) / len(dl) if dl else 0
            lean = ('напряжение растёт' if d >= 5 else 'напряжение снижается' if d <= -5 else 'пока без явного изменения')
            top = sorted([x for x in rs['ind'] if x['blk'] in (b1, b2) and x['status'] == 'ok'], key=lambda x: -x['stress'])[:2]
            scen.append(dict(h=name, lean=lean, drivers=f'{PRIOR[b1]} {rs["blocks"][b1]["score"]:.0f}, {PRIOR[b2]} {rs["blocks"][b2]["score"]:.0f}',
                             watch=', '.join(f'{x["name"]} ({x["stress"]:.0f})' for x in top)))
        by = rs['by']
        bs = {b: rs['blocks'][b]['score'] for b in BW}
        gx = lambda k: by[k]['x'] if by[k]['status'] == 'ok' else None  # noqa
        a = rs['aux']
        flags = reg.get('checks', {})
        trans = [
            dict(to='Рынок Risk-off',
                 cond='Есть рыночное подтверждение (VIX ≥ 25, S&P ниже средней на 3% или просадка ≥ 10%) и Market Stress ≥ 55 либо VIX ≥ 30/просадка ≥ 10%',
                 now=f'VIX {fmt("num", gx("vix"))}, S&P к 200-дн. {fmt("chg", a["s200"])}, Market {bs["market"]:.0f}',
                 met=flags.get('riskoff',False)),
            dict(to='Системный стресс (не диагноз кризиса)',
                 cond='Risk-off, Credit ≥ 65, Liquidity ≥ 60 или Market ≥ 65; при этом просадка ≥ 15% или VIX ≥ 35',
                 now=f'Credit {bs["credit"]:.0f}, Liquidity {bs["liq"]:.0f}, Market {bs["market"]:.0f}',
                 met=flags.get('systemic',False)),
            dict(to='Высокий стресс-индекс',cond='Индекс ≥ 70 и минимум 2 блока ≥ 60',
                 now=f'индекс {score:.0f}, блоков ≥ 60: {rs["n_conf"]}',
                 met=score>=70 and rs['n_conf']>=2),
            dict(to='Сильные макросигналы спада',cond='≥ 3 подтверждения из 4; не официальная рецессия',
                 now=f'подтверждено {cyc["n"]} из {cyc.get("known",0)} доступных',
                 met=cyc['n'] is not None and cyc['n']>=3),
            dict(to='Смягчение напряжения',
                 cond='Net Liquidity за 4 недели > 0, HY OAS за 4 недели < 0, индекс не растёт',
                 now=f'Net Liquidity {fmt("chg", gx("netliq4"))}, HY {fmt("bp", gx("hy4w"))}',
                 met=(gx('netliq4') is not None and gx('netliq4')>0 and
                      gx('hy4w') is not None and gx('hy4w')<0 and trend in ('flat','down')))]
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
            chk.append('сильные сигналы замедления и нейтральная модель рисков')
        inv = dict(risk=risk, trend=trend, scenarios=scen, transitions=trans, modes=modes_out, factors=factors, consistency=chk)

    ok = rs['n_ok']
    headline = None
    if inv:
        tr = {'up': 'растёт', 'down': 'снижается', 'flat': 'без изменений'}[trend or 'flat']
        headline = (f'Макросигналы: {cyc["label"].lower()}. Рынок: {reg["label"]}. '
                    f'Модельный финансовый стресс {score:.0f}/100 ({inv["risk"].lower()}, {tr}). '
                    'Это оценка состояния, не прогноз и не торговый приказ.')
    quality = dict(ok=ok, stale=rs['n_stale'], missing=rs['n'] - ok - rs['n_stale'], n=rs['n'],
                   coverage=round((ok + rs['n_stale']) / rs['n'], 2), freshness=round(ok / max(1, ok + rs['n_stale']), 2),
                   problems=[dict(name=x['name'], status=x['status'], why=x.get('why') or f'возраст {x.get("age")} дн. > допуска {x.get("maxage")}')
                             for x in rs['ind'] if x['w'] > 0 and x['status'] != 'ok'],
                   errors=data.err)
    latest = dict(schema=3, model_version='3.4', generated_at=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), asof=today.isoformat(),
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
    history = dict(model_version='3.4', dates=[d.isoformat() for d in ds],
                   score=[r1(hist[d]['value']) if hist[d] else None for d in ds],
                   **{b: [r1(light[d]['blocks'][b]['score']) for d in ds] for b in BW},
                   netliq=[nlv(d) for d in ds],
                   note='Исторические ряды пересчитаны сегодня по пересмотренным значениям; календарь публикаций приближённый. НЕ является честным историческим бэктестом.')
    return latest, history


# ───── Durable network adapters (keep engine.py calculations untouched) ─────
CACHE_FILE = os.path.join(OUT, 'source_cache.json')
FRED_KEY = os.environ.get('FRED_API_KEY', '').strip()
USER_AGENT = 'CrisisEngine/3.1 (+https://github.com/limonfree/crisis)'


def _download(url, timeout=13, retries=2):
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                'Accept': 'application/json,text/csv,*/*'})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.7 * (2 ** attempt) + random.uniform(0, .5))
    raise last_error


def _parse_csv(payload, sid):
    rows = csv.reader(io.StringIO(payload.decode('utf-8-sig', 'replace')))
    header = next(rows, [])
    # FRED may return DATE,SID or observation_date,SID.
    if len(header) < 2:
        raise ValueError(f'{sid}: invalid CSV header')
    dates, values = [], []
    for row in rows:
        if len(row) < 2 or row[1] in ('', '.', 'NaN'):
            continue
        try:
            day, value = eng.D.fromisoformat(row[0]), float(row[1])
            if math.isfinite(value):
                dates.append(day); values.append(value)
        except (ValueError, TypeError):
            continue
    if len(dates) < 2:
        raise ValueError(f'{sid}: CSV contains insufficient valid observations')
    return dates, values


def _fred_api(sid):
    params = urllib.parse.urlencode({'series_id': sid, 'api_key': FRED_KEY,
                                      'file_type': 'json', 'observation_start': '1990-01-01'})
    payload = json.loads(_download('https://api.stlouisfed.org/fred/series/observations?' + params))
    dates, values = [], []
    for item in payload.get('observations', []):
        try:
            day, value = eng.D.fromisoformat(item['date']), float(item['value'])
            if math.isfinite(value):
                dates.append(day); values.append(value)
        except (ValueError, TypeError, KeyError):
            pass
    if len(dates) < 2:
        raise ValueError(f'{sid}: FRED API returned insufficient observations')
    return dates, values


def _dbnomics(sid):
    # DBnomics mirrors FRED series; not an independent measurement.
    # FRED data is attributed to the original provider by engine.SRC.
    url = f'https://api.db.nomics.world/v22/series/FRED/{urllib.parse.quote(sid)}?observations=1'
    data = json.loads(_download(url))
    docs = data.get('series', {}).get('docs', [])
    if not docs:
        raise ValueError(f'{sid}: DBnomics series not found')
    record = docs[0]
    dates, values = [], []
    for stamp, value in zip(record.get('period', []), record.get('value', [])):
        try:
            day = eng.D.fromisoformat(str(stamp)[:10])
            val = float(value)
            if math.isfinite(val):
                dates.append(day); values.append(val)
        except (ValueError, TypeError):
            pass
    if len(dates) < 2:
        raise ValueError(f'{sid}: DBnomics returned insufficient observations')
    return dates, values


def _fred_robust(sid):
    errors = []
    providers = ([('FRED API', lambda: _fred_api(sid))] if FRED_KEY else []) + [
        ('DBnomics', lambda: _dbnomics(sid)),
        ('FRED CSV', lambda: _parse_csv(_download(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?' +
            urllib.parse.urlencode({'id': sid, 'cosd': '1990-01-01'}),
            timeout=9, retries=1), sid))]
    for name, fn in providers:
        try:
            return fn()
        except Exception as exc:
            errors.append(f'{name}: {type(exc).__name__}: {str(exc)[:65]}')
    raise RuntimeError('; '.join(errors))


def _load_cache():
    try:
        with open(CACHE_FILE, encoding='utf-8') as handle:
            obj = json.load(handle)
        if obj.get('schema') == 1 and isinstance(obj.get('series'), dict):
            return obj['series']
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _cache_valid(key, entry):
    try:
        if not isinstance(entry, dict) or not isinstance(entry.get('dates'), list):
            return False
        dates, values = entry['dates'], entry['values']
        if len(dates) != len(values) or len(dates) < 2:
            return False
        last_success = eng.D.fromisoformat(entry['fetched_at'][:10])
        freq = eng.SRC[key][2]
        # Recent successful retrieval only; source observations can be older
        # according to their normal frequency + publication lag.
        max_cache_days = {'D': 3, 'W': 10, 'M': 40, 'Q': 100}[freq]
        age = (eng.dt.datetime.now(eng.dt.timezone.utc).date() - last_success).days
        return 0 <= age <= max_cache_days
    except (KeyError, TypeError, ValueError):
        return False


def _series_from_cache(key, entry):
    dates = [eng.D.fromisoformat(x) for x in entry['dates']]
    vals = [float(x) for x in entry['values']]
    if not all(math.isfinite(v) for v in vals):
        raise ValueError('non-finite cached values')
    d, v = eng._clean(dates, vals)
    return eng.Ser(d, v, lag=eng.SRC[key][3])


def _fetch_key(key):
    kind, sid, freq, lag, _ = eng.SRC[key]
    if kind == 'fred':
        dates, values = _fred_robust(sid)
    elif kind == 'yahoo':
        dates, values = eng.yahoo(sid)
    else:
        dates, values = eng.llama_stables()
    dates, values = eng._clean(dates, values)
    if len(dates) < 2 or not all(math.isfinite(x) for x in values):
        raise ValueError('empty / invalid source series')
    return eng.Ser(dates, values, lag=lag)


def fetch_with_cache():
    old = _load_cache()
    new = dict(old)
    raw, errors = {}, {}
    # Lower FRED concurrency; independent sources can still finish.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_key, key): key for key in eng.SRC}
        for future in as_completed(futures):
            key = futures[future]
            try:
                series = future.result()
                raw[key] = series
                new[key] = {'fetched_at': eng.dt.datetime.now(eng.dt.timezone.utc).isoformat(),
                            'dates': [day.isoformat() for day in series.d],
                            'values': series.v}
                print(f'OK {key}: {len(series.d)} points, last={series.d[-1]}', flush=True)
            except Exception as exc:
                reason = f'{type(exc).__name__}: {str(exc)[:250]}'
                entry = old.get(key)
                if entry is not None and _cache_valid(key, entry):
                    try:
                        raw[key] = _series_from_cache(key, entry)
                        errors[key] = f'CACHE FALLBACK: {reason}'
                        print(f'CACHE {key}: {reason}', flush=True)
                        continue
                    except (KeyError, ValueError, TypeError) as cache_exc:
                        reason += f'; bad cache: {cache_exc}'
                errors[key] = reason
                print(f'FAILED {key}: {reason}', flush=True)
    return raw, errors, new


def _write_json_atomic(filename, obj):
    import tempfile
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', suffix='.json', dir=os.path.dirname(filename))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(obj, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            handle.write('\n')
        os.replace(tmp, filename)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    os.makedirs(OUT, exist_ok=True)
    raw, err, new_cache = fetch_with_cache()
    fred_count = sum(k in raw for k in SRC if SRC[k][0] == 'fred')
    print(f'Data coverage: {len(raw)}/{len(SRC)}, FRED: {fred_count}/23', flush=True)
    # Store only verified real measurements. Cache may aid later scheduled runs.
    if new_cache != _load_cache():
        _write_json_atomic(CACHE_FILE, {'schema': 1, 'series': new_cache})
    if len(raw) < len(SRC) * .5:
        print('Недостаточно источников. Предыдущие latest/history сохранены.', file=sys.stderr)
        for key, reason in err.items():
            print(f'  {key}: {reason}', file=sys.stderr)
        sys.exit(1)
    live = live_crypto()
    latest, history = run(raw, err, live, dt.datetime.now(dt.timezone.utc).date())
    if not latest.get('valid') or latest.get('score') is None:
        print('Недостоверный или неполный индекс. Последние опубликованные файлы сохранены.', file=sys.stderr)
        print('Причина:', latest.get('reason'), file=sys.stderr)
        sys.exit(1)
    _write_json_atomic(os.path.join(OUT, 'latest.json'), latest)
    _write_json_atomic(os.path.join(OUT, 'history.json'), history)
    print('SUCCESS', latest['asof'], 'score', latest['score']['value'],
          'valid', latest['valid'], 'source errors', len(err))


if __name__ == '__main__':
    main()
