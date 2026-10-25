#!/usr/bin/env python3
"""Собирает рыночные ряды (Yahoo -> резерв Stooq) и пишет data.json для страницы.
Запускается из GitHub Actions. FRED/CBOE/крипто страница по-прежнему берёт сама."""
import csv, io, json, sys, time, datetime as dt
import pandas as pd
import requests
import yfinance as yf

MACRO = ['^VIX', '^VIX3M', '^VVIX', '^MOVE', 'TLT', 'KRE', 'GLD', 'SPY', 'QQQ',
         'CL=F', 'BDRY', 'RSP', 'IWM', 'HG=F', 'GC=F']
CAP, KEEP, CHUNK = 100, 260, 25
UA = {'User-Agent': 'Mozilla/5.0'}
FALLBACK = ['AAPL','MSFT','NVDA','AVGO','ORCL','ADBE','CRM','AMD','QCOM','TXN','GOOGL','META','NFLX','DIS',
            'AMZN','TSLA','HD','MCD','NKE','PG','KO','PEP','COST','WMT','BRK-B','JPM','V','MA','BAC','WFC',
            'LLY','UNH','JNJ','ABBV','MRK','GE','CAT','RTX','UNP','XOM','CVX','COP','NEE','DUK','PLD','AMT','LIN']

def sp500_sample():
    try:
        t = requests.get('https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv', timeout=30).text
        syms = [r['Symbol'].strip().replace('.', '-') for r in csv.DictReader(io.StringIO(t)) if r.get('Symbol')]
        if len(syms) >= 400:
            return syms[::-(-len(syms) // CAP)]
    except Exception as e:
        print('список S&P500 недоступен:', e)
    return FALLBACK

def close_of(df, s):
    try:
        c = df[s]['Close']
    except Exception:
        try: c = df['Close']
        except Exception: return None
    return c.iloc[:, 0] if isinstance(c, pd.DataFrame) else c

def yahoo(symbols):
    out = {}
    for i in range(0, len(symbols), CHUNK):
        chunk, df = symbols[i:i + CHUNK], None
        for attempt in range(3):
            try:
                df = yf.download(chunk, period='14mo', interval='1d', auto_adjust=False,
                                 group_by='ticker', progress=False, threads=False)
                if df is not None and not df.empty: break
            except Exception as e:
                print('yahoo ошибка:', str(e)[:80])
            time.sleep(5 * (attempt + 1))
        if df is None or df.empty: continue
        for s in chunk:
            c = close_of(df, s)
            if c is None: continue
            c = c.dropna()
            if len(c) >= 5: out[s] = [round(float(x), 4) for x in c.tolist()[-KEEP:]]
        time.sleep(2)
    return out

def stooq(s):
    z = s.lower() if s.startswith('^') else (s[:-2].lower() + '.f' if s.endswith('=F') else s.lower() + '.us')
    try:
        r = requests.get('https://stooq.com/q/d/l/', params={'s': z, 'i': 'd'}, headers=UA, timeout=20)
        rows = list(csv.reader(r.text.strip().splitlines()))[1:]
        c = [float(x[4]) for x in rows if len(x) >= 5 and x[4].replace('.', '', 1).isdigit()]
        return c[-KEEP:] if len(c) >= 5 else None
    except Exception:
        return None

def main():
    sample = sp500_sample()
    series = yahoo(MACRO + sample)
    for s in MACRO:                      # резерв Stooq только для макро-символов
        if s not in series:
            c = stooq(s)
            if c: series[s] = c
    got_macro = sum(s in series for s in MACRO)
    breadth = [s for s in sample if s in series]
    print(f'macro {got_macro}/{len(MACRO)}, breadth {len(breadth)}/{len(sample)}')
    if got_macro < 8 or len(breadth) < 30:
        print('слишком мало данных — data.json не обновляю'); sys.exit(1)
    out = {'updated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
           'breadth_symbols': breadth, 'series': series}
    json.dump(out, open('data.json', 'w'), separators=(',', ':'))

if __name__ == '__main__':
    main()
