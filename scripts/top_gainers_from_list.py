#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
top_gainers_from_list.py

从 ALL_STOCKS（用户提供的全量自选股，逗号分隔）中，剔除当日「涨停」个股后，
按当日「涨跌幅」取前 TOP_N 只（强趋势但未封板），写入 STOCK_LIST 供后续分析。

设计目标：
- 每个交易日动态选出「当日涨幅前 TOP_N（剔除涨停）」进行分析与推送。
- 多数据源 + 重试，尽量在 GitHub Runner 上也能成功取到当日涨幅与涨停价：
  1) 东方财富全市场快照（akshare stock_zh_a_spot_em）——覆盖全 A，但 GitHub IP 常被限流；
  2) 腾讯行情 qt.gtimg.cn 单只批量查询（与项目 REALTIME_SOURCE_PRIORITY 首选一致）——
     只需你提供的 146 只代码即可，天然规避全市场快照的限流，且直接给出涨停价；
  3) 新浪行情 hq.sinajs.cn 单只批量查询——备用（涨停价需按板块推算）。
- 上述全部失败时才回退到 ALL_STOCKS 的前 TOP_N 只（静态），保证每日运行不整跑失败。
"""
from __future__ import annotations

import os
import sys
import time
import types
import urllib.request
import urllib.parse

TOP_N = int(os.getenv("TOP_N", "10"))
SNAPSHOT_MAX_RETRY = int(os.getenv("SNAPSHOT_MAX_RETRY", "3"))
SNAPSHOT_RETRY_BACKOFF = float(os.getenv("SNAPSHOT_RETRY_BACKOFF", "2.0"))


def normalize(code: str) -> str:
    """剥掉 SH/SZ/BJ/SS 等前缀，返回 6 位纯数字（A股/ETF/BJ）。"""
    c = code.strip().upper()
    for p in ("SH.", "SZ.", "SS.", "BJ.", "SH", "SZ", "SS", "BJ"):
        if c.startswith(p) and len(c) > len(p):
            c = c[len(p):]
            break
    return c


def to_tencent_symbol(code: str) -> str:
    base = normalize(code)
    if base.startswith(("6", "5", "9")):
        return f"sh{base}"
    return f"sz{base}"


def to_sina_symbol(code: str) -> str:
    return to_tencent_symbol(code)


def _limit_up_ratio(code: str, name: str = "") -> float:
    """按板块返回涨停幅度（ST=5%，双创/创业板=20%，其余=10%）。"""
    n = (name or "").upper()
    if n.startswith("*ST") or n.startswith("ST"):
        return 0.05
    if code.startswith(("688", "689", "300", "301", "302")):
        return 0.20
    return 0.10


def resolve_all_stocks() -> list[str]:
    raw = os.getenv("ALL_STOCKS", "") or os.getenv("STOCK_LIST", "")
    if not raw.strip():
        return []
    return [normalize(x) for x in raw.split(",") if x.strip()]


def is_individual_stock(code: str) -> bool:
    """判断是否为个股（排除 ETF/基金/指数）。

    A股个股代码规律：
      - 沪市主板/科创板：60xxxx、688xxx、689xxx
      - 深市主板/创业板：00xxxx、30xxx（300/301/302 创业板）
      - 北交所：8xxxxx、43xxxx、92xxxx（920xxx）
    基金/ETF 一律以 5 或 1 开头（如 517180、159516、510300），指数如 000688。
    """
    if not code or not code.isdigit() or len(code) != 6:
        return False
    if code.startswith(("5", "1")):  # 基金/ETF
        return False
    if code in ("000688",):  # 科创50 等指数
        return False
    if code.startswith("000") and code != "000688":
        # 000xxx 多为深市主板个股（如 000063 中兴通讯），保留
        return True
    return code[:2] in ("60", "68", "69", "00", "30", "43", "92", "88", "83", "87")


def pick_static_front(all_stocks: list[str], n: int) -> list[str]:
    tradeable = [c for c in all_stocks if is_individual_stock(c)]
    return (tradeable or all_stocks)[:n]


def _is_limit_up(price: float, up_price: float, preclose: float, code: str, name: str = "") -> bool:
    """判定是否涨停：优先用真实涨停价比较；否则按板块幅度推算。"""
    if price <= 0:
        return False
    if up_price > 0:
        return price >= up_price - 0.02
    if preclose > 0:
        return price >= preclose * (1.0 + _limit_up_ratio(code, name)) - 0.02
    return False


def _snapshot_via_eastmoney() -> dict[str, dict] | None:
    """东方财富全市场快照。返回 {code: {"chg":.., "lu":..}}；失败返回 None。"""
    try:
        import akshare as ak
    except Exception as e:  # noqa: BLE001
        print(f"[snapshot] akshare unavailable: {e}", file=sys.stderr)
        return None
    last_err = None
    for attempt in range(1, SNAPSHOT_MAX_RETRY + 1):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or "代码" not in df.columns or "涨跌幅" not in df.columns:
                last_err = "snapshot missing columns"
                break
            out: dict[str, dict] = {}
            has_up = "涨停价" in df.columns
            has_cur = "最新价" in df.columns
            has_pre = "昨收" in df.columns
            for _, row in df.iterrows():
                code = normalize(str(row.get("代码", "")))
                try:
                    chg = float(row.get("涨跌幅"))
                except (TypeError, ValueError):
                    chg = float("-inf")
                price = float(row.get("最新价", 0)) if has_cur else 0.0
                up_price = float(row.get("涨停价", 0)) if has_up else 0.0
                preclose = float(row.get("昨收", 0)) if has_pre else 0.0
                lu = _is_limit_up(price, up_price, preclose, code)
                out[code] = {"chg": chg, "lu": lu}
            return out
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            print(f"[snapshot] attempt {attempt}/{SNAPSHOT_MAX_RETRY} failed: {last_err}", file=sys.stderr)
            if attempt < SNAPSHOT_MAX_RETRY:
                time.sleep(SNAPSHOT_RETRY_BACKOFF * attempt)
    return None


def _fetch_tencent(codes: list[str]) -> dict[str, dict] | None:
    """腾讯行情 qt.gtimg.cn 批量查询。返回 {code: {"chg":.., "lu":..}}；失败返回 None。"""
    if not codes:
        return None
    syms = [to_tencent_symbol(c) for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(syms)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("gbk", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"[tencent] fetch failed: {e}", file=sys.stderr)
        return None
    out: dict[str, dict] = {}
    # 每行: v_sh600519="名称~代码~今开~现价~昨收~...~涨跌幅~涨停价~跌停价"; 字段以 ~ 分隔
    # 关键索引: 1=名称, 3=现价, 4=昨收, 32=涨跌幅(%), 33=涨停价
    for line in raw.split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        sym = line.split("=")[0].replace("v_", "").strip()
        payload = line.split("=", 1)[1].strip().strip('"')
        if not payload:
            continue
        fields = payload.split("~")
        try:
            chg = float(fields[32])
        except (IndexError, ValueError):
            chg = float("-inf")
        try:
            price = float(fields[3])
            preclose = float(fields[4])
        except (IndexError, ValueError):
            price = preclose = 0.0
        try:
            up_price = float(fields[33])
        except (IndexError, ValueError):
            up_price = 0.0
        name = fields[1] if len(fields) > 1 else ""
        base = normalize(sym)
        lu = _is_limit_up(price, up_price, preclose, base, name)
        out[base] = {"chg": chg, "lu": lu}
    return out if out else None


def _fetch_sina(codes: list[str]) -> dict[str, dict] | None:
    """新浪行情 hq.sinajs.cn 批量查询。返回 {code: {"chg":.., "lu":..}}；失败返回 None。"""
    if not codes:
        return None
    syms = [to_sina_symbol(c) for c in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(syms)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("gbk", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"[sina] fetch failed: {e}", file=sys.stderr)
        return None
    out: dict[str, dict] = {}
    # 每行: var hq_str_sh600519="名称,今开,昨收,现价,...,涨跌,涨跌幅,...";
    # 索引: 0=名称, 2=昨收, 3=现价, 13=涨跌, 14=涨跌幅(%)
    for line in raw.splitlines():
        if "=" not in line:
            continue
        sym = line.split("=")[0].replace("var hq_str_", "").strip()
        payload = line.split("=", 1)[1].strip().strip('"').strip(";")
        if not payload:
            continue
        fields = payload.split(",")
        try:
            preclose = float(fields[2])
            price = float(fields[3])
            chg = (price - preclose) / preclose * 100.0 if preclose else 0.0
        except (IndexError, ValueError, ZeroDivisionError):
            price = preclose = chg = 0.0
        name = fields[0] if fields else ""
        base = normalize(sym)
        lu = _is_limit_up(price, 0.0, preclose, base, name)
        out[base] = {"chg": chg, "lu": lu}
    return out if out else None


def build_change_map(all_stocks: list[str]) -> tuple[dict[str, dict], str]:
    """依次尝试各数据源，返回 (code->{"chg","lu"}, 来源描述)。全失败返回 ({}, 原因)。"""
    m = _snapshot_via_eastmoney()
    if m:
        return m, "eastmoney snapshot"

    m = _fetch_tencent(all_stocks)
    if m:
        return m, "tencent batch"

    m = _fetch_sina(all_stocks)
    if m:
        return m, "sina batch"

    return {}, "all realtime sources failed"


def pick_top_gainers(all_stocks: list[str], n: int) -> tuple[list[str], str]:
    change_map, src = build_change_map(all_stocks)
    if not change_map:
        return pick_static_front(all_stocks, n), "fallback to static front (no realtime data)"

    # 仅考虑个股（排除 ETF/基金/指数），剔除涨停，再按涨跌幅倒序取前 n
    matched = [(c, change_map[c]["chg"]) for c in all_stocks
               if c in change_map and is_individual_stock(c) and not change_map[c]["lu"]]
    if not matched:
        return pick_static_front(all_stocks, n), "fallback to static front (all limit-up/non-stock)"
    matched.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in matched[:n]]
    reasons = ", ".join(f"{c}:{chg:+.2f}%" for c, chg in matched[:n])
    return top, f"top gainers (excl. limit-up) via {src}: {reasons}"


def main() -> int:
    all_stocks = resolve_all_stocks()
    if not all_stocks:
        print("__STOCK_LIST__=", file=sys.stderr)
        print("ERROR: ALL_STOCKS empty", file=sys.stderr)
        return 1

    chosen, reason = pick_top_gainers(all_stocks, TOP_N)
    if not chosen:
        chosen = pick_static_front(all_stocks, TOP_N)
        reason = "fallback to static front (empty pick)"

    joined = ",".join(chosen)
    print(f"__STOCK_LIST__={joined}")
    print(f"SELECTED_REASON={reason}")
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"stock_list={joined}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
