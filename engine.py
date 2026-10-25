#!/usr/bin/env python3
"""CRISIS ENGINE v3.2 — validated economic risk engine.

Загрузка первичных данных и расчёт индексов. Только стандартная библиотека.
Одна и та же функция compute(data, asof) считает и «сегодня», и любую дату в прошлом
(только по данным, опубликованным к этой дате) — на ней же построены история и бэктест.
"""
import bisect, csv, io, json, time, urllib.parse, urllib.request
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

D, TD = dt.date, dt.timedelta
UA = {"User-Agent": "Mozilla/5.0 (compatible; crisis-engine/3.0)"}


def http(url, timeout=30, tries=3):
    err = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa
            err = e
            time.sleep(1.5 * (k + 1))
    raise err


# ───────────────────────── ряды ─────────────────────────
class Ser:
    """d — даты наблюдений, a — оценка даты публикации (d + лаг), v — значения."""
    def __init__(self, d, v, lag=0, a=None):
        self.d, self.v = d, v
        self.a = a if a is not None else [x + TD(days=lag) for x in d]

    def idx(self, asof):
        return bisect.bisect_right(self.a, asof)

    def map(self, f):
        d, v, a = [], [], []
        for i in range(len(self.v)):
            y = f(self.v, i)
            if y is not None:
                d.append(self.d[i]); v.append(y); a.append(self.a[i])
        return Ser(d, v, a=a)


def _clean(d, v):
    out = {}
    for x, y in zip(d, v):
        out[x] = y
    ks = sorted(out)
    return ks, [out[k] for k in ks]


def pct(n):   return lambda v, i: (v[i] / v[i - n] - 1) * 100 if i >= n and v[i - n] else None
def diff(n):  return lambda v, i: v[i] - v[i - n] if i >= n else None
def lvl():    return lambda v, i: v[i]
def ann3():   return lambda v, i: ((v[i] / v[i - 3]) ** 4 - 1) * 100 if i >= 3 and v[i - 3] > 0 else None
def dd(n):    return lambda v, i: (v[i] / max(v[max(0, i - n + 1):i + 1]) - 1) * 100 if i >= n - 1 else None
def wmin(n):  return lambda v, i: min(v[i - n + 1:i + 1]) if i >= n - 1 else None
def sma_gap(n): return lambda v, i: (v[i] / (sum(v[i - n + 1:i + 1]) / n) - 1) * 100 if i >= n - 1 else None
def ma_vs_min(m, n):
    def f(v, i):
        if i < n - 1:
            return None
        lo = min(v[i - n + 1:i + 1])
        return ((sum(v[i - m + 1:i + 1]) / m) / lo - 1) * 100 if lo > 0 else None
    return f


def ratio(a, b):
    mb, ab = dict(zip(b.d, b.v)), dict(zip(b.d, b.a))
    d, v, ar = [], [], []
    for x, y, aa in zip(a.d, a.v, a.a):
        z = mb.get(x)
        if z:
            d.append(x); v.append(y / z); ar.append(max(aa, ab[x]))
    return Ser(d, v, a=ar)


RRP_START = D(2013, 9, 23)  # до запуска facility ON RRP = 0 (это факт отсутствия программы, а не «нет данных»)


def netliq(walcl, tga, rrp):
    """Net Liquidity = WALCL − TGA − ON RRP, $ млрд.
    Единицы: WALCL и TGA — млн $, ON RRP — млрд $ (поэтому ×1000)."""
    d, v, a = [], [], []
    for x, w, aa in zip(walcl.d, walcl.v, walcl.a):
        j = bisect.bisect_right(tga.d, x)
        if not j or (x - tga.d[j - 1]).days > 7:
            continue
        r, ar = 0.0, aa
        if x >= RRP_START:
            if rrp is None:
                continue
            k = bisect.bisect_right(rrp.d, x)
            if not k or (x - rrp.d[k - 1]).days > 7:
                continue
            r, ar = rrp.v[k - 1] * 1000, rrp.a[k - 1]
        d.append(x); v.append((w - tga.v[j - 1] - r) / 1000); a.append(max(aa, tga.a[j - 1], ar))
    return Ser(d, v, a=a)


