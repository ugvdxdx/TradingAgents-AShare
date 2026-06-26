#!/usr/bin/env python3
"""召回阶段消融实验: 全池 vs 召回筛选 (top_n=50/100/150), 验证"G模式全池更优"。

背景:
  生产 load_top_n(top_n=9999) 实际是全池(~530只), 召回筛选未生效。
  历史结论: "回测证明预筛帮倒忙, 改为全池直接排序"。
  本实验用 G 模式(无封顶) + cutoff 重建数据, 量化验证这个结论。

实验设计:
  召回排序键 = chain + capital (load_top_n 实际用的入池排序, 去掉 surge)
  最终排序键 = chain + capital×2 + surge×SURGE_WEIGHT (quantum_rank 锚)
  对比 top_n ∈ {50, 100, 150, 200, 全池}:
    - 在召回阶段取 chain+capital top_n 子集
    - 在子集内按锚排序取 TOP10
    - 用全池算 Spearman (子集内排序的预测力)
  注意: 召回筛选会改变参与排序的股票池, Spearman 在不同池上不可直接比,
        所以核心看 TOP10 涨幅 (策略实际收益) + 召回命中率。

用法:
  uv run python3 scripts/experiment_recall.py
"""
import json
import os
import pickle
import statistics
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths
from picker.scoring.v3_full_score import (
    KLINE_CACHE_DIR, compute_capital_updates, _get_industry,
)

PF_HISTORY = os.path.join(paths.CACHES_DIR, "price_factor_history.json")
CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
HOLD_DAYS = 30

# 召回规模: None = 全池
TOP_N_VALUES: List = [50, 100, 150, 200, 300, None]


def spearman(a, b):
    def ranks(vals):
        si = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0] * len(vals)
        for pos, i in enumerate(si):
            r[i] = pos + 1
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra)
    db = sum((y - mb) ** 2 for y in rb)
    return num / ((da ** 0.5) * (db ** 0.5)) if da * db > 0 else 0.0


def real_returns(code, cutoff):
    suffix = "_SH.pkl" if code.startswith("6") else "_SZ.pkl"
    p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
    if not os.path.exists(p):
        return None
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    valid = df[df["trade_date"] <= cutoff]
    idx = len(valid) - 1
    end = idx + HOLD_DAYS
    if idx < 0 or end >= len(df):
        return None
    return round((df["close"].iloc[end] / df["close"].iloc[idx] - 1) * 100, 2)


def r5r20_at(code, cutoff):
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if not os.path.exists(p):
            continue
        try:
            df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
            df = df[df["trade_date"] <= cutoff]
            if len(df) < 21:
                return None
            close = df["close"]
            r20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
            r5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0
            return r5, r20
        except Exception:
            return None
    return None


def price_factor_g(r5, r20):
    if r20 > 20:
        return 1.3 if r5 > 5 else (0.9 if r5 < -5 else 1.1)
    elif r20 > 0:
        return (1.0 + r20 * 0.01) if r5 > 0 else 0.9
    elif r20 > -10:
        return 0.9 if r5 > 0 else 0.7
    else:
        return 0.6


def d2_factor(r20, sector_median):
    if sector_median is None:
        return 1.0
    if r20 > sector_median + 15:
        return 1.15
    elif r20 < sector_median - 10:
        return 0.85
    return 1.0


def classify(industry, kw_index):
    if not industry:
        return ""
    best, best_hit, best_kw_len = "", 0, 0
    for sec, kws in kw_index.items():
        matched = [k for k in kws if k in industry]
        h = len(matched)
        if h <= 0:
            continue
        max_kw_len = max(len(k) for k in matched)
        if h > best_hit or (h == best_hit and max_kw_len > best_kw_len):
            best_hit, best_kw_len, best = h, max_kw_len, sec
    return best


