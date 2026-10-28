#!/usr/bin/env python3
"""Fixed-rule, next-open signal study across explicitly selected HK stocks."""
import argparse
import datetime as dt
import hashlib
import json
import math
import datetime
import random
import sys
from zoneinfo import ZoneInfo

if __package__:
    from . import hk_calendar
    from . import backtest
    from . import research_data
    from .tracker import fetch_kline, fill_missing_turnover, normalize_hk_code, validate_kline
else:
    import hk_calendar
    import backtest
    import research_data
    from tracker import fetch_kline, fill_missing_turnover, normalize_hk_code, validate_kline

SIGNALS = ("S1", "S2", "S3u", "S3d")
HORIZONS = (5, 10, 20)


def block_interval(values, block_size=3, draws=1000):
    """Exploratory moving-block interval for the mean of ordered trade returns."""
    if len(values) < 30:
        return None
    rng = random.Random(42)
    n = len(values)
    means = []
    for _ in range(draws):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n - block_size + 1)
            sample.extend(values[start:start + block_size])
        means.append(sum(sample[:n]) / n * 100)
    means.sort()
    return [means[int(draws * .025)], means[min(draws - 1, int(draws * .975))]]


def window_status(rows, start, end):
    """Reject windows with missing sessions or dates outside the verified calendar."""
    first = dt.date.fromisoformat(rows[start]['date'])
    last = dt.date.fromisoformat(rows[end]['date'])
    if any(row['date'] in hk_calendar.IRREGULAR_SESSIONS for row in rows[start:end + 1]):
        return 'irregular_session'
    expected = []
    day = first
    while day <= last:
        try:
            if hk_calendar.session_close(day) is not None:
                expected.append(day.isoformat())
        except ValueError:
            return 'calendar_unavailable'
        day += dt.timedelta(days=1)
    return 'complete' if expected == [r['date'] for r in rows[start:end + 1]] else 'missing_or_non_session'


def trade_returns(rows, flags, horizon, start, stop, cost_bps, action_dates=()):
    """Signal at close; next open entry; exit at open after h full sessions.

    stop is exclusive and both decision and execution must be in the window.
    No overlapping trades within a signal/horizon/window.
    """
    trades = []
    next_free = start
    for i in range(max(0, start), min(stop, len(rows))):
        if not flags[i] or (i and flags[i - 1]) or i < next_free:
            continue
        entry, exit_ = i + 1, i + 1 + horizon
        if exit_ >= min(stop, len(rows)):
            continue
        if window_status(rows, i, exit_) != "complete":
            continue
        if any(rows[i]["date"] < day <= rows[exit_]["date"] for day in action_dates):
            continue
        buy, sell = rows[entry]["open"], rows[exit_]["open"]
        if not buy or not sell or buy <= 0 or sell <= 0:
            continue
        # Per-side cost approximates fees, spread and slippage on each notional.
        cost = cost_bps / 10000
        ret = sell * (1 - cost) / (buy * (1 + cost)) - 1
        trades.append({"signal_date": rows[i]["date"], "entry_date": rows[entry]["date"],
                       "exit_date": rows[exit_]["date"], "net_return": ret})
        next_free = exit_
    return trades


