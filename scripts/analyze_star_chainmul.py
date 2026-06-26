#!/usr/bin/env python3
"""新晋股 chain×系数机制分析 (只分析, 不改生产代码)。

用户指定的方案: 对新晋股的 chain 乘以一个系数 (而非加固定 B), 让 TOP10 中
新晋股平均每期达到目标只数 (1.5 / 2 / 3), 然后看实盘涨幅。

机制:
  对 is_star 的股: chain_eff = chain × coef
  anchor = chain_eff + capital×2 + surge×SURGE_WEIGHT
  非 star: 不变

为什么乘法而非加法 (区别于 analyze_rising_star_boost):
  - 加法 B 给所有 star 同等加成, chain=0 的纯概念股和 chain=5 的产业链股同等受益。
  - 乘法 coef 放大 chain 的区分度: 产业链强的 star (chain=5) 加得多, 概念炒作的
    star (chain=1) 加得少。这和上一轮发现"用 chain 选最强 star 效果最好"逻辑一致。
  - star 的 chain 分布: 中位 2.5, 均值 2.67, 仅 16% <1 → 乘法可行 (非 0×N 问题)。

方法: 对每个目标 star 数, 二分搜索 coef 使 TOP10 平均 star 数 ≈ 目标。
然后评估该 coef 下的实盘表现, 与基线/上一轮 TOP9+1席方案对比。

评估门槛 (沿用):
  1. TOP10 实盘涨幅实质提升 (Δ > +1.0pp)
  2. 逐期稳健 (↑期 ≥ ↓期)
  3. 全池 Spearman 不崩 (作为参考, 预留席位类机制不保证 Spearman)

用法:
  uv run python3 scripts/analyze_star_chainmul.py                    # 全部 21 期
  uv run python3 scripts/analyze_star_chainmul.py --start 2026-04-01 # 仅正常期 (剔除战争期)
"""
import json
import os
import sys
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_rising_star_boost import (
    STAR_R20_MIN, STAR_V3_MAX, SAFE_MEAN_RHO, SAFE_MIN_RHO,
    build_periods, get_all_cutoffs, spearman,
)

THRESHOLD_PP = 1.0


# ══════════════════════════════════════════════════════════
# 锚定义
# ══════════════════════════════════════════════════════════

def anchor_base(r: dict) -> float:
    return r["chain"] + r["capital"] * 2 - r["surge"] * 0.5


def make_chainmul(coef: float) -> Callable[[dict], float]:
    """star 股 chain × coef, 非 star 基线锚。"""
    def fn(r: dict) -> float:
        if r["is_star"]:
            return r["chain"] * coef + r["capital"] * 2 - r["surge"] * 0.5
        return anchor_base(r)
    return fn


# ══════════════════════════════════════════════════════════
# 二分搜索 coef 使 TOP10 平均 star 数 ≈ target
# ══════════════════════════════════════════════════════════

def avg_star_in_top10(periods: Dict[str, List[dict]], fn: Callable[[dict], float]) -> float:
    """用 fn 排序取 TOP10, 返回平均 star 数/期。"""
    total_star = 0
    n_periods = 0
    for rows in periods.values():
        if len(rows) < 10:
            continue
        order = sorted(range(len(rows)), key=lambda i: -fn(rows[i]))[:10]
        total_star += sum(1 for i in order if rows[i]["is_star"])
        n_periods += 1
    return total_star / n_periods if n_periods else 0.0


def search_coef(periods: Dict[str, List[dict]], target_star: float,
                lo: float = 1.0, hi: float = 50.0, iters: int = 60) -> float:
    """二分搜索 coef 使 avg_star_in_top10 ≈ target_star。

    coef 越大 → star 锚越高 → 进 TOP10 的 star 越多 → avg_star 越大 (单调)。
    """
    for _ in range(iters):
        mid = (lo + hi) / 2
        got = avg_star_in_top10(periods, make_chainmul(mid))
        if got < target_star:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════

def eval_coef(periods: Dict[str, List[dict]], coef: float
              ) -> Tuple[List[Optional[float]], List[dict]]:
    """对给定 coef 算逐期 (全池 Spearman, TOP10 详情)。"""
    fn = make_chainmul(coef)
    rhos: List[Optional[float]] = []
    top10_recs: List[dict] = []
    for cutoff, rows in periods.items():
        if len(rows) < 10:
            rhos.append(None)
            top10_recs.append({})
            continue
        vals = [fn(r) for r in rows]
        rhos.append(spearman(vals, [r["ret"] for r in rows]))
        order = sorted(range(len(rows)), key=lambda i: -vals[i])[:10]
        top_rets = [rows[i]["ret"] for i in order]
        n_star = sum(1 for i in order if rows[i]["is_star"])
        top10_recs.append({
            "cutoff": cutoff,
            "avg_ret": round(sum(top_rets) / len(top_rets), 2),
            "n_star": n_star,
        })
    return rhos, top10_recs


