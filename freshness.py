"""Crisis Engine: explicit source-age and completed-US-session checks.

Pure standard library. Weekend/regular US-equity holidays handled. Exchange
unscheduled closures and publication-specific lags cannot be inferred here;
those conditions intentionally fail closed for hard market sources.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
CRITICAL = ('VIX', 'GSPC')
DAILY_WATCH = ('RRPONTSYD','HY','CCC','BBB','BAA','DGS10','CURVE','SPY','RSP','QQQ','IWM','KRE','MOVE','HG','GC','DXY','JPY','BTC','ETH')


def nth_weekday(y, m, wd, n):
    d = date(y, m, 1)
    return d + timedelta(days=(wd - d.weekday()) % 7 + 7*(n-1))


def last_weekday(y, m, wd):
    if m == 12: d = date(y+1,1,1)-timedelta(days=1)
    else: d = date(y,m+1,1)-timedelta(days=1)
    return d - timedelta(days=(d.weekday()-wd) % 7)


def observed(d):
    if d.weekday() == 5:return d-timedelta(days=1)
    if d.weekday() == 6:return d+timedelta(days=1)
    return d


def easter(y):
    a=y%19;b=y//100;c=y%100;d=b//4;e=b%4;f=(b+8)//25;g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30;i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7
    m=(a+11*h+22*l)//451;month=(h+l-7*m+114)//31;day=((h+l-7*m+114)%31)+1
    return date(y,month,day)


def us_stock_holidays(year):
    # Adjacent-year New Year's observation is included, e.g. Dec 31.
    hs={observed(date(year,1,1)), observed(date(year+1,1,1)),
        nth_weekday(year,1,0,3), nth_weekday(year,2,0,3),
        easter(year)-timedelta(days=2),last_weekday(year,5,0),
        observed(date(year,6,19)), observed(date(year,7,4)),
        nth_weekday(year,9,0,1), nth_weekday(year,11,3,4),
        observed(date(year,12,25))}
    return hs


def is_us_session(day):
    return day.weekday()<5 and day not in us_stock_holidays(day.year)


def prev_session(day):
    d=day-timedelta(days=1)
    for _ in range(20):
        if is_us_session(d):return d
        d-=timedelta(days=1)
    raise RuntimeError('US calendar cannot find a previous trading session')


def expected_completed_session(now):
    """Latest market close that should be available; allow 75m after 16:00 ET."""
    if now.tzinfo is None:raise ValueError('timezone-aware UTC datetime required')
    ny=now.astimezone(NY)
    d=ny.date()
    if is_us_session(d) and ny.time()>=time(17,15):return d
    return prev_session(d)


def trading_sessions_behind(obs, expected):
    if obs is None:return None
    if isinstance(obs,str): obs=date.fromisoformat(obs[:10])
    if obs>=expected:return 0
    c=0; day=obs
    for _ in range(100):
        day+=timedelta(days=1)
        if is_us_session(day):c+=1
        if day>=expected:return c
    return 999


def market_health(raw, errors, captured_at, providers=None):
    expected=expected_completed_session(captured_at)
    providers=providers or {}
    rows={}
    for k in (*CRITICAL,*DAILY_WATCH):
        s=raw.get(k)
        obs=s.d[-1] if s and s.d else None
        if k in ('BTC','ETH'):
            crypto_expected=captured_at.astimezone(timezone.utc).date()-timedelta(days=1)
            lag=None if obs is None else max(0,(crypto_expected-obs).days)
        else:
            lag=trading_sessions_behind(obs,expected)
        if lag is None:status='unavailable'
        elif lag==0:status='current_close'
        elif lag==1:status='one_session_late'
        else:status='stale'
        rows[k]={'status':status,'observation':obs.isoformat() if obs else None,
                 'sessions_behind':lag,'source':providers.get(k),'fetch_error':errors.get(k)}
    blockers=[k for k in CRITICAL if rows[k]['status']!='current_close']
    # Credit may normally be one session late, but MUST be reported as such.
    warnings=[k for k in rows if k not in blockers and rows[k]['status']!='current_close']
    risk_events=[]
    if not blockers:
        vix_series=raw['VIX']
        if vix_series.v[-1]>=25:
            risk_events.append({'id':'vix_elevated','message':'VIX ≥ 25: высокая ожидаемая волатильность S&P 500',
                                'observed':round(vix_series.v[-1],2),'date':str(vix_series.d[-1])})
        spx_series=raw['GSPC']
        if len(spx_series.v)>=2 and spx_series.v[-2]>0:
            pct=(spx_series.v[-1]/spx_series.v[-2]-1)*100
            if pct<=-3:
                risk_events.append({'id':'sp500_drop','message':'S&P 500 снизился более чем на 3% за сессию',
                                    'observed_pct':round(pct,2),'date':str(spx_series.d[-1])})
    return {'schema':1,'checked_at':captured_at.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
            'expected_last_completed_us_session':expected.isoformat(),
            'status':'critical' if blockers else ('warning' if warnings else 'ok'),
            'critical_ready':not blockers,'critical_blockers':blockers,'warnings':warnings,
            'rows':rows,'risk_events':risk_events,
            'disclaimer':'Current_close means latest completed regular NYSE session, not live intraday data. Holidays and unusual closures may need manual overrides.'}
