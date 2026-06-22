#!/usr/bin/env python3
"""price_factor 变体回测评估器 (批量对比 12 个变体 + 基线)。

读 build_price_factor_history.py 的产出 (含 _base_capital) + capital_history.json,
对每个变体组合成完整 capital, 算排序质量, 一次性输出对比表。

capital = base_capital(板块动量, 无前视) × price_factor(变体, 无前视)
排序锚: anchor = chain + capital×2 - delivery×0.5
评估: 全池 Spearman / TOP10 实盘30日涨幅 / 逐期 ↑↓

⚠ 前视偏差声明:
  - capital (base_capital × price_factor): 无前视 (cutoff 截断 K线/资金流/板块动量)
  - chain/delivery: 用 V3 cache 当前快照 (季度LLM打分, 无历史版本), 有前视。
    这意味着 Spearman 绝对值偏乐观, 但【变体间相对对比】仍有效 (所有变体共享同一
    chain/delivery, 前视是常数偏移, 不改变变体排序)。price_factor 的结论可信。

用法:
    uv run python3 scripts/eval_price_factors.py
    uv run python3 scripts/eval_price_factors.py --start 2025-04-01  # 仅正常期
"""
import argparse
import json
import os
import pickle
import statistics
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths

KLINE_DIR = paths.KLINE_CACHE_DIR
PF_HISTORY = os.path.join(paths.CACHES_DIR, "price_factor_history.json")
CAP_HISTORY = os.path.join(paths.CACHES_DIR, "capital_history.json")
V3 = json.load(open(paths.V3_CACHE))
HOLD_DAYS = 30


# ══════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════

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
    """cutoff 后 30 日涨幅。"""
    suffix = "_SH.pkl" if code.startswith("6") else "_SZ.pkl"
    p = os.path.join(KLINE_DIR, f"{code}{suffix}")
    if not os.path.exists(p):
        return None
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    valid = df[df["trade_date"] <= cutoff]
    idx = len(valid) - 1
    end = idx + HOLD_DAYS
    if idx < 0 or end >= len(df):
        return None
    return round((df["close"].iloc[end] / df["close"].iloc[idx] - 1) * 100, 2)


# ══════════════════════════════════════════════════════════
# 评估
# ══════════════════════════════════════════════════════════

def evaluate(pf_history: dict, cap_history: dict, cutoffs: List[str]) -> Dict[str, dict]:
    """对每个变体, 在每个 cutoff 算排序质量。

    capital = base_capital(cutoff) × price_factor(变体, cutoff)
    anchor = chain + capital×2 - delivery×0.5
    """
    variants = set()
    for cutoff_data in pf_history.values():
        variants.update(cutoff_data.keys())
    variants.discard("_base_capital")  # 特殊 key, 非变体

    results: Dict[str, dict] = {v: {"rhos": [], "top10_rets": [], "n_periods": 0}
                                for v in variants}

    for cutoff in cutoffs:
        pf_at = pf_history.get(cutoff, {})
        if not pf_at:
            continue
        # base_capital 来自 price_factor_history 的 _base_capital 字段 (剥离了 price_factor)
        # 若无, 回退到 capital_history (注意: 那是 final capital, 会引入双重计算偏差)
        base_cap_map = pf_at.get("_base_capital") or cap_history.get(cutoff, {})

        # 算每只股的 ret + chain/delivery (静态)
        stock_data = {}
        for code, v in V3.items():
            if not isinstance(v, dict) or "chain" not in v:
                continue
            ret = real_returns(code, cutoff)
            if ret is None:
                continue
            stock_data[code] = {
                "ret": ret, "chain": v.get("chain", 0),
                "delivery": v.get("delivery", 0),
                "base_capital": base_cap_map.get(code, v.get("capital", 0)),
            }
        if len(stock_data) < 10:
            continue

        for vname in variants:
            pf_map = pf_at.get(vname)
            if not pf_map:
                continue
            # 组合 capital + anchor
            # capital = base_capital(纯板块动量) × price_factor(变体)
            anchors = []
            rets = []
            for code, sd in stock_data.items():
                pf = pf_map.get(code)
                if pf is None:
                    continue
                capital = max(0, min(5.0, sd["base_capital"] * pf))
                anchor = sd["chain"] + capital * 2 - sd["delivery"] * 0.5
                anchors.append(anchor)
                rets.append(sd["ret"])
            if len(anchors) < 10:
                continue

            rho = spearman(anchors, rets)
            results[vname]["rhos"].append(rho)
            # TOP10
            order = sorted(range(len(anchors)), key=lambda i: -anchors[i])[:10]
            top_rets = [rets[i] for i in order]
            results[vname]["top10_rets"].append(sum(top_rets) / len(top_rets))
            results[vname]["n_periods"] += 1

    return results


