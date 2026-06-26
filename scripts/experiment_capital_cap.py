#!/usr/bin/env python3
"""封顶机制实验 (G 模式): 扫描不同 capital 封顶值对排序质量的影响。

背景:
  生产 G 模式 capital = round(min(8.0, base + d2×2 + pf×2), 1)
  16.8% 的股撞顶到 8.0 (89只), 与主升浪强势股拿相同 capital, 抹平区分度。
  D 模式回测测不出 (D模式 max capital=6.5, 封顶7以上无效), 必须用 G 模式。

本实验:
  - 完整 G 模式 capital: base + d2×2 + pf×2, 全部 cutoff 化 (无前视)
  - d2 复用 build_price_factor_history 的行业 r20 中位数算法
  - 扫描封顶值: 6/7/8/10/12/15/无封顶 (base=5.0时 G模式理论max=9.9)
  - 对比 Spearman + TOP10 涨幅 + 撞顶股比例

用法:
  uv run python3 scripts/experiment_capital_cap.py
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
    KLINE_CACHE_DIR, compute_capital_updates,
)

PF_HISTORY = os.path.join(paths.CACHES_DIR, "price_factor_history.json")
CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
HOLD_DAYS = 30

# 待测封顶值: None = 无封顶 (G模式理论max≈9.9)
CAP_VALUES: List = [6.0, 7.0, 8.0, 10.0, 12.0, 15.0, None]


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


def r5r20_at(code: str, cutoff: str) -> Optional[tuple]:
    """cutoff 截断的 r5/r20。"""
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


def price_factor_g(r5: float, r20: float) -> float:
    """G 模式 pf (与 v3_full_score._compute_price_factor 一致)。"""
    if r20 > 20:
        return 1.3 if r5 > 5 else (0.9 if r5 < -5 else 1.1)
    elif r20 > 0:
        return (1.0 + r20 * 0.01) if r5 > 0 else 0.9
    elif r20 > -10:
        return 0.9 if r5 > 0 else 0.7
    else:
        return 0.6


def d2_factor(r20: float, sector_median: Optional[float]) -> float:
    """G 模式 d2 (与 v3_full_score._compute_d2_factor 一致)。"""
    if sector_median is None:
        return 1.0
    if r20 > sector_median + 15:
        return 1.15
    elif r20 < sector_median - 10:
        return 0.85
    return 1.0


def classify(industry, kw_index):
    """平局裁决的 classify (与 v3_full_score._classify_sector 一致)。"""
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
    pf_history = json.load(open(PF_HISTORY, encoding="utf-8"))
    cap_history = json.load(open(CAP_HISTORY, encoding="utf-8"))
    cutoffs = sorted(set(pf_history.keys()) & set(cap_history.keys()))

    print("=" * 76)
    print("  封顶机制实验 (G 模式: base + d2×2 + pf×2)")
    print("=" * 76)
    print(f"  cutoff 数: {len(cutoffs)} | 待测封顶: {CAP_VALUES}")
    print(f"  anchor = chain + capital×2 + surge×SURGE_WEIGHT\n")

    results = {cap: {"rhos": [], "top10": [], "capped_ratios": []} for cap in CAP_VALUES}

    for ci, cutoff in enumerate(cutoffs, 1):
        # G 模式 capital: 调 compute_capital_updates 取无封顶 capital (base+d2×2+pf×2)
        # base 自然含板块动量, 不需要 sub_sector_override
        result = compute_capital_updates(cutoff_date=cutoff)
        if result is None:
            continue
        cap_cache = result[0]
        momentum = result[2]
        if not momentum.get("hot_sectors"):
            continue

        # 拿 G capital → 后续施加封顶变体 → anchor → Spearman
        stock_data = {}
        for code, v in cap_cache.items():
            if not isinstance(v, dict) or "chain" not in v:
                continue
            ret = real_returns(code, cutoff)
            if ret is None:
                continue
            stock_data[code] = {
                "ret": ret, "chain": v.get("chain", 0),
                "surge": v.get("surge", 0),
                "raw_capital": v.get("capital", 0),
            }
        if len(stock_data) < 10:
            continue

        rets = [sd["ret"] for sd in stock_data.values()]
        raw_caps = [sd["raw_capital"] for sd in stock_data.values()]

        for cap in CAP_VALUES:
            anchors = []
            for sd in stock_data.values():
                c = sd["raw_capital"]
                c = min(c, cap) if cap is not None else c
                c = max(0.0, c)
                anchors.append(sd["chain"] + c * 2 - sd["surge"] * 0.5)
            rho = spearman(anchors, rets)
            results[cap]["rhos"].append(rho)
            order = sorted(range(len(anchors)), key=lambda i: -anchors[i])[:10]
            results[cap]["top10"].append(sum(rets[i] for i in order) / 10)
            if cap is not None:
                capped = sum(1 for c in raw_caps if c > cap)
                results[cap]["capped_ratios"].append(capped / len(raw_caps))
            else:
                results[cap]["capped_ratios"].append(0.0)

        if ci % 25 == 0:
            print(f"  ... {ci}/{len(cutoffs)}")

    n_periods = len(results[8.0]["rhos"])
    base_rho = sum(results[8.0]["rhos"]) / n_periods
    base_top10 = sum(results[8.0]["top10"]) / n_periods

    print(f"\n  有效期数: {n_periods} (基线=min(8.0), 即当前生产值)\n")
    print(f"  {'封顶值':<10}{'Spearman':>10}{'Δρ(vs8.0)':>12}{'min':>7}"
          f"{'TOP10涨':>10}{'Δ涨':>9}{'撞顶%':>8}{'判定':>8}")
    print("  " + "-" * 72)

    for cap in CAP_VALUES:
        r = results[cap]
        rho_avg = sum(r["rhos"]) / n_periods
        rho_min = min(r["rhos"])
        top10_avg = sum(r["top10"]) / n_periods
        capped_avg = sum(r["capped_ratios"]) / n_periods * 100
        d_rho = rho_avg - base_rho
        d_top10 = top10_avg - base_top10
        label = "无封顶" if cap is None else f"min({cap:.0f})"
        if cap == 8.0:
            flag = "★基线"
        elif d_rho > 0.005 and d_top10 >= 0:
            flag = "✅更好"
        elif d_rho > 0:
            flag = "⚠略升"
        elif d_rho < -0.005:
            flag = "✗下降"
        else:
            flag = "≈持平"
        print(f"  {label:<10}{rho_avg:>+10.3f}{d_rho:>+12.4f}{rho_min:>+7.2f}"
              f"{top10_avg:>+10.2f}{d_top10:>+9.2f}{capped_avg:>7.1f}%{flag:>8}")

    print(f"\n{'='*76}")
    print("  结论")
    print(f"{'='*76}")
    ranked = sorted(CAP_VALUES, key=lambda c: -(sum(results[c]["rhos"]) / n_periods))
    best = ranked[0]
    best_label = "无封顶" if best is None else f"min({best:.0f})"
    best_rho = sum(results[best]["rhos"]) / n_periods
    if best != 8.0:
        print(f"  Spearman 最优: {best_label} (ρ={best_rho:+.3f}, 高于 min(8.0) 的 {base_rho:+.3f})")
        print(f"  → 建议改生产封顶: 8.0 → {best_label}")
    else:
        print(f"  Spearman 最优: {best_label} (ρ={best_rho:+.3f}), 维持 min(8.0)")


if __name__ == "__main__":
    main()
