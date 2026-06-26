"""debate_picker v5 — 数据采集 + 三分析师节点 (M2)。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from picker.scoring.tech_analysis import compute_tech_score

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

def collect_data(state) -> Dict[str, Any]:
    """加载全池候选 + 技术面 + 资金流 + 数据校验。

    回测验证(125期, G模式): 召回预筛(top50/100) TOP10涨幅与全池无差异,
    且 load_top_n 的保送机制(新晋股/研报热门/强制纳入)需全池才能生效 → 固定全池。
    """
    run_dir = state["run_dir"]
    cutoff = state.get("cutoff_date")
    print(f"\n{'='*60}\n📡 [阶段 1/4] 数据采集 (全池)\n{'='*60}")

    # ── 先更新 capital 子维度 (纯量化, 0次LLM, 几秒完成) ──
    # 只算不写文件 (persist=False), 避免与手动跑 _v3_full_score 的文件竞争
    # 回测模式 (cutoff_date 非空): pf/d2 按 cutoff 截断 K线重算, base 用当前 momentum 快照
    #   (研报无可靠历史版, 故 base 不重算; 这是回测的已知近似, 详见 CLAUDE.md)
    v3_cache_override = None
    try:
        from picker.scoring.v3_full_score import update_capital
        # capital: G 模式 (base+d2×2+pf×2 无封顶, 策略回测月均+31%)
        v3_cache_override = update_capital(persist=False, cutoff_date=cutoff or "")
    except Exception as e:
        print(f"  [capital] 更新失败(不影响流程): {e}")

    pool = data_io.load_top_n(v3_cache=v3_cache_override, cutoff_date=cutoff or "")

    mf_cache = data_io.load_mf_cache()
    print(f"  V3 全池 {len(pool)} 只: v3 {pool[0]['v3']:.1f} ~ {pool[-1]['v3']:.1f} | 资金流缓存 {len(mf_cache)} 只")

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
        # 量价动量信号 (回测验证的最强涨幅先行指标: r20>15%或距高点<5%的股后续涨幅显著更高)
        df_k = df.sort_values("trade_date").reset_index(drop=True) if hasattr(df, "sort_values") else df
        close = df_k["close"] if hasattr(df_k, "close") else None
        r5 = r20 = dist_high = None
        if close is not None and len(close) >= 21:
            r5 = round((close.iloc[-1] / close.iloc[-6] - 1) * 100, 1)
            r20 = round((close.iloc[-1] / close.iloc[-21] - 1) * 100, 1)
            dist_high = round((close.iloc[-1] / close.iloc[-20:].max() - 1) * 100, 1)
        c = dict(s)
        c.update(
            tech_total=tech.total, tech_trend=tech.trend, tech_mom=tech.momentum,
            fund_5d=fund, data_quality=quality,
            r5=r5, r20=r20, dist_high=dist_high,
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
# 阶段 2: 三分析师 (并行) — ⚠ 未接入当前4节点基线, 待"任务2"启用
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
            f"{c['code']} {c['name']} V3={c['v3']:.1f} [链{c['chain']}+爆{c['surge']}+资{c['capital']}]\n"
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
        print("  ▶ [未接入] 技术面分析师")
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
        print("  ▶ [未接入] 资金面分析师")
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
        print("  ▶ [未接入] 基本面/催化面分析师")
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
