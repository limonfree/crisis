#!/usr/bin/env python3
"""Crisis Engine v4: independent explainable diagnostics and point-in-time collection.

Stdlib only. Diagnostic cutoffs are transparent *unvalidated hypotheses*, not a
portfolio allocation policy. Audit stores the first successful UTC snapshot/day.
"""
from __future__ import annotations
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics

VERSION = '4.0.0'
ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
MODELS = ('market', 'credit_liquidity', 'macro', 'crypto')
MODEL_DESCRIPTIONS = {
    'market': ('Рынок акций', 'Проверка цены S&P 500, тренда и VIX',
               'Не предсказывает падение. Показывает, подтверждена ли слабость фактическими ценами и волатильностью.'),
    'credit_liquidity': ('Кредит и деньги', 'Спреды HY, NFCI и чистая ликвидность',
               'Кредитный сигнал способен опередить акции, но расширение спреда само по себе не является сигналом продажи.'),
    'macro': ('Реальная экономика', 'Безработица, правило Сэм, заявки и производственный опрос',
               'Данные выходят с задержкой и пересматриваются. Это не официальная датировка рецессии.'),
    'crypto': ('Крипторынок', 'BTC и ETH относительно тренда, просадка BTC, стейблкоины',
               'Это наблюдение за структурой рынка, а не прогноз цен BTC или ETH.')
}

def utcstamp():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

def _round(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) and not isinstance(x, bool) else None

def _measure(rs, name):
    ind = rs.get('by', {}).get(name)
    return ind.get('x') if ind and ind.get('status') == 'ok' else None

def _datum(label, value, threshold, on):
    return {'label': label, 'value': _round(value), 'rule': threshold, 'flag': on if value is not None else None}

def _part(state, label, desc, tests, why, *, tone='neutral'):
    known = sum(x['flag'] is not None for x in tests)
    return {'state':state, 'label':label, 'summary':why, 'scope':desc, 'tests':tests,
            'known':known, 'total':len(tests), 'tone':tone, 'validated':False,
            'qualification':'Правило диагностики, не калиброванная вероятность и не торговая команда.'}

