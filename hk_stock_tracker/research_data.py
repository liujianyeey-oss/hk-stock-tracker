#!/usr/bin/env python3
"""Capture normalized market data and predeclare a study; no credentials required."""
import argparse
import datetime as dt
import hashlib
import json
import math
import io
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import backtest
    from .tracker import fetch_kline, normalize_hk_code, validate_kline
else:
    import backtest
    from tracker import fetch_kline, normalize_hk_code, validate_kline

ROOT = Path(__file__).resolve().parent
HK = ZoneInfo('Asia/Hong_Kong')


def code_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('research.py', 'research_data.py', 'backtest.py', 'tracker.py', 'hk_calendar.py', 'observe.py', 'replay.py')}


def save_new(path, data):
    # Serialize before opening so encoding errors cannot leave a truncated file.
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    with open(path, 'x', encoding='utf-8') as out:
        out.write(content)



def compare_snapshots(old, new):
    """Compare overlapping normalized rows; differences are not automatically attributed."""
    for snapshot in (old, new):
        if snapshot.get('format') != 'hk-tracker-normalized-kline-v1':
            raise ValueError('快照格式无效')
    old_stocks = {s['code']: s for s in old['stocks']}
    new_stocks = {s['code']: s for s in new['stocks']}
    if len(old_stocks) != len(old['stocks']) or len(new_stocks) != len(new['stocks']):
        raise ValueError('快照股票代码重复')
    comparisons = []
    for code in sorted(set(old_stocks) | set(new_stocks)):
        if code not in old_stocks or code not in new_stocks:
            comparisons.append({'code': code, 'status': 'stock_missing_in_one_snapshot'})
            continue
        before, after = old_stocks[code], new_stocks[code]
        validate_kline(before['rows'])
        validate_kline(after['rows'])
        left = {r['date']: r for r in before['rows']}
        right = {r['date']: r for r in after['rows']}
        start = max(min(left), min(right))
        end = min(max(left), max(right))
        shared = sorted(set(left) & set(right))
        fields = ('open', 'high', 'low', 'close', 'vol', 't')
        changes = [{'date': day, 'fields': {k: {'old': left[day].get(k), 'new': right[day].get(k)}
                    for k in fields if left[day].get(k) != right[day].get(k)}}
                   for day in shared if any(left[day].get(k) != right[day].get(k) for k in fields)]
        comparisons.append({'code': code, 'status': 'compared' if shared else 'no_overlap',
            'old_retrieved_at': before.get('retrieved_at'), 'new_retrieved_at': after.get('retrieved_at'),
            'old_provenance': before.get('provenance'), 'new_provenance': after.get('provenance'),
            'shared_dates': len(shared), 'changed_dates': len(changes), 'changes': changes,
            'missing_in_old_overlap': sorted(d for d in set(right)-set(left) if start <= d <= end),
            'missing_in_new_overlap': sorted(d for d in set(left)-set(right) if start <= d <= end)})
    return {'format': 'hk-tracker-snapshot-comparison-v1', 'comparisons': comparisons,
            'notice': '差异可能来自复权、修订或口径变化，不能自动判断原因；一致也不能证明历史时点可得。'}

