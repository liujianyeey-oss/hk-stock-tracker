#!/usr/bin/env python3
"""Record current-day fixed-rule signals and the data actually fetched."""
import argparse
import datetime as dt
import hashlib
import json
import sys

if __package__:
    from . import backtest
    from . import hk_calendar
    from . import research
    from . import research_data
    from .tracker import fetch_kline, fill_missing_turnover, normalize_hk_code, validate_kline
else:
    import backtest
    import hk_calendar
    import research
    import research_data
    from tracker import fetch_kline, fill_missing_turnover, normalize_hk_code, validate_kline


def collect(plan, plan_hash, now, fetch=fetch_kline, clock=None):
    if clock is None:
        clock = lambda: dt.datetime.now(research_data.HK)
    if plan.get('format') != 'hk-tracker-study-plan-v1' or plan.get('code_sha256') != research_data.code_hashes():
        raise ValueError('计划格式无效或研究代码已变化')
    created = dt.datetime.fromisoformat(plan['created_at'])
    split = dt.date.fromisoformat(plan['split'])
    end = dt.date.fromisoformat(plan['end'])
    if created.tzinfo is None or created.astimezone(research_data.HK).date() >= split:
        raise ValueError('计划须在样本外起始日期之前创建')
    if not split <= now.date() <= end:
        raise ValueError('今天不在计划样本外观察期内')
    if not hk_calendar.is_finished(now.date(), now):
        raise ValueError('今天尚未确认收市或不是已配置的交易日')
    codes = [normalize_hk_code(c) for c in plan['codes']]
    if not codes or len(codes) != len(set(codes)):
        raise ValueError('计划股票列表为空或重复')
    stocks = []
    for code in codes:
        rows = fetch(now.date().isoformat(), code)
        retrieved = clock()
        provenance = getattr(rows, 'provenance', None)
        selected = {p['selected_field'] for p in provenance or [] if p.get('selected_field')}
        if len(selected) > 1:
            raise ValueError(f'{code} 日K返回字段混用，复权口径未知，拒绝记录')
        if retrieved.tzinfo is None or retrieved.astimezone(research_data.HK).date() != now.date():
            raise ValueError('跨日抓取，无法作为同一交易日的观察记录')
        validate_kline(rows, minimum=21)
        if rows[-1]['date'] != now.date().isoformat():
            raise ValueError(f'{code} 缺少当天日K，无法记录；可能停牌或数据延迟')
        if any(row['date'] > now.date().isoformat() for row in rows):
            raise ValueError(f'{code} 行情包含未来日期')
        calculated = [dict(row) for row in rows]
        estimated = fill_missing_turnover(calculated)
        profits, peaks = backtest.rolling_chip_metrics(calculated, plan.get('chip_width', 0.5))
        flags = backtest.signal_flags(calculated, profits, peaks, {}, {}, estimated)
        stock_signals = {key: {'active': flags[key][-1],
                               'first_hit': flags[key][-1] and not flags[key][-2]}
                         for key in research.SIGNALS}
        recent = rows[-min(30, len(rows)):]
        observed = {r['date'] for r in recent}
        cursor = dt.date.fromisoformat(recent[0]['date'])
        missing = []
        while cursor <= now.date():
            if cursor.year not in hk_calendar.HOLIDAYS:
                missing = None
                break
            if hk_calendar.session_close(cursor) is not None and cursor.isoformat() not in observed:
                missing.append(cursor.isoformat())
            cursor += dt.timedelta(days=1)
        quality = {'last_kline_date': rows[-1]['date'], 'recent_window_rows': len(recent),
                   'recent_estimated_turnover_count': sum(r['date'] in estimated for r in recent),
                   'recent_missing_expected_sessions': missing,
                   'warning': '静态日历不含临时停市或个股停牌；缺失原因需要核验'}
        stocks.append({'code': code, 'retrieved_at': retrieved.isoformat(),
                       'data_source': '腾讯行情解析后的日K，可能前复权',
                       'historical_publication_time': None, 'provenance': provenance,
                       'estimated_turnover_dates': estimated, 'quality': quality,
                       'profit_percent': profits[-1], 'chip_peak': peaks[-1],
                       'signals': stock_signals, 'rows': rows})
    return {'format': 'hk-tracker-observation-v1', 'date': now.date().isoformat(),
            'started_at': now.isoformat(), 'plan_sha256': plan_hash, 'plan': plan,
            'code_sha256': research_data.code_hashes(), 'stocks': stocks,
            'notice': '仅记录本次抓取时的计算，非成交记录；本地文件与时间可修改，需外部存证。'
                      '数据可能延迟、修订或前复权，收市及停牌状态未覆盖临时安排。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='收市后保存计划内当天行情与信号，不执行交易')
    parser.add_argument('--plan', required=True)
    parser.add_argument('--output', required=True, help='新建 JSON 文件，不覆盖')
    args = parser.parse_args(argv)
    try:
        with open(args.plan, 'rb') as fh:
            content = fh.read()
        plan = json.loads(content)
        now = dt.datetime.now(research_data.HK)
        observation = collect(plan, hashlib.sha256(content).hexdigest(), now)
        research_data.save_new(args.output, observation)
        print(f'已保存当天观察记录：{args.output}')
        return 0
    except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
        print(f'观察失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
