#!/usr/bin/env python3
"""delivery 权重消融实验: 扫描锚公式里 delivery 的权重系数。

背景:
  当前锚 = chain + capital×2 - delivery×0.5 (delivery 负权重)
  但回测显示全池 delivery vs 30日涨幅 Spearman = +0.082 (正向! 95/125 期正向)
  负权重的依据是"新晋股子池"(-0.33), 被错误推广到全池。

本实验:
  - G 模式 capital (无封顶, cutoff 化)
  - 扫描 delivery 权重: -0.5(当前) / -0.2 / 0 / +0.3 / +0.5
  - 锚 = chain + capital×2 + delivery×W
  - 对比 Spearman + TOP10 涨幅 + 最差期

用法:
  uv run python3 scripts/experiment_delivery_weight.py
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
    KLINE_CACHE_DIR, _compute_capital_from_momentum, _get_industry,
    _load_sub_sector_override,
)

PF_HISTORY = os.path.join(paths.CACHES_DIR, "price_factor_history.json")
CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
HOLD_DAYS = 30

WEIGHTS = [-0.5, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0]


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
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if os.path.exists(p):
            df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
            valid = df[df["trade_date"] <= cutoff]
            idx = len(valid) - 1
            end = idx + HOLD_DAYS
            if idx < 0 or end >= len(df):
                return None
            return round((df["close"].iloc[end] / df["close"].iloc[idx] - 1) * 100, 2)
    return None


def r5r20_at(code, cutoff):
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if os.path.exists(p):
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
    override_sorted = sorted(_load_sub_sector_override().items(), key=lambda x: -len(x[0]))

    industry_map = {code: _get_industry(code) for code in V3
                    if isinstance(V3[code], dict) and "chain" in V3[code]}
    sector_map = {code: classify(ind, kw_index) for code, ind in industry_map.items()}

    print("=" * 78)
    print("  delivery 权重消融实验 (G模式无封顶)")
    print("=" * 78)
    print(f"  cutoff 数: {len(cutoffs)} | 锚 = chain + capital×2 + delivery×W")
    print(f"  权重 W: {WEIGHTS}\n")

    results = {w: {"rhos": [], "top5": [], "top10": []} for w in WEIGHTS}

    for ci, cutoff in enumerate(cutoffs, 1):
        momentum = get_sector_momentum(days=14)
        if not momentum.get("hot_sectors"):
            continue

        sector_r20s: Dict[str, list] = {}
        code_r5r20: Dict[str, tuple] = {}
        for code in industry_map:
            rv = r5r20_at(code, cutoff)
            if rv is None:
                continue
            code_r5r20[code] = rv
            sec = sector_map.get(code)
            if sec:
                sector_r20s.setdefault(sec, []).append(rv[1])
        sector_median = {sec: statistics.median(v) for sec, v in sector_r20s.items() if v}

        stock_data = {}
        for code in industry_map:
            rv = code_r5r20.get(code)
            if rv is None:
                continue
            ret = real_returns(code, cutoff)
            if ret is None:
                continue
            v = V3[code]
            r5, r20 = rv
            pf = price_factor_g(r5, r20)
            sec = sector_map.get(code, "")
            base = _compute_capital_from_momentum(sec, momentum)
            for keyword, cap_val in override_sorted:
                if keyword in industry_map[code]:
                    base = cap_val
                    break
            d2 = d2_factor(r20, sector_median.get(sec))
            capital = max(0, base + d2 * 2 + pf * 2)
            stock_data[code] = {
                "ret": ret, "chain": v.get("chain", 0),
                "delivery": v.get("delivery", 0), "capital": capital,
            }
        if len(stock_data) < 10:
            continue

        rets = [sd["ret"] for sd in stock_data.values()]
        for w in WEIGHTS:
            anchors = []
            for sd in stock_data.values():
                anchors.append(sd["chain"] + sd["capital"] * 2 + sd["delivery"] * w)
            rho = spearman(anchors, rets)
            results[w]["rhos"].append(rho)
            order = sorted(range(len(anchors)), key=lambda i: -anchors[i])
            top5_idx = order[:5]
            top10_idx = order[:10]
            results[w]["top5"].append(sum(rets[i] for i in top5_idx) / len(top5_idx))
            results[w]["top10"].append(sum(rets[i] for i in top10_idx) / len(top10_idx))

        if ci % 30 == 0:
            print(f"  ... {ci}/{len(cutoffs)}")

    n_periods = len(results[-0.5]["rhos"])
    base_rho = sum(results[-0.5]["rhos"]) / n_periods
    base_top5 = sum(results[-0.5]["top5"]) / n_periods
    base_top10 = sum(results[-0.5]["top10"]) / n_periods

    print(f"\n  有效期数: {n_periods} (基线 W=-0.5, 即当前生产值)\n")
    print("  ── 均值与稳定性 ──")
    print(f"  {'权重W':<9}{'Spearman':>9}{'TOP5均':>9}{'TOP5中位':>9}{'TOP5最差':>9}"
          f"{'TOP5σ':>7}{'正收益%':>8}{'判定':>8}")
    print("  " + "-" * 68)

    for w in WEIGHTS:
        r = results[w]
        rho_avg = sum(r["rhos"]) / n_periods
        top5_avg = sum(r["top5"]) / n_periods
        top5_med = statistics.median(r["top5"])
        top5_min = min(r["top5"])
        top5_std = statistics.stdev(r["top5"])
        pos_rate = sum(1 for x in r["top5"] if x > 0) / n_periods * 100
        d_rho = rho_avg - base_rho
        d_top5 = top5_avg - base_top5
        label = f"{w:+.1f}"
        if w == -0.5:
            flag = "★基线"
        elif d_top5 > 0.5 and top5_min >= min(results[-0.5]["top5"]) - 1:
            flag = "✅更好"
        elif d_top5 > 0.5:
            flag = "⚠涨↑稳↓"
        elif d_top5 < -0.5:
            flag = "✗下降"
        else:
            flag = "≈持平"
        print(f"  {label:<9}{rho_avg:>+9.3f}{top5_avg:>+9.2f}{top5_med:>+9.2f}"
              f"{top5_min:>+9.2f}{top5_std:>7.2f}{pos_rate:>7.0f}%{flag:>8}")

    # 逐期配对对比: 各正权重 vs W=-0.5 (TOP5涨幅)
    print(f"\n  ── 逐期配对对比 (各 W vs W=-0.5, TOP5涨幅) ──")
    base_list = results[-0.5]["top5"]
    for w in WEIGHTS:
        if w == -0.5:
            continue
        w_list = results[w]["top5"]
        win = sum(1 for a, b in zip(w_list, base_list) if a > b + 0.5)
        lose = sum(1 for a, b in zip(w_list, base_list) if a < b - 0.5)
        tie = n_periods - win - lose
        print(f"  W={w:+.1f}: 胜 {win}/{lose} 负 / {tie} 平 "
              f"(胜率 {win/n_periods*100:.0f}%)")

    print(f"\n{'='*78}")
    print("  结论")
    print(f"{'='*78}")
    best_rho_w = max(WEIGHTS, key=lambda w: sum(results[w]["rhos"]) / n_periods)
    best_t5_w = max(WEIGHTS, key=lambda w: sum(results[w]["top5"]) / n_periods)
    best_t10_w = max(WEIGHTS, key=lambda w: sum(results[w]["top10"]) / n_periods)
    print(f"  Spearman 最优:   W={best_rho_w:+.1f} (ρ={sum(results[best_rho_w]['rhos'])/n_periods:+.3f})")
    print(f"  TOP5涨幅最优:    W={best_t5_w:+.1f} (涨={sum(results[best_t5_w]['top5'])/n_periods:+.2f}%)")
    print(f"  TOP10涨幅最优:   W={best_t10_w:+.1f} (涨={sum(results[best_t10_w]['top10'])/n_periods:+.2f}%)")


if __name__ == "__main__":
    main()
