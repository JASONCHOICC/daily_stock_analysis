#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
top_gainers_from_list.py

从 ALL_STOCKS（用户提供的全量自选股，逗号分隔）中，按当日「涨跌幅」取前 TOP_N 只，
写入 STOCK_LIST 环境变量（直接导出，供后续 main.py 使用）。

设计目标：
- 每个交易日动态选出「当日涨幅前 TOP_N」进行分析与推送。
- 多数据源 + 重试，尽量在 GitHub Runner 上也能成功取到当日涨幅：
  1) 东方财富全市场快照（akshare stock_zh_a_spot_em）——覆盖全 A，但 GitHub IP 常被限流；
  2) 腾讯行情 qt.gtimg.cn 单只批量查询（与项目 REALTIME_SOURCE_PRIORITY 首选一致）——
     只需你提供的 146 只代码即可，天然规避全市场快照的限流；
  3) 新浪行情 hq.sinajs.cn 单只批量查询——备用。
- 上述全部失败时才回退到 ALL_STOCKS 的前 TOP_N 只（静态），保证每日运行不整跑失败。
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
import urllib.parse

TOP_N = int(os.getenv("TOP_N", "20"))
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


def resolve_all_stocks() -> list[str]:
    raw = os.getenv("ALL_STOCKS", "") or os.getenv("STOCK_LIST", "")
    if not raw.strip():
        return []
    return [normalize(x) for x in raw.split(",") if x.strip()]


def pick_static_front(all_stocks: list[str], n: int) -> list[str]:
    return all_stocks[:n]


def _snapshot_via_eastmoney() -> dict[str, float] | None:
    """东方财富全市场快照。返回 {code: change_pct}；失败返回 None。"""
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
            out: dict[str, float] = {}
            for _, row in df.iterrows():
                code = normalize(str(row.get("代码", "")))
                try:
                    chg = float(row.get("涨跌幅"))
                except (TypeError, ValueError):
                    chg = float("-inf")
                out[code] = chg
            return out
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            print(f"[snapshot] attempt {attempt}/{SNAPSHOT_MAX_RETRY} failed: {last_err}", file=sys.stderr)
            if attempt < SNAPSHOT_MAX_RETRY:
                time.sleep(SNAPSHOT_RETRY_BACKOFF * attempt)
    return None


def _fetch_tencent(codes: list[str]) -> dict[str, float] | None:
    """腾讯行情 qt.gtimg.cn 批量查询。返回 {code: change_pct}；失败返回 None。"""
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
    out: dict[str, float] = {}
    # 每行: v_sh600519="..."; 字段以 ~ 分隔，第 32 位是 涨跌幅(%)
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
        base = normalize(sym)
        out[base] = chg
    return out if out else None


def _fetch_sina(codes: list[str]) -> dict[str, float] | None:
    """新浪行情 hq.sinajs.cn 批量查询。返回 {code: change_pct}；失败返回 None。"""
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
    out: dict[str, float] = {}
    # 每行: var hq_str_sh600519="名称,今开,昨收,...,涨跌,涨跌幅,...";
    # 注意新浪的「涨跌幅」字段是按昨收的百分比，需结合 涨跌 与 昨收 计算更稳妥：
    for line in raw.splitlines():
        if "=" not in line:
            continue
        sym = line.split("=")[0].replace("var hq_str_", "").strip()
        payload = line.split("=", 1)[1].strip().strip('"').strip(";")
        if not payload:
            continue
        fields = payload.split(",")
        try:
            pre_close = float(fields[2])
            change = float(fields[3])
            chg = (change / pre_close * 100.0) if pre_close else 0.0
        except (IndexError, ValueError, ZeroDivisionError):
            chg = float("-inf")
        out[normalize(sym)] = chg
    return out if out else None


def build_change_map(all_stocks: list[str]) -> tuple[dict[str, float], str]:
    """依次尝试各数据源，返回 (code->change_pct, 来源描述)。全失败返回 ({}, 原因)。"""
    # 1) 东方财富全市场快照
    m = _snapshot_via_eastmoney()
    if m:
        return m, "eastmoney snapshot"

    # 2) 腾讯单只批量（只需 146 只代码，天然避限流）
    m = _fetch_tencent(all_stocks)
    if m:
        return m, "tencent batch"

    # 3) 新浪单只批量
    m = _fetch_sina(all_stocks)
    if m:
        return m, "sina batch"

    return {}, "all realtime sources failed"


def pick_top_gainers(all_stocks: list[str], n: int) -> tuple[list[str], str]:
    change_map, src = build_change_map(all_stocks)
    if not change_map:
        return pick_static_front(all_stocks, n), "fallback to static front (no realtime data)"

    matched = [(c, change_map.get(c, float("-inf"))) for c in all_stocks if c in change_map]
    if not matched:
        return pick_static_front(all_stocks, n), "fallback to static front (no overlap)"

    matched.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in matched[:n]]
    reasons = ", ".join(f"{c}:{chg:+.2f}%" for c, chg in matched[:n])
    return top, f"top gainers via {src}: {reasons}"


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
