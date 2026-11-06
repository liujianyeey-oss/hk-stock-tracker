#!/usr/bin/env python3
"""个股深度回测扩展: A 全历史 B S2c两日确认 C 含成本 D 事件研究。
复用 tracker.py/backtest.py 引擎。纯标准库。
用法: python3 -m hk_stock_tracker.backtest_deep [--code 00700]"""
import argparse
import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

if __package__:
    from . import hk_calendar, tracker
    from .backtest import (completed_kline, fetch_southbound_all,
                           rolling_chip_metrics, signal_flags, stats)
    from .tracker import (choose_bin_width, fetch_short_selling, fill_missing_turnover,
                          validate_kline, DEFAULT_CODE, normalize_hk_code)
else:
    import hk_calendar
    import tracker
    from backtest import (completed_kline, fetch_southbound_all,
                          rolling_chip_metrics, signal_flags, stats)
    from tracker import (choose_bin_width, fetch_short_selling, fill_missing_turnover,
                         validate_kline, DEFAULT_CODE, normalize_hk_code)

HORIZONS = (5, 10, 20)
TODAY = "2026-11-06"
SUB_START = "2023-01-01"
FOMC = ["2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26",
        "2023-09-20", "2023-11-01", "2023-12-13", "2024-01-31", "2024-03-20",
        "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07",
        "2024-12-18", "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
        "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
        "2026-09-16", "2026-10-28", "2026-12-09"]  # 决议日, 2025-2026 为近似


def first_hits(flags):
    return [i for i in range(len(flags)) if flags[i] and (i == 0 or not flags[i - 1])]


def eval_signals(data, ratio_map, south_map, width=0.5):
    est = fill_missing_turnover(data)
    profits, peaks = rolling_chip_metrics(data, width)
    sig = signal_flags(data, profits, peaks, ratio_map, south_map, est)
    cl = [d["close"] for d in data]
    n = len(data)

    def fwd(i, h, base=0):
        j = i + base
        return cl[j + h] / cl[j] - 1 if j + h < n else None

    bench = {h: stats([fwd(i, h) for i in range(n) if fwd(i, h) is not None])
             for h in HORIZONS}

    # S2c: 两日确认——突破日T, T+1收盘仍站上T日峰价, 信号日从T+1起算
    s2_hits = first_hits(sig["S2"])
    s2c_hits = [i + 1 for i in s2_hits if i + 1 < n and cl[i + 1] > peaks[i]]
    out = {"bench": bench, "cl": cl}
    for k in ("S1", "S2", "S3u", "S3d", "S4", "S5a", "S5b"):
        hits = first_hits(sig[k])
        out[k] = {h: stats([fwd(i, h) for i in hits if fwd(i, h) is not None])
                  for h in HORIZONS}
        out[k]["n"] = len(hits)
    out["S2c"] = {h: stats([fwd(i - 1, h, base=1) for i in s2c_hits
                            if fwd(i - 1, h, base=1) is not None]) for h in HORIZONS}
    out["S2c"]["n"] = len(s2c_hits)
    return out


def d_ok(data, i):
    return True


def fmt(st, bench=None, net=False):
    if not st:
        return "无样本"
    cost = 0.3 if net else 0.0
    s = f"{st['avg'] - cost:+.2f}/{st['med'] - cost:+.2f}/{st['win']:.0f}({st['n']})"
    if bench:
        edge = st["avg"] - cost - bench["avg"]
        s += f" 边际{edge:+.2f}"
    return s


