#!/usr/bin/env python3
"""新晋股 TOP1 选星指标优化 (只分析, 不改生产代码)。

背景: 已确定 TOP9+1席 方案 (锚排序 TOP9 + 1 席新晋股)。本脚本回答:
  那 1 席新晋股, 用什么指标从该期所有新晋股里选 TOP1, 能让它"又大又稳"?

评估对象: 用某指标选出的 TOP1 新晋股的【后续 30 日实际涨幅】。
目标: 均值高(大) + min 不亏(稳) + 标准差小(稳) + 正收益占比高。

正常期 (剔除 3 月战争期): 12 期, 每期 13~106 只新晋股候选。

发现: chain 单独均值最高 (+91) 但 min -2.6、不稳;
      chain/5 + r20/50 (归一化让 chain 与 r20 量纲接近) 夏普最高 (1.86),
      用 r20 在 chain 并列时做 tiebreak, min 提到 +20.9 (从不亏)。

用法:
  uv run python3 scripts/analyze_star_top1_metric.py
  uv run python3 scripts/analyze_star_top1_metric.py --start 2026-04-01  # 仅正常期
  uv run python3 scripts/analyze_star_top1_metric.py --include-war       # 含战争期
"""
import argparse
import json
import os
import statistics
import sys
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_rising_star_boost import V3, _load_kline, get_all_cutoffs


# ══════════════════════════════════════════════════════════
# 数据构建: 每期新晋股候选 + 特征
# ══════════════════════════════════════════════════════════

def build_star_samples(cutoffs: List[str], hold_days: int = 30
                       ) -> Dict[str, List[dict]]:
    """每期: 收集新晋股 (v3<15 & r20>15) + 量价特征 + 后续涨幅。

    所有特征都用 cutoff 截断的 K 线算 (无前视), 涨幅用 cutoff 后 30 日。
    """
    klines: Dict[str, object] = {}
    for code in V3:
        df = _load_kline(code)
        if df is not None:
            klines[code] = df
    base = klines["300308"]
    date_to_idx = {d: i for i, d in enumerate(base["trade_date"])}

    periods: Dict[str, List[dict]] = {}
    for cutoff in cutoffs:
        if cutoff not in date_to_idx:
            periods[cutoff] = []
            continue
        stars: List[dict] = []
        for code, v in V3.items():
            if not isinstance(v, dict) or "sector_score" not in v:
                continue
            df = klines.get(code)
            if df is None:
                continue
            valid = df[df["trade_date"] <= cutoff]
            if len(valid) <= 20:
                continue
            idx = len(valid) - 1
            close = df["close"]
            vol = df["volume"]
            r5 = (close.iloc[idx] / close.iloc[idx - 5] - 1) * 100
            r20 = (close.iloc[idx] / close.iloc[idx - 20] - 1) * 100
            ss = v["sector_score"]
            if not (ss < 15 and r20 > 15):
                continue
            end = idx + hold_days
            if end >= len(df):
                continue
            ret = (close.iloc[end] / close.iloc[idx] - 1) * 100
            vma5 = vol.iloc[idx - 5:idx + 1].mean()
            vma20 = vol.iloc[idx - 20:idx + 1].mean()
            vol_ratio = vma5 / vma20 if vma20 > 0 else 1.0
            dist_high20 = (close.iloc[idx] / close.iloc[idx - 20:idx + 1].max() - 1) * 100
            r10 = (close.iloc[idx] / close.iloc[idx - 10] - 1) * 100 if idx >= 10 else r20
            stars.append({
                "code": code, "ret": ret, "r5": r5, "r10": r10, "r20": r20,
                "chain": v.get("chain", 0), "capital": v.get("capital", 0),
                "delivery": v.get("delivery", 0), "v3": ss,
                "vol_ratio": vol_ratio, "dist_high20": dist_high20,
            })
        periods[cutoff] = stars
    return periods


# ══════════════════════════════════════════════════════════
# 候选指标库
# ══════════════════════════════════════════════════════════

