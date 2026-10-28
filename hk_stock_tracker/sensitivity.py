#!/usr/bin/env python3
"""Compare chip-model assumptions on an explicitly captured snapshot."""
import argparse
import json
import math
import sys

if __package__:
    from . import backtest
    from . import research_data
    from .tracker import fill_missing_turnover, normalize_hk_code, validate_kline
else:
    import backtest
    import research_data
    from tracker import fill_missing_turnover, normalize_hk_code, validate_kline


def compare(rows, widths, windows):
    validate_kline(rows, minimum=21)
    result = []
    for window in windows:
        subset = [dict(r) for r in rows[-window:]]
        if len(subset) < 21:
            raise ValueError('窗口不足 21 条行情')
        estimated = fill_missing_turnover(subset)
        for width in widths:
            profits, peaks = backtest.rolling_chip_metrics(subset, width)
            result.append({'window_requested': window, 'window_actual': len(subset),
                           'width': width, 'profit_percent': profits[-1], 'peak': peaks[-1],
                           'estimated_turnover_days': len(estimated)})
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description='筹码分箱和历史窗口敏感性，描述模型差异')
    p.add_argument('--snapshot', required=True)
    p.add_argument('--code', required=True)
    p.add_argument('--widths', nargs='+', type=float, required=True)
    p.add_argument('--windows', nargs='+', type=int, required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args(argv)
    try:
        if any(not math.isfinite(w) or w <= 0 for w in a.widths) or any(w < 21 for w in a.windows):
            raise ValueError('宽度必须为正有限数，窗口至少21条')
        with open(a.snapshot, encoding='utf-8') as f:
            snapshot = json.load(f)
        if snapshot.get('format') != 'hk-tracker-normalized-kline-v1':
            raise ValueError('快照格式无效')
        code = normalize_hk_code(a.code)
        matches = [s for s in snapshot['stocks'] if s['code'] == code]
        if len(matches) != 1:
            raise ValueError('快照标的不存在或重复')
        results = compare(matches[0]['rows'], a.widths, a.windows)
        research_data.save_new(a.output, {'code': code, 'snapshot': a.snapshot, 'results': results,
            'notice': '描述模型假设敏感性；价格网格采用研究模型，不代表真实持仓成本或参数最优性。'})
        print(f'已保存敏感性结果：{a.output}')
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f'敏感性分析失败：{e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
