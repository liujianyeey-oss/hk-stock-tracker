import copy
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
from zoneinfo import ZoneInfo

from hk_stock_tracker import observe
from hk_stock_tracker import replay
from hk_stock_tracker import sensitivity
from test_observe import make_rows, plan


class ReplayTests(unittest.TestCase):
    def records(self):
        start = dt.date(2026, 10, 20)
        dates = replay.sessions(start, dt.date(2026, 10, 30))
        result = []
        for day in dates:
            now = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(17), ZoneInfo('Asia/Hong_Kong'))
            obj = observe.collect(plan(), 'shared', now, fetch=lambda end, code: make_rows(now.date()), clock=lambda: now)
            for s in obj['stocks']:
                for signal in s['signals'].values():
                    signal['first_hit'] = False
            result.append(obj)
        result[0]['stocks'][0]['signals']['S1']['first_hit'] = True
        return result

    def test_review_complete_and_pending_and_missing(self):
        records = self.records()
        out = replay.review(records)
        one = [r for r in out['outcomes'] if r['horizon'] == 5][0]
        self.assertEqual(one['status'], 'complete')
        self.assertIn('00700', replay.markdown_report(out))
        self.assertEqual(one['entry_date'], '2026-10-21')
        self.assertEqual(one['exit_date'], '2026-10-28')
        self.assertAlmostEqual(one['net_return'], .9985 / 1.0015 - 1)
        self.assertIn('pending', [r['status'] for r in out['outcomes']])
        missing = replay.review(records[:2] + records[3:])
        self.assertIn('2026-10-22', missing['missing_observation_dates'])
        self.assertIn('missing_observation', [r['status'] for r in missing['outcomes']])

    def test_revised_entry_price_is_not_used_for_return(self):
        records = self.records()
        entry_day = '2026-10-21'
        exit_record = next(r for r in records if r['date'] == '2026-10-28')
        for row in exit_record['stocks'][0]['rows']:
            if row['date'] == entry_day:
                row['open'] = 5
        outcomes = replay.review(records)['outcomes']
        outcome = next(x for x in outcomes if x['horizon'] == 5)
        self.assertEqual(outcome['status'], 'price_revision')
        self.assertNotIn('net_return', outcome)

    def test_duplicate_and_mismatched_plan_are_rejected(self):
        records = self.records()
        with self.assertRaisesRegex(ValueError, '重复'):
            replay.review([records[0], records[0]])
        bad = copy.deepcopy(records[1]); bad['plan_sha256'] = 'other'
        with self.assertRaisesRegex(ValueError, '不一致'):
            replay.review([records[0], bad])

    def test_refuses_recompute_with_different_strategy_version(self):
        records = self.records()
        with patch.object(replay.research_data, 'code_hashes', return_value={'different.py': 'hash'}):
            with self.assertRaisesRegex(ValueError, '版本不一致'):
                replay.review(records)

    def test_output_failure_does_not_leave_partial_or_overwrite(self):
        result = replay.review(self.records())
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result.json'
            markdown = Path(tmp) / 'report.md'
            markdown.write_text('keep', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                replay.save_reports(str(output), result, str(markdown))
            self.assertFalse(output.exists())
            self.assertEqual(markdown.read_text(encoding='utf-8'), 'keep')
            with self.assertRaisesRegex(ValueError, '不同路径'):
                replay.save_reports(str(output), result, str(output))
            self.assertFalse(output.exists())

    def test_archived_version_executes_replay(self):
        records = self.records()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in replay.research_data.code_hashes():
                shutil.copy2(replay.research_data.ROOT / name, directory / name)
            paths = []
            for record in records:
                path = directory / (record['date'] + '.json')
                path.write_text(json.dumps(record), encoding='utf-8')
                paths.append(str(path))
            output = directory / 'review.json'
            completed = subprocess.run([sys.executable, '-B', str(directory / 'replay.py'), *paths,
                                        '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text())['plan_sha256'], 'shared')

    def test_sensitivity_results(self):
        values = sensitivity.compare(make_rows(dt.date(2026, 10, 28)), [0.5, 1], [21, 30])
        self.assertEqual(len(values), 4)
        self.assertEqual({v['window_actual'] for v in values}, {21, 30})


if __name__ == '__main__':
    unittest.main()
