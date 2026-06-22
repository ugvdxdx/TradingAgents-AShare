#!/usr/bin/env python3
"""串行黑名单回测: 表现差的股彻底排除出 TOP5 (无前视)。

比软降权更激进: 一旦触发黑名单, 该股不能再进 TOP5。
测多个严格梯度, 找到有效的止损规则。

规则 (每个 code 维护历史入选收益列表):
  - consecutive: 连续N次入选涨幅都<0 → 拉黑
  - cumulative: 累计跌幅>X% → 拉黑 (止损)
  - highdl_loss: delivery≥7 且入选≥3次且均涨<0 → 拉黑 (针对高delivery毒瘤)
  - strict: 连续2次亏损 OR 累计跌10% → 拉黑 (最严)
"""
import json
import os
import pickle
import statistics
import sys
from collections import defaultdict
from typing import Dict, List, Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths
from picker.scoring.v3_full_score import (
    KLINE_CACHE_DIR, _compute_capital_from_momentum, _get_industry,
    _load_sub_sector_override,
)

CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
HOLD = 30


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
    if not os.path.exists(p) or not d2:
        return 2
    try:
        df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
        return max(len(df[(df["trade_date"] > d1) & (df["trade_date"] <= d2)]), 1)
    except Exception:
        return 2


# ── 黑名单规则定义 ──
# 每个规则: fn(code, history_rets, delivery) -> bool (True=拉黑)
# history_rets: 该股截至当前的所有历史入选30日涨幅(%)

def rule_none(code, hist, dl):
    return False

def rule_consec3(code, hist, dl):
    """连续3次亏损 → 拉黑"""
    return len(hist) >= 3 and all(r < 0 for r in hist[-3:])

def rule_consec2(code, hist, dl):
    """连续2次亏损 → 拉黑 (更严)"""
    return len(hist) >= 2 and all(r < 0 for r in hist[-2:])

def rule_cumul_loss10(code, hist, dl):
    """累计跌幅>10% → 拉黑 (止损)"""
    # 累计 = 连乘
    cum = 1.0
    for r in hist:
        cum *= (1 + r / 100)
    return len(hist) >= 2 and (cum - 1) * 100 < -10

def rule_cumul_loss5(code, hist, dl):
    """累计跌幅>5% → 拉黑 (更严止损)"""
    cum = 1.0
    for r in hist:
        cum *= (1 + r / 100)
    return len(hist) >= 2 and (cum - 1) * 100 < -5

def rule_highdl_loss(code, hist, dl):
    """delivery≥7 且入选≥3次且均涨<0 → 拉黑 (高delivery毒瘤)"""
    return dl >= 7.0 and len(hist) >= 3 and statistics.mean(hist) < 0

def rule_strict(code, hist, dl):
    """最严: 连续2次亏损 OR 累计跌10% OR (delivery≥7均涨<0)"""
    return rule_consec2(code, hist, dl) or rule_cumul_loss10(code, hist, dl) or rule_highdl_loss(code, hist, dl)