def run_stock(code, rows, split, cost_bps, chip_width=0.5, action_dates=()):
    validate_kline(rows, minimum=21)
    rows = [dict(row) for row in rows]
    estimated = fill_missing_turnover(rows)
    profits, peaks = backtest.rolling_chip_metrics(rows, chip_width)
    # Publication timestamps for these sources are unverified. Strict mode excludes them.
    flags = backtest.signal_flags(rows, profits, peaks, {}, {}, estimated)
    boundary = next((i for i, row in enumerate(rows) if row['date'] >= split), len(rows))
    if boundary < 21 or boundary == len(rows):
        raise ValueError(f'{code}: 划分日期必须留下至少 21 个样本内交易日及样本外数据')
    results = {}
    for label, start, stop in (("in_sample", 0, boundary), ("out_of_sample", boundary, len(rows))):
        results[label] = {}
        cost = cost_bps / 10000
        baseline = {}
        for horizon in HORIZONS:
            daily = [rows[i+1+horizon]['open'] * (1-cost) / (rows[i+1]['open'] * (1+cost)) - 1
                     for i in range(start, stop) if i + 1 + horizon < stop
                     and window_status(rows, i, i + 1 + horizon) == "complete"
                     and not any(rows[i]["date"] < day <= rows[i+1+horizon]["date"] for day in action_dates)]
            baseline[str(horizon)] = backtest.stats(daily)
        results[label]['baseline_all_days'] = baseline
        results[label]['excluded_windows'] = {}
        for horizon in HORIZONS:
            reasons = {}
            for i in range(start, stop):
                if i + 1 + horizon < stop:
                    reason = window_status(rows, i, i + 1 + horizon)
                    if reason == 'complete' and any(rows[i]['date'] < day <= rows[i+1+horizon]['date'] for day in action_dates):
                        reason = 'corporate_action'
                    if reason != 'complete':
                        reasons[reason] = reasons.get(reason, 0) + 1
            results[label]['excluded_windows'][str(horizon)] = reasons
        for signal in SIGNALS:
            results[label][signal] = {}
            for horizon in HORIZONS:
                trades = trade_returns(rows, flags[signal], horizon, start, stop, cost_bps, action_dates)
                ordered = [t['net_return'] for t in trades]
                values = sorted(ordered)
                summary = backtest.stats(values)
                if summary:
                    summary['p25_pct'] = values[(len(values)-1)//4] * 100
                    summary['p75_pct'] = values[3*(len(values)-1)//4] * 100
                    summary['mean_block_bootstrap_95_pct'] = block_interval(ordered)
                    summary['evidence'] = '样本少于30笔，仅作描述' if len(values) < 30 else '描述性统计，仍需独立验证'
                results[label][signal][str(horizon)] = {
                    "summary": summary, "trades": trades}
    return {"code": code, "window_filter": '仅使用静态日历覆盖且日期连续的窗口；缺失可能为停牌、临时停市或数据缺口', "first_date": rows[0]['date'], "last_date": rows[-1]['date'],
            "estimated_turnover_dates": estimated, "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="港股固定规则样本内/样本外研究（不构成交易建议）")
    parser.add_argument('--codes', nargs='+', help='预先选定的港股代码')
    parser.add_argument('--split', help='样本外开始日期 YYYY-MM-DD（含）')
    parser.add_argument('--end', help='截止日期 YYYY-MM-DD（含）')
    parser.add_argument('--chip-width', type=float, help='筹码分箱宽度，默认0.5；应与日报配置一致')
    parser.add_argument('--cost-bps', type=float, help='每边总成本假设，单位基点；需自行估计')
    parser.add_argument('--output', required=True, help='新建 JSON 结果文件，不覆盖')
    parser.add_argument('--actions', help='经核验的公司行为 JSON；排除跨事件交易窗口')
    parser.add_argument('--snapshot', help='由 research_data.py capture 生成的快照')
    parser.add_argument('--plan', help='由 research_data.py plan 生成的预声明计划；不能与研究参数同时使用')
    args = parser.parse_args(argv)
    try:
        plan = None
        if args.plan and args.actions:
            raise ValueError('计划模式尚未固定公司行为文件，请勿混用 --actions')
        if args.plan:
            if any(x is not None for x in (args.codes, args.split, args.end, args.cost_bps, args.chip_width)):
                raise ValueError('使用 --plan 时不能同时指定股票、日期或成本')
            with open(args.plan, encoding='utf-8') as fh:
                plan = json.load(fh)
            if plan.get('format') != 'hk-tracker-study-plan-v1' or plan.get('code_sha256') != research_data.code_hashes():
                raise ValueError('计划格式无效或研究代码已变化')
            created = dt.datetime.fromisoformat(plan['created_at'])
            if created.tzinfo is None or created.astimezone(ZoneInfo('Asia/Hong_Kong')).date() >= dt.date.fromisoformat(plan['split']):
                raise ValueError('计划创建时间须早于样本外起始日期')
            args.codes, args.split, args.end, args.cost_bps = (plan['codes'], plan['split'], plan['end'], plan['cost_bps_per_side'])
            args.chip_width = plan.get('chip_width', 0.5)
        if any(x is None for x in (args.codes, args.split, args.end, args.cost_bps)):
            raise ValueError('需要提供 --plan 或全部股票、日期和成本参数')
        if args.chip_width is None:
            args.chip_width = 0.5
        if not math.isfinite(args.chip_width) or args.chip_width <= 0:
            raise ValueError('分箱宽度必须为正有限数')
        split, end = dt.date.fromisoformat(args.split), dt.date.fromisoformat(args.end)
        now = dt.datetime.now(ZoneInfo('Asia/Hong_Kong'))
        if split >= end or end > now.date():
            raise ValueError('要求划分日期早于截止日期，且截止日期不能晚于今天')
        if not math.isfinite(args.cost_bps) or not 0 <= args.cost_bps < 10000:
            raise ValueError('每边成本必须为 0（含）到 10000（不含）基点')
        codes = [normalize_hk_code(code) for code in args.codes]
        if len(set(codes)) != len(codes):
            raise ValueError('股票代码不能重复')
        snapshot = None
        snapshot_sha256 = None
        if args.snapshot:
            with open(args.snapshot, 'rb') as fh:
                content = fh.read()
            snapshot_sha256 = hashlib.sha256(content).hexdigest()
            snapshot = json.loads(content)
            if snapshot.get('format') != 'hk-tracker-normalized-kline-v1' or snapshot['requested_end'] < args.end:
                raise ValueError('快照格式无效或截止日期不足')
            snapstocks = {item['code']: item for item in snapshot['stocks']}
            if len(snapstocks) != len(snapshot['stocks']) or any(code not in snapstocks for code in codes):
                raise ValueError('快照股票重复或缺少研究标的')
        actions = None
        actions_sha256 = None
        if args.actions:
            with open(args.actions, 'rb') as fh:
                action_content = fh.read()
            actions = json.loads(action_content)
            actions_sha256 = hashlib.sha256(action_content).hexdigest()
            if actions.get('format') != 'hk-tracker-corporate-actions-v1':
                raise ValueError('公司行为文件格式无效')
            for code in codes:
                entry = actions['stocks'][code]
                coverage_start = dt.date.fromisoformat(entry['coverage_start'])
                coverage_end = dt.date.fromisoformat(entry['coverage_end'])
                if coverage_start > coverage_end or coverage_start > split or coverage_end < end:
                    raise ValueError(f'{code}: 公司行为覆盖范围不足')
                for event in entry['events']:
                    event_day = dt.date.fromisoformat(event['date'])
                    if not coverage_start <= event_day <= coverage_end:
                        raise ValueError(f'{code}: 公司行为日期超出覆盖范围')
                    if event.get('type') not in ('cash_dividend', 'split', 'rights', 'other'):
                        raise ValueError(f'{code}: 公司行为类型无效')
        result = {"method": "fixed rules; signal close; next open entry; open exit after horizon sessions",
                  "split": args.split, "end": args.end, "cost_bps_per_side": args.cost_bps, "chip_width": args.chip_width,
                  "fetched_at_hong_kong": now.isoformat(),
                  "limitations": ["腾讯日K可能前复权或被修订；非 point-in-time 数据",
                    "信号日收盘数据实际可用时间未经核实；未使用沽空及南向信号",
                    "使用下一开盘价的假设成交，未建模停牌、流动性、涨跌跳空和实际撮合",
                    "样本外是时间划分；若用样本外数据选择股票或规则则不再独立",
                    "收益为独立交易统计，不是资金曲线或组合收益；同一交易不能跨越划分边界"],
                  "study_plan": plan, "corporate_actions_sha256": actions_sha256,
                  "return_basis": "模拟价格收益；不单独计入现金分红，不能视为总收益；复权口径仍须核验",
                  "corporate_actions": "按用户提供文件排除跨事件窗口，数据完整性未独立核实" if actions else "未提供公司行为文件，无法排除跨事件窗口",
                  "snapshot_used": bool(snapshot), "snapshot_sha256": snapshot_sha256,
                  "stocks": []}
        for code in codes:
            if snapshot:
                item = snapstocks[code]
                rows = [row for row in item['rows'] if row['date'] <= args.end]
                retrieved = item['retrieved_at']
                provenance = item.get('provenance')
            else:
                fetched = fetch_kline(args.end, code)
                provenance = getattr(fetched, 'provenance', None)
                rows = backtest.completed_kline(fetched, now)
                retrieved = dt.datetime.now(ZoneInfo('Asia/Hong_Kong')).isoformat()
            selected_fields = {window['selected_field'] for window in provenance or [] if window.get('selected_field')}
            if len(selected_fields) > 1:
                raise ValueError(f'{code}: 日K返回字段混用，复权口径未知，拒绝研究')
            if actions and rows and actions['stocks'][code]['coverage_start'] > rows[0]['date']:
                raise ValueError(f'{code}: 公司行为文件未覆盖样本内起始日期')
            action_dates = [e['date'] for e in actions['stocks'][code]['events']] if actions else []
            stock = run_stock(code, rows, args.split, args.cost_bps, args.chip_width, action_dates)
            stock['data_source'] = {'name': '腾讯行情日K', 'retrieved_at': retrieved,
                                    'historical_publication_time': None, 'adjustment': '请求 qfq；实际口径尚需核验',
                                    'provenance': provenance}
            result['stocks'].append(stock)
        with open(args.output, 'x', encoding='utf-8') as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, allow_nan=False)
            fh.write('\n')
        print(f"已保存研究结果：{args.output}")
        return 0
    except (ValueError, OSError, KeyError, IndexError) as exc:
        print(f'研究失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
