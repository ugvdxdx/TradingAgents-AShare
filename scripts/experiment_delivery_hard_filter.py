#!/usr/bin/env python3
"""delivery<2 硬控影响分析: 这些股进入 TOP5/10 后的表现。

只看实际进入 TOP5/10 的低 delivery 股, 不看全池 (能影响策略的只有头部)。
对比: 不加硬控 vs 加硬控(delivery<2 排除出TOP10) 的 TOP5/10 收益。
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
    KLINE_CACHE_DIR, _compute_capital_from_momentum, _get_industry,
    _load_sub_sector_override,
)

CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
HOLD = 30


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

    cap_hist = json.load(open(CAP_HISTORY, encoding="utf-8"))
    cutoffs = sorted(cap_hist.keys())

    kw_index = get_sector_keyword_index()
    override_sorted = sorted(_load_sub_sector_override().items(), key=lambda x: -len(x[0]))
    industry_map = {code: _get_industry(code) for code in V3
                    if isinstance(V3[code], dict) and "chain" in V3[code]}
    sector_map = {code: classify(ind, kw_index) for code, ind in industry_map.items()}

    print("=" * 78)
    print("  delivery<2 硬控影响分析 (只看实际进入 TOP5/10 的低 delivery 股)")
    print("=" * 78)
    print(f"  cutoff 数: {len(cutoffs)} | 验证窗口: {HOLD} 日\n")

    # 每个 cutoff 算排序 + TOP5/10, 记录低 delivery 股的入选情况和收益
    low_dl_in_top5 = []   # (cutoff, code, delivery, ret30)
    low_dl_in_top10 = []

    # 两种方案的 TOP5/10 收益
    top5_no_filter = []   # 不加硬控
    top5_filter = []      # delivery<2 排除后重新取 TOP5
    top10_no_filter = []
    top10_filter = []

    for cutoff in cutoffs:
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
            r = ret_n(code, cutoff, HOLD)
            if r is None:
                continue
            v = V3[code]
            r5, r20 = rv
            pf = (1.3 if r20 > 20 and r5 > 5 else (0.9 if r20 > 20 and r5 < -5 else (1.1 if r20 > 20 else
                  ((1.0 + r20 * 0.01) if r20 > 0 and r5 > 0 else (0.9 if r20 > 0 else
                  (0.9 if r20 > -10 and r5 > 0 else (0.7 if r20 > -10 else 0.6)))))))
            sec = sector_map.get(code, "")
            base = _compute_capital_from_momentum(sec, momentum)
            for kw, cv in override_sorted:
                if kw in industry_map[code]:
                    base = cv
                    break
            d2 = 1.15 if sector_median.get(sec) and r20 > sector_median[sec] + 15 else (
                 0.85 if sector_median.get(sec) and r20 < sector_median[sec] - 10 else 1.0)
            capital = max(0, base + d2 * 2 + pf * 2)
            anchor = v.get("chain", 0) + capital * 2 - v.get("delivery", 0) * 0.5
            stock_data[code] = {"anchor": anchor, "delivery": v.get("delivery", 0),
                                "ret": r, "name": v.get("name", "")}

        if len(stock_data) < 20:
            continue

        ranked = sorted(stock_data.items(), key=lambda x: -x[1]["anchor"])

        # 不加硬控的 TOP5/10
        t5_nf = ranked[:5]
        t10_nf = ranked[:10]
        # 加硬控: 排除 delivery<2 后重新取
        ranked_f = [x for x in ranked if x[1]["delivery"] >= 2.0]
        t5_f = ranked_f[:5]
        t10_f = ranked_f[:10]

        # 记录低 delivery 股的入选
        for code, d in t5_nf:
            if d["delivery"] < 2.0:
                low_dl_in_top5.append((cutoff, code, d["delivery"], d["ret"], d["name"]))
        for code, d in t10_nf:
            if d["delivery"] < 2.0:
                low_dl_in_top10.append((cutoff, code, d["delivery"], d["ret"], d["name"]))

        # TOP5/10 收益 (不加 vs 加硬控)
        top5_no_filter.append(sum(d["ret"] for _, d in t5_nf) / len(t5_nf))
        top5_filter.append(sum(d["ret"] for _, d in t5_f) / len(t5_f))
        top10_no_filter.append(sum(d["ret"] for _, d in t10_nf) / len(t10_nf))
        top10_filter.append(sum(d["ret"] for _, d in t10_f) / len(t10_f))

    # ── 分析 1: 低 delivery 股进入 TOP5/10 的频率和表现 ──
    print("═" * 78)
    print("  分析 1: delivery<2 的股进入 TOP5/10 的实际情况")
    print("═" * 78)
    n_periods = len(top5_no_filter)
    print(f"\n  【TOP5】{len(low_dl_in_top5)} 只次入选 (across {n_periods} 期)")
    if low_dl_in_top5:
        rets = [x[3] for x in low_dl_in_top5]
        print(f"  30日均涨: {statistics.mean(rets):+.1f}% | 中位{statistics.median(rets):+.1f}% | 最差{min(rets):+.1f}%")
        print(f"  大涨>20%: {sum(1 for r in rets if r>20)}/{len(rets)} | 大跌<-15%: {sum(1 for r in rets if r<-15)}/{len(rets)}")
        print(f"  入选期数: {len(set(x[0] for x in low_dl_in_top5))}/{n_periods} ({len(set(x[0] for x in low_dl_in_top5))/n_periods*100:.0f}%)")
        # 列出具体股票
        print(f"\n  具体股票 (按出现次数):")
        from collections import Counter
        cnt = Counter((x[1], x[4]) for x in low_dl_in_top5)
        for (code, name), c in cnt.most_common(10):
            rts = [x[3] for x in low_dl_in_top5 if x[1] == code]
            print(f"    {code} {name[:10]:<10} 入选{c}次 | 涨幅{statistics.mean(rts):+.1f}% (最差{min(rts):+.1f}%)")

    print(f"\n  【TOP10】{len(low_dl_in_top10)} 只次入选")
    if low_dl_in_top10:
        rets = [x[3] for x in low_dl_in_top10]
        print(f"  30日均涨: {statistics.mean(rets):+.1f}% | 中位{statistics.median(rets):+.1f}% | 最差{min(rets):+.1f}%")

    # ── 分析 2: 加硬控 vs 不加硬控的 TOP5/10 收益对比 ──
    print(f"\n{'═'*78}")
    print("  分析 2: 加 delivery<2 硬控 vs 不加 (TOP5/10 收益对比)")
    print("═" * 78)
    for label, nf, f in [("TOP5", top5_no_filter, top5_filter), ("TOP10", top10_no_filter, top10_filter)]:
        avg_nf = statistics.mean(nf)
        avg_f = statistics.mean(f)
        med_nf = statistics.median(nf)
        med_f = statistics.median(f)
        min_nf = min(nf)
        min_f = min(f)
        pos_nf = sum(1 for r in nf if r > 0) / len(nf) * 100
        pos_f = sum(1 for r in f if r > 0) / len(f) * 100
        # 配对胜率
        win = sum(1 for a, b in zip(f, nf) if a > b + 0.5)
        lose = sum(1 for a, b in zip(f, nf) if a < b - 0.5)
        print(f"\n  【{label}】 不加硬控 vs 加硬控(delivery<2排除):")
        print(f"    均值:   {avg_nf:+.2f}% → {avg_f:+.2f}% (Δ{avg_f-avg_nf:+.2f}pp)")
        print(f"    中位:   {med_nf:+.2f}% → {med_f:+.2f}%")
        print(f"    最差期: {min_nf:+.2f}% → {min_f:+.2f}% (Δ{min_f-min_nf:+.2f}pp)")
        print(f"    正收益: {pos_nf:.0f}% → {pos_f:.0f}%")
        print(f"    配对胜率: 硬控胜{win}/负{lose} ({win/len(nf)*100:.0f}%)")


if __name__ == "__main__":
    main()
