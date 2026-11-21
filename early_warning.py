"""Experimental cross-asset risk watch; NOT a calibrated forecast or trade advice.
Only uses values present in the current published capture; never history.json.
"""
VERSION = '1.0.0'

def compute(latest):
    blocks = latest.get('blocks') or {}
    score = (latest.get('score') or {}).get('value')
    conditions = (
        ('aggregate', score, 60, 'Общий индекс ≥ 60'),
        ('credit', (blocks.get('credit') or {}).get('score'), 60, 'Кредитный стресс ≥ 60'),
        ('market', (blocks.get('market') or {}).get('score'), 58, 'Рыночный стресс ≥ 58'),
        ('liquidity', (blocks.get('liq') or {}).get('score'), 65, 'Стресс ликвидности ≥ 65'),
    )
    tests = [{'id':key,'label':label,'value':value,'threshold':limit,
              'triggered':value >= limit if isinstance(value,(int,float)) and not isinstance(value,bool) else None}
             for key,value,limit,label in conditions]
    known = sum(t['triggered'] is not None for t in tests)
    count = sum(t['triggered'] is True for t in tests)
    # Unknown is not interpreted as safe. Require all 4 inputs for a clear status.
    if known < 4 or not latest.get('valid'):
        state,label='unknown','Недостаточно подтверждённых данных'
    elif count >= 3:state,label='high','Сильное межрыночное напряжение'
    elif count >= 2:state,label='elevated','Повышенная уязвимость BTC/ETH'
    elif count == 1:state,label='watch','Раннее предупреждение'
    else:state,label='normal','Условия предупреждения не сработали'
    return {'schema':1,'model_version':VERSION,'asof':latest.get('asof'),
            'state':state,'label':label,'triggered':count,'available':known,'tests':tests,
            'validated':False,'not_a_trade_signal':True,
            'method':'Фиксированные предварительные пороги, без обучения на истории; не вероятность падения.',
            'limits':'Общий фон, не индивидуальный прогноз BTC/ETH. При устаревшем снимке не считать текущим сигналом.'}