# ───────────────────────── источники ─────────────────────────
# ключ: (провайдер, id, частота D/W/M/Q, лаг публикации в днях, подпись первоисточника)
SRC = {
    'WALCL': ('fred', 'WALCL', 'W', 1, 'ФРС · баланс (H.4.1)'),
    'WDTGAL': ('fred', 'WDTGAL', 'W', 1, 'Минфин США · счёт TGA (H.4.1)'),
    'WTREGEN': ('fred', 'WTREGEN', 'W', 1, 'Минфин США · TGA, средн. за неделю'),
    'RRPONTSYD': ('fred', 'RRPONTSYD', 'D', 1, 'ФРБ Нью-Йорка · ON RRP'),
    'M2SL': ('fred', 'M2SL', 'M', 52, 'ФРС · денежная масса M2'),
    'ECBASSETSW': ('fred', 'ECBASSETSW', 'W', 5, 'ЕЦБ · активы Евросистемы'),
    'JPNASSETS': ('fred', 'JPNASSETS', 'M', 35, 'Банк Японии · активы'),
    'HY': ('fred', 'BAMLH0A0HYM2', 'D', 1, 'ICE BofA · HY OAS'),
    'CCC': ('fred', 'BAMLH0A3HYC', 'D', 1, 'ICE BofA · CCC OAS'),
    'BBB': ('fred', 'BAMLC0A4CBBB', 'D', 1, 'ICE BofA · BBB OAS'),
    'BAA': ('fred', 'BAA10Y', 'D', 1, "Moody's · Baa − 10Y UST"),
    'NFCI': ('fred', 'NFCI', 'W', 5, 'ФРБ Чикаго · NFCI'),
    'SLOOS': ('fred', 'DRTSCIS', 'Q', 35, 'ФРС · опрос SLOOS (C&I)'),
    'VIX': ('fred', 'VIXCLS', 'D', 1, 'CBOE · VIX'),
    'SAHM': ('fred', 'SAHMREALTIME', 'M', 34, 'ФРБ Сент-Луиса · Sahm Rule'),
    'UNRATE': ('fred', 'UNRATE', 'M', 34, 'BLS · безработица'),
    'ICSA': ('fred', 'ICSA', 'W', 5, 'Минтруд США · первичные заявки'),
    'GDPNOW': ('fred', 'GDPNOW', 'W', 1, 'ФРБ Атланты · GDPNow (дата FRED не является временем публикации прогноза)'),
    'PHILLY': ('fred', 'GACDFSA066MSFRBPHI', 'M', 15, 'ФРБ Филадельфии · произв. индекс'),
    'CPI': ('fred', 'CPIAUCSL', 'M', 44, 'BLS · CPI-U, сезонно скорректированный ряд'),
    'PCE': ('fred', 'PCEPILFE', 'M', 58, 'BEA · core PCE'),
    'DGS10': ('fred', 'DGS10', 'D', 1, 'Минфин США · доходность 10Y'),
    'CURVE': ('fred', 'T10Y2Y', 'D', 1, 'Минфин США · кривая 10Y−2Y'),
    'GSPC': ('yahoo', '^GSPC', 'D', 0, 'S&P 500'),
    'MOVE': ('yahoo', '^MOVE', 'D', 0, 'ICE BofA · MOVE'),
    'SPY': ('yahoo', 'SPY', 'D', 0, 'SPY'),
    'RSP': ('yahoo', 'RSP', 'D', 0, 'RSP'),
    'QQQ': ('yahoo', 'QQQ', 'D', 0, 'QQQ'),
    'IWM': ('yahoo', 'IWM', 'D', 0, 'IWM'),
    'KRE': ('yahoo', 'KRE', 'D', 0, 'KRE · региональные банки'),
    'HG': ('yahoo', 'HG=F', 'D', 0, 'Медь (фьючерс)'),
    'GC': ('yahoo', 'GC=F', 'D', 0, 'Золото (фьючерс)'),
    'DXY': ('yahoo', 'DX-Y.NYB', 'D', 0, 'ICE · индекс доллара DXY'),
    'JPY': ('yahoo', 'JPY=X', 'D', 0, 'USD/JPY'),
    'BTC': ('yahoo', 'BTC-USD', 'D', 0, 'BTC/USD'),
    'ETH': ('yahoo', 'ETH-USD', 'D', 0, 'ETH/USD'),
    'STABLE': ('llama', None, 'D', 1, 'DefiLlama · предложение стейблкоинов'),
}
URL = {'fred': 'https://fred.stlouisfed.org/series/%s', 'yahoo': 'https://finance.yahoo.com/quote/%s',
       'llama': 'https://defillama.com/stablecoins'}
MAXAGE = {'GDPNOW': 25}  # особые случаи: обновляется нерегулярно
# частота → окно истории (в наблюдениях), шаг выборки для перцентиля, минимум истории, допуск
FQ = {'D': dict(per=1, grace=5, win=3900, stride=5, minn=130),
      'W': dict(per=7, grace=7, win=780, stride=1, minn=130),
      'M': dict(per=31, grace=10, win=180, stride=1, minn=36),
      'Q': dict(per=92, grace=20, win=60, stride=1, minn=24)}


