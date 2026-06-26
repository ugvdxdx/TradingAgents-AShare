#!/usr/bin/env python3
"""短期验证回测: 覆盖回测盲区 (2026-05~06), 用 10 日验证窗口。

标准回测用 30 日验证窗口, 最新 cutoff 只到 2026-05-07 (留 30 天 holdout)。
本脚本用 10 日窗口, 把 cutoff 推到 2026-06-04, 覆盖最近的行情。
代价: 10 日窗口噪声更大, 结论仅供参考。

对比 surge 权重 W=-0.5 vs W=+1.0 在最近 1.5 个月的表现。
"""
import json
import os
import pickle
import statistics
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths
from picker.scoring.v3_full_score import (
    KLINE_CACHE_DIR, compute_capital_updates, _get_industry,
)

CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
WEIGHTS = [-0.5, 1.0]
HOLD = 10  # 短验证窗口


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
                return ((close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0,
                        (close.iloc[-1] / close.iloc[-21] - 1) * 100)
            except Exception:
                return None
    return None


def ret_n(code, cutoff, days):
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                v = df[df["trade_date"] <= cutoff]
                idx = len(v) - 1
                end = idx + days
                if idx < 0 or end >= len(df):
                    return None
                return round((df["close"].iloc[end] / df["close"].iloc[idx] - 1) * 100, 2)
            except Exception:
                return None
    return None


def classify(industry, kw_index):
    if not industry:
        return ""
    best, best_hit, best_kw_len = "", 0, 0
    for sec, kws in kw_index.items():
        matched = [k for k in kws if k in industry]
        h = len(matched)
        if h <= 0:
            continue
        mkl = max(len(k) for k in matched)
        if h > best_hit or (h == best_hit and mkl > best_kw_len):
            best_hit, best_kw_len, best = h, mkl, sec
    return best


def main():
    from tradingagents.research.normalize import get_sector_keyword_index
    from tradingagents.research.consumer import get_sector_momentum

    kw_index = get_sector_keyword_index()
    industry_map = {code: _get_industry(code) for code in V3
                    if isinstance(V3[code], dict) and "chain" in V3[code]}
    sector_map = {code: classify(ind, kw_index) for code, ind in industry_map.items()}

    # 手动生成 cutoff: 从 capital_history 最后 cutoff 之后, 每 2 天一个
    cap_hist = json.load(open(CAP_HISTORY, encoding="utf-8"))
    base_dates = sorted(cap_hist.keys())
    # 用基准 K 线生成所有交易日, 取 5/7 之后能留 10 天的
    df = pickle.load(open(os.path.join(KLINE_CACHE_DIR, "300308_SZ.pkl"), "rb"))
    all_dates = sorted(df["trade_date"].unique())
    new_cutoffs = [d for d in all_dates
                   if d > base_dates[-1] and all_dates.index(d) <= len(all_dates) - HOLD - 1]
    # 合并: 原有最后几个 + 新的
    cutoffs = base_dates[-4:] + new_cutoffs[::2]  # 最后2个原有 + 新的每2天

    print("=" * 78)
    print(f"  短期验证回测 ({HOLD}日窗口) — 覆盖回测盲区")
    print("=" * 78)
    print(f"  cutoff: {cutoffs[0]} ~ {cutoffs[-1]} ({len(cutoffs)} 期)")
    print(f"  权重: {WEIGHTS} | 验证窗口: {HOLD} 日\n")

    top5_hist = {w: {} for w in WEIGHTS}
    for cutoff in cutoffs:
        momentum = get_sector_momentum(days=14)
        if not momentum.get("hot_sectors"):
            continue
        # G 模式 capital (base + d2×2 + pf×2 无封顶), cutoff 化无前视
        cap_cache = compute_capital_updates(cutoff_date=cutoff)
        cap_dict = cap_cache[0] if cap_cache else {}
        if not cap_dict:
            continue

        stock_data = {}
        for code in industry_map:
            capital = cap_dict.get(code, {}).get("capital")
            if capital is None:
                continue
            v = V3[code]
            stock_data[code] = {"chain": v.get("chain", 0), "surge": v.get("surge", 0), "capital": capital}

        if len(stock_data) < 10:
            continue
        for w in WEIGHTS:
            scored = sorted(stock_data.items(),
                            key=lambda x: -(x[1]["chain"] + x[1]["capital"] * 2 + x[1]["surge"] * w))
            top5_hist[w][cutoff] = [(c, d) for c, d in scored[:5]]

    # 算 TOP5 的 HOLD 日涨幅
    print(f"  {'cutoff':<12}{'W=-0.5 TOP5':>28}{'涨%':>7}{'W=+1.0 TOP5':>28}{'涨%':>7}{'差':>7}")
    print("  " + "-" * 90)
    for cutoff in cutoffs:
        if cutoff not in top5_hist[-0.5]:
            continue
        line = f"  {cutoff:<12}"
        for w in WEIGHTS:
            top5 = top5_hist[w][cutoff]
            rets = [ret_n(c, cutoff, HOLD) for c, _ in top5]
            rets = [r for r in rets if r is not None]
            avg = sum(rets) / len(rets) if rets else 0
            names = ",".join(f"{c}" for c, _ in top5[:3])
            line += f"{names:>28}{avg:>+7.1f}"
        r0 = sum(r for r in [ret_n(c, cutoff, HOLD) for c, _ in top5_hist[-0.5].get(cutoff, [])] if r is not None) / max(1, len([r for r in [ret_n(c, cutoff, HOLD) for c, _ in top5_hist[-0.5].get(cutoff, [])] if r is not None]))
        r1 = sum(r for r in [ret_n(c, cutoff, HOLD) for c, _ in top5_hist[1.0].get(cutoff, [])] if r is not None) / max(1, len([r for r in [ret_n(c, cutoff, HOLD) for c, _ in top5_hist[1.0].get(cutoff, [])] if r is not None]))
        line += f"{r1-r0:>+7.1f}"
        print(line)

    # 汇总
    print(f"\n{'='*78}")
    print(f"  汇总 ({HOLD}日窗口, {len(cutoffs)}期)")
    print(f"{'='*78}")
    for w in WEIGHTS:
        all_rets = []
        overlap_with_other = 0
        total = 0
        for cutoff in cutoffs:
            if cutoff not in top5_hist[w]:
                continue
            top5 = top5_hist[w][cutoff]
            rets = [ret_n(c, cutoff, HOLD) for c, _ in top5]
            rets = [r for r in rets if r is not None]
            if rets:
                all_rets.extend(rets)
                s1 = {c for c, _ in top5}
                s2 = {c for c, _ in top5_hist[-0.5 if w == 1.0 else 1.0].get(cutoff, [])}
                overlap_with_other += len(s1 & s2)
                total += len(s1)
        avg = sum(all_rets) / len(all_rets) if all_rets else 0
        pos = sum(1 for r in all_rets if r > 0)
        print(f"  W={w:+.1f}: {HOLD}日均涨{avg:+.2f}% | 正收益{pos}/{len(all_rets)}({pos/len(all_rets)*100:.0f}%) "
              f"| 与对方TOP5重叠{overlap_with_other}/{total}({overlap_with_other/max(total,1)*100:.0f}%)")


if __name__ == "__main__":
    main()
