"""debate_picker v5 — LangGraph 图编排骨架 (M1)。

7 阶段流程:
  collect_data → [technical | fund | fundamental] (并行)
              → screen_round1 (海选 50→20)
              → debate_loop   (claim 驱动交叉辩论 20→10, 可循环)
              → final_judge   (终极 PK 10→排名)
              → risk_review   (可信度 + 风险)
              → report_render (终端报告 + 落盘)
              → END

M1 阶段: 所有节点为 stub, 仅验证图能 compile + 跑通到 END。
后续里程碑逐节点替换为真实实现。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from . import analysts as analyst_nodes
from . import debaters as debater_nodes
from . import incremental as incremental_nodes
from . import judges as judge_nodes
from . import reporter as reporter_nodes
from .llm_helper import LLMHelper
from .picker_state import PickerState, new_debate_ledger


class PickerGraph:
    """30 天涨幅竞争辩论图。"""

    def __init__(self, config: Dict[str, Any] | None = None, max_debate_rounds: int = 3,
                 top_n: int = 50, screen_mode: str = "hybrid",
                 debate_top_k: int = 10, force_k: int = 6):
        self.config = config or {}
        self.max_debate_rounds = max_debate_rounds
        self.top_n = top_n
        self.screen_mode = screen_mode
        self.debate_top_k = debate_top_k
        self.force_k = force_k
        self.llm = LLMHelper(self.config)
        self.graph = self._build_graph()

    # ──────────────────────────────────────────────
    # 节点
    # ──────────────────────────────────────────────

    def _collect_data(self, state: PickerState) -> Dict[str, Any]:
        return analyst_nodes.collect_data(state, top_n=self.top_n)

    def _analysts_done(self, state: PickerState) -> Dict[str, Any]:
        """fan-in 汇合节点 (空操作, 仅用于同步三个并行分析师)。"""
        return {}

    # ──────────────────────────────────────────────
    # 条件边: 辩论循环收敛
    # ──────────────────────────────────────────────

    def _should_continue_debate(self, state: PickerState) -> str:
        ledger = state.get("debate_ledger") or {}
        if ledger.get("finished"):
            return "final_judge"
        return "debate_round"

    # ──────────────────────────────────────────────
    # 图构建
    # ──────────────────────────────────────────────

    def _build_graph(self):
        g = StateGraph(PickerState)

        g.add_node("collect_data", self._collect_data)
        g.add_node("incremental_info", incremental_nodes.make_incremental_info(self.llm))
        g.add_node("technical_analyst", analyst_nodes.make_technical_analyst(self.llm))
        g.add_node("fund_analyst", analyst_nodes.make_fund_analyst(self.llm))
        g.add_node("fundamental_analyst", analyst_nodes.make_fundamental_analyst(self.llm))
        g.add_node("analysts_done", self._analysts_done)
        g.add_node("screen_round1", judge_nodes.make_screen_round1(self.llm))
        g.add_node("debate_round", debater_nodes.make_debate_round(self.llm))
        g.add_node("final_judge", judge_nodes.make_final_judge(self.llm))
        g.add_node("risk_review", reporter_nodes.make_risk_review())
        g.add_node("report_render", reporter_nodes.make_report_render())

        # START → 数据采集 → 增量信息
        g.add_edge(START, "collect_data")
        g.add_edge("collect_data", "incremental_info")

        # 增量信息 → 三分析师并行 (fan-out)
        g.add_edge("incremental_info", "technical_analyst")
        g.add_edge("incremental_info", "fund_analyst")
        g.add_edge("incremental_info", "fundamental_analyst")

        # 三分析师 → 汇合 (fan-in)
        g.add_edge(
            ["technical_analyst", "fund_analyst", "fundamental_analyst"],
            "analysts_done",
        )

        # 汇合 → 海选 → 辩论
        g.add_edge("analysts_done", "screen_round1")
        g.add_edge("screen_round1", "debate_round")

        # 辩论循环: debate_round → (debate_round | final_judge)
        g.add_conditional_edges(
            "debate_round",
            self._should_continue_debate,
            {"debate_round": "debate_round", "final_judge": "final_judge"},
        )

        # 终极 PK → 风险 → 报告 → END
        g.add_edge("final_judge", "risk_review")
        g.add_edge("risk_review", "report_render")
        g.add_edge("report_render", END)

        return g.compile()

    # ──────────────────────────────────────────────
    # 辅助
    # ──────────────────────────────────────────────

    @staticmethod
    def _trace(node: str, note: str) -> dict:
        return {"node": node, "note": note, "ts": datetime.now().isoformat()}

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
                "max_debate_rounds": self.max_debate_rounds,
                "screen_mode": self.screen_mode,
                "debate_top_k": self.debate_top_k,
                "force_k": self.force_k,
            },
        }
        print(f"{'='*60}\n  debate_picker v5 — {trade_date}"
              f"{' (回测 cutoff=' + cutoff_date + ')' if cutoff_date else ''}\n"
              f"  架构: 7阶段 LangGraph (3分析师 + 海选 + 交叉辩论 + 终极PK + 风控)\n"
              f"  落盘: {run_dir}\n{'='*60}")
        result = self.graph.invoke(init)
        print(f"\n  ▶ 图执行完成")
        return result
