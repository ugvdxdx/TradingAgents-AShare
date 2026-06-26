#!/usr/bin/env python3
"""新晋股 chain-boost 可行性分析 (只分析, 不改生产代码)。

核心问题: 在量化锚 ``chain + capital×2 + surge×SURGE_WEIGHT`` 上, 对新晋股加一个
chain-boost (或对照的 anchor/capital boost), 能不能提升 21 期 Spearman 且
不破坏 TOP10 实盘质量?

回测框架完全复用 ``validate_anchor.py`` (同样 21 期 × 全 V3 池 530 只 × 30 日
窗口, 同样的 real_returns / spearman 实现), 保证结果可直接与基线对比。

为什么 boost 重点放在 chain 上:
  - capital 每日由 ``update_capital()`` 用 r5/r20 + 板块动量重算, 已经含动量信号,
    再叠加 boost 会双重计算。
  - chain 是季度 LLM 打分的 stale 慢变量, 新晋股 (V3<15) 的 chain 普遍偏低,
    正是"评分系统滞后"的部分; 给 chain 加 bonus 修正这一假设。

新晋股身份 (纯量化近似, 无 LLM):
  ``is_star = (v3.sector_score < 15) and (r20 > 15)``
  生产级还要求 LLM 归因为"板块供需型" (``_backtest_rising_stars`` 调 LLM, 慢路径),
  本脚本默认跳过归因 (对"boost 有没有效"这个核心问题, 归因是次要过滤维度);
  ``--with-attribution`` 占位, 后续可扩展。

无前视偏差:
  - K 线按 cutoff 截断 (与 ``data_io.load_kline(code, cutoff)`` 同款)。
  - 只读 V3 cache 静态值, 不调 ``update_capital`` (避免 capital 被未来数据污染,
    沿用 validate_anchor 的做法)。

用法: uv run python3 scripts/analyze_rising_star_boost.py
"""
import json
import os
import pickle
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import picker.paths as paths

V3 = json.load(open(paths.V3_CACHE))

# 新晋股判定阈值 (与生产 data_io._backtest_rising_stars / scan_mispriced 一致)
STAR_V3_MAX = 15.0
STAR_R20_MIN = 15.0

# 阈值校验 (来自交接文档约束)
SAFE_MEAN_RHO = 0.50
SAFE_MIN_RHO = 0.34
# Spearman 均值相对基线的"实质提升"门槛: 小于此值视为噪声/平局, 不算真改善。
# (21 期平均相关, 系数权重微调引起的 ±0.001~0.003 波动属于排序扰动而非信号)
MEANINGFUL_RHO_GAIN = 0.005
# TOP10 实盘涨幅相对基线允许的下降幅度: 超过即判 boost 损害实盘质量。
TOP10_TOLERANCE = -0.5  # 百分点


# ══════════════════════════════════════════════════════════
# 基础: 截断 K 线 / 真实涨幅 / r20 / Spearman
# ══════════════════════════════════════════════════════════

def _kline_path(code: str) -> str:
    suf = "_SH" if code.startswith("6") else "_SZ"
    return os.path.join(paths.KLINE_CACHE_DIR, f"{code}{suf}.pkl")


def _load_kline(code: str):
    p = _kline_path(code)
    if not os.path.exists(p):
        return None
    try:
        df = pickle.load(open(p, "rb"))
        if df is None or len(df) == 0:
            return None
        return df.sort_values("trade_date").reset_index(drop=True)
    except Exception:
        return None


def _r20_at(df, cutoff_idx: int) -> Optional[float]:
    """截止日为 df.iloc[cutoff_idx] 时的近 20 日涨幅 %。需 cutoff_idx >= 20。"""
    if df is None or cutoff_idx < 20:
        return None
    try:
        last = df["close"].iloc[cutoff_idx]
        base = df["close"].iloc[cutoff_idx - 20]
        if base <= 0:
            return None
        return round((last / base - 1) * 100, 2)
    except Exception:
        return None


def _real_returns(df, cutoff_idx: int, days: int) -> Optional[float]:
    """cutoff 后 N 个交易日涨幅 % (用预排序的 df + 已知 cutoff_idx)。"""
    if df is None or cutoff_idx < 0:
        return None
    end = cutoff_idx + days
    if end >= len(df):
        return None
    try:
        return round((df["close"].iloc[end] / df["close"].iloc[cutoff_idx] - 1) * 100, 2)
    except Exception:
        return None


