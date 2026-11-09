#!/usr/bin/env python3
"""港股筹码跟踪。纯标准库。
用法: python3 tracker.py daily [--code 0700]      每日收盘筹码日报(落盘)
      python3 tracker.py intraday [--code 0700]   盘中日内操作分析(仅显著信号落日志)
      python3 tracker.py init --code 0700         初始化新标的的关键位草稿"""
import csv
import hashlib
import io
import datetime
import json
import os
import sys
import argparse
from contextlib import contextmanager
import math
import re
import stat
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

if __package__:
    from . import hk_calendar
else:
    import hk_calendar

_source_dir = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_source_dir) == "hk_stock_tracker":
    _parent = os.path.dirname(_source_dir)
    _default_base = _parent if os.path.isfile(os.path.join(_parent, "pyproject.toml")) else os.path.expanduser("~/.local/share/hk-stock-tracker")
else:
    _default_base = _source_dir
BASE = os.path.abspath(os.path.expanduser(os.environ.get("HK_TRACKER_HOME", _default_base)))
DATA_DIR = os.path.join(BASE, "data")
REPORT_DIR = os.path.join(BASE, "reports")
DEFAULT_CODE = "03690"
KLINE_START = "2023-01-01"
UA = {"User-Agent": "Mozilla/5.0"}


def normalize_hk_code(value):
    """兼容 700 / 0700 / 00700 / 700.HK，统一为 5 位代码。"""
    match = re.fullmatch(r"([0-9]{1,5})(?:\.HK)?", str(value).strip(), re.IGNORECASE)
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"无效港股代码: {value!r}；应为 1–5 位数字，可带 .HK")
    return match.group(1).zfill(5)


def etnet_code(code):
    return str(int(code))


def tencent_symbol(code):
    return "hk" + code


def stock_dirs(code):
    """3690 保留旧路径；新标的按代码隔离。"""
    if code == DEFAULT_CODE:
        return DATA_DIR, REPORT_DIR
    return os.path.join(DATA_DIR, code), os.path.join(REPORT_DIR, code)


@contextmanager
def stock_lock(data_dir):
    """对同一标的的写入命令加非阻塞进程锁，进程退出后自动释放。"""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, ".tracker.lock")
    with open(path, "a+b") as fh:
        if os.name == "nt":
            import msvcrt
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError(f"该标的已有任务运行，请稍后重试：{data_dir}") from exc
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"该标的已有任务运行，请稍后重试：{data_dir}") from exc
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def choose_bin_width(price):
    raw = max(float(price), 0.01) * 0.0075
    candidates = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100]
    return min(candidates, key=lambda x: abs(x - raw))


def _positive_width(value, name):
    try:
        width = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是正数") from exc
    if not math.isfinite(width) or width <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return width


def daily_bin_width(levels, price, code=DEFAULT_CODE):
    return _positive_width(levels.get("chip_bin_width", 0.5 if code == DEFAULT_CODE
                                      else choose_bin_width(price)), "chip_bin_width")


def intraday_bin_width(levels, price, code=DEFAULT_CODE):
    return _positive_width(levels.get("intraday_bin_width", 1.0 if code == DEFAULT_CODE
                                      else choose_bin_width(price) / 2.0), "intraday_bin_width")


def fill_missing_turnover(data):
    """仅使用已出现的历史值估算缺失换手；返回估算的日期。"""
    observed = []
    estimated = []
    for row in data:
        value = row.get("t")
        if value is None or value <= 0:
            row["t"] = sum(observed[-5:]) / len(observed[-5:]) if observed else 0.5
            estimated.append(row["date"])
        else:
            if not math.isfinite(value) or value > 100:
                raise ValueError(f"{row['date']} 换手率无效: {value}")
            observed.append(value)
    return estimated


def validate_kline(data, minimum=1):
    if len(data) < minimum:
        raise ValueError(f"K线样本不足：需要至少 {minimum} 条，实际 {len(data)} 条")
    previous_date = None
    for row in data:
        try:
            date = datetime.date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("K线日期无效") from exc
        if previous_date is not None and date <= previous_date:
            raise ValueError("K线日期必须严格递增，不能重复")
        previous_date = date
        for key in ("open", "close", "high", "low"):
            value = row.get(key)
            if value is None or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{row['date']} K线字段 {key} 无效")
        if row["low"] > row["high"] or not row["low"] <= row["close"] <= row["high"]:
            raise ValueError(f"{row['date']} K线高低价或收盘价无效")
        if row["vol"] < 0 or not math.isfinite(row["vol"]):
            raise ValueError(f"{row['date']} 成交量无效")


def load_levels(path):
    try:
        with open(path, encoding="utf-8") as fh:
            levels = json.load(fh)
    except FileNotFoundError as exc:
        raise ValueError(f"未找到配置 {path}；请先运行 init --code <代码>") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取配置 {path}: {exc}") from exc
    if not isinstance(levels, dict):
        raise ValueError("配置必须是 JSON 对象")
    if "auto_generated" in levels and not isinstance(levels["auto_generated"], bool):
        raise ValueError("auto_generated 必须为布尔值")
    for kind in ("support", "resistance"):
        values = levels.get(kind)
        if not isinstance(values, list) or not values:
            raise ValueError(f"配置 {kind} 必须是非空列表")
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("note"), str):
                raise ValueError(f"配置 {kind} 每项必须包含 price 和 note")
            item["price"] = _positive_width(item.get("price"), f"{kind}.price")
    if not isinstance(levels.get("thresholds"), dict) or not isinstance(levels.get("playbook"), list):
        raise ValueError("配置缺少 thresholds 或 playbook")
    for key in ("vol_active", "spike_x", "lull_x"):
        levels["thresholds"][key] = _positive_width(levels["thresholds"].get(key), f"thresholds.{key}")
    valid_keys = {"close_above", "close_below", "close_between", "turnover_above", "turnover_below"}
    for rule in levels["playbook"]:
        if not isinstance(rule, dict) or any(not isinstance(rule.get(k), str) for k in ("id", "group", "stance")):
            raise ValueError("playbook 每条规则必须包含 id、group 和 stance")
        if rule.get("action", "observe") not in {"entry", "exit", "reduce", "observe"}:
            raise ValueError("playbook action 必须为 entry、exit、reduce 或 observe")
        when = rule.get("when")
        if not isinstance(when, dict) or not when or set(when) - valid_keys:
            raise ValueError("规则条件为空或包含不支持的字段")
        for key, value in when.items():
            if key == "close_between":
                if not isinstance(value, list) or len(value) != 2:
                    raise ValueError("close_between 必须包含两个价格")
                when[key] = [_positive_width(x, key) for x in value]
                if when[key][0] > when[key][1]:
                    raise ValueError("close_between 下限不能大于上限")
            else:
                when[key] = _positive_width(value, key)
    if not isinstance(levels.get("default_stance"), str):
        raise ValueError("配置缺少 default_stance")
    return levels


def http_get(url, encoding="utf-8"):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(encoding, errors="replace")


class KlineRows(list):
    """Parsed rows with per-request provenance; still behaves like a list."""
    pass


def fetch_kline(today, code=DEFAULT_CODE, adjust="qfq"):
    rows = {}
    provenance = []
    symbol = tencent_symbol(code)
    end = datetime.date.fromisoformat(today)
    start = datetime.date.fromisoformat(KLINE_START)
    if end < start:
        raise ValueError(f"截止日期不能早于 {KLINE_START}")
    windows = []
    while start <= end:
        window_end = min(end, datetime.date(start.year + 1, 12, 31))
        windows.append((start.isoformat(), window_end.isoformat()))
        start = window_end + datetime.timedelta(days=1)
    for a, b in windows:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
               f"param={symbol},day,{a},{b},800,{adjust}")
        raw = http_get(url)
        k = json.loads(raw)["data"][symbol]
        selected = "day" if k.get("day") else "qfqday" if k.get("qfqday") else "hfqday" if k.get("hfqday") else None
        provenance.append({"start": a, "end": b, "requested_adjustment": adjust,
                           "returned_fields": [key for key in ("day", "qfqday", "hfqday") if key in k],
                           "selected_field": selected, "response_text_sha256": hashlib.sha256(raw.encode('utf-8')).hexdigest()})
        for rec in k.get(selected, []) if selected else []:
            if rec and len(rec) >= 6 and a <= rec[0] <= b:
                rows[rec[0]] = rec
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    out = []
    for d in sorted(rows):
        if d < KLINE_START:
            continue
        r = rows[d]
        out.append({"date": d, "open": f(r[1]), "close": f(r[2]), "high": f(r[3]),
                    "low": f(r[4]), "vol": f(r[5]) or 0.0,
                    "t": f(r[7]) if len(r) > 7 else None})
    result = KlineRows(out)
    result.provenance = provenance
    return result


def fetch_quote(code=DEFAULT_CODE):
    """实时行情：东财为主（腾讯港股免费报价延迟约15分钟，2026-11-09 实测），
    东财失败回退腾讯。返回字段口径不变。"""
    try:
        return _fetch_quote_eastmoney(code)
    except Exception:
        return _fetch_quote_tencent(code)