def main():
    from tradingagents.research.normalize import get_sector_keyword_index
    from tradingagents.research.consumer import get_sector_momentum

    cap_history = json.load(open(CAP_HISTORY, encoding="utf-8"))
    cutoffs = sorted(cap_history.keys())

    kw_index = get_sector_keyword_index()

    # 预读 industry
    industry_map = {code: _get_industry(code) for code in V3
                    if isinstance(V3[code], dict) and "chain" in V3[code]}
    sector_map = {code: classify(ind, kw_index) for code, ind in industry_map.items()}

    print("=" * 78)
    print("  召回阶段消融实验 (G模式无封顶): 全池 vs 召回筛选")
    print("=" * 78)
    print(f"  cutoff 数: {len(cutoffs)} | 召回规模: {TOP_N_VALUES}")
    print(f"  召回排序: chain+capital | 最终锚: chain+capital×2+surge×SURGE_WEIGHT\n")

    # results[top_n] = {rhos, top10, pool_sizes, hit_in_top10}
    results = {n: {"top10": [], "pool_sizes": [], "rhos": []} for n in TOP_N_VALUES}

    for ci, cutoff in enumerate(cutoffs, 1):
        momentum = get_sector_momentum(cutoff_date=cutoff, days=14)
        if not momentum.get("hot_sectors"):
            continue

        # G 模式 capital (base + d2×2 + pf×2 无封顶), cutoff 化无前视
        cap_cache = compute_capital_updates(cutoff_date=cutoff)
        cap_dict = cap_cache[0] if cap_cache else {}
        if not cap_dict:
            continue

        # 算全池 capital + 收益
        all_stocks = {}  # code -> {ret, chain, surge, capital, recall_key}
        for code in industry_map:
            capital = cap_dict.get(code, {}).get("capital")
            if capital is None:
                continue
            ret = real_returns(code, cutoff)
            if ret is None:
                continue
            v = V3[code]
            all_stocks[code] = {
                "ret": ret, "chain": v.get("chain", 0),
                "surge": v.get("surge", 0),
                "capital": capital,
                "recall_key": v.get("chain", 0) + capital,  # 召回排序键
            }
        if len(all_stocks) < 30:
            continue

        # 召回排序 (chain+capital 降序)
        recall_order = sorted(all_stocks.items(), key=lambda x: -x[1]["recall_key"])

        for top_n in TOP_N_VALUES:
            if top_n is None:
                pool = recall_order  # 全池
            else:
                pool = recall_order[:top_n]
            results[top_n]["pool_sizes"].append(len(pool))

            # 在召回子集内按锚排序
            anchors = []
            rets = []
            for code, sd in pool:
                anchor = sd["chain"] + sd["capital"] * 2 - sd["surge"] * 0.5
                anchors.append(anchor)
                rets.append(sd["ret"])
            if len(anchors) < 10:
                continue
            # Spearman (子集内的排序预测力)
            results[top_n]["rhos"].append(spearman(anchors, rets))
            # TOP10 涨幅 (策略实际收益)
            order = sorted(range(len(anchors)), key=lambda i: -anchors[i])[:10]
            top_rets = [rets[i] for i in order]
            results[top_n]["top10"].append(sum(top_rets) / len(top_rets))

        if ci % 30 == 0:
            print(f"  ... {ci}/{len(cutoffs)}")

    n_periods = len(results[None]["rhos"])
    full_top10 = sum(results[None]["top10"]) / n_periods
    full_rho = sum(results[None]["rhos"]) / n_periods

    print(f"\n  有效期数: {n_periods} (基线=全池)\n")
    print(f"  {'召回规模':<10}{'池规模':>8}{'Spearman':>10}{'Δρ':>9}"
          f"{'TOP10涨':>10}{'Δ涨':>9}{'判定':>8}")
    print("  " + "-" * 62)

    for top_n in TOP_N_VALUES:
        r = results[top_n]
        n = len(r["rhos"])
        rho_avg = sum(r["rhos"]) / n
        top10_avg = sum(r["top10"]) / n
        pool_avg = sum(r["pool_sizes"]) / len(r["pool_sizes"])
        d_rho = rho_avg - full_rho
        d_top10 = top10_avg - full_top10
        label = "全池" if top_n is None else f"top{top_n}"
        if top_n is None:
            flag = "★基线"
        elif d_top10 > 0.5:
            flag = "✅更好"
        elif d_top10 > -0.5:
            flag = "≈持平"
        else:
            flag = "✗更差"
        print(f"  {label:<10}{pool_avg:>8.0f}{rho_avg:>+10.3f}{d_rho:>+9.4f}"
              f"{top10_avg:>+10.2f}{d_top10:>+9.2f}{flag:>8}")

    print(f"\n{'='*78}")
    print("  结论")
    print(f"{'='*78}")
    best = max(TOP_N_VALUES, key=lambda n: sum(results[n]["top10"]) / len(results[n]["top10"]))
    best_label = "全池" if best is None else f"top{best}"
    best_top10 = sum(results[best]["top10"]) / len(results[best]["top10"])
    if best is None:
        print(f"  TOP10涨幅最优: 全池 ({best_top10:+.2f}%) → 维持 top_n=9999 (全池)")
        print(f"  验证了历史结论: 召回预筛在G模式下帮倒忙")
    else:
        print(f"  TOP10涨幅最优: {best_label} ({best_top10:+.2f}%, 全池 {full_top10:+.2f}%)")
        full_vs_best = full_top10 - best_top10
        if full_vs_best < -0.5:
            print(f"  → 全池更差 {abs(full_vs_best):.2f}pp, 召回筛选 {best_label} 更优")
        else:
            print(f"  → 两者接近, 维持全池 (更简单)")


if __name__ == "__main__":
    main()
