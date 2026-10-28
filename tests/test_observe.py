import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hk_stock_tracker import observe
from hk_stock_tracker import research_data

HK = ZoneInfo('Asia/Hong_Kong')


def make_rows(day):
    return [{'date': (day - dt.timedelta(days=29-i)).isoformat(), 'open': 10,
             'high': 11, 'low': 9, 'close': 10, 'vol': 100, 't': 1} for i in range(30)]


def plan():
    return {'format': 'hk-tracker-study-plan-v1', 'created_at': '2026-10-01T12:00:00+08:00',
            'split': '2026-10-02', 'end': '2026-12-31', 'codes': ['00700'],
            'code_sha256': research_data.code_hashes(), 'cost_bps_per_side': 15}


class ObservationTests(unittest.TestCase):
    def test_records_today_data_and_signals(self):
        now = dt.datetime(2026, 10, 28, 16, 30, tzinfo=HK)
        rows = make_rows(now.date())
        result = observe.collect(plan(), 'abc', now, fetch=lambda end, code: rows, clock=lambda: now)
        stock = result['stocks'][0]
        self.assertEqual(stock['rows'], rows)
        self.assertEqual(stock['rows'][-1]['date'], '2026-10-28')
        self.assertEqual(set(stock['signals']), {'S1', 'S2', 'S3u', 'S3d'})
        self.assertEqual(result['plan_sha256'], 'abc')
        self.assertEqual(stock['historical_publication_time'], None)

    def test_refuses_during_session_and_missing_today(self):
        now = dt.datetime(2026, 10, 28, 15, 0, tzinfo=HK)
        with self.assertRaisesRegex(ValueError, '尚未确认收市'):
            observe.collect(plan(), 'abc', now)
        now = now.replace(hour=17)
        with self.assertRaisesRegex(ValueError, '缺少当天'):
            observe.collect(plan(), 'abc', now, fetch=lambda end, code: make_rows(now.date() - dt.timedelta(days=1)), clock=lambda: now)

    def test_rejects_code_change_or_cross_day_capture(self):
        now = dt.datetime(2026, 10, 28, 17, 0, tzinfo=HK)
        altered = plan()
        altered['code_sha256'] = {}
        with self.assertRaisesRegex(ValueError, '代码已变化'):
            observe.collect(altered, 'abc', now)
        with self.assertRaisesRegex(ValueError, '跨日抓取'):
            observe.collect(plan(), 'abc', now, fetch=lambda end, code: make_rows(now.date()),
                            clock=lambda: now + dt.timedelta(days=1))


if __name__ == '__main__':
    unittest.main()
