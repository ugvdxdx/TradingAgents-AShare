#!/usr/bin/env python3
"""
头部30只 V3 vs V2 区分度回测

⚠️ 前视偏差警告：fundamentals 快照为 2026-06-07，含涨幅区间内已兑现的业绩叙事，
   故 ρ 会系统性虚高。本回测只用于横向对比「V3小数排序 vs V2整数死锁」哪个
   与实际涨幅更一致，不可作为 V3 的绝对 alpha 证据。
"""
import json, os, sys, math, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "fundamentals-scorer", "scripts"))
from backtest_correlation import get_price_tencent, get_price_akshare, get_price_mootdx  # 复用取价

START_DATE, END_DATE = "2025-12-09", "2026-06-09"


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0, 0.0

    def rank(arr):
        idx = sorted(range(n), key=lambda i: arr[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n and arr[idx[j]] == arr[idx[i]]:
                j += 1
            avg = (i + j - 1) / 2.0 + 1
            for k in range(i, j):
                r[idx[k]] = avg
            i = j
        return r

    xr, yr = rank(xs), rank(ys)
    mx, my = sum(xr) / n, sum(yr) / n
    cov = sum((xr[i] - mx) * (yr[i] - my) for i in range(n))
    sx = math.sqrt(sum((r - mx) ** 2 for r in xr))
    sy = math.sqrt(sum((r - my) ** 2 for r in yr))
    rho = cov / (sx * sy) if sx * sy > 0 else 0.0
    t = rho * math.sqrt((n - 2) / (1 - rho * rho)) if abs(rho) < 1 else float("inf")
    return rho, t


def main():
    v3 = json.load(open(os.path.join(ROOT, ".fundamental_v3_scores.json")))
    v2raw = json.load(open(os.path.join(ROOT, ".fundamental_llm_scores.json")))
    v2c = {k.split("_")[0]: v for k, v in v2raw.items()}

    rows = []
    for code, r in v3.items():
        if "sector_score" not in r:
            continue
        rows.append({
            "code": code,
            "v3": r["sector_score"],
            "v2": v2c.get(code, {}).get("sector_score"),
        })

    print(f"样本 {len(rows)} 只，获取半年涨幅 ({START_DATE} → {END_DATE})…")
    providers = [get_price_tencent, get_price_akshare, get_price_mootdx]
    ok = 0
    for s in rows:
        price = None
        for p in providers:
            price = p(s["code"])
            if price:
                break
        if price and price[0] > 0 and price[1] > 0:
            s["ret"] = (price[1] - price[0]) / price[0]
            ok += 1
        else:
            s["ret"] = None
        time.sleep(0.05)

    merged = [s for s in rows if s["ret"] is not None and s["v2"] is not None]
    print(f"有效样本 {len(merged)} / {len(rows)}（取价失败 {len(rows)-ok}）\n")

    rets = [s["ret"] for s in merged]
    v3s = [s["v3"] for s in merged]
    v2s = [float(s["v2"]) for s in merged]

    rho3, t3 = spearman(v3s, rets)
    rho2, t2 = spearman(v2s, rets)

    print("=" * 64)
    print(f"  头部30只 区分度回测 (n={len(merged)})")
    print("=" * 64)
    print(f"  {'分数':<14}{'唯一值':>8}{'Spearman ρ':>14}{'t-stat':>10}")
    print(f"  {'-'*46}")
    print(f"  {'V2 整数赛道':<13}{len(set(v2s)):>8}{rho2:>14.4f}{t2:>10.2f}")
    print(f"  {'V3 小数赛道':<13}{len(set(v3s)):>8}{rho3:>14.4f}{t3:>10.2f}")
    print(f"\n  注：V2 仅 {len(set(v2s))} 个分值，大量并列 → 秩相关分母塌缩，ρ 失真")

    # 分组：V3 前半 vs 后半，看涨幅差（这是头部区分度的直接检验）
    sm = sorted(merged, key=lambda x: -x["v3"])
    half = len(sm) // 2
    top, bot = sm[:half], sm[half:]
    at = sum(s["ret"] for s in top) / len(top)
    ab = sum(s["ret"] for s in bot) / len(bot)
    print(f"\n  V3 高分半区 (n={len(top)}) 平均涨幅: {at*100:+.2f}%")
    print(f"  V3 低分半区 (n={len(bot)}) 平均涨幅: {ab*100:+.2f}%")
    print(f"  头部内部多空差: {(at-ab)*100:+.2f}%")

    # V2=25 子集（最严重的死锁组），看 V3 能否在其内部分出涨幅梯度
    locked = [s for s in merged if s["v2"] == 25]
    if len(locked) >= 4:
        lk = sorted(locked, key=lambda x: -x["v3"])
        h = len(lk) // 2
        ath = sum(s["ret"] for s in lk[:h]) / h
        abh = sum(s["ret"] for s in lk[h:]) / (len(lk) - h)
        rL, tL = spearman([s["v3"] for s in lk], [s["ret"] for s in lk])
        print(f"\n  ── V2=25 死锁组内部检验 (n={len(lk)}) ──")
        print(f"  仅看这组：V3 vs 涨幅 ρ={rL:.4f} (t={tL:.2f})")
        print(f"  V3高分半 {ath*100:+.2f}% vs 低分半 {abh*100:+.2f}% → 差 {(ath-abh)*100:+.2f}%")

    print("\n  涨幅明细（按 V3 降序）：")
    for s in sm:
        print(f"   {s['code']}  V2={s['v2']:>2} V3={s['v3']:>4.1f}  半年涨幅 {s['ret']*100:>+7.2f}%")

    out = {
        "warning": "前视偏差：fundamentals快照2026-06-07，ρ系统性虚高，仅供V2/V3横向对比",
        "n": len(merged), "v3_rho": round(rho3, 4), "v2_rho": round(rho2, 4),
        "v3_unique": len(set(v3s)), "v2_unique": len(set(v2s)),
        "top_half_ret": round(at, 4), "bot_half_ret": round(ab, 4),
    }
    json.dump(out, open(os.path.join(ROOT, ".v3_head_backtest.json"), "w"),
              ensure_ascii=False, indent=2)
    print(f"\n💾 已保存 .v3_head_backtest.json")


if __name__ == "__main__":
    main()
