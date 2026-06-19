"""debate_picker v5 — 数据采集 + 三分析师节点 (M2)。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from tech_analysis import compute_tech_score

from . import data_io
from .llm_helper import LLMHelper
from .prompts import (
    FUND_ANALYST_SYSTEM,
    FUNDAMENTAL_ANALYST_SYSTEM,
    TECHNICAL_ANALYST_SYSTEM,
)


def _trace(node: str, note: str) -> dict:
    return {"node": node, "note": note, "ts": datetime.now().isoformat()}


def _dump(run_dir: str, name: str, content: Any, as_json: bool = False):
    """落盘辅助。"""
    path = os.path.join(run_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        if as_json:
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(content))


# ══════════════════════════════════════════════════════════
# 阶段 1: 数据采集
# ══════════════════════════════════════════════════════════

def collect_data(state, top_n: int = 50) -> Dict[str, Any]:
    """加载 Top-N 候选 + 技术面 + 资金流 + 数据校验。"""
    run_dir = state["run_dir"]
    cutoff = state.get("cutoff_date")
    print(f"\n{'='*60}\n📡 [阶段 1/7] 数据采集 (Top{top_n})\n{'='*60}")

    # ── 先更新 capital 子维度 (纯量化, 0次LLM, 几秒完成) ──
    # 只算不写文件 (persist=False), 避免与手动跑 _v3_full_score 的文件竞争
    # 回测模式 (cutoff_date 非空) 跳过, 避免用未来数据污染
    v3_cache_override = None
    if not cutoff:
        try:
            from _v3_full_score import update_capital, detect_overheated
            capital_mode = os.environ.get("CAPITAL_MODE", "D")
            v3_cache_override = update_capital(mode=capital_mode, persist=False)
            # 过热股检测: 高分但持续下跌的标的, 搜索验证 + 风险标记 (不改 V3 分)
            if v3_cache_override:
                v3_cache_override = detect_overheated(v3_cache_override)
        except Exception as e:
            print(f"  [capital] 更新失败(不影响流程): {e}")

    pool = data_io.load_top_n(top_n, v3_cache=v3_cache_override)

    # 回写过热风险标记到候选池 (供 format_stock_brief 显示 + 辩论参考)
    if not cutoff:
        try:
            oh_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".overheated_risk_cache.json")
            if os.path.exists(oh_path):
                oh_cache = json.load(open(oh_path))
                oh_marked = 0
                for s in pool:
                    oh = oh_cache.get(s["code"])
                    if oh and oh.get("risk_type") not in ("未知", "技术回调", ""):
                        s["_overheated_risk"] = oh.get("risk_type", "")
                        oh_marked += 1
                if oh_marked:
                    print(f"  ⚠ 过热风险标记: {oh_marked} 只")
        except Exception:
            pass

    mf_cache = data_io.load_mf_cache()
    print(f"  V3 Top{top_n}: {pool[0]['v3']:.1f} ~ {pool[-1]['v3']:.1f} | 资金流缓存 {len(mf_cache)} 只")

    candidates: List[dict] = []
    n_missing = n_partial = 0
    for s in pool:
        code = s["code"]
        df = data_io.load_kline(code, cutoff)
        if df is None:
            n_missing += 1
            continue  # K线不足, 剔除
        tech = compute_tech_score(df.reset_index() if hasattr(df, "index") else df)
        fund = data_io.fund_flow_5d(mf_cache, code, cutoff)
        quality = "ok"
        if fund is None:
            quality = "partial"
            fund = 0.0
            n_partial += 1
        if not s.get("essence"):
            quality = "partial" if quality == "ok" else quality
        c = dict(s)
        c.update(
            tech_total=tech.total, tech_trend=tech.trend, tech_mom=tech.momentum,
            fund_5d=fund, data_quality=quality,
        )
        candidates.append(c)

    print(f"  候选 {len(candidates)} 只 (剔除 {n_missing} 只数据缺失, {n_partial} 只资金流缺失)")
    _dump(run_dir, "01_candidates.json", candidates, as_json=True)

    return {
        "candidates": candidates,
        "trace": [_trace("collect_data",
                         f"候选{len(candidates)} 剔除{n_missing} 资金缺{n_partial}")],
    }


# ══════════════════════════════════════════════════════════
# 阶段 2: 三分析师 (并行)
# ══════════════════════════════════════════════════════════

def _fmt_technical(candidates: List[dict]) -> str:
    return "\n".join(
        f"{c['code']} {c['name']}: tech={c['tech_total']:.0f}/100 "
        f"(趋势{c['tech_trend']:.0f} 动量{c['tech_mom']:.0f})"
        for c in candidates
    )


def _fmt_fund(candidates: List[dict]) -> str:
    return "\n".join(
        f"{c['code']} {c['name']}: 5日主力净{c['fund_5d']:+.1f}亿"
        f"{' [资金流缺失]' if c['data_quality'] == 'partial' else ''}"
        for c in candidates
    )


def _fmt_fundamental(candidates: List[dict]) -> str:
    lines = []
    for c in candidates:
        e = c.get("essence", {})
        lines.append(
            f"{c['code']} {c['name']} V3={c['v3']:.1f} [链{c['chain']}+兑{c['delivery']}+资{c['capital']}]\n"
            f"  卡位:{e.get('chain_position', '')} | 催化:{e.get('core_catalyst', '')}\n"
            f"  多头:{e.get('biggest_bull', '')} | 空头:{e.get('biggest_bear', '')}\n"
            f"  红线:{e.get('quality_redline', '')} | horizon:{e.get('catalyst_horizon', 'mid')}"
        )
    return "\n\n".join(lines)


def _fmt_incremental(candidates: List[dict], briefs: Dict[str, str]) -> str:
    """格式化增量信息简报 (实时财务+新闻+量化信号)。"""
    lines = []
    for c in candidates:
        b = briefs.get(c["code"], "")
        if b:
            lines.append(b)
    return "\n\n".join(lines) if lines else "(无增量信息)"


def make_technical_analyst(llm: LLMHelper):
    def node(state) -> Dict[str, Any]:
        print("  ▶ [阶段 2/7] 技术面分析师")
        cands = state.get("candidates", [])
        if state.get("dry_run") or not cands:
            return {"analyst_reports": {"technical": "(dry-run)"},
                    "trace": [_trace("technical_analyst", "dry-run/空")]}
        human = _fmt_technical(cands)
        # 注入量化差分信号
        briefs = state.get("incremental_briefs", {})
        incr = _fmt_incremental(cands, briefs)
        if incr and incr != "(无增量信息)":
            human += f"\n\n--- 量化差分信号 ---\n{incr}"
        report = llm.call(TECHNICAL_ANALYST_SYSTEM, human,
                          deep=False, max_chars=3000)
        _dump(state["run_dir"], "02_analyst_technical.md", report)
        return {"analyst_reports": {"technical": report},
                "trace": [_trace("technical_analyst", f"{len(report)}字")]}
    return node


def make_fund_analyst(llm: LLMHelper):
    def node(state) -> Dict[str, Any]:
        print("  ▶ [阶段 2/7] 资金面分析师")
        cands = state.get("candidates", [])
        if state.get("dry_run") or not cands:
            return {"analyst_reports": {"fund": "(dry-run)"},
                    "trace": [_trace("fund_analyst", "dry-run/空")]}
        human = _fmt_fund(cands)
        # 注入资金流趋势等增量信息
        briefs = state.get("incremental_briefs", {})
        incr = _fmt_incremental(cands, briefs)
        if incr and incr != "(无增量信息)":
            human += f"\n\n--- 增量信息 ---\n{incr}"
        rot = state.get("rotation_context", "")
        if rot:
            human += f"\n\n--- 板块资金轮动 ---\n{rot}"
        # 研报行业动量 (领先信号, 与资金流滞后确认互补)
        rctx = state.get("research_context", "")
        if rctx:
            human += f"\n\n--- 研报行业动量与市场情绪 ---\n{rctx}"
        report = llm.call(FUND_ANALYST_SYSTEM, human,
                          deep=False, max_chars=3000)
        _dump(state["run_dir"], "02_analyst_fund.md", report)
        return {"analyst_reports": {"fund": report},
                "trace": [_trace("fund_analyst", f"{len(report)}字")]}
    return node


def make_fundamental_analyst(llm: LLMHelper):
    def node(state) -> Dict[str, Any]:
        print("  ▶ [阶段 2/7] 基本面/催化面分析师")
        cands = state.get("candidates", [])
        if state.get("dry_run") or not cands:
            return {"analyst_reports": {"fundamental": "(dry-run)"},
                    "trace": [_trace("fundamental_analyst", "dry-run/空")]}
        human = _fmt_fundamental(cands)
        # 注入实时财务+新闻+竞争分析 (最关键的增量信息)
        briefs = state.get("incremental_briefs", {})
        incr = _fmt_incremental(cands, briefs)
        if incr and incr != "(无增量信息)":
            human += f"\n\n--- 实时增量信息 (财务+新闻+竞争+量化) ---\n{incr}"
        # 研报行业动量 + 市场情绪 (外部市场视角, V3没有)
        rctx = state.get("research_context", "")
        if rctx:
            human += f"\n\n--- 研报行业动量与市场情绪 ---\n{rctx}"
        report = llm.call(FUNDAMENTAL_ANALYST_SYSTEM, human,
                          deep=True, max_chars=5000)
        _dump(state["run_dir"], "02_analyst_fundamental.md", report)
        return {"analyst_reports": {"fundamental": report},
                "trace": [_trace("fundamental_analyst", f"{len(report)}字")]}
    return node