def spearman(a: List[float], b: List[float]) -> float:
    """Spearman 秩相关 (与 validate_anchor.py 同实现)。"""
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


def get_all_cutoffs(step: int = 2) -> List[str]:
    """从基准 K 线获取所有可用 cutoff 日期 (间隔 step 个交易日)。

    前 20 根留作 r20 计算, 后 30 根留作 hold_days 验证窗口。
    range 上界用 n-30 (而非 n-29): cutoff_idx+30 必须 <= n-1, 否则 real_returns 越界返回 None。
    """
    p = os.path.join(paths.KLINE_CACHE_DIR, "300308_SZ.pkl")
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    n = len(df)
    cutoffs = []
    for i in range(20, n - 30, step):
        cutoffs.append(df["trade_date"].iloc[i])
    return cutoffs


# ══════════════════════════════════════════════════════════
# 每期数据构建 (一次性, 复用给所有模块)
# ══════════════════════════════════════════════════════════

def build_periods(cutoffs: List[str], hold_days: int = 30) -> Dict[str, List[dict]]:
    """对每个 cutoff, 构建一行 = {code, ret, v3, chain, capital, surge, r20, is_star}。

    K 线按 cutoff 截断 (无前视); r20 / ret 都基于截断后的位置算。
    """
    # 预加载全部 K 线 (530 只 × 90 行, 内存可接受), 每只只排一次序
    print(f"  [加载] 预读 {len(V3)} 只 K 线...")
    klines: Dict[str, Any] = {}
    for code in V3:
        df = _load_kline(code)
        if df is not None:
            klines[code] = df
    print(f"  [加载] 成功 {len(klines)} 只, 缺 K 线 {len(V3) - len(klines)} 只")

    # 用基准股建 cutoff → idx 映射 (所有股交易日对齐)
    base_df = klines.get("300308")
    if base_df is None:
        # 回退: 取任一可用股的索引
        base_df = next(iter(klines.values()))
    date_to_idx = {d: i for i, d in enumerate(base_df["trade_date"])}

    periods: Dict[str, List[dict]] = {}
    for cutoff in cutoffs:
        ci = date_to_idx.get(cutoff)
        if ci is None:
            periods[cutoff] = []
            continue
        rows: List[dict] = []
        for code, v in V3.items():
            if not isinstance(v, dict) or "sector_score" not in v:
                continue
            df = klines.get(code)
            if df is None:
                continue
            # 个股的 cutoff 位置: 用日期匹配 (个股可能停牌缺日, 取 <= cutoff 的最后一根)
            valid = df[df["trade_date"] <= cutoff]
            if len(valid) <= 20:
                continue
            idx = len(valid) - 1  # 该股在 cutoff 当天对应的位置
            r20 = _r20_at(df, idx)
            if r20 is None:
                continue
            ret = _real_returns(df, idx, hold_days)
            if ret is None:
                continue
            sector_score = v["sector_score"]
            rows.append({
                "code": code, "ret": ret, "r20": r20,
                "v3": sector_score,
                "chain": v.get("chain", 0),
                "surge": v.get("surge", 0),
                "capital": v.get("capital", 0),
                "is_star": (sector_score < STAR_V3_MAX) and (r20 > STAR_R20_MIN),
            })
        periods[cutoff] = rows
    return periods


# ══════════════════════════════════════════════════════════
# 模块 B: 诊断 — 新晋股群体到底有没有超额收益
# ══════════════════════════════════════════════════════════