def eval_base(periods: Dict[str, List[dict]]) -> Tuple[List[Optional[float]], List[dict]]:
    rhos: List[Optional[float]] = []
    top10_recs: List[dict] = []
    for cutoff, rows in periods.items():
        if len(rows) < 10:
            rhos.append(None)
            top10_recs.append({})
            continue
        vals = [anchor_base(r) for r in rows]
        rhos.append(spearman(vals, [r["ret"] for r in rows]))
        order = sorted(range(len(rows)), key=lambda i: -vals[i])[:10]
        top_rets = [rows[i]["ret"] for i in order]
        n_star = sum(1 for i in order if rows[i]["is_star"])
        top10_recs.append({
            "cutoff": cutoff, "avg_ret": round(sum(top_rets) / len(top_rets), 2),
            "n_star": n_star,
        })
    return rhos, top10_recs


def summarize_recs(top10_recs: List[dict], base_recs: List[dict]) -> dict:
    """对比 base_recs, 返回汇总。"""
    recs = [r for r in top10_recs if r]
    bases = [r for r in base_recs if r]
    if not recs:
        return {}
    avg_ret = sum(r["avg_ret"] for r in recs) / len(recs)
    base_ret = sum(r["avg_ret"] for r in bases) / len(bases)
    d_rets = [r["avg_ret"] - b["avg_ret"] for r, b in zip(recs, bases)]
    up = sum(1 for d in d_rets if d > 0.5)
    down = sum(1 for d in d_rets if d < -0.5)
    return {
        "avg_ret": avg_ret, "base_ret": base_ret, "d_ret": avg_ret - base_ret,
        "avg_n_star": sum(r["n_star"] for r in recs) / len(recs),
        "up": up, "down": down, "flat": len(d_rets) - up - down,
        "min_d_ret": min(d_rets), "max_d_ret": max(d_rets),
        "per_period": [{"cutoff": r["cutoff"], "avg_ret": r["avg_ret"],
                        "base_ret": b["avg_ret"], "d_ret": round(r["avg_ret"]-b["avg_ret"],2),
                        "n_star": r["n_star"]} for r, b in zip(recs, bases)],
    }


