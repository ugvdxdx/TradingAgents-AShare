"""debate_picker — 量化锚排序 + (可选)LLM辩论回测工具。

生产路径 (picker_graph):
  make_ranking_debate → _anchor_score 排序 → TOP10
  纯量化, 不调LLM (回测: 锚分Spearman=+0.555 远超LLM的-0.14)。

回测/调试工具 (scripts/test_deep_rank.py):
  _run_debate_unit / _adjudicate / _finalize_ranking / _quantum_rank
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
# 节点: 海选辩论 (蛇形分3组 × 每组多轮 → 合并30只)
# ══════════════════════════════════════════════════════════

def make_screen_debate(llm: LLMHelper, n_groups: int = 3,
                       rounds_per_group: int = 2, top_per_group: int = 10):
    """海选节点: 候选池按量化锚(chain+capital×2-delivery×0.5)收窄。

    纯量化, 不调LLM (回测验证: 量化锚Spearman=+0.555, LLM从头排序=负相关)。
    蛇形分组后每组按锚分取前top_per_group晋级。
    """
    def node(state) -> Dict[str, Any]:
        from . import data_io

        cands = state.get("candidates", [])
        meta = state.get("metadata") or {}
        groups_n = int(meta.get("screen_groups", n_groups))
        top_pg = int(meta.get("screen_top_per_group", top_per_group))

        print(f"\n{'='*60}")
        print(f"🗂️  [阶段] 海选 (量化锚排序, 蛇形分{groups_n}组 → 各取{top_pg}只)")
        print(f"{'='*60}")

        groups = data_io.snake_split(cands, groups_n)
        promoted = []
        for i, g in enumerate(groups):
            top = sorted(g, key=lambda x: -_anchor_score(x))[:top_pg]
            top_codes = [c["code"] for c in top]
            promoted.extend(top_codes)
            print(f"  G{i+1}({len(g)}只) → 锚选{len(top)}只: {' '.join(top_codes)}")
            _dump(state["run_dir"], f"04_screen_g{i+1}.json",
                  {"group_size": len(g), "promoted": top_codes,
                   "top_anchors": [{"code": c["code"], "anchor": round(_anchor_score(c), 1)}
                                   for c in top]}, as_json=True)

        print(f"  ✅ 海选完成: {len(promoted)}只晋级")
        _dump(state["run_dir"], "03_screen_result.json",
              {"mode": "quantum_anchor", "groups": groups_n, "promoted": promoted}, as_json=True)
        return {"screen_promoted": promoted,
                "trace": [_trace("screen", f"量化锚晋级{len(promoted)}只")]}
    return node


# ══════════════════════════════════════════════════════════
# 节点: 排名辩论 (30→10, 多轮收窄, 最后一轮产出最终排名)
# ══════════════════════════════════════════════════════════

def make_ranking_debate(llm: LLMHelper, max_rounds: int = 3, final_top_k: int = 10):
    """排名节点: 海选晋级股 → 量化锚排序 → TOP10 最终排名。

    纯量化, 不调LLM (回测验证: 量化锚Spearman=+0.555 远超LLM从头排序的-0.14)。
    按anchor_score降序直接出排名, 多空论点从essence提取(供报告展示)。
    """
    def node(state) -> Dict[str, Any]:
        from .picker_state import new_debate_ledger

        cands = {c["code"]: c for c in state.get("candidates", [])}
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
        print(f"📊 [阶段] 量化排名 ({n}只 → TOP{top_k}, 锚=chain+capital×2-delivery×0.5)")
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
    """量化排序锚: chain + capital×2 - delivery×0.5。

    回测验证(21期×530只×30日窗口): Spearman=+0.555, 20/20期正相关, 最低+0.34。
    - chain(产业链卡位) + capital(资金热度)×2: 主信号(+0.54)
    - delivery(业绩兑现)×(-0.5): 轻微惩罚, 业绩好但卡位差的股涨幅弹性低(+0.014提升)
    """
    return (c.get("chain", 0) + c.get("capital", 0) * 2
            - c.get("delivery", 0) * 0.5)


def _quantum_rank(finalists: List[Dict[str, Any]], ledger: Dict[str, Any],
                  state: Dict[str, Any], top_k: int = 10) -> List[Dict[str, Any]]:
    """量化锚排序: 按anchor_score降序出排名, 不依赖LLM裁决。

    LLM辩论的claim仅用于:
      1. 生成key_thesis/key_risk(报告展示)
      2. 风险下调: 被高置信空头攻击且未反驳的 → 下调N位
    """
    from .judges import _confidence_level

    # 1. 量化锚排序
    base = sorted(finalists, key=lambda x: -_anchor_score(x))
    base_rank = {c["code"]: i + 1 for i, c in enumerate(base)}

    # 2. LLM风险下调 (空头claim, 不依赖resolved状态)
    claims = ledger.get("claims", [])
    bear_claims = {}
    for cl in claims:
        code = cl.get("code", "")
        if cl.get("stance") == "bearish":
            bear_claims.setdefault(code, []).append(
                (cl.get("claim_id", ""), float(cl.get("confidence", 0.6) or 0.6)))
    # 被多头明确反驳(target_claim_ids)的空头不计
    bull_rebutted = set()
    for cl in claims:
        if cl.get("stance") == "bullish":
            for tid in cl.get("target_claim_ids", []):
                bull_rebutted.add(tid)

    code_scores = {}
    for code, bears in bear_claims.items():
        score = 0
        for cid, conf in bears:
            if cid not in bull_rebutted:
                score -= conf
        code_scores[code] = score

    max_drop = int((state.get("metadata") or {}).get("max_rank_drop", 4))
    cmap = {c["code"]: c for c in finalists}
    adjusted = []
    for c in base:
        code = c["code"]
        delta = 0
        score = code_scores.get(code, 0)
        if score < -0.3:
            delta = -min(int(abs(score) / 0.3) + 1, max_drop)
        adjusted.append({
            "code": code, "name": c["name"],
            "anchor": round(_anchor_score(c), 1),
            "v3": c.get("v3", 0),
            "_base_rank": base_rank[code],
            "_delta": delta,
            "confidence": 0.7 if delta == 0 else max(0.4, 0.7 + score * 0.1),
        })

    adjusted.sort(key=lambda x: (x["_base_rank"] + x["_delta"], x["_base_rank"]))

    # 3. 生成最终排名 (附LLM论点)
    claims_by_code = {}
    for cl in claims:
        claims_by_code.setdefault(cl.get("code", ""), []).append(cl)
    ranking = []
    for i, a in enumerate(adjusted[:top_k]):
        c = cmap.get(a["code"], {})
        # 从claim里提取多空论点
        bull_claims = [cl for cl in claims_by_code.get(a["code"], [])
                       if cl.get("stance") == "bullish"]
        bear_claims_c = [cl for cl in claims_by_code.get(a["code"], [])
                         if cl.get("stance") == "bearish"]
        thesis = bull_claims[0].get("claim", "") if bull_claims else c.get("essence", {}).get("biggest_bull", "")
        risk = bear_claims_c[0].get("claim", "") if bear_claims_c else c.get("essence", {}).get("biggest_bear", "")
        risk_flags = []
        if a["_delta"] < 0:
            risk_flags.append(f"LLM风险下调{abs(a['_delta'])}位")
        ranking.append({
            "rank": i + 1,
            "code": a["code"], "name": a["name"],
            "score": a["anchor"],
            "confidence": round(a["confidence"], 2),
            "confidence_level": _confidence_level(a["confidence"]),
            "key_thesis": thesis,
            "key_risk": risk,
            "supporting_claim_ids": [cl.get("claim_id") for cl in bull_claims[:5]],
            "risk_flags": risk_flags,
        })
    return ranking


def _compute_funnel(n: int, top_k: int, rounds: int) -> List[int]:
    """计算收窄漏斗: 从 n 经 rounds 轮平滑收窄到 top_k。

    例: n=30, top_k=10, rounds=3 → [20, 14, 10]
    保证严格递减, 末项 = top_k。
    """
    if n <= top_k or rounds <= 0:
        return [top_k]
    if rounds == 1:
        return [top_k]
    step = (n - top_k) / rounds
    raw = [round(n - step * (i + 1)) for i in range(rounds)]
    raw[-1] = top_k
    out: List[int] = []
    prev = n
    for v in raw:
        v = max(top_k, v)
        if v < prev:
            out.append(v)
            prev = v
    if not out or out[-1] != top_k:
        out.append(top_k)
    return out


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
