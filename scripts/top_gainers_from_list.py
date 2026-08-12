#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
top_gainers_from_list.py

从 ALL_STOCKS（用户提供的全量自选股，逗号分隔）中，按当日「涨跌幅」取前 TOP_N 只，
写入 STOCK_LIST 环境变量（直接导出，供后续 main.py 使用）。

设计目标：
- 每个交易日动态选出「当日涨幅前 TOP_N」进行分析与推送。
- 行情源（akshare 东方财富全市场快照）失败/被限流时，自动回退到 ALL_STOCKS 的前 TOP_N 只，
  保证每日运行不会因数据源抖动而整跑失败。

用法（被 workflow 调用）：
    python scripts/top_gainers_from_list.py
脚本会把选出的代码 export 成 STOCK_LIST，并额外打印一份 JSON 到 stdout（带 __STOCK_LIST__ 前缀），
便于 workflow 用 `echo "$OUTPUT" | ...` 或 GITHUB_OUTPUT 捕获。
"""
from __future__ import annotations

import os
import sys
import json

TOP_N = int(os.getenv("TOP_N", "20"))


def normalize(code: str) -> str:
    """剥掉 SH/SZ/BJ/SS 等前缀，返回 6 位纯数字（A股/ETF/BJ）。"""
    c = code.strip().upper()
    for p in ("SH.", "SZ.", "SS.", "BJ.", "SH", "SZ", "SS", "BJ"):
        if c.startswith(p) and len(c) > len(p):
            c = c[len(p):]
            break
    return c


def resolve_all_stocks() -> list[str]:
    raw = os.getenv("ALL_STOCKS", "") or os.getenv("STOCK_LIST", "")
    if not raw.strip():
        return []
    return [normalize(x) for x in raw.split(",") if x.strip()]


def pick_static_front(all_stocks: list[str], n: int) -> list[str]:
    return all_stocks[:n]


def pick_top_gainers(all_stocks: list[str], n: int) -> tuple[list[str], str]:
    """尝试用 akshare 全市场快照按涨跌幅取前 n 只。失败则返回 (静态前 n, 原因)。"""
    try:
        import akshare as ak
    except Exception as e:  # noqa: BLE001
        return pick_static_front(all_stocks, n), f"akshare import failed: {e}"

    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:  # noqa: BLE001
        return pick_static_front(all_stocks, n), f"snapshot fetch failed: {e}"

    if df is None or "代码" not in df.columns or "涨跌幅" not in df.columns:
        return pick_static_front(all_stocks, n), "snapshot missing columns"

    want = set(all_stocks)
    # 快照代码可能带前缀或不带，统一归一化比较
    snap = {}
    for _, row in df.iterrows():
        code = normalize(str(row.get("代码", "")))
        try:
            chg = float(row.get("涨跌幅"))
        except (TypeError, ValueError):
            chg = float("-inf")
        snap[code] = chg

    matched = [(c, snap[c]) for c in all_stocks if c in snap]
    if not matched:
        return pick_static_front(all_stocks, n), "no overlap with snapshot"

    matched.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in matched[:n]]
    reasons = ", ".join(f"{c}:{chg:+.2f}%" for c, chg in matched[:n])
    return top, f"top gainers from snapshot: {reasons}"


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
    # 导出供后续步骤使用
    print(f"__STOCK_LIST__={joined}")
    print(f"SELECTED_REASON={reason}")
    # 同时写 GITHUB_OUTPUT（若存在）
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"stock_list={joined}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
