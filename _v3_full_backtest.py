#!/usr/bin/env python3
"""
V3 全量 vs 半年涨幅回测 (2025-12-09 → 2026-06-09)

⚠️ 前视偏差：fundamentals 快照含已兑现叙事，ρ 系统性虚高。仅供横向对比。
"""
import json, os, sys, math, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "fundamentals-scorer", "scripts"))
from backtest_correlation import get_price_tencent, get_price_akshare, get_price_mootdx

V3_FILE = os.path.join(ROOT, ".fundamental_v3_scores.json")
V2_FILE = os.path.join(ROOT, ".fundamental_llm_scores.json")

def spearman(xs, ys):
    n = len(xs)
    if n < 3: return 0, 0
    def rank(arr):
        idx = sorted(range(n), key=lambda i: arr[i])
        r = [0.0] * n; i = 0
        while i < n:
            j = i
            while j < n and arr[idx[j]] == arr[idx[i]]: j += 1
            avg = (i + j - 1) / 2.0 + 1
            for k in range(i, j): r[idx[k]] = avg
            i = j
        return r
    xr, yr = rank(xs), rank(ys)
    mx, my = sum(xr)/n, sum(yr)/n
    cov = sum((xr[i]-mx)*(yr[i]-my) for i in range(n))
    sx = math.sqrt(sum((r-mx)**2 for r in xr))
    sy = math.sqrt(sum((r-my)**2 for r in yr))
    rho = cov/(sx*sy) if sx*sy>0 else 0
    t = rho*math.sqrt((n-2)/(1-rho*rho)) if abs(rho)<1 else 0
    return rho, t

def pearson(xs, ys):
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    cov = sum((xs[i]-mx)*(ys[i]-my) for i in range(n))
    sx = math.sqrt(sum((x-mx)**2 for x in xs))
    sy = math.sqrt(sum((y-my)**2 for y in ys))
    return cov/(sx*sy) if sx*sy>0 else 0

print("加载评分...")
v3 = json.load(open(V3_FILE))
v2raw = json.load(open(V2_FILE))
v2c = {k.split('_')[0]: v for k, v in v2raw.items()}

rows = []
for code, d in v3.items():
    if "sector_score" not in d: continue
    v2 = v2c.get(code, {})
    rows.append({
        "code": code,
        "v3": d["sector_score"],
        "v3_chain": d.get("chain"),
        "v3_deliv": d.get("delivery"),
        "v3_cap": d.get("capital"),
        "v2_sect": v2.get("sector_score"),
        "v2_fund": v2.get("fundamental_score"),
        "v2_total": v2.get("total"),
    })

print(f"V3评分 {len(rows)} 只，获取半年涨幅...")
providers = [get_price_tencent, get_price_akshare, get_price_mootdx]
ok = 0
for s in rows:
    price = None
    for p in providers:
        price = p(s["code"])
        if price and price[0] > 0: break
    if price: s["ret"] = (price[1]-price[0])/price[0]; ok += 1
    else: s["ret"] = None
    time.sleep(0.03)

merged = [s for s in rows if s["ret"] is not None]
rets = [s["ret"] for s in merged]
n = len(merged)
print(f"有效样本 {ok}/{len(rows)}\n")

# ====== 相关性 ======
print("=" * 75)
print(f"  V3 全量 vs 半年涨幅 (n={n})")
print("=" * 75)
print(f"  {'维度':<20}{'Spearman ρ':>12}{'t-stat':>10}{'显著性':>10}{'Pearson r':>12}")

def sig(t):
    if abs(t) > 3.3: return "***"
    elif abs(t) > 2.6: return "**"
    elif abs(t) > 1.96: return "*"
    return ""

for label, xs in [
    ("V3 total (链+绩+资)", [s["v3"] for s in merged]),
    ("V3 产业链位置", [s["v3_chain"] or 0 for s in merged]),
    ("V3 业绩兑现度", [s["v3_deliv"] or 0 for s in merged]),
    ("V3 资金关注度", [s["v3_cap"] or 0 for s in merged]),
    ("V2 sector_score", [s["v2_sect"] or 0 for s in merged]),
    ("V2 fundamental", [s["v2_fund"] or 0 for s in merged]),
    ("V2 total (基本+赛道)", [s["v2_total"] or 0 for s in merged]),
]:
    rho, t = spearman(xs, rets); r = pearson(xs, rets)
    s = sig(t)
    print(f"  {label:<20}{rho:>12.4f}{t:>10.2f}{s:>10}{r:>12.4f}")

# ====== 分组回测 ======
sm = sorted(merged, key=lambda x: x["v3"])
qsize = n // 5
print(f"\n{'='*75}")
print(f"  V3 五等分分组回测")
print(f"{'='*75}")
for q in range(5):
    si = q * qsize
    ei = si + qsize if q < 4 else n
    grp = sm[si:ei]
    avg_r = sum(s["ret"] for s in grp) / len(grp)
    avg_v = sum(s["v3"] for s in grp) / len(grp)
    pos = sum(1 for s in grp if s["ret"] > 0)
    print(f"  Q{q+1} (V3 {grp[0]['v3']:.1f}-{grp[-1]['v3']:.1f}, avg={avg_v:.1f}): "
          f"涨幅{avg_r*100:+.1f}%  上涨{pos}/{len(grp)}")

top20 = sm[-20:]; bot20 = sm[:20]
at = sum(s["ret"] for s in top20)/20
ab = sum(s["ret"] for s in bot20)/20
print(f"\n  Top20(V3高分)均涨幅: {at*100:+.1f}%")
print(f"  Bot20(V3低分)均涨幅: {ab*100:+.1f}%")
print(f"  多空收益差: {(at-ab)*100:+.1f}%")

# V3: 链+资(去掉兑现度)重配
chain_cap = [s["v3_chain"]+s["v3_cap"] for s in merged]
rho_cc, t_cc = spearman(chain_cap, rets)
print(f"\n  重配比「链+资(去兑现)」ρ={rho_cc:.4f} (t={t_cc:.2f}) {'*' if abs(t_cc)>1.96 else ''}")

out = {"n": n, "v3_rho": round(spearman([s["v3"] for s in merged], rets)[0], 4),
       "v2_sect_rho": round(spearman([s["v2_sect"] or 0 for s in merged], rets)[0], 4),
       "top20": round(at, 4), "bot20": round(ab, 4), "spread": round(at-ab, 4)}
json.dump(out, open(os.path.join(ROOT, ".v3_full_backtest.json"), "w"), ensure_ascii=False, indent=2)
print(f"\n💾 已保存 .v3_full_backtest.json")