RULES = [
    ("无黑名单(基线)", rule_none),
    ("连续3次亏", rule_consec3),
    ("连续2次亏", rule_consec2),
    ("累计跌10%", rule_cumul_loss10),
    ("累计跌5%", rule_cumul_loss5),
    ("高DL毒瘤(dl≥7均涨<0)", rule_highdl_loss),
    ("最严(2亏/跌10%/高DL)", rule_strict),
]


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

    print("=" * 80)
    print("  串行黑名单回测 (表现差彻底排除, 无前视)")
    print("=" * 80)
    print(f"  cutoff: {len(cutoffs)} 期 | {len(RULES)} 种规则\n")

    # 预算每个 cutoff 的基础锚分
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
            sd[code] = v.get("chain", 0) + capital * 2 - v.get("delivery", 0) * 0.5
        base_anchors[cutoff] = sd
        if ci % 40 == 0:
            print(f"  ... 基础锚分 {ci}/{len(cutoffs)}")

    valid_cutoffs = sorted(base_anchors.keys())

    # 对每个规则串行模拟
    results = {}
    for rule_name, rule_fn in RULES:
        holdings: Dict[str, str] = {}
        miss: Dict[str, int] = {}
        perf: Dict[str, list] = defaultdict(list)
        blacklist: set = set()
        cumulative = 1.0
        period_rets = []
        n_buys = 0
        days_total = 0
        bl_count = 0  # 累计拉黑次数
        monthly = defaultdict(lambda: {"ret": 1.0, "days": 0})

        for i, cutoff in enumerate(valid_cutoffs):
            next_cutoff = valid_cutoffs[i + 1] if i + 1 < len(valid_cutoffs) else None
            gap_days = count_trading_days(cutoff, next_cutoff)

            # 更新黑名单: 用截至当前的历史
            for code in base_anchors[cutoff]:
                if code in blacklist:
                    continue
                dl = V3.get(code, {}).get("delivery", 0)
                if rule_fn(code, perf.get(code, []), dl):
                    blacklist.add(code)
                    bl_count += 1

            # 排除黑名单后排序
            scored = [(c, a) for c, a in base_anchors[cutoff].items() if c not in blacklist]
            scored.sort(key=lambda x: -x[1])
            top5 = set(c for c, _ in scored[:5])

            # 买1卖2
            for code in list(holdings.keys()):
                if code not in top5:
                    miss[code] = miss.get(code, 0) + 1
                    if miss[code] >= 2:
                        del holdings[code]
                else:
                    miss[code] = 0
            for c in top5:
                if c not in holdings:
                    holdings[c] = cutoff
                    n_buys += 1

            if next_cutoff and holdings:
                port_rets = []
                for code in holdings:
                    p0 = price_at(code, cutoff)
                    p1 = price_at(code, next_cutoff)
                    if p0 and p1 and p0 > 0:
                        port_rets.append(p1 / p0 - 1)
                if port_rets:
                    period_ret = sum(port_rets) / len(port_rets)
                    cumulative *= (1 + period_ret)
                    period_rets.append(period_ret)
                    days_total += gap_days
                    monthly[cutoff[:7]]["ret"] *= (1 + period_ret)
                    monthly[cutoff[:7]]["days"] += gap_days
                    for code in holdings:
                        p0 = price_at(code, cutoff)
                        p1 = price_at(code, next_cutoff)
                        if p0 and p1 and p0 > 0:
                            perf[code].append((p1 / p0 - 1) * 100)

        n_mo = days_total / 21
        monthly_ret = (cumulative ** (1 / max(n_mo, 0.01)) - 1) * 100 if cumulative > 0 else -100
        pos = sum(1 for r in period_rets if r > 0) / max(len(period_rets), 1) * 100
        mn = min(period_rets) * 100 if period_rets else 0
        std = statistics.stdev(period_rets) * 100 if len(period_rets) > 1 else 0
        turnover = n_buys / max(len(valid_cutoffs), 1)
        results[rule_name] = {
            "cumulative": cumulative, "monthly": monthly_ret, "turnover": turnover,
            "pos": pos, "min": mn, "std": std, "bl_count": bl_count,
            "blacklist_size": len(blacklist), "monthly_data": monthly,
        }

    # 输出汇总
    print(f"\n{'='*80}")
    print(f"  黑名单规则对比 (买1卖2)")
    print(f"{'='*80}")
    print(f"  {'规则':<22}{'累计':>9}{'月化':>8}{'换手':>7}{'正收益':>7}{'最差期':>8}{'σ':>6}{'拉黑':>5}{'黑名单':>7}")
    print("  " + "-" * 80)
    for rule_name, _ in RULES:
        r = results[rule_name]
        flag = " ★基线" if rule_name.startswith("无") else ""
        print(f"  {rule_name:<22}{(r['cumulative']-1)*100:>+9.1f}{r['monthly']:>+8.2f}{r['turnover']:>7.2f}"
              f"{r['pos']:>6.0f}%{r['min']:>+8.2f}{r['std']:>6.2f}{r['bl_count']:>5}{r['blacklist_size']:>7}{flag}")

    # 分月对比最优 vs 基线
    # 找月化最高的规则
    best_rule = max(RULES[1:], key=lambda x: results[x[0]]["monthly"])[0]
    print(f"\n  ── 分月对比: 基线 vs {best_rule} ──")
    print(f"  {'月份':<9}{'基线':>10}{best_rule:>18}{'差值':>9}")
    print("  " + "-" * 48)
    all_m = sorted(set(results[RULES[0][0]]["monthly_data"].keys()) |
                   set(results[best_rule]["monthly_data"].keys()))
    for m in all_m:
        r0 = results[RULES[0][0]]["monthly_data"].get(m, {"ret": 1.0})
        rb = results[best_rule]["monthly_data"].get(m, {"ret": 1.0})
        r0v = (r0["ret"] - 1) * 100
        rbv = (rb["ret"] - 1) * 100
        diff = rbv - r0v
        flag = " ✅" if diff > 1 else (" ✗" if diff < -1 else "")
        print(f"  {m:<9}{r0v:>+10.2f}{rbv:>+18.2f}{diff:>+9.2f}{flag}")


if __name__ == "__main__":
    main()