def diagnose(data, rs, latest, asof):
    import engine as e
    parts = {}
    a = rs.get('aux', {})
    vix, dd, s200 = _measure(rs, 'vix'), _measure(rs, 'spxdd'), a.get('s200')
    tests = [_datum('VIX ≥ 25', vix, '≥25', vix >=25 if vix is not None else None),
             _datum('S&P ниже MA200 на 3%', s200, '≤−3%', s200 <= -3 if s200 is not None else None),
             _datum('S&P упал от годового максимума на 10%', dd, '≤−10%', dd <= -10 if dd is not None else None)]
    if all(z is not None for z in (vix, dd, s200)):
        n = sum(t['flag'] is True for t in tests)
        if n >= 2: st, label, summary, tone = 'defensive', 'Снижение цен подтверждено', 'Несколько независимых рыночных признаков показывают ослабление.', 'high'
        elif n == 1: st, label, summary, tone = 'mixed', 'Есть рыночная слабость', 'Один тревожный признак; полного подтверждения нет.', 'watch'
        elif s200 > 0 and dd > -5: st, label, summary, tone = 'uptrend', 'Восходящий тренд сохраняется', 'S&P выше долгосрочного тренда, значительной просадки и высоких значений VIX нет.', 'calm'
        else: st, label, summary, tone = 'mixed', 'Сигналы смешанные', 'Нет совпадающих предупреждений, но и устойчивый восходящий тренд не подтверждён.', 'neutral'
    else: st, label, summary, tone = 'unknown','Недостаточно данных','Не удалось проверить VIX, S&P и его среднюю.', 'neutral'
    parts['market'] = _part(st, label, MODEL_DESCRIPTIONS['market'][1], tests, summary, tone=tone)

    hy, hy4, nfci, liq = (_measure(rs, k) for k in ('hy','hy4w','nfci','netliq4'))
    # HY level and its 4-week change form ONE risk domain, to avoid double-counting.
    credit_dom = (hy >=5 or hy4 >= .5) if hy is not None and hy4 is not None else None
    nfci_dom = nfci > 0 if nfci is not None else None
    liq_dom = liq <= -1 if liq is not None else None
    tests = [_datum('HY OAS: уровень', hy, '≥5 п.п.', hy >= 5 if hy is not None else None),
             _datum('HY OAS: изменение за 4 недели', hy4, '≥0,5 п.п.', hy4>=.5 if hy4 is not None else None),
             _datum('NFCI: условия жёстче исторической нормы', nfci, '>0', nfci_dom),
             _datum('Net Liquidity: снижение за 4 недели', liq, '≤−1%', liq_dom)]
    groups = [credit_dom, nfci_dom, liq_dom]
    known = sum(v is not None for v in groups)
    count = sum(v is True for v in groups)
    if known < 2: st,label,summary,tone='unknown','Недостаточно данных','Менее двух доступных групп факторов.', 'neutral'
    elif count >=2: st,label,summary,tone='confirmed','Напряжение подтверждено','Ухудшение одновременно затрагивает несколько разных групп факторов.', 'high'
    elif count ==1: st,label,summary,tone='early','Один источник напряжения','Есть отдельное предупреждение; остальные проверенные группы его не подтверждают.', 'watch'
    elif known ==3: st,label,summary,tone='stable','Нет подтверждённого стресса','По заданным порогам три группы не показывают одновременного ухудшения.', 'calm'
    else: st,label,summary,tone='mixed','Подтверждение неполное','Доступные группы без предупреждения, но третья отсутствует.', 'neutral'
    part = _part(st,label, MODEL_DESCRIPTIONS['credit_liquidity'][1],tests,summary,tone=tone)
    part.update(domain_coverage=known, domain_total=3, domain_flags={'credit':credit_dom,'financial_conditions':nfci_dom,'liquidity':liq_dom})
    parts['credit_liquidity'] = part

    c = latest.get('cycle') or {}
    signals = c.get('signals') or []
    tests = [{'label':x.get('name'), 'value':x.get('val'), 'rule':'условие на экране', 'flag':x.get('on')} for x in signals]
    n = sum(t['flag'] is True for t in tests); known = sum(t['flag'] is not None for t in tests)
    if known < 3: st,label,summary,tone='unknown','Недостаточно статистики','Для диагноза нужны хотя бы три из четырёх публикаций.','neutral'
    elif n >=3: st,label,summary,tone='strong','Несколько признаков спада','Три или четыре макроиндикатора одновременно указывают на слабость. Это не официальный диагноз рецессии.','high'
    elif n==2: st,label,summary,tone='weakness','Ухудшение экономики','Два из доступных индикаторов указывают на ослабление.','watch'
    elif n==1: st,label,summary,tone='early','Один признак охлаждения','Один признак ослабления без достаточных подтверждений.','watch'
    else: st,label,summary,tone='none','Спад по тестам не подтверждён','Ни один из проверенных сигналов не сработал. Это не доказательство роста.','calm'
    parts['macro'] = _part(st,label,MODEL_DESCRIPTIONS['macro'][1],tests,summary,tone=tone)

    btc_ma = e.last(data.X.get('btc200'),asof,7)
    btc_dd = e.last(data.X.get('btc_dd'),asof,7)
    eth_ma = e.last(data.X.get('eth200'),asof,7)
    stable = e.last(data.X.get('stab30'),asof,7)
    tests = [_datum('BTC относительно MA200',btc_ma,'<0%',btc_ma<0 if btc_ma is not None else None),
             _datum('BTC: просадка 90 дней',btc_dd,'≤−20%',btc_dd<=-20 if btc_dd is not None else None),
             _datum('Предложение стейблкоинов за 30 дней',stable,'<−1%',stable<-1 if stable is not None else None),
             _datum('ETH относительно MA200',eth_ma,'<0%',eth_ma<0 if eth_ma is not None else None)]
    important=tests[:3]
    valid_count=sum(x['flag'] is not None for x in important)
    n=sum(x['flag'] is True for x in important)
    if valid_count<3: st,label,summary,tone='unknown','Недостаточно данных','Нет всех трёх базовых признаков для BTC и стейблкоинов.','neutral'
    elif n>=2: st,label,summary,tone='defensive','Крипторынок ослаблен','Как минимум два независимых признака указывают на ослабление.','high'
    elif n==1: st,label,summary,tone='mixed','Есть отдельный риск','Один тревожный признак, остальные пока не подтверждают ухудшение.','watch'
    else: st,label,summary,tone='constructive','Тренд BTC и денежный фон устойчивы','По трём проверенным признакам ухудшение не подтверждено.','calm'
    parts['crypto'] = _part(st,label,MODEL_DESCRIPTIONS['crypto'][1],tests,summary,tone=tone)
    return {'schema':1,'model_version':VERSION,'models':parts,
            'method':'Независимые описательные правила; пороги предварительные, без оптимизации и статистической калибровки.',
            'disagreements':_disagreements(parts),
            'readiness':'monitor_only', 'not_a_trade_signal':True,
            'descriptions':{k:{'title':v[0],'scope':v[1],'limit':v[2]} for k,v in MODEL_DESCRIPTIONS.items()}}

def _disagreements(parts):
    notes=[]
    if parts['credit_liquidity']['state'] in ('early','confirmed') and parts['market']['state']=='uptrend':
        notes.append('Кредит и ликвидность предупреждают, но тренд акций пока сохраняется.')
    if parts['market']['state'] in ('defensive','mixed') and parts['macro']['state']=='none':
        notes.append('Рынок акций показывает слабость, но выбранные макроиндикаторы спада не сработали.')
    if parts['crypto']['state']=='defensive' and parts['market']['state']=='uptrend':
        notes.append('Крипторынок ослаблен сильнее, чем акции. Нельзя механически переносить сигнал S&P на BTC и ETH.')
    return notes