METRICS: Dict[str, Callable[[dict], float]] = {
    # 基本面单因子
    "chain": lambda s: s["chain"],
    "capital": lambda s: s["capital"],
    "delivery": lambda s: s["delivery"],
    "v3": lambda s: s["v3"],
    # 量价单因子
    "r5": lambda s: s["r5"],
    "r10": lambda s: s["r10"],
    "r20": lambda s: s["r20"],
    "vol_ratio": lambda s: s["vol_ratio"],
    # 基本面组合 (全池锚系)
    "anchor": lambda s: s["chain"] + s["capital"] * 2 - s["delivery"] * 0.5,
    "chain+capital-delivery": lambda s: s["chain"] + s["capital"] - s["delivery"],
    "chain-delivery": lambda s: s["chain"] - s["delivery"],
    "chain×2+capital": lambda s: s["chain"] * 2 + s["capital"],
    "chain×2+capital-delivery": lambda s: s["chain"] * 2 + s["capital"] - s["delivery"],
    "chain×3-delivery": lambda s: s["chain"] * 3 - s["delivery"],
    # chain + 量价 (归一化, 让 chain 主导、r20 做 tiebreak)
    "chain/5+r20/50": lambda s: s["chain"] / 5 + s["r20"] / 50,
    "chain/10+r20/50": lambda s: s["chain"] / 10 + s["r20"] / 50,
    "chain+r20×0.3": lambda s: s["chain"] + s["r20"] * 0.3,
    "chain+r20×0.5": lambda s: s["chain"] + s["r20"] * 0.5,
    "chain×3+r20": lambda s: s["chain"] * 3 + s["r20"],
    # chain + capital + r20 三因子
    "chain/5+capital/5+r20/50": lambda s: s["chain"] / 5 + s["capital"] / 5 + s["r20"] / 50,
    "chain×2+capital+r20×0.3": lambda s: s["chain"] * 2 + s["capital"] + s["r20"] * 0.3,
}


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════

def eval_top1(periods: Dict[str, List[dict]], metric_fn: Callable[[dict], float]
              ) -> Optional[dict]:
    """用 metric_fn 每期选 TOP1 star, 返回选中股的涨幅统计 + 逐期明细。"""
    rets: List[float] = []
    detail: List[dict] = []
    for cutoff, stars in periods.items():
        if not stars:
            continue
        best = max(stars, key=metric_fn)
        rets.append(best["ret"])
        detail.append({"cutoff": cutoff, "code": best["code"], "ret": best["ret"],
                       "chain": best["chain"], "r20": best["r20"]})
    if not rets:
        return None
    avg = sum(rets) / len(rets)
    return {
        "avg": avg, "med": statistics.median(rets), "min": min(rets),
        "max": max(rets), "pos_pct": sum(1 for r in rets if r > 0) / len(rets) * 100,
        "std": statistics.stdev(rets) if len(rets) > 1 else 0,
        "sharpe": avg / statistics.stdev(rets) if len(rets) > 1 and statistics.stdev(rets) > 0 else 0,
        "rets": rets, "detail": detail, "n": len(rets),
    }


def score(r: dict) -> float:
    """复合评分: 均值(大) + min×0.5(稳, 惩罚最差期) + 正收益%×0.1(稳)。"""
    return r["avg"] + r["min"] * 0.5 + r["pos_pct"] * 0.1


# ══════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════

def print_ranking(periods: Dict[str, List[dict]], title: str):
    results = []
    for name, fn in METRICS.items():
        r = eval_top1(periods, fn)
        if r:
            results.append((name, r))
    results.sort(key=lambda x: -score(x[1]))

    print(f"\n  {title}")
    print(f"  评分 = 均值 + min×0.5 + 正收益%×0.1")
    print(f"  {'指标(按评分降序)':<30}{'均值':>7}{'中位':>7}{'min':>7}"
          f"{'正收益':>7}{'标准差':>7}{'夏普':>6}{'评分':>8}")
    print("-" * 86)
    for name, r in results:
        flag = "★" if score(r) > 90 else ""
        print(f"  {name:<28}{r['avg']:>+7.1f}{r['med']:>+7.1f}{r['min']:>+7.1f}"
              f"{r['pos_pct']:>6.0f}%{r['std']:>7.1f}{r['sharpe']:>6.2f}"
              f"{score(r):>+8.1f} {flag}")
    return results


def print_per_period(results: List[Tuple[str, dict]], names: List[str], cutoffs: List[str]):
    """逐期对比指定指标选出的 TOP1。"""
    print(f"\n  逐期对比 ({', '.join(names)})")
    header = f"  {'cutoff':>10}{'star#':>6}"
    for n in names:
        header += f"{n:>20}"
    print(header)
    print("-" * (16 + 20 * len(names)))

    detail_map = {n: {d["cutoff"]: d for d in dict(results)[n]["detail"]} for n in names
                  if n in dict(results)}
    # 需要每期 star 数
    star_counts = {}
    from scripts.analyze_rising_star_boost import build_periods
    # 简化: 直接用 results 里第一个指标的 detail 取 cutoff
    all_cutoffs = sorted({d["cutoff"] for d in dict(results)[names[0]]["detail"]})
    for c in all_cutoffs:
        line = f"  {c:>10}"
        # star 数从任一指标都一样 (候选池相同), 取 detail 存在即说明有 star
        line += f"{('?'):>6}"
        for n in names:
            d = detail_map.get(n, {}).get(c)
            if d:
                line += f"  {d['code']} {d['ret']:+7.1f}%"
            else:
                line += f"{'N/A':>20}"
        print(line)


