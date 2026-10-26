#!/usr/bin/env python3
"""Forward, no-lookahead validation from archived immutable daily signals only.

There is deliberately NO backfilled history, probability, or simulated past signal.
Uses captured unadjusted *closing* quotes, so misses intraday lows and dividends.
"""
from __future__ import annotations
import datetime as dt
import math
import statistics

HORIZONS = (5,20,60)  # next DISTINCT end-of-day quote dates, not calendar days
WARN_STATES={'market':{'defensive'},'credit_liquidity':{'confirmed'},
             'macro':{'strong'},'crypto':{'defensive'}}
ASSETS={'market':('SPY',),'credit_liquidity':('SPY',),'macro':('SPY',),
        'crypto':('BTC','ETH')}
MIN_EPISODES=30
COOLDOWN_DAYS=20

def _day(x):return dt.date.fromisoformat(x)
def _round(x):return round(x,3) if x is not None else None

def _wilson(k,n):
    if not n:return None
    z=1.96;p=k/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den
    rad=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [_round(max(0,center-rad)),_round(min(1,center+rad))]

def _quote_series(snaps,asset):
    # First saved observation for each close date cannot be overwritten by a later download.
    seen={}
    for s in snaps:
        q=s.get('prices',{}).get(asset)
        if not q:continue
        try:
            ob=_day(q['obs']); captured=_day(s['asof']); price=float(q['close'])
            if ob>=captured or price<=0 or not math.isfinite(price):continue
            if q['obs'] not in seen:seen[q['obs']]=price
        except (ValueError,KeyError,TypeError,OverflowError):continue
    return sorted([(_day(d),v) for d,v in seen.items()])

def _episode_starts(snaps,model):
    episodes=[];last=None;was_warning=False
    for s in snaps:
        m=s.get('models',{}).get(model,{})
        yes=m.get('state') in WARN_STATES[model]
        day=_day(s['asof'])
        # One start per distinct warning episode; cool-down against repeated daily signals.
        if yes and not was_warning and (last is None or (day-last).days >= COOLDOWN_DAYS):
            episodes.append(s);last=day
        was_warning=yes
    return episodes

def _outcome(quotes,snap,asset,horizon):
    q=snap.get('prices',{}).get(asset)
    if not q:return None
    try:begin=_day(q['obs']); p0=float(q['close'])
    except (KeyError,TypeError,ValueError):return None
    if p0<=0:return None
    pos=next((i for i,(d,p) in enumerate(quotes) if d==begin),None)
    if pos is None or pos+horizon>=len(quotes):return None
    after=quotes[pos+1:pos+horizon+1]
    # Reject sparse sample dates (outages are NOT counted as real trading sessions).
    maxgap=6 if asset=='SPY' else 3
    seq=[quotes[pos]]+after
    if any((seq[i+1][0]-seq[i][0]).days>maxgap for i in range(len(seq)-1)):return None
    if (after[-1][0]-begin).days > max(15,horizon*3):return None
    ret=(after[-1][1]/p0-1)*100
    min_close=min(p for d,p in after)
    worst=(min_close/p0-1)*100
    return {'return_pct':ret,'worst_forward_close_pct':worst,'event_decline10':worst<=-10,
            'end':after[-1][0].isoformat()}

