#!/usr/bin/env python3
"""CRISIS ENGINE v3.0 — исторический бэктест (point-in-time).

Каждая дата оценивается только по наблюдениям, у которых оценка даты публикации (obs + лаг) ≤ даты оценки.
Запуск:  python backtest.py   →  BACKTEST.md + data/backtest.json
"""
import json, os, bisect
import datetime as dt
from engine import *  # noqa

ROOT = os.path.dirname(os.path.abspath(__file__))
# (название, начало окна, конец окна, порог падения для «момента падения»)
EVENTS = [('2008 · мировой кризис', '2007-06-01', '2009-06-30', .10),
          ('2011 · долговой кризис / даунгрейд США', '2011-03-01', '2011-12-31', .10),
          ('2018 · Q4 распродажа', '2018-06-01', '2019-01-31', .10),
          ('2020 · COVID', '2020-01-01', '2020-04-30', .10),
          ('2022 · медвежий рынок', '2021-12-01', '2022-12-31', .10),
          ('2024 · коррекция (carry-unwind)', '2024-06-01', '2024-08-15', .05),
          ('2025 · тарифный шок (вне ТЗ, для контроля)', '2025-01-15', '2025-05-01', .10)]
THRESHOLDS = (45, 55, 65)   # порог индекса для тревоги (+ ≥ 2 подтверждённых блока и 2 недели подряд)
FWD = (1, 3, 6)             # горизонты, мес.


def px_at(s, d):
    i = bisect.bisect_right(s.d, d)
    return s.v[i - 1] if i else None


def fwd(s, d, months):
    """(макс. просадка, доходность) на горизонте months; None, если окно ещё не завершилось."""
    end = d + TD(days=round(months * 30.4))
    if end > s.d[-1]:
        return None
    i0, i1 = bisect.bisect_right(s.d, d), bisect.bisect_right(s.d, end)
    if not i0 or i1 <= i0:
        return None
    p0 = s.v[i0 - 1]
    return min(s.v[i0:i1]) / p0 - 1, s.v[i1 - 1] / p0 - 1


