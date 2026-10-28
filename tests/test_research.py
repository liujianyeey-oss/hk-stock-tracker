import datetime
import unittest
import contextlib
import io
import json
import zipfile
import tempfile
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from hk_stock_tracker import backtest
from hk_stock_tracker import research
from hk_stock_tracker import research_data
from hk_stock_tracker import tracker


def rows(count):
    start = datetime.date(2025, 1, 1)
    return [{"date": (start + datetime.timedelta(days=i)).isoformat(),
             "open": 100 + i, "close": 100 + i, "high": 101 + i,
             "low": 99 + i, "vol": 1000, "t": 2} for i in range(count)]


class ResearchTests(unittest.TestCase):
    def test_rolling_metrics_match_daily_prefixes(self):
        data = rows(25)
        for i, row in enumerate(data):
            row['close'] += 0.2 if i % 2 else -0.2
            row['t'] = 1 + (i % 3)
        profits, peaks = backtest.rolling_chip_metrics(data, 0.5)
        for i in range(len(data)):
            chips, centers, width = tracker.build_chips(data[:i+1], 0.5)
            daily = tracker.chip_metrics(chips, centers, width, data[i]['close'])
            self.assertAlmostEqual(profits[i], daily['profit'], places=8)
            self.assertAlmostEqual(peaks[i], daily['peaks'][0][0], places=8)

    def test_missing_market_session_is_not_a_trade(self):
        dates = ['2026-10-20', '2026-10-21', '2026-10-23', '2026-10-26']
        data = [dict(rows(1)[0], date=d) for d in dates]
        self.assertEqual(research.window_status(data, 0, 3), 'missing_or_non_session')
        self.assertEqual(research.trade_returns(data, [True, False, False, False], 1, 0, 4, 0), [])
        unknown = [dict(data[0], date='2022-01-03'), dict(data[1], date='2022-01-04')]
        self.assertEqual(research.window_status(unknown, 0, 1), 'calendar_unavailable')

    def test_grid_limit_rejects_small_width(self):
        with self.assertRaisesRegex(ValueError, '分箱'):
            backtest.rolling_chip_metrics(rows(1), width=0.000001)

    def test_snapshot_comparison_detects_revisions_and_missing_dates(self):
        before = rows(25)
        after = [dict(row) for row in before]
        after[3]['close'] += .5
        after.pop(5)
        def snapshot(data):
            return {'format': 'hk-tracker-normalized-kline-v1', 'stocks': [
                {'code': '00700', 'retrieved_at': '2026-01-01T17:00:00+08:00', 'rows': data}]}
        diff = research_data.compare_snapshots(snapshot(before), snapshot(after))['comparisons'][0]
        self.assertEqual(diff['changed_dates'], 1)
        self.assertIn('close', diff['changes'][0]['fields'])
        self.assertEqual(diff['missing_in_new_overlap'], [before[5]['date']])

    def test_kline_records_selected_field_and_response_hash(self):
        from hk_stock_tracker import tracker
        rec = ['2023-01-03', '10', '10', '11', '9', '100', '0', '1']
        payload = json.dumps({'data': {'hk00700': {'day': [rec], 'qfqday': [rec]}}})
        with patch.object(tracker, 'http_get', return_value=payload):
            result = tracker.fetch_kline('2023-01-03', '00700')
        self.assertEqual(result.provenance[0]['selected_field'], 'day')
        self.assertEqual(result.provenance[0]['returned_fields'], ['day', 'qfqday'])
        self.assertEqual(len(result.provenance[0]['response_text_sha256']), 64)

    def test_corporate_action_excludes_crossing_trade(self):
        dates = ['2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23']
        data = [dict(rows(1)[0], date=d) for d in dates]
        flags = [True, False, False, False]
        self.assertEqual(len(research.trade_returns(data, flags, 1, 0, 4, 0)), 1)
        self.assertEqual(research.trade_returns(data, flags, 1, 0, 4, 0, ['2026-10-22']), [])

    def test_historical_calendar_holidays_and_weather_closures(self):
        from hk_stock_tracker import hk_calendar
        for date in ('2023-01-23', '2023-07-17', '2023-09-01', '2023-09-08', '2024-09-06', '2025-10-07'):
            self.assertIsNone(hk_calendar.session_close(datetime.date.fromisoformat(date)))
        self.assertEqual(hk_calendar.session_close(datetime.date(2024, 2, 9)), datetime.time(12, 10))

    def test_plan_bundle_contains_verified_sources(self):
        tomorrow = datetime.datetime.now(research_data.HK).date() + datetime.timedelta(days=1)
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / 'plan.json'
            bundle = Path(tmp) / 'source.zip'
            rc = research_data.main(['plan', '--codes', '700', '--split', tomorrow.isoformat(),
                '--end', (tomorrow + datetime.timedelta(days=30)).isoformat(), '--cost-bps', '15',
                '--bundle', str(bundle), '--output', str(plan_path)])
            self.assertEqual(rc, 0)
            plan = json.loads(plan_path.read_text())
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(set(archive.namelist()), set(plan['code_sha256']))
                import hashlib
                for name in archive.namelist():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), plan['code_sha256'][name])
                archive.extractall(Path(tmp) / 'version')
            check = subprocess.run([sys.executable, '-B', str(Path(tmp) / 'version/research_data.py'),
                                    'check-plan', '--plan', str(plan_path)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            replay_check = subprocess.run([sys.executable, '-B', str(Path(tmp) / 'version/replay.py'), '--help'],
                                          capture_output=True, text=True)
            self.assertEqual(replay_check.returncode, 0, replay_check.stderr)

    def test_interval_requires_sufficient_data_and_is_reproducible(self):
        self.assertIsNone(research.block_interval([.01] * 29))
        values = [i / 1000 for i in range(30)]
        self.assertEqual(research.block_interval(values), research.block_interval(values))
        self.assertLessEqual(*research.block_interval(values))

    def test_causal_grid_is_independent_of_future_extremes(self):
        data = rows(30)
        later = rows(5)
        for i, row in enumerate(later):
            row.update(date=(datetime.date(2025, 2, 1) + datetime.timedelta(days=i)).isoformat(),
                       low=1, high=1000, open=10, close=10)
        before = backtest.rolling_chip_metrics(data)
        after = backtest.rolling_chip_metrics(data + later)
        self.assertEqual(before, (after[0][:len(data)], after[1][:len(data)]))

    def test_next_open_cost_boundary_and_overlap(self):
        data = rows(12)
        flags = [False] * len(data)
        flags[0] = flags[2] = flags[8] = True
        with patch.object(research.hk_calendar, "session_close", return_value=datetime.time(16, 10)):
            trades = research.trade_returns(data, flags, 3, 0, 10, 10)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['entry_date'], data[1]['date'])
        self.assertEqual(trades[0]['exit_date'], data[4]['date'])
        self.assertAlmostEqual(trades[0]['net_return'], 104 * .999 / (101 * 1.001) - 1)

    def test_split_prevents_exits_crossing_boundary(self):
        from hk_stock_tracker import hk_calendar
        dates = []
        day = datetime.date(2026, 1, 2)
        while len(dates) < 85:
            if hk_calendar.session_close(day) is not None:
                dates.append(day.isoformat())
            day += datetime.timedelta(days=1)
        data = [dict(rows(1)[0], date=date) for date in dates]
        split = dates[40]
        flags = [False] * len(data)
        for i in (22, 38, 42, 55):
            flags[i] = True
        signals = {key: (flags if key == 'S1' else [False] * len(data)) for key in research.SIGNALS}
        with patch.object(research.backtest, 'signal_flags', return_value=signals):
            result = research.run_stock('03690', data, split, 5)
        in_trades = result['results']['in_sample']['S1']['5']['trades']
        out_trades = result['results']['out_of_sample']['S1']['5']['trades']
        self.assertTrue(in_trades)
        self.assertTrue(out_trades)
        self.assertNotIn(dates[38], [t['signal_date'] for t in in_trades])
        for trade in in_trades:
            self.assertLess(trade['exit_date'], split)
        for trade in out_trades:
            self.assertGreaterEqual(trade['signal_date'], split)
            self.assertGreaterEqual(trade['entry_date'], split)

    def test_capture_and_offline_replay_without_overwrite(self):
        data = rows(60)
        with tempfile.TemporaryDirectory() as tmp:
            snap = str(Path(tmp) / 'snapshot.json')
            output = str(Path(tmp) / 'result.json')
            with patch.object(research_data, 'fetch_kline', return_value=data):
                self.assertEqual(research_data.main(['capture', '--codes', '700', '--end', '2025-03-01', '--output', snap]), 0)
                self.assertEqual(research_data.main(['capture', '--codes', '700', '--end', '2025-03-01', '--output', snap]), 1)
            with patch.object(research, 'fetch_kline', side_effect=AssertionError('network forbidden')):
                rc = research.main(['--codes', '700', '--split', '2025-02-01', '--end', '2025-03-01',
                                    '--cost-bps', '15', '--snapshot', snap, '--output', output])
            self.assertEqual(rc, 0)
            result = json.loads(Path(output).read_text())
            self.assertTrue(result['snapshot_used'])
            self.assertEqual(result['stocks'][0]['data_source']['historical_publication_time'], None)

    def test_plan_rejects_changed_code_hash_and_ambiguous_parameters(self):
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        with tempfile.TemporaryDirectory() as tmp:
            plan = str(Path(tmp) / 'plan.json')
            self.assertEqual(research_data.main(['plan', '--codes', '700', '--split', tomorrow.isoformat(),
                             '--end', (tomorrow + datetime.timedelta(days=30)).isoformat(), '--cost-bps', '15',
                             '--output', plan]), 0)
            with patch.object(research_data, 'code_hashes', return_value={'changed': 'yes'}):
                self.assertEqual(research.main(['--plan', plan, '--output', str(Path(tmp) / 'out.json')]), 1)
            self.assertEqual(research.main(['--plan', plan, '--codes', '700', '--output', str(Path(tmp) / 'out.json')]), 1)

    def test_invalid_cost_rejected(self):
        self.assertEqual(research.main(['--codes', '700', '--split', '2025-01-01',
                                        '--end', '2025-12-01', '--cost-bps', 'nan',
                                        '--output', '/tmp/unused-research-test.json']), 1)


if __name__ == '__main__':
    unittest.main()