def prices_at_capture(raw, capture_date):
    """Only dates PRIOR to UTC capture date: never use unclosed same-day candle."""
    prices={}
    for key in ('SPY','GSPC','BTC','ETH'):
        s=raw.get(key)
        if s is None: continue
        inds=[j for j,d in enumerate(s.d) if d < capture_date and s.v[j] > 0]
        if not inds: continue
        i=inds[-1]
        max_age = 6 if key in ('SPY','GSPC') else 3
        if (capture_date-s.d[i]).days > max_age:continue
        prices[key]={'obs':s.d[i].isoformat(),'close':round(s.v[i],8),
                     'source':'Yahoo Finance', 'price_type':'unadjusted_close'}
    return prices

def code_hash():
    h=hashlib.sha256()
    for name in ('engine.py','collect.py','v4.py'):
        p=ROOT/name
        if p.exists():h.update(name.encode()+b'\0'+p.read_bytes())
    return h.hexdigest()

def build_snapshot(latest,raw,errors):
    day=dt.date.fromisoformat(latest['asof'])
    capture=latest['generated_at']
    return {'schema':1,'captured_at':capture,'asof':str(day),
            'model_version':VERSION,'model_code_sha256':code_hash(),
            'score_v3_reference':(latest.get('score') or {}).get('value'),
            'models':latest['diagnostics']['models'], 'prices':prices_at_capture(raw,day),
            'sources':{'status':{'ok':sorted(k for k in raw if k not in errors),
                                 'fallback_or_error':errors},
                       'indicators':[{k:x.get(k) for k in ('id','obs','pub','status','disp','stress','pctl','src','series')}
                                     for x in latest.get('indicators',[])]},
            'warning':'Снимок отражает данные, полученные к моменту записи, а не полные исторические винтажи официальных публикаций.'}

def _write_json_atomic(path,obj):
    import tempfile
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent, prefix='.tmp-',suffix='.json')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
            f.write('\n')
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _create_new(path, content):
    """True exclusive create: never overwrite a previously published source snapshot."""
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with open(path,'xb') as f:f.write(content)
        return True
    except FileExistsError:
        return False

def archive_first_daily(latest, raw, errors):
    """First successful collection each UTC day wins; reruns never change it.
    Full source arrays are archived GZIP so future research can reproduce the
    observed data vintage as *collected*, not assert official real-time vintages.
    """
    date=dt.date.fromisoformat(latest['asof'])
    folder=DATA/'snapshots'/f'{date.year:04d}'/f'{date.month:02d}'
    outfile=folder/f'{date.isoformat()}.json'
    if outfile.exists():return False
    day_payload=build_snapshot(latest,raw,errors)
    raw_payload={'schema':1,'captured_at':day_payload['captured_at'],
                 'description':'Fetched full series as available to this run. Historical dates may contain revisions already known by the fetch date.',
                 'series':{k:{'dates':[x.isoformat() for x in s.d], 'values':s.v,
                              'status':'cache_fallback' if k in errors else 'fresh_fetch'} for k,s in sorted(raw.items())}}
    bytes_raw=json.dumps(raw_payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    vintage_bytes=gzip.compress(bytes_raw,compresslevel=6,mtime=0)
    raw_path=DATA/'vintages'/f'{date.year:04d}'/f'{date.month:02d}'/(date.isoformat()+'.json.gz')
    day_payload['source_vintage']={'path':raw_path.relative_to(ROOT).as_posix(),
                                   'sha256':hashlib.sha256(vintage_bytes).hexdigest(),
                                   'count':len(raw)}
    # The raw archive is created before the metadata, and metadata is the sentinel.
    if not _create_new(raw_path,vintage_bytes):
        raise RuntimeError('Raw vintage exists without metadata snapshot; review repository before retry')
    _create_new(outfile,(json.dumps(day_payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode())
    return True

def read_snapshots():
    snaps=[]
    for p in sorted((DATA/'snapshots').glob('*/*/*.json')):
        try:
            with open(p,encoding='utf-8') as f:s=json.load(f)
            if s.get('schema')==1 and s.get('asof') and s.get('captured_at'):
                snaps.append(s)
        except (OSError,ValueError,TypeError):pass
    return sorted(snaps,key=lambda s:(s['asof'],s['captured_at']))

def materialize_audit():
    import evaluate
    snaps=read_snapshots()
    entries=[{'date':s['asof'],'captured_at':s['captured_at'],'model_version':s['model_version'],'score_reference':s.get('score_v3_reference'),
              'code_sha256':s.get('model_code_sha256'),'models':{k:{'state':m['state'],'label':m['label']} for k,m in s.get('models',{}).items()},
              'source_vintage_sha256':s.get('source_vintage',{}).get('sha256')}
             for s in snaps[-365:]]
    _write_json_atomic(DATA/'signal_log.json',{'schema':1,'asof':utcstamp(),'n_total':len(snaps),
                                               'entries':entries, 'immutable_sources':True,
                                               'start_date':snaps[0]['asof'] if snaps else None})
    _write_json_atomic(DATA/'evaluation.json',evaluate.evaluate(snaps))
