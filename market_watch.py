#!/usr/bin/env python3
"""Lightweight independent market pulse. Separate from 3-hour FRED collection.

Quote is labelled intraday ONLY when the timestamp is within 40 minutes and
inside the US regular session. Otherwise use a verifiable last *close*.
Never call unverified/delayed observations realtime. Standard library only.
"""
from __future__ import annotations
import datetime as dt
import json
import math
import urllib.parse
from zoneinfo import ZoneInfo
from pathlib import Path
import collect
import engine
import freshness

NY=ZoneInfo('America/New_York')
ROOT=Path(__file__).resolve().parent


def market_is_open(now):
    local=now.astimezone(NY)
    return freshness.is_us_session(local.date()) and dt.time(9,30)<=local.time()<dt.time(16,0)


def yahoo_intraday(symbol, now):
    url='https://query1.finance.yahoo.com/v8/finance/chart/'+urllib.parse.quote(symbol)
    url+='?range=5d&interval=5m&includePrePost=false'
    obj=json.loads(collect._download(url,timeout=11,retries=2))
    r=obj['chart']['result'][0]
    timestamps=r.get('timestamp') or []
    closes=r['indicators']['quote'][0]['close']
    last=None
    for stamp,val in zip(timestamps,closes):
        if val is None: continue
        timestamp=dt.datetime.fromtimestamp(stamp,dt.timezone.utc)
        if timestamp>now+dt.timedelta(minutes=2):continue
        local=timestamp.astimezone(NY)
        if not (freshness.is_us_session(local.date()) and dt.time(9,30)<=local.time()<dt.time(16,0)):
            continue
        if math.isfinite(float(val)) and float(val)>0:
            last=(timestamp,float(val))
    if not last:raise ValueError('No regular-session intraday quote')
    stamp,val=last
    age=(now-stamp).total_seconds()/60
    if not 0<=age<=40 or not market_is_open(now):
        raise ValueError(f'Intraday quote not recent ({age:.1f} minutes) or market closed')
    meta=r.get('meta') or {}
    previous=meta.get('chartPreviousClose') or meta.get('previousClose')
    move=(val/float(previous)-1)*100 if previous and float(previous)>0 else None
    return {'value':round(val,2),'observed_at':stamp.isoformat().replace('+00:00','Z'),
            'source':'Yahoo Finance 5m','kind':'intraday_delayed','minutes_old':round(age,1),
            'change_pct_1d':round(move,2) if move is not None else None}


def crypto_intraday(symbol,now):
    url='https://query1.finance.yahoo.com/v8/finance/chart/'+urllib.parse.quote(symbol)
    url+='?range=2d&interval=5m'
    obj=json.loads(collect._download(url,timeout=11,retries=2))
    r=obj['chart']['result'][0]
    points=[]
    for stamp,val in zip(r.get('timestamp') or [],r['indicators']['quote'][0]['close']):
        if val is None:continue
        t=dt.datetime.fromtimestamp(stamp,dt.timezone.utc)
        if t<=now+dt.timedelta(minutes=2) and math.isfinite(float(val)) and float(val)>0:points.append((t,float(val)))
    if not points:raise ValueError('No crypto intraday price')
    t,px=points[-1]
    if not 0<=(now-t).total_seconds()/60<=40:raise ValueError('Stale crypto quote')
    yesterday=now.astimezone(dt.timezone.utc).date()-dt.timedelta(days=1)
    d,v=engine.yahoo(symbol)
    d,v=collect._completed_market_history(d,v,now=now,crypto=True)
    prev=v[-1] if d[-1]==yesterday else None
    move=(px/prev-1)*100 if prev and prev>0 else None
    return {'value':round(px,2),'observed_at':t.isoformat().replace('+00:00','Z'),
            'source':'Yahoo Finance 5m / daily close','kind':'intraday_delayed',
            'minutes_old':round((now-t).total_seconds()/60,1),
            'change_pct_1d':round(move,2) if move is not None else None}


