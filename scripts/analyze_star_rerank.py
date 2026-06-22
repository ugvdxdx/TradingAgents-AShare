#!/usr/bin/env python3
"""新晋股锚重算可行性分析 (只分析, 不改生产代码)。

上一轮 (analyze_rising_star_boost) 发现:
  - 新晋股虽有超额 (+13.8% 正常期), 但被锚排序系统性压出 TOP10 (出现率 0.5%)。
  - 无差别加 chain-boost 无效 (扰动全池排序, TOP10 涨幅反降)。
  - 但在新晋股【池内部】, chain+capital-delivery 与后续 30 日涨幅 Spearman=+0.494
    (20/20 期正相关), 远超全池锚 chain+capital×2-delivery×0.5 的 +0.356。

本脚本验证核心假设: 既然新晋股内部的"正确排序信号"和全池不同 (delivery 是反向),
能否对【新晋股子集】用专属锚重算, 让真正会涨的 star 进 TOP10, 同时不破坏全池排序?

机制维度 (3 种):
  - rerank:        star 股用 ``chain+capital-delivery`` 重算 (非 star 用基线锚)
  - delivery_pen:  star 股 delivery 权重加大 (基线 -0.5 → -1.0/-1.5/-2.0), 其余不变
  - rerank_strict: star 股用 ``chain-delivery`` (最强单组合) 重算

评估 (三重门槛, 沿用上一轮严格标准):
  1. 全池 Spearman 不显著低于基线 (Δ ≥ -0.005, 防排序整体劣化)
  2. 全池 TOP10 实盘 30 日涨幅 有实质提升 (Δ > +0.5pp, 即超过容差)
  3. 安全区: Spearman 均值 ≥ 0.50, min ≥ 0.34

用法: uv run python3 scripts/analyze_star_rerank.py
"""
import json
import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_rising_star_boost import (
    STAR_R20_MIN, STAR_V3_MAX, SAFE_MEAN_RHO, SAFE_MIN_RHO,
    MEANINGFUL_RHO_GAIN, TOP10_TOLERANCE,
    build_periods, get_all_cutoffs, spearman,
)


# ══════════════════════════════════════════════════════════
# 锚定义
# ══════════════════════════════════════════════════════════

def anchor_base(r: dict) -> float:
    """基线锚: chain + capital×2 - delivery×0.5。"""
    return r["chain"] + r["capital"] * 2 - r["delivery"] * 0.5


def make_rerank(star_anchor: Callable[[dict], float], label: str) -> Callable[[dict], float]:
    """star 用 star_anchor 重算, 非 star 用基线锚。"""
    def fn(r: dict) -> float:
        return star_anchor(r) if r["is_star"] else anchor_base(r)
    fn.__label__ = label
    return fn


# star 专属锚候选
STAR_ANCHORS = {
    "chain+capital-delivery": lambda r: r["chain"] + r["capital"] - r["delivery"],
    "chain-delivery": lambda r: r["chain"] - r["delivery"],
}


def make_delivery_pen(weight: float) -> Callable[[dict], float]:
    """star 股 delivery 权重加大: chain+capital×2-delivery×weight, 非 star 基线。"""
    def fn(r: dict) -> float:
        if r["is_star"]:
            return r["chain"] + r["capital"] * 2 - r["delivery"] * weight
        return anchor_base(r)
    return fn


def build_variants() -> List[Tuple[str, Callable[[dict], float]]]:
    out: List[Tuple[str, Callable[[dict], float]]] = [("(基线) anchor", anchor_base)]
    # 方案1: star 重算 (两个 star 锚)
    for name, fn in STAR_ANCHORS.items():
        out.append((f"star重算[{name}]", make_rerank(fn, name)))
    # 方案2: star delivery 加权惩罚
    for w in (1.0, 1.5, 2.0):
        out.append((f"star delivery×{w}", make_delivery_pen(w)))
    return out


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════

def eval_variants(periods: Dict[str, List[dict]]) -> Tuple[dict, dict]:
    variants = build_variants()
    sp_results: Dict[str, List[Optional[float]]] = {n: [] for n, _ in variants}
    top10_results: Dict[str, List[dict]] = {n: [] for n, _ in variants}

    for cutoff, rows in periods.items():
        if len(rows) < 10:
            for n, _ in variants:
                sp_results[n].append(None)
                top10_results[n].append({})
            continue
        rets = [r["ret"] for r in rows]
        for n, fn in variants:
            vals = [fn(r) for r in rows]
            sp_results[n].append(spearman(vals, rets))
            order = sorted(range(len(rows)), key=lambda i: -vals[i])[:10]
            top_rets = [rows[i]["ret"] for i in order]
            n_star = sum(1 for i in order if rows[i]["is_star"])
            top10_results[n].append({
                "cutoff": cutoff,
                "avg_ret": round(sum(top_rets) / len(top_rets), 2),
                "n_star": n_star,
            })
    return sp_results, top10_results


