#!/usr/bin/env python3
"""新晋股预留席位机制分析 (只分析, 不改生产代码)。

前几轮发现 (见 analyze_star_rerank / analyze_rising_star_boost):
  - 锚排序系数微调 (boost / 重算) 都无法把新晋股推上 TOP10: star 锚分整体太低。
  - 但新晋股跑赢 TOP10 中位数的命中率 18.7% vs 全池 14.6% vs 同低v3控制组 6.5%,
    量价异动带来真增量 (+12.2pp vs 控制组)。
  - 每期最强新晋股 20/20 期碾压 TOP10 末位, 平均 +127pp。
  - 新晋股内部 ``chain+capital-surge`` 与后续涨幅 ρ=+0.494 (20/20 正)。

本脚本验证假设: 既然排序推不动、但最强新晋股确定性碾压 TOP10 末位,
正确做法不是改 _anchor_score, 而是【产出后保留席位】:
  组合 = 锚排序 TOP(10-K) + K 只新晋股(按内部信号选最强)
  对比纯锚 TOP10 的实盘 30 日涨幅。

机制维度:
  - K (预留席位数): 1, 2, 3
  - star 内部排序指标 (从无前视偏差的静态值算):
      * chain+capital-surge  (新晋股内最强 ρ=+0.49)
      * chain-surge          (ρ=+0.47)
      * anchor (chain+cap×2-deliv×0.5)  (全池基线锚, 对照)
      * r20                     (纯量价, 看是否"最强异动"就够)
      * chain                   (单因子最强)

评估 (门槛):
  1. 组合 TOP10 实盘 30 日涨幅有实质提升 (Δ > +1.0pp, 比之前的 0.5pp 更严,
     因为预留席位是结构性改动, 需要更大收益才值得)
  2. 逐期稳健: 提升期数 ≥ 下降期数 (不能靠少数期暴涨掩盖多数期下跌)
  3. 不引入前视偏差: 只用 V3 静态值 + cutoff 截断的量价

用法: uv run python3 scripts/analyze_star_seat.py
"""
import json
import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_rising_star_boost import (
    STAR_R20_MIN, STAR_V3_MAX, SAFE_MEAN_RHO, SAFE_MIN_RHO,
    build_periods, get_all_cutoffs, spearman,
)


# ══════════════════════════════════════════════════════════
# 锚 + 新晋股内部排序指标
# ══════════════════════════════════════════════════════════

def anchor_base(r: dict) -> float:
    return r["chain"] + r["capital"] * 2 - r["surge"] * 0.5


STAR_METRICS: Dict[str, Callable[[dict], float]] = {
    "chain+capital-surge": lambda r: r["chain"] + r["capital"] - r["surge"],
    "chain-surge": lambda r: r["chain"] - r["surge"],
    "anchor(对照)": lambda r: anchor_base(r),
    "r20(量价对照)": lambda r: r["r20"],
    "chain": lambda r: r["chain"],
}


# ══════════════════════════════════════════════════════════
# 组合构建: TOP(10-K) 锚 + K 席 star
# ══════════════════════════════════════════════════════════

def build_portfolio(rows: List[dict], K: int,
                    star_metric: Callable[[dict], float],
                    total: int = 10) -> List[dict]:
    """组合 = 锚排序 TOP(total-K) + K 只最强 star (去重)。

    star 候选 = 候选池里的新晋股; 按 star_metric 降序取前 K。
    若 star 不足 K 只, 有几只填几只 (剩余席位不回填锚, 保持透明)。
    """
    # 锚排序
    by_anchor = sorted(rows, key=lambda r: -anchor_base(r))
    anchor_top = by_anchor[:total - K]

    # star 按 star_metric 排序, 排除已在 anchor_top 里的
    in_anchor = {r["code"] for r in anchor_top}
    stars = [r for r in rows if r["is_star"] and r["code"] not in in_anchor]
    stars.sort(key=lambda r: -star_metric(r))
    star_picks = stars[:K]

    return anchor_top + star_picks


def baseline_portfolio(rows: List[dict], total: int = 10) -> List[dict]:
    return sorted(rows, key=lambda r: -anchor_base(r))[:total]


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════

