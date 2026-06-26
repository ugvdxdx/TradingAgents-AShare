#!/usr/bin/env python3
"""新旧 Prompt 锚分回测对比 — 同一方法论下, 比较两套 chain/surge 分数的排序预测力。

⚠ 前视偏差: 本脚本用【当前】的 chain/surge 分数对齐所有历史 cutoff (无法取历史
   chain/surge 快照), 与 validate_anchor.py 同一已知近似。但作为新旧 prompt 的
   【相对】对比是有效的 (同方法、同 cutoff、同股票池, 只换分数来源)。

用法: python3 scripts/backtest_compare_prompts.py
"""
import json, os, sys, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picker.paths as paths

OLD = json.load(open(os.path.join(paths.DATA_DIR, "caches", "fundamental_v3_scores.json.old")))
NEW = json.load(open(paths.V3_CACHE))
HOLD = 30  # 持有天数


def real_returns(code, cutoff, days):
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


def get_cutoffs(step=2):
    p = os.path.join(paths.KLINE_CACHE_DIR, "300308_SZ.pkl")
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    n = len(df)
    out = []
    for i in range(20, n - HOLD - 1, step):
        out.append(df["trade_date"].iloc[i])
    return out


# 关键因子 (CLAUDE.md 记录的最优锚: chain + capital×2 + surge×SURGE_WEIGHT)
FACTORS = {
    "V3总分(sector_score)": lambda v: v.get("sector_score", 0),
    "chain only": lambda v: v.get("chain", 0),
    "chain+capital×2": lambda v: v.get("chain", 0) + v.get("capital", 0) * 2,
    "锚:chain+cap×2-del×0.5": lambda v: v.get("chain", 0) + v.get("capital", 0) * 2 - v.get("surge", 0) * 0.5,
}


def run_backtest(cache, cutoffs):
    """对一套分数跑全 cutoff 回测, 返回 {因子: [各cutoff的spearman]}"""
    results = {name: [] for name in FACTORS}
    for cutoff in cutoffs:
        rows = []
        for code, v in cache.items():
            if not isinstance(v, dict) or "sector_score" not in v:
                continue
            r = real_returns(code, cutoff, HOLD)
            if r is None:
                continue
            rows.append((v, r))
        if len(rows) < 10:
            for name in FACTORS:
                results[name].append(None)
            continue
        rets = [r for _, r in rows]
        for name, fn in FACTORS.items():
            vals = [fn(v) for v, _ in rows]
            results[name].append(spearman(vals, rets))
    return results


def summarize(rhos):
    valid = [r for r in rhos if r is not None]
    if not valid:
        return None
    avg = sum(valid) / len(valid)
    mn = min(valid)
    pos = sum(1 for r in valid if r > 0)
    return {"avg": avg, "min": mn, "winrate": f"{pos}/{len(valid)}"}


def main():
    cutoffs = get_cutoffs()
    print(f"{'='*90}")
    print(f"  新旧 Prompt 锚分回测对比 ({len(cutoffs)}个时间点 × 全池 × {HOLD}日窗口)")
    print(f"  旧: deepseek-v4-pro + 旧prompt | 新: GLM-5.2 + 新prompt")
    print(f"  ⚠ 同方法相对对比 (chain/surge 用当前分数, 有前视近似)")
    print(f"{'='*90}")

    print("  回测中 (旧→新)...", flush=True)
    old_res = run_backtest(OLD, cutoffs)
    new_res = run_backtest(NEW, cutoffs)

    print(f"\n{'因子':<26} {'│ 旧 avg':>9} {'旧min':>6} {'旧胜率':>7} {'│ 新 avg':>9} {'新min':>6} {'新胜率':>7} {'│ Δavg':>7}")
    print("─" * 90)
    for name in FACTORS:
        o = summarize(old_res[name])
        n = summarize(new_res[name])
        if not o or not n:
            continue
        delta = n["avg"] - o["avg"]
        d_color = "\033[92m" if delta > 0.005 else ("\033[91m" if delta < -0.005 else "")
        d_sign = "↑" if delta > 0.005 else ("↓" if delta < -0.005 else "→")
        print(f"  {name:<24} │ {o['avg']:>+8.3f} {o['min']:>+6.2f} {o['winrate']:>7}"
              f" │ {n['avg']:>+8.3f} {n['min']:>+6.2f} {n['winrate']:>7}"
              f" │ {d_color}{d_sign}{delta:+.3f}\033[0m")

    # 重点: 生产用的锚
    anchor_name = "锚:chain+cap×2-del×0.5"
    o = summarize(old_res[anchor_name])
    n = summarize(new_res[anchor_name])
    print(f"\n{'='*90}")
    print(f"  生产锚 {anchor_name}")
    print(f"  旧: avg={o['avg']:+.3f}  min={o['min']:+.2f}  正相关期 {o['winrate']}")
    print(f"  新: avg={n['avg']:+.3f}  min={n['min']:+.2f}  正相关期 {n['winrate']}")
    delta = n["avg"] - o["avg"]
    if delta > 0.005:
        print(f"  \033[92m✓ 新 prompt 排序质量提升 {delta:+.3f}, 可放心上线\033[0m")
    elif delta > -0.02:
        print(f"  \033[93m→ 新 prompt 排序质量基本持平 ({delta:+.3f}), 可上线\033[0m")
    else:
        print(f"  \033[91m⚠ 新 prompt 排序质量下降 {delta:+.3f}, 需排查 prompt\033[0m")


if __name__ == "__main__":
    main()
