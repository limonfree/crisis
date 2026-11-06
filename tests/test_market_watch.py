import unittest
import datetime as dt
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import market_watch
UTC=dt.timezone.utc

class MarketWatchTests(unittest.TestCase):
    def test_pre_market_only_last_close(self):
        now=dt.datetime(2026,11,6,10,0,tzinfo=UTC)
        def close(sym,when):return dict(value=28.18 if sym=='^VIX' else 7353.73,kind='daily_close',observed_at='2026-11-05')
        def intraday(sym,when):raise AssertionError('Should not fetch intraday premarket')
        r=market_watch.build(now,intraday=intraday,close=close,crypto_quote=lambda sym,when:dict(value=2000,kind="intraday_delayed",observed_at="2026-11-06T15:55:00Z"))
        self.assertEqual(r['status'],'ok'); self.assertEqual(r['quotes']['vix']['value'],28.18)
    def test_market_open_old_close_cannot_be_called_live(self):
        now=dt.datetime(2026,11,6,16,0,tzinfo=UTC)
        def intraday(sym,when):raise ValueError('not fresh')
        def close(sym,when):return dict(value=28.18,kind='daily_close',observed_at='2026-11-05')
        r=market_watch.build(now,intraday=intraday,close=close,crypto_quote=lambda sym,when:dict(value=2000,kind="intraday_delayed",observed_at="2026-11-06T15:55:00Z"))
        self.assertEqual(r['status'],'partial')
        self.assertEqual(r['quotes']['vix']['kind'],'daily_close')
    def test_market_open_fresh_intraday(self):
        now=dt.datetime(2026,11,6,16,0,tzinfo=UTC)
        def intraday(sym,when):return dict(value=25.12,kind='intraday_delayed',observed_at='2026-11-06T15:55:00Z')
        def close(sym,when):raise AssertionError('Unneeded')
        r=market_watch.build(now,intraday=intraday,close=close,crypto_quote=lambda sym,when:dict(value=2000,kind="intraday_delayed",observed_at="2026-11-06T15:55:00Z"))
        self.assertEqual(r['status'],'ok');self.assertAlmostEqual(r['quotes']['vix']['value'],25.12)
    def test_both_providers_down_alarm(self):
        now=dt.datetime(2026,11,6,16,0,tzinfo=UTC)
        def bad(*a):raise TimeoutError('blocked')
        r=market_watch.build(now,intraday=bad,close=bad,crypto_quote=bad)
        self.assertEqual(r['status'],'unavailable')
        self.assertEqual(r['quotes'],{})

if __name__=='__main__':unittest.main()