def fred(sid):
    raw = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=1990-01-01").decode("utf-8", "ignore")
    rows = csv.reader(io.StringIO(raw)); next(rows, None)
    d, v = [], []
    for r in rows:
        if len(r) > 1 and r[1] not in ("", "."):
            try:
                d.append(D.fromisoformat(r[0])); v.append(float(r[1]))
            except ValueError:
                pass
    if not d:
        raise ValueError("пустой ряд " + sid)
    return d, v


def yahoo(sym):
    p1, p2 = int(dt.datetime(2000, 1, 1).timestamp()), int(time.time()) + 86400
    j = json.loads(http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                        f"?period1={p1}&period2={p2}&interval=1d"))
    r = j["chart"]["result"][0]
    d, v = [], []
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c is not None:
            d.append(dt.datetime.fromtimestamp(t, dt.timezone.utc).date()); v.append(float(c))
    if not d:
        raise ValueError("пустой ряд " + sym)
    return d, v


def llama_stables():
    j = json.loads(http("https://stablecoins.llama.fi/stablecoincharts/all"))
    d, v = [], []
    for p in j:
        tot = (p.get("totalCirculatingUSD") or p.get("totalCirculating") or {}).get("peggedUSD")
        if tot:
            d.append(dt.datetime.fromtimestamp(int(p["date"]), dt.timezone.utc).date()); v.append(float(tot))
    if not d:
        raise ValueError("пустой ответ DefiLlama")
    return d, v


def live_crypto():
    """Funding и open interest — только «живые» значения (истории для перцентилей нет)."""
    out = {}
    try:
        j = json.loads(http("https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100"))
        r = [float(x["fundingRate"]) for x in j["data"]]
        out["funding"], out["funding_avg"] = r[0], sum(r) / len(r)
        out["funding_ts"] = int(j["data"][0]["fundingTime"]) // 1000
    except Exception as e:  # noqa
        out["funding_err"] = str(e)[:80]
    try:
        j = json.loads(http("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1D"))
        rows = j["data"]
        out["oi_chg7"] = (float(rows[0][1]) / float(rows[7][1]) - 1) * 100
        out["oi_ts"] = int(rows[0][0]) // 1000
    except Exception as e:  # noqa
        out["oi_err"] = str(e)[:80]
    return out


def _load_one(key):
    kind, sid, freq, lag, _ = SRC[key]
    d, v = {'fred': lambda: fred(sid), 'yahoo': lambda: yahoo(sid), 'llama': llama_stables}[kind]()
    return key, Ser(*_clean(d, v), lag=lag)


def fetch_all():
    raw, err = {}, {}
    # Do not flood a rate-limited upstream with eight simultaneous connections.
    with ThreadPoolExecutor(3) as ex:
        futs = {k: ex.submit(_load_one, k) for k in SRC}
        for k, f in futs.items():
            try:
                raw[k] = f.result()[1]
            except Exception as e:  # noqa
                err[k] = f"{type(e).__name__}: {str(e)[:90]}"
    return raw, err


# ───────────────────────── индикаторы ─────────────────────────
BW = {'liq': .25, 'credit': .30, 'market': .25, 'macro': .20}
BNAME = {'liq': 'Liquidity Stress', 'credit': 'Credit Stress', 'market': 'Market Stress', 'macro': 'Macro Stress'}


def I(id, name, blk, base, tf, inv=False, w=1.0, fmt='num', shown='x', fl=(), hint=''):
    return dict(id=id, name=name, blk=blk, base=base, tf=tf, inv=inv, w=w, fmt=fmt, shown=shown, fl=fl, hint=hint)


