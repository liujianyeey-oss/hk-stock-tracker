#!/usr/bin/env python3
"""美团-W (hk03690) 信号规则历史回测。纯标准库, 复用 tracker.py 数据拉取与筹码模型。
用法: python3 backtest.py"""
import argparse
import datetime
import json
import math
import os
from zoneinfo import ZoneInfo
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __package__:
    from . import hk_calendar
    from .tracker import UA, atomic_write, fetch_kline, fetch_short_selling, fill_missing_turnover, validate_kline, chip_metrics
else:
    import hk_calendar
    from tracker import UA, atomic_write, fetch_kline, fetch_short_selling, fill_missing_turnover, validate_kline, chip_metrics

_source_dir = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_source_dir) == "hk_stock_tracker":
    _parent = os.path.dirname(_source_dir)
    _default_base = _parent if os.path.isfile(os.path.join(_parent, "pyproject.toml")) else os.path.expanduser("~/.local/share/hk-stock-tracker")
else:
    _default_base = _source_dir
BASE = os.path.abspath(os.path.expanduser(os.environ.get("HK_TRACKER_HOME", _default_base)))
HORIZONS = (5, 10, 20)


def fetch_southbound_all():
    out, page = {}, 1
    while True:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               f"sortColumns=HOLD_DATE&sortTypes=-1&pageSize=500&pageNumber={page}&"
               "reportName=RPT_MUTUAL_STOCK_HOLDRANKS&columns=ALL&"
               "filter=(SECURITY_CODE%3D%2203690%22)")
        req = urllib.request.Request(url, headers={**UA, "Referer": "https://data.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        rows = d["result"]["data"]
        for x in rows:
            dt = x["HOLD_DATE"][:10]
            c = x.get("HOLD_SHARES_CHANGE") or 0
            out[dt] = max(out.get(dt, c), c)
        if len(rows) < 500 or page * 500 >= d["result"]["count"]:
            break
        page += 1
    return out


MAX_CHIP_BINS = 100_000


def rolling_chip_metrics(data, width=0.5):
    """前向递推；价格网格仅随已观察到的价格扩展。"""
    if not math.isfinite(width) or width <= 0:
        raise ValueError("分箱宽度必须为正有限数")
    if any(not math.isfinite(d['low'] / width) or not math.isfinite(d['high'] / width)
           or d['high'] / width > 2**52 for d in data):
        raise ValueError("分箱宽度过小，价格网格无法可靠计算")
    W = width
    b0 = int(data[0]["low"] / W) * W
    nb = int((data[0]["high"] - b0) / W) + 2
    if nb > MAX_CHIP_BINS:
        raise ValueError(f"筹码网格超过 {MAX_CHIP_BINS} 个分箱，请增大分箱宽度")
    centers = [b0 + (i + 0.5) * W for i in range(nb)]

    def tri(L, H, P):
        w = [0.0] * nb
        if H <= L:
            w[min(max(int((L - b0) / W), 0), nb - 1)] = 1.0
            return w
        for i, x in enumerate(centers):
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
            w[i] = max(v, 0.0)
        s = sum(w)
        if not s:
            w[min(max(int((P - b0) / W), 0), nb - 1)] = 1.0
            return w
        return [v / s for v in w]

    chips = [0.0] * nb
    profits, peaks = [None] * len(data), [None] * len(data)
    for i, d in enumerate(data):
        new_b0 = int(d["low"] / W) * W
        if new_b0 < b0:
            extra = round((b0 - new_b0) / W)
            if len(chips) + extra > MAX_CHIP_BINS:
                raise ValueError(f"筹码网格超过 {MAX_CHIP_BINS} 个分箱，请增大分箱宽度")
            chips = [0.0] * extra + chips
            b0 = new_b0
        needed = int((d["high"] - b0) / W) + 2
        if needed > MAX_CHIP_BINS:
            raise ValueError(f"筹码网格超过 {MAX_CHIP_BINS} 个分箱，请增大分箱宽度")
        if needed > len(chips):
            chips.extend([0.0] * (needed - len(chips)))
        nb = len(chips)
        centers = [b0 + (j + 0.5) * W for j in range(nb)]
        t = d["t"] / 100.0
        if i > 0:
            chips = [c * (1.0 - t) for c in chips]
        w = tri(d["low"], d["high"], (d["high"] + d["low"] + 2 * d["close"]) / 4.0)
        add = t * 100.0
        chips = [c + add * wi for c, wi in zip(chips, w)]
        tot = sum(chips)
        if tot <= 0:
            continue
        metrics = chip_metrics([c / tot * 100 for c in chips], centers, W, d['close'])
        profits[i] = metrics['profit']
        peaks[i] = metrics['peaks'][0][0]
    return profits, peaks


def stats(rets):
    if not rets:
        return None
    s = sorted(rets)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return {"n": n, "avg": sum(s) / n * 100, "med": med * 100,
            "win": sum(1 for x in s if x > 0) / n * 100}


def signal_flags(data, profits, peaks, ratio_map, south_map, estimated_dates=()):
    """保守地使用前一交易日的南向与沽空数据；实际发布时间仍需核验。"""
    cl = [d["close"] for d in data]
    tt = [d["t"] for d in data]
    dt = [d["date"] for d in data]
    estimated = set(estimated_dates)
    sig = {key: [False] * len(data) for key in ("S1", "S2", "S3u", "S3d", "S4", "S5a", "S5b")}
    for i in range(len(data)):
        if profits[i] is not None and profits[i] < 10:
            sig["S1"][i] = True
        if i > 0 and dt[i] not in estimated and peaks[i] is not None and tt[i] > 0.8 \
                and cl[i] > peaks[i] and cl[i - 1] <= peaks[i]:
            sig["S2"][i] = True
        if i >= 20 and not any(d in estimated for d in dt[i - 20:i + 1]):
            m20 = sum(tt[i - 20:i]) / 20
            if tt[i] > 2 * m20:
                sig["S3u" if cl[i] > cl[i - 1] else "S3d"][i] = True
        if i > 0 and dt[i - 1] in ratio_map and ratio_map[dt[i - 1]] > 25:
            sig["S4"][i] = True
        if i >= 5:
            win5 = dt[i - 5:i]
            if all(d in south_map for d in win5):
                chg5 = [south_map[d] for d in win5]
                if i >= 6 and all(c > 0 for c in chg5) and cl[i - 1] < cl[i - 6]:
                    sig["S5a"][i] = True
                if all(c < 0 for c in chg5):
                    sig["S5b"][i] = True
    return sig


def completed_kline(data, now):
    """盘中排除今日K线，不把未完成交易日当作收盘。"""
    today_rows = [row for row in data if row["date"] == now.date().isoformat()]
    if today_rows and not hk_calendar.is_finished(now.date(), now):
        return [row for row in data if row["date"] != now.date().isoformat()]
    return list(data)


def main(end_date=None, output=None, overwrite=False):
    now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    today = now.date()
    end_date = end_date or today.isoformat()
    try:
        requested = datetime.date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("--end 需要 YYYY-MM-DD 日期") from exc
    if requested < datetime.date(2023, 1, 1) or requested > today:
        raise ValueError("--end 必须在 2023-01-01 至今天之间")
    print("拉取日K...")
    data = fetch_kline(end_date)
    completed = completed_kline(data, now)
    if len(completed) != len(data):
        print("今日K线尚未收盘，已排除。")
    data = completed
    validate_kline(data, minimum=21)
    estimated_dates = fill_missing_turnover(data)
    print(f"{len(data)} 交易日 {data[0]['date']}~{data[-1]['date']}; 滚动筹码重算...")
    profits, peaks = rolling_chip_metrics(data)

    try:
        shorts = fetch_short_selling()
    except Exception as exc:
        print(f"沽空数据不可用：{exc}", file=sys.stderr)
        shorts = []
    ratio_map = {x["date"]: x["ratio"] for x in shorts}
    try:
        south_map = fetch_southbound_all()
    except Exception as exc:
        print(f"南向数据不可用：{exc}", file=sys.stderr)
        south_map = {}
    # 不把截止日期之后的数据统计进报告；信号仍按上一交易日对齐。
    ratio_map = {day: ratio for day, ratio in ratio_map.items() if day <= data[-1]["date"]}
    south_map = {day: change for day, change in south_map.items() if day <= data[-1]["date"]}
    cl = [d["close"] for d in data]
    n = len(data)
    sig = signal_flags(data, profits, peaks, ratio_map, south_map, estimated_dates)

    def first_hits(flags):
        return [i for i in range(n) if flags[i] and (i == 0 or not flags[i - 1])]

    def fwd(i, h):
        return cl[i + h] / cl[i] - 1 if i + h < n else None

    bench = {h: stats([fwd(i, h) for i in range(n) if fwd(i, h) is not None]) for h in HORIZONS}

    names = {"S1": "S1 极端套牢(获利盘<10%)", "S2": "S2 上穿最大筹码峰+换手>0.8%",
             "S3u": "S3 放量异动·涨(攻击)", "S3d": "S3 放量异动·跌(恐慌)",
             "S4": "S4 沽空比例>25%(ETNet仅40日)", "S5a": "S5a 南向连续5日增持+价跌",
             "S5b": "S5b 南向连续5日减持(对照)"}
    lines = []
    A = lines.append
    A(f"# 美团-W 信号回测 {data[0]['date']} ~ {data[-1]['date']}")
    A("")
    A(f"说明：{n} 个交易日、单标的，统计功效有限；连续触发按首次去重；"
      "收益自信号日收盘起算，未扣交易成本。南向和沽空采用前一交易日数据，"
      "不保证覆盖真实发布时间；前复权历史可能受到后续公司行为影响。"
      f"有 {len(estimated_dates)} 日换手率使用仅依赖历史的估算，相关放量信号不触发。")
    A("")
    hdr = "信号 | 触发 | +5日均/中/胜% | +10日均/中/胜% | +20日均/中/胜%"
    A(hdr.replace(" | ", " | "))
    A("--- | --- | --- | --- | ---")

    def row(name, hits):
        cells = []
        for h in HORIZONS:
            st = stats([fwd(i, h) for i in hits if fwd(i, h) is not None])
            if st:
                cells.append(f"{st['avg']:+.2f}/{st['med']:+.2f}/{st['win']:.0f}"
                             + f"({st['n']})")
            else:
                cells.append("无样本")
        thin = " ⚠薄样本" if len(hits) < 10 else ""
        return f"{name} | {len(hits)}{thin} | " + " | ".join(cells)

    A(row("基准(全部交易日)", list(range(n))))
    results = {}
    for k in ("S1", "S2", "S3u", "S3d", "S4", "S5a", "S5b"):
        hits = first_hits(sig[k])
        results[k] = hits
        A(row(names[k], hits))
    A("")
    A("注：括号内为该期限可用样本数(尾部交易日无足够后续数据)；收益单位%。")
    A("")
    A("## 结论")
    for k in ("S1", "S2", "S3u", "S3d", "S4", "S5a", "S5b"):
        hits = results[k]
        if len(hits) < 10:
            A(f"- {names[k]}：样本 {len(hits)} 次，过薄无法判断。")
            continue
        verdicts = []
        for h in HORIZONS:
            st = stats([fwd(i, h) for i in hits if fwd(i, h) is not None])
            if st and st["n"] >= 10:
                edge = st["avg"] - bench[h]["avg"]
                verdicts.append(f"+{h}日边际{edge:+.2f}pp胜率{st['win']:.0f}%"
                                f"(基准{bench[h]['win']:.0f}%)")
        A(f"- {names[k]}：" + ("；".join(verdicts) if verdicts else "各期限样本均不足。"))

    south_days = sorted(south_map)
    A(f"\n数据说明：沽空历史 {len(ratio_map)} 日(ETNet)；南向历史 "
      f"{south_days[0] if south_days else '-'} ~ {south_days[-1] if south_days else '-'}"
      f" 共 {len(south_map)} 日(东财接口仅覆盖该区间)。")
    text = "\n".join(lines) + "\n"
    rpath = output or os.path.join(BASE, "reports", f"backtest-{end_date}.md")
    os.makedirs(os.path.dirname(os.path.abspath(rpath)), exist_ok=True)
    try:
        if overwrite:
            atomic_write(rpath, lambda fh: fh.write(text))
        else:
            with open(rpath, "x", encoding="utf-8") as fh:
                fh.write(text)
    except FileExistsError as exc:
        raise ValueError(f"报告已存在：{rpath}；指定 --output 或 --overwrite") from exc
    print(text)
    print(f"--- 报告: {rpath}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="美团历史信号研究（仅供参考）")
    parser.add_argument("--end", help="截止日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--output", help="报告文件路径，默认 reports/backtest-<截止日期>.md")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有报告")
    args = parser.parse_args()
    try:
        sys.exit(main(args.end, args.output, args.overwrite))
    except (ValueError, OSError, KeyError, IndexError) as exc:
        print(f"回测失败：{exc}", file=sys.stderr)
        sys.exit(1)
