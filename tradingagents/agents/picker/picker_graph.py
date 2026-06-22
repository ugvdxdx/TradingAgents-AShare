"""debate_picker v5 — 量化选股图编排 (纯量化基线)。

候选池(V3 全池 + 新晋股 + 研报热门股)统一在 stage1 汇入, 按量化锚排序收窄:
  collect_data → quantum_rank → risk_review → report_render → END

排序锚 = chain + capital×2 - delivery×0.5
(回测验证: 21期×530只×30日 Spearman=+0.555, 20/20正相关)。
无 LLM 辩论 — LLM 从头排序回测为负相关(-0.14), 会破坏量化信号。

注: analysts/incremental 里的三分析师 + 增量信息节点暂未接入,
    待"任务2: 增量信息用于风险调整"时再启用。
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
    """量化选股图: 候选池 → 量化锚排序 → TOP5。

    4 节点基线 (collect_data → quantum_rank → risk_review → report_render),
    零 LLM 调用。排序锚 = chain + capital×2 - delivery×0.5
    (回测验证: 21期×530只×30日 Spearman=+0.555, 20/20正相关)。
    """

    def __init__(self, config: Dict[str, Any] | None = None,
                 debate_top_k: int = 5):
        """量化选股基线。

        Args:
            debate_top_k: 最终排名规模 (默认5, 策略回测最优)。
        """
        self.config = config or {}
        self.debate_top_k = debate_top_k
        self.llm = LLMHelper(self.config)
        self.graph = self._build_graph()

    # ──────────────────────────────────────────────
    # 节点
    # ──────────────────────────────────────────────

    def _collect_data(self, state: PickerState) -> Dict[str, Any]:
        return analyst_nodes.collect_data(state)

    # ──────────────────────────────────────────────
    # 图构建
    # ──────────────────────────────────────────────

    def _build_graph(self):
        """量化选股基线拓扑 (4 节点, 无LLM辩论):
        collect_data → quantum_rank → risk_review → report_render → END

        - collect_data: V3缓存+K线+资金流 → 候选池(含chain/capital/delivery)
        - quantum_rank: 按锚(chain+capital×2-delivery×0.5)排序取TOP_k
        - risk_review: 可信度+风险提示
        - report_render: 终端报告+落盘+🎯策略信号

        回测验证: 锚分Spearman=+0.555 (21期×530只×30日, 20/20正相关)。
        增量信息/三分析师节点暂未接入, 待"任务2"启用。
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
              f"  架构: 纯量化基线 "
              f"(全池 → 量化锚排序 → TOP{self.debate_top_k}, 无LLM辩论)\n"
              f"  落盘: {run_dir}\n{'='*60}")
        result = self.graph.invoke(init)

        # 实盘模式: 存每日快照 (全池分数 + TOP推荐 + 理由)
        # 同日多次跑会覆盖, 只留最后一次。回测按 cutoff 取最近快照, 消除前视偏差。
        if not cutoff_date and not dry_run:
            self._save_daily_snapshot(trade_date, result)

        print(f"\n  ▶ 图执行完成")
        return result

    @staticmethod
    def _save_daily_snapshot(trade_date: str, result: PickerState):
        """存每日选股快照: 全池 V3 分数 + TOP 推荐结果 + 推荐理由。

        格式: data/caches/v3_snapshots/YYYY-MM-DD.json
        同日覆盖 (一天只留最后一次)。回测用 cutoff ≤ T 的最近快照。
        """
        import json
        from picker.paths import V3_SNAPSHOT_DIR
        os.makedirs(V3_SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(V3_SNAPSHOT_DIR, f"{trade_date}.json")

        # 全池分数 (从 candidates 提取 chain/delivery/capital)
        scores = {}
        for c in result.get("candidates", []):
            code = c.get("code", "")
            if not code:
                continue
            scores[code] = {
                "chain": c.get("chain", 0),
                "delivery": c.get("delivery", 0),
                "capital": c.get("capital", 0),
            }

        snapshot = {
            "date": trade_date,
            "scores": scores,  # {code: {chain, delivery, capital}}
            "ranking": result.get("final_ranking", []),  # TOP5/10 含 key_thesis/key_risk
        }
        try:
            json.dump(snapshot, open(path, "w"), ensure_ascii=False, indent=1)
            print(f"  📸 每日快照已存: {path} ({len(scores)} 只股)")
        except Exception as e:
            print(f"  ⚠ 快照存储失败: {e}")