def stats_of(rhos: List[Optional[float]]) -> dict:
    valid = [r for r in rhos if r is not None]
    if not valid:
        return {"avg": 0, "min": 0, "wins": 0, "n": 0, "valid": False}
    return {
        "avg": sum(valid) / len(valid), "min": min(valid),
        "wins": sum(1 for r in valid if r > 0), "n": len(valid), "valid": True,
    }


def top10_stats(recs: List[dict]) -> dict:
    recs = [r for r in recs if r]
    if not recs:
        return {"avg_top10_ret": 0, "avg_n_star": 0}
    return {
        "avg_top10_ret": sum(r["avg_ret"] for r in recs) / len(recs),
        "avg_n_star": sum(r["n_star"] for r in recs) / len(recs),
    }


# ══════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════

def print_tables(sp_results, top10_results, cutoffs):
    # --- Spearman 表 ---
    ranked = sorted(sp_results.items(),
                    key=lambda kv: -stats_of(kv[1])["avg"])
    header = f"{'变体':<26}"
    for c in cutoffs:
        header += f"{c[5:]:>8}"
    header += f"{'均值':>7}{'min':>6}{'胜率':>7}"
    print(f"\n  模块 C1: 全池 Spearman (基线 vs 重算变体)")
    print(header)
    print("-" * (26 + 8 * len(cutoffs) + 20))
    for n, rhos in ranked:
        s = stats_of(rhos)
        line = f"  {n:<24}"
        for rho in rhos:
            line += f"{rho:>+8.3f}" if rho is not None else f"{'N/A':>8}"
        line += f"{s['avg']:>+7.3f}{s['min']:>+6.2f}{s['wins']:>3}/{s['n']}"
        print(line)

    # --- TOP10 表 ---
    base_t10 = top10_stats(top10_results["(基线) anchor"])
    print(f"\n  模块 C2: 全池 TOP10 实盘质量 (排序取 TOP10, 算 30 日实际涨幅均值)")
    # 按 TOP10 涨幅降序
    t10_ranked = sorted(top10_results.items(),
                        key=lambda kv: -top10_stats(kv[1])["avg_top10_ret"])
    print(f"{'变体':<26}{'TOP10均涨':>10}{'vs基线':>9}{'TOP10 star':>11}{'vs基线':>9}")
    print("-" * 68)
    for n, recs in t10_ranked:
        s = top10_stats(recs)
        d_ret = s["avg_top10_ret"] - base_t10["avg_top10_ret"]
        d_star = s["avg_n_star"] - base_t10["avg_n_star"]
        print(f"  {n:<24}{s['avg_top10_ret']:>+10.2f}{d_ret:>+9.2f}"
              f"{s['avg_n_star']:>11.2f}{d_star:>+9.2f}")
    return ranked, base_t10


def print_per_period(sp_results, top10_results, best_name, cutoffs):
    """逐期展示最优变体 vs 基线的 TOP10 涨幅 + star 数变化。"""
    print(f"\n  模块 D: 逐期 TOP10 详情 ({best_name} vs 基线)")
    print(f"{'cutoff':>10}{'基线TOP10涨':>12}{'变体TOP10涨':>12}{'Δ':>8}"
          f"{'基线star':>9}{'变体star':>9}{'全池ρ(基/变)':>16}")
    print("-" * 80)
    base_rhos = sp_results["(基线) anchor"]
    var_rhos = sp_results[best_name]
    for i, c in enumerate(cutoffs):
        b = top10_results["(基线) anchor"][i]
        v = top10_results[best_name][i]
        if not b or not v:
            continue
        d = v["avg_ret"] - b["avg_ret"]
        br = base_rhos[i] if base_rhos[i] is not None else 0
        vr = var_rhos[i] if var_rhos[i] is not None else 0
        flag = "↑" if d > 0.5 else ("↓" if d < -0.5 else " ")
        print(f"{c:>10}{b['avg_ret']:>+12.1f}{v['avg_ret']:>+12.1f}{d:>+8.1f}{flag}"
              f"{b['n_star']:>9}{v['n_star']:>9}{br:>+8.2f}/{vr:+.2f}")


