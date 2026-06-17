"""debate_picker v5 — claim 驱动交叉辩论节点 (M3)。

每次进入 debate_round 执行"一轮"辩论: 多头建 claim → 空头反驳。
claim 账本随轮次累积更新, 由 conditional_edge 判断收敛。
首轮进入时从海选 20 只里按 V3 收窄到 top_k(=10) 作为辩论标的。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from .judges import format_stock_brief
from .llm_helper import LLMHelper, extract_tagged_json
from .prompts import BEAR_DEBATER_SYSTEM, BULL_DEBATER_SYSTEM


def _trace(node: str, note: str) -> dict:
    return {"node": node, "note": note, "ts": datetime.now().isoformat()}


def _dump(run_dir: str, name: str, content: Any, as_json: bool = False):
    path = os.path.join(run_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        if as_json:
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(content))


ROUND_GOALS = [
    "基于增量信息(新闻催化/财务增速/资金流明细/K线走势)建立最核心的多空 claim。"
    "每个 claim 必须引用至少一条带日期或数值的具体证据, 说明为何是现在、相对其他候选谁涨更多。",
    "针对对手 claim 的【证据本身】反驳: 证据是否过时、数据是否夸大、量价是否背离。"
    "用 target_claim_ids 精确指向被攻击的 claim, 不要扩散到新议题。",
    "围绕30天时间窗口与失败路径, 结合资金流趋势与新高位置, 判断谁的涨幅逻辑最扎实、排序应最高。",
]


def _ingest_claims(ledger: Dict[str, Any], payload: Dict[str, Any],
                   stance: str, prefix: str) -> None:
    """把一条 LLM 机读块的 claim 变更并入账本 (借鉴 debate_utils 思路, 精简版)。"""
    # 防御: LLM 可能输出 list 而非 dict
    if isinstance(payload, list):
        payload = {"new_claims": payload} if payload else {}
    if not isinstance(payload, dict):
        return
    claims: List[dict] = ledger.setdefault("claims", [])
    cmap = {c["claim_id"]: c for c in claims}
    counter = int(ledger.get("claim_counter", 0))

    resolved = set(ledger.get("resolved_claim_ids", []))
    unresolved = set(ledger.get("unresolved_claim_ids", []))
    open_ids = set(ledger.get("open_claim_ids", []))

    for cid in payload.get("resolved_claim_ids", []) or []:
        if cid in cmap:
            cmap[cid]["status"] = "resolved"
        resolved.add(cid); unresolved.discard(cid); open_ids.discard(cid)
    for cid in payload.get("unresolved_claim_ids", []) or []:
        if cid in cmap:
            cmap[cid]["status"] = "unresolved"
        unresolved.add(cid); resolved.discard(cid); open_ids.add(cid)

    for nc in payload.get("new_claims", []) or []:
        text = str(nc.get("claim", "")).strip()
        if not text:
            continue
        counter += 1
        cid = f"{prefix}-{counter}"
        entry = {
            "claim_id": cid, "code": str(nc.get("code", "")).strip(),
            "speaker": "多头" if stance == "bullish" else "空头", "stance": stance,
            "claim": text,
            "evidence": [str(e).strip() for e in (nc.get("evidence") or [])[:3] if str(e).strip()],
            "confidence": round(float(nc.get("confidence", 0.6) or 0.6), 3),
            "status": "open",
            "target_claim_ids": [t for t in (nc.get("target_claim_ids") or []) if t in cmap],
            "round_index": int(ledger.get("round", 1)),
        }
        claims.append(entry); cmap[cid] = entry; open_ids.add(cid)

    ledger["claim_counter"] = counter
    ledger["resolved_claim_ids"] = sorted(resolved)
    ledger["unresolved_claim_ids"] = sorted(unresolved)
    ledger["open_claim_ids"] = sorted(open_ids)


def make_debate_round(llm: LLMHelper, top_k: int = 10):
    def node(state) -> Dict[str, Any]:
        from .picker_state import new_debate_ledger
        ledger = dict(state.get("debate_ledger") or new_debate_ledger(3))
        rnd = int(ledger.get("round", 1))
        max_rounds = int(ledger.get("max_rounds", 3))
        print(f"\n{'='*60}\n💬 [阶段 4/7] claim 驱动交叉辩论 — 第 {rnd}/{max_rounds} 轮\n{'='*60}")

        cands = {c["code"]: c for c in state.get("candidates", [])}
        promoted = state.get("round1_promoted") or []
        k = int((state.get("metadata") or {}).get("debate_top_k", top_k))
        finalists = [cands[c] for c in promoted if c in cands][:k]

        out: Dict[str, Any] = {}
        # 首轮收窄 round2_promoted = 辩论标的
        if rnd == 1:
            out["round2_promoted"] = [c["code"] for c in finalists]

        if state.get("dry_run") or not finalists:
            ledger["round"] = rnd + 1
            ledger["finished"] = ledger["round"] > max_rounds
            out["debate_ledger"] = ledger
            out["trace"] = [_trace("debate_round", f"dry-run round={rnd}")]
            return out

        ledger["round_goal"] = ROUND_GOALS[min(rnd - 1, len(ROUND_GOALS) - 1)]
        stock_text = "\n\n".join(format_stock_brief(c) for c in finalists)

        # 已有 claim 摘要 (供本轮参考)
        claim_brief = "\n".join(
            f"[{c['claim_id']}] {c['stance']} {c['code']}: {c['claim']} ({c['status']})"
            for c in ledger.get("claims", [])
        ) or "暂无 claim"

        # 分析师报告 (技术面+资金面+基本面/催化面, 提供深度数据支撑)
        reports = state.get("analyst_reports") or {}
        report_text = ""
        for role in ("technical", "fund", "fundamental"):
            md = reports.get(role, "")
            if md:
                label = {"technical": "技术面", "fund": "资金面", "fundamental": "基本面/催化面"}[role]
                report_text += f"\n--- {label}分析师报告 ---\n{md}\n"

        # 增量信息 (实时财务+新闻+量化信号, V3没有的新信息)
        # 时效优化: 仅首轮全量灌入(供建立 claim); 后续轮次聚焦 claim 攻防, 省略冗长明细以提速。
        briefs = state.get("incremental_briefs", {})
        incr_text = ""
        if rnd == 1:
            for c in finalists:
                b = briefs.get(c["code"], "")
                if b:
                    incr_text += f"\n{b}\n"
        else:
            incr_text = "(增量信息已在首轮提供, 本轮聚焦对已有 claim 的证据攻防)"

        # 研报行业动量 + 市场情绪 (外部市场视角, 首轮注入)
        research_text = ""
        if rnd == 1:
            rctx = state.get("research_context", "")
            if rctx:
                research_text = f"\n\n--- 研报行业动量与市场情绪 ---\n{rctx}"

        # ── 多头 ──
        bull_human = (f"本轮目标: {ledger['round_goal']}\n候选股:\n{stock_text}\n\n"
                      f"增量信息(实时财务+新闻+量化):\n{incr_text}\n\n"
                      f"分析师报告:\n{report_text}\n\n"
                      f"已有claim:\n{claim_brief}{research_text}")
        bull_raw = llm.call(BULL_DEBATER_SYSTEM, bull_human, deep=True, max_chars=4000)
        _ingest_claims(ledger, extract_tagged_json(bull_raw, "DEBATE_STATE"), "bullish", "BULL")

        # ── 空头 (看到多头最新 claim) ──
        claim_brief2 = "\n".join(
            f"[{c['claim_id']}] {c['stance']} {c['code']}: {c['claim']} ({c['status']})"
            for c in ledger.get("claims", [])
        )
        bear_human = (f"本轮目标: {ledger['round_goal']}\n候选股:\n{stock_text}\n\n"
                      f"增量信息(实时财务+新闻+量化):\n{incr_text}\n\n"
                      f"分析师报告:\n{report_text}\n\n"
                      f"多头已提出的claim:\n{claim_brief2}\n\n请针对性反驳。{research_text}")
        bear_raw = llm.call(BEAR_DEBATER_SYSTEM, bear_human, deep=True, max_chars=4000)
        _ingest_claims(ledger, extract_tagged_json(bear_raw, "DEBATE_STATE"), "bearish", "BEAR")

        n_claims = len(ledger.get("claims", []))
        n_open = len(ledger.get("open_claim_ids", []))
        print(f"  claim 总数 {n_claims} | 未决 {n_open} | "
              f"已解决 {len(ledger.get('resolved_claim_ids', []))}")

        _dump(state["run_dir"], f"04_debate_round{rnd}.json",
              {"round": rnd, "goal": ledger["round_goal"],
               "bull_raw": bull_raw, "bear_raw": bear_raw,
               "claims_snapshot": ledger.get("claims", [])}, as_json=True)

        ledger["round"] = rnd + 1
        # 收敛: 达到轮次上限, 或本轮后无未决 claim 且已过首轮(无信息增量)
        ledger["finished"] = (ledger["round"] > max_rounds) or (rnd >= 2 and n_open == 0)
        out["debate_ledger"] = ledger
        out["trace"] = [_trace("debate_round", f"round={rnd} claims={n_claims} open={n_open}")]
        return out
    return node