def last_close(symbol, now):
    expected=(now.astimezone(dt.timezone.utc).date()-dt.timedelta(days=1)) if symbol in ('BTC-USD','ETH-USD') else freshness.expected_completed_session(now)
    if symbol=='^VIX':
        d,v,provider=collect._vix_robust(check_fred=False)
    else:
        d,v=engine.yahoo(symbol)
        d,v=collect._completed_market_history(d,v,now=now,crypto=(symbol in ('BTC-USD','ETH-USD')))
        provider='Yahoo Finance daily'
    obs=d[-1]
    lag=max(0,(expected-obs).days) if symbol in ('BTC-USD','ETH-USD') else freshness.trading_sessions_behind(obs,expected)
    if lag!=0:raise ValueError(f'Latest {symbol} close is {obs}; expected {expected}; {lag} sessions behind')
    change=(v[-1]/v[-2]-1)*100 if len(v)>1 and v[-2]>0 else None
    return {'value':round(v[-1],2),'observed_at':obs.isoformat(),
            'source':provider,'kind':'daily_close','completed_session':str(obs),
            'change_pct_1d':round(change,2) if change is not None else None}


def build(now, intraday=yahoo_intraday, close=last_close, crypto_quote=None):
    if now.tzinfo is None:raise ValueError('timezone required')
    if crypto_quote is None:crypto_quote=crypto_intraday
    expected=freshness.expected_completed_session(now)
    out={'schema':1,'generated_at':now.astimezone(dt.timezone.utc).isoformat().replace('+00:00','Z'),
         'expected_close':str(expected),'market_open':market_is_open(now),
         'status':'ok','quotes':{},'errors':{},'risk_events':[],
         'notice':'Intraday is a delayed public quote and never a guaranteed realtime execution price.'}
    for label,sym in (('vix','^VIX'),('sp500','^GSPC')):
        q=None
        if out['market_open']:
            try:q=intraday(sym,now)
            except Exception as exc:out['errors'][label+'_intraday']=str(exc)[:220]
        if q is None:
            try:q=close(sym,now)
            except Exception as exc:out['errors'][label+'_close']=str(exc)[:220]
        if q is not None:out['quotes'][label]=q
    for label,sym in (('btc','BTC-USD'),('eth','ETH-USD')):
        q=None
        try:q=crypto_quote(sym,now)
        except Exception as exc:out['errors'][label+'_intraday']=str(exc)[:220]
        if q is None:
            try:q=close(sym,now)
            except Exception as exc:out['errors'][label+'_close']=str(exc)[:220]
        if q is not None:out['quotes'][label]=q
    if 'vix' not in out['quotes'] or 'sp500' not in out['quotes']:
        out['status']='unavailable'
    elif out['market_open'] and any(x.get('kind')!='intraday_delayed' for x in out['quotes'].values()):
        out['status']='partial'  # NOT fully current during open session
    vix=out['quotes'].get('vix')
    spx=out['quotes'].get('sp500')
    for k in ('btc','eth'):
        q=out['quotes'].get(k)
        if q and q.get('change_pct_1d') is not None and q['change_pct_1d']<=-5:
            out['risk_events'].append(k.upper()+' упал за сутки/от вчерашнего закрытия на '+str(q['change_pct_1d'])+'%')
    if vix and vix.get('value',0)>=25:
        out['risk_events'].append('VIX ≥ 25: повышенная ожидаемая волатильность')
    if spx and spx.get('change_pct_1d') is not None and spx['change_pct_1d']<=-3:
        out['risk_events'].append('S&P 500 упал ≥ 3% за текущую или последнюю завершённую сессию')
    return out


def main():
    now=dt.datetime.now(dt.timezone.utc)
    out=build(now)
    collect._write_json_atomic(str(ROOT/'data'/'market_watch.json'),out)
    print('Market watch',out['status'],out['generated_at'],
          {k:(v['value'],v['kind'],v['observed_at']) for k,v in out['quotes'].items()},flush=True)
    if out['errors']:print('Quote errors:',out['errors'],flush=True)
    # Critical failure is still published as an alarm by the workflow.

if __name__=='__main__':main()