IND = [
    # Ликвидность
    I('netliq4', 'Net Liquidity, 4 нед.', 'liq', 'NETLIQ', pct(4), True, 1.5, 'chg', hint='WALCL − TGA − ON RRP; падение = стресс'),
    I('netliq12', 'Net Liquidity, 12 нед.', 'liq', 'NETLIQ', pct(12), True, 1.0, 'chg'),
    I('m2yoy', 'M2, год к году', 'liq', 'M2SL', pct(12), True, 1.0, 'chg', hint='YoY — отдельно от 3-мес. темпа'),
    I('m2ann3', 'M2, 3 мес. (годовой темп)', 'liq', 'M2SL', ann3(), True, 0.7, 'chg'),
    I('ecb13', 'Активы ЕЦБ, 13 нед.', 'liq', 'ECBASSETSW', pct(13), True, 0.5, 'chg'),
    I('boj3', 'Активы Банка Японии, 3 мес.', 'liq', 'JPNASSETS', pct(3), True, 0.5, 'chg'),
    # Кредит
    I('hy', 'HY OAS', 'credit', 'HY', lvl(), False, 1.5, 'bp'),
    I('ccc', 'CCC OAS', 'credit', 'CCC', lvl(), False, 1.0, 'bp'),
    I('bbb', 'BBB OAS', 'credit', 'BBB', lvl(), False, 0.7, 'bp'),
    I('baa', 'Baa − 10Y UST', 'credit', 'BAA', lvl(), False, 0.7, 'bp', hint='длинная история: страховка, если ICE ограничила ряды'),
    I('hy4w', 'HY OAS, изменение 4 нед.', 'credit', 'HY', diff(20), False, 0.8, 'bp', hint='скорость ухудшения'),
    I('nfci', 'NFCI (фин. условия)', 'credit', 'NFCI', lvl(), False, 1.0, 'num2', fl=(('>', 0, 60),), hint='>0 — условия жёстче среднего'),
    I('sloos', 'SLOOS: банки ужесточают C&I, %', 'credit', 'SLOOS', lvl(), False, 0.8, 'num'),
    I('kre', 'Региональные банки (KRE), от макс. 6 мес.', 'credit', 'KRE', dd(125), True, 0.8, 'chg', hint='банковские проблемы'),
    # Рынок
    I('vix', 'VIX', 'market', 'VIX', lvl(), False, 1.5, 'num'),
    I('move', 'MOVE', 'market', 'MOVE', lvl(), False, 1.0, 'num'),
    I('spxdd', 'S&P 500 от макс. 52 нед.', 'market', 'GSPC', dd(252), True, 1.5, 'chg'),
    I('breadth', 'Ширина: RSP/SPY, 3 мес.', 'market', 'RSPSPY', pct(63), True, 0.8, 'chg', hint='прокси ширины: равные веса vs капитализация'),
    I('narrow', 'Узость: IWM/QQQ, 3 мес.', 'market', 'IWMQQQ', pct(63), True, 0.5, 'chg'),
    I('dxy', 'DXY, 3 мес.', 'market', 'DXY', pct(63), False, 0.7, 'chg'),
    I('usdjpy', 'USD/JPY, 4 нед.', 'market', 'JPY', pct(20), True, 0.7, 'chg', hint='укрепление иены = раскрутка carry'),
    I('cug', 'Медь/золото, 3 мес.', 'market', 'CUAU', pct(63), True, 0.7, 'chg'),
    # Макро
    I('sahm', 'Sahm Rule', 'macro', 'SAHM', lvl(), False, 1.5, 'num2', fl=(('>=', 0.5, 90),)),
    I('unrate', 'Безработица, 12 мес.', 'macro', 'UNRATE', diff(12), False, 1.0, 'pp'),
    I('claims', 'Первичные заявки: 4н ср. к мин. года', 'macro', 'ICSA', ma_vs_min(4, 52), False, 1.0, 'chg'),
    I('gdpnow', 'GDPNow, квартальный темп SAAR (справочно)', 'macro', 'GDPNOW', lvl(), True, 0.0, 'num', fl=(('<', 0, 80),)),
    I('philly', 'Philly Fed: индекс активности', 'macro', 'PHILLY', lvl(), True, 1.0, 'num', hint='Не равен ISM PMI; собственная шкала и состав'),
    I('curve', 'Кривая 10Y−2Y (мин. за 12 мес.)', 'macro', 'CURVE', wmin(260), True, 1.0, 'pp', shown='base'),
    I('cpi', 'CPI-U SA, г/г (не headline NSA)', 'macro', 'CPI', pct(12), False, 0.5, 'chg'),
    I('pce', 'Core PCE, г/г', 'macro', 'PCE', pct(12), False, 0.5, 'chg'),
    I('y10', 'Доходность 10Y, 3 мес.', 'macro', 'DGS10', diff(63), False, 0.5, 'pp'),
]
CRIT_ALL = ['vix', 'netliq4']  # критические для итогового индекса; + любой из hy/baa


def fmt(kind, v):
    if v is None:
        return '—'
    return {'chg': f'{v:+.1f}%', 'bp': f'{v * 100:.0f} б.п.', 'pp': f'{v:+.2f} п.п.',
            'num': f'{v:.1f}', 'num2': f'{v:.2f}'}[kind]