def _aggregate(complete,immature,invalid,episode_count):
    n=len(complete)
    base={'n_mature':n,'n_pending':immature,'n_censored':invalid,'episodes':episode_count,
          'min_n_required':MIN_EPISODES,'reportable':n>=MIN_EPISODES,'calibrated':False}
    if n<MIN_EPISODES:
        return {**base,'status':'insufficient_sample','explanation':f'{n} завершённых независимых наблюдений; требуется минимум {MIN_EPISODES}. Не публикуем проценты как прогноз.',
                'event_rate':None,'false_warning_rate':None,'mean_return_pct':None,'max_drawdown_pct':None}
    events=sum(x['event_decline10'] for x in complete)
    return {**base,'status':'descriptive_only',
            'event_rate':_round(events/n),'event_rate_wilson95':_wilson(events,n),
            'false_warning_rate':_round((n-events)/n),
            'mean_return_pct':_round(statistics.mean(x['return_pct'] for x in complete)),
            'median_return_pct':_round(statistics.median(x['return_pct'] for x in complete)),
            'worst_observed_forward_close_pct':_round(min(x['worst_forward_close_pct'] for x in complete)),
            'explanation':'Частота событий ПОСЛЕ предупреждения, с 95% Wilson-интервалом; это не будущая вероятность. Нет нейтральной контрольной группы и транзакционных издержек.'}

def evaluate(snaps):
    """Snapshots must be actual archived capture-days, never reconstructed."""
    result={'schema':1,'research_version':'4.0.0','source':'immutable daily observation archives',
            'point_in_time_status':'captured_forward_only',
            'what_is_tested':'Просадка по закрытиям ≥10% в следующие 5/20/60 РЕАЛЬНО НАБЛЮДЁННЫХ новых сессий. Не intraday, не total return.',
            'limits':['Цена SPY здесь не учитывает выплаты дивидендов, сплиты и комиссии.',
                      'При пропусках ежедневных снимков тесты цензурируются.',
                      'Зависимые эпизоды объединяются; без группы сравнения нельзя утверждать predictive edge.',
                      'Исторические графики v3 не используются для доказательства результатов.',
                      'Статистика без настоящих винтажей ALFRED не равна историческому макробэктесту.'],
            'minimum_independent_episodes':MIN_EPISODES,
            'earliest_capture':snaps[0]['captured_at'] if snaps else None,
            'n_snapshots':len(snaps),'models':{}}
    quotes={a:_quote_series(snaps,a) for a in ('SPY','BTC','ETH')}
    for model in WARN_STATES:
        episodes=_episode_starts(snaps,model)
        result['models'][model]={}
        for asset in ASSETS[model]:
            result['models'][model][asset]={}
            for horizon in HORIZONS:
                mature=[];pending=0;censored=0
                for snap in episodes:
                    ob=snap.get('prices',{}).get(asset,{}).get('obs')
                    if not ob:censored+=1;continue
                    q=quotes[asset]
                    pos=next((j for j,(d,v) in enumerate(q) if d==_day(ob)),None)
                    if pos is None:censored+=1;continue
                    if pos+horizon>=len(q):pending+=1;continue
                    res=_outcome(q,snap,asset,horizon)
                    if res is None:censored+=1
                    else:mature.append(res)
                result['models'][model][asset][str(horizon)] = _aggregate(mature,pending,censored,len(episodes))
    result['threshold_sensitivity'] = sensitivity(snaps)
    return result


def sensitivity(snaps):
    """Only how often hypothetical thresholds fired IN RECORDED v4 sessions.
    Not a threshold optimization and not proof of predictive value.
    """
    specs=[('market','VIX >= X','VIX ≥ X', [20,25,30], lambda t: t.get('VIX ≥ 25')),
           ('credit_liquidity','HY OAS 4w >= X pp','HY 4 недели ≥ X п.п.',[.3,.5,.7],lambda t:t.get('HY OAS: изменение за 4 недели'))]
    result=[]
    for model,code,label,cutoffs,pick in specs:
        v=[]
        for s in snaps:
            tests={t['label']:t.get('value') for t in s.get('models',{}).get(model,{}).get('tests',[])}
            cur=pick(tests)
            if isinstance(cur,(int,float)) and math.isfinite(cur):v.append(float(cur))
        result.append({'model':model,'name':label,'n':len(v),'cutoffs':[{'threshold':x,'days_triggered':sum(z>=x for z in v)} for x in cutoffs],
                       'warning':'Частота срабатывания порога без оценки дальнейшей доходности; по этим числам нельзя выбирать лучший порог.'})
    return result