def main():
    raw, err = fetch_all()
    if 'GSPC' not in raw:
        raise SystemExit('нет ряда S&P 500: ' + str(err))
    data = Data(raw, err)
    spx = data.B['GSPC']
    d0, d1 = D(2006, 1, 6), spx.d[-1]
    weeks = [d0 + TD(days=7 * k) for k in range((d1 - d0).days // 7 + 1)]  # пятницы
    rs = [compute(data, d, light=True) for d in weeks]
    sc, nc = [], []
    for i, r in enumerate(rs):
        f = finalize(r, rs[i - 4]['base'] if i >= 4 and rs[i - 4]['valid'] else None)
        sc.append(f['value'] if f else None)
        nc.append(r['n_conf'] if r['valid'] else 0)
    n_valid = sum(x is not None for x in sc)

    def alarms(th):
        raw_a = [s is not None and s >= th and nc[i] >= 2 for i, s in enumerate(sc)]
        return [raw_a[i] and i > 0 and raw_a[i - 1] for i in range(len(raw_a))]  # 2 недели подряд

    out = dict(generated=dt.date.today().isoformat(), weeks=len(weeks), valid_weeks=n_valid, events=[], sens={})
    lines = ['# CRISIS ENGINE v3.0 — исторический бэктест', '',
             f'Сгенерировано {dt.date.today().isoformat()} · выборка {weeks[0]} … {weeks[-1]} · еженедельно (пятницы).',
             f'Недель с рассчитанным индексом: **{n_valid} из {len(weeks)}** ({100 * n_valid / len(weeks):.0f}%); '
             'остальные — «недостаточно данных» (индекс не выдаётся).', '',
             '**Правило тревоги:** индекс ≥ порога **и** ≥ 2 блока ≥ 60 **и** условие выполнено 2 недели подряд.', '']
    for th in THRESHOLDS:
        al = alarms(th)
        lines += [f'## Порог {th}', '', '| Событие | Пик S&P | Падение | Первая тревога | До пика, дн. | До −' + '10%/−5% (момент падения), дн. | Итог |',
                  '|---|---|---|---|---|---|---|']
        det = 0
        evs = []
        for name, a, b, thr in EVENTS:
            w0, w1 = D.fromisoformat(a), D.fromisoformat(b)
            ids = [k for k, d in enumerate(spx.d) if w0 <= d <= w1]
            pk = max(ids, key=lambda k: spx.v[k])
            tr = min(range(pk, ids[-1] + 1), key=lambda k: spx.v[k])
            peak, trough = spx.d[pk], spx.d[tr]
            fall = next((spx.d[k] for k in range(pk, tr + 1) if spx.v[k] <= spx.v[pk] * (1 - thr)), trough)
            first = next((weeks[i] for i in range(len(weeks)) if al[i] and peak - TD(days=180) <= weeks[i] <= trough), None)
            dd = spx.v[tr] / spx.v[pk] - 1
            if first is None:
                res = 'пропущено'
            elif first <= fall:
                res = 'вовремя'; det += 1
            else:
                res = 'поздно (после начала падения)'
            evs.append(dict(name=name, peak=peak.isoformat(), trough=trough.isoformat(), dd=round(dd * 100, 1),
                            first=first.isoformat() if first else None, result=res,
                            lead_peak=(peak - first).days if first else None, lead_fall=(fall - first).days if first else None))
            lines.append(f'| {name} | {peak} | {dd * 100:.0f}% | {first or "—"} | {(peak - first).days if first else "—"} | '
                         f'{(fall - first).days if first else "—"} | {res} |')
        n_ev = len(evs)
        # ложные тревоги: эпизоды (разрыв > 8 недель) без падения ≥ 10% в следующие 6 мес.
        eps, cur = [], None
        for i, a_ in enumerate(al):
            if a_:
                if cur and i - cur[1] <= 8: cur[1] = i
                else:
                    if cur: eps.append(cur)
                    cur = [i, i]
        if cur: eps.append(cur)
        false, true, unk = [], 0, 0
        for s0, s1 in eps:
            f6 = fwd(spx, weeks[s0], 6)
            if f6 is None: unk += 1
            elif f6[0] <= -0.10: true += 1
            else: false.append((weeks[s0].isoformat(), weeks[s1].isoformat(), round(f6[0] * 100, 1)))
        lines += ['', f'Своевременно обнаружено: **{det} из {n_ev}**. Эпизодов тревоги: {len(eps)} '
                      f'(подтверждены падением ≥ 10% за 6 мес.: {true}; **ложных: {len(false)}**; не оценено: {unk}).']
        if false:
            lines += ['', 'Ложные тревоги (начало → конец, худшая просадка за 6 мес.): ' +
                      '; '.join(f'{a_} → {b_} ({d_}%)' for a_, b_, d_ in false)]
        lines += ['', '| Горизонт | Состояние | Недель | Ср. макс. просадка | Доля просадок ≥ 10% | Ср. доходность |', '|---|---|---|---|---|---|']
        sens = {}
        for h in FWD:
            for lab, cond in (('тревога', True), ('без тревоги', False)):
                xs = []
                for i, w in enumerate(weeks):
                    if sc[i] is None or al[i] != cond: continue
                    f = fwd(spx, w, h)
                    if f: xs.append(f)
                if xs:
                    row = (len(xs), sum(x[0] for x in xs) / len(xs) * 100, 100 * sum(x[0] <= -0.10 for x in xs) / len(xs),
                           sum(x[1] for x in xs) / len(xs) * 100)
                    lines.append(f'| {h} мес. | {lab} | {row[0]} | {row[1]:.1f}% | {row[2]:.0f}% | {row[3]:+.1f}% |')
                    sens[f'{h}m_{lab}'] = row
        lines.append('')
        out['sens'][str(th)] = dict(detected=det, events=n_ev, episodes=len(eps), false=len(false), unknown=unk, stats=sens)
        if th == THRESHOLDS[1]:
            out['events'] = evs
    lines += ['## Ограничения (честно)', '',
              '- Даты публикации — **оценка** (дата наблюдения + типичный лаг релиза), а не реальные vintage-даты ALFRED. Ревизии M2, GDP, заявок, CPI не учтены.',
              '- ISM лицензирован — вместо него Philly Fed; ширина рынка — прокси RSP/SPY и IWM/QQQ, а не % акций выше 50-дн. средней.',
              '- ICE BofA OAS на FRED может иметь ограниченную историю; тогда перцентили кредитного блока считаются по доступному окну, а для ранних лет опорой служат Baa−10Y, NFCI, SLOOS. Недельная доля «нет данных» выше выведена в начале файла.',
              '- Крипторынок в бэктест не входит (ETF-потоки и ликвидации не имеют бесплатного первоисточника), индекс стресса считается по макро/ликвидности/кредиту/рынку.',
              '- Выборка — 6–7 событий: любая статистика здесь описательная. **Калибровка вероятностей не проводилась, проценты в интерфейсе не показываются.**', '']
    open(os.path.join(ROOT, 'BACKTEST.md'), 'w', encoding='utf-8').write('\n'.join(lines))
    os.makedirs(os.path.join(ROOT, 'data'), exist_ok=True)
    json.dump(out, open(os.path.join(ROOT, 'data', 'backtest.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print('BACKTEST.md записан; валидных недель', n_valid, 'из', len(weeks))


if __name__ == '__main__':
    main()