class Data:
    """Базовые ряды B, производные X (по одному на индикатор), метаданные M."""
    def __init__(self, raw, err=None):
        self.err = err or {}
        B, M = {}, {}
        for k, s in raw.items():
            kind, sid, freq, lag, label = SRC[k]
            B[k] = s
            M[k] = dict(freq=freq, lag=lag, label=label, url=URL[kind].replace('%s', sid or ''), id=sid or k, maxage=MAXAGE.get(k))
        self.tga_key = 'WDTGAL' if 'WDTGAL' in B else 'WTREGEN'
        tga = B.get(self.tga_key)
        if 'WALCL' in B and tga is not None:
            B['NETLIQ'] = netliq(B['WALCL'], tga, B.get('RRPONTSYD'))
            M['NETLIQ'] = dict(freq='W', lag=1, label='WALCL − TGA − ON RRP (ФРС, Минфин, ФРБ НЙ)',
                               url=M['WALCL']['url'], id='NETLIQ', maxage=None)
        for key, a, b, label in (('RSPSPY', 'RSP', 'SPY', 'RSP / SPY'), ('IWMQQQ', 'IWM', 'QQQ', 'IWM / QQQ'),
                                 ('CUAU', 'HG', 'GC', 'Медь / золото')):
            if a in B and b in B:
                B[key] = ratio(B[a], B[b])
                M[key] = dict(freq='D', lag=0, label=label, url=M[a]['url'], id=key, maxage=None)
        self.B, self.M = B, M
        self.X = {}
        for s in IND:
            if s['base'] in B:
                self.X[s['id']] = B[s['base']].map(s['tf'])
        g = B.get('GSPC')
        if g:
            self.X['aux_s200'] = g.map(sma_gap(200))
            self.X['aux_r20'] = g.map(pct(20))
            if 'spxdd' in self.X:
                self.X['aux_ddmin'] = self.X['spxdd'].map(wmin(120))
        for k, tf in (('btc_dd', ('BTC', dd(90))), ('btc200', ('BTC', sma_gap(200))), ('eth200', ('ETH', sma_gap(200))),
                      ('stab30', ('STABLE', pct(30)))):
            if tf[0] in B:
                self.X[k] = B[tf[0]].map(tf[1])


def pctile(w, x):
    lt = eq = 0
    for y in w:
        if y < x:
            lt += 1
        elif y == x:
            eq += 1
    return 100 * (lt + .5 * eq) / len(w)


def last(ser, asof, maxage=10):
    """Последнее доступное значение вспомогательного ряда (None, если пусто/устарело)."""
    if ser is None:
        return None
    i = ser.idx(asof)
    if not i or ser.d[i - 1] > asof or (asof - ser.d[i - 1]).days > maxage:
        return None
    return ser.v[i - 1]


def back(ser, asof, n):
    if ser is None:
        return None
    i = ser.idx(asof)
    return ser.v[i - 1 - n] if i - 1 - n >= 0 else None


def ev(data, s, asof):
    out = dict(id=s['id'], name=s['name'], blk=s['blk'], w=s['w'], status='missing', hint=s['hint'])
    m = data.M.get(s['base'])
    if m:
        out.update(src=m['label'], url=m['url'], series=m['id'])
    elif s['base'] in SRC:
        k = SRC[s['base']]
        out.update(src=k[4], url=URL[k[0]].replace('%s', k[1] or ''), series=k[1])
    X = data.X.get(s['id'])
    if X is None:
        out['why'] = data.err.get(s['base'], 'источник недоступен')
        return out
    # FRED GDPNOW series observation date denotes a reference period, not necessarily
    # the date when Atlanta Fed published a particular nowcast revision.
    # Using it as a live trading signal or historical point-in-time feature is unsafe.
    if s['id'] == 'gdpnow':
        out['why'] = 'исключён из скоринга: для GDPNow нужна история датированных релизов/vintage'
        return out
    i = X.idx(asof)
    if i == 0:
        out['why'] = 'нет данных на дату'
        return out
    obs, x = X.d[i - 1], X.v[i - 1]
    if obs > asof:
        out['why'] = 'дата наблюдения находится в будущем относительно расчёта'
        return out
    fq = FQ[m['freq']]
    age = (asof - obs).days
    out.update(obs=obs.isoformat(), pub=X.a[i - 1].isoformat(), age=age, x=x)
    shown = x
    if s['shown'] == 'base':
        shown = last(data.B[s['base']], asof, 10 ** 6)
    out['disp'] = fmt(s['fmt'], shown)
    # Reference distribution excludes the current observation, avoids self-ranking.
    # Historical dataset remains revised (not vintage), so no claim of true PIT backtest.
    w = X.v[max(0, i - 1 - fq['win'] * fq['stride']):i - 1][::-1][::fq['stride']]
    if len(w) < fq['minn']:
        out['why'] = 'мало истории для перцентиля'
        return out
    p = pctile(w, x)
    st = 100 - p if s['inv'] else p
    for op, thr, val in s['fl']:
        if (op == '>' and x > thr) or (op == '>=' and x >= thr) or (op == '<' and x < thr):
            st = max(st, val)
    maxage = m['maxage'] or fq['per'] + m['lag'] + fq['grace']
    out.update(status='stale' if age > maxage else 'ok', pctl=round(p), stress=round(st, 1), maxage=maxage)
    return out