def eval_seat_mechanism(periods: Dict[str, List[dict]]) -> Tuple[dict, dict]:
    """对所有 (K, star_metric) 组合算逐期 TOP10 涨幅 + star 数。

    返回:
      top10_results: {variant_name: [{cutoff, avg_ret, n_star, n_anchor, base_ret, d_ret}]}
      per_variant_summary: 汇总统计
    """
    Ks = [1, 2, 3]
    variants: List[Tuple[str, int, Callable]] = []
    for K in Ks:
        for mname, mfn in STAR_METRICS.items():
            variants.append((f"TOP{10-K}+{K}席[{mname}]", K, mfn))

    top10_results: Dict[str, List[dict]] = {n: [] for n, _, _ in variants}

    for cutoff, rows in periods.items():
        if len(rows) < 10:
            for n, _, _ in variants:
                top10_results[n].append({})
            continue
        base = baseline_portfolio(rows)
        base_ret = sum(r["ret"] for r in base) / len(base)

        for n, K, mfn in variants:
            port = build_portfolio(rows, K, mfn)
            if not port:
                top10_results[n].append({})
                continue
            avg_ret = sum(r["ret"] for r in port) / len(port)
            n_star = sum(1 for r in port if r["is_star"])
            top10_results[n].append({
                "cutoff": cutoff, "avg_ret": round(avg_ret, 2),
                "n_star": n_star, "n_anchor": len(port) - n_star,
                "base_ret": round(base_ret, 2),
                "d_ret": round(avg_ret - base_ret, 2),
            })
    return top10_results, variants


def summarize(top10_results: dict) -> Dict[str, dict]:
    out = {}
    for n, recs in top10_results.items():
        recs = [r for r in recs if r]
        if not recs:
            continue
        avg_ret = sum(r["avg_ret"] for r in recs) / len(recs)
        base = sum(r["base_ret"] for r in recs) / len(recs)
        d_rets = [r["d_ret"] for r in recs]
        up = sum(1 for d in d_rets if d > 0.5)
        down = sum(1 for d in d_rets if d < -0.5)
        flat = len(d_rets) - up - down
        out[n] = {
            "avg_ret": avg_ret, "base_ret": base, "d_ret": avg_ret - base,
            "n_star": sum(r["n_star"] for r in recs) / len(recs),
            "n_periods": len(recs),
            "up": up, "down": down, "flat": flat,
            "min_d_ret": min(d_rets), "max_d_ret": max(d_rets),
        }
    return out


# ══════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════

THRESHOLD_PP = 1.0  # 实质提升门槛 (pp)


def print_summary_table(summary: dict):
    # 基线
    base_ret = next(iter(summary.values()))["base_ret"]  # 所有变体基线相同
    print(f"\n  基线 (纯锚 TOP10) 实盘 30 日涨幅均值: {base_ret:+.2f}%\n")

    ranked = sorted(summary.items(), key=lambda kv: -kv[1]["d_ret"])
    print(f"  {'组合(按Δ降序)':<28}{'TOP10均涨':>9}{'vs基线':>8}"
          f"{'star均':>6}{'↑期':>5}{'↓期':>5}{'=期':>5}{'min Δ':>7}")
    print("-" * 75)
    for n, s in ranked:
        flag = "✅" if s["d_ret"] > THRESHOLD_PP and s["up"] >= s["down"] else (
            "⚠" if s["d_ret"] > 0 else "❌")
        print(f"  {n:<26}{s['avg_ret']:>+9.2f}{s['d_ret']:>+8.2f}"
              f"{s['n_star']:>6.1f}{s['up']:>5}{s['down']:>5}{s['flat']:>5}"
              f"{s['min_d_ret']:>+7.1f} {flag}")
    print(f"\n  (门槛: Δ>{THRESHOLD_PP}pp 且 ↑期≥↓期 → ✅)")


def print_per_period(top10_results: dict, best_name: str, cutoffs: List[str]):
    print(f"\n  模块 D: 逐期详情 ({best_name} vs 基线)")
    print(f"  {'cutoff':>10}{'基线TOP10':>10}{'组合TOP10':>10}{'Δ':>8}"
          f"{'锚席':>5}{'star席':>6}{'最强star涨幅':>12}")
    print("-" * 70)
    recs = {r["cutoff"]: r for r in top10_results[best_name] if r}
    # 同时算每期最强 star 涨幅 (诊断: 进席位的 star 是不是最强的)
    for c in cutoffs:
        r = recs.get(c)
        if not r:
            continue
        flag = "↑" if r["d_ret"] > 0.5 else ("↓" if r["d_ret"] < -0.5 else "=")
        print(f"  {c:>10}{r['base_ret']:>+10.1f}{r['avg_ret']:>+10.1f}"
              f"{r['d_ret']:>+8.1f}{r['n_anchor']:>5}{r['n_star']:>6}{flag:>6}")


