#!/usr/bin/env python3
"""surge<2 硬控影响分析: 这些股进入 TOP5/10 后的表现。

只看实际进入 TOP5/10 的低 surge 股, 不看全池 (能影响策略的只有头部)。
对比: 不加硬控 vs 加硬控(surge<2 排除出TOP10) 的 TOP5/10 收益。
"""
import json
import os
import pickle
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths
from picker.scoring.v3_full_score import (
    KLINE_CACHE_DIR, compute_capital_updates, _get_industry,
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


def main():
    cap_hist = json.load(open(CAP_HISTORY, encoding="utf-8"))
    cutoffs = sorted(cap_hist.keys())

    industry_map = {code: _get_industry(code) for code in V3
                    if isinstance(V3[code], dict) and "chain" in V3[code]}

    print("=" * 78)
    print("  surge<2 硬控影响分析 (只看实际进入 TOP5/10 的低 surge 股)")
    print("=" * 78)
    print(f"  cutoff 数: {len(cutoffs)} | 验证窗口: {HOLD} 日\n")

    # 每个 cutoff 算排序 + TOP5/10, 记录低 surge 股的入选情况和收益
    low_dl_in_top5 = []   # (cutoff, code, surge, ret30)
    low_dl_in_top10 = []

    # 两种方案的 TOP5/10 收益
    top5_no_filter = []   # 不加硬控
    top5_filter = []      # surge<2 排除后重新取 TOP5
    top10_no_filter = []
    top10_filter = []

    for cutoff in cutoffs:
        # G 模式 capital (base+d2×2+pf×2 无封顶), cutoff 化无前视。
        # compute_capital_updates 内部已含板块动量 base + d2 行业相对强度, 不再需要 sub_sector_override。
        cap_result = compute_capital_updates(cutoff_date=cutoff)
        cap_dict = cap_result[0] if cap_result else {}
        if not cap_dict:
            continue

        stock_data = {}
        for code in industry_map:
            cap_entry = cap_dict.get(code)
            if not isinstance(cap_entry, dict) or "capital" not in cap_entry:
                continue
            r = ret_n(code, cutoff, HOLD)
            if r is None:
                continue
            v = V3[code]
            capital = cap_entry["capital"]
            anchor = v.get("chain", 0) + capital * 2 - v.get("surge", 0) * 0.5
            stock_data[code] = {"anchor": anchor, "surge": v.get("surge", 0),
                                "ret": r, "name": v.get("name", "")}

        if len(stock_data) < 20:
            continue

        ranked = sorted(stock_data.items(), key=lambda x: -x[1]["anchor"])

        # 不加硬控的 TOP5/10
        t5_nf = ranked[:5]
        t10_nf = ranked[:10]
        # 加硬控: 排除 surge<2 后重新取
        ranked_f = [x for x in ranked if x[1]["surge"] >= 2.0]
        t5_f = ranked_f[:5]
        t10_f = ranked_f[:10]

        # 记录低 surge 股的入选
        for code, d in t5_nf:
            if d["surge"] < 2.0:
                low_dl_in_top5.append((cutoff, code, d["surge"], d["ret"], d["name"]))
        for code, d in t10_nf:
            if d["surge"] < 2.0:
                low_dl_in_top10.append((cutoff, code, d["surge"], d["ret"], d["name"]))

        # TOP5/10 收益 (不加 vs 加硬控)
        top5_no_filter.append(sum(d["ret"] for _, d in t5_nf) / len(t5_nf))
        top5_filter.append(sum(d["ret"] for _, d in t5_f) / len(t5_f))
        top10_no_filter.append(sum(d["ret"] for _, d in t10_nf) / len(t10_nf))
        top10_filter.append(sum(d["ret"] for _, d in t10_f) / len(t10_f))

    # ── 分析 1: 低 surge 股进入 TOP5/10 的频率和表现 ──
    print("═" * 78)
    print("  分析 1: surge<2 的股进入 TOP5/10 的实际情况")
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
    print("  分析 2: 加 surge<2 硬控 vs 不加 (TOP5/10 收益对比)")
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
        print(f"\n  【{label}】 不加硬控 vs 加硬控(surge<2排除):")
        print(f"    均值:   {avg_nf:+.2f}% → {avg_f:+.2f}% (Δ{avg_f-avg_nf:+.2f}pp)")
        print(f"    中位:   {med_nf:+.2f}% → {med_f:+.2f}%")
        print(f"    最差期: {min_nf:+.2f}% → {min_f:+.2f}% (Δ{min_f-min_nf:+.2f}pp)")
        print(f"    正收益: {pos_nf:.0f}% → {pos_f:.0f}%")
        print(f"    配对胜率: 硬控胜{win}/负{lose} ({win/len(nf)*100:.0f}%)")


if __name__ == "__main__":
    main()
