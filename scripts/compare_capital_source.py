#!/usr/bin/env python3
"""对比 capital 来源: G模式量化capital vs LLM直出capital, 哪个排序预测力更强。

V3 cache 同时存了:
  - capital: G模式量化重算 (base+D2×2+pf×2, 无封顶, 每日更新)
  - sector_score_model: LLM原始 sector_score (chain+delivery+LLM_capital)
  → LLM_capital = sector_score_model - chain - delivery (LLM打的0-5分)

对比两个锚的 Spearman:
  锚A (G模式):  chain + capital_G×2 - delivery×0.5   ← 当前生产
  锚B (LLM直出): chain + capital_L×2 - delivery×0.5  ← 用户提议

用法: python3 scripts/compare_capital_source.py
"""
import json, os, sys, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picker.paths as paths

C = json.load(open(paths.V3_CACHE))
HOLD = 30


def real_returns(code, cutoff, days):
    suf = "_SH" if code.startswith("6") else "_SZ"
    p = os.path.join(paths.KLINE_CACHE_DIR, f"{code}{suf}".replace(".", "_") + ".pkl")
    if not os.path.exists(p): return None
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    m = df["trade_date"] <= cutoff
    if m.sum() == 0: return None
    bi = m.sum() - 1
    if bi + days >= len(df): return None
    return round((df["close"].iloc[bi + days] / df["close"].iloc[bi] - 1) * 100, 2)


def spearman(a, b):
    def ranks(vals):
        si = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0] * len(vals)
        for pos, i in enumerate(si): r[i] = pos + 1
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra)/n, sum(rb)/n
    num = sum((x-ma)*(y-mb) for x, y in zip(ra, rb))
    da = sum((x-ma)**2 for x in ra); db = sum((y-mb)**2 for y in rb)
    return num / ((da**0.5)*(db**0.5)) if da*db > 0 else 0


def get_cutoffs(step=2):
    p = os.path.join(paths.KLINE_CACHE_DIR, "300308_SZ.pkl")
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    return [df["trade_date"].iloc[i] for i in range(20, len(df) - HOLD - 1, step)]


def run():
    cutoffs = get_cutoffs()
    print(f"{'='*80}")
    print(f"  capital 来源对比 ({len(cutoffs)}期 × 全池 × {HOLD}日)")
    print(f"  锚A=G模式量化capital | 锚B=LLM直出capital")
    print(f"{'='*80}")

    factors = {
        "锚A: G模式cap×2-del×0.5 (生产)": lambda v: v.get("chain",0) + v.get("capital",0)*2 - v.get("delivery",0)*0.5,
        "锚B: LLM cap×2-del×0.5":        lambda v: v.get("chain",0) + llm_cap(v)*2 - v.get("delivery",0)*0.5,
        "G模式 sector_score (chain+del+Gcap)": lambda v: v.get("sector_score",0),
        "LLM sector_score_model (chain+del+LLMcap)": lambda v: v.get("sector_score_model",0),
        "纯 chain (无capital)":           lambda v: v.get("chain",0),
    }
    res = {k: [] for k in factors}

    for cutoff in cutoffs:
        rows = []
        for code, v in C.items():
            if not isinstance(v, dict) or "chain" not in v: continue
            if not isinstance(v.get("sector_score_model"), (int, float)): continue  # 需LLM capital可反推
            r = real_returns(code, cutoff, HOLD)
            if r is None: continue
            rows.append((v, r))
        if len(rows) < 10:
            for k in factors: res[k].append(None)
            continue
        rets = [r for _, r in rows]
        for k, fn in factors.items():
            res[k].append(spearman([fn(v) for v, _ in rows], rets))

    print(f"\n{'因子':<40} {'avg':>7} {'min':>6} {'胜率':>7}")
    print("-" * 65)
    ranked = sorted(res.items(), key=lambda x: -sum(r for r in x[1] if r is not None)/max(1, len([r for r in x[1] if r is not None])))
    for k, rhos in ranked:
        valid = [r for r in rhos if r is not None]
        if not valid: continue
        avg = sum(valid)/len(valid); mn = min(valid); pos = sum(1 for r in valid if r > 0)
        print(f"  {k:<38} {avg:>+7.3f} {mn:>+6.2f} {pos:>4}/{len(valid)}")

    ga = sum(r for r in res[list(factors)[0]] if r is not None)/len([r for r in res[list(factors)[0]] if r is not None])
    gb = sum(r for r in res[list(factors)[1]] if r is not None)/len([r for r in res[list(factors)[1]] if r is not None])
    print(f"\n{'='*80}")
    print(f"  G模式锚 avg={ga:+.3f}  vs  LLM直出锚 avg={gb:+.3f}  (Δ={gb-ga:+.3f})")
    if gb > ga + 0.005:
        print(f"  \033[92m✓ LLM直出 capital 排序质量更高 ({gb-ga:+.3f}), 支持切换\033[0m")
    elif ga > gb + 0.005:
        print(f"  \033[91m→ G模式 capital 仍更优 ({ga-gb:+.3f}), 维持现状\033[0m")
    else:
        print(f"  \033[93m→ 两者基本持平, 但 LLM capital 无9.6伪信号 + 更简单, 倾向切换\033[0m")


def llm_cap(v):
    """从 sector_score_model 反推 LLM 原始 capital"""
    ssm = v.get("sector_score_model")
    if not isinstance(ssm, (int, float)): return v.get("capital", 0)
    return round(ssm - v.get("chain", 0) - v.get("delivery", 0), 1)


if __name__ == "__main__":
    run()