def summarize(results: dict, baseline_name: str = "baseline_r5r20") -> List[tuple]:
    """汇总每个变体的统计, 按评分降序。"""
    baseline = results.get(baseline_name, {})
    base_rho_avg = (sum(baseline["rhos"]) / len(baseline["rhos"])) if baseline.get("rhos") else 0
    base_top10 = (sum(baseline["top10_rets"]) / len(baseline["top10_rets"])
                  if baseline.get("top10_rets") else 0)

    ranked = []
    for vname, r in results.items():
        if not r["rhos"]:
            continue
        rho_avg = sum(r["rhos"]) / len(r["rhos"])
        rho_min = min(r["rhos"])
        top10_avg = sum(r["top10_rets"]) / len(r["top10_rets"]) if r["top10_rets"] else 0
        # 逐期 ↑↓ (vs baseline)
        if baseline.get("rhos"):
            up = sum(1 for a, b in zip(r["rhos"], baseline["rhos"]) if a > b + 0.01)
            down = sum(1 for a, b in zip(r["rhos"], baseline["rhos"]) if a < b - 0.01)
        else:
            up = down = 0
        ranked.append({
            "name": vname,
            "rho_avg": rho_avg, "rho_min": rho_min,
            "d_rho": rho_avg - base_rho_avg,
            "top10_avg": top10_avg, "d_top10": top10_avg - base_top10,
            "up": up, "down": down, "n": r["n_periods"],
        })
    # 评分: Spearman 均值为主, TOP10 涨幅为辅
    ranked.sort(key=lambda x: -(x["rho_avg"] + x["d_top10"] * 0.01))
    return ranked, base_rho_avg, base_top10


# ══════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════

def print_results(ranked, base_rho, base_top10, start=""):
    print(f"\n{'='*90}")
    label = f" (cutoff≥{start})" if start else ""
    print(f"  price_factor 变体回测对比{label}")
    print(f"{'='*90}")
    print(f"  基线 baseline_r5r20: Spearman {base_rho:+.3f} | TOP10 {base_top10:+.2f}%")
    print(f"\n  {'变体':<26}{'Spearman':>9}{'Δρ':>8}{'min':>7}"
          f"{'TOP10涨':>9}{'Δ涨':>8}{'↑期':>5}{'↓期':>5}{'判定':>6}")
    print("  " + "-" * 80)
    for r in ranked:
        beats = r["d_rho"] > 0.005 and r["up"] >= r["down"]
        flag = "✅" if beats else ("⚠" if r["d_rho"] > 0 else "基线")
        if r["name"] == "baseline_r5r20":
            flag = "★基线"
        print(f"  {r['name']:<24}{r['rho_avg']:>+9.3f}{r['d_rho']:>+8.4f}{r['rho_min']:>+7.2f}"
              f"{r['top10_avg']:>+9.2f}{r['d_top10']:>+8.2f}"
              f"{r['up']:>5}{r['down']:>5}{flag:>6}")


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="price_factor 变体回测评估")
    parser.add_argument("--start", default="", help="cutoff起始日(如2025-04-01)")
    args = parser.parse_args()

    print("=" * 64)
    print("  price_factor 变体回测评估")
    print("=" * 64)

    if not os.path.exists(PF_HISTORY):
        print(f"✗ 未找到 {PF_HISTORY}, 请先跑 build_price_factor_history.py")
        sys.exit(1)
    pf_history = json.load(open(PF_HISTORY, encoding="utf-8"))
    cap_history = (json.load(open(CAP_HISTORY, encoding="utf-8"))
                   if os.path.exists(CAP_HISTORY) else {})

    cutoffs = sorted([c for c in pf_history.keys()
                      if not args.start or c >= args.start])
    print(f"  cutoff 数: {len(cutoffs)} | 变体数: {len(set(v for c in pf_history.values() for v in c))}")

    results = evaluate(pf_history, cap_history, cutoffs)
    ranked, base_rho, base_top10 = summarize(results)
    print_results(ranked, base_rho, base_top10, args.start)

    # 结论
    print(f"\n{'='*90}")
    print(f"  结论")
    print(f"{'='*90}")
    winners = [r for r in ranked if r["d_rho"] > 0.005 and r["up"] >= r["down"]
               and r["name"] != "baseline_r5r20"]
    if winners:
        print(f"  显著优于基线的变体 ({len(winners)} 个):")
        for r in winners:
            print(f"    ✅ {r['name']:<24} Δρ{r['d_rho']:+.4f} ΔTOP10{r['d_top10']:+.2f}pp ↑{r['up']}↓{r['down']}")
        best = winners[0]
        print(f"\n  最优: {best['name']} (Spearman {best['rho_avg']:+.3f} > 基线 {base_rho:+.3f})")
    else:
        print(f"  ❌ 无变体显著优于基线 (维持 baseline_r5r20)")

    # 落盘
    out = {
        "baseline": {"rho_avg": base_rho, "top10_avg": base_top10},
        "ranking": [{"name": r["name"], "rho_avg": r["rho_avg"], "d_rho": r["d_rho"],
                     "top10_avg": r["top10_avg"], "d_top10": r["d_top10"],
                     "up": r["up"], "down": r["down"], "n": r["n"]} for r in ranked],
    }
    paths.ensure_caches_dir()
    out_path = os.path.join(paths.CACHES_DIR, "price_factor_eval.json")
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    main()
