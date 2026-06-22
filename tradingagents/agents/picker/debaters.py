"""debate_picker — 量化锚排序 + (可选)LLM辩论回测工具。

生产路径 (picker_graph):
  make_ranking_debate → _anchor_score 排序 → TOP10
  纯量化, 不调LLM (回测: 锚分Spearman=+0.555 远超LLM的-0.14)。

回测/调试工具 (scripts/test_deep_rank.py):
  _run_debate_unit / _adjudicate / _finalize_ranking
  这些函数仅供回测脚本对比"LLM辩论 vs 量化锚"的效果, 生产流程不调用。
  下一步尝试接入增量信息时可能复用。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from .judges import format_comparison_matrix, format_stock_brief
from .llm_helper import LLMHelper, extract_json_array, extract_tagged_json
from .prompts import (ADJUDICATOR_SYSTEM, BEAR_DEBATER_SYSTEM,
                      BULL_DEBATER_SYSTEM)


def _trace(node: str, note: str) -> dict:
    return {"node": node, "note": note, "ts": datetime.now().isoformat()}


def _dump(run_dir: str, name: str, content: Any, as_json: bool = False):
    path = os.path.join(run_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        if as_json:
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(content))


# ══════════════════════════════════════════════════════════
# claim 入账 (排序竞争: 解析 target_codes / verdict)
# ══════════════════════════════════════════════════════════

def _ingest_claims(ledger: Dict[str, Any], payload: Any,
                   stance: str, prefix: str) -> None:
    """把一条 LLM 机读块的 claim 变更并入账本。

    排序竞争导向: 每个 claim 可含 target_codes (比较对) 和 verdict (">="或"<"),
    论证的是 A vs B 谁涨幅更高, 而非孤立评价。
    """
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
        # 解析比较对与裁决
        target_codes = [str(t).strip() for t in (nc.get("target_codes") or []) if str(t).strip()]
        verdict = str(nc.get("verdict", "")).strip()
        if verdict not in (">=", "<"):
            verdict = ">=" if stance == "bullish" else "<"
        entry = {
            "claim_id": cid, "code": str(nc.get("code", "")).strip(),
            "speaker": "多头" if stance == "bullish" else "空头", "stance": stance,
            "claim": text,
            "evidence": [str(e).strip() for e in (nc.get("evidence") or [])[:3] if str(e).strip()],
            "confidence": round(float(nc.get("confidence", 0.6) or 0.6), 3),
            "status": "open",
            "target_claim_ids": [t for t in (nc.get("target_claim_ids") or []) if t in cmap],
            "target_codes": target_codes,
            "verdict": verdict,
            "round_index": int(ledger.get("round", 1)),
        }
        claims.append(entry); cmap[cid] = entry; open_ids.add(cid)

    ledger["claim_counter"] = counter
    ledger["resolved_claim_ids"] = sorted(resolved)
    ledger["unresolved_claim_ids"] = sorted(unresolved)
    ledger["open_claim_ids"] = sorted(open_ids)


# ══════════════════════════════════════════════════════════
# 统一辩论单元: 一轮 多空 claim 攻防 → 裁决收窄
# ══════════════════════════════════════════════════════════

def _format_claim_brief(ledger: Dict[str, Any], stance_filter: Optional[str] = None) -> str:
    """格式化已有 claim 摘要供本轮参考。"""
    lines = []
    for c in ledger.get("claims", []):
        if stance_filter and c.get("stance") != stance_filter:
            continue
        tc = c.get("target_codes", [])
        tc_str = f" vs {','.join(tc[1:])}" if len(tc) > 1 else ""
        verdict = c.get("verdict", "")
        lines.append(
            f"[{c['claim_id']}] {c['stance']} {c['code']}{tc_str}({verdict}): "
            f"{c['claim']} (conf={c.get('confidence')}, {c.get('status')})"
        )
    return "\n".join(lines) or "暂无 claim"


def _build_context(finalists: List[Dict[str, Any]], state: Dict[str, Any],
                   rnd: int, total_rounds: int, ledger: Dict[str, Any],
                   first_round_full: bool = True) -> Dict[str, str]:
    """构建本轮辩论所需的上下文文本 (stock_text / claim_brief / 报告 / 增量信息 / 研报)。"""
    stock_text = "\n\n".join(format_stock_brief(c) for c in finalists)
    # 横向对比矩阵 (仅首轮注入, 帮助 LLM 建立相对排序认知)
    if rnd == 1:
        stock_text += "\n\n" + format_comparison_matrix(finalists)

    claim_brief = _format_claim_brief(ledger)

    # 分析师报告
    reports = state.get("analyst_reports") or {}
    report_text = ""
    for role in ("technical", "fund", "fundamental"):
        md = reports.get(role, "")
        if md:
            label = {"technical": "技术面", "fund": "资金面", "fundamental": "基本面/催化面"}[role]
            report_text += f"\n--- {label}分析师报告 ---\n{md}\n"

    # 增量信息 (首轮全量灌入; 后续轮次聚焦 claim 攻防, 省略冗长明细提速)
    briefs = state.get("incremental_briefs", {})
    incr_text = ""
    if rnd == 1 or first_round_full:
        for c in finalists:
            b = briefs.get(c["code"], "")
            if b:
                incr_text += f"\n{b}\n"
    else:
        incr_text = "(增量信息已在首轮提供, 本轮聚焦对已有 claim 的证据攻防)"

    # 研报行业动量 (首轮注入)
    research_text = ""
    if rnd == 1:
        rctx = state.get("research_context", "")
        if rctx:
            research_text = f"\n\n--- 研报行业动量与市场情绪 ---\n{rctx}"

    return {
        "stock_text": stock_text, "claim_brief": claim_brief,
        "report_text": report_text, "incr_text": incr_text,
        "research_text": research_text,
    }


def _run_debate_unit(
    llm: LLMHelper,
    finalists: List[Dict[str, Any]],
    state: Dict[str, Any],
    ledger: Dict[str, Any],
    rnd: int,
    total_rounds: int,
    deep: bool = True,
) -> Dict[str, Any]:
    """执行一轮辩论: 多头建 claim → 空头反驳 → 多头再反驳。

    返回 {bull_raw, bear_raw, bull_rebuttal_raw} 供落盘/裁决。
    ledger 原地更新 (claim 累积)。海选与排名辩论复用本函数。
    """
    ctx = _build_context(finalists, state, rnd, total_rounds, ledger,
                         first_round_full=True)
    round_goal = f"第{rnd}/{total_rounds}轮: "
    if rnd == 1:
        round_goal += ("建立核心比较型 claim。每个 claim 必须指向一个比较对(target_codes), "
                       "论证 A vs B 谁涨幅更高, 并引用带日期/数值的证据。")
    elif rnd < total_rounds:
        round_goal += ("针对对手 claim 的证据反驳: 证据是否过时/数据夸大/量价背离。"
                       "用 target_claim_ids 精确指向被攻击的 claim。")
    else:
        round_goal += ("最终轮: 综合全部 claim 攻防, 论证谁的涨幅逻辑最扎实应排最前。")
    ledger["round_goal"] = round_goal

    # ── 多头 ──
    bull_human = (f"本轮目标: {round_goal}\n候选股:\n{ctx['stock_text']}\n\n"
                  f"增量信息(实时财务+新闻+量化):\n{ctx['incr_text']}\n\n"
                  f"分析师报告:\n{ctx['report_text']}\n\n"
                  f"已有claim:\n{ctx['claim_brief']}{ctx['research_text']}")
    bull_raw = llm.call(BULL_DEBATER_SYSTEM, bull_human, deep=deep, max_chars=4000)
    _ingest_claims(ledger, extract_tagged_json(bull_raw, "DEBATE_STATE"), "bullish", "BULL")

    # ── 空头 (看到多头最新 claim) ──
    claim_brief2 = _format_claim_brief(ledger)
    bear_human = (f"本轮目标: {round_goal}\n候选股:\n{ctx['stock_text']}\n\n"
                  f"增量信息:\n{ctx['incr_text']}\n\n"
                  f"分析师报告:\n{ctx['report_text']}\n\n"
                  f"多头已提出的claim:\n{claim_brief2}\n\n请针对性反驳(论证谁的涨幅更少)。{ctx['research_text']}")
    bear_raw = llm.call(BEAR_DEBATER_SYSTEM, bear_human, deep=deep, max_chars=4000)
    _ingest_claims(ledger, extract_tagged_json(bear_raw, "DEBATE_STATE"), "bearish", "BEAR")

    # ── 多头反驳 (看到空头攻击后回应) ──
    bear_claims_brief = _format_claim_brief(ledger, stance_filter="bearish")
    bull_rebuttal_human = (f"空头已提出以下攻击:\n{bear_claims_brief}\n\n"
                           f"请作为多头回应: 哪些空头攻击你不同意(标记unresolved)? "
                           f"哪些你认可(标记resolved)? 最多反驳3条最关键的。")
    bull_rebuttal_raw = llm.call(BULL_DEBATER_SYSTEM, bull_rebuttal_human,
                                 deep=False, max_chars=2000)
    _ingest_claims(ledger, extract_tagged_json(bull_rebuttal_raw, "DEBATE_STATE"),
                   "bullish", "BULL")

    return {"bull_raw": bull_raw, "bear_raw": bear_raw,
            "bull_rebuttal_raw": bull_rebuttal_raw}


def _adjudicate(
    llm: LLMHelper,
    finalists: List[Dict[str, Any]],
    state: Dict[str, Any],
    ledger: Dict[str, Any],
    produce_rank: bool = False,
    deep: bool = True,
):
    """[已废弃] LLM裁决排序。回测证明LLM从头排序Spearman为负(-0.14), 破坏量化信号。
    海选和排名辩论已改为量化锚(_anchor_score)排序, 本函数仅保留供参考/回测对比。
    """
    ctx = _build_context(finalists, state, 1, 1, ledger, first_round_full=False)
    claim_text = _format_claim_brief(ledger)

    human = (f"本轮辩论 claim 账本:\n{claim_text}\n\n"
             f"候选股:\n{ctx['stock_text']}\n\n"
             f"增量信息:\n{ctx['incr_text']}\n\n"
             f"分析师报告:\n{ctx['report_text']}\n\n"
             f"请按【预期30天涨幅从高到低】排序全部 {len(finalists)} 只候选股。")
    raw = llm.call(ADJUDICATOR_SYSTEM, human, deep=deep, max_chars=4000)
    result = extract_json_array(raw)

    cmap = {c["code"]: c for c in finalists}
    seen: set = set()
    ordered: List[Any] = []
    for r in result:
        code = str(r.get("code", "")).strip()
        if code not in cmap or code in seen:
            continue
        seen.add(code)
        c = cmap[code]
        if produce_rank:
            ordered.append({
                "code": code, "name": c["name"],
                "score": round(float(r.get("score", c.get("v3", 0)) or c.get("v3", 0)), 1),
                "key_thesis": r.get("key_thesis", ""),
                "key_risk": r.get("key_risk", ""),
                "_rank_hint": int(r.get("rank", len(ordered) + 1)),
            })
        else:
            ordered.append(code)

    # LLM 遗漏的按 v3 顺序补齐 (保持裁决完整性)
    for c in finalists:
        if c["code"] in seen:
            continue
        seen.add(c["code"])
        if produce_rank:
            ordered.append({"code": c["code"], "name": c["name"],
                            "score": round(c.get("v3", 0), 1),
                            "key_thesis": c.get("essence", {}).get("biggest_bull", ""),
                            "key_risk": c.get("essence", {}).get("biggest_bear", ""),
                            "_rank_hint": len(ordered) + 1})
        else:
            ordered.append(c["code"])

    return ordered


# ══════════════════════════════════════════════════════════
# 节点: 排名辩论 (30→10, 多轮收窄, 最后一轮产出最终排名)
# ══════════════════════════════════════════════════════════

def make_ranking_debate(llm: LLMHelper, max_rounds: int = 3, final_top_k: int = 10):
    """排名节点: 候选池 → 量化锚排序 → TOP_k 最终排名。

    纯量化, 不调LLM (回测验证: 量化锚Spearman=+0.555 远超LLM从头排序的-0.14)。
    按 anchor_score 降序直接出排名, 多空论点从 essence 提取(供报告展示)。
    """
    def node(state) -> Dict[str, Any]:
        from .picker_state import new_debate_ledger

        cands = {c["code"]: c for c in state.get("candidates", [])}
        # screen_promoted 来自已废弃的 make_screen_debate (海选), 当前基线永远为空,
        # 故走 else 分支用全池 candidates。保留读取仅为前向兼容, 不影响逻辑。
        promoted = state.get("screen_promoted") or []
        meta = state.get("metadata") or {}
        top_k = int(meta.get("debate_top_k", final_top_k))

        # 基线模式(无海选): 直接用全部candidates; 有海选结果时用海选晋级
        if promoted:
            finalists = [cands[c] for c in promoted if c in cands]
        else:
            finalists = list(cands.values())
        n = len(finalists)

        print(f"\n{'='*60}")
        print(f"📊 [阶段 2/4] 量化排名 ({n}只 → TOP{top_k}, 锚=chain+capital×2-delivery×0.5)")
        print(f"{'='*60}")

        ledger = new_debate_ledger(1)
        if not finalists:
            _dump(state["run_dir"], "05_final_ranking.json", [], as_json=True)
            return {"final_ranking": [], "debate_ledger": ledger,
                    "trace": [_trace("ranking", "无候选")]}

        # 量化锚排序 + essence论点提取 (不调LLM)
        from .judges import _confidence_level
        ordered = sorted(finalists, key=lambda x: -_anchor_score(x))[:top_k]
        ranking = []
        for i, c in enumerate(ordered):
            e = c.get("essence", {})
            ranking.append({
                "rank": i + 1,
                "code": c["code"], "name": c["name"],
                "score": round(_anchor_score(c), 1),
                "confidence": 0.7,
                "confidence_level": _confidence_level(0.7),
                "key_thesis": e.get("biggest_bull", ""),
                "key_risk": e.get("biggest_bear", ""),
                "supporting_claim_ids": [],
                "risk_flags": [],
            })

        _dump(state["run_dir"], "05_final_ranking.json", ranking, as_json=True)
        print(f"  ✅ 量化排名完成: TOP{len(ranking)}")
        for r in ranking[:5]:
            print(f"    #{r['rank']} {r['code']} {r['name']} 锚={r['score']}")
        return {"final_ranking": ranking, "debate_ledger": ledger,
                "trace": [_trace("ranking", f"量化锚TOP{len(ranking)}")]}
    return node


def _anchor_score(c: Dict[str, Any]) -> float:
    """量化排序锚 (薄封装, 真相源在 data_io.anchor_score)。

    保留本函数: make_ranking_debate 内部调用, 以及 scripts/test_deep_rank.py 的导入兼容。
    公式变更请改 data_io.anchor_score 并回测验证。
    """
    from .data_io import anchor_score
    return anchor_score(c)


def _finalize_ranking(ranked_items: List[Dict[str, Any]],
                      ledger: Dict[str, Any],
                      state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把裁决排名项转为最终 RankItem, 应用 claim 驱动的硬风险下调。

    复用旧 final_judge 的风险下调逻辑: 高置信未决空头 claim + 硬风险标签 → 下调。
    """
    from .judges import _confidence_level

    claims = ledger.get("claims", [])
    unresolved_ids = set(ledger.get("unresolved_claim_ids", [])
                         + ledger.get("open_claim_ids", []))
    bear_claims: Dict[str, List] = {}
    for cl in claims:
        cid = cl.get("claim_id", "")
        code = cl.get("code", "")
        if cid in unresolved_ids and cl.get("stance") == "bearish":
            bear_claims.setdefault(code, []).append(cl)

    HARD_RISK_FLAGS = {"趋势破位", "主力持续流出", "质量红线", "催化证伪", "量价背离",
                       "催化衰竭", "资金衰减", "透支风险", "题材无支撑"}
    max_drop = int((state.get("metadata") or {}).get("max_rank_drop", 3))

    rows: List[dict] = []
    for r in ranked_items:
        code = r["code"]
        bears = bear_claims.get(code, [])
        strong_bears = [cl for cl in bears if float(cl.get("confidence", 0.6) or 0.6) >= 0.7]
        sup = [cl.get("claim_id") for cl in claims
               if cl.get("code") == code and cl.get("stance") == "bullish"]
        risk_flags = []
        if strong_bears:
            bear_text = " ".join(cl.get("claim", "") + " " + " ".join(cl.get("evidence", []))
                                 for cl in strong_bears)
            for flag in HARD_RISK_FLAGS:
                if flag[:2] in bear_text:
                    risk_flags.append(flag)
        conf = 0.7 if not strong_bears else max(0.4, 0.7 - 0.1 * len(strong_bears))
        rows.append({
            "code": code, "name": r.get("name", ""),
            "score": r.get("score", 0),
            "confidence": round(conf, 2),
            "confidence_level": _confidence_level(conf),
            "key_thesis": r.get("key_thesis", ""),
            "key_risk": r.get("key_risk", ""),
            "supporting_claim_ids": sup,
            "risk_flags": risk_flags,
            "_rank_hint": r.get("_rank_hint", 99),
            "_delta": -min(len(risk_flags) + len(strong_bears), max_drop) if risk_flags else 0,
        })

    for row in rows:
        row["_final_rank"] = row["_rank_hint"] + row["_delta"]
    rows.sort(key=lambda x: (x["_final_rank"], x["_rank_hint"]))

    ranking: List[dict] = []
    for i, row in enumerate(rows):
        for k in ("_rank_hint", "_delta", "_final_rank"):
            row.pop(k, None)
        row["rank"] = i + 1
        ranking.append(row)
    return ranking