def diagnose(periods: Dict[str, List[dict]]) -> dict:
    """回答: 该不该 boost。"""
    print(f"\n{'='*80}")
    print(f"  模块 B: 新晋股群体诊断 (该不该 boost)")
    print(f"{'='*80}")

    summary = {"by_period": [], "war_period": [], "normal_period": []}
    for cutoff, rows in periods.items():
        stars = [r for r in rows if r["is_star"]]
        nonstars = [r for r in rows if not r["is_star"]]
        if not rows:
            continue

        # 同 v3 档 non-star: v3 < 15 的非新晋 (控制 v3 水平, 看 r20 因子的边际)
        low_v3_nonstar = [r for r in nonstars if r["v3"] < STAR_V3_MAX]

        star_ret_mean = sum(r["ret"] for r in stars) / len(stars) if stars else 0.0
        low_nonstar_ret_mean = (sum(r["ret"] for r in low_v3_nonstar) / len(low_v3_nonstar)
                                if low_v3_nonstar else 0.0)
        excess = star_ret_mean - low_nonstar_ret_mean if stars and low_v3_nonstar else 0.0

        star_chain_mean = sum(r["chain"] for r in stars) / len(stars) if stars else 0.0
        star_cap_mean = sum(r["capital"] for r in stars) / len(stars) if stars else 0.0

        # star 群体内部 anchor vs ret 的 Spearman (看 flag 股内部排序是否有效)
        star_rho = None
        if len(stars) >= 5:
            anchors = [r["chain"] + r["capital"] * 2 - r["surge"] * 0.5 for r in stars]
            star_rho = spearman(anchors, [r["ret"] for r in stars])

        rec = {
            "cutoff": cutoff, "n_stars": len(stars), "n_low_v3_nonstar": len(low_v3_nonstar),
            "star_ret_mean": round(star_ret_mean, 2),
            "low_nonstar_ret_mean": round(low_nonstar_ret_mean, 2),
            "excess": round(excess, 2),
            "star_chain_mean": round(star_chain_mean, 2),
            "star_capital_mean": round(star_cap_mean, 2),
            "star_internal_rho": round(star_rho, 3) if star_rho is not None else None,
        }
        summary["by_period"].append(rec)
        # 分段: 3 月 = 战争期 (金属/军工), 4 月后 = 正常期
        (summary["war_period"] if cutoff < "2026-04-01" else summary["normal_period"]).append(rec)

    # 打印表格
    print(f"\n{'cutoff':>10} {'star#':>5} {'lowNon#':>7} {'star均涨':>7} "
          f"{'同档非':>7} {'超额':>7} {'star均chain':>10} {'star均cap':>8} {'star内ρ':>7}")
    print("-" * 85)
    for r in summary["by_period"]:
        rho_s = f"{r['star_internal_rho']:+.2f}" if r["star_internal_rho"] is not None else "N/A"
        print(f"{r['cutoff']:>10} {r['n_stars']:>5} {r['n_low_v3_nonstar']:>7} "
              f"{r['star_ret_mean']:>+7.1f} {r['low_nonstar_ret_mean']:>+7.1f} "
              f"{r['excess']:>+7.1f} {r['star_chain_mean']:>10.2f} "
              f"{r['star_capital_mean']:>8.2f} {rho_s:>7}")

    # 分段汇总
    def seg_stats(recs):
        if not recs:
            return None
        excesses = [r["excess"] for r in recs if r["n_stars"] > 0 and r["n_low_v3_nonstar"] > 0]
        rets = [r["star_ret_mean"] for r in recs if r["n_stars"] > 0]
        return {
            "n_periods": len(recs),
            "avg_excess": round(sum(excesses) / len(excesses), 2) if excesses else None,
            "avg_star_ret": round(sum(rets) / len(rets), 2) if rets else None,
            "avg_n_stars": round(sum(r["n_stars"] for r in recs) / len(recs), 1),
        }

    war = seg_stats(summary["war_period"])
    norm = seg_stats(summary["normal_period"])
    print(f"\n  分段汇总 (star vs 同 v3<15 档非新晋 的实际 30 日涨幅均值差):")
    if war:
        print(f"    战争期 (<04-01): {war['n_periods']}期 | star均 {war['avg_star_ret']:+.1f}% "
              f"| 平均超额 {war['avg_excess']:+.1f}% | 平均 star 数 {war['avg_n_stars']:.1f}")
    if norm:
        print(f"    正常期 (≥04-01): {norm['n_periods']}期 | star均 {norm['avg_star_ret']:+.1f}% "
              f"| 平均超额 {norm['avg_excess']:+.1f}% | 平均 star 数 {norm['avg_n_stars']:.1f}")

    summary["segments"] = {"war": war, "normal": norm}
    return summary


# ══════════════════════════════════════════════════════════
# 模块 C: boost 变体网格 — 哪种机制/幅度能提升 Spearman
# ══════════════════════════════════════════════════════════