def conclude(sp_results, top10_results, base_t10) -> dict:
    print(f"\n{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")

    base_s = stats_of(sp_results["(基线) anchor"])
    base_avg = base_s["avg"]
    base_min = base_s["min"]
    base_ret = base_t10["avg_top10_ret"]

    print(f"\n  基线: 全池Spearman {base_avg:+.3f} (min {base_min:+.3f}) | "
          f"TOP10均涨 {base_ret:+.2f}% | 含star {base_t10['avg_n_star']:.2f}只")
    print(f"  门槛: 全池Spearman Δ≥-{MEANINGFUL_RHO_GAIN} | TOP10涨幅 Δ>+{abs(TOP10_TOLERANCE)}pp | "
          f"安全[均≥{SAFE_MEAN_RHO}, min≥{SAFE_MIN_RHO}]")

    # 找 TOP10 涨幅最优且通过门槛的变体
    winners = []
    for n, rhos in sp_results.items():
        if n == "(基线) anchor":
            continue
        s = stats_of(rhos)
        t = top10_stats(top10_results[n])
        d_ret = t["avg_top10_ret"] - base_ret
        d_rho = s["avg"] - base_avg
        # 三重门槛
        rho_ok = d_rho >= -MEANINGFUL_RHO_GAIN  # Spearman 不显著劣化
        ret_ok = d_ret > abs(TOP10_TOLERANCE)   # TOP10 实质提升
        safe = s["avg"] >= SAFE_MEAN_RHO and s["min"] >= SAFE_MIN_RHO
        passed = rho_ok and ret_ok and safe
        winners.append({
            "name": n, "avg": s["avg"], "min": s["min"],
            "d_rho": d_rho, "d_ret": d_ret,
            "n_star": t["avg_n_star"], "passed": passed,
            "reasons": _reasons(rho_ok, ret_ok, safe, d_rho, d_ret, s),
        })
    winners.sort(key=lambda x: -x["d_ret"])

    for w in winners:
        tag = "✅ 通过" if w["passed"] else "❌ " + w["reasons"]
        print(f"  {w['name']:<24} Spearman {w['avg']:+.3f}(Δ{w['d_rho']:+.4f}) | "
              f"TOP10 {w['d_ret']:+.2f}pp | star {w['n_star']:.2f}只 → {tag}")

    ok = [w for w in winners if w["passed"]]
    print(f"\n  ── 最终建议 ──")
    if ok:
        w = ok[0]
        print(f"  ✅ 推荐: {w['name']}")
        print(f"     TOP10 实盘涨幅 {base_ret:+.2f}% → {base_ret+w['d_ret']:+.2f}% "
              f"(Δ{w['d_ret']:+.2f}pp), TOP10 star {base_t10['avg_n_star']:.2f}→{w['n_star']:.2f}只")
        print(f"     全池 Spearman {base_avg:+.3f}→{w['avg']:+.3f} (Δ{w['d_rho']:+.4f}, 不降)。")
        return {"recommend": w["name"], "winners": winners,
                "baseline": {"avg": base_avg, "min": base_min, "top10_ret": base_ret}}
    else:
        print(f"  ❌ 无变体通过三重门槛。维持基线 anchor。")
        return {"recommend": None, "winners": winners,
                "baseline": {"avg": base_avg, "min": base_min, "top10_ret": base_ret}}


def _reasons(rho_ok, ret_ok, safe, d_rho, d_ret, s):
    rs = []
    if not rho_ok:
        rs.append(f"Spearman降{d_rho:+.4f}")
    if not ret_ok:
        rs.append(f"TOP10仅{d_ret:+.2f}pp")
    if not safe:
        rs.append(f"不安全(min{s['min']:+.2f})")
    return "; ".join(rs) or "通过"


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    cutoffs = get_all_cutoffs(step=2)
    hold_days = 30
    print(f"{'='*80}")
    print(f"  新晋股锚重算可行性分析 (只分析, 不改生产)")
    print(f"  {len(cutoffs)} 期 × 全V3池 × {hold_days}日窗口 | cutoff {cutoffs[0]}~{cutoffs[-1]}")
    print(f"  star判定: v3<{STAR_V3_MAX} & r20>{STAR_R20_MIN}")
    print(f"{'='*80}")

    periods = build_periods(cutoffs, hold_days)
    n_with_stars = sum(1 for rows in periods.values() if any(r["is_star"] for r in rows))
    print(f"  含新晋股期数: {n_with_stars}/{len(cutoffs)}")

    sp_results, top10_results = eval_variants(periods)
    sp_ranked, base_t10 = print_tables(sp_results, top10_results, cutoffs)

    # 逐期详情: 选 TOP10 涨幅最优的非基线变体
    t10_order = sorted(
        [(n, top10_stats(r)) for n, r in top10_results.items() if n != "(基线) anchor"],
        key=lambda x: -x[1]["avg_top10_ret"],
    )
    if t10_order:
        print_per_period(sp_results, top10_results, t10_order[0][0], cutoffs)

    conclusion = conclude(sp_results, top10_results, base_t10)

    # 落盘
    import picker.paths as paths
    out = {
        "generated_at": datetime.now().isoformat(),
        "config": {"cutoffs": cutoffs, "hold_days": hold_days,
                   "star_v3_max": STAR_V3_MAX, "star_r20_min": STAR_R20_MIN},
        "spearman": {n: stats_of(r) for n, r in sp_results.items()},
        "top10": {n: top10_stats(r) for n, r in top10_results.items()},
        "conclusion": conclusion,
    }
    paths.ensure_caches_dir()
    out_path = os.path.join(paths.CACHES_DIR, "star_rerank_analysis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    main()