def conclude(results: List[Tuple[str, dict]]) -> dict:
    print(f"\n{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")
    chain_r = dict(results).get("chain")
    print(f"\n  基准 (chain 选星): 均值 {chain_r['avg']:+.1f} | min {chain_r['min']:+.1f} | "
          f"夏普 {chain_r['sharpe']:.2f} | 正收益 {chain_r['pos_pct']:.0f}%")

    # 最优: 按 score
    best_name, best_r = results[0]
    print(f"\n  最优指标: {best_name}")
    print(f"    均值 {best_r['avg']:+.1f} (vs chain {chain_r['avg']:+.1f}, "
          f"Δ{best_r['avg']-chain_r['avg']:+.1f})")
    print(f"    min  {best_r['min']:+.1f} (vs chain {chain_r['min']:+.1f}, "
          f"Δ{best_r['min']-chain_r['min']:+.1f})  ← 稳定性")
    print(f"    夏普 {best_r['sharpe']:.2f} (vs chain {chain_r['sharpe']:.2f})  ← 风险调整后收益")
    print(f"    正收益 {best_r['pos_pct']:.0f}% (vs chain {chain_r['pos_pct']:.0f}%)")

    better = (best_r["avg"] >= chain_r["avg"] * 0.9  # 均值不显著低于 chain
              and best_r["min"] > chain_r["min"]      # min 严格更好
              and best_r["sharpe"] > chain_r["sharpe"])
    if best_name != "chain" and better:
        print(f"\n  ✅ 推荐: 用 {best_name} 替代 chain 作为新晋股选星指标。")
        print(f"     牺牲少量均值换取'从不亏'+ 更高夏普, 整体又大又稳。")
        print(f"     生产接入: make_ranking_debate 选第10席 star 时, "
              f"按 {best_name} 降序取首只。")
        return {"recommend": best_name, "beats_chain": True, "best": best_r, "chain": chain_r}
    elif best_name == "chain":
        print(f"\n  chain 仍是最优 (均值最高且评分领先)。维持 chain 选星。")
        return {"recommend": "chain", "beats_chain": False, "best": best_r, "chain": chain_r}
    else:
        print(f"\n  ⚠ {best_name} 评分更高但未必全面优于 chain, 视偏好取舍。")
        return {"recommend": best_name, "beats_chain": False, "best": best_r, "chain": chain_r}


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="新晋股 TOP1 选星指标优化")
    parser.add_argument("--start", default="2026-04-01",
                        help="cutoff 起始日(含), 默认 2026-04-01 剔除战争期")
    parser.add_argument("--include-war", action="store_true", help="含战争期 (覆盖 --start)")
    args = parser.parse_args()

    all_cutoffs = get_all_cutoffs(step=2)
    if args.include_war:
        cutoffs = all_cutoffs
        label = f"全部 {len(cutoffs)} 期 (含战争期)"
    else:
        start = args.start
        cutoffs = [c for c in all_cutoffs if c >= start]
        label = f"正常期 {len(cutoffs)} 期 (cutoff {cutoffs[0]}~{cutoffs[-1]}, 剔除<{start})"

    print(f"{'='*80}")
    print(f"  新晋股 TOP1 选星指标优化 (只分析, 不改生产)")
    print(f"  {label}")
    print(f"  问题: TOP9+1席方案的第10席, 用什么指标选新晋股里的 TOP1?")
    print(f"  目标: 选出股的后续30日涨幅 均值高(大) + min不亏(稳) + 夏普高")
    print(f"{'='*80}")

    periods = build_star_samples(cutoffs)
    total_stars = sum(len(s) for s in periods.values())
    n_with = sum(1 for s in periods.values() if s)
    print(f"  含新晋股期数: {n_with}/{len(cutoffs)} | 新晋股样本总数: {total_stars}")

    results = print_ranking(periods, "选星指标排行榜")

    # 逐期对比 top3
    top3_names = [n for n, _ in results[:3]]
    print_per_period(results, top3_names, cutoffs)

    conclusion = conclude(results)

    # 落盘
    import picker.paths as paths
    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "config": {"cutoffs": cutoffs, "label": label},
        "ranking": [{"name": n, "avg": r["avg"], "med": r["med"], "min": r["min"],
                     "pos_pct": r["pos_pct"], "std": r["std"], "sharpe": r["sharpe"],
                     "score": score(r)}
                    for n, r in results],
        "conclusion": {"recommend": conclusion["recommend"],
                       "beats_chain": conclusion["beats_chain"]},
    }
    paths.ensure_caches_dir()
    suffix = "all" if args.include_war else "normal"
    out_path = os.path.join(paths.CACHES_DIR, f"star_top1_metric_{suffix}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    main()
