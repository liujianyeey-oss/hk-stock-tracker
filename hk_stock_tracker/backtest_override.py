#!/usr/bin/env python3
"""盘中破位 override 规则回测（日线近似）。
规则：盘中带量跌破关键防线 → 不等收盘，以破位价减半仓；对照现行 T+1 次日开盘卖出。
防线口径：昨日收盘下方最近的筹码局部峰(换手衰减模型, 5bin平滑, 间距4bin)，
无下方峰时取前20日最低价。只用 T-1 及更早数据定防线，无前视。
局限：日线无法知道盘中确切破位时点与深度，破位价=防线价×(1-0.1%滑点)为近似；
美团 qfq 口径(无分红)；其余标的 qfq 2016 起(早段负价失真已剔除, 2016-2020 收益率
口径偏高约1.3-1.6x, 对"价差pp"类相对指标影响小)。
用法: python3 -m hk_stock_tracker.backtest_override [--codes 03690,00700,...]"""
import argparse
import datetime
import math
import os
import sys
from zoneinfo import ZoneInfo

if __package__:
    from . import tracker
    from .tracker import (fetch_kline, fill_missing_turnover, validate_kline,
                          DEFAULT_CODE, normalize_hk_code, choose_bin_width)
    from .backtest import stats
else:
    import tracker
    from tracker import (fetch_kline, fill_missing_turnover, validate_kline,
                         DEFAULT_CODE, normalize_hk_code, choose_bin_width)
    from backtest import stats

TODAY = "2026-11-09"
SLIP = 0.001          # 破位滑点 0.1%
VOL_X = 1.5           # 带量阈值: 当日量/20日均量
RANGE_POS = 0.3       # 无反抽阈值: (收-低)/(高-低)


def rolling_defense(data, width):
    """逐日递推筹码分布, 记录每日(收盘后)全部局部峰; defense[i] 用 peaks[i-1] 与 close[i-1]。"""
    W = width
    b0 = int(data[0]["low"] / W) * W
    chips = [0.0] * (int((data[0]["high"] - b0) / W) + 2)
    peaks_list = [None] * len(data)
    for i, d in enumerate(data):
        nb0 = int(d["low"] / W) * W
        if nb0 < b0:
            chips = [0.0] * round((b0 - nb0) / W) + chips
            b0 = nb0
        need = int((d["high"] - b0) / W) + 2
        if need > len(chips):
            chips.extend([0.0] * (need - len(chips)))
        nb = len(chips)
        centers = [b0 + (j + 0.5) * W for j in range(nb)]
        t = d["t"] / 100.0
        if i:
            chips = [c * (1.0 - t) for c in chips]
        L, H, P = d["low"], d["high"], (d["high"] + d["low"] + 2 * d["close"]) / 4.0
        w = [0.0] * nb
        if H <= L:
            w[min(max(int((L - b0) / W), 0), nb - 1)] = 1.0
        else:
            for j, x in enumerate(centers):
                if x < L or x > H:
                    continue
                if P <= L:
                    v = (H - x) / (H - L)
                elif P >= H:
                    v = (x - L) / (H - L)
                elif x <= P:
                    v = (x - L) / (P - L)
                else:
                    v = (H - x) / (H - P)
                w[j] = max(v, 0.0)
        s = sum(w)
        if s:
            add = t * 100.0
            chips = [c + add * wi / s for c, wi in zip(chips, w)]
        sm = [sum(chips[max(0, j - 2):j + 3]) for j in range(nb)]
        loc = [j for j in range(1, nb - 1) if sm[j] >= sm[j - 1] and sm[j] > sm[j + 1]]
        loc.sort(key=lambda j: -sm[j])
        picked = []
        for j in loc:
            if all(abs(j - k[0]) >= 4 for k in picked):
                share = sum(chips[max(0, j - 2):j + 3])
                picked.append((centers[j], share))
            if len(picked) >= 8:
                break
        peaks_list[i] = sorted(picked)
    return peaks_list


def stock_events(code, label):
    tracker.KLINE_START = "2018-09-20" if code == DEFAULT_CODE else "2018-01-01"
    data = fetch_kline(TODAY, code)
    data = [d for d in data if min(d["open"], d["close"], d["high"], d["low"]) > 0]
    validate_kline(data, minimum=60)
    fill_missing_turnover(data)
    width = 0.5 if code == DEFAULT_CODE else choose_bin_width(data[-1]["close"])
    peaks_list = rolling_defense(data, width)
    cl = [d["close"] for d in data]
    out = []
    last_hit = -99
    for i in range(21, len(data) - 1):
        if i - last_hit < 10:
            continue  # 冷却10个交易日, 避免连跌链式重复计
        prev_close = cl[i - 1]
        # 主要防线 = 昨收下方筹码份额最大的峰(主峰翻支撑口径), 且距昨收 2%-15%
        below = [(p, s) for p, s in (peaks_list[i - 1] or [])
                 if prev_close * 0.85 <= p <= prev_close * 0.98]
        if below:
            defense = max(below, key=lambda x: x[1])[0]
        else:
            defense = min(x["low"] for x in data[i - 20:i])
            if not prev_close * 0.85 <= defense <= prev_close * 0.98:
                continue
        d = data[i]
        if not (d["low"] < defense and d["close"] < defense and prev_close >= defense):
            continue
        last_hit = i
        volr = d["vol"] / (sum(x["vol"] for x in data[i - 20:i]) / 20)
        rng = d["high"] - d["low"]
        pos = (d["close"] - d["low"]) / rng if rng > 0 else 1.0
        a = defense * (1 - SLIP)
        b = data[i + 1]["open"]
        out.append({"code": code, "label": label, "date": d["date"], "defense": defense,
                    "low": d["low"], "close": d["close"], "a": a, "b": b,
                    "diff_pp": (a - b) / defense * 100, "volr": volr, "pos": pos})
    return out, len(data)