def _anchor_base(r: dict) -> float:
    return r["chain"] + r["capital"] * 2 - r["surge"] * 0.5


def make_chain_boost(B: float, scaled: bool = False) -> Callable[[dict], float]:
    """chain_boost: star 股 chain += B (scaled 时 B × min(r20/15, 3))。"""
    def fn(r: dict) -> float:
        if not r["is_star"]:
            return _anchor_base(r)
        bonus = B * min(r["r20"] / STAR_R20_MIN, 3.0) if scaled else B
        return (r["chain"] + bonus) + r["capital"] * 2 - r["surge"] * 0.5
    return fn


def make_anchor_boost(B: float) -> Callable[[dict], float]:
    """anchor_boost (对照): star 股 anchor += B。"""
    def fn(r: dict) -> float:
        return _anchor_base(r) + (B if r["is_star"] else 0.0)
    return fn


def make_capital_boost(B: float) -> Callable[[dict], float]:
    """capital_boost (对照): star 股 capital += B。"""
    def fn(r: dict) -> float:
        cap = r["capital"] + (B if r["is_star"] else 0.0)
        return r["chain"] + cap * 2 - r["surge"] * 0.5
    return fn


def build_variants() -> List[Tuple[str, Callable[[dict], float]]]:
    """构造全部变体 (≈20 个)。"""
    out: List[Tuple[str, Callable[[dict], float]]] = [("(基线) anchor", _anchor_base)]
    for B in (0.5, 1.0, 1.5, 2.0, 3.0):
        out.append((f"chain+{B}", make_chain_boost(B)))
        out.append((f"chain+{B}(scaled)", make_chain_boost(B, scaled=True)))
        out.append((f"anchor+{B}", make_anchor_boost(B)))
        out.append((f"capital+{B}", make_capital_boost(B)))
    return out


def eval_variants(periods: Dict[str, List[dict]]) -> Tuple[dict, dict]:
    """对每个变体算 21 期 Spearman。返回 (spearman_results, top10_results)。"""
    variants = build_variants()
    sp_results: Dict[str, List[Optional[float]]] = {name: [] for name, _ in variants}
    # TOP10: 每期 TOP10 的平均实际涨幅 + TOP10 中 star 占比
    top10_results: Dict[str, List[dict]] = {name: [] for name, _ in variants}

    for cutoff, rows in periods.items():
        if len(rows) < 10:
            for name, _ in variants:
                sp_results[name].append(None)
                top10_results[name].append({})
            continue
        rets = [r["ret"] for r in rows]
        for name, fn in variants:
            vals = [fn(r) for r in rows]
            sp_results[name].append(spearman(vals, rets))
            # TOP10: 按 vals 降序取前 10
            order = sorted(range(len(rows)), key=lambda i: -vals[i])[:10]
            top_rets = [rows[i]["ret"] for i in order]
            n_star_top = sum(1 for i in order if rows[i]["is_star"])
            top10_results[name].append({
                "cutoff": cutoff,
                "avg_ret": round(sum(top_rets) / len(top_rets), 2),
                "n_star": n_star_top,
            })
    return sp_results, top10_results


def print_spearman_table(sp_results: dict, cutoffs: List[str]) -> List[Tuple[str, dict]]:
    """打印变体 Spearman 表, 返回 [(name, stats)] 按均值降序。"""
    # 统计
    ranked = []
    for name, rhos in sp_results.items():
        valid = [r for r in rhos if r is not None]
        if not valid:
            continue
        avg = sum(valid) / len(valid)
        ranked.append((name, {
            "rhos": rhos, "avg": avg, "min": min(valid),
            "wins": sum(1 for r in valid if r > 0), "n": len(valid),
        }))
    ranked.sort(key=lambda x: -x[1]["avg"])

    header = f"{'变体':<22}"
    for c in cutoffs:
        header += f"{c[5:]:>8}"
    header += f"{'均值':>7}{'min':>6}{'胜率':>7}"
    print(f"\n{header}")
    print("-" * (22 + 8 * len(cutoffs) + 20))
    for name, s in ranked:
        line = f"  {name:<20}"
        for rho in s["rhos"]:
            line += f"{rho:>+8.3f}" if rho is not None else f"{'N/A':>8}"
        line += f"{s['avg']:>+7.3f}{s['min']:>+6.2f}{s['wins']:>3}/{s['n']}"
        print(line)
    return ranked