def _fetch_quote_eastmoney(code):
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": f"116.{code}",
              "fields": "f43,f44,f45,f57,f58,f59,f60,f86,f170,f174,f175",
              "ut": "fa5fd1943c7b386f172d6893dbfba10b"}
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={**UA, "Referer": "https://quote.eastmoney.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]
    scale = 10 ** int(d["f59"])
    return {"name": d["f58"] or code, "price": d["f43"] / scale,
            "prev_close": d["f60"] / scale, "pct": d["f170"] / 100.0,
            "high": d["f44"] / scale, "low": d["f45"] / scale,
            "time": datetime.datetime.fromtimestamp(d["f86"]),
            "w52_high": d["f174"] / scale, "w52_low": d["f175"] / scale}


def _fetch_quote_tencent(code):
    txt = http_get(f"https://qt.gtimg.cn/q={tencent_symbol(code)}", encoding="gbk")
    f = txt.split("~")
    q = {"name": f[1].strip() or code, "price": float(f[3]),
         "prev_close": float(f[4]), "pct": float(f[32]),
         "high": float(f[33]), "low": float(f[34]),
         "time": datetime.datetime.strptime(f[30], "%Y/%m/%d %H:%M:%S"),
         "w52_high": float(f[48]), "w52_low": float(f[49])}
    return q


def build_chips(data, W):
    if not data:
        raise ValueError("无可用于筹码计算的K线")
    W = _positive_width(W, "筹码分箱宽度")
    b0 = int(min(d["low"] for d in data) / W) * W
    nb = int((max(d["high"] for d in data) - b0) / W) + 2
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
    for d in data:
        t = d["t"] / 100.0
        if d is not data[0]:
            chips = [c * (1.0 - t) for c in chips]
        w = tri(d["low"], d["high"], (d["high"] + d["low"] + 2 * d["close"]) / 4.0)
        chips = [c + t * 100.0 * wi for c, wi in zip(chips, w)]
    total = sum(chips)
    if total <= 0:
        raise ValueError("筹码总量为零，无法计算")
    return [c / total * 100.0 for c in chips], centers, W


def chip_metrics(chips, centers, W, price):
    cdf, s = [], 0.0
    for c in chips:
        s += c
        cdf.append(s)

    def pct_price(q):
        for i, v in enumerate(cdf):
            if v >= q:
                if i == 0:
                    return centers[0]
                gap = cdf[i] - cdf[i - 1]
                frac = (q - cdf[i - 1]) / gap if gap > 0 else 0.5
                return centers[i - 1] + frac * W
        return centers[-1]

    profit = 0.0
    for i, c in enumerate(chips):
        Lb, Hb = centers[i] - W / 2, centers[i] + W / 2
        if Hb <= price:
            profit += c
        elif Lb < price < Hb:
            profit += c * (price - Lb) / W
    m = {"profit": profit,
         "avg_cost": sum(x * c for x, c in zip(centers, chips)) / 100.0,
         "p5": pct_price(5), "p95": pct_price(95),
         "p15": pct_price(15), "p85": pct_price(85)}
    m["conc90"] = (m["p95"] - m["p5"]) / (m["p95"] + m["p5"]) * 100
    m["conc70"] = (m["p85"] - m["p15"]) / (m["p85"] + m["p15"]) * 100
    nb = len(chips)
    sm = [sum(chips[max(0, i - 2):i + 3]) / len(chips[max(0, i - 2):i + 3]) for i in range(nb)]
    locmax = [i for i in range(1, nb - 1) if sm[i] >= sm[i - 1] and sm[i] > sm[i + 1]]
    locmax.sort(key=lambda i: -sm[i])
    picked = []
    for i in locmax:
        if all(abs(i - j) >= 4 for j in picked):
            picked.append(i)
        if len(picked) >= 8:
            break
    if not picked:
        picked = [max(range(nb), key=lambda i: sm[i])]
    picked.sort(key=lambda i: -sm[i])
    m["peaks"] = [(centers[i], sum(chips[max(0, i - 2):i + 3])) for i in picked[:5]]
    return m


def load_snapshots(snap_path):
    if not os.path.exists(snap_path):
        return []
    with open(snap_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


SNAP_FIELDS = ["date", "status", "close", "pct_chg", "turnover", "t20", "t60",
               "profit", "avg_cost", "p5", "p95", "conc90", "p15", "p85",
               "conc70", "peak1", "peak1_share", "short_amount", "short_ratio",
               "south_hold", "south_chg", "events", "stance"]


def atomic_write(path, write_content, *, newline=None):
    """在同一目录写临时文件并原子替换，失败时保留原文件。"""
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if os.path.islink(path):
        raise ValueError(f"拒绝替换符号链接：{path}")
    previous_mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else None
    fd, temporary = tempfile.mkstemp(prefix=".mt-write-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            write_content(fh)
            fh.flush()
            os.fsync(fh.fileno())
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot_text(row, snap_path):
    rows = [r for r in load_snapshots(snap_path) if r["date"] != row["date"]]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=SNAP_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def save_snapshot(row, snap_path):
    text = snapshot_text(row, snap_path)
    atomic_write(snap_path, lambda fh: fh.write(text), newline="")


def _file_hash(path):
    if os.path.islink(path):
        raise ValueError(f"拒绝读取符号链接：{path}")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recover_daily(data_dir, report_dir):
    """在持有标的锁时完成中断的写入；目标被外部修改时拒绝覆盖。"""
    journal = os.path.join(data_dir, ".daily-transaction.json")
    if os.path.islink(journal):
        raise ValueError(f"拒绝读取符号链接：{journal}")
    if not os.path.exists(journal):
        return False
    try:
        with open(journal, encoding="utf-8") as fh:
            record = json.load(fh)
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError("事务版本无效")
        date = record["date"]
        if not isinstance(date, str) or datetime.date.fromisoformat(date).isoformat() != date:
            raise ValueError("事务日期无效")
        pairs = [(os.path.join(data_dir, "snapshots.csv"), record["snapshot"], record["snapshot_before"]),
                 (os.path.join(report_dir, f"{date}.md"), record["report"], record["report_before"])]
        for path, text, before in pairs:
            if not isinstance(text, str) or (before is not None and
                    (not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{64}", before))):
                raise ValueError("事务记录字段无效")
            current = _file_hash(path)
            if current not in (before, _text_hash(text)):
                raise ValueError(f"待恢复文件已被外部修改：{path}；保留事务记录，请人工检查")
        for path, text, _ in pairs:
            if _file_hash(path) != _text_hash(text):
                atomic_write(path, lambda fh, content=text: fh.write(content), newline="")
        os.unlink(journal)
        return True
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"日报事务记录损坏：{journal}；请人工检查") from exc


def commit_daily(data_dir, report_dir, date, row, report):
    """先保存恢复记录，再分别原子替换快照和日报；要求调用方持有标的锁。"""
    journal = os.path.join(data_dir, ".daily-transaction.json")
    if os.path.lexists(journal):
        raise ValueError("存在待恢复的日报事务，不能覆盖")
    snap_path = os.path.join(data_dir, "snapshots.csv")
    report_path = os.path.join(report_dir, f"{date}.md")
    record = {
        "version": 1, "date": date,
        "snapshot": snapshot_text(row, snap_path), "report": report,
        "snapshot_before": _file_hash(snap_path), "report_before": _file_hash(report_path),
    }
    atomic_write(journal, lambda fh: json.dump(record, fh, ensure_ascii=False))
    recover_daily(data_dir, report_dir)


def risk_reward(levels, price):
    supports = [lv["price"] for lv in levels["support"] if lv["price"] < price]
    resistances = [lv["price"] for lv in levels["resistance"] if lv["price"] > price]
    if not supports or not resistances:
        return None
    downside = (price - max(supports)) / price * 100
    upside = (min(resistances) - price) / price * 100
    return upside, downside, upside / downside


def rule_match(when, close, turnover):
    valid_keys = {"close_above", "close_below", "close_between", "turnover_above", "turnover_below"}
    if not isinstance(when, dict) or not when or set(when) - valid_keys:
        raise ValueError("规则条件为空或包含不支持的字段")
    if "close_above" in when and not close > when["close_above"]:
        return False
    if "close_below" in when and not close < when["close_below"]:
        return False
    if "close_between" in when:
        lo, hi = when["close_between"]
        if not lo <= close <= hi:
            return False
    if "turnover_above" in when and not turnover >= when["turnover_above"]:
        return False
    if "turnover_below" in when and not turnover < when["turnover_below"]:
        return False
    return True


def evaluate_playbook(levels, price, turnover, rr, *, intraday, t_est):
    """只返回满足数据质量和已标记入场风险条件的规则。
    自动草稿标的中，backtest_validated 规则照常评估（关键位未校准不影响回测规则）。"""
    fired, suppressed = [], []
    if not levels.get("auto_generated"):
        rules = levels["playbook"]
    else:
        rules = [r for r in levels["playbook"] if r.get("backtest_validated")]
    for rule in rules:
        when = rule["when"]
        if not rule_match(when, price, turnover):
            continue
        reason = None
        if intraday and any(key.startswith("close_") for key in when):
            reason = "收盘条件盘中未确认"
        elif t_est and any(key.startswith("turnover_") for key in when):
            reason = "换手率为估算值"
        elif rule.get("action") == "entry" and (rr is None or rr[2] < 1):
            reason = "赔率不足1" if rr is not None else "缺少可计算赔率的支撑或阻力"
        if reason:
            suppressed.append(f"{rule['id']}：{reason}")
        else:
            fired.append(rule)
    return fired, suppressed


def daily(code=DEFAULT_CODE):
    code = normalize_hk_code(code)
    data_dir, _ = stock_dirs(code)
    with stock_lock(data_dir):
        if recover_daily(data_dir, stock_dirs(code)[1]):
            print("已恢复上次中断的日报事务。")
        return _daily(code)


def _daily(code):
    data_dir, report_dir = stock_dirs(code)
    levels_path = os.path.join(data_dir, "levels.json")
    snap_path = os.path.join(data_dir, "snapshots.csv")
    now = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong"))
    today = now.date().isoformat()
    levels = load_levels(levels_path)
    quote = fetch_quote(code)
    data = fetch_kline(today, code)
    validate_kline(data, minimum=79)

    last = data[-1]
    if quote["time"].date().isoformat() != last["date"]:
        raise ValueError("实时行情日期与最新K线不一致，暂停生成报告")
    prev_snaps = load_snapshots(snap_path)
    prev = next((r for r in reversed(prev_snaps) if r["date"] < last["date"]), None)
    if prev_snaps and last["date"] == prev_snaps[-1]["date"] and last["date"] != today:
        print(f"无新交易日：最新K线为 {last['date']}（今日 {today} 非交易日/未开盘），不落盘。")
        return 0

    if last["date"] == today and hk_calendar.session_close(now.date()) is None:
        raise ValueError("日历显示今日休市，但行情包含今日K线；请核验数据源")
    intraday = not hk_calendar.is_finished(datetime.date.fromisoformat(last["date"]), now)
    status = "盘中未收盘" if intraday else "收盘"

    estimated_dates = fill_missing_turnover(data)
    t_est = last["date"] in estimated_dates

    ts = [d["t"] for d in data]
    t20 = sum(ts[-20:]) / 20
    t60 = sum(ts[-60:]) / 60
    price = quote["price"] or last["close"]

    W = daily_bin_width(levels, price, code)
    chips, centers, W = build_chips(data, W)
    m = chip_metrics(chips, centers, W, price)
    peak1, peak1_share = m["peaks"][0]

    events = []
    th = levels["thresholds"]
    if prev:
        pc = float(prev["close"])
        for lv in levels["resistance"] + levels["support"]:
            p = lv["price"]
            if pc < p <= price:
                events.append(f"上穿关键位 {p}({lv['note']})")
            elif pc > p >= price:
                events.append(f"下穿关键位 {p}({lv['note']})")
        for thp in (10, 50, 90):
            pp = float(prev["profit"])
            if pp < thp <= m["profit"]:
                events.append(f"获利盘上穿 {thp}% 阈值({pp:.1f}%→{m['profit']:.1f}%)")
            elif pp > thp >= m["profit"]:
                events.append(f"获利盘下穿 {thp}% 阈值({pp:.1f}%→{m['profit']:.1f}%)")
        if abs(float(prev["peak1"]) - peak1) > 1e-9:
            events.append(f"主峰迁移 {float(prev['peak1']):.2f}→{peak1:.2f}")
    if not t_est and last["t"] > th["spike_x"] * t20:
        events.append(f"放量异动：换手 {last['t']:.2f}% > {th['spike_x']}x20日均({t20:.2f}%)")
    elif not t_est and last["t"] < th["lull_x"] * t20:
        events.append(f"极度缩量：换手 {last['t']:.2f}% < {th['lull_x']}x20日均({t20:.2f}%)")

    rr = risk_reward(levels, price)
    fired, suppressed = evaluate_playbook(
        levels, price, last["t"], rr, intraday=intraday, t_est=t_est
    )
    stances = [f"[{r['group']}] {r['stance']}" for r in fired]
    if not stances:
        stances = (["自动生成草稿尚未人工校准，仅供观察，不产生交易触发。"]
                   if levels.get("auto_generated") else [levels["default_stance"]])
    if suppressed:
        stances.append("未触发：" + "；".join(suppressed))

    short_txt, short_same = [], False
    try:
        shorts = fetch_short_selling(code)
    except Exception:
        shorts = []
    if shorts:
        cur = shorts[0]
        short_same = cur["date"] == last["date"]
        ratios_all = [x["ratio"] for x in shorts]
        base5 = ratios_all[1:6] if short_same else ratios_all[0:5]
        base20 = ratios_all[1:21] if short_same else ratios_all[0:20]
        m5 = sum(base5) / len(base5) if base5 else None
        m20 = sum(base20) / len(base20) if base20 else None
        sd20 = (sum((x - m20) ** 2 for x in base20) / len(base20)) ** 0.5 if base20 else None
        tag = "" if short_same else f"(最新数据 {cur['date']})"
        short_txt.append(
            f"- 卖空 {cur['shares'] / 1e4:.0f}万股 / {cur['amount'] / 1e8:.2f}亿港元  "
            f"沽空比例 {cur['ratio']:.2f}%{tag}"
            + ("(金额估算)" if cur.get("amount_estimated") else "")
            + (f"  5日均 {m5:.1f}%" if m5 is not None else "")
            + (f"  20日均 {m20:.1f}%(±{sd20:.1f})" if m20 is not None else "")
            + f"  [{len(shorts)}日样本]")
        if short_same:
            r0 = cur["ratio"]
            if r0 > 25:
                events.append(f"沽空异动：沽空比例 {r0:.1f}% > 25%")
            elif m20 is not None and len(base20) >= 10 and r0 > m20 + sd20:
                events.append(f"沽空异动：{r0:.1f}% 超20日均值+1σ({m20 + sd20:.1f}%)")
            if m5 and m5 > 20 and r0 < m5 - 8 and quote["pct"] > 0:
                events.append(f"疑似逼空/空头回补：比例 {m5:.1f}%→{r0:.1f}% 骤降且收涨")
            spike = r0 > 25 or (m20 is not None and r0 > m20 + sd20)
            support0 = levels["support"][0]["price"]
            if spike and price < support0:
                short_txt.append("- 解读：沽空飙升+跌破支撑=空头主动进攻，看跌确认")
            elif spike:
                short_txt.append("- 解读：沽空飙升+价格未破支撑=潜在逼空燃料，观察承接")
            if m5 and r0 < m5 - 8 and quote["pct"] > 0:
                short_txt.append("- 解读：沽空骤降+价升=空头回补助推反弹")
        short_txt.append("- 注：港股沽空含做市商衍生品对冲，单看比例有噪音，须结合价格行为")
    else:
        short_txt.append("- 沽空数据获取失败(ETNet)，本节跳过")

    south_txt, south = [], []
    try:
        south = fetch_southbound(code)
    except Exception:
        south = []
    if south:
        s0 = south[0]
        chgs = [x["chg"] for x in south]
        net5, net20 = sum(chgs[:5]), sum(chgs[:20])
        consec, sign = 0, (1 if chgs[0] > 0 else -1 if chgs[0] < 0 else 0)
        for c in chgs:
            if sign and (c > 0) == (sign > 0) and c != 0:
                consec += 1
            else:
                break
        dir_txt = f"连续{'增持' if sign > 0 else '减持'}{consec}日" if consec else "当日持平"
        south_txt.append(
            f"- 港股通持股 {s0['hold'] / 1e8:.2f}亿股(占{s0['ratio']:.2f}%)  "
            f"当日{s0['chg'] / 1e4:+.0f}万股 | 5日净{net5 / 1e4:+.0f}万 "
            f"20日净{net20 / 1e4:+.0f}万 {dir_txt} [数据{s0['date']}, T+1]")
        kidx = {d["date"]: i for i, d in enumerate(data)}
        day_pct = None
        if s0["date"] in kidx and kidx[s0["date"]] > 0:
            i = kidx[s0["date"]]
            day_pct = (data[i]["close"] / data[i - 1]["close"] - 1) * 100
        if consec >= 5 and sign < 0:
            events.append(f"南向撤退：连续{consec}日净减持")
        if day_pct is not None and day_pct < -2 and s0["chg"] > 0:
            events.append(f"内资抄底：{s0['date']}跌{day_pct:.1f}%但南向净增持{s0['chg'] / 1e4:.0f}万股")
        if len(chgs) >= 21:
            b = chgs[1:21]
            mb = sum(b) / 20
            sdb = (sum((x - mb) ** 2 for x in b) / 20) ** 0.5
            if abs(s0["chg"] - mb) > 2 * sdb:
                events.append(f"南向异动：{s0['chg'] / 1e4:+.0f}万股 超20日均值±2σ")
        if day_pct is not None:
            if day_pct < 0 < s0["chg"]:
                south_txt.append("- 解读：价跌+南向增持=内资接盘，底部支撑信号")
            elif day_pct > 0 > s0["chg"]:
                south_txt.append("- 解读：价涨+南向减持=内资兑现，反弹质量存疑")
            elif day_pct > 0 and s0["chg"] > 0:
                south_txt.append("- 解读：价涨+南向增持=内外资共振，最健康")
            else:
                south_txt.append("- 解读：价跌+南向减持=内外资同撤，弱势确认")
        south_txt.append("- 注：南向是趋势资金，单日意义小、连续方向意义大")
    else:
        south_txt.append("- 南向数据获取失败(东财)，本节跳过")

    top10_txt = []
    try:
        top10 = fetch_southbound_top10(code)
    except Exception:
        top10 = None
        top10_txt.append("- 南向十大成交获取失败(东财)，本节跳过")
    if top10:
        rk = "/".join(f"{'沪' if k == '002' else '深'}#{v}" for k, v in sorted(top10["ranks"].items()))
        top10_txt.append(
            f"- 上榜({top10['date']} {rk})：净买{top10['net'] / 1e8:+.2f}亿"
            f"(买{top10['buy'] / 1e8:.2f}/卖{top10['sell'] / 1e8:.2f}, 人民币, 当晚更新)")
        if top10["date"] == last["date"]:
            save_south_top10(data_dir, top10)
        if top10["net"] < -2e8:
            events.append(f"南向大额净卖出 {top10['net'] / 1e8:.2f}亿(十大成交{top10['date']})")
        elif top10["net"] > 2e8:
            events.append(f"南向大额净买入 +{top10['net'] / 1e8:.2f}亿(十大成交{top10['date']})")
    elif not top10_txt:
        top10_txt.append("- 当日未上榜(十大成交之外)")
    if now.weekday() == 4:
        events.append("CCASS周度跟踪日：托管仓位变动手动查询(见 ccass 子命令)")

    tk = tech_indicators(data)

    try:
        macro = macro_liquidity()
    except Exception:
        macro = None
    ev_line = macro_event_line(load_macro_events(now.date()), now.date())

    lines = []
    A = lines.append
    A(f"# {quote['name']} ({tencent_symbol(code)}) 日报 {last['date']} [{status}]")
    A(f"- 现价 {price:.2f}({quote['pct']:+.2f}%) 高{quote['high']:.2f}/低{quote['low']:.2f}"
      f" 52周{quote['w52_low']:.2f}~{quote['w52_high']:.2f} | 换手 {last['t']:.3f}%"
      f"{'(历史值估算)' if t_est else ''}(20均{t20:.3f}/60均{t60:.3f})")
    A(f"## 筹码(衰减模型{len(data)}日) 获利{m['profit']:.1f}% 套牢{100 - m['profit']:.1f}%"
      f" 均成本{m['avg_cost']:.2f}")
    A(f"- 90%区间{m['p5']:.2f}~{m['p95']:.2f}(集中{m['conc90']:.1f}%) "
      f"70%区间{m['p15']:.2f}~{m['p85']:.2f}({m['conc70']:.1f}%) | 峰: "
      + " ".join(f"{p:.2f}({s:.1f}%)" for p, s in m["peaks"]))
    A("## 沽空(ETNet)")
    for s in short_txt:
        A(s)
    A("## 南向资金(港股通)")
    for s in south_txt:
        A(s)
    A("## 南向当日净买卖(十大成交)")
    for s in top10_txt:
        A(s)
    A("## 技术状态")
    A(f"- 均线 MA5 {tk['ma5']:.2f}/MA10 {tk['ma10']:.2f}/MA20 {tk['ma20']:.2f}"
      f"/MA60 {tk['ma60']:.2f} {tk['align']}")
    A(f"- MACD DIF {tk['dif']:.2f}/DEA {tk['dea']:.2f}/柱{tk['bar']:+.2f} {tk['macd_st']}"
      f" 底背离:{'有' if tk['macd_div'] else '无'} | RSI(14) {tk['rsi']:.0f}"
      + (" 超卖" if tk['rsi'] and tk['rsi'] < 30 else (" 超买" if tk['rsi'] and tk['rsi'] > 70 else "")))
    A(f"- 布林 %B {tk['bpct']:.2f} 带宽{tk['bw']:.1f}%{'[波动率挤压,变盘临近]' if tk['squeeze'] else ''}"
      f" | ADX(14) {tk['adx']:.0f} {tk['regime']}")
    A(f"- OBV 20日斜率{tk['obv_slope'] / 1e4:+.0f}万股/日 底背离:{'有(吸筹特征)' if tk['obv_div'] else '无'}"
      + (f" | 赔率 阻力{rr[0]:+.1f}%/支撑-{rr[1]:.1f}% 比值{rr[2]:.1f}{'[赔率不佳]' if rr[2] and rr[2] < 1 else ''}"
         if rr else ""))
    A("## 宏观流动性" + (f"(评分{macro['score']:+d}/±2)" if macro else ""))
    if macro:
        A("- " + "  ".join(macro["items"]) + " | 联系汇率7.75-7.85,贴7.85=流出压力(美债10Y/HIBOR无源,见TODO)")
    else:
        A("- 宏观数据获取失败，本节跳过")
    if ev_line:
        A(f"- {ev_line}")
    A("## 关键位 阻力: " + " ".join(f"{lv['price']}({(lv['price'] - price) / price * 100:+.1f}%)"
                                    for lv in levels["resistance"])
      + " | 支撑: " + " ".join(f"{lv['price']}({(lv['price'] - price) / price * 100:+.1f}%)"
                               for lv in levels["support"]))
    A("## 事件")
    for e in events:
        A(f"- {e}")
    if not events:
        A("- 无")
    A("## 立场")
    A(f"- 市型：{tk['regime']}(ADX {tk['adx']:.0f})——"
      + ("箱体playbook适用，趋势规则慎用" if tk["regime"] == "震荡市"
         else "趋势playbook适用，箱体高抛低吸慎用" if tk["regime"] == "趋势市"
         else "过渡市，箱体/趋势规则均减半仓执行"))
    if macro and macro["score"] >= 2:
        A("- 宏观流动性顺风(评分≥+2)")
    elif macro and macro["score"] <= -2:
        A("- 宏观流动性逆风(评分≤-2)，仓位口径降一档")
    for s in stances:
        A(f"- {s}")
    if levels.get("auto_generated"):
        A("- 提示：关键位为自动生成草稿，尚未人工校准；仅观察结构变化，暂不作为交易触发。")
    report = "\n".join(lines) + "\n"

    short_amt = short_r = ""
    if shorts and short_same:
        if not shorts[0].get("amount_estimated"):
            short_amt = f"{shorts[0]['amount'] / 1e8:.2f}"
        short_r = f"{shorts[0]['ratio']:.2f}"
    south_h = south_c = ""
    if south:
        south_h = f"{south[0]['hold'] / 1e8:.2f}"
        south_c = f"{south[0]['chg'] / 1e4:+.0f}"
    row = {"date": last["date"], "status": status, "close": f"{price:.2f}",
                   "pct_chg": f"{quote['pct']:+.2f}", "turnover": f"{last['t']:.3f}",
                   "t20": f"{t20:.3f}", "t60": f"{t60:.3f}", "profit": f"{m['profit']:.1f}",
                   "avg_cost": f"{m['avg_cost']:.2f}", "p5": f"{m['p5']:.2f}",
                   "p95": f"{m['p95']:.2f}", "conc90": f"{m['conc90']:.2f}",
                   "p15": f"{m['p15']:.2f}", "p85": f"{m['p85']:.2f}",
                   "conc70": f"{m['conc70']:.2f}", "peak1": f"{peak1:.2f}",
                   "peak1_share": f"{peak1_share:.2f}", "short_amount": short_amt,
                   "short_ratio": short_r, "south_hold": south_h, "south_chg": south_c,
                   "events": ";".join(events) or "-",
                   "stance": " | ".join(r["id"] for r in fired) or "default"}
    rpath = os.path.join(report_dir, f"{last['date']}.md")
    commit_daily(data_dir, report_dir, last["date"], row, report)

    print(report)
    print(f"--- 快照已写入 {os.path.relpath(snap_path, BASE)}({last['date']} {status}, 同日重跑覆盖); "
          f"报告: {os.path.relpath(rpath, BASE)}")
    return 0


def init_stock(code):
    code = normalize_hk_code(code)
    data_dir, _ = stock_dirs(code)
    with stock_lock(data_dir):
        if recover_daily(data_dir, stock_dirs(code)[1]):
            print("已恢复上次中断的日报事务。")
        return _init_stock(code)


def _init_stock(code):
    data_dir, _ = stock_dirs(code)
    levels_path = os.path.join(data_dir, "levels.json")
    if os.path.exists(levels_path):
        print(f"配置已存在：{os.path.relpath(levels_path, BASE)}，不覆盖。")
        return 0
    quote = fetch_quote(code)
    data = fetch_kline(datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat(), code)
    if data and quote["time"].date().isoformat() != data[-1]["date"]:
        raise ValueError("实时行情日期与最新K线不一致，暂停初始化")
    if len(data) < 60:
        print(f"K线样本不足({len(data)}条)，暂不初始化。")
        return 1
    price = quote["price"] or data[-1]["close"]
    W = choose_bin_width(price)
    validate_kline(data, minimum=60)
    estimated_dates = fill_missing_turnover(data)
    chips, centers, W = build_chips(data, W)
    m = chip_metrics(chips, centers, W, price)
    peaks = m["peaks"] or []
    above = sorted(p for p, _ in peaks if p > price * 1.01)[:5]
    below = sorted((p for p, _ in peaks if p < price * 0.99), reverse=True)[:5]
    if not above:
        above = [max(d["high"] for d in data[-252:]), price * 1.08]
    if not below:
        below = [min(d["low"] for d in data[-252:]), price * 0.92]
    lows = sorted(d["low"] for d in data[-252:])
    highs = sorted((d["high"] for d in data[-252:]), reverse=True)
    levels = {
        "updated": datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat(),
        "code": code,
        "name": quote.get("name", ""),
        "auto_generated": True,
        "benchmark": "hkHSI",
        "chip_bin_width": W,
        "intraday_bin_width": choose_bin_width(price) / 2.0,
        "note": "自动生成草稿：关键位来自筹码峰与52周结构，未人工校准，不作为交易触发。",
        "resistance": [{"price": round(p, 2), "note": "自动筹码/价格峰草稿"} for p in above],
        "support": [{"price": round(p, 2), "note": "自动筹码/价格谷草稿"} for p in below],
        "thresholds": {"vol_active": 0.8, "spike_x": 2.0, "lull_x": 0.5},
        "playbook": [],
        "default_stance": "自动草稿：关键位未人工校准，仅观察，不给交易建议。",
        "reference": {"w52_low": lows[0], "w52_high": highs[0], "avg_cost": m["avg_cost"]}
    }
    os.makedirs(data_dir, exist_ok=True)
    with open(levels_path, "w") as fh:
        json.dump(levels, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"已生成 {os.path.relpath(levels_path, BASE)}")
    if estimated_dates:
        print(f"提示：{len(estimated_dates)} 个交易日换手率缺失，使用历史值估算。")
    print("请先人工校准 resistance/support 与 playbook，再启用交易类分析。")
    return 0


def fetch_minute(code=DEFAULT_CODE):
    symbol = tencent_symbol(code)
    d = json.loads(http_get(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"))
    out = []
    for row in d["data"][symbol]["data"]["data"]:
        p = row.split()
        out.append({"hm": p[0], "price": float(p[1]),
                    "cumvol": float(p[2]), "cumamt": float(p[3])})
    return out


def fetch_index(code="hkHSTECH"):
    f = http_get(f"https://qt.gtimg.cn/q={code}", encoding="gbk").split("~")
    price, prev = float(f[3]), float(f[4])
    return {"price": price, "pct": (price - prev) / prev * 100}


def fetch_short_selling(code=DEFAULT_CODE):
    """ETNet 个股沽空历史(约40+交易日)。2026-10 页面改版：HTML 表格保留
    (当日临时数据单元格带红*前缀, 需剥离), 另有 drawComboChart2 JS 数组兜底。
    同一 URL: quote_shortsell.php?code=3690, 数据说明见 docs/JOURNAL.md。"""
    import re
    url = ("https://www.etnet.com.hk/www/eng/stocks/realtime/"
           f"quote_shortsell.php?code={etnet_code(code)}")
    html = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                html = r.read().decode("utf-8", errors="replace")
            break
        except Exception:
            continue
    if html is None:
        return []

    def num(s):
        s = s.lstrip("*").replace(",", "").rstrip("%")
        m = re.match(r"^([\d.]+)([MB]?)$", s)
        if not m:
            return None
        v = float(m.group(1))
        return v * {"": 1, "M": 1e6, "B": 1e9}[m.group(2)]
    out = []
    cells = [re.sub(r"<[^>]+>", "", c).strip()
             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", html, re.S)]
    for i, c in enumerate(cells):
        if re.match(r"^\d{2}/\d{2}/\d{4}$", c) and i + 5 < len(cells):
            d, mth, y = c.split("/")
            sh, amt, pct = num(cells[i + 1]), num(cells[i + 2]), num(cells[i + 5])
            if amt is None or pct is None:
                continue
            out.append({"date": f"{y}-{mth}-{d}", "shares": sh or 0.0,
                        "amount": amt, "ratio": pct})
    def unique_sorted(rows):
        # HTML 表格优先；重复日期不重复计入均值。
        by_date = {}
        for row in rows:
            by_date.setdefault(row["date"], row)
        return sorted(by_date.values(), key=lambda row: row["date"], reverse=True)

    if len(unique_sorted(out)) >= 5:
        return unique_sorted(out)
    # 兜底: drawComboChart2('stock', title, [dates], [closes], [pct], [sharesM], ...)
    m = re.search(r"drawComboChart2\('stock'[^[]*(\[[^\]]*\])\s*,\s*(\[[^\]]*\])\s*,"
                  r"\s*(\[[^\]]*\])\s*,\s*(\[[^\]]*\])", html, re.S)
    if not m:
        return unique_sorted(out)
    try:
        dates = json.loads(m.group(1).replace("\\/", "/"))
        closes = json.loads(m.group(2))
        pcts = json.loads(m.group(3))
        shares = json.loads(m.group(4))
    except (ValueError, TypeError):
        return unique_sorted(out)
    for d, c, p, shares_m in zip(dates, closes, pcts, shares):
        try:
            date = datetime.datetime.strptime(d, "%d/%m/%Y").date().isoformat()
            close, ratio, volume = float(c), float(p), float(shares_m)
            if any(not math.isfinite(v) or v < 0 for v in (close, ratio, volume)):
                continue
        except (TypeError, ValueError):
            continue
        out.append({"date": date, "shares": volume * 1e6,
                    "amount": volume * close * 1e6, "ratio": ratio,
                    "amount_estimated": True})
    return unique_sorted(out)


# TODO: 港交所股票期权日报(PCR/行权价持仓)——dmstat/dayrpt 下 stopt/dqe/d 等命名变体
# (261022 试) 均 404 或软404, 数据源暂缺, 待后续发现后补充。


def fetch_southbound(code=DEFAULT_CODE):
    """东财港股通持仓(T+1滞后)。002/004两行数值相同(均为合计), 按日期取max去重。"""
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "sortColumns=HOLD_DATE&sortTypes=-1&pageSize=60&"
           "reportName=RPT_MUTUAL_STOCK_HOLDRANKS&columns=ALL&"
           f"filter=(SECURITY_CODE%3D%22{code}%22)")
    req = urllib.request.Request(url, headers={**UA, "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    by = {}
    for x in d["result"]["data"]:
        dt = x["HOLD_DATE"][:10]
        h, c = x.get("HOLD_SHARES") or 0, x.get("HOLD_SHARES_CHANGE") or 0
        if dt in by:
            by[dt]["hold"] = max(by[dt]["hold"], h)
            by[dt]["chg"] = max(by[dt]["chg"], c)
        else:
            by[dt] = {"hold": h, "chg": c, "ratio": x.get("TOTAL_SHARES_RATIO") or 0}
    return [{"date": k, **v} for k, v in sorted(by.items(), reverse=True)]


# TODO: 宏观流动性补充指标数据源暂缺——美债10年收益率(东财 secid 112.US10Y/112.US10YN/
# 100.US10Y/101.US10Y 均 data=null, 2026-11-05 试)、HIBOR 1M(HKAB 页面 404+Nuxt JS 渲染)。
# 待发现可用源后并入 macro_liquidity 评分(总分范围需相应调整)。


def fetch_dxy_closes():
    """东财美元指数日线收盘序列。现价口径 f43/100, 此处直接取日K。"""
    beg = (datetime.date.today() - datetime.timedelta(days=70)).strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=100.UDI"
           f"&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg={beg}&end=20500101")
    req = urllib.request.Request(url, headers={**UA, "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return [float(k.split(",")[1]) for k in d["data"]["klines"]]


def fetch_usdhkd_closes():
    """腾讯 whUSDHKD 日线收盘序列(记录: 日期,开,收,高,低,...)。"""
    today = datetime.date.today()
    url = ("https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=whUSDHKD,day,"
           f"{(today - datetime.timedelta(days=70)).isoformat()},{today.isoformat()},50")
    d = json.loads(http_get(url))["data"]["whUSDHKD"]
    return [float(k[2]) for k in (d.get("day") or d.get("qfqday") or [])]


def macro_liquidity():
    """DXY/USDHKD 20日方向评分: 变化>±0.3%记升降, 下降(弱美元/强港元)=宽松=+1。"""
    def direction(closes):
        if len(closes) < 21:
            raise ValueError("历史不足21日")
        pct = (closes[-1] / closes[-21] - 1) * 100
        txt = "升" if pct > 0.3 else ("降" if pct < -0.3 else "平")
        score = -1 if pct > 0.3 else (1 if pct < -0.3 else 0)
        return txt, score, pct
    out = {"items": [], "score": 0}
    try:
        c = fetch_dxy_closes()
        t, s, p = direction(c)
        out["items"].append(f"DXY {c[-1]:.2f}(20日{p:+.1f}%{t}{s:+d})")
        out["score"] += s
    except Exception:
        out["items"].append("DXY获取失败")
    try:
        c = fetch_usdhkd_closes()
        t, s, p = direction(c)
        out["items"].append(f"USDHKD {c[-1]:.4f}(20日{p:+.2f}%{t}{s:+d})")
        out["score"] += s
    except Exception:
        out["items"].append("USDHKD获取失败")
    if not out["items"]:
        raise ValueError("宏观指标全部获取失败")
    return out


def load_macro_events(today):
    """静态事件(FOMC/财报)读 data/macro_events.json; 非农/CPI 程序生成。"""
    try:
        with open(os.path.join(DATA_DIR, "macro_events.json"), encoding="utf-8") as fh:
            events = json.load(fh).get("events", [])
    except (OSError, ValueError):
        events = []
    y, m = today.year, today.month
    for k in range(-2, 15):
        mm = (m - 1 + k) % 12 + 1
        yy = y + (m - 1 + k) // 12
        d = datetime.date(yy, mm, 1)
        while d.weekday() != 4:
            d += datetime.timedelta(days=1)
        events.append({"name": "非农", "date": d.isoformat(), "type": "jobs",
                       "note": "每月首个周五, 香港时间21:30(冬令)"})
        events.append({"name": "美国CPI", "date": datetime.date(yy, mm, 11).isoformat(),
                       "type": "cpi", "note": "每月11日记账, 实际日期为近似"})
    seen, out = set(), []
    for e in events:
        try:
            ed = datetime.date.fromisoformat(e["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if (e["name"], e["date"]) in seen:
            continue
        seen.add((e["name"], e["date"]))
        e["_d"] = ed
        out.append(e)
    return sorted(out, key=lambda e: e["_d"])


def macro_event_line(events, today):
    """未来2个事件倒计时; T-1 预警, 当日注记。"""
    warn = {"jobs": "明晚21:30非农公布", "cpi": "明晚21:30美国CPI(日期近似)",
            "fed": "FOMC决议(香港时间次日凌晨)", "earnings": "美团财报窗口(预估待公告校准)"}
    parts = []
    for e in [e for e in events if e["_d"] >= today][:2]:
        dd = (e["_d"] - today).days
        tag = "今日" if dd == 0 else ("明天" if dd == 1 else f"T-{dd}")
        s = f"{e['name']} {e['_d'].isoformat()[5:]}({tag})"
        if dd == 1:
            s += f" ⚠{warn.get(e['type'], '事件')}，事件前控仓口径"
        elif dd == 0:
            s += f" ⚠今日：{warn.get(e['type'], '事件').replace('明晚', '今晚')}，按控仓口径执行"
        parts.append(s)
    return "事件: " + " | ".join(parts) if parts else None


def fetch_southbound_top10(code=DEFAULT_CODE):
    """东财港股通十大成交(沪002+深004合并, RPT_MUTUAL_TOP10DEAL)。
    仅当日上榜有数据; 金额单位为人民币元; 通常交易日晚间更新。未上榜返回 None。"""
    found = []
    for mt in ("002", "004"):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               "reportName=RPT_MUTUAL_TOP10DEAL&columns=ALL&pageNumber=1&pageSize=10&"
               "sortColumns=TRADE_DATE,RANK&sortTypes=-1,1&source=WEB&client=WEB&"
               f"filter=(MUTUAL_TYPE%3D%22{mt}%22)")
        req = urllib.request.Request(url, headers={**UA, "Referer": "https://data.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        for x in (d.get("result") or {}).get("data") or []:
            if x.get("SECURITY_CODE") == code:
                found.append(x)
    if not found:
        return None
    return {"date": found[0]["TRADE_DATE"][:10],
            "net": sum(x.get("NET_BUY_AMT") or 0 for x in found),
            "buy": sum(x.get("BUY_AMT") or 0 for x in found),
            "sell": sum(x.get("SELL_AMT") or 0 for x in found),
            "ranks": {x["MUTUAL_TYPE"]: x["RANK"] for x in found},
            "close": found[0].get("CLOSE_PRICE")}


def save_south_top10(data_dir, top10):
    path = os.path.join(data_dir, "south_top10.csv")
    fields = ["date", "rank_h", "rank_s", "buy", "sell", "net", "close"]
    rec = {"date": top10["date"], "rank_h": top10["ranks"].get("002", ""),
           "rank_s": top10["ranks"].get("004", ""), "buy": f"{top10['buy'] / 1e8:.2f}",
           "sell": f"{top10['sell'] / 1e8:.2f}", "net": f"{top10['net'] / 1e8:+.2f}",
           "close": top10.get("close") or ""}
    rows = []
    if os.path.exists(path) and not os.path.islink(path):
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("date") != rec["date"]]
    rows.append(rec)
    rows.sort(key=lambda r: r["date"])

    def write(fh):
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    atomic_write(path, write, newline="")


def ccass(code=DEFAULT_CODE):
    """CCASS 托管周度跟踪(手动模式)。
    披露易 ToS 2.3 明确禁止程序化抓取(2026-11-09 技术验证 POST 可达但弃用)，
    故本命令输出手动查询指引; 若 data/ccass_<code>.csv 有手工录入的历史,
    自动算周度变动榜并标记变动超 0.3pp 总股本的参与者。"""
    code = normalize_hk_code(code)
    data_dir, _ = stock_dirs(code)
    print("== CCASS 托管仓位周度跟踪(手动模式) ==")
    print("披露易使用条款 2.3 禁止脚本化接入, 本工具不自动抓取。手动步骤:")
    print("  1. 打开 https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx")
    print("  2. 持股日期选最近周五, 股份代号输入", code)
    print(f"  3. 把前15大参与者+中国结算(港股通席位)追加到 data/ccass_{code}.csv, 列:")
    print("     date,participant,shares,pct  (pct=占已发行股份%)")
    path = os.path.join(data_dir, f"ccass_{code}.csv")
    if not os.path.exists(path):
        print(f"\n暂无历史({os.path.relpath(path, BASE)}), 录入两周后此处自动产出变动榜。")
        return 0
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    dates = sorted({r["date"] for r in rows})
    if len(dates) < 2:
        print("\n仅一周数据, 变动榜需至少两周。")
        return 0
    cur, prev = dates[-1], dates[-2]
    by = {}
    for r in rows:
        if r["date"] in (cur, prev):
            try:
                by.setdefault(r["participant"], {})[r["date"]] = float(r["pct"])
            except (KeyError, TypeError, ValueError):
                continue
    moves = []
    for name, d in by.items():
        if cur in d and prev in d:
            moves.append((name, d[prev], d[cur], d[cur] - d[prev]))
    moves.sort(key=lambda x: -abs(x[3]))
    print(f"\n托管变动榜 {prev} → {cur} (按|变动|排序, 单位pp):")
    for name, p0, p1, dp in moves[:15]:
        flag = " ⚠>0.3pp" if abs(dp) > 0.3 else ""
        print(f"  {name[:40]:<40} {p0:6.2f}% → {p1:6.2f}%  {dp:+.2f}{flag}")
    return 0


def ema_series(vals, n):
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def adx14(highs, lows, closes, n=14):
    trs, pdm, ndm = [], [], []
    for i in range(1, len(closes)):
        h, l, ph, pl, pc = highs[i], lows[i], highs[i - 1], lows[i - 1], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)

    def smooth(v):
        s = sum(v[:n])
        out = [s]
        for x in v[n:]:
            s = s - s / n + x
            out.append(s)
        return out
    dxs = []
    for a, p, nn in zip(smooth(trs), smooth(pdm), smooth(ndm)):
        pdi = 100 * p / a if a else 0.0
        ndi = 100 * nn / a if a else 0.0
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
    adx = sum(dxs[:n]) / n
    for x in dxs[n:]:
        adx = (adx * (n - 1) + x) / n
    return adx


def tech_indicators(data):
    closes = [d["close"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    t = {}
    for n in (5, 10, 20, 60):
        t[f"ma{n}"] = sum(closes[-n:]) / n
    ms = [t["ma5"], t["ma10"], t["ma20"], t["ma60"]]
    if all(ms[i] > ms[i + 1] for i in range(3)):
        t["align"] = "多头排列"
    elif all(ms[i] < ms[i + 1] for i in range(3)):
        t["align"] = "空头排列"
    elif all(abs(ms[i] - ms[i + 1]) / ms[i + 1] < 0.02 for i in range(3)):
        t["align"] = "纠缠粘合"
    else:
        t["align"] = "交叉过渡"
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema_series(dif, 9)
    t["dif"], t["dea"], t["bar"] = dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2
    if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
        t["macd_st"] = "金叉"
    elif dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
        t["macd_st"] = "死叉"
    else:
        t["macd_st"] = "DIF>DEA多头" if dif[-1] > dea[-1] else "DIF<DEA空头"
    t["macd_div"] = closes[-1] <= min(closes[-20:]) and dif[-1] > min(dif[-20:])
    t["rsi"] = rsi14(closes)
    win = closes[-20:]
    mid = sum(win) / 20
    sd = (sum((x - mid) ** 2 for x in win) / 20) ** 0.5
    up, lo_ = mid + 2 * sd, mid - 2 * sd
    t["bpct"] = (closes[-1] - lo_) / (up - lo_) if up > lo_ else 0.5
    t["bw"] = (up - lo_) / mid * 100

    def bw_at(i):
        w = closes[i - 19:i + 1]
        m_ = sum(w) / 20
        s_ = (sum((x - m_) ** 2 for x in w) / 20) ** 0.5
        return 4 * s_ / m_ * 100
    bws = [bw_at(i) for i in range(len(closes) - 60, len(closes))]
    t["squeeze"] = sum(1 for b in bws if b < t["bw"]) / len(bws) * 100 <= 10
    t["adx"] = adx14(highs, lows, closes)
    t["regime"] = "震荡市" if t["adx"] < 20 else ("趋势市" if t["adx"] > 25 else "过渡市")
    obv, o = [], 0.0
    for i in range(1, len(closes)):
        o += (1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0) * data[i]["vol"]
        obv.append(o)
    t["obv_slope"] = (obv[-1] - obv[-20]) / 20 if len(obv) >= 20 else 0.0
    t["obv_div"] = closes[-1] <= min(closes[-20:]) and obv[-1] > min(obv[-20:])
    return t


def rsi14(closes):
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[:14]) / 14
    al = sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        ag = (ag * 13 + gains[i]) / 14
        al = (al * 13 + losses[i]) / 14
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr14(data):
    ds = data[:-1][-15:]
    trs = [max(d["high"] - d["low"], abs(d["high"] - ds[i - 1]["close"]),
               abs(d["low"] - ds[i - 1]["close"])) for i, d in enumerate(ds) if i]
    return sum(trs) / len(trs) if trs else 0.0


def intraday(code=DEFAULT_CODE):
    code = normalize_hk_code(code)
    data_dir, _ = stock_dirs(code)
    with stock_lock(data_dir):
        if recover_daily(data_dir, stock_dirs(code)[1]):
            print("已恢复上次中断的日报事务。")
        return _intraday(code)


def _intraday(code):
    data_dir, report_dir = stock_dirs(code)
    levels_path = os.path.join(data_dir, "levels.json")
    levels = load_levels(levels_path)
    mins = fetch_minute(code)
    if not mins:
        print("无当日分钟数据（未开盘？）")
        return 1
    quote = fetch_quote(code)
    today = datetime.datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    if quote["time"].date() != today:
        raise ValueError("行情不是今日数据，暂停日内分析")
    benchmark = levels.get("benchmark", "hkHSTECH" if code == DEFAULT_CODE else "hkHSI")
    try:
        idx = fetch_index(benchmark)
        idx_ok = True
    except Exception:
        idx = {"price": 0.0, "pct": 0.0}
        idx_ok = False
    kdata = fetch_kline(today.isoformat(), code)
    validate_kline(kdata, minimum=21)
    if any(not math.isfinite(r["price"]) or r["price"] <= 0 or
           not math.isfinite(r["cumvol"]) or not math.isfinite(r["cumamt"]) or
           r["cumvol"] < 0 or r["cumamt"] < 0 for r in mins):
        raise ValueError("分钟行情包含无效价格或成交数据")
    if any(b["cumvol"] < a["cumvol"] or b["cumamt"] < a["cumamt"]
           for a, b in zip(mins, mins[1:])):
        raise ValueError("分钟行情累计成交数据递减，暂停分析")
    price = quote["price"] or mins[-1]["price"]
    hm = mins[-1]["hm"]

    vols, prices = [], []
    pc = 0.0
    for r in mins:
        vols.append(r["cumvol"] - pc)
        prices.append(r["price"])
        pc = r["cumvol"]
    tot_v, tot_a = mins[-1]["cumvol"], mins[-1]["cumamt"]
    if tot_v <= 0 or tot_a <= 0:
        raise ValueError("分钟行情成交量或成交额为零，无法计算日内指标")

    vwap = tot_a / tot_v
    sig = (sum(v * (p - vwap) ** 2 for v, p in zip(vols, prices)) / tot_v) ** 0.5
    dev = (price - vwap) / vwap * 100
    vsw = [r["cumamt"] / r["cumvol"] if r["cumvol"] else vwap for r in mins]
    k = 30 if len(vsw) > 30 else len(vsw) - 1
    slope = (vsw[-1] - vsw[-1 - k]) / vsw[-1 - k] * 100
    slope_txt = "上行" if slope > 0.05 else ("下行" if slope < -0.05 else "走平")

    ibw = intraday_bin_width(levels, price, code)
    b0 = int(min(prices) / ibw) * ibw
    nb = int((max(prices) - b0) / ibw) + 1
    prof = [0.0] * nb
    for v, p in zip(vols, prices):
        i = min(max(int((p - b0) / ibw), 0), nb - 1)
        prof[i] += v
    poc_i = max(range(nb), key=lambda i: prof[i])
    va, li, ri = prof[poc_i], poc_i, poc_i
    while va < 0.7 * tot_v and (li > 0 or ri < nb - 1):
        lv = prof[li - 1] if li > 0 else -1
        rv = prof[ri + 1] if ri < nb - 1 else -1
        if rv >= lv:
            ri += 1
            va += prof[ri]
        else:
            li -= 1
            va += prof[li]
    poc = b0 + (poc_i + 0.5) * ibw
    va_lo, va_hi = b0 + li * ibw, b0 + (ri + 1) * ibw

    def orb(end):
        win = [r for r in mins if r["hm"] <= end]
        return (max(r["price"] for r in win), min(r["price"] for r in win), win)
    if not any(r["hm"] <= "0945" for r in mins):
        raise ValueError("缺少开盘阶段分钟数据，无法计算 ORB")
    o15h, o15l, _ = orb("0945")
    o30h, o30l, w30 = orb("1000")
    or_avg = sum(vols[:len(w30)]) / len(w30)
    pos = "区间内"
    brk = None
    for i, r in enumerate(mins):
        if r["hm"] <= "1000":
            continue
        if r["price"] > o30h:
            brk = ("上破", i)
            break
        if r["price"] < o30l:
            brk = ("下破", i)
            break
    brk_txt = ""
    if brk:
        side, bi = brk
        v5 = sum(vols[bi:bi + 5])
        vol_ok = v5 > 1.5 * 5 * or_avg
        back_in = any(o30l <= r["price"] <= o30h for r in mins[bi:bi + 15])
        pos = f"已{side}({mins[bi]['hm']})" + ("放量" if vol_ok else "无量")
        if back_in:
            pos += "→回落区间内(假突破)"
        brk_txt = side if vol_ok and not back_in else ""
    elif price > o30h:
        pos = "上沿之上"
    elif price < o30l:
        pos = "下沿之下"

    avg20v = sum(d["vol"] for d in kdata[:-1][-20:]) / 20
    proj = tot_v / len(mins) * 330
    rvol = proj / avg20v if avg20v else 0.0
    v30 = sum(vols[:30])
    share30 = v30 / tot_v * 100

    bars = [mins[i:i + 5] for i in range(0, len(mins), 5)]
    bclose = [b[-1]["price"] for b in bars]
    rsi = rsi14(bclose)
    lb = bars[-1]
    lb_o, lb_c = lb[0]["price"], lb[-1]["price"]
    lb_h = max(r["price"] for r in lb)
    lb_l = min(r["price"] for r in lb)
    lb_v = sum(vols[len(mins) - len(lb):])
    avg5 = tot_v / len(mins) * 5
    lb_range = lb_h - lb_l
    up_shadow = (lb_h - max(lb_o, lb_c)) / lb_range if lb_range else 0
    dn_shadow = (min(lb_o, lb_c) - lb_l) / lb_range if lb_range else 0

    atr = atr14(kdata)
    day_hi, day_lo = max(prices), min(prices)
    amp = day_hi - day_lo
    atr_x = amp / atr if atr else 0

    excess = quote["pct"] - idx["pct"] if idx_ok else None

    # 分钟级主买/主卖(uptick 方向推断): 分钟收盘>前分钟=主动买入, < =主动卖出, = 对半分
    amts, pa_ = [], 0.0
    for r in mins:
        amts.append(r["cumamt"] - pa_)
        pa_ = r["cumamt"]
    buy_amt = sell_amt = 0.0
    net_series = [0.0] * len(mins)
    for i in range(1, len(mins)):
        a = amts[i]
        if prices[i] > prices[i - 1]:
            buy_amt += a
            net_series[i] = a
        elif prices[i] < prices[i - 1]:
            sell_amt += a
            net_series[i] = -a
        else:
            buy_amt += a / 2
            sell_amt += a / 2
    net_act = buy_amt - sell_amt
    buy_pct = buy_amt / (buy_amt + sell_amt) * 100 if buy_amt + sell_amt else 50.0
    k30 = min(30, len(mins) - 1)
    net30 = sum(net_series[-k30:]) if k30 else 0.0
    net_prev30 = sum(net_series[-2 * k30:-k30]) if len(mins) > 2 * k30 else None
    if net_prev30 is None:
        trend30 = ""
    elif net30 > 0 and net30 > abs(net_prev30):
        trend30 = "，较上前30分钟放大流入"
    elif net30 > 0:
        trend30 = "，流入收敛"
    elif net30 < 0 and abs(net30) > abs(net_prev30):
        trend30 = "，流出加速"
    else:
        trend30 = "，流出收敛"

    lv_lines = []
    for lv in levels["resistance"] + levels["support"]:
        p = lv["price"]
        s = f"{p}({(p - price) / price * 100:+.1f}%)"
        if day_lo <= p <= day_hi:
            ti = next((i for i in range(1, len(mins))
                       if (prices[i - 1] - p) * (prices[i] - p) <= 0), 0)
            vt = sum(vols[max(0, ti - 2):ti + 3])
            s += f" 已触及@{mins[ti]['hm']}(量能{vt / avg5:.1f}x)"
        lv_lines.append(s)

    sigs = []
    if hm < "1000":
        sigs.append("时间规则：9:30-10:00 不定方向，不重仓")
    if hm >= "1400" and sum(vols[-30:]) / 30 < 0.5 * tot_v / len(vols):
        sigs.append("时间规则：14:00后无量，今日大概率无行情")
    if atr_x > 1.2:
        sigs.append(f"空间规则：振幅已 {atr_x:.1f}xATR(>1.2)，追单性价比低，空间大概率走完")
    if abs(dev) > 2 * sig / vwap * 100:
        sigs.append(f"VWAP：偏离 {dev:+.2f}% 超2σ，不追，等回归")
    elif price > vwap and slope > 0.05:
        sigs.append("VWAP：价在VWAP上方且VWAP上行，日内偏强" if levels.get("auto_generated")
                    else "VWAP：价在VWAP上方且VWAP上行=日内强，回踩VWAP不破可持")
    elif price < vwap:
        sigs.append("VWAP：价在VWAP下方，日内偏弱" if levels.get("auto_generated")
                    else "VWAP：价在VWAP下方=日内偏弱，反弹至VWAP附近承压视为减仓窗")
    for lv in ([] if levels.get("auto_generated") else levels["resistance"]):
        if abs(price - lv["price"]) / price <= 0.005 and lb_v > 1.5 * avg5 \
                and (lb_c < lb_o or up_shadow > 0.6):
            sigs.append(f"做T减仓：{lv['price']} 放量滞涨(5min量{lb_v / avg5:.1f}x+"
                        f"{'收阴' if lb_c < lb_o else '长上影'})，单次≤1/3仓当日了结，止损1-1.5%")
    for lv in ([] if levels.get("auto_generated") else levels["support"]):
        if abs(price - lv["price"]) / price <= 0.005 and lb_v < 0.5 * avg5 \
                and (lb_c >= lb_o or dn_shadow > 0.5):
            sigs.append(f"做T接回：{lv['price']} 缩量企稳(5min量{lb_v / avg5:.1f}x+止跌)，"
                        f"单次≤1/3仓当日了结，止损1-1.5%")
    if brk_txt:
        sigs.append(f"ORB：10:00区间放量{brk_txt}；3根5min内回落区间内视为假突破")
    if excess is not None and abs(excess) > 1:
        sigs.append(f"背离：相对基准指数超额 {excess:+.2f}%，个股独立行情，注意个股消息")
    if buy_pct < 40 and quote["pct"] < 0:
        sigs.append(f"主动卖压主导：主买占比{buy_pct:.0f}%<40%且价跌(uptick方向推断)")
    elif buy_pct > 60 and quote["pct"] > 0:
        sigs.append(f"主动买压主导：主买占比{buy_pct:.0f}%>60%且价涨(uptick方向推断)")
    elif buy_pct < 40 and quote["pct"] > 0:
        sigs.append(f"价涨但主买占比仅{buy_pct:.0f}%：被动承接反弹，质量存疑(uptick方向推断)")
    if not sigs:
        sigs.append("无显著日内信号：观察为主。")

    if rsi is not None:
        rsi_t = f"{rsi:.0f}" + (" 超买>70" if rsi > 70 else (" 超卖<30" if rsi < 30 else ""))
    else:
        rsi_t = "样本不足"
    orb_note = f"15min {o15l:.2f}~{o15h:.2f} / 30min {o30l:.2f}~{o30h:.2f}  现价{pos}"
    L = []
    A = L.append
    A(f"# {quote['name']} 日内分析 {today.isoformat()} {hm[:2]}:{hm[2:]} [盘中]")
    A("")
    idx_txt = f"{idx['price']:.2f} ({idx['pct']:+.2f}%)" if idx_ok else "获取失败"
    excess_txt = f"{excess:+.2f}%" if excess is not None else "N/A"
    A(f"- 现价 {price:.2f} ({quote['pct']:+.2f}%)  基准指数 {idx_txt}  超额 {excess_txt}")
    A(f"- VWAP {vwap:.2f}  偏离 {dev:+.2f}%  σ {sig:.2f}  "
      f"±1σ {vwap - sig:.2f}~{vwap + sig:.2f}  ±2σ {vwap - 2 * sig:.2f}~{vwap + 2 * sig:.2f}"
      f"  斜率(30min): {slope_txt}")
    A(f"- 日内筹码({ibw:g}港元箱): POC {poc:.1f}({prof[poc_i] / tot_v * 100:.1f}%)  "
      f"70%价值区 {va_lo:.2f}~{va_hi:.2f}")
    A(f"- ORB: {orb_note}")
    A(f"- RVOL(投影口径): {rvol:.2f}  [已交易{len(mins)}分钟, 投影全日{proj / 1e4:.0f}万股"
      f" vs 20日均{avg20v / 1e4:.0f}万股]")
    A(f"- 首30分钟量占比 {share30:.1f}%(U型常态25-30%, 盘中天然偏高)")
    A(f"- 主动方向(uptick估算): 主买{buy_amt / 1e8:.2f}亿/主卖{sell_amt / 1e8:.2f}亿"
      f" 净{net_act / 1e8:+.2f}亿(主买占比{buy_pct:.0f}%) | 近30分钟净主动{net30 / 1e8:+.2f}亿{trend30}")
    A(f"  注: 分钟bar uptick 方向推断估算，非真实逐笔成交")
    A(f"- 5min RSI(14): {rsi_t}")
    A(f"- ATR(14日) {atr:.2f}  今日振幅 {amp:.2f}({atr_x:.2f}x)  "
      f"估算剩余空间 {max(atr - amp, 0):.2f}")
    A(f"- 关键位: " + "  ".join(lv_lines))
    A("")
    A("## 日内立场")
    for s in sigs:
        A(f"- {s}")
    if levels.get("auto_generated"):
        A("- 提示：关键位为自动生成草稿，尚未人工校准；仅观察结构变化，暂不作为交易触发。")
    report = "\n".join(L)
    print(report)

    if len(sigs) > 1 or "做T" in report or "ORB：10" in report or "背离" in report:
        path = os.path.join(data_dir, "intraday_log.csv")
        os.makedirs(data_dir, exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["date", "time", "price", "signals"])
            w.writerow([today.isoformat(), hm, f"{price:.2f}",
                        "; ".join(sigs)])
        print(f"--- 显著信号已追加 {os.path.relpath(path, BASE)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="港股筹码跟踪工具")
    parser.add_argument("command", choices=("init", "daily", "intraday", "ccass"))
    parser.add_argument("--code", "-c", default=DEFAULT_CODE,
                        help="港股代码，支持 700/0700/00700/700.HK，默认 03690")
    args = parser.parse_args()
    try:
        code = normalize_hk_code(args.code)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if args.command == "init":
            sys.exit(init_stock(code))
        if args.command == "daily":
            sys.exit(daily(code))
        if args.command == "ccass":
            sys.exit(ccass(code))
        sys.exit(intraday(code))
    except (ValueError, OSError, KeyError, IndexError, urllib.error.URLError) as exc:
        print(f"分析失败：{exc}", file=sys.stderr)
        sys.exit(1)
