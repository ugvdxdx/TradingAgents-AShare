#!/usr/bin/env python3
"""
debate_picker_v5 滚动回测

对每个历史窗口，跑 V3 基线 (纯 sector_score 排序) 和 v5 LangGraph 辩论，
对比两者 T+N 真实收益，量化辩论价值增量。

复用 v4 回测的窗口生成 / 数据截断 / 收益验证逻辑，
将辩论部分替换为 PickerGraph(cutoff_date=...)。

用法:
  uv run python3 _v5_rolling_backtest.py --windows 5
  uv run python3 _v5_rolling_backtest.py --windows 5 --dry-run
  uv run python3 _v5_rolling_backtest.py --rounds 2
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(override=True)

from tradingagents.agents.picker import data_io
from tradingagents.agents.picker.picker_graph import PickerGraph

CACHE_DIR = os.path.join(ROOT, "kline_cache")


# ══════════════════════════════════════════════════════════
# 窗口生成 + 收益验证 (复用 v4 思路)
# ══════════════════════════════════════════════════════════

def _get_windows():
    """从 K 线缓存生成 (cutoff_date, future_days) 窗口列表。"""
    sample = None
    for f in os.listdir(CACHE_DIR):
        if f.endswith(".pkl"):
            sample = f.replace(".pkl", "").rsplit("_", 1)[0]
            break
    if not sample:
        return []
    df = data_io._read_kline_raw(sample)
    if df is None:
        return []
    dates = sorted(df["trade_date"].unique())
    windows = []
    for i in range(20, len(dates) - 10, 5):
        windows.append((str(dates[i]), len(dates) - i - 1))
    return windows


def _validate(codes, cutoff_date, n_days):
    """给定股票代码列表，计算截止日之后 n_days 的持仓收益。"""
    results = []
    for code in codes:
        df_full = data_io._read_kline_raw(code)
        if df_full is None:
            continue
        idx = df_full[df_full["trade_date"] <= cutoff_date].index
        if len(idx) == 0:
            continue
        pos = idx[-1]
        future = df_full.iloc[pos + 1:pos + 1 + n_days]
        if len(future) < 10:
            continue
        ret = (future.iloc[-1]["close"] - df_full.iloc[pos]["close"]) / df_full.iloc[pos]["close"] * 100
        results.append(round(ret, 2))
    return results


def _stats(rets):
    if not rets:
        return 0.0, 0.0
    return round(np.mean(rets), 2), round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1)


# ══════════════════════════════════════════════════════════
# 单窗口
# ══════════════════════════════════════════════════════════

def run_window(graph, cutoff_date, future_days, top_n, dry_run):
    t0 = time.time()
    # V3 基线: 直接按 sector_score 取 Top10
    pool = data_io.load_top_n(top_n)
    v3_top10 = [s["code"] for s in pool[:10]]

    # v5 辩论
    state = graph.run(cutoff_date=cutoff_date, dry_run=dry_run)
    ranking = state.get("final_ranking", [])
    db_top10 = [r["code"] for r in ranking[:10]]

    v3_t5_avg, v3_t5_pos = _stats(_validate(v3_top10[:5], cutoff_date, future_days))
    v3_t10_avg, v3_t10_pos = _stats(_validate(v3_top10, cutoff_date, future_days))
    db_t5_avg, db_t5_pos = _stats(_validate(db_top10[:5], cutoff_date, future_days))
    db_t10_avg, db_t10_pos = _stats(_validate(db_top10, cutoff_date, future_days))

    elapsed = time.time() - t0
    print(f"\n  [窗口 {cutoff_date}] V3-T5 {v3_t5_avg:+.2f}% | v5-T5 {db_t5_avg:+.2f}% "
          f"(Δ{db_t5_avg - v3_t5_avg:+.2f}%) | v5-T10 {db_t10_avg:+.2f}% [{elapsed:.0f}s]")

    return {
        "cutoff_date": cutoff_date, "future_days": future_days,
        "v3_top5_avg": v3_t5_avg, "v3_top10_avg": v3_t10_avg,
        "db_top5_avg": db_t5_avg, "db_top10_avg": db_t10_avg,
        "v3_top10_pos": v3_t10_pos, "db_top10_pos": db_t10_pos,
        "v3_top10_codes": v3_top10, "db_top10_codes": db_top10,
        "elapsed_s": round(elapsed, 0),
    }


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="debate_picker_v5 滚动回测")
    ap.add_argument("--windows", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start-from", type=str, default="")
    args = ap.parse_args()

    print(f"{'='*70}")
    print(f"  debate_picker_v5 滚动回测 — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  对比: V3基线 vs v5辩论 (LangGraph) | 模式: {'dry-run' if args.dry_run else '完整辩论'}")
    print(f"{'='*70}")

    windows = _get_windows()
    if not windows:
        print("  ⚠️ K 线缓存不足")
        return
    if args.windows:
        windows = windows[-args.windows:]
    if args.start_from:
        windows = [(d, f) for d, f in windows if d >= args.start_from]
    print(f"  窗口: {len(windows)} ({windows[0][0]} ~ {windows[-1][0]})")

    graph = PickerGraph(max_debate_rounds=args.rounds, top_n=args.top_n)
    results = []
    for wi, (cutoff_date, future_days) in enumerate(windows):
        print(f"\n{'─'*60}\n  窗口 {wi+1}/{len(windows)} 截止日={cutoff_date} 验证={future_days}交易日")
        results.append(run_window(graph, cutoff_date, future_days, args.top_n, args.dry_run))

    # 汇总
    def agg(key):
        xs = [r[key] for r in results]
        return round(np.mean(xs), 2) if xs else 0.0

    print(f"\n{'='*70}\n  滚动回测汇总 — V3基线 vs v5辩论\n{'='*70}")
    print(f"  V3基线  Top5 均值: {agg('v3_top5_avg'):+.2f}%  Top10: {agg('v3_top10_avg'):+.2f}%")
    print(f"  v5辩论  Top5 均值: {agg('db_top5_avg'):+.2f}%  Top10: {agg('db_top10_avg'):+.2f}%")
    delta5 = agg("db_top5_avg") - agg("v3_top5_avg")
    delta10 = agg("db_top10_avg") - agg("v3_top10_avg")
    print(f"  辩论增量 Δ:       Top5 {delta5:+.2f}%  Top10 {delta10:+.2f}%  "
          f"{'✅ 有效' if delta5 > 0 else '❌ 负贡献'}")

    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": "dry-run" if args.dry_run else "full-debate",
        "windows": len(results),
        "v3_top5_avg": agg("v3_top5_avg"), "db_top5_avg": agg("db_top5_avg"),
        "v3_top10_avg": agg("v3_top10_avg"), "db_top10_avg": agg("db_top10_avg"),
        "debate_delta_top5": round(delta5, 2), "debate_delta_top10": round(delta10, 2),
        "results": results,
    }
    fname = f"backtest_v5_{datetime.now():%Y%m%d_%H%M}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n💾 {fname}")


if __name__ == "__main__":
    main()
