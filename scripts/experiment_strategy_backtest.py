#!/usr/bin/env python3
"""买1卖2 策略持仓轮动回测: 对比 delivery 权重的月化收益。

策略规则 (买1卖2):
  - 买入: 某股进入 TOP5 即买入 (买1, 无需确认)
  - 卖出: 连续 2 个 cutoff 不在 TOP5 才卖出 (卖2, 容忍1次掉出)
  - 持仓: 始终持有 5 只 (TOP5), 按等权配置

收益计算:
  - 每个 cutoff 调仓, 持仓收益 = 持有股在下一 cutoff 区间的实际涨幅
  - 月化 = (1 + 累计收益) ** (21/持仓交易日数) - 1
  - 换手率 = 平均每 cutoff 买入只数

用法:
  uv run python3 scripts/experiment_strategy_backtest.py
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
WEIGHTS = [-0.5, 1.0]
# 按月分段统计的起始月份 (YYYY-MM)
MONTH_BUCKETS = ["2025-04", "2025-06", "2025-08", "2025-10", "2025-12", "2026-02", "2026-04"]


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


def _price_at(code, date):
    """某股在 date 当天的收盘价。"""
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, "rb"))
                df = df.sort_values("trade_date").reset_index(drop=True)
                row = df[df["trade_date"] <= date]
                if len(row) == 0:
                    return None
                return float(row.iloc[-1]["close"])
            except Exception:
                return None
    return None


def _next_trading_day_after(code, cutoff, cutoffs):
    """cutoff 之后第一个有 K 线数据的交易日。"""
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                after = df[df["trade_date"] > cutoff]
                if len(after) > 0:
                    return after.iloc[0]["trade_date"]
            except Exception:
                pass
    return None


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
    print("  买1卖2 策略持仓轮动回测 (对比 delivery 权重)")
    print("=" * 78)
    print(f"  cutoff 数: {len(cutoffs)} | 权重: {WEIGHTS}")
    print(f"  规则: 进入TOP5买入, 连续2次cutoff不在TOP5才卖出\n")

    # 每个 cutoff 每个权重算出 TOP5
    top5_history = {w: {} for w in WEIGHTS}  # w -> {cutoff: [codes]}

    # 快照覆盖情况 (回测用历史快照的 chain/delivery, 无快照则回退当前 V3 cache)
    from picker.snapshot import get_snapshot_at, snapshot_coverage
    snap_lo, snap_hi, snap_n = snapshot_coverage()
    print(f"  快照覆盖: {snap_lo}~{snap_hi} ({snap_n}份) | 无快照的cutoff回退当前V3 cache(前视近似)\n")

    for ci, cutoff in enumerate(cutoffs, 1):
        momentum = get_sector_momentum(days=14)
        if not momentum.get("hot_sectors"):
            continue
        # 按 cutoff 取历史快照的 chain/delivery (消除前视偏差)
        snap_scores, snap_src = get_snapshot_at(cutoff)
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
            # chain/delivery 优先用历史快照, 无快照回退 V3 cache
            sv = snap_scores.get(code, {})
            if sv:
                chain_v = sv.get("chain", 0)
                delivery_v = sv.get("delivery", 0)
            else:
                chain_v = V3.get(code, {}).get("chain", 0)
                delivery_v = V3.get(code, {}).get("delivery", 0)
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
                "chain": chain_v,
                "delivery": delivery_v, "capital": capital,
            }
        if len(stock_data) < 10:
            continue

        for w in WEIGHTS:
            scored = sorted(stock_data.items(),
                            key=lambda x: -(x[1]["chain"] + x[1]["capital"] * 2 + x[1]["delivery"] * w))
            top5_history[w][cutoff] = [c for c, _ in scored[:5]]

        if ci % 40 == 0:
            print(f"  ... 排序 {ci}/{len(cutoffs)}")

    # 模拟持仓轮动 + 收益 (每个权重独立模拟, 记录逐期明细供分月统计)
    from collections import defaultdict
    monthly_stats = {w: defaultdict(lambda: {"ret": 1.0, "buys": 0, "periods": 0, "days": 0})
                     for w in WEIGHTS}

    for w in WEIGHTS:
        hist = top5_history[w]
        sorted_cutoffs = sorted(hist.keys())
        holdings: Dict[str, str] = {}
        miss_count: Dict[str, int] = {}

        cumulative = 1.0
        n_buys_total = 0
        n_periods = 0
        holding_days_total = 0
        period_returns = []

        for i, cutoff in enumerate(sorted_cutoffs):
            top5 = set(hist[cutoff])
            for code in list(holdings.keys()):
                if code not in top5:
                    miss_count[code] = miss_count.get(code, 0) + 1
                    if miss_count[code] >= 2:
                        del holdings[code]
                else:
                    miss_count[code] = 0
            new_buys = [c for c in top5 if c not in holdings]
            n_buys_this = len(new_buys)
            for c in new_buys:
                holdings[c] = cutoff
                n_buys_total += 1
            n_periods += 1

            if i + 1 < len(sorted_cutoffs):
                next_cutoff = sorted_cutoffs[i + 1]
                gap_days = _count_trading_days(cutoff, next_cutoff)
                holding_days_total += gap_days
                if holdings:
                    port_rets = []
                    for code in holdings:
                        p0 = _price_at(code, cutoff)
                        p1 = _price_at(code, next_cutoff)
                        if p0 and p1 and p0 > 0:
                            port_rets.append(p1 / p0 - 1)
                    if port_rets:
                        period_ret = sum(port_rets) / len(port_rets)
                        cumulative *= (1 + period_ret)
                        period_returns.append(period_ret)
                        # 归入月份 (用 cutoff 的月份)
                        month = cutoff[:7]
                        monthly_stats[w][month]["ret"] *= (1 + period_ret)
                        monthly_stats[w][month]["buys"] += n_buys_this
                        monthly_stats[w][month]["periods"] += 1
                        monthly_stats[w][month]["days"] += gap_days

        total_days = holding_days_total
        n_months = total_days / 21 if total_days > 0 else 1
        monthly = (cumulative ** (1 / max(n_months, 0.01)) - 1) * 100 if cumulative > 0 else -100
        annualized = cumulative ** (1 / max(n_months / 12, 0.01)) - 1 if cumulative > 0 else -1
        avg_turnover = n_buys_total / max(n_periods, 1)

        print(f"\n  ══ delivery 权重 W={w:+.1f} ══")
        print(f"  累计收益: {(cumulative - 1) * 100:+.2f}%")
        print(f"  持仓交易日: {total_days} ({n_months:.1f} 月)")
        print(f"  月化收益: {monthly:+.2f}%")
        print(f"  年化收益: {annualized * 100:+.1f}%")
        print(f"  换手率: {avg_turnover:.2f} 只/cutoff")
        if period_returns:
            pos = sum(1 for r in period_returns if r > 0)
            print(f"  正收益 cutoff: {pos}/{len(period_returns)} ({pos / len(period_returns) * 100:.0f}%)")
            print(f"  单期收益: 均{sum(period_returns) / len(period_returns) * 100:+.2f}% "
                  f"中位{statistics.median(period_returns) * 100:+.2f}% "
                  f"最差{min(period_returns) * 100:+.2f}% σ{statistics.stdev(period_returns) * 100:.2f}%")

    # 分月对比
    print(f"\n{'='*78}")
    print("  分月收益对比 (按 cutoff 月份归集)")
    print(f"{'='*78}")
    all_months = sorted(set(m for w in WEIGHTS for m in monthly_stats[w].keys()))
    print(f"  {'月份':<9}{'W=-0.5 月收益':>14}{'换手':>7}{'W=+1.0 月收益':>14}{'换手':>7}{'差值':>9}")
    print("  " + "-" * 60)
    for month in all_months:
        s_neg = monthly_stats[-0.5].get(month, {"ret": 1.0, "buys": 0, "periods": 0})
        s_pos = monthly_stats[1.0].get(month, {"ret": 1.0, "buys": 0, "periods": 0})
        ret_neg = (s_neg["ret"] - 1) * 100
        ret_pos = (s_pos["ret"] - 1) * 100
        to_neg = s_neg["buys"] / max(s_neg["periods"], 1)
        to_pos = s_pos["buys"] / max(s_pos["periods"], 1)
        diff = ret_pos - ret_neg
        flag = "  ✅+1.0" if diff > 1 else ("  ✗-0.5" if diff < -1 else "")
        print(f"  {month:<9}{ret_neg:>+14.2f}{to_neg:>7.1f}{ret_pos:>+14.2f}{to_pos:>7.1f}{diff:>+9.2f}{flag}")

    # 最近 3 个月 / 最近 6 个月 对比
    print(f"\n  ── 近期分段 ──")
    for label, recent in [("近3月", all_months[-3:]), ("近6月", all_months[-6:])]:
        cum_neg = 1.0
        cum_pos = 1.0
        days_neg = days_pos = 0
        for m in recent:
            sn = monthly_stats[-0.5].get(m, {"ret": 1.0, "days": 0})
            sp = monthly_stats[1.0].get(m, {"ret": 1.0, "days": 0})
            cum_neg *= sn["ret"]
            cum_pos *= sp["ret"]
            days_neg += sn.get("days", 0)
            days_pos += sp.get("days", 0)
        mn_neg = (cum_neg ** (21 / max(days_neg, 1)) - 1) * 100 if cum_neg > 0 else -100
        mn_pos = (cum_pos ** (21 / max(days_pos, 1)) - 1) * 100 if cum_pos > 0 else -100
        print(f"  {label} ({recent[0]}~{recent[-1]}): "
              f"W=-0.5 累计{(cum_neg-1)*100:+.1f}% 月化{mn_neg:+.2f}% | "
              f"W=+1.0 累计{(cum_pos-1)*100:+.1f}% 月化{mn_pos:+.2f}%")


def _count_trading_days(d1, d2):
    """两个 cutoff 之间的交易日数 (用 K 线日期推算)。"""
    p = os.path.join(KLINE_CACHE_DIR, "000001_SZ.pkl")
    if not os.path.exists(p):
        return 2
    try:
        df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
        between = df[(df["trade_date"] > d1) & (df["trade_date"] <= d2)]
        return max(len(between), 1)
    except Exception:
        return 2


if __name__ == "__main__":
    main()