def fmt_g(name, rows):
    if not rows:
        return f"{name} | 0 | - | - | - | - | -"
    diffs = sorted(r["diff_pp"] for r in rows)
    n = len(diffs)
    med = diffs[n // 2] if n % 2 else (diffs[n // 2 - 1] + diffs[n // 2]) / 2
    win = sum(1 for x in diffs if x > 0) / n * 100
    return (f"{name} | {n} | {sum(diffs) / n:+.2f} | {med:+.2f} | {win:.0f}% | "
            f"{diffs[0]:+.2f} | {diffs[-1]:+.2f}")


def main(codes, output):
    names = {"03690": "美团", "00700": "腾讯", "00005": "汇丰", "00388": "港交所"}
    all_events, span_note = [], []
    for c in codes:
        ev, ndays = stock_events(c, names.get(c, c))
        all_events.extend(ev)
        span_note.append(f"{names.get(c, c)}(hk{c}) {ndays}交易日 {len(ev)}次")
        print(f"hk{c}: {ndays} 交易日, 破线事件 {len(ev)} 次")
    all_events.sort(key=lambda e: e["date"])
    mt = [e for e in all_events if e["code"] == DEFAULT_CODE]

    L = []
    A = L.append
    A("# 盘中破位 override 规则回测(日线近似) 2026-11-09")
    A("")
    A("规则假设：盘中带量跌破关键防线 → 不等收盘, 以破位价(防线×(1-0.1%滑点))减半仓;"
      "对照现行规则 T+1 次日开盘卖出。事件=当日 low<防线 且 close<防线 且昨收≥防线"
      "(排除纯假摔日), 同一标的冷却10个交易日防连跌链式重复计。")
    A("防线口径：昨收下方筹码份额最大的局部峰(主峰翻支撑), 且距昨收2%-15%; 无合格峰时"
      "取前20日低点(同样需在2%-15%带内, 否则当日不构防)。只用T-1及更早数据, 无前视。")
    A("核心指标=每笔价差 (A−B)/防线(pp)：正=override 更优。成本两边对称(均含印花税佣金), 不重复计。")
    A(f"样本：{'；'.join(span_note)}。美团 qfq(无分红,≈名义)；其余 qfq 自2018起"
      "(更早 qfq 近零价失真爆炸已剔除；2018-2020 段收益率口径仍偏高约1.3-1.8倍,"
      "方向与组间对比有效, 绝对幅度看美团列)。"
      "局限：日线近似不知盘中真实破位时点/深度；防线为动态代理口径, 与 playbook 静态点位存在偏差。")
    A("")
    A("## 事件明细")
    A("")
    A("标的 | 日期 | 防线 | 最低 | 收盘 | A破位价 | B次日开 | 价差pp | 量比 | 幅位")
    A("--- | --- | --- | --- | --- | --- | --- | --- | --- | ---")
    for e in all_events:
        A(f"{e['label']} | {e['date']} | {e['defense']:.2f} | {e['low']:.2f} | {e['close']:.2f} | "
          f"{e['a']:.2f} | {e['b']:.2f} | {e['diff_pp']:+.2f} | {e['volr']:.1f}x | {e['pos']:.2f}")
    A("")
    A("## 分组统计(价差pp: 均值/中位/胜率/最小/最大)")
    A("")
    A("分组 | n | 均值 | 中位 | 胜率 | 最小 | 最大")
    A("--- | --- | --- | --- | --- | --- | ---")
    A(fmt_g("全部(合并)", all_events))
    if mt:
        A(fmt_g("美团单独", mt))
    for vol_g, cond in (("带量≥1.5x", lambda e: e["volr"] >= VOL_X),
                        ("无量<1.5x", lambda e: e["volr"] < VOL_X)):
        rows = [e for e in all_events if cond(e)]
        A(fmt_g(vol_g, rows))
        for pos_g, cond2 in (("  且幅位<0.3(无反抽)", lambda e: e["pos"] < RANGE_POS),
                             ("  且幅位≥0.3(有反抽)", lambda e: e["pos"] >= RANGE_POS)):
            A(fmt_g(vol_g + pos_g, [e for e in rows if cond2(e)]))
    for pos_g, cond in (("幅位<0.3(全部)", lambda e: e["pos"] < RANGE_POS),
                        ("幅位≥0.3(全部)", lambda e: e["pos"] >= RANGE_POS)):
        A(fmt_g(pos_g, [e for e in all_events if cond(e)]))
    A("")
    A("## 结论(占位, 由分析填入)")
    text = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"--- 报告: {output}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="盘中破位 override 规则回测")
    parser.add_argument("--codes", default="03690,00700,00005,00388")
    parser.add_argument("--output", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "reports",
        f"backtest-override-{TODAY}.md"))
    args = parser.parse_args()
    sys.exit(main([normalize_hk_code(c) for c in args.codes.split(",")],
                  os.path.normpath(args.output)))
