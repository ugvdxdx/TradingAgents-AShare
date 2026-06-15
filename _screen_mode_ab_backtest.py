#!/usr/bin/env python3
"""
保送逻辑 A/B/C 对照回测

针对"是否保留 V3 Top 保送"这一问题, 对每个历史窗口分别跑三种海选模式,
量化对比决赛名单差异、黑马命中率与 T+N 真实收益, 用数据支撑去留决策。

三种模式 (screen_mode):
  A "promote" (现状): V3 Top-debate_top_k 直接进决赛 (黑马仅参考, 不占名额)
  B "llm"     : 50 只全部经 LLM 海选(带先验+增量信息), 取 Top-debate_top_k
  C "hybrid"  : V3 Top-force_k 保送 + 剩余经 LLM 海选竞争, 合并 debate_top_k 只

评估指标:
  - 收益: 各模式 Top5/Top10 的 T+N 平均收益 + 相对 V3 基线的 Δ
  - 名单重合度: B/C 相对 A 的 Top-K Jaccard 相似度 (衡量海选是否真的换了选股)
  - 黑马命中率: B/C 新纳入(非 A 名单)的股票, 其实际收益是否跑赢被替换掉的股票

用法:
  uv run python3 _screen_mode_ab_backtest.py --windows 5
  uv run python3 _screen_mode_ab_backtest.py --windows 5 --modes promote llm hybrid
  uv run python3 _screen_mode_ab_backtest.py --windows 3 --rounds 2 --force-k 6
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
# 窗口生成 + 收益验证 (复用 _v5_rolling_backtest 思路)
# ══════════════════════════════════════════════════════════

def _get_windows():
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


def _ret_of(code, cutoff_date, n_days):
    """单只股 cutoff 后 n_days 的收益(%); 数据不足返回 None。"""
    df_full = data_io._read_kline_raw(code)
    if df_full is None:
        return None
    idx = df_full[df_full["trade_date"] <= cutoff_date].index
    if len(idx) == 0:
        return None
    pos = idx[-1]
    future = df_full.iloc[pos + 1:pos + 1 + n_days]
    if len(future) < 10:
        return None
    return round((future.iloc[-1]["close"] - df_full.iloc[pos]["close"])
                 / df_full.iloc[pos]["close"] * 100, 2)


def _validate(codes, cutoff_date, n_days):
    out = []
    for code in codes:
        r = _ret_of(code, cutoff_date, n_days)
        if r is not None:
            out.append(r)
    return out


def _avg(rets):
    return round(np.mean(rets), 2) if rets else 0.0


def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return round(len(sa & sb) / len(sa | sb), 3)


# ══════════════════════════════════════════════════════════
# 单窗口: 跑全部模式
# ══════════════════════════════════════════════════════════

def run_window(cutoff_date, future_days, modes, args):
    # V3 基线
    pool = data_io.load_top_n(args.top_n)
    v3_top10 = [s["code"] for s in pool[:10]]
    v3_t5 = _avg(_validate(v3_top10[:5], cutoff_date, future_days))
    v3_t10 = _avg(_validate(v3_top10, cutoff_date, future_days))

    mode_out = {}
    for mode in modes:
        t0 = time.time()
        graph = PickerGraph(max_debate_rounds=args.rounds, top_n=args.top_n,
                            screen_mode=mode, debate_top_k=args.top_k,
                            force_k=args.force_k)
        state = graph.run(cutoff_date=cutoff_date, dry_run=args.dry_run)
        ranking = state.get("final_ranking", [])
        codes = [r["code"] for r in ranking[:10]]
        t5 = _avg(_validate(codes[:5], cutoff_date, future_days))
        t10 = _avg(_validate(codes, cutoff_date, future_days))
        elapsed = time.time() - t0
        mode_out[mode] = {
            "codes": codes, "t5_avg": t5, "t10_avg": t10,
            "elapsed_s": round(elapsed, 0),
        }
        print(f"    [{mode:8s}] T5 {t5:+.2f}% | T10 {t10:+.2f}% "
              f"(vs V3-T5 Δ{t5 - v3_t5:+.2f}%) [{elapsed:.0f}s]")

    # 重合度 & 黑马命中 (以 promote 为基准)
    diff = {}
    base = mode_out.get("promote", {}).get("codes", [])
    for mode in modes:
        if mode == "promote" or not base:
            continue
        codes = mode_out[mode]["codes"]
        jac = _jaccard(base[:args.top_k], codes[:args.top_k])
        added = [c for c in codes if c not in base]      # 新纳入
        dropped = [c for c in base if c not in codes]    # 被替换掉
        added_ret = _avg(_validate(added, cutoff_date, future_days))
        dropped_ret = _avg(_validate(dropped, cutoff_date, future_days))
        diff[mode] = {
            "jaccard": jac, "added": added, "dropped": dropped,
            "added_ret": added_ret, "dropped_ret": dropped_ret,
            "darkhorse_edge": round(added_ret - dropped_ret, 2),  # >0 = 换入的更强
        }
        print(f"    [{mode:8s} vs promote] Jaccard {jac} | "
              f"换入{added}({added_ret:+.2f}%) 换出{dropped}({dropped_ret:+.2f}%) "
              f"黑马优势Δ{added_ret - dropped_ret:+.2f}%")

    return {
        "cutoff_date": cutoff_date, "future_days": future_days,
        "v3_top5_avg": v3_t5, "v3_top10_avg": v3_t10, "v3_top10_codes": v3_top10,
        "modes": mode_out, "diff_vs_promote": diff,
    }


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="保送逻辑 A/B/C 对照回测")
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--top-k", type=int, default=10, help="决赛名额 debate_top_k")
    ap.add_argument("--force-k", type=int, default=6, help="hybrid 模式保送名额")
    ap.add_argument("--modes", nargs="+", default=["promote", "llm", "hybrid"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start-from", type=str, default="")
    args = ap.parse_args()

    print(f"{'='*70}")
    print(f"  保送逻辑 A/B/C 对照回测 — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  模式: {args.modes} | 决赛名额={args.top_k} force_k={args.force_k} "
          f"| {'dry-run' if args.dry_run else '完整辩论'}")
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

    results = []
    for wi, (cutoff_date, future_days) in enumerate(windows):
        print(f"\n{'─'*60}\n  窗口 {wi+1}/{len(windows)} 截止日={cutoff_date} "
              f"验证={future_days}交易日")
        results.append(run_window(cutoff_date, future_days, args.modes, args))

    # ══ 汇总 ══
    print(f"\n{'='*70}\n  汇总 — 各模式跨窗口平均\n{'='*70}")
    v3_t5 = _avg([r["v3_top5_avg"] for r in results])
    v3_t10 = _avg([r["v3_top10_avg"] for r in results])
    print(f"  V3基线        T5 {v3_t5:+.2f}%  T10 {v3_t10:+.2f}%")

    summary = {"v3_top5_avg": v3_t5, "v3_top10_avg": v3_t10, "modes": {}}
    for mode in args.modes:
        t5 = _avg([r["modes"][mode]["t5_avg"] for r in results if mode in r["modes"]])
        t10 = _avg([r["modes"][mode]["t10_avg"] for r in results if mode in r["modes"]])
        summary["modes"][mode] = {"t5_avg": t5, "t10_avg": t10,
                                  "delta_t5": round(t5 - v3_t5, 2)}
        print(f"  {mode:13s} T5 {t5:+.2f}%  T10 {t10:+.2f}%  (vs V3-T5 Δ{t5 - v3_t5:+.2f}%)")

    # 重合度 & 黑马优势汇总
    print(f"\n  — 相对 promote 的差异 —")
    summary["diff_vs_promote"] = {}
    for mode in args.modes:
        if mode == "promote":
            continue
        jacs = [r["diff_vs_promote"][mode]["jaccard"]
                for r in results if mode in r.get("diff_vs_promote", {})]
        edges = [r["diff_vs_promote"][mode]["darkhorse_edge"]
                 for r in results if mode in r.get("diff_vs_promote", {})]
        avg_jac = round(np.mean(jacs), 3) if jacs else 1.0
        avg_edge = round(np.mean(edges), 2) if edges else 0.0
        win_rate = round(sum(1 for e in edges if e > 0) / len(edges) * 100, 1) if edges else 0.0
        summary["diff_vs_promote"][mode] = {
            "avg_jaccard": avg_jac, "avg_darkhorse_edge": avg_edge,
            "darkhorse_win_rate": win_rate,
        }
        verdict = "✅换入更强" if avg_edge > 0 else "❌换入更弱"
        print(f"  {mode:13s} 平均Jaccard {avg_jac} | 黑马优势Δ{avg_edge:+.2f}% "
              f"(胜率{win_rate}%) {verdict}")

    # 决策提示
    print(f"\n{'='*70}\n  决策参考\n{'='*70}")
    promote_t5 = summary["modes"].get("promote", {}).get("t5_avg", 0)
    for mode in args.modes:
        if mode == "promote":
            continue
        m = summary["modes"][mode]
        edge = summary["diff_vs_promote"].get(mode, {}).get("avg_darkhorse_edge", 0)
        better = m["t5_avg"] > promote_t5 and edge > 0
        print(f"  {mode:8s}: T5收益 {'优于' if m['t5_avg'] > promote_t5 else '劣于'} promote "
              f"({m['t5_avg']:+.2f}% vs {promote_t5:+.2f}%), 黑马{'增益' if edge > 0 else '损耗'} "
              f"→ {'建议采纳' if better else '维持 promote'}")

    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": "dry-run" if args.dry_run else "full-debate",
        "config": {"windows": len(results), "rounds": args.rounds,
                   "top_k": args.top_k, "force_k": args.force_k, "modes": args.modes},
        "summary": summary,
        "results": results,
    }
    fname = f"backtest_screenmode_{datetime.now():%Y%m%d_%H%M}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n💾 {fname}")


if __name__ == "__main__":
    main()
