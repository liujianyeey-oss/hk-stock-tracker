#!/usr/bin/env python3
"""Audit and evaluate recorded observations, never reconstruct missing signals."""
import argparse
import datetime as dt
import json
import math
import os
import sys

if __package__:
    from . import hk_calendar
    from . import backtest
    from .tracker import fill_missing_turnover
    from . import research
    from . import research_data
else:
    import hk_calendar
    import backtest
    from tracker import fill_missing_turnover
    import research
    import research_data


def sessions(start, end):
    out = []
    day = start
    while day <= end:
        if hk_calendar.session_close(day) is not None:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def review(records):
    if not records:
        raise ValueError('没有观察记录')
    by_date = {}
    first = records[0]
    plan, digest = first['plan'], first['plan_sha256']
    if plan.get('code_sha256') != research_data.code_hashes():
        raise ValueError('当前代码与观察计划版本不一致，请使用计划对应的源码归档复盘')
    for r in records:
        day = dt.date.fromisoformat(r['date'])
        if (r.get('format') != 'hk-tracker-observation-v1' or r['plan_sha256'] != digest
                or r['plan'] != plan or r['code_sha256'] != plan['code_sha256']):
            raise ValueError('观察记录的计划或代码指纹不一致')
        if not plan['split'] <= r['date'] <= plan['end'] or r['date'] in by_date:
            raise ValueError('观察日期重复或超出计划范围')
        if hk_calendar.session_close(day) is None:
            raise ValueError('观察日期不是已配置交易日')
        stocks = {s['code']: s for s in r['stocks']}
        if len(stocks) != len(r['stocks']) or set(stocks) != set(plan['codes']):
            raise ValueError('观察股票列表与计划不一致')
        for s in stocks.values():
            if not s['rows'] or s['rows'][-1]['date'] != r['date']:
                raise ValueError('观察记录缺少当天行情')
        by_date[r['date']] = stocks
    dates = sessions(dt.date.fromisoformat(plan['split']), dt.date.fromisoformat(max(by_date)))
    missing = [d for d in dates if d not in by_date]
    cost = plan['cost_bps_per_side'] / 10000
    if not math.isfinite(cost) or not 0 <= cost < 1:
        raise ValueError('计划成本无效')
    differences = []
    for code in plan['codes']:
        latest = [dict(row) for row in by_date[max(by_date)][code]['rows']]
        estimated = fill_missing_turnover(latest)
        profits, peaks = backtest.rolling_chip_metrics(latest, plan.get('chip_width', 0.5))
        flags = backtest.signal_flags(latest, profits, peaks, {}, {}, estimated)
        indices = {row['date']: i for i, row in enumerate(latest)}
        for day in sorted(by_date):
            if day not in indices:
                continue
            idx = indices[day]
            for signal in research.SIGNALS:
                recorded = by_date[day][code]['signals'][signal]['active']
                if recorded != flags[signal][idx]:
                    differences.append({'date': day, 'code': code, 'signal': signal,
                                        'recorded': recorded, 'recomputed': flags[signal][idx]})
    outcomes = []
    for index, day in enumerate(dates):
        if day not in by_date:
            continue
        for code in plan['codes']:
            s = by_date[day][code]
            for signal in research.SIGNALS:
                if not s['signals'][signal]['first_hit']:
                    continue
                for horizon in research.HORIZONS:
                    end_index = index + 1 + horizon
                    entry_index = index + 1
                    result = {'date': day, 'code': code, 'signal': signal, 'horizon': horizon}
                    if end_index >= len(dates):
                        result['status'] = 'pending'
                    elif any(d not in by_date for d in dates[index + 1:end_index + 1]):
                        result['status'] = 'missing_observation'
                    else:
                        entry_day = dates[entry_index]
                        exit_day = dates[end_index]
                        original_entry = by_date[entry_day][code]['rows'][-1]['open']
                        exit_rows = {r['date']: r for r in by_date[exit_day][code]['rows']}
                        if entry_day not in exit_rows:
                            result['status'] = 'missing_price_basis'
                        else:
                            entry = exit_rows[entry_day]['open']
                            exit_ = exit_rows[exit_day]['open']
                            if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (original_entry, entry, exit_)):
                                raise ValueError('开盘价无效')
                            if not math.isclose(original_entry, entry, rel_tol=1e-9, abs_tol=1e-8):
                                result.update(status='price_revision', entry_date=entry_day, exit_date=exit_day,
                                              original_entry=original_entry, revised_entry=entry)
                            else:
                                result.update(status='complete', entry_date=entry_day, exit_date=exit_day,
                                              net_return=exit_ * (1-cost) / (entry * (1+cost)) - 1)
                    outcomes.append(result)
    return {'format': 'hk-tracker-observation-review-v1', 'plan_sha256': digest,
            'observed_dates': sorted(by_date), 'missing_observation_dates': missing,
            'outcomes': outcomes, 'signal_differences_against_latest_snapshot': differences, 'notice': '首次触发取自每日原始观察记录；收益是模拟，不是实际成交。'
            '缺失日期不补造；静态日历不覆盖临时停市和个股停牌。观察文件可能被修改，哈希不证明时间。'
            '信号可以重叠，结果不是组合收益；入场价发生修订时不计算收益；相同价格也无法证明不存在公司行为。'}


