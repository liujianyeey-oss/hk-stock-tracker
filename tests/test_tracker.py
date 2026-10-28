import contextlib
import datetime
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hk_stock_tracker import tracker as mt
from hk_stock_tracker import backtest
from hk_stock_tracker import hk_calendar


class InputTests(unittest.TestCase):
    def test_normalize_codes_and_reject_other_text(self):
        for text in ("700", "0700", "00700", "700.HK", "700.hk"):
            self.assertEqual(mt.normalize_hk_code(text), "00700")
        for text in ("", "0", "abc700", "700.US", "1/2", "123456"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                mt.normalize_hk_code(text)

    def test_missing_turnover_uses_only_prior_observations(self):
        rows = [{"date": "a", "t": None}, {"date": "b", "t": 2.0},
                {"date": "c", "t": None}, {"date": "d", "t": 80.0}]
        self.assertEqual(mt.fill_missing_turnover(rows), ["a", "c"])
        self.assertEqual([r["t"] for r in rows], [0.5, 2.0, 2.0, 80.0])
        with self.assertRaises(ValueError):
            mt.fill_missing_turnover([{"date": "e", "t": 101}])

    def test_config_width_and_rule_validation(self):
        with self.assertRaises(ValueError):
            mt.daily_bin_width({"chip_bin_width": 0}, 10)
        with self.assertRaises(ValueError):
            mt.rule_match({"typo": 1}, 10, 1)
        self.assertTrue(mt.rule_match({"close_above": 9, "turnover_above": 1}, 10, 1))

    def test_narrow_price_range_preserves_chip_mass(self):
        data = [{"date": "2024-01-01", "open": 10.01, "high": 10.02,
                 "low": 10.01, "close": 10.02, "vol": 1, "t": 1}]
        chips, _, _ = mt.build_chips(data, 0.5)
        self.assertAlmostEqual(sum(chips), 100.0)

    def test_kline_end_date_is_respected_even_for_overlapping_requests(self):
        rows = [["2023-01-01", "1", "1", "1", "1", "1", "0", "1"],
                ["2024-01-01", "1", "1", "1", "1", "1", "0", "1"]]
        with patch.object(mt, "http_get", return_value=json.dumps({"data": {"hk03690": {"day": rows}}})):
            data = mt.fetch_kline("2023-12-31")
        self.assertEqual([r["date"] for r in data], ["2023-01-01"])


class DecisionAndPersistenceTests(unittest.TestCase):
    def test_entry_requires_adequate_known_risk_reward_but_exit_is_preserved(self):
        levels = {"playbook": [
            {"id": "buy", "action": "entry", "when": {"close_above": 9}},
            {"id": "exit", "action": "exit", "when": {"close_above": 9}},
        ]}
        for ratio in (None, (5, 10, .5)):
            fired, suppressed = mt.evaluate_playbook(levels, 10, 1, ratio, intraday=False, t_est=False)
            self.assertEqual([r["id"] for r in fired], ["exit"])
            self.assertTrue(any("buy" in reason for reason in suppressed))
        fired, suppressed = mt.evaluate_playbook(levels, 10, 1, (10, 5, 2), intraday=False, t_est=False)
        self.assertEqual([r["id"] for r in fired], ["buy", "exit"])
        self.assertFalse(suppressed)

    def test_close_and_estimated_turnover_are_not_treated_as_confirmed(self):
        levels = {"playbook": [
            {"id": "close", "when": {"close_above": 9}},
            {"id": "volume", "when": {"turnover_above": .8}},
        ]}
        fired, suppressed = mt.evaluate_playbook(levels, 10, 1, None, intraday=True, t_est=True)
        self.assertFalse(fired)
        self.assertTrue(any("盘中" in reason for reason in suppressed))
        self.assertTrue(any("估算" in reason for reason in suppressed))

    def test_draft_suppresses_all_rules(self):
        levels = {"auto_generated": True, "playbook": [{"id": "x", "when": {"close_above": 9}}]}
        self.assertEqual(mt.evaluate_playbook(levels, 10, 1, (1, 1, 1), intraday=False, t_est=False), ([], []))

    def test_atomic_write_failure_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshot.csv"
            path.write_text("existing", encoding="utf-8")
            with patch.object(mt.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    mt.atomic_write(path, lambda fh: fh.write("replacement"))
            self.assertEqual(path.read_text(encoding="utf-8"), "existing")
            self.assertEqual([p.name for p in Path(d).iterdir()], ["snapshot.csv"])

    def test_snapshot_replaces_same_day_and_keeps_other_dates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshots.csv"
            row = dict.fromkeys(mt.SNAP_FIELDS, "")
            for date, close in [("2026-01-01", "1"), ("2026-01-02", "2"), ("2026-01-02", "3")]:
                mt.save_snapshot({**row, "date": date, "close": close}, str(path))
            rows = mt.load_snapshots(str(path))
            self.assertEqual([(r["date"], r["close"]) for r in rows],
                             [("2026-01-01", "1"), ("2026-01-02", "3")])


class MarketCalendarTests(unittest.TestCase):
    def test_full_day_auction_boundary(self):
        zone = ZoneInfo("Asia/Hong_Kong")
        before = datetime.datetime(2026, 10, 28, 16, 9, tzinfo=zone)
        after = before.replace(minute=10)
        self.assertFalse(hk_calendar.is_finished(before.date(), before))
        self.assertTrue(hk_calendar.is_finished(after.date(), after))

    def test_half_day_auction_boundary(self):
        zone = ZoneInfo("Asia/Hong_Kong")
        before = datetime.datetime(2026, 12, 24, 12, 9, tzinfo=zone)
        self.assertFalse(hk_calendar.is_finished(before.date(), before))
        self.assertTrue(hk_calendar.is_finished(before.date(), before.replace(minute=10)))

    def test_weekend_and_holiday_do_not_finish_session(self):
        for value in (datetime.date(2026, 12, 25), datetime.date(2026, 12, 26)):
            self.assertIsNone(hk_calendar.session_close(value))

    def test_unknown_year_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "交易日历未配置"):
            hk_calendar.session_close(datetime.date(2028, 1, 3))


class TransactionTests(unittest.TestCase):
    def make_data(self, root):
        data, reports = Path(root) / "data", Path(root) / "reports"
        data.mkdir(); reports.mkdir()
        snapshot = data / "snapshots.csv"
        row = dict.fromkeys(mt.SNAP_FIELDS, "")
        mt.save_snapshot({**row, "date": "2026-10-27", "close": "10"}, str(snapshot))
        report = reports / "2026-10-28.md"
        report.write_text("old report", encoding="utf8")
        return data, reports, snapshot, report, {**row, "date": "2026-10-28", "close": "11"}

    def test_interrupted_report_write_recovers_both_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            data, reports, snapshot, report, row = self.make_data(root)
            real = mt.atomic_write
            def fail_report(path, write_content, **kwargs):
                if Path(path) == report:
                    raise OSError("simulated interruption")
                return real(path, write_content, **kwargs)
            with patch.object(mt, "atomic_write", side_effect=fail_report):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    mt.commit_daily(str(data), str(reports), "2026-10-28", row, "new report")
            self.assertTrue((data / ".daily-transaction.json").exists())
            self.assertEqual(report.read_text(), "old report")
            self.assertTrue(mt.recover_daily(str(data), str(reports)))
            self.assertFalse((data / ".daily-transaction.json").exists())
            self.assertEqual(report.read_text(), "new report")
            self.assertEqual(mt.load_snapshots(str(snapshot))[-1]["close"], "11")
            self.assertFalse(mt.recover_daily(str(data), str(reports)))

    def test_recovery_refuses_to_overwrite_external_changes(self):
        with tempfile.TemporaryDirectory() as root:
            data, reports, snapshot, report, row = self.make_data(root)
            real = mt.atomic_write
            def fail_report(path, write_content, **kwargs):
                if Path(path) == report:
                    raise OSError("interruption")
                return real(path, write_content, **kwargs)
            with patch.object(mt, "atomic_write", side_effect=fail_report):
                with self.assertRaises(OSError):
                    mt.commit_daily(str(data), str(reports), "2026-10-28", row, "new report")
            report.write_text("external change", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "外部修改"):
                mt.recover_daily(str(data), str(reports))
            self.assertEqual(report.read_text(), "external change")
            self.assertTrue((data / ".daily-transaction.json").exists())

    def test_next_daily_recovers_before_running_new_work(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            data, reports, snapshot, report, row = self.make_data(root)
            real = mt.atomic_write
            def fail_report(path, write_content, **kwargs):
                if Path(path) == report:
                    raise OSError("interruption")
                return real(path, write_content, **kwargs)
            with patch.object(mt, "atomic_write", side_effect=fail_report):
                with self.assertRaises(OSError):
                    mt.commit_daily(str(data), str(reports), "2026-10-28", row, "new report")
            with patch.object(mt, "DATA_DIR", str(data)), patch.object(mt, "REPORT_DIR", str(reports)), \
                 patch.object(mt, "_daily", return_value=42), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(mt.daily(), 42)
                self.assertIn("已恢复", output.getvalue())
            self.assertEqual(report.read_text(), "new report")
            self.assertEqual(mt.load_snapshots(str(snapshot))[-1]["close"], "11")

    def test_corrupt_journal_is_retained(self):
        with tempfile.TemporaryDirectory() as root:
            data, reports = Path(root) / "data", Path(root) / "reports"
            data.mkdir(); reports.mkdir()
            journal = data / ".daily-transaction.json"
            journal.write_text("broken", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "事务记录损坏"):
                mt.recover_daily(str(data), str(reports))
            self.assertTrue(journal.exists())


class BacktestTests(unittest.TestCase):
    def test_southbound_and_short_data_not_used_on_same_day(self):
        data = [{"date": f"2025-01-{i:02}", "close": 10, "t": 1} for i in range(1, 9)]
        south = {r["date"]: -1 for r in data[:5]}
        short = {data[4]["date"]: 30}
        flags = backtest.signal_flags(data, [50] * 8, [11] * 8, short, south)
        self.assertFalse(flags["S5b"][4])
        self.assertTrue(flags["S5b"][5])
        self.assertFalse(flags["S4"][4])
        self.assertTrue(flags["S4"][5])

    def test_estimated_turnover_does_not_trigger_volume_signal(self):
        data = [{"date": f"2025-01-{i:02}", "close": 10 if i < 21 else 12, "t": 1 if i < 21 else 5}
                for i in range(1, 23)]
        flags = backtest.signal_flags(data, [50] * 22, [11] * 22, {}, {}, [data[20]["date"]])
        self.assertFalse(flags["S2"][20])
        self.assertFalse(flags["S3u"][20])

    def test_southbound_pagination_uses_raw_count_not_assumed_duplicates(self):
        start = datetime.date(2023, 1, 1)
        rows = [{"HOLD_DATE": (start + datetime.timedelta(days=i)).isoformat(), "HOLD_SHARES_CHANGE": 1}
                for i in range(800)]
        pages = [io.BytesIO(json.dumps({"result": {"data": chunk, "count": 800}}).encode())
                 for chunk in (rows[:500], rows[500:])]
        with patch.object(backtest.urllib.request, "urlopen", side_effect=pages) as request:
            result = backtest.fetch_southbound_all()
        self.assertEqual(len(result), 800)
        self.assertEqual(request.call_count, 2)

    def test_overwrite_failure_keeps_previous_backtest_report(self):
        today = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
        data = [{"date": (today - datetime.timedelta(days=90-i)).isoformat(), "open": 10,
                 "close": 10, "high": 11, "low": 9, "vol": 100, "t": 1} for i in range(90)]
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "existing.md"
            dest.write_text("preserve", encoding="utf8")
            with patch.object(backtest, "fetch_kline", return_value=data), \
                 patch.object(backtest, "fetch_short_selling", return_value=[]), \
                 patch.object(backtest, "fetch_southbound_all", return_value={}), \
                 patch.object(mt.os, "replace", side_effect=OSError("disk error")), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(OSError, "disk error"):
                    backtest.main(today.isoformat(), str(dest), overwrite=True)
            self.assertEqual(dest.read_text(encoding="utf8"), "preserve")

    def test_report_will_not_overwrite_existing_file(self):
        today = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
        data = []
        for i in range(90):
            day = (today - datetime.timedelta(days=90 - i)).isoformat()
            data.append({"date": day, "open": 10, "close": 10, "high": 11, "low": 9, "vol": 100, "t": 1})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "existing.md"
            dest.write_text("preserve", encoding="utf8")
            with patch.object(backtest, "fetch_kline", return_value=data), \
                 patch.object(backtest, "fetch_short_selling", return_value=[]), \
                 patch.object(backtest, "fetch_southbound_all", return_value={}), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "报告已存在"):
                    backtest.main(today.isoformat(), str(dest))
            self.assertEqual(dest.read_text(encoding="utf8"), "preserve")


class ConcurrencyAndDataSourceTests(unittest.TestCase):
    def test_process_lock_blocks_same_stock_and_releases(self):
        helper = ("import sys\nfrom hk_stock_tracker import tracker as mt\n"
                  "try:\n"
                  " with mt.stock_lock(sys.argv[1]): pass\n"
                  "except ValueError as exc:\n"
                  " print(exc); sys.exit(3)\n")
        with tempfile.TemporaryDirectory() as root:
            one, two = str(Path(root) / "one"), str(Path(root) / "two")
            with mt.stock_lock(one):
                blocked = subprocess.run([sys.executable, "-B", "-c", helper, one],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(blocked.returncode, 3, blocked.stderr)
                self.assertIn("已有任务运行", blocked.stdout)
                other = subprocess.run([sys.executable, "-B", "-c", helper, two],
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual(other.returncode, 0, other.stderr)
            released = subprocess.run([sys.executable, "-B", "-c", helper, one],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(released.returncode, 0, released.stderr)

    def test_stock_lock_releases_when_task_raises(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with mt.stock_lock(root):
                    raise RuntimeError("boom")
            with mt.stock_lock(root):
                pass

    def test_intraday_kline_not_used_as_close_in_backtest(self):
        rows = [{"date": "2026-10-27"}, {"date": "2026-10-28"}]
        morning = datetime.datetime(2026, 10, 28, 10, 30, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        auction = morning.replace(hour=16, minute=5)
        evening = morning.replace(hour=16, minute=10)
        self.assertEqual(backtest.completed_kline(rows, morning), rows[:1])
        self.assertEqual(backtest.completed_kline(rows, auction), rows[:1])
        self.assertEqual(backtest.completed_kline(rows, evening), rows)

    def test_kline_dates_must_be_unique_and_sorted(self):
        item = {"date": "2026-01-02", "open": 10, "close": 10, "high": 11, "low": 9, "vol": 1}
        with self.assertRaisesRegex(ValueError, "严格递增"):
            mt.validate_kline([item, dict(item)])
        with self.assertRaisesRegex(ValueError, "日期无效"):
            mt.validate_kline([{**item, "date": "invalid"}])

    def test_short_sell_table_wins_over_overlapping_chart_data(self):
        html = ("<td>01/01/2026</td><td>100</td><td>1000</td><td>-</td><td>-</td><td>10%</td>"
                "drawComboChart2('stock','title',[\"01/01/2026\",\"02/01/2026\"],"
                "[10,20],[20,30],[1,2])")
        with patch.object(mt.urllib.request, "urlopen", return_value=io.BytesIO(html.encode())):
            data = mt.fetch_short_selling("03690")
        self.assertEqual([r["date"] for r in data], ["2026-01-02", "2026-01-01"])
        self.assertEqual(data[1]["ratio"], 10)
        self.assertNotIn("amount_estimated", data[1])
        self.assertTrue(data[0]["amount_estimated"])


class EndToEndTests(unittest.TestCase):
    def test_init_missing_turnover_and_draft_daily_suppresses_playbook(self):
        today = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
        data = []
        for i in range(90):
            date = (today - datetime.timedelta(days=89-i)).isoformat()
            data.append({"date": date, "open": 10, "close": 10, "high": 10.5,
                         "low": 9.5, "vol": 1000, "t": None if i == 89 else 1})
        quote = {"name": "示例", "price": 10, "prev_close": 10, "pct": 0,
                 "high": 10.5, "low": 9.5, "time": datetime.datetime.combine(today, datetime.time(16)),
                 "w52_high": 11, "w52_low": 9}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(mt, "DATA_DIR", str(root / "data")), patch.object(mt, "REPORT_DIR", str(root / "reports")), \
                 patch.object(mt, "fetch_quote", return_value=quote), patch.object(mt, "fetch_kline", side_effect=lambda *args: [dict(x) for x in data]), \
                 patch.object(mt, "fetch_short_selling", return_value=[]), patch.object(mt, "fetch_southbound", return_value=[]), \
                 patch.object(hk_calendar, "session_close", return_value=datetime.time(16, 10)), \
                 patch.object(hk_calendar, "is_finished", return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(mt.init_stock("00700"), 0)
                config = root / "data" / "00700" / "levels.json"
                levels = json.loads(config.read_text())
                levels["playbook"] = [{"id": "buy", "group": "test", "when": {"close_above": 1}, "stance": "测试买入"}]
                config.write_text(json.dumps(levels), encoding="utf8")
                self.assertEqual(mt.daily("00700"), 0)
                self.assertNotIn("测试买入", output.getvalue())
                self.assertIn("仅供观察", output.getvalue())
                self.assertTrue((root / "reports" / "00700" / f"{today}.md").exists())

    def test_draft_intraday_emits_no_level_trade_signal(self):
        today = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
        levels = {"auto_generated": True, "thresholds": {"vol_active": 0.8, "spike_x": 2, "lull_x": .5},
                  "playbook": [], "default_stance": "观察", "resistance": [{"price": 10, "note": "草稿"}],
                  "support": [{"price": 9, "note": "草稿"}]}
        data = [{"date": (today - datetime.timedelta(days=30-i)).isoformat(), "open": 10, "close": 10,
                 "high": 11, "low": 9, "vol": 10000, "t": 1} for i in range(30)]
        mins = []
        for i in range(40):
            m = 30 + i
            price = 10.1 if i < 39 else 10
            vol = 100 if i < 35 else 1000
            prev_v = mins[-1]["cumvol"] if mins else 0
            prev_a = mins[-1]["cumamt"] if mins else 0
            mins.append({"hm": f"{9 + m // 60:02}{m % 60:02}", "price": price,
                         "cumvol": prev_v + vol, "cumamt": prev_a + price * vol})
        quote = {"name": "示例", "price": 10, "pct": 0, "time": datetime.datetime.combine(today, datetime.time(10, 10))}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "data" / "00700").mkdir(parents=True)
            (root / "data" / "00700" / "levels.json").write_text(json.dumps(levels), encoding="utf8")
            with patch.object(mt, "DATA_DIR", str(root / "data")), patch.object(mt, "REPORT_DIR", str(root / "reports")), \
                 patch.object(mt, "fetch_quote", return_value=quote), patch.object(mt, "fetch_kline", return_value=data), \
                 patch.object(mt, "fetch_minute", return_value=mins), patch.object(mt, "fetch_index", return_value={"price": 100, "pct": 0}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(mt.intraday("00700"), 0)
                self.assertNotIn("做T减仓", output.getvalue())
                self.assertNotIn("做T接回", output.getvalue())
                self.assertIn("仅观察结构变化", output.getvalue())


if __name__ == "__main__":
    unittest.main()
