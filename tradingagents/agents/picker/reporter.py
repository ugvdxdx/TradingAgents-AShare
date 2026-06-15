"""debate_picker v5 — 风控复核 + 终端富文本报告 (M4)。

风控复核以规则计算为主 (可信度/风险), 不再额外调 LLM。
终端报告沿用项目现有 print + 分隔线 + emoji 风格, 面向量化小白:
结论榜 + 逐股解读 + 术语解释 + 辩论回放 + 风险提示。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List


def _trace(node: str, note: str) -> dict:
    return {"node": node, "note": note, "ts": datetime.now().isoformat()}


def _dump(run_dir: str, name: str, content: Any, as_json: bool = False):
    path = os.path.join(run_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        if as_json:
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(content))


# 术语解释 (面向量化小白)
GLOSSARY = {
    "催化时效(horizon)": "near=30天内有明确催化事件; mid=1-3月; far=更久。越近弹性越大。",
    "卡位(chain_position)": "公司在产业链中的位置, 越核心越稀缺, 涨幅弹性越大。",
    "主力净流入": "近5日主力资金净买入额(亿元), >0 表示主力资金看好。",
    "技术趋势分": "0-35分, 衡量均线/价格的多头排列强度, >20 为强势。",
    "置信度": "评判官对该排名可靠程度的自评(高/中/低), 综合证据强度与多空分歧。",
    "风险标签": "趋势破位/主力持续流出/质量红线/催化证伪/题材无支撑/数据不足等需警惕的信号。",
}


# ══════════════════════════════════════════════════════════
# 阶段 6: 风控复核 (可信度评估 + 风险提示)
# ══════════════════════════════════════════════════════════

def make_risk_review():
    def node(state) -> Dict[str, Any]:
        print(f"\n{'='*60}\n⚖️  [阶段 6/7] 可信度评估 + 风险提示\n{'='*60}")
        ranking = state.get("final_ranking", [])
        cands = {c["code"]: c for c in state.get("candidates", [])}

        # 数据完整度
        all_cands = state.get("candidates", [])
        n_ok = sum(1 for c in all_cands if c.get("data_quality") == "ok")
        data_integrity = round(n_ok / len(all_cands), 2) if all_cands else 0.0

        # 整体可信度 = 排名置信度均值 × 数据完整度
        confs = [r.get("confidence", 0.5) for r in ranking]
        avg_conf = round(sum(confs) / len(confs), 2) if confs else 0.0
        overall = round(avg_conf * (0.7 + 0.3 * data_integrity), 2)

        # 汇总风险标签
        flag_counts: Dict[str, int] = {}
        for r in ranking:
            for f in r.get("risk_flags", []):
                flag_counts[f] = flag_counts.get(f, 0) + 1

        led = state.get("debate_ledger", {})
        review = {
            "overall_confidence": overall,
            "overall_level": "高" if overall >= 0.6 else ("中" if overall >= 0.4 else "低"),
            "data_integrity": data_integrity,
            "avg_rank_confidence": avg_conf,
            "debate_rounds": int(led.get("round", 1)) - 1,
            "total_claims": len(led.get("claims", [])),
            "unresolved_claims": len(led.get("unresolved_claim_ids", [])),
            "risk_flag_summary": flag_counts,
            "disclaimer": "本结果由多智能体辩论自动生成, 仅供研究参考, 不构成投资建议。市场有风险, 决策需谨慎。",
        }
        _dump(state["run_dir"], "06_risk_review.json", review, as_json=True)
        print(f"  整体可信度: {review['overall_level']} ({overall}) | "
              f"数据完整度 {data_integrity} | 风险标签 {dict(flag_counts)}")
        return {"risk_review": review, "trace": [_trace("risk_review", f"conf={overall}")]}
    return node


# ══════════════════════════════════════════════════════════
# 阶段 7: 终端富文本报告 + 归档
# ══════════════════════════════════════════════════════════

def make_report_render():
    def node(state) -> Dict[str, Any]:
        print(f"\n{'='*60}\n📊 [阶段 7/7] 最终报告\n{'='*60}")
        report = _render_report(state)
        print(report)
        _dump(state["run_dir"], "report.md", report)

        # 对外主结果 (兼容 v4 .debate_result.json 的消费方)
        result = {
            "trade_date": state.get("trade_date"),
            "cutoff_date": state.get("cutoff_date"),
            "generated_at": datetime.now().isoformat(),
            "ranking": state.get("final_ranking", []),
            "risk_review": state.get("risk_review", {}),
            "run_dir": state.get("run_dir"),
        }
        _dump(state["run_dir"], "result.json", result, as_json=True)
        # 根目录兼容文件
        try:
            with open(".debate_result.json", "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        print(f"\n  📍 归档目录: {os.path.abspath(state['run_dir'])}")
        return {"trace": [_trace("report_render", "报告完成")]}
    return node


def _render_report(state) -> str:
    ranking = state.get("final_ranking", [])
    review = state.get("risk_review", {})
    led = state.get("debate_ledger", {})
    cands = {c["code"]: c for c in state.get("candidates", [])}
    date = state.get("trade_date", "")
    lines: List[str] = []

    lines.append("\n" + "═" * 70)
    lines.append(f"  📈 30天涨幅竞争辩论榜 — {date}")
    lines.append(f"  整体可信度: {review.get('overall_level', '-')} "
                 f"({review.get('overall_confidence', '-')}) | "
                 f"辩论 {review.get('debate_rounds', 0)} 轮 | "
                 f"claim {review.get('total_claims', 0)} 条")
    lines.append("═" * 70)

    # ── 结论榜 ──
    lines.append(f"\n{'排名':<4}{'代码':<8}{'名称':<10}{'综合分':<7}{'置信度':<6}风险标签")
    lines.append("─" * 70)
    for r in ranking:
        flags = " ".join(r.get("risk_flags", [])) or "-"
        name = r.get("name", "")[:8]
        lines.append(f"{r['rank']:<5}{r['code']:<9}{name:<11}"
                     f"{r.get('score', 0):<8.0f}{r.get('confidence_level', '-'):<7}{flags}")

    # ── 逐股解读 ──
    lines.append("\n" + "─" * 70)
    lines.append("  📋 逐股解读")
    lines.append("─" * 70)
    for r in ranking:
        c = cands.get(r["code"], {})
        e = c.get("essence", {})
        lines.append(f"\n#{r['rank']} {r['code']} {r.get('name', '')}  "
                     f"[综合分{r.get('score', 0):.0f} 置信度{r.get('confidence_level', '-')}]")
        lines.append(f"  ✅ 核心逻辑: {r.get('key_thesis') or e.get('biggest_bull', '-')}")
        lines.append(f"  ⚠️  核心风险: {r.get('key_risk') or e.get('biggest_bear', '-')}")
        if r.get("risk_flags"):
            lines.append(f"  🚩 风险标签: {' / '.join(r['risk_flags'])}")
        if c:
            lines.append(f"  📊 数据: tech={c.get('tech_total', 0):.0f}/100 "
                         f"主力5日净{c.get('fund_5d', 0):+.1f}亿 "
                         f"催化时效={e.get('catalyst_horizon', '-')}")
        if r.get("supporting_claim_ids"):
            lines.append(f"  🔗 支撑论点: {', '.join(r['supporting_claim_ids'])}")

    # ── 辩论回放 ──
    claims = led.get("claims", [])
    if claims:
        lines.append("\n" + "─" * 70)
        lines.append("  💬 辩论关键论点回放")
        lines.append("─" * 70)
        for cl in claims:
            tag = "🟢多" if cl.get("stance") == "bullish" else "🔴空"
            ev = "; ".join(cl.get("evidence", [])[:2])
            lines.append(f"  {tag} [{cl.get('claim_id')}] {cl.get('code')}: "
                         f"{cl.get('claim')} (置信{cl.get('confidence')})")
            if ev:
                lines.append(f"       证据: {ev}")

    # ── 术语解释 ──
    lines.append("\n" + "─" * 70)
    lines.append("  📖 术语解释 (新手向)")
    lines.append("─" * 70)
    for term, desc in GLOSSARY.items():
        lines.append(f"  • {term}: {desc}")

    # ── 风险提示 ──
    lines.append("\n" + "─" * 70)
    lines.append("  ⚠️  可信度与风险提示")
    lines.append("─" * 70)
    lines.append(f"  整体可信度: {review.get('overall_level', '-')} "
                 f"({review.get('overall_confidence', '-')})")
    lines.append(f"  数据完整度: {review.get('data_integrity', '-')} | "
                 f"未决分歧 claim: {review.get('unresolved_claims', 0)} 条")
    if review.get("risk_flag_summary"):
        lines.append(f"  风险标签分布: {review['risk_flag_summary']}")
    lines.append(f"\n  📢 {review.get('disclaimer', '')}")
    lines.append("═" * 70)
    return "\n".join(lines)
