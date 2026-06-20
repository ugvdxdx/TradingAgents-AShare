#!/usr/bin/env python3
"""大规模验证: 纯量化锚(chain+capital)的排序预测力。

不调LLM, 秒级跑完全V3池530只 × 21个时间点。
对比多个因子组合的Spearman, 找到最稳健的排序锚。

用法: uv run python3 scripts/validate_anchor.py
"""
import json, os, sys, pickle
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import picker.paths as paths

V3 = json.load(open(paths.V3_CACHE))

def real_returns(code, cutoff, days):
    """cutoff后N个交易日涨幅%"""
    suf = "_SH" if code.startswith("6") else "_SZ"
    p = os.path.join(paths.KLINE_CACHE_DIR, f"{code}{suf}".replace(".", "_") + ".pkl")
    if not os.path.exists(p):
        return None
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    m = df["trade_date"] <= cutoff
    if m.sum() == 0:
        return None
    base_idx = m.sum() - 1
    if base_idx + days >= len(df):
        return None
    return round((df["close"].iloc[base_idx + days] / df["close"].iloc[base_idx] - 1) * 100, 2)

def spearman(a, b):
    """Spearman秩相关"""
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
    return num / ((da ** 0.5) * (db ** 0.5)) if da * db > 0 else 0

def get_all_cutoffs(step=2):
    """从K线获取所有可用cutoff日期 (间隔step个交易日)"""
    p = os.path.join(paths.KLINE_CACHE_DIR, "300308_SZ.pkl")
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    n = len(df)
    cutoffs = []
    for i in range(20, n - 29, step):  # 前20根(算r20用), 后留30根(验证用)
        cutoffs.append(df["trade_date"].iloc[i])
    return cutoffs

def run(hold_days=30, step=2):
    cutoffs = get_all_cutoffs(step=step)
    print(f"{'='*80}")
    print(f"  大规模验证: {len(cutoffs)}个时间点 × 全V3池530只 × {hold_days}日验证窗口")
    print(f"  cutoff范围: {cutoffs[0]} ~ {cutoffs[-1]}")
    print(f"{'='*80}")

    # 预计算每期的数据
    periods = {}
    for cutoff in cutoffs:
        rows = []
        for code, v in V3.items():
            if not isinstance(v, dict) or "sector_score" not in v:
                continue
            r = real_returns(code, cutoff, hold_days)
            if r is None:
                continue
            rows.append({
                "code": code, "ret": r,
                "v3": v["sector_score"],
                "chain": v.get("chain", 0),
                "delivery": v.get("delivery", 0),
                "capital": v.get("capital", 0),
            })
        periods[cutoff] = rows

    # 对比因子
    factors = {
        "V3总分(sector_score)": lambda r: r["v3"],
        "chain only": lambda r: r["chain"],
        "capital only": lambda r: r["capital"],
        "delivery only": lambda r: r["delivery"],
        "chain+capital(等权)": lambda r: r["chain"] + r["capital"],
        "chain+capital×1.5": lambda r: r["chain"] + r["capital"] * 1.5,
        "chain+capital×2": lambda r: r["chain"] + r["capital"] * 2,
        "chain+capital×3": lambda r: r["chain"] + r["capital"] * 3,
        "chain×2+capital": lambda r: r["chain"] * 2 + r["capital"],
    }

    # 计算每个因子在每个cutoff的Spearman
    results = {name: [] for name in factors}
    for cutoff in cutoffs:
        rows = periods[cutoff]
        if len(rows) < 10:
            for name in factors:
                results[name].append(None)
            continue
        rets = [r["ret"] for r in rows]
        for name, fn in factors.items():
            vals = [fn(r) for r in rows]
            results[name].append(spearman(vals, rets))

    # 输出表格
    header = f"{'因子':<24}"
    for c in cutoffs:
        header += f"{c[5:]:>8}"
    header += f"{'均值':>7}{'min':>6}{'胜率':>6}"
    print(f"\n{header}")
    print("-" * (24 + 8 * len(cutoffs) + 19))

    ranked = sorted(results.items(), key=lambda x: -sum(v for v in x[1] if v is not None) / max(1, len([v for v in x[1] if v is not None])))
    for name, rhos in ranked:
        valid = [r for r in rhos if r is not None]
        if not valid:
            continue
        avg = sum(valid) / len(valid)
        mn = min(valid)
        positive = sum(1 for r in valid if r > 0)
        line = f"  {name:<22}"
        for rho in rhos:
            line += f"{rho:>+8.3f}" if rho is not None else f"{'N/A':>8}"
        line += f"{avg:>+7.3f}{mn:>+6.2f}{positive:>4}/{len(valid)}"
        print(line)

    # 最优因子详情
    best_name, best_rhos = ranked[0]
    best_valid = [r for r in best_rhos if r is not None]
    best_avg = sum(best_valid) / len(best_valid)
    print(f"\n{'='*80}")
    print(f"  最优因子: {best_name}")
    print(f"  Spearman均值: {best_avg:+.3f} | 最低: {min(best_valid):+.3f} | "
          f"正相关率: {sum(1 for r in best_valid if r>0)}/{len(best_valid)}")
    print(f"  (Spearman>0.3=强, >0.1=有效, >0=至少方向对)")

    return ranked

if __name__ == "__main__":
    print(f"\n{'='*80}")
    print(f"  验证1: 30日持仓窗口")
    print(f"{'='*80}")
    r30 = run(hold_days=30, step=2)

    print(f"\n\n{'='*80}")
    print(f"  验证2: 10日持仓窗口 (更短频)")
    print(f"{'='*80}")
    r10 = run(hold_days=10, step=2)
