import unittest
import datetime as dt
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collect

def points(day, last):
    ds=[day-dt.timedelta(days=i) for i in range(150,0,-1)]+[day]
    vs=[18.0]*(len(ds)-1)+[last]
    return ds,vs

class SourceFallbackTest(unittest.TestCase):
    def test_yahoo_nov5_beats_old_fred_and_cboe(self):
        with patch.object(collect,'_cboe_vix',return_value=points(dt.date(2026,11,4),16.61)), \
             patch.object(collect.eng,'yahoo',return_value=points(dt.date(2026,11,5),28.18)), \
             patch.object(collect,'_fred_robust',return_value=points(dt.date(2026,11,4),16.61)):
            ds,vals,src=collect._vix_robust()
        self.assertEqual(str(ds[-1]),'2026-11-05')
        self.assertAlmostEqual(vals[-1],28.18)
        self.assertIn('Yahoo',src)
    def test_cboe_primary_same_day_agreement(self):
        with patch.object(collect,'_cboe_vix',return_value=points(dt.date(2026,11,5),28.18)), \
             patch.object(collect.eng,'yahoo',return_value=points(dt.date(2026,11,5),28.22)), \
             patch.object(collect,'_fred_robust',return_value=points(dt.date(2026,11,4),16.61)):
            ds,vals,src=collect._vix_robust()
        self.assertEqual(vals[-1],28.18)
        self.assertIn('Cboe',src)
    def test_conflicting_same_day_providers_stop(self):
        with patch.object(collect,'_cboe_vix',return_value=points(dt.date(2026,11,5),16.61)), \
             patch.object(collect.eng,'yahoo',return_value=points(dt.date(2026,11,5),28.18)), \
             patch.object(collect,'_fred_robust',return_value=points(dt.date(2026,11,4),16.61)):
            with self.assertRaisesRegex(RuntimeError,'VIX CONFLICT'):
                collect._vix_robust()
    def test_no_provider_fails_closed(self):
        def bad(*args,**kwargs):raise TimeoutError('network disabled')
        with patch.object(collect,'_cboe_vix',side_effect=bad), \
             patch.object(collect.eng,'yahoo',side_effect=bad), \
             patch.object(collect,'_fred_robust',side_effect=bad):
            with self.assertRaisesRegex(RuntimeError,'VIX unavailable'):
                collect._vix_robust()
    def test_cboe_csv_parse(self):
        dates=points(dt.date(2026,11,5),28.18)[0]
        payload=('DATE,OPEN,HIGH,LOW,CLOSE\n'+'\n'.join(f'{x:%m/%d/%Y},10,29,9,{28.18 if x==dates[-1] else 16.61}' for x in dates)).encode()
        with patch.object(collect,'_download',return_value=payload):
            d,v=collect._cboe_vix()
        self.assertEqual(d[-1],dt.date(2026,11,5)); self.assertEqual(v[-1],28.18)

if __name__=='__main__':unittest.main()

class DailyCandleCutoffTest(unittest.TestCase):
    def test_no_unfinished_nov6_candle_before_ny_close(self):
        now=dt.datetime(2026,11,6,16,0,tzinfo=dt.timezone.utc)
        d=[dt.date(2026,11,4),dt.date(2026,11,5),dt.date(2026,11,6)]
        v=[16.61,28.18,25.12]
        dd,vv=collect._completed_market_history(d,v,now=now)
        self.assertEqual(dd[-1],dt.date(2026,11,5))
        self.assertEqual(vv[-1],28.18)
    def test_nov6_close_allowed_after_settlement_grace(self):
        now=dt.datetime(2026,11,6,23,0,tzinfo=dt.timezone.utc)
        d=[dt.date(2026,11,4),dt.date(2026,11,5),dt.date(2026,11,6)]
        dd,vv=collect._completed_market_history(d,[16.61,28.18,25.12],now=now)
        self.assertEqual(dd[-1],dt.date(2026,11,6))
    def test_crypto_yesterday_utc_daily_candle(self):
        now=dt.datetime(2026,11,6,23,0,tzinfo=dt.timezone.utc)
        d=[dt.date(2026,11,4),dt.date(2026,11,5),dt.date(2026,11,6)]
        dd,vv=collect._completed_market_history(d,[1,2,3],now=now,crypto=True)
        self.assertEqual(dd[-1],dt.date(2026,11,5))
