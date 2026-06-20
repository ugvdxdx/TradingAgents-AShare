"""debate_picker v5 — LangGraph 图编排 (两段式辩论漏斗)。

候选池(V3 Top50 + 新晋股全部 + 研报热门股)统一在 stage1 汇入, 经排序竞争辩论收窄:
  collect_data → incremental_info → [technical | fund | fundamental] (并行)
              → screen_debate  (海选: 蛇形分3组 × 每组多轮辩论 → 30只)
              → ranking_debate (排名辩论: 多轮 claim 攻防逐轮收窄 30→10, 末轮出排名)
              → risk_review    (可信度 + 风险)
              → report_render  (终端报告 + 落盘)
              → END

辩论导向: 排序竞争 (比谁涨更多, 非多空对抗)。海选与排名辩论复用同一套 claim 攻防+裁决逻辑。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from . import analysts as analyst_nodes
from . import debaters as debater_nodes
from . import reporter as reporter_nodes
from .llm_helper import LLMHelper
from .picker_state import PickerState


class PickerGraph:
    """量化选股图: 候选池 → 量化锚排序 → TOP10。

    排序锚 = chain + capital×2 - delivery×0.5
    (回测验证: 21期×530只×30日 Spearman=+0.555, 20/20正相关)
    """

    def __init__(self, config: Dict[str, Any] | None = None,
                 top_n: int = 50, debate_top_k: int = 10):
        """量化选股基线。

        Args:
            top_n: V3 Top-N 候选池规模 (不含新晋股/研报股加挂)。
            debate_top_k: 最终排名规模 (TOP10)。
        """
        self.config = config or {}
        self.top_n = top_n
        self.debate_top_k = debate_top_k
        self.llm = LLMHelper(self.config)
        self.graph = self._build_graph()

    # ──────────────────────────────────────────────
    # 节点
    # ──────────────────────────────────────────────

    def _collect_data(self, state: PickerState) -> Dict[str, Any]:
        return analyst_nodes.collect_data(state, top_n=self.top_n)

    # ──────────────────────────────────────────────
    # 图构建
    # ──────────────────────────────────────────────

    def _build_graph(self):
        """量化选股基线拓扑 (极简, 无LLM辩论):
        collect_data → quantum_rank → risk_review → report_render → END

        - collect_data: V3缓存+K线+资金流 → 候选池(含chain/capital/delivery)
        - quantum_rank: 按锚(chain+capital×2-delivery×0.5)排序取TOP10
        - risk_review: 可信度+风险提示
        - report_render: 终端报告+落盘

        回测验证: 锚分Spearman=+0.555 (21期×530只×30日, 20/20正相关)。
        增量信息/三分析师暂时跳过, 下一步尝试接入。
        """
        g = StateGraph(PickerState)

        g.add_node("collect_data", self._collect_data)
        g.add_node("quantum_rank", debater_nodes.make_ranking_debate(
            self.llm, 1, self.debate_top_k))
        g.add_node("risk_review", reporter_nodes.make_risk_review())
        g.add_node("report_render", reporter_nodes.make_report_render())

        g.add_edge(START, "collect_data")
        g.add_edge("collect_data", "quantum_rank")
        g.add_edge("quantum_rank", "risk_review")
        g.add_edge("risk_review", "report_render")
        g.add_edge("report_render", END)

        return g.compile()

    def run(self, trade_date: str | None = None, cutoff_date: str | None = None,
            dry_run: bool = False) -> PickerState:
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        sub = "backtest/" + (cutoff_date or trade_date) if cutoff_date else f"{trade_date}_{stamp}"
        run_dir = os.path.join("results", "picker_v5", sub)
        os.makedirs(run_dir, exist_ok=True)

        init: PickerState = {
            "trade_date": trade_date,
            "cutoff_date": cutoff_date,
            "dry_run": dry_run,
            "run_dir": run_dir,
            "metadata": {
                "debate_top_k": self.debate_top_k,
            },
        }
        print(f"{'='*60}\n  debate_picker v5 — {trade_date}"
              f"{' (回测 cutoff=' + cutoff_date + ')' if cutoff_date else ''}\n"
              f"  架构: 两段式辩论漏斗 "
              f"(候选{self.top_n}+ → 量化锚排序 → TOP{self.debate_top_k}, 无LLM辩论)\n"
              f"  落盘: {run_dir}\n{'='*60}")
        result = self.graph.invoke(init)
        print(f"\n  ▶ 图执行完成")
        return result