def compute(data, asof, light=False):
    inds = [ev(data, s, asof) for s in IND]
    by = {x['id']: x for x in inds}
    blocks = {}
    for b in BW:
        L = [x for x in inds if x['blk'] == b]
        ok = [x for x in L if x['status'] == 'ok' and x['w'] > 0]
        ws = sum(x['w'] for x in ok)
        # GDPNow is informational until release timestamps are available.
        scored = [x for x in L if x['w'] > 0]
        cov = len(ok) / len(scored) if scored else 0
        valid = cov >= 0.6 and len(ok) >= 2 and ws > 0
        blocks[b] = dict(score=(sum(x['w'] * x['stress'] for x in ok) / ws) if valid else None, cov=cov,
                         ok=len(ok), n=len(L), valid=valid,
                         stale=sum(x['status'] == 'stale' for x in L))
    miss = [b for b in BW if not blocks[b]['valid']]
    okid = lambda k: by[k]['status'] == 'ok'  # noqa
    crit = [k for k in CRIT_ALL if not okid(k)]
    if not (okid('hy') or okid('baa')):
        crit.append('hy/baa')
    n_ok = sum(x['status'] == 'ok' for x in inds)
    reason = None
    if miss:
        reason = 'Недостаточно данных в блоках: ' + ', '.join(BNAME[b] for b in miss)
    elif crit:
        reason = 'Отсутствуют критические показатели: ' + ', '.join(crit)
    elif n_ok / sum(i['w'] > 0 for i in IND) < 0.7:
        reason = 'Покрытие данных ниже 70%'
    valid = reason is None
    rs = dict(asof=asof, ind=inds, by=by, blocks=blocks, valid=valid, reason=reason,
              n_ok=n_ok, n_stale=sum(x['status'] == 'stale' for x in inds), n=len(inds))
    if valid:
        rs['base'] = sum(BW[b] * blocks[b]['score'] for b in BW)
        rs['n_conf'] = sum(blocks[b]['score'] >= 60 for b in BW)
    if not light:
        rs['aux'] = dict(s200=last(data.X.get('aux_s200'), asof), r20=last(data.X.get('aux_r20'), asof),
                         ddmin=last(data.X.get('aux_ddmin'), asof), vix20=back(data.B.get('VIX'), asof, 20))
    return rs


def finalize(rs, base_prev):
    """Итоговый индекс: взвешенный блоки + надбавки за подтверждение и скорость (прозрачно)."""
    if not rs['valid']:
        return None
    bc = 5 if rs['n_conf'] >= 3 else 0
    sp = 5 if (base_prev is not None and rs['base'] - base_prev >= 10) else 0
    return dict(value=min(100.0, rs['base'] + bc + sp), base=rs['base'], bonus_conf=bc, bonus_speed=sp)


def band(score, n_conf):
    if score < 35: return 'Низкий'
    if score < 55: return 'Умеренный'
    if score < 70 or n_conf < 2: return 'Повышенный'
    return 'Высокий'


# ───────────────────────── цикл и режим ─────────────────────────
CYC = {'growth': 'Рост / без признаков спада', 'slowdown': 'Признаки замедления', 'risk': 'Повышенный риск спада', 'recession': 'Сильные сигналы спада (не официальная рецессия)', 'recovery': 'Улучшение после напряжения'}


