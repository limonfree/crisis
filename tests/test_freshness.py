import unittest
import datetime as dt
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import freshness
import engine

UTC=dt.timezone.utc


def mkseries(day,value=18):
    d=[day-dt.timedelta(days=1),day]
    return engine.Ser(d,[value-1,value])


class FreshnessTest(unittest.TestCase):
    def test_previous_close_before_ny_session(self):
        self.assertEqual(freshness.expected_completed_session(dt.datetime(2026,11,6,10,0,tzinfo=UTC)),dt.date(2026,11,5))
    def test_same_close_after_session(self):
        self.assertEqual(freshness.expected_completed_session(dt.datetime(2026,11,6,23,0,tzinfo=UTC)),dt.date(2026,11,6))
    def test_weekend(self):
        self.assertEqual(freshness.expected_completed_session(dt.datetime(2026,11,8,18,0,tzinfo=UTC)),dt.date(2026,11,6))
    def test_thanksgiving(self):
        self.assertEqual(freshness.expected_completed_session(dt.datetime(2026,11,27,13,0,tzinfo=UTC)),dt.date(2026,11,25))
    def test_christmas(self):
        self.assertEqual(freshness.expected_completed_session(dt.datetime(2026,12,25,15,0,tzinfo=UTC)),dt.date(2026,12,24))
    def test_sessions_not_calendar_days(self):
        self.assertEqual(freshness.trading_sessions_behind('2026-11-04',dt.date(2026,11,5)),1)
        self.assertEqual(freshness.trading_sessions_behind('2026-10-30',dt.date(2026,11,2)),1)
    def test_fred_old_vix_blocks_but_new_sp500(self):
        now=dt.datetime(2026,11,6,10,0,tzinfo=UTC)
        raw={'VIX':mkseries(dt.date(2026,11,4),16.61),'GSPC':mkseries(dt.date(2026,11,5),7353.73),
             'HY':mkseries(dt.date(2026,11,4),3.32)}
        health=freshness.market_health(raw,{},now,{'VIX':'FRED','GSPC':'Yahoo'})
        self.assertFalse(health['critical_ready'])
        self.assertIn('VIX',health['critical_blockers'])
        self.assertEqual(health['rows']['VIX']['sessions_behind'],1)
        self.assertIn('HY',health['warnings'])
    def test_new_vix_restores_critical_ready(self):
        now=dt.datetime(2026,11,6,10,0,tzinfo=UTC)
        raw={'VIX':mkseries(dt.date(2026,11,5),28.18),'GSPC':mkseries(dt.date(2026,11,5),7353.73),
             'CCC':mkseries(dt.date(2026,11,4),12.97)}
        h=freshness.market_health(raw,{},now)
        self.assertTrue(h['critical_ready'])
        self.assertIn('CCC',h['warnings'])
    def test_no_market_data_fails_closed(self):
        h=freshness.market_health({}, {}, dt.datetime(2026,11,6,10,0,tzinfo=UTC))
        self.assertFalse(h['critical_ready'])
        self.assertEqual(set(h['critical_blockers']),{'VIX','GSPC'})
    def test_2025_new_year_observed(self):
        self.assertFalse(freshness.is_us_session(dt.date(2026,1,1)))
    def test_cpi_exact_month(self):
        d=[dt.date(2025,8,1),dt.date(2025,9,1),dt.date(2026,9,1)]
        self.assertEqual(engine.calendar_prior_index(d,2,months=12),1)
        self.assertEqual(engine.calendar_prior_index([d[0],d[2]],1,months=12),None)

if __name__=='__main__':unittest.main()