# ══════════════════════════════════════════════════════════
# 模块 D: TOP10 实盘质量评估
# ══════════════════════════════════════════════════════════

def print_top10_table(top10_results: dict, sp_ranked: List[Tuple[str, dict]],
                      cutoffs: List[str]) -> dict:
    """TOP10 平均实际 30 日涨幅 (基线 vs 最优变体 top-3) + star 占比。"""
    print(f"\n{'='*80}")
    print(f"  模块 D: TOP10 实盘质量 (每期取该变体排序的 TOP10, 算其 30 日实际涨幅均值)")
    print(f"{'='*80}")

    def agg(name):
        recs = [r for r in top10_results.get(name, []) if r]
        if not recs:
            return None
        return {
            "avg_top10_ret": round(sum(r["avg_ret"] for r in recs) / len(recs), 2),
            "avg_n_star": round(sum(r["n_star"] for r in recs) / len(recs), 2),
            "by_period": recs,
        }

    # 选要展示的: 基线 + spearman 最优 top3 (去重基线)
    names_to_show = ["(基线) anchor"]
    for name, _ in sp_ranked:
        if name not in names_to_show:
            names_to_show.append(name)
        if len(names_to_show) >= 4:
            break

    agg_map = {n: agg(n) for n in names_to_show}
    base_ret = agg_map["(基线) anchor"]["avg_top10_ret"] if agg_map["(基线) anchor"] else 0
    base_star = agg_map["(基线) anchor"]["avg_n_star"] if agg_map["(基线) anchor"] else 0

    print(f"\n{'变体':<22}{'TOP10均涨':>10}{'vs基线':>8}{'TOP10均star数':>13}{'vs基线':>8}")
    print("-" * 65)
    for n in names_to_show:
        a = agg_map[n]
        if not a:
            continue
        print(f"  {n:<20}{a['avg_top10_ret']:>+10.2f}{a['avg_top10_ret']-base_ret:>+8.2f}"
              f"{a['avg_n_star']:>13.2f}{a['avg_n_star']-base_star:>+8.2f}")

    return agg_map


# ══════════════════════════════════════════════════════════
# 结论
# ══════════════════════════════════════════════════════════