def cycle(rs, prev_state=None):
    g = lambda k: rs['by'][k] if rs['by'][k]['status'] == 'ok' else None  # noqa
    sig = []

    def add(name, key, test):
        it = g(key)
        sig.append(dict(name=name, on=None if it is None else bool(test(it['x'])), val=it['disp'] if it else '—'))
    add('Sahm Rule ≥ 0.5', 'sahm', lambda x: x >= 0.5)
    add('Заявки: +20% к минимуму года', 'claims', lambda x: x >= 20)
    add('Безработица +0.5 п.п. за 12 мес.', 'unrate', lambda x: x >= 0.5)
    add('GDPNow (исключён: нужны датированные релизы)', 'gdpnow', lambda x: x < 0)
    add('Philly Fed < −10', 'philly', lambda x: x < -10)
    known = [s for s in sig if s['on'] is not None]
    if len(known) < 3:
        return dict(state=None, label='Недостаточно данных', signals=sig, n=None, why='доступно менее 3 из 5 макро-подтверждений')
    n = sum(s['on'] for s in known)
    sah = g('sahm')
    warn = bool(sah and 0.3 <= sah['x'] < 0.5)
    inv = bool(g('curve') and g('curve')['x'] < 0)
    soft = bool((g('gdpnow') and g('gdpnow')['x'] < 1) or (g('philly') and g('philly')['x'] < 0))
    if n >= 3:
        st, why = 'recession', f'{n} из {len(known)} сигналов указывают на спад; это не официальная датировка NBER'
    elif n == 2 or (n >= 1 and (warn or inv)):
        st, why = 'risk', f'{n} подтверждение' + ('; кривая инвертировалась за год' if inv else '') + ('; Sahm в зоне 0.3–0.5' if warn else '')
    elif prev_state in ('risk', 'recession') and n <= 1 and not soft:
        st, why = 'recovery', 'после риска рецессии сигналы ослабли'
    elif n == 1 or warn or soft:
        st, why = 'slowdown', 'отдельные признаки охлаждения без подтверждений'
    else:
        st, why = 'growth', 'макро-подтверждений нет'
    return dict(state=st, label=CYC[st], signals=sig, n=n, why=why)


REG = {'riskon': 'Risk-on', 'late': 'Late Risk-on', 'riskoff': 'Risk-off', 'correction': 'Коррекция',
       'crisis': 'Системный стресс', 'capitulation': 'Капитуляция', 'recovery': 'Восстановление', 'mixed': 'Нейтрально'}


def regime(rs, cyc):
    bl = {b: rs['blocks'][b]['score'] for b in BW}
    a, by = rs['aux'], rs['by']
    dd_ = by['spxdd']['x'] if by['spxdd']['status'] == 'ok' else None
    vix = by['vix']['x'] if by['vix']['status'] == 'ok' else None
    if dd_ is None or vix is None or a['s200'] is None:
        return dict(state=None, label='Недостаточно данных', type=None, why='нет S&P 500 / VIX')
    cr, lq, mk = bl['credit'], bl['liq'], bl['market']
    narrow = by['breadth']['stress'] if by['breadth']['status'] == 'ok' else 0
    if dd_ <= -15 and vix >= 40:
        st, why = 'capitulation', f'S&P {dd_:.0f}% от максимума, VIX {vix:.0f}'
    elif (cr >= 65 and (lq >= 60 or mk >= 65)) or (dd_ <= -20 and cr >= 55):
        st, why = 'crisis', f'Credit {cr:.0f} подтверждён ' + ('ликвидностью' if lq >= 60 else 'рынком')
    elif (a['ddmin'] is not None and a['ddmin'] <= -10 and dd_ > a['ddmin'] + 5 and (a['r20'] or 0) > 2
          and a['vix20'] is not None and vix < a['vix20']):
        st, why = 'recovery', f'отскок от просадки {a["ddmin"]:.0f}%, VIX снижается'
    elif dd_ <= -10:
        st, why = 'correction', f'S&P {dd_:.0f}% от максимума, кредит не подтверждает'
    elif mk >= 55 or a['s200'] < -3 or vix >= 25:
        st, why = 'riskoff', f'Market Stress {mk:.0f}, S&P {a["s200"]:+.0f}% к 200-дн. средней'
    elif dd_ > -5 and a['s200'] > 0 and (narrow >= 60 or cr >= 50 or lq >= 55 or (cyc['state'] in ('slowdown', 'risk', 'recession'))):
        st, why = 'late', 'рост у максимумов при ухудшающемся фоне (ширина/кредит/ликвидность/цикл)'
    elif mk < 45:
        st, why = 'riskon', f'Market Stress {mk:.0f}, тренд вверх'
    else:
        st, why = 'mixed', 'нет доминирующего сигнала'
    typ = None
    if st in ('crisis', 'riskoff', 'correction', 'capitulation'):
        if lq >= 60 and cr >= 60: typ = 'Одновременный стресс ликвидности и кредита'
        elif lq >= 60: typ = 'Повышенный стресс ликвидности'
        elif cr >= 60: typ = 'Повышенный кредитный стресс'
        else: typ = 'Рыночная волатильность / коррекция'
    return dict(state=st, label=REG[st], type=typ, why=why)


# ───────────────────────── крипторынок ─────────────────────────
LVL = ['накопление', 'нейтрально', 'сокращение риска', 'защитный режим']