def main(code=DEFAULT_CODE):
    now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    adjust = "qfq"  # hfq 后复权在港股上收益率序列断裂(已实测弃用), 统一 qfq
    tracker.KLINE_START = "2018-09-20" if code == DEFAULT_CODE else "2016-01-01"
    print(f"拉取全历史日K(hk{code}, {tracker.KLINE_START}起, {adjust})...")
    data_full = tracker.fetch_kline(TODAY, code, adjust)
    dropped = sum(1 for d in data_full if min(d["open"], d["close"], d["high"], d["low"]) <= 0)
    data_full = [d for d in data_full if min(d["open"], d["close"], d["high"], d["low"]) > 0]
    if dropped:
        print(f"剔除复权非正价格行 {dropped} 条")
    if code != DEFAULT_CODE:
        print("注：高分红标的 qfq 早段为负价/失真，全历史自2016起截取；"
              "2016-2020 段收益率较名义口径放大约1.3-1.6倍(逐年收敛至1)，方向有效、幅度偏高。")
    data_full = completed_kline(data_full, now)
    validate_kline(data_full, minimum=60)
    data_sub = [dict(d) for d in data_full if d["date"] >= SUB_START]
    print(f"全历史 {len(data_full)} 日 / 子窗口 {len(data_sub)} 日")

    try:
        shorts = fetch_short_selling(code)
    except Exception:
        shorts = []
    ratio_map = {x["date"]: x["ratio"] for x in shorts}
    try:
        south_map = fetch_southbound_all(code)
    except Exception:
        south_map = {}

    est_full = [d["date"] for d in data_full if not d["t"] or d["t"] <= 0]
    print(f"换手率缺失待估算: 全历史 {len(est_full)} 日(集中在2018-2022)")
    width = 0.5 if code == DEFAULT_CODE else choose_bin_width(data_full[-1]["close"])
    print(f"分箱宽度 {width:g}; 滚动筹码重算: 全历史...")
    res_full = eval_signals([dict(d) for d in data_full], ratio_map, south_map, width)
    print("滚动筹码重算: 2023起子窗口...")
    res_sub = eval_signals(data_sub, ratio_map, south_map, width)

    L = []
    A = L.append
    A(f"# hk{code} 深度回测扩展 2026-11-06 (分箱{width:g})")
    A("")
    A("纪律：全部阈值既定未回调；S2c 来自 2026-10-23 已声明假设而非结果驱动；"
      "收益自信号日收盘起算；连续触发按首次去重。")
    A(f"数据质量：复权口径 {adjust}" + ("（美团无分红，qfq≈名义）" if code == DEFAULT_CODE else
      "（qfq 前复权，锚定最新价；高分红股 2016 前为负价已剔除，2016-2020 收益率较名义口径"
      "放大 1.3-1.6 倍逐年收敛——方向有效、幅度偏高；hfq 后复权经实测收益率断裂不可用）") +
      "；腾讯早期换手率字段大面积为0，已用纯历史滚动均值估算"
      "（估算日不触发 S2/S3 放量类信号）；南向仅 2024-10 起(472日)，S5 在全历史"
      "模式下仍为该子样本；沽空 ETNet 仅约40日。")
    A("")
    A("## A. 全历史(2018-09-20起) vs 940日窗口(2023-01起)")
    A("")
    A("信号 | 窗口 | 触发 | +20日均/中/胜(n) | +20边际pp | +5边际 | +10边际")
    A("--- | --- | --- | --- | --- | --- | ---")
    names = {"S1": "S1 极端套牢<10%", "S2": "S2 放量破峰", "S3u": "S3 放量涨",
             "S3d": "S3 放量跌", "S4": "S4 沽空>25%", "S5a": "S5a 南向增持抄底",
             "S5b": "S5b 南向连减"}

    def arow(label, res, key):
        st, b = res[key], res["bench"]
        if not st[20]:
            return f"{label} | {st['n']}⚠ | 无样本 | - | - | -"
        e20 = st[20]["avg"] - b[20]["avg"]
        e5 = st[5]["avg"] - b[5]["avg"] if st[5] else 0
        e10 = st[10]["avg"] - b[10]["avg"] if st[10] else 0
        thin = "⚠" if st["n"] < 10 else ""
        return (f"{label} | {st['n']}{thin} | {fmt(st[20])} | {e20:+.2f} | "
                f"{e5:+.2f} | {e10:+.2f}")

    A(f"基准 | 全历史 | - | {fmt(res_full['bench'][20])} | - | - | -")
    A(f"基准 | 940日 | - | {fmt(res_sub['bench'][20])} | - | - | -")
    for k in ("S1", "S2", "S3u", "S3d", "S4", "S5a", "S5b"):
        A(f"{names[k]} | 全历史 {arow('', res_full, k)}")
        A(f"{names[k]} | 940日 {arow('', res_sub, k)}")
    A("")
    A("## B. 两日确认规则 S2c vs S2(原单日)")
    A("")
    A("规则 | 窗口 | 触发 | +5日均/中/胜 | +10日 | +20日 | +20边际pp")
    A("--- | --- | --- | --- | --- | --- | ---")
    for label, res in (("全历史", res_full), ("940日", res_sub)):
        for key, rn in (("S2", "S2单日"), ("S2c", "S2c两日确认")):
            st = res[key]
            e20 = f"{st[20]['avg'] - res['bench'][20]['avg']:+.2f}" if st[20] else "-"
            A(f"{rn} | {label} | {st['n']}{'⚠' if st['n'] < 10 else ''} | "
              f"{fmt(st[5])} | {fmt(st[10])} | {fmt(st[20])} | {e20}")
    A("")
    A("注：S2c 信号日=T+1(确认日)收盘，收益从T+1起算；确认为T+1收盘仍高于T日峰价。")
    A("")
    A("## C. 含成本净收益(双边0.3%, 全历史, +20日)")
    A("")
    A("信号 | 触发 | 毛边际pp | 净边际pp(毛-0.3) | 胜率")
    A("--- | --- | --- | --- | ---")
    for k in ("S1", "S2", "S2c", "S3u", "S3d", "S4", "S5a", "S5b"):
        st = res_full[k]
        if not st[20]:
            A(f"{names.get(k, 'S2c两日确认')} | {st['n']}⚠ | - | 样本不足 | -")
            continue
        gross = st[20]["avg"] - res_full["bench"][20]["avg"]
        A(f"{names.get(k, 'S2c两日确认')} | {st['n']}{'⚠' if st['n'] < 10 else ''} | "
          f"{gross:+.2f} | {gross - 0.3:+.2f} | {st[20]['win']:.0f}%")
    A("")
    A("## D. 宏观事件研究(2023-01起)")
    ev_dates = set(FOMC)
    d = datetime.date(2023, 1, 1)
    while d <= datetime.date(2026, 11, 30):
        f = d.replace(day=1)
        while f.weekday() != 4:
            f += datetime.timedelta(days=1)
        ev_dates.add(f.isoformat())
        d = (d.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
    dt = [x["date"] for x in data_full]
    cl = [x["close"] for x in data_full]
    idx = {d_: i for i, d_ in enumerate(dt)}
    n1, ev1 = [], set()
    for e in sorted(ev_dates):
        nxt = next((i for i, d_ in enumerate(dt) if d_ > e and d_ >= "2023-01-01"), None)
        if nxt and nxt + 1 < len(dt):
            ev1.add(nxt)
            ret = cl[nxt] / cl[nxt - 1] - 1
            amp = (data_full[nxt]["high"] - data_full[nxt]["low"]) / cl[nxt - 1]
            n1.append((ret, abs(ret), amp))
    oth = [(cl[i] / cl[i - 1] - 1, abs(cl[i] / cl[i - 1] - 1),
            (data_full[i]["high"] - data_full[i]["low"]) / cl[i - 1])
           for i in range(1, len(dt)) if dt[i] >= "2023-01-01" and i not in ev1]
    me = [sum(x[j] for x in n1) / len(n1) for j in range(3)]
    mo = [sum(x[j] for x in oth) / len(oth) for j in range(3)]
    A(f"- 事件次日(T+1)样本 {len(n1)} 次(非农首周五+FOMC决议日, 2023-01起); 对照非事件日 {len(oth)} 日")
    A(f"- 事件次日: 平均收益 {me[0] * 100:+.2f}%  平均|收益| {me[1] * 100:.2f}%  平均振幅 {me[2] * 100:.2f}%")
    A(f"- 非事件日: 平均收益 {mo[0] * 100:+.2f}%  平均|收益| {mo[1] * 100:.2f}%  平均振幅 {mo[2] * 100:.2f}%")
    A(f"- **波动倍数: |收益| {me[1] / mo[1]:.2f}x, 振幅 {me[2] / mo[2]:.2f}x**")
    A("")
    A("## 稳健性附表(全历史, +20日边际/胜率; 只展示不挑最优)")
    A("")
    A("参数 | 边际pp | 胜率 | 触发")
    A("--- | --- | --- | ---")
    tt = [x["t"] for x in data_full]
    cl2 = cl
    profits, _ = rolling_chip_metrics(data_full, width)
    for thp in (5, 10, 15):
        hits = first_hits([p is not None and p < thp for p in profits])
        rets = [cl2[i + 20] / cl2[i] - 1 for i in hits if i + 20 < len(cl2)]
        st = stats(rets)
        if st:
            A(f"S1 获利盘<{thp}% | {st['avg'] - res_full['bench'][20]['avg']:+.2f} | "
              f"{st['win']:.0f}% | {len(hits)}")
    for mult in (1.5, 2.0, 2.5):
        flags = [False] * len(cl2)
        for i in range(20, len(cl2)):
            m20 = sum(tt[i - 20:i]) / 20
            if tt[i] > mult * m20:
                flags[i] = True
        hits = first_hits(flags)
        rets = [cl2[i + 20] / cl2[i] - 1 for i in hits if i + 20 < len(cl2)]
        st = stats(rets)
        if st:
            A(f"S3 换手>{mult}x20日均(不分方向) | {st['avg'] - res_full['bench'][20]['avg']:+.2f} | "
              f"{st['win']:.0f}% | {len(hits)}")
    A("")
    A("## 结论(见会话分析, 此处为占位)")
    text = "\n".join(L) + "\n"
    rname = f"backtest-meituan-deep-{TODAY}.md" if code == DEFAULT_CODE else f"backtest-deep-{code}-{TODAY}.md"
    rpath = os.path.join(tracker.BASE, "reports", rname)
    with open(rpath, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"--- 报告: {rpath}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="个股深度回测扩展")
    parser.add_argument("--code", "-c", default=DEFAULT_CODE, help="港股代码，默认 03690")
    args = parser.parse_args()
    try:
        sys.exit(main(normalize_hk_code(args.code)))
    except (ValueError, OSError, KeyError, IndexError) as exc:
        print(f"回测失败：{exc}", file=sys.stderr)
        sys.exit(1)