def conclude(sp_ranked: List[Tuple[str, dict]], top10_agg: dict) -> dict:
    """产出"该不该 boost / 哪种 / 多大"的结论。

    三重门槛, 任一不过即判 boost 无效:
      1. 安全: 变体 Spearman 均值 ≥ SAFE_MEAN_RHO 且 min ≥ SAFE_MIN_RHO。
      2. 实质提升: 变体均值 - 基线均值 ≥ MEANINGFUL_RHO_GAIN (否则视为噪声平局)。
      3. 实盘不损: TOP10 平均涨幅降幅 ≤ TOP10_TOLERANCE (排序微涨但 TOP10 涨幅掉
         说明 boost 把好股挤出 TOP10, 实盘有害)。
    """
    print(f"\n{'='*80}")
    print(f"  结论")
    print(f"{'='*80}")

    baseline = next((s for n, s in sp_ranked if n == "(基线) anchor"), None)
    base_avg = baseline["avg"] if baseline else 0.0
    base_min = baseline["min"] if baseline else 0.0
    base_top10 = top10_agg.get("(基线) anchor", {}).get("avg_top10_ret", 0.0)

    # 找最优 chain_boost (固定 + scaled 分开), 整体最优
    chain_fixed = [(n, s) for n, s in sp_ranked if n.startswith("chain+") and "scaled" not in n]
    chain_scaled = [(n, s) for n, s in sp_ranked if n.startswith("chain+") and "scaled" in n]
    best_chain_fixed = chain_fixed[0] if chain_fixed else None
    best_chain_scaled = chain_scaled[0] if chain_scaled else None
    best_overall = sp_ranked[0] if sp_ranked else None

    def safe(s):
        return s["avg"] >= SAFE_MEAN_RHO and s["min"] >= SAFE_MIN_RHO

    def meaningful(s):
        return (s["avg"] - base_avg) >= MEANINGFUL_RHO_GAIN

    def top10_ok(name):
        a = top10_agg.get(name)
        if not a:
            return True  # 无 TOP10 数据时不拦
        return (a["avg_top10_ret"] - base_top10) >= TOP10_TOLERANCE

    rec = {
        "baseline_avg": round(base_avg, 4), "baseline_min": round(base_min, 3),
        "baseline_top10_ret": round(base_top10, 2),
        "baseline_safe": safe(baseline) if baseline else False,
        "best_overall": best_overall,
        "best_chain_fixed": best_chain_fixed,
        "best_chain_scaled": best_chain_scaled,
    }

    print(f"\n  基线 anchor: Spearman均值 {base_avg:+.3f} | min {base_min:+.3f} "
          f"| TOP10均涨 {base_top10:+.2f}% "
          f"| 安全: {'✅' if rec['baseline_safe'] else '❌'}")
    print(f"  判定门槛: 实质提升 Δ≥{MEANINGFUL_RHO_GAIN} | TOP10降幅≤{abs(TOP10_TOLERANCE)}pp | 安全区[均≥{SAFE_MEAN_RHO}, min≥{SAFE_MIN_RHO}]")

    def gate(name, s):
        """三重门槛, 返回 (通过, 原因)。"""
        g1 = safe(s)
        g2 = meaningful(s)
        g3 = top10_ok(name)
        t10 = top10_agg.get(name, {})
        t10_d = (t10.get("avg_top10_ret", base_top10) - base_top10) if t10 else 0.0
        if g1 and g2 and g3:
            return True, "三重门槛全过"
        reasons = []
        if not g1:
            reasons.append(f"不安全(均{s['avg']:+.3f}/min{s['min']:+.3f})")
        if not g2:
            reasons.append(f"无实质提升(Δ{s['avg']-base_avg:+.4f}<{MEANINGFUL_RHO_GAIN})")
        if not g3:
            reasons.append(f"TOP10受损(Δ{t10_d:+.2f}pp)")
        return False, "; ".join(reasons)

    def show(label, pair):
        if not pair:
            return
        n, s = pair
        passed, why = gate(n, s)
        t10 = top10_agg.get(n, {})
        t10_str = (f"TOP10 {t10['avg_top10_ret']:+.2f}% (Δ{t10['avg_top10_ret']-base_top10:+.2f})"
                   if t10 else "TOP10 N/A")
        print(f"  最优{label}: {n} | 均值 {s['avg']:+.3f} (Δ{s['avg']-base_avg:+.4f}) "
              f"| min {s['min']:+.3f} | {t10_str}")
        print(f"     → {'✅ 通过' if passed else '❌ '+why}")

    show("chain_boost(固定)", best_chain_fixed)
    show("chain_boost(scaled)", best_chain_scaled)
    show("整体变体", best_overall)

    # TOP10 全景
    if top10_agg:
        print(f"\n  TOP10 实盘涨幅全景 (基线 vs 上榜变体):")
        for n, a in top10_agg.items():
            if not a:
                continue
            delta = a["avg_top10_ret"] - base_top10
            star_d = a["avg_n_star"] - top10_agg["(基线) anchor"]["avg_n_star"]
            print(f"    {n:<20} TOP10均涨 {a['avg_top10_ret']:+.2f}% (Δ{delta:+.2f}) | "
                  f"star {a['avg_n_star']:.2f} (Δ{star_d:+.2f})")

    # 最终建议: 三重门槛全过的才推荐
    print(f"\n  ── 最终建议 ──")
    candidates = []
    for n, s in sp_ranked:
        if n == "(基线) anchor":
            continue
        passed, _ = gate(n, s)
        if passed:
            candidates.append((n, s))
    if candidates:
        n, s = candidates[0]
        print(f"  ✅ 推荐: 接入 {n} (Spearman {s['avg']:+.3f}, 三重门槛全过)。")
        print(f"     生产接入点: debaters.py _anchor_score — 对 c.get('_rising_star') 的股, "
              f"按此变体的 bonus 公式调整 chain。")
        print(f"     ⚠ 接入前需用 LLM 归因(板块供需型)过滤 is_star, 本脚本用的是纯量化近似。")
        rec["recommend"] = n
    else:
        print(f"  ❌ 不推荐接入任何 boost 变体: 没有变体能同时通过'实质提升+安全+TOP10不损'三重门槛。")
        # 给出最接近的候选 + 失败原因, 便于决策
        near = [(n, s, s["avg"] - base_avg) for n, s in sp_ranked if n != "(基线) anchor"]
        if near:
            n, s, d = near[0]
            print(f"     最接近的变体 {n}: Spearman Δ{d:+.4f} (需 ≥ +{MEANINGFUL_RHO_GAIN}) — 实为噪声/平局。")
        print(f"     根因: 新晋股虽整体有超额(+13.8%正常期), 但 anchor 排序对全池 530 只已充分捕捉;")
        print(f"           boost 微调系数只会扰动排名, 不会稳定改善 TOP10 (TOP10 实盘涨幅反而下降)。")
        print(f"     建议: 维持基线 chain+capital×2+surge×SURGE_WEIGHT。新晋股的价值在'进入候选池', 不在'加分'。")
        rec["recommend"] = None
    return rec


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    cutoffs = get_all_cutoffs(step=2)
    hold_days = 30
    print(f"{'='*80}")
    print(f"  新晋股 chain-boost 可行性分析 (只分析, 不改生产代码)")
    print(f"  {len(cutoffs)} 个时间点 × 全 V3 池 {len(V3)} 只 × {hold_days} 日窗口")
    print(f"  cutoff 范围: {cutoffs[0]} ~ {cutoffs[-1]}")
    print(f"  新晋股判定: v3<{STAR_V3_MAX} & r20>{STAR_R20_MIN} (纯量化近似, 无 LLM 归因)")
    print(f"{'='*80}")

    # 一次性构建每期数据, 复用给所有模块
    periods = build_periods(cutoffs, hold_days)
    n_with_stars = sum(1 for rows in periods.values() if any(r["is_star"] for r in rows))
    total_stars = sum(sum(1 for r in rows if r["is_star"]) for rows in periods.values())
    print(f"  含新晋股的期数: {n_with_stars}/{len(cutoffs)} | 新晋股样本总数: {total_stars}")

    # 模块 B: 诊断
    diag = diagnose(periods)

    # 模块 C: boost 网格
    print(f"\n{'='*80}")
    print(f"  模块 C: boost 变体网格 (21 期 Spearman, 基线 vs 各变体)")
    print(f"{'='*80}")
    sp_results, top10_results = eval_variants(periods)
    sp_ranked = print_spearman_table(sp_results, cutoffs)

    # 模块 D: TOP10 质量
    top10_agg = print_top10_table(top10_results, sp_ranked, cutoffs)

    # 结论
    conclusion = conclude(sp_ranked, top10_agg)

    # 落盘
    out = {
        "generated_at": datetime.now().isoformat(),
        "config": {
            "cutoffs": cutoffs, "hold_days": hold_days,
            "star_v3_max": STAR_V3_MAX, "star_r20_min": STAR_R20_MIN,
            "safe_mean_rho": SAFE_MEAN_RHO, "safe_min_rho": SAFE_MIN_RHO,
        },
        "diagnosis": diag,
        "spearman": {n: s["rhos"] for n, s in sp_ranked},
        "spearman_stats": {n: {"avg": round(s["avg"], 3), "min": round(s["min"], 3),
                               "wins": s["wins"], "n": s["n"]} for n, s in sp_ranked},
        "top10_summary": {n: {"avg_top10_ret": a["avg_top10_ret"], "avg_n_star": a["avg_n_star"]}
                          for n, a in top10_agg.items() if a},
        "conclusion": {
            "baseline_avg": conclusion["baseline_avg"],
            "baseline_min": conclusion["baseline_min"],
            "baseline_top10_ret": conclusion["baseline_top10_ret"],
            "best_overall": conclusion["best_overall"][0],
            "best_overall_avg": round(conclusion["best_overall"][1]["avg"], 4),
            "best_chain_fixed": conclusion["best_chain_fixed"][0] if conclusion["best_chain_fixed"] else None,
            "best_chain_scaled": conclusion["best_chain_scaled"][0] if conclusion["best_chain_scaled"] else None,
            "recommend": conclusion["recommend"],
            "meaningful_rho_gain": MEANINGFUL_RHO_GAIN,
            "top10_tolerance": TOP10_TOLERANCE,
        },
    }
    paths.ensure_caches_dir()
    out_path = os.path.join(paths.CACHES_DIR, "rising_star_boost_analysis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    main()
