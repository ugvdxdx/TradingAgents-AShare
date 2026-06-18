"""debate_picker v5 — 海选评委 + 终极PK 节点 (M3/M4)。"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List

from . import data_io
from .llm_helper import LLMHelper, extract_json_array
from .prompts import FINAL_JUDGE_SYSTEM


def _trace(node: str, note: str) -> dict:
    return {"node": node, "note": note, "ts": datetime.now().isoformat()}


def _dump(run_dir: str, name: str, content: Any, as_json: bool = False):
    path = os.path.join(run_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        if as_json:
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(content))


def format_stock_brief(c: Dict[str, Any]) -> str:
    """候选股精简档案 (喂给评委/辩论的统一格式)。"""
    e = c.get("essence", {})
    return (
        f"{c['code']} {c['name']} V3={c['v3']:.1f} [链{c['chain']}+兑{c['delivery']}+资{c['capital']}]\n"
        f"  卡位:{e.get('chain_position', '')} | 催化:{e.get('core_catalyst', '')}\n"
        f"  多头:{e.get('biggest_bull', '')} | 空头:{e.get('biggest_bear', '')}\n"
        f"  红线:{e.get('quality_redline', '')} | horizon:{e.get('catalyst_horizon', 'mid')}\n"
        f"  实时: tech={c['tech_total']:.0f}/100(趋势{c['tech_trend']:.0f}) "
        f"5日主力净{c['fund_5d']:+.1f}亿"
    )


def _apply_ranking(group: List[Dict[str, Any]], result: List[dict]) -> List[Dict[str, Any]]:
    """按 LLM 排序结果重排一组候选股, 遗漏的按 V3 补末尾。"""
    cmap = {c["code"]: c for c in group}
    ordered: List[Dict[str, Any]] = []
    for r in result:
        code = str(r.get("code", "")).strip()
        if code in cmap:
            c = cmap.pop(code)
            c["screen_reason"] = r.get("reason", "")
            ordered.append(c)
    for c in sorted(cmap.values(), key=lambda x: -x["v3"]):
        ordered.append(c)
    return ordered


# ══════════════════════════════════════════════════════════
# 阶段 3: 分组海选 Map-Reduce (50→20)
# ══════════════════════════════════════════════════════════

def make_screen_round1(llm: LLMHelper, v3_auto_promote: int = 20,
                       dark_horse_groups: int = 3, take_per_group: int = 2):
    """海选节点: 将 50 只候选收窄为决赛阵容 (debate_top_k 只)。

    支持三种 screen_mode (经 metadata 传入), 用于 A/B/C 对照实验:
      - "promote" (A, 现状): V3 Top-debate_top_k 直接进决赛 (黑马发掘仅作参考, 不占名额)。
      - "llm"     (B): 50 只全部经 LLM 海选(带先验+增量信息), 取 Top-debate_top_k。
      - "hybrid"  (C): V3 Top-force_k 保送 + 剩余经 LLM 海选竞争, 合并为 debate_top_k 只。

    关键修复: 决赛名单 round1_promoted 显式保序输出, 前 debate_top_k 即决赛阵容,
    黑马/海选晋级股不再被下游 V3 排序 + top_k 截断丢弃。

    设计理念: V3 基本面分是经过严格筛选的, 早期 LLM 海选(无先验)会系统性误杀 AI 主线龙头。
    引入先验(PRIOR_KNOWLEDGE)与增量信息后, 该偏见显著下降, 故开放 B/C 模式做对照评估。
    """
    def node(state) -> Dict[str, Any]:
        from .picker_state import new_debate_ledger
        cands = state.get("candidates", [])
        meta = state.get("metadata") or {}
        max_rounds = int(meta.get("max_debate_rounds", 3))
        screen_mode = str(meta.get("screen_mode", "promote"))
        debate_top_k = int(meta.get("debate_top_k", 10))
        force_k = int(meta.get("force_k", 6))  # hybrid 模式下的保送名额

        cands_sorted = sorted(cands, key=lambda x: -x["v3"])

        # 研报黑马保送: 近期有 bullish 催化但不在 Top50 的个股
        dark_horse_from_research = []
        research_risk_codes = set()
        try:
            from tradingagents.research.consumer import (
                get_dark_horse_stocks, get_research_risk_signals,
            )
            existing_codes = [c["code"] for c in cands_sorted]
            cutoff = state.get("cutoff_date") or state.get("trade_date", "")
            dark_horse_from_research = get_dark_horse_stocks(
                cutoff_date=cutoff, days=14,
                existing_codes=existing_codes, min_bullish=2,
            )
            # 研报风险信号: 被看空的个股
            risks = get_research_risk_signals(cutoff_date=cutoff, days=14)
            for s in risks.get("bearish_stocks", []):
                research_risk_codes.add(s.get("code", ""))
            # 将研报黑马注入候选池 (保送入海选)
            for dh in dark_horse_from_research:
                cands_sorted.append({
                    "code": dh["code"],
                    "name": dh.get("name", ""),
                    "v3": 0.0,  # 无 V3 分, 靠研报催化保送
                    "chain": 0, "delivery": 0, "capital": 0,
                    "essence": {
                        "chain_position": "研报黑马",
                        "core_catalyst": "; ".join(dh.get("reasons", [])[:2]),
                        "biggest_bull": f"研报{dh.get('bullish_count',0)}次看多",
                        "biggest_bear": "",
                        "quality_redline": "",
                        "catalyst_horizon": "short",
                    },
                    "tech_total": 0, "tech_trend": 0, "fund_5d": 0,
                    "screen_reason": "研报黑马保送",
                })
            if dark_horse_from_research:
                print(f"  🐎 研报黑马: {len(dark_horse_from_research)}只近期bullish催化但未入池")
            if research_risk_codes:
                print(f"  ⚠ 研报看空: {len(research_risk_codes)}只近期bearish")
        except Exception:
            pass  # research.db 不存在时静默跳过

        print(f"\n{'='*60}")
        print(f"🗂️  [阶段 3/7] 海选 (mode={screen_mode}, 决赛名额={debate_top_k})")
        print(f"{'='*60}")

        # ── dry-run: 直接按 V3 取前 debate_top_k ──
        if state.get("dry_run"):
            promoted = cands_sorted[:debate_top_k]
            return {"round1_promoted": [c["code"] for c in promoted],
                    "debate_ledger": new_debate_ledger(max_rounds),
                    "trace": [_trace("screen_round1", f"dry-run mode={screen_mode} 晋级{len(promoted)}")]}

        dark_horse_prompt = (
            "你是A股短期涨幅发掘官。以下候选股可能存在被遗漏的催化/资金信号。\n"
            "请从中选出【未来30天涨幅可能超预期】的股票, 要求:\n"
            "- 必须有明确的短期催化(产品验证/订单放量/政策利好)\n"
            "- 资金面有主力净流入迹象\n"
            "- 不得因'大客户集中/估值高/催化已兑现'等传统理由排除\n"
            "- 标注'研报黑马'的股票有近期博主看多催化, 请特别关注\n\n"
            "严格输出JSON数组(按涨幅潜力高→低排序):\n"
            '[{"code":"代码","rank":1,"reason":"20字内理由"},...]\n'
        )
        # 研报风险提示
        if research_risk_codes:
            dark_horse_prompt += (
                f"\n\n【研报风险提示 (近期被博主看空的个股)】\n"
                f"代码: {', '.join(sorted(research_risk_codes)[:10])}\n"
                "如果这些股出现在候选中, 请谨慎评估。"
            )

        def _screen_pool(pool: List[Dict[str, Any]], take: int) -> tuple:
            """对一个候选池分组跑 LLM 海选, 返回 (按涨幅排序的晋级股, 日志)。"""
            if take <= 0 or not pool:
                return [], []
            groups = data_io.snake_split(pool, dark_horse_groups)
            # 每组取的名额: 平均分配, 保证总数 >= take
            per_group = max(take_per_group, -(-take // max(1, len(groups))))
            picked: List[Dict[str, Any]] = []
            logs = []
            with ThreadPoolExecutor(max_workers=len(groups)) as ex:
                def judge_group(idx_group):
                    idx, group = idx_group
                    text = "\n\n".join(format_stock_brief(c) for c in group)
                    raw = llm.call(dark_horse_prompt, f"候选股({len(group)}只):\n{text}",
                                   deep=False, max_chars=2000)
                    result = extract_json_array(raw)
                    ordered = _apply_ranking(group, result)
                    return idx, ordered[:per_group], {"group": idx + 1, "raw": raw, "result": result}

                for idx, top, log in ex.map(judge_group, list(enumerate(groups))):
                    picked.extend(top)
                    logs.append(log)
                    print(f"  海选G{idx+1}({len(groups[idx])}只) → "
                          f"候选 {' '.join(c['code'] for c in top)}")
            return picked, logs

        # ══ 三种模式 ══
        if screen_mode == "llm":
            # B: 全部候选经 LLM 海选, 取 Top-debate_top_k (V3 仅作并列打破)
            picked, logs = _screen_pool(cands_sorted, debate_top_k * 2)
            # 去重并保持 LLM 涨幅排序; 不足则按 V3 补齐
            seen = set()
            finalists: List[Dict[str, Any]] = []
            for c in picked:
                if c["code"] not in seen:
                    finalists.append(c); seen.add(c["code"])
            for c in cands_sorted:
                if len(finalists) >= debate_top_k:
                    break
                if c["code"] not in seen:
                    finalists.append(c); seen.add(c["code"])
            finalists = finalists[:debate_top_k]
            screen_log = {"mode": "llm", "llm_picked": [c["code"] for c in picked],
                          "finalists": [c["code"] for c in finalists], "group_logs": logs}

        elif screen_mode == "hybrid":
            # C: V3 Top-force_k 保送 + 剩余经 LLM 海选竞争剩余名额
            auto = cands_sorted[:force_k]
            rest = cands_sorted[force_k:]
            seen = {c["code"] for c in auto}
            remaining = debate_top_k - len(auto)
            picked, logs = _screen_pool(rest, remaining * 2)
            extra: List[Dict[str, Any]] = []
            for c in picked:
                if len(extra) >= remaining:
                    break
                if c["code"] not in seen:
                    extra.append(c); seen.add(c["code"])
            # 不足则按 V3 从 rest 补齐
            for c in rest:
                if len(extra) >= remaining:
                    break
                if c["code"] not in seen:
                    extra.append(c); seen.add(c["code"])
            finalists = auto + extra
            screen_log = {"mode": "hybrid", "auto_promote": [c["code"] for c in auto],
                          "llm_picked": [c["code"] for c in extra],
                          "finalists": [c["code"] for c in finalists], "group_logs": logs}

        else:
            # A (promote, 现状): V3 Top-debate_top_k 直接进决赛
            finalists = cands_sorted[:debate_top_k]
            # 黑马发掘仅作参考记录(不占决赛名额), 保留与历史一致的可观测性
            rest = cands_sorted[v3_auto_promote:]
            picked, logs = _screen_pool(rest, dark_horse_groups * take_per_group) if rest else ([], [])
            screen_log = {"mode": "promote", "finalists": [c["code"] for c in finalists],
                          "dark_horse_ref": [c["code"] for c in picked], "group_logs": logs}

        print(f"  ✅ 决赛阵容({len(finalists)}只): {' '.join(c['code'] for c in finalists)}")
        _dump(state["run_dir"], "03_round1_screen.json", screen_log, as_json=True)
        return {"round1_promoted": [c["code"] for c in finalists],
                "debate_ledger": new_debate_ledger(max_rounds),
                "trace": [_trace("screen_round1",
                                 f"mode={screen_mode} 决赛{len(finalists)}")]}
    return node


# ══════════════════════════════════════════════════════════
# 阶段 5: 终极 PK (10→最终排名)
# ══════════════════════════════════════════════════════════

def _confidence_level(conf: float) -> str:
    if conf >= 0.7:
        return "高"
    if conf >= 0.4:
        return "中"
    return "低"


def make_final_judge(llm: LLMHelper, top_k: int = 10):
    def node(state) -> Dict[str, Any]:
        cands = {c["code"]: c for c in state.get("candidates", [])}
        promoted = state.get("round2_promoted") or state.get("round1_promoted") or []
        k = int((state.get("metadata") or {}).get("debate_top_k", top_k))
        finalists = [cands[c] for c in promoted if c in cands][:k]
        print(f"\n{'='*60}\n🏆 [阶段 5/7] 终极 PK {len(finalists)}→最终排名\n{'='*60}")

        ledger = state.get("debate_ledger") or {}
        claims = ledger.get("claims", [])

        if state.get("dry_run") or not finalists:
            ranking = [{
                "rank": i + 1, "code": c["code"], "name": c["name"],
                "score": round(c["v3"], 1), "confidence": 0.5, "confidence_level": "中",
                "key_thesis": c.get("essence", {}).get("biggest_bull", ""),
                "key_risk": c.get("essence", {}).get("biggest_bear", ""),
                "supporting_claim_ids": [], "risk_flags": [],
            } for i, c in enumerate(finalists)]
            _dump(state["run_dir"], "05_final_ranking.json", ranking, as_json=True)
            return {"final_ranking": ranking,
                    "trace": [_trace("final_judge", f"dry-run {len(ranking)}名")]}

        stock_text = "\n\n".join(format_stock_brief(c) for c in finalists)
        claim_text = "\n".join(
            f"[{cl.get('claim_id')}] {cl.get('stance')} {cl.get('code')}: "
            f"{cl.get('claim')} (conf={cl.get('confidence')}, {cl.get('status')})"
            for cl in claims
        ) or "无登记 claim"
        # 分析师报告 (提供深度数据支撑给终极评判)
        reports = state.get("analyst_reports") or {}
        report_text = ""
        for role in ("technical", "fund", "fundamental"):
            md = reports.get(role, "")
            if md:
                label = {"technical": "技术面", "fund": "资金面", "fundamental": "基本面/催化面"}[role]
                report_text += f"\n--- {label}分析师报告 ---\n{md}\n"
        # 增量信息 (实时财务+新闻+量化信号)
        briefs = state.get("incremental_briefs", {})
        incr_text = ""
        for c in finalists:
            b = briefs.get(c["code"], "")
            if b:
                incr_text += f"\n{b}\n"
        human = (f"候选股({len(finalists)}只):\n{stock_text}\n\n"
                 f"增量信息(实时财务+新闻+量化):\n{incr_text}\n\n"
                 f"分析师报告:\n{report_text}\n\n"
                 f"claim账本:\n{claim_text}")
        rot = state.get("rotation_context", "")
        if rot:
            human += f"\n\n板块资金轮动(判断主线是否在切换, 流出板块的龙头需谨慎):\n{rot}"
        raw = llm.call(FINAL_JUDGE_SYSTEM, human, deep=True, max_chars=4000)
        result = extract_json_array(raw)

        # V3 基准排名 (按 V3 分降序)
        _v3_sorted = sorted(finalists, key=lambda c: c.get("v3", 0), reverse=True)
        v3_rank = {c["code"]: i + 1 for i, c in enumerate(_v3_sorted)}

        cmap = {c["code"]: c for c in finalists}
        rows: List[dict] = []
        for r in result:
            code = str(r.get("code", "")).strip()
            if code not in cmap:
                continue
            c = cmap[code]
            conf = float(r.get("confidence", 0.5) or 0.5)
            sup = [cl.get("claim_id") for cl in claims if cl.get("code") == code]
            llm_rank = int(r.get("rank", len(rows) + 1))
            rows.append({
                "llm_rank": llm_rank, "code": code, "name": c["name"],
                "score": round(float(r.get("score", c["v3"]) or c["v3"]), 1),
                "confidence": round(conf, 2), "confidence_level": _confidence_level(conf),
                "key_thesis": r.get("key_thesis", ""), "key_risk": r.get("key_risk", ""),
                "supporting_claim_ids": sup, "risk_flags": r.get("risk_flags", []),
            })
        # LLM 遗漏的按 V3 顺序补齐
        for c in finalists:
            if c["code"] not in {row["code"] for row in rows}:
                rows.append({
                    "llm_rank": len(rows) + 1, "code": c["code"], "name": c["name"],
                    "score": round(c["v3"], 1), "confidence": 0.4, "confidence_level": "低",
                    "key_thesis": c.get("essence", {}).get("biggest_bull", ""),
                    "key_risk": c.get("essence", {}).get("biggest_bear", ""),
                    "supporting_claim_ids": [], "risk_flags": ["数据不足"],
                })

        # ── 条件性调整: 辩论只在有硬证据时才调整 V3 排名 ──
        # 规则:
        # 1. 如果某股有未解决的空头 claim 且含真风险标签 → 允许下调(最多降 max_drop 位)
        # 2. 如果某股有未解决的多头 claim 且无空头风险标签 → 允许上调(最多升 max_rise 位)
        # 3. 新晋股(量价保送, _rising_star)若有未决多头 claim → 上限放宽到 max_rise+2
        #    (新晋股V3=0排名靠后, 需要更大空间才能进入有效排名区间)
        # 4. 其余保持 V3 排名
        HARD_RISK_FLAGS = {"趋势破位", "主力持续流出", "质量红线", "催化证伪", "量价背离"}
        # 可持续性相关的风险标签 (见 SUSTAINABILITY_CRITERIA)
        SUSTAINABILITY_RISK_FLAGS = {"催化衰竭", "资金衰减", "量价背离", "透支风险", "纯题材"}
        ALL_RISK_FLAGS = HARD_RISK_FLAGS | SUSTAINABILITY_RISK_FLAGS
        max_drop = int((state.get("metadata") or {}).get("max_rank_drop", 3))
        max_rise = int((state.get("metadata") or {}).get("max_rank_rise", 3))

        # 新晋股集合 (用于放宽上调上限)
        rising_star_codes = {c["code"] for c in cands.values() if c.get("_rising_star")}

        unresolved_ids = set(ledger.get("unresolved_claim_ids", [])
                             + ledger.get("open_claim_ids", []))
        # 每只股的未决空头/多头 claim
        bear_claims = {code: [] for code in v3_rank}
        bull_claims = {code: [] for code in v3_rank}
        for cl in claims:
            cid = cl.get("claim_id", "")
            code = cl.get("code", "")
            if cid in unresolved_ids:
                if cl.get("stance") == "bearish":
                    bear_claims.setdefault(code, []).append(cl)
                elif cl.get("stance") == "bullish":
                    bull_claims.setdefault(code, []).append(cl)

        # 计算调整量
        adjustments = {}  # code → delta (负=下调, 正=上调)
        for row in rows:
            code = row["code"]
            flags = set(row.get("risk_flags", []))
            hard_flags = flags & ALL_RISK_FLAGS
            bears = bear_claims.get(code, [])
            bulls = bull_claims.get(code, [])
            has_bear = len(bears) > 0
            has_bull = len(bulls) > 0
            is_rising_star = code in rising_star_codes

            # 新晋股上调上限放宽 (V3=0 需要更大空间)
            effective_max_rise = max_rise + (2 if is_rising_star else 0)

            delta = 0
            if hard_flags and has_bear:
                # 有风险标签 + 未决空头 claim → 下调
                strong_bear = sum(1 for cl in bears if float(cl.get("confidence", 0.6) or 0.6) >= 0.7)
                delta = -min(len(hard_flags) + strong_bear, max_drop)
            elif has_bull and not has_bear:
                # 有未决多头 claim 且无空头反驳 → 上调
                # 可持续性信号: 多头 claim 提及催化余量/资金持续/业绩支撑 → 额外+1
                strong_bull = sum(1 for cl in bulls if float(cl.get("confidence", 0.6) or 0.6) >= 0.7)
                sustainability_bonus = sum(
                    1 for cl in bulls
                    if any(kw in cl.get("claim", "") for kw in ["可持续", "催化余量", "资金持续", "业绩支撑"])
                )
                delta = min(max(1, strong_bull) + sustainability_bonus, effective_max_rise)
            elif has_bear and not hard_flags:
                # 有空头 claim 但无风险标签 → 不降权
                if has_bull:
                    delta = min(1, effective_max_rise)
                else:
                    delta = 0

            adjustments[code] = delta
            if delta != 0:
                tag = f"新晋股+{delta}" if is_rising_star and delta > 0 else str(delta)
                print(f"    调整: {code} {row.get('name','')} delta={tag}")

        # 应用调整: 按 V3 排名 + delta 排序 (回测验证: V3 做主排序优于 LLM 排名)
        for row in rows:
            row["_adj_rank"] = v3_rank.get(row["code"], row["llm_rank"]) + adjustments.get(row["code"], 0)
        rows.sort(key=lambda x: (x["_adj_rank"], v3_rank.get(x["code"], 99)))

        ranking: List[dict] = []
        for i, row in enumerate(rows):
            row.pop("_adj_rank", None)
            row.pop("llm_rank", None)
            row["rank"] = i + 1
            ranking.append(row)

        _dump(state["run_dir"], "05_final_ranking.json", ranking, as_json=True)
        _dump(state["run_dir"], "_final_judge_raw.txt", raw)
        adj_summary = {code: d for code, d in adjustments.items() if d != 0}
        return {"final_ranking": ranking,
                "trace": [_trace("final_judge", f"{len(ranking)}名 调整{adj_summary}")]}
    return node