def conclude(summary: dict) -> dict:
    print(f"\n{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")
    base_ret = next(iter(summary.values()))["base_ret"]
    print(f"\n  基线纯锚 TOP10: {base_ret:+.2f}% | 门槛 Δ>{THRESHOLD_PP}pp 且 ↑期≥↓期")

    winners = []
    for n, s in summary.items():
        passed = (s["d_ret"] > THRESHOLD_PP) and (s["up"] >= s["down"])
        winners.append((n, s, passed))
    winners.sort(key=lambda x: -x[1]["d_ret"])

    for n, s, passed in winners:
        tag = "✅ 通过" if passed else "❌"
        print(f"  {n:<26} Δ{s['d_ret']:+.2f}pp | ↑{s['up']}↓{s['down']}=  {s['flat']} "
              f"| star均{s['n_star']:.1f} | minΔ{s['min_d_ret']:+.1f} → {tag}")

    ok = [(n, s) for n, s, p in winners if p]
    print(f"\n  ── 最终建议 ──")
    if ok:
        n, s = ok[0]
        print(f"  ✅ 推荐: {n}")
        print(f"     TOP10 实盘涨幅 {base_ret:+.2f}% → {s['avg_ret']:+.2f}% (Δ{s['d_ret']:+.2f}pp)")
        print(f"     逐期稳健: ↑{s['up']} ↓{s['down']} ={s['flat']} (提升期数 {s['up']}/{s['n_periods']})")
        print(f"     最差单期: Δ{s['min_d_ret']:+.1f}pp | 平均含 star {s['n_star']:.1f} 只")
        return {"recommend": n, "base_ret": base_ret, "winners": [
            {"name": n, "d_ret": s["d_ret"], "up": s["up"], "down": s["down"],
             "avg_ret": s["avg_ret"]} for n, s, _ in winners]}
    else:
        print(f"  ❌ 无组合通过门槛 (Δ>{THRESHOLD_PP}pp 且 ↑期≥↓期)。")
        best = winners[0] if winners else None
        if best:
            n, s, _ = best
            print(f"     最接近: {n} Δ{s['d_ret']:+.2f}pp ↑{s['up']}↓{s['down']}")
        return {"recommend": None, "base_ret": base_ret, "winners": [
            {"name": n, "d_ret": s["d_ret"], "up": s["up"], "down": s["down"],
             "avg_ret": s["avg_ret"]} for n, s, _ in winners]}


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    cutoffs = get_all_cutoffs(step=2)
    hold_days = 30
    print(f"{'='*80}")
    print(f"  新晋股预留席位机制分析 (只分析, 不改生产)")
    print(f"  {len(cutoffs)} 期 × 全V3池 × {hold_days}日窗口 | cutoff {cutoffs[0]}~{cutoffs[-1]}")
    print(f"  机制: 组合 = 锚排序 TOP(10-K) + K 只最强 star (按内部信号选)")
    print(f"  star 内部信号 (无前视, V3静态值): {list(STAR_METRICS.keys())}")
    print(f"  门槛: Δ>{THRESHOLD_PP}pp 且 提升期数≥下降期数")
    print(f"{'='*80}")

    periods = build_periods(cutoffs, hold_days)
    n_with_stars = sum(1 for rows in periods.values() if any(r["is_star"] for r in rows))
    print(f"  含新晋股期数: {n_with_stars}/{len(cutoffs)}")

    top10_results, variants = eval_seat_mechanism(periods)
    summary = summarize(top10_results)

    print_summary_table(summary)

    # 逐期详情: 选 Δ 最高的
    best_name = max(summary.items(), key=lambda kv: kv[1]["d_ret"])[0]
    print_per_period(top10_results, best_name, cutoffs)

    conclusion = conclude(summary)

    # 落盘
    import picker.paths as paths
    out = {
        "generated_at": datetime.now().isoformat(),
        "config": {"cutoffs": cutoffs, "hold_days": hold_days, "threshold_pp": THRESHOLD_PP,
                   "star_v3_max": STAR_V3_MAX, "star_r20_min": STAR_R20_MIN},
        "summary": summary,
        "conclusion": conclusion,
    }
    paths.ensure_caches_dir()
    out_path = os.path.join(paths.CACHES_DIR, "star_seat_analysis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    main()