def main(argv=None):
    parser = argparse.ArgumentParser(description='捕获行情快照或提前声明研究计划')
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture', help='保存解析后的日K快照')
    capture.add_argument('--codes', nargs='+', required=True)
    capture.add_argument('--end', required=True)
    capture.add_argument('--output', required=True)
    capture.add_argument('--previous', help='上一份快照；同时记录重叠行情差异')
    plan = sub.add_parser('plan', help='创建未来样本外研究计划')
    plan.add_argument('--codes', nargs='+', required=True)
    plan.add_argument('--split', required=True)
    plan.add_argument('--end', required=True)
    plan.add_argument('--cost-bps', type=float, required=True)
    plan.add_argument('--chip-width', type=float, default=0.5)
    plan.add_argument('--bundle', help='可选：保存计划依赖源码的 ZIP，不覆盖')
    plan.add_argument('--output', required=True)
    check = sub.add_parser('check-plan', help='显示计划内容及代码文件变化')
    check.add_argument('--plan', required=True)
    compare = sub.add_parser('compare', help='对比两份行情快照的历史数据修订')
    compare.add_argument('--old', required=True)
    compare.add_argument('--new', required=True)
    compare.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        now = dt.datetime.now(HK)
        if args.command == 'compare':
            with open(args.old, 'rb') as fh:
                old_content = fh.read()
            with open(args.new, 'rb') as fh:
                new_content = fh.read()
            result = compare_snapshots(json.loads(old_content), json.loads(new_content))
            result['old_sha256'] = hashlib.sha256(old_content).hexdigest()
            result['new_sha256'] = hashlib.sha256(new_content).hexdigest()
            save_new(args.output, result)
            print(f'已保存快照对比：{args.output}')
            return 0
        if args.command == 'check-plan':
            with open(args.plan, 'rb') as fh:
                content = fh.read()
            saved = json.loads(content)
            if saved.get('format') != 'hk-tracker-study-plan-v1':
                raise ValueError('计划格式无效')
            current = code_hashes()
            old = saved['code_sha256']
            changed = sorted(name for name in set(old) | set(current) if old.get(name) != current.get(name))
            print(json.dumps({'plan_sha256': hashlib.sha256(content).hexdigest(), 'plan': saved,
                              'changed_files': changed, 'matches_current_code': not changed,
                              'notice': '文件指纹不能证明创建时间或未被修改；需要外部存证。'}, ensure_ascii=False, indent=2))
            return 1 if changed else 0
        codes = [normalize_hk_code(c) for c in args.codes]
        if len(set(codes)) != len(codes):
            raise ValueError('股票代码不能重复')
        end = dt.date.fromisoformat(args.end)
        if args.command == 'capture':
            if end > now.date():
                raise ValueError('截止日期不能晚于今天')
            stocks = []
            for code in codes:
                fetched = fetch_kline(args.end, code)
                provenance = getattr(fetched, 'provenance', None)
                rows = backtest.completed_kline(fetched, now)
                validate_kline(rows)
                stocks.append({'code': code, 'retrieved_at': dt.datetime.now(HK).isoformat(),
                               'provenance': provenance, 'rows': rows})
            data = {'format': 'hk-tracker-normalized-kline-v1', 'requested_end': args.end,
                    'source': '腾讯行情：解析后的日K，可能前复权', 'historical_publication_time': None,
                    'notice': '非原始响应；可能已修订或前复权；抓取时间不能证明历史可用时间', 'stocks': stocks}
            if args.previous:
                with open(args.previous, 'rb') as fh:
                    previous = fh.read()
                data['previous_snapshot_sha256'] = hashlib.sha256(previous).hexdigest()
                data['revision_check'] = compare_snapshots(json.loads(previous), data)
        else:
            split = dt.date.fromisoformat(args.split)
            if split <= now.date() or end <= split:
                raise ValueError('样本外起始日期须晚于今天，截止日期须晚于起始日期')
            if not math.isfinite(args.cost_bps) or not 0 <= args.cost_bps < 10000:
                raise ValueError('成本必须为 0（含）到 10000（不含）基点')
            if not math.isfinite(args.chip_width) or args.chip_width <= 0:
                raise ValueError('分箱宽度必须为正有限数')
            data = {'format': 'hk-tracker-study-plan-v1', 'created_at': now.isoformat(),
                    'codes': codes, 'split': args.split, 'end': args.end,
                    'cost_bps_per_side': args.cost_bps, 'chip_width': args.chip_width, 'code_sha256': code_hashes(),
                    'notice': '本地文件和时间戳可以修改；请在样本外开始前使用可信外部时间戳或公开存证。'
                              '仅固定计划并不能证明行情为历史时点数据，也不能证明真实执行。'}
            if args.bundle:
                if Path(args.bundle).resolve() == Path(args.output).resolve():
                    raise ValueError('计划文件与源码归档必须使用不同路径')
                if Path(args.output).exists():
                    raise ValueError('计划文件已存在，不覆盖')
                content = io.BytesIO()
                with zipfile.ZipFile(content, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for filename in data['code_sha256']:
                        source = (ROOT / filename).read_bytes()
                        if hashlib.sha256(source).hexdigest() != data['code_sha256'][filename]:
                            raise ValueError('打包过程中源码发生变化，请重试')
                        archive.writestr(filename, source)
                bundle = content.getvalue()
                with open(args.bundle, 'xb') as out:
                    out.write(bundle)
                data['source_bundle_sha256'] = hashlib.sha256(bundle).hexdigest()
        save_new(args.output, data)
        print(f'已保存：{args.output}')
        return 0
    except (ValueError, OSError, KeyError, IndexError) as exc:
        print(f'操作失败：{exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
