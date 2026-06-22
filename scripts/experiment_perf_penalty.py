#!/usr/bin/env python3
"""串行软降权回测: 历史入选表现差的股降权 (无前视偏差)。

规则:
  维护 perf_history[code] = [历史入选的30日涨幅]
  截至当前 cutoff, 若某股:
    - 入选次数 >= MIN_APPEARANCES (3)
    - 历史均涨 < THRESHOLD (0%)
  则锚分扣 PENALTY 分 (1.0/2.0), 仍可能进TOP5但排名下降。

串行: 每个 cutoff 严格只用之前的历史, 不看未来 (无前视)。

对比: 无降权 vs 降权(penalty=1/2) 的月化收益 + 分月。
"""
import json
import os
import pickle
import statistics
import sys
from collections import defaultdict
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
MIN_APPEARANCES = 3   # 至少入选N次才降权
THRESHOLD = 0.0       # 历史均涨低于此值才降权
PENALTIES = [0, 1.0, 2.0]  # 0=无降权(基线), 1.0/2.0=降权力度


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


def price_at(code, date):
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                row = df[df["trade_date"] <= date]
                if len(row) == 0:
                    return None
                return float(row.iloc[-1]["close"])
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


def count_trading_days(d1, d2):
    p = os.path.join(KLINE_CACHE_DIR, "000001_SZ.pkl")
    if not os.path.exists(p):
        return 2
    try:
        df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
        return max(len(df[(df["trade_date"] > d1) & (df["trade_date"] <= d2)]), 1)
    except Exception:
        return 2


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

    # 预计算每个 cutoff 的基础锚分 (不含降权) + 30日涨幅
    print("=" * 78)
    print("  串行软降权回测 (历史表现降权, 无前视)")
    print("=" * 78)
    print(f"  cutoff: {len(cutoffs)} 期 | 规则: 入选≥{MIN_APPEARANCES}次且历史均涨<{THRESHOLD}% → 降权")

    # base_anchors[cutoff] = {code: {anchor, ret}}
    base_anchors = {}
    for ci, cutoff in enumerate(cutoffs, 1):
        mom = get_sector_momentum(days=14)
        if not mom.get("hot_sectors"):
            continue
        sr20: Dict[str, list] = {}
        cr: Dict[str, tuple] = {}
        for code in industry_map:
            rv = r5r20_at(code, cutoff)
            if rv is None:
                continue
            cr[code] = rv
            s = sector_map.get(code)
            if s:
                sr20.setdefault(s, []).append(rv[1])
        sm = {s: statistics.median(v) for s, v in sr20.items() if v}

        sd = {}
        for code in industry_map:
            rv = cr.get(code)
            if rv is None:
                continue
            v = V3[code]
            r5, r20 = rv
            pf = (1.3 if r20 > 20 and r5 > 5 else (0.9 if r20 > 20 and r5 < -5 else (1.1 if r20 > 20 else
                  ((1.0 + r20 * 0.01) if r20 > 0 and r5 > 0 else (0.9 if r20 > 0 else
                  (0.9 if r20 > -10 and r5 > 0 else (0.7 if r20 > -10 else 0.6)))))))
            s = sector_map.get(code, "")
            base = _compute_capital_from_momentum(s, mom)
            for kw, cv in override_sorted:
                if kw in industry_map[code]:
                    base = cv
                    break
            d2 = 1.15 if sm.get(s) and r20 > sm[s] + 15 else (
                 0.85 if sm.get(s) and r20 < sm[s] - 10 else 1.0)
            capital = max(0, base + d2 * 2 + pf * 2)
            anchor = v.get("chain", 0) + capital * 2 - v.get("delivery", 0) * 0.5
            sd[code] = anchor
        base_anchors[cutoff] = sd
        if ci % 40 == 0:
            print(f"  ... 基础锚分 {ci}/{len(cutoffs)}")

    # 串行模拟: 对每个 penalty 值, 维护 perf_history 并算 TOP5
    valid_cutoffs = sorted(base_anchors.keys())
    results = {p: {"monthly_data": defaultdict(lambda: {"ret": 1.0, "days": 0}),
                   "cumulative": 1.0, "period_rets": [], "n_buys": 0, "days": 0,
                   "holdings": {}, "miss": {}, "perf": defaultdict(list),
                   "penalized_count": 0}
               for p in PENALTIES}

    for i, cutoff in enumerate(valid_cutoffs):
        # 算下一 cutoff 的收益区间
        next_cutoff = valid_cutoffs[i + 1] if i + 1 < len(valid_cutoffs) else None
        gap_days = count_trading_days(cutoff, next_cutoff) if next_cutoff else 0

        for penalty in PENALTIES:
            r = results[penalty]
            # 用截至当前的历史表现算降权
            penalized = {}
            for code in base_anchors[cutoff]:
                hist = r["perf"].get(code, [])
                if len(hist) >= MIN_APPEARANCES and statistics.mean(hist) < THRESHOLD:
                    penalized[code] = penalty
            # 算降权后的锚分
            scored = []
            for code, anchor in base_anchors[cutoff].items():
                adj_anchor = anchor - penalized.get(code, 0)
                scored.append((code, adj_anchor))
            scored.sort(key=lambda x: -x[1])
            top5 = set(c for c, _ in scored[:5])

            # 买1卖2 持仓更新
            for code in list(r["holdings"].keys()):
                if code not in top5:
                    r["miss"][code] = r["miss"].get(code, 0) + 1
                    if r["miss"][code] >= 2:
                        del r["holdings"][code]
                else:
                    r["miss"][code] = 0
            new_buys = [c for c in top5 if c not in r["holdings"]]
            for c in new_buys:
                r["holdings"][c] = cutoff
                r["n_buys"] += 1
            r["penalized_count"] += len(penalized)

            # 持仓收益
            if next_cutoff and r["holdings"]:
                port_rets = []
                for code in r["holdings"]:
                    p0 = price_at(code, cutoff)
                    p1 = price_at(code, next_cutoff)
                    if p0 and p1 and p0 > 0:
                        port_rets.append(p1 / p0 - 1)
                if port_rets:
                    period_ret = sum(port_rets) / len(port_rets)
                    r["cumulative"] *= (1 + period_ret)
                    r["period_rets"].append(period_ret)
                    r["days"] += gap_days
                    month = cutoff[:7]
                    r["monthly_data"][month]["ret"] *= (1 + period_ret)
                    r["monthly_data"][month]["days"] += gap_days
                    # 记录持仓股的收益到 perf_history (供未来 cutoff 用)
                    for code in r["holdings"]:
                        p0 = price_at(code, cutoff)
                        p1 = price_at(code, next_cutoff)
                        if p0 and p1 and p0 > 0:
                            r["perf"][code].append((p1 / p0 - 1) * 100)

    # 输出
    print(f"\n{'='*78}")
    print(f"  对比 (买1卖2 持仓轮动)")
    print(f"{'='*78}")
    print(f"  {'降权':<12}{'累计收益':>10}{'月化':>9}{'换手':>8}{'正收益':>8}{'最差期':>9}{'σ':>7}{'降权股次':>9}")
    print("  " + "-" * 72)
    for penalty in PENALTIES:
        r = results[penalty]
        cum = r["cumulative"]
        n_months = r["days"] / 21
        monthly = (cum ** (1 / max(n_months, 0.01)) - 1) * 100 if cum > 0 else -100
        pr = r["period_rets"]
        avg_turnover = r["n_buys"] / max(len(valid_cutoffs), 1)
        pos = sum(1 for x in pr if x > 0) / max(len(pr), 1) * 100
        mn = min(pr) * 100 if pr else 0
        std = statistics.stdev(pr) * 100 if len(pr) > 1 else 0
        label = f"无降权(基线)" if penalty == 0 else f"扣{penalty:.0f}分"
        print(f"  {label:<12}{(cum-1)*100:>+10.1f}{monthly:>+9.2f}{avg_turnover:>8.2f}{pos:>7.0f}%{mn:>+9.2f}{std:>7.2f}{r['penalized_count']:>9}")

    # 分月对比 (无降权 vs 扣2分)
    print(f"\n  ── 分月对比 (无降权 vs 扣2分) ──")
    print(f"  {'月份':<9}{'无降权':>10}{'扣2分':>10}{'差值':>9}")
    print("  " + "-" * 40)
    all_months = sorted(set(results[0]["monthly_data"].keys()) | set(results[2.0]["monthly_data"].keys()))
    for m in all_months:
        r0 = results[0]["monthly_data"].get(m, {"ret": 1.0})
        r2 = results[2.0]["monthly_data"].get(m, {"ret": 1.0})
        ret0 = (r0["ret"] - 1) * 100
        ret2 = (r2["ret"] - 1) * 100
        diff = ret2 - ret0
        flag = " ✅" if diff > 1 else (" ✗" if diff < -1 else "")
        print(f"  {m:<9}{ret0:>+10.2f}{ret2:>+10.2f}{diff:>+9.2f}{flag}")


if __name__ == "__main__":
    main()