def crypto(data, asof, live=None):
    X = data.X
    v = lambda k: last(X.get(k), asof, 7)  # noqa
    btc_dd, b200, e200, st30 = v('btc_dd'), v('btc200'), v('eth200'), v('stab30')
    items = []

    def it(name, val, disp, src, url, state=''):
        items.append(dict(name=name, disp=disp, ok=val is not None, src=src, url=url, state=state))
    it('BTC от макс. 90 дн.', btc_dd, fmt('chg', btc_dd) if btc_dd is not None else '—', 'Yahoo Finance', URL['yahoo'] % 'BTC-USD')
    it('BTC к 200-дн. средней', b200, fmt('chg', b200) if b200 is not None else '—', 'Yahoo Finance', URL['yahoo'] % 'BTC-USD')
    it('ETH к 200-дн. средней', e200, fmt('chg', e200) if e200 is not None else '—', 'Yahoo Finance', URL['yahoo'] % 'ETH-USD')
    it('Стейблкоины, 30 дн.', st30, fmt('chg', st30) if st30 is not None else '—', 'DefiLlama', URL['llama'])
    fund = oi = None
    if live:
        fund, oi = live.get('funding'), live.get('oi_chg7')
    it('Funding BTC (за 8 ч)', fund, f'{fund * 100:+.4f}%' if fund is not None else '—', 'OKX', 'https://www.okx.com/trade-swap/btc-usdt-swap',
       '' if fund is None else ('перегрев лонгов' if fund > 3e-4 else 'отрицательный' if fund < 0 else 'норма'))
    it('Open interest BTC, 7 дн.', oi, fmt('chg', oi) if oi is not None else '—', 'OKX', 'https://www.okx.com/trade-swap/btc-usdt-swap')
    it('ETF-потоки', None, 'нет источника', 'нужен платный API (CoinGlass/эмитенты)', 'https://www.coinglass.com')
    it('Ликвидации', None, 'нет источника', 'нужен платный API (CoinGlass)', 'https://www.coinglass.com')
    if btc_dd is None or b200 is None or st30 is None:
        return dict(items=items, lvl_btc=None, lvl_eth_adj=None, e200=e200, why='нет BTC / стейблкоинов')
    if btc_dd <= -35 and st30 < -1: lv = 3
    elif b200 < 0 or st30 < -1 or (fund is not None and fund > 3e-4): lv = 2
    elif b200 > 0 and st30 > 0 and not (fund is not None and fund > 2e-4): lv = 0
    else: lv = 1
    return dict(items=items, lvl_btc=lv, e200=e200, why='')


def modes(rs, score, cyc, reg, cr, trend):
    """Режимы для акций/BTC/ETH по проверяемым правилам. Уровни: 0 накопление … 3 защитный."""
    if score is None or cyc['state'] is None or reg['state'] is None:
        return None
    bl = {b: rs['blocks'][b]['score'] for b in BW}
    why = []
    if reg['state'] == 'capitulation' and bl['credit'] < 60 and bl['liq'] < 60:
        eq = 0; why.append('капитуляция без кредитного/ликвидностного подтверждения')
    elif reg['state'] == 'capitulation' and trend == 'down':
        eq = 1; why.append('капитуляция, стресс уже снижается')
    elif score >= 70 and rs['n_conf'] >= 2:
        eq = 3; why.append(f'индекс {score:.0f} ≥ 70 при {rs["n_conf"]} подтверждённых блоках')
    elif score >= 55 or reg['state'] in ('riskoff', 'crisis'):
        eq = 2; why.append(f'индекс {score:.0f} ≥ 55 или режим {reg["label"]}')
    elif score < 35 and reg['state'] in ('riskon', 'recovery'):
        eq = 0; why.append(f'индекс {score:.0f} < 35, режим {reg["label"]}')
    else:
        eq = 1; why.append('сигналы без выраженного перекоса')
    floor = {'recession': 2, 'risk': 1}.get(cyc['state'], 0)
    if floor > eq:
        eq = floor; why.append(f'цикл: {cyc["label"]}')
    return dict(eq=eq, eq_why='; '.join(why), liq_hi=bl['liq'] >= 60)


def explain(rs, prev, blk):
    """Что сдвинуло блок за 4 недели: топ-2 индикатора по изменению стресса."""
    mv = []
    for x in rs['ind']:
        if x['blk'] != blk or x['status'] != 'ok':
            continue
        p = prev['by'].get(x['id'])
        if p and p['status'] == 'ok':
            mv.append((x['stress'] - p['stress'], x['name']))
    mv.sort(key=lambda t: -abs(t[0]))
    return '; '.join(f'{n} {d:+.0f}' for d, n in mv[:2] if abs(d) >= 3) or 'без заметных сдвигов'
