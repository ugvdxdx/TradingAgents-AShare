"""debate_picker v5 — 图状态定义 (State Schema)。

借鉴 tradingagents/agents/utils/agent_states.py 的 TypedDict + Annotated 范式，
为 30 天涨幅竞争辩论系统定义结构化状态。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional

from typing_extensions import TypedDict


def merge_dict(left: Dict[str, Any] | None, right: Dict[str, Any] | None) -> Dict[str, Any]:
    """Reducer: 合并两个 dict (用于并行节点写同一字段, 如三分析师报告)。"""
    out = dict(left or {})
    out.update(right or {})
    return out


# ══════════════════════════════════════════════════════════
# 候选股档案
# ══════════════════════════════════════════════════════════

class StockProfile(TypedDict, total=False):
    """单只候选股的完整档案。"""
    code: str
    name: str
    # ── 基本面打分 (V3) ──
    v3: float                       # V3 综合基本面分 (sector_score)
    chain: float                    # 产业链卡位子维度
    delivery: float                 # 业绩兑现子维度
    capital: float                  # 资金/热度子维度
    # ── 基本面精简信息 (essence, V3 LLM 提炼的定性精华) ──
    essence: Dict[str, Any]         # chain_position/core_catalyst/biggest_bull/
                                    # biggest_bear/quality_redline/catalyst_horizon
    brief: str                      # 一句话综述
    # ── 实时定量 ──
    tech_total: float               # 技术综合分 0~100
    tech_trend: float               # 趋势分
    tech_mom: float                 # 动量分
    fund_5d: float                  # 5 日主力净流入 (亿)
    # ── 数据质量 ──
    data_quality: str               # ok / partial / missing


# ══════════════════════════════════════════════════════════
# 辩论论点 (Claim) — 辩论严谨性核心
# ══════════════════════════════════════════════════════════

class Claim(TypedDict, total=False):
    """单条结构化论点，可被引用、反驳、解决、标记未决。"""
    claim_id: str                   # "BULL-3" / "BEAR-7"
    code: str                       # 针对哪只股 (空=泛论点)
    speaker: str                    # 发言者标签
    stance: str                     # bullish / bearish
    claim: str                      # 论点文本
    evidence: List[str]             # 证据 (最多 3 条)
    confidence: float               # 0~1
    status: str                     # open / addressed / resolved / unresolved
    target_claim_ids: List[str]     # 反驳了哪些 claim
    round_index: int


class DebateLedger(TypedDict, total=False):
    """claim 账本 + 辩论进程状态。"""
    round: int
    max_rounds: int
    claims: List[Claim]
    open_claim_ids: List[str]
    resolved_claim_ids: List[str]
    unresolved_claim_ids: List[str]
    focus_claim_ids: List[str]      # 下一轮必须回应
    round_summary: str
    round_goal: str
    claim_counter: int
    finished: bool


# ══════════════════════════════════════════════════════════
# 排名结果
# ══════════════════════════════════════════════════════════

class RankItem(TypedDict, total=False):
    rank: int
    code: str
    name: str
    score: float                    # 综合涨幅潜力分
    confidence: float               # 该排名置信度 0~1
    confidence_level: str           # 高 / 中 / 低
    key_thesis: str                 # 核心多头逻辑 (完整, 非 15 字)
    key_risk: str                   # 核心风险
    supporting_claim_ids: List[str] # 支撑该排名的 claim
    risk_flags: List[str]           # 风险标签


# ══════════════════════════════════════════════════════════
# 全局图状态
# ══════════════════════════════════════════════════════════

class PickerState(TypedDict, total=False):
    # ── 运行参数 ──
    trade_date: str                 # 实盘=当日, 回测=截止日
    cutoff_date: Optional[str]      # 回测模式专用 (None=实盘)
    dry_run: bool                   # True=跳过 LLM, 用于验证管道
    run_dir: str                    # 本次运行落盘目录

    # ── 阶段产物 ──
    candidates: List[StockProfile]  # Top-N 候选 (采集后)
    incremental_briefs: Dict[str, str]  # 增量信息简报 (code→text, 基本面深度+量化信号+事件)
    rotation_context: str           # 行业轮动上下文 (板块资金流排名+主线切换信号, 实盘才有)
    research_context: str           # 研报行业动量+市场情绪 (research.db, 回测安全)
    analyst_reports: Annotated[Dict[str, str], merge_dict]  # 并行写, dict 合并
    analyst_claims: Annotated[List[Claim], operator.add]    # 分析师阶段初始 claim
    round1_promoted: List[str]      # 海选晋级 codes (50→20)
    round2_promoted: List[str]      # 交叉辩论晋级 codes (20→10)
    debate_ledger: DebateLedger     # claim 账本
    final_ranking: List[RankItem]   # 最终排名 (10→排序)
    risk_review: Dict[str, Any]     # 可信度评估 + 风险提示

    # ── 可追溯 ──
    trace: Annotated[List[dict], operator.add]  # 全过程决策链 (逐节点 append)
    metadata: Dict[str, Any]


# ══════════════════════════════════════════════════════════
# 工厂函数
# ══════════════════════════════════════════════════════════

def new_debate_ledger(max_rounds: int = 3) -> DebateLedger:
    """构造空的 claim 账本。"""
    return {
        "round": 1,
        "max_rounds": max_rounds,
        "claims": [],
        "open_claim_ids": [],
        "resolved_claim_ids": [],
        "unresolved_claim_ids": [],
        "focus_claim_ids": [],
        "round_summary": "",
        "round_goal": "",
        "claim_counter": 0,
        "finished": False,
    }