def spearman_stats(rhos: List[Optional[float]]) -> dict:
    valid = [r for r in rhos if r is not None]
    return {
        "avg": sum(valid)/len(valid) if valid else 0,
        "min": min(valid) if valid else 0,
        "wins": sum(1 for r in valid if r > 0),
        "n": len(valid),
    }


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="新晋股 chain×系数机制分析")
    parser.add_argument("--start", default="", help="cutoff 起始日 (含), 早于此的剔除。如 --start 2026-04-01 剔除战争期")
    args = parser.parse_args()

    all_cutoffs = get_all_cutoffs(step=2)
    cutoffs = [c for c in all_cutoffs if not args.start or c >= args.start]
    hold_days = 30
    targets = [0.5, 1.0, 1.5, 2.0, 3.0]
    period_label = f"{len(cutoffs)} 期 (cutoff {cutoffs[0]}~{cutoffs[-1]})"
    if args.start:
        period_label += f" | 已剔除 <{args.start} 的战争期"
    print(f"{'='*80}")
    print(f"  新晋股 chain×系数机制 (只分析, 不改生产)")
    print(f"  {period_label} × 全V3池 × {hold_days}日窗口")
    print(f"  机制: star 股 chain × coef (放大产业链强 star 的优势)")
    print(f"  目标: TOP10 平均 star 数 = {targets}")
    print(f"{'='*80}")

    periods = build_periods(cutoffs, hold_days)

    # 基线
    base_rhos, base_recs = eval_base(periods)
    base_sum = summarize_recs(base_recs, base_recs)
    base_sp = spearman_stats(base_rhos)
    print(f"\n  基线 (纯锚): TOP10 均涨 {base_sum['avg_ret']:+.2f}% | "
          f"平均 star {base_sum['avg_n_star']:.2f} 只 | 全池 Spearman {base_sp['avg']:+.3f}")

    # 对每个目标搜索 coef 并评估
    results = {}
    print(f"\n  {'目标star':>8} {'coef':>7} {'实际star':>8} {'TOP10均涨':>9} "
          f"{'Δ vs基线':>9} {'↑期':>4}{'↓期':>4}{'min Δ':>7} {'全池ρ':>7}")
    print("-" * 75)
    for target in targets:
        coef = search_coef(periods, target)
        actual_star = avg_star_in_top10(periods, make_chainmul(coef))
        rhos, recs = eval_coef(periods, coef)
        s = summarize_recs(recs, base_recs)
        sp = spearman_stats(rhos)
        results[target] = {"coef": coef, "actual_star": actual_star,
                           "summary": s, "spearman": sp}
        flag = "✅" if (s["d_ret"] > THRESHOLD_PP and s["up"] >= s["down"]) else (
            "⚠" if s["d_ret"] > 0 else "❌")
        print(f"  {target:>8.1f} {coef:>7.2f} {actual_star:>8.2f} {s['avg_ret']:>+9.2f} "
              f"{s['d_ret']:>+9.2f} {s['up']:>4}{s['down']:>4}{s['min_d_ret']:>+7.1f} "
              f"{sp['avg']:>+7.3f} {flag}")

    # 逐期详情: 对每个目标展示
    for target in targets:
        r = results[target]
        s = r["summary"]
        print(f"\n  ── 目标 {target} 只 star (coef={r['coef']:.2f}, 实际 {r['actual_star']:.2f} 只) 逐期 ──")
        print(f"  {'cutoff':>10}{'基线':>8}{'组合':>8}{'Δ':>7}{'star':>5}")
        print("  " + "-"*42)
        for p in s["per_period"]:
            fl = "↑" if p["d_ret"] > 0.5 else ("↓" if p["d_ret"] < -0.5 else "=")
            print(f"  {p['cutoff']:>10}{p['base_ret']:>+8.1f}{p['avg_ret']:>+8.1f}"
                  f"{p['d_ret']:>+7.1f}{p['n_star']:>5} {fl}")

    # 结论
    print(f"\n{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")
    print(f"\n  基线: TOP10 均涨 {base_sum['avg_ret']:+.2f}% | star {base_sum['avg_n_star']:.2f}只 | ρ {base_sp['avg']:+.3f}")
    print(f"  门槛: Δ>{THRESHOLD_PP}pp 且 ↑期≥↓期\n")
    best = None
    for target in targets:
        r = results[target]
        s = r["summary"]
        sp = r["spearman"]
        passed = s["d_ret"] > THRESHOLD_PP and s["up"] >= s["down"]
        tag = "✅ 通过" if passed else "❌"
        print(f"  目标{target:.1f}只 (coef={r['coef']:.2f}): "
              f"Δ{s['d_ret']:+.2f}pp | ↑{s['up']}↓{s['down']} | "
              f"star {r['actual_star']:.2f} | ρ {sp['avg']:+.3f}(Δ{sp['avg']-base_sp['avg']:+.3f}) → {tag}")
        if passed and (best is None or s["d_ret"] > best[1]["summary"]["d_ret"]):
            best = (target, r)

    print(f"\n  ── 最终建议 ──")
    if best:
        target, r = best
        s = r["summary"]
        print(f"  ✅ 推荐: 目标 {target:.1f} 只 star, coef={r['coef']:.2f}")
        print(f"     TOP10 {base_sum['avg_ret']:+.2f}% → {s['avg_ret']:+.2f}% (Δ{s['d_ret']:+.2f}pp)")
        print(f"     逐期 ↑{s['up']} ↓{s['down']} | 最差单期 {s['min_d_ret']:+.1f}pp")
        print(f"     生产接入: debaters._anchor_score 对 c.get('_rising_star') 的股, "
              f"chain × {r['coef']:.2f}")
        recommend = {"target": target, "coef": r["coef"]}
    else:
        # 所有目标都不过, 报告最接近的
        best_d = max(results.items(), key=lambda kv: kv[1]["summary"]["d_ret"])
        print(f"  ❌ 无目标档位通过门槛。")
        print(f"     最接近: 目标{best_d[0]:.1f}只 Δ{best_d[1]['summary']['d_ret']:+.2f}pp")
        recommend = None

    # 落盘
    import picker.paths as paths
    out = {
        "generated_at": datetime.now().isoformat(),
        "config": {"cutoffs": cutoffs, "hold_days": hold_days, "targets": targets,
                   "threshold_pp": THRESHOLD_PP},
        "baseline": {"avg_ret": base_sum["avg_ret"], "avg_n_star": base_sum["avg_n_star"],
                     "spearman_avg": base_sp["avg"]},
        "results": {str(t): {"coef": r["coef"], "actual_star": r["actual_star"],
                             "d_ret": r["summary"]["d_ret"], "up": r["summary"]["up"],
                             "down": r["summary"]["down"], "spearman_avg": r["spearman"]["avg"]}
                    for t, r in results.items()},
        "recommend": recommend,
    }
    paths.ensure_caches_dir()
    out_path = os.path.join(paths.CACHES_DIR, "star_chainmul_analysis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    main()