def markdown_report(result):
    lines = ['# 前瞻观察复盘', '', f"计划指纹：`{result['plan_sha256']}`", '',
             f"观察天数：{len(result['observed_dates'])}；缺失预期交易日：{len(result['missing_observation_dates'])}。", '',
             '## 交易结果', '', '| 标的 | 信号 | 持有期 | 已完成 | 平均净收益 | 待到期 | 数据缺失或价格修订 |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    groups = {}
    for item in result['outcomes']:
        groups.setdefault((item['code'], item['signal'], item['horizon']), []).append(item)
    for (code, signal, horizon), items in sorted(groups.items()):
        completed = [item['net_return'] for item in items if item['status'] == 'complete']
        avg = f'{sum(completed)/len(completed)*100:+.2f}%' if completed else '无'
        pending = sum(item['status'] == 'pending' for item in items)
        bad = len(items) - len(completed) - pending
        lines.append(f'| {code} | {signal} | {horizon} | {len(completed)} | {avg} | {pending} | {bad} |')
    if not groups:
        lines.append('| 无 | 无 | — | 0 | 无 | 0 | 0 |')
    lines += ['', '## 数据检查', '',
              '缺失日期：' + ('、'.join(result['missing_observation_dates']) or '无'),
              f"重算信号与记录不一致：{len(result['signal_differences_against_latest_snapshot'])} 项。", '',
              result['notice'], '']
    return '\n'.join(lines)


def save_reports(output, result, markdown=None):
    """Create both outputs exclusively; roll back files we created on ordinary errors."""
    if markdown and os.path.realpath(output) == os.path.realpath(markdown):
        raise ValueError('JSON 和 Markdown 必须使用不同路径')
    data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    files = [(output, data)]
    if markdown:
        files.append((markdown, markdown_report(result)))
    created = []
    try:
        for path, content in files:
            with open(path, 'x', encoding='utf-8') as fh:
                st = os.fstat(fh.fileno())
                created.append((path, st.st_dev, st.st_ino))
                fh.write(content)
    except (OSError, ValueError):
        for path, device, inode in reversed(created):
            try:
                st = os.lstat(path)
                if (st.st_dev, st.st_ino) == (device, inode):
                    os.unlink(path)
            except FileNotFoundError:
                pass
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description='检查并复盘逐日观察记录')
    p.add_argument('records', nargs='+', help='观察 JSON 文件')
    p.add_argument('--output', required=True)
    p.add_argument('--markdown', help='另存 Markdown 汇总，不覆盖')
    args = p.parse_args(argv)
    try:
        records = []
        for name in args.records:
            with open(name, encoding='utf-8') as fh:
                records.append(json.load(fh))
        result = review(records)
        save_reports(args.output, result, args.markdown)
        print(f'已保存复盘：{args.output}')
        return 0
    except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
        print(f'复盘失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
