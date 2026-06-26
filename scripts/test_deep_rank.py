#!/usr/bin/env python3
"""深辩排序优化测试: 跳过海选, 从候选池分层抽样30只直接进排名辩论。

用法:
  uv run python3 scripts/test_deep_rank.py --date 2026-04-24
  uv run python3 scripts/test_deep_rank.py --date 2026-04-24 --sample 40

验证: 深辩产出的TOP10排名 vs 该30只的实际10/30日涨幅。
用于快速迭代优化排名辩论的排序质量, 成熟后再迁移给浅辩(海选)。
"""
import argparse
import json
import os
import random
import sys
import pickle
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(override=True)

import picker.paths as paths
from tradingagents.agents.picker.data_io import load_top_n, load_kline
from tradingagents.agents.picker.debaters import (
    _run_debate_unit, _adjudicate, _finalize_ranking, _dump, _trace,
)
from tradingagents.agents.picker.picker_state import new_debate_ledger
from tradingagents.agents.picker.llm_helper import LLMHelper


def stratified_sample(cands, n=30, seed=42):
    """分层抽样: 按v3分高/中/低三层, 各抽 n//3 只。

    保证v3低的新晋股一定被抽到, 避免纯随机漏测。
    """
    rng = random.Random(seed)
    sorted_c = sorted(cands, key=lambda x: x.get("v3", 0))
    third = len(sorted_c) // 3
    tiers = [sorted_c[:third], sorted_c[third:2*third], sorted_c[2*third:]]
    per = n // 3
    sampled = []
    for tier in tiers:
        # 优先抽新晋股/研报股 (确保身份标记股被测到)
        tagged = [c for c in tier if c.get("_rising_star") or c.get("_research_hot")]
        normal = [c for c in tier if not (c.get("_rising_star") or c.get("_research_hot"))]
        rng.shuffle(tagged); rng.shuffle(normal)
        pick = tagged[:max(1, per // 3)] + normal[:per - min(per // 3, len(tagged))]
        sampled.extend(pick[:per])
    return sampled[:n]


def real_returns(code, cutoff_date):
    """算cutoff日后10/30交易日真实涨幅"""
    suf = "_SH" if code.startswith("6") else "_SZ"
    p = os.path.join(paths.KLINE_CACHE_DIR, f"{code}{suf}".replace(".", "_") + ".pkl")
    if not os.path.exists(p):
        return None, None
    df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
    mask = df["trade_date"] <= cutoff_date
    if mask.sum() == 0:
        return None, None
    base_idx = mask.sum() - 1
    base = df["close"].iloc[base_idx]
    n = len(df)
    r10 = (df["close"].iloc[min(base_idx + 10, n - 1)] / base - 1) * 100 if base_idx + 10 < n else None
    r30 = (df["close"].iloc[min(base_idx + 30, n - 1)] / base - 1) * 100 if base_idx + 30 < n else None
    return r10, r30


def main():
    ap = argparse.ArgumentParser(description="深辩排序优化测试")
    ap.add_argument("--date", type=str, default="", help="单个回测截止日 (如 2026-04-24)")
    ap.add_argument("--dates", type=str, default="", help="多个日期逗号分隔 (如 2026-04-03,2026-04-24)")
    ap.add_argument("--sample", type=int, default=30, help="抽样规模")
    ap.add_argument("--rounds", type=int, default=3, help="深辩轮次")
    ap.add_argument("--seed", type=int, default=42, help="随机种子(可复现)")
    ap.add_argument("--version", type=str, default="v6",
                    choices=["v5", "v6", "v7"],
                    help="排序版本: v5=纯LLM排序 v6=LLM+动量prompt v7=V3量化锚+LLM风险下调")
    ap.add_argument("--parallel", type=int, default=3, help="并行日期数(每个占一份LLM并发)")
    args = ap.parse_args()

    dates = []
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    elif args.date:
        dates = [args.date]
    if not dates:
        ap.error("请指定 --date 或 --dates")

    if len(dates) == 1:
        run_single(dates[0], args)
    else:
        run_batch(dates, args)


def run_batch(dates, args):
    """批量并行: 多个日期同时跑, 最后汇总跨期规律。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"{'='*60}")
    print(f"  批量深辩测试 — {len(dates)}个时间点, 并行{args.parallel}")
    print(f"  日期: {', '.join(dates)}")
    print(f"{'='*60}")

    results = {}
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {ex.submit(run_single, d, args, quiet=True): d for d in dates}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                results[d] = fut.result()
                r = results[d]
                print(f"\n[完成] {d}: 命中{r['hit10']}/10, 新晋股预测#{r['star_pred_avg']:.0f} vs 实际#{r['star_actual_avg']:.0f}")
            except Exception as e:
                print(f"\n[失败] {d}: {e}")
                results[d] = None

    # 汇总跨期规律
    print(f"\n{'='*80}")
    print(f"  跨期汇总 ({sum(1 for v in results.values() if v)}个有效样本)")
    print(f"{'='*80}")
    valid = {d: r for d, r in results.items() if r}
    if not valid:
        print("  无有效结果"); return

    hits = [r["hit10"] for r in valid.values()]
    star_gaps = [r["star_pred_avg"] - r["star_actual_avg"] for r in valid.values() if r.get("star_pred_avg")]
    print(f"\n  TOP10命中率: {hits} 均值{sum(hits)/len(hits):.1f}/10")
    print(f"  新晋股排名偏差(预测-实际, 负=低估): {[round(g) for g in star_gaps]} 均值{sum(star_gaps)/len(star_gaps):.0f}")
    print(f"  (负值=系统性低估新晋股, 正值=高估)")

    # 每期TOP5实际涨幅股的特征
    print(f"\n  各期实际涨幅TOP3:")
    for d, r in sorted(valid.items()):
        for i, (code, name, ret) in enumerate(r["actual_top3"]):
            tag = "★" if r["actual_top3_tags"][i] else " "
            pred = r["actual_top3_pred_rank"][i]
            print(f"    {d}: #{i+1} {code} {name} {tag} 30日{ret:+.0f}% 预测排名{pred}")


def _v7_ranking(sample, state, ledger):
    """v7: V3+r20 混合量化锚 + LLM风险下调。

    回测证明:
    - V3分与30日涨幅Spearman=+0.52, 强预测因子
    - r20在正常市场期也有正相关(+0.2~+0.5)
    - 纯LLM从头排序破坏了量化信号 (Spearman变负)
    v7 不让LLM重新排序, 而是:
      1. 基础排名 = V3×0.65 + r20归一×0.35 混合分降序
      2. LLM空头claim → 风险下调 (降低门槛: 任何未决空头都计入)
      3. 多头claim → 不上调 (V3+r20已是上限, 防止LLM过度乐观)
    """
    from tradingagents.agents.picker.judges import _confidence_level

    # 1. chain+capital 降序 = 基础排名
    # 回测验证: chain+capital Spearman=+0.60, 优于V3总分(+0.52)
    # surge子维度预测力弱(+0.10), 包含在V3总分里反而稀释信号
    for c in sample:
        c["_anchor_score"] = c.get("chain", 0) + c.get("capital", 0)
    base = sorted(sample, key=lambda x: -x.get("_anchor_score", 0))
    base_rank = {c["code"]: i + 1 for i, c in enumerate(base)}

    # 2. LLM风险计分
    # v7b 修复: 不依赖unresolved状态 (多头几乎把所有空头都标成resolved, 导致风险下调失效)
    # 直接用空头claim本身: 只要空头提出了conf>=0.6的攻击, 就计入风险分
    # 多头claim只有在明确反驳了某空头时才能抵消 (target_claim_ids指向该空头)
    claims = ledger.get("claims", [])
    bear_claims = {}  # code -> [(claim_id, conf)]
    for cl in claims:
        code = cl.get("code", "")
        if not code:
            continue
        if cl.get("stance") == "bearish":
            bear_claims.setdefault(code, []).append(
                (cl.get("claim_id", ""), float(cl.get("confidence", 0.6) or 0.6)))
    # 多头反驳: target_claim_ids 明确指向被反驳的空头
    bull_rebutted = set()  # 被多头明确反驳的空头claim_id
    for cl in claims:
        if cl.get("stance") == "bullish":
            for tid in cl.get("target_claim_ids", []):
                bull_rebutted.add(tid)

    code_scores = {}
    for code, bears in bear_claims.items():
        score = 0
        for cid, conf in bears:
            if cid in bull_rebutted:
                continue  # 被多头明确反驳, 不计
            score -= conf  # 未被反驳的空头攻击计入
        code_scores[code] = score

    max_drop = int((state.get("metadata") or {}).get("max_rank_drop", 4))
    # 3. 风险下调
    adjusted = []
    for c in sample:
        code = c["code"]
        delta = 0
        score = code_scores.get(code, 0)
        if score < -0.3:  # 降低门槛: 任何有效空头攻击都下调
            delta = -min(int(abs(score) / 0.3) + 1, max_drop)
        adjusted.append({
            "code": code, "name": c["name"],
            "v3": c.get("v3", 0),
            "anchor": round(c.get("_anchor_score", 0), 1),
            "_base_rank": base_rank[code],
            "_delta": delta,
            "_risk_score": round(score, 2),
            "confidence": 0.7 if delta == 0 else max(0.4, 0.7 + score * 0.1),
            "key_thesis": c.get("essence", {}).get("biggest_bull", ""),
            "key_risk": c.get("essence", {}).get("biggest_bear", ""),
        })

    adjusted.sort(key=lambda x: (x["_base_rank"] + x["_delta"], x["_base_rank"]))

    claims_by_code = {}
    for cl in claims:
        claims_by_code.setdefault(cl.get("code", ""), []).append(cl.get("claim_id"))
    ranking = []
    for i, a in enumerate(adjusted):
        risk_flags = []
        if a["_delta"] < 0:
            risk_flags.append(f"LLM风险下调{abs(a['_delta'])}位(score={a['_risk_score']})")
        ranking.append({
            "rank": i + 1,
            "code": a["code"], "name": a["name"],
            "score": a["anchor"],
            "confidence": round(a["confidence"], 2),
            "confidence_level": _confidence_level(a["confidence"]),
            "key_thesis": a["key_thesis"],
            "key_risk": a["key_risk"],
            "supporting_claim_ids": claims_by_code.get(a["code"], [])[:5],
            "risk_flags": risk_flags,
        })
    return ranking


def run_single(cutoff, args, quiet=False):
    """单个时间点的深辩测试, 返回汇总指标 dict。"""
    run_dir = os.path.join("results", "deep_rank_test", cutoff)
    os.makedirs(run_dir, exist_ok=True)

    def log(msg):
        if not quiet:
            print(msg)

    log(f"\n{'='*60}")
    log(f"  深辩测试 — cutoff={cutoff}, 样本{args.sample}只, {args.rounds}轮")
    log(f"{'='*60}")

    # 1. 加载候选池 (回测模式, 截断到cutoff)
    llm = LLMHelper({})
    log("\n[1] 加载候选池...")
    cands = load_top_n(50, cutoff_date=cutoff)
    from tradingagents.agents.picker.data_io import load_mf_cache, fund_flow_5d
    from picker.scoring.tech_analysis import compute_tech_score
    mf = load_mf_cache()
    ready = []
    for s in cands:
        df = load_kline(s["code"], cutoff)
        if df is None:
            continue
        ts = compute_tech_score(df.reset_index() if hasattr(df, "index") else df)
        # TechScore 可能是 dataclass 或 float
        if hasattr(ts, "total"):
            s["tech_total"] = float(ts.total)
            s["tech_trend"] = float(ts.trend)
            s["tech_mom"] = float(ts.momentum)
        else:
            s["tech_total"] = float(ts)
            s["tech_trend"] = s["tech_total"] * 0.4
            s["tech_mom"] = s["tech_total"] * 0.3
        fund = fund_flow_5d(mf, s["code"], cutoff)
        s["fund_5d"] = fund if fund is not None else 0.0
        s["data_quality"] = "ok"
        # v6: 量价动量信号
        df_k = df.sort_values("trade_date").reset_index(drop=True) if hasattr(df, "sort_values") else df
        close = df_k["close"] if hasattr(df_k, "close") else None
        if close is not None and len(close) >= 21:
            s["r5"] = round((close.iloc[-1] / close.iloc[-6] - 1) * 100, 1)
            s["r20"] = round((close.iloc[-1] / close.iloc[-21] - 1) * 100, 1)
            s["dist_high"] = round((close.iloc[-1] / close.iloc[-20:].max() - 1) * 100, 1)
        ready.append(s)
    cands = ready
    log(f"  候选池: {len(cands)}只")

    # 2. 分层抽样
    sample = stratified_sample(cands, n=args.sample, seed=args.seed)
    log(f"\n[2] 分层抽样: {len(sample)}只")
    star_n = sum(1 for c in sample if c.get("_rising_star"))
    log(f"  含新晋股★: {star_n}只")

    # 3. 深辩
    state = {
        "candidates": cands, "run_dir": run_dir, "cutoff_date": cutoff,
        "incremental_briefs": {}, "analyst_reports": {},
        "research_context": "", "rotation_context": "",
        "metadata": {"debate_top_k": 10, "max_rank_drop": 3},
    }
    ledger = new_debate_ledger(args.rounds)
    log(f"\n[3] 深辩 {args.rounds}轮...")
    for rnd in range(1, args.rounds + 1):
        _run_debate_unit(llm, sample, state, ledger, rnd, args.rounds, deep=True)
        log(f"  R{rnd}/{args.rounds}: claim累计{len(ledger.get('claims', []))}条")

    # 4. 裁决 (v5/v6: LLM排序; v7: V3量化锚+LLM风险下调)
    if args.version == "v7":
        log(f"\n[4] v7裁决: V3量化锚 + LLM风险下调...")
        ranking = _v7_ranking(sample, state, ledger)
    else:
        log(f"\n[4] {args.version}裁决: LLM从头排序...")
        ordered = _adjudicate(llm, sample, state, ledger, produce_rank=True, deep=True)
        ranked_items = [o for o in ordered if isinstance(o, dict)][:15]
        ranking = _finalize_ranking(ranked_items, ledger, state)

    _dump(run_dir, "ranking.json", ranking, as_json=True)
    _dump(run_dir, "ledger.json", ledger.get("claims", []), as_json=True)
    _dump(run_dir, "sample.json",
          [{"code": c["code"], "name": c["name"], "v3": c["v3"],
            "star": c.get("_rising_star", False)} for c in sample], as_json=True)

    # 5. 验证
    actual_r30 = sorted(
        [(c, r30) for c in sample if (r30 := real_returns(c["code"], cutoff)[1]) is not None],
        key=lambda x: -x[1])

    pred_top10 = set(r["code"] for r in ranking[:10])
    actual_top10 = set(c["code"] for c, _ in actual_r30[:10])
    hit10 = len(pred_top10 & actual_top10)

    # 新晋股排名对比
    star_preds, star_actuals = [], []
    for c in sample:
        if not c.get("_rising_star"):
            continue
        p_rank = next((r["rank"] for r in ranking if r["code"] == c["code"]), 999)
        a_rank = next((i + 1 for i, (cc, _) in enumerate(actual_r30) if cc["code"] == c["code"]), 999)
        star_preds.append(p_rank); star_actuals.append(a_rank)

    star_pred_avg = sum(star_preds) / len(star_preds) if star_preds else 0
    star_actual_avg = sum(star_actuals) / len(star_actuals) if star_actuals else 0

    # 实际TOP3 的预测排名
    actual_top3 = [(c["code"], c["name"], r30) for c, r30 in actual_r30[:3]]
    actual_top3_pred_rank = [
        next((r["rank"] for r in ranking if r["code"] == code), 999)
        for code, _, _ in actual_top3]
    actual_top3_tags = [
        next((c.get("_rising_star", False) for c in sample if c["code"] == code), False)
        for code, _, _ in actual_top3]

    if not quiet:
        print(f"\n{'预测排名':<6}{'代码':<8}{'名称':<10}{'v3':>5}{'身份':<4}{'10日':>8}{'30日':>8}{'实际#':>6}")
        print("-" * 65)
        for r in ranking[:15]:
            c = next((cc for cc in sample if cc["code"] == r["code"]), {})
            r10, r30 = real_returns(r["code"], cutoff)
            star = "★" if c.get("_rising_star") else " "
            a_rank = next((i + 1 for i, (cc, _) in enumerate(actual_r30) if cc["code"] == r["code"]), 999)
            r10s = f"{r10:+.1f}%" if r10 is not None else "N/A"
            r30s = f"{r30:+.1f}%" if r30 is not None else "N/A"
            print(f"  #{r['rank']:<4}{r['code']:<8}{r['name']:<10}{c.get('v3',0):>5.1f} {star}  {r10s:>8}{r30s:>8}{a_rank:>6}")
        print(f"\n  命中率: {hit10}/10 | 新晋股 预测#{star_pred_avg:.0f} vs 实际#{star_actual_avg:.0f}")

    return {
        "hit10": hit10,
        "star_pred_avg": star_pred_avg, "star_actual_avg": star_actual_avg,
        "actual_top3": actual_top3,
        "actual_top3_pred_rank": actual_top3_pred_rank,
        "actual_top3_tags": actual_top3_tags,
        "ranking": ranking,
    }


if __name__ == "__main__":
    main()
