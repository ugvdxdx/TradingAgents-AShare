"""ReportArchiver: persist each analysis stage into a structured file tree.

File tree layout:
    {results_dir}/{ticker}/{YYYY-MM-DD}/
      00_meta/        — metadata + provider trace
      01_data_collection/ — raw data from DataCollector cache
      02_analysts/     — 7 analyst reports + traces
      03_debate/       — bull/bear arguments + debate state
      04_research_manager/ — investment plan
      05_trader/       — trader plan
      06_risk_debate/  — 3 risk debaters + risk debate state
      07_risk_judge/   — final decision + risk feedback
      summary.md       — combined summary
"""

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles common non-serializable types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, float):
            if math.isinf(obj):
                return "Infinity" if obj > 0 else "-Infinity"
            if math.isnan(obj):
                return "NaN"
            return obj
        # Fallback: convert to string
        try:
            return str(obj)
        except Exception:
            return f"<unserializable {type(obj).__name__}>"


def _safe_ticker(ticker: str) -> str:
    """Sanitize ticker for filesystem paths."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker) or "unknown"


def _write_md(path: Path, content: str) -> None:
    """Write a markdown file. Empty content gets a placeholder."""
    if not content or not content.strip():
        content = "(此阶段未产出内容)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: Any) -> None:
    """Write a JSON file with SafeJSONEncoder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, cls=SafeJSONEncoder)


class ReportArchiver:
    """Writes a completed analysis state into a structured file tree."""

    def __init__(self, results_dir: str = None):
        self.results_dir = results_dir or os.getenv("TA_RESULTS_DIR", "./results")

    def archive(
        self,
        ticker: str,
        trade_date: str,
        final_state: Dict[str, Any],
        *,
        duration_seconds: Optional[float] = None,
        provider_traces: Optional[List[str]] = None,
        data_pool: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        status: str = "completed",
    ) -> Path:
        """Main entry point. Creates the directory tree and writes all files.

        Args:
            ticker: Stock symbol.
            trade_date: YYYY-MM-DD string.
            final_state: The completed LangGraph state dict.
            duration_seconds: Wall-clock time of the analysis run.
            provider_traces: Captured provider routing trace messages.
            data_pool: DataCollector cache dict (for 01_data_collection stage).
            config: Runtime config dict (for model metadata).
            status: "completed" or "failed".

        Returns:
            Root path of the archive directory.
        """
        safe = _safe_ticker(ticker)
        root = Path(self.results_dir) / safe / trade_date

        self._write_meta(root, ticker, trade_date, final_state,
                         duration_seconds, provider_traces, config, status)
        self._write_data_collection(root, data_pool)
        self._write_analysts(root, final_state)
        self._write_debate(root, final_state)
        self._write_research_manager(root, final_state)
        self._write_trader(root, final_state)
        self._write_risk_debate(root, final_state)
        self._write_risk_judge(root, final_state)
        self._write_summary(root, final_state)

        return root

    # ── 00_meta ──────────────────────────────────────────────

    def _write_meta(
        self, root: Path, ticker: str, trade_date: str,
        final_state: Dict[str, Any],
        duration_seconds: Optional[float],
        provider_traces: Optional[List[str]],
        config: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        meta_dir = root / "00_meta"

        # Signal extraction
        decision = final_state.get("final_trade_decision", "")
        signal = "UNKNOWN"
        if decision:
            m = re.search(r'<!--\s*VERDICT:\s*(\{.*?\})\s*-->', decision, re.DOTALL)
            if m:
                try:
                    d = json.loads(m.group(1))
                    signal = d.get("direction", "UNKNOWN")
                except Exception:
                    pass

        metadata = {
            "ticker": ticker,
            "trade_date": trade_date,
            "status": status,
            "signal": signal,
            "duration_seconds": duration_seconds,
            "horizon": final_state.get("horizon", ""),
            "company_of_interest": final_state.get("company_of_interest", ""),
            "instrument_context": final_state.get("instrument_context", {}),
            "market_context": final_state.get("market_context", {}),
            "user_context": final_state.get("user_context", {}),
            "workflow_context": final_state.get("workflow_context", {}),
        }
        if config:
            metadata["models"] = {
                "deep_think": config.get("deep_think_llm", ""),
                "quick_think": config.get("quick_think_llm", ""),
                "provider": config.get("llm_provider", ""),
            }
            metadata["debate_config"] = {
                "max_debate_rounds": config.get("max_debate_rounds"),
                "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds"),
            }

        _write_json(meta_dir / "metadata.json", metadata)
        _write_json(meta_dir / "provider_trace.json", provider_traces or [])

    # ── 01_data_collection ───────────────────────────────────

    def _write_data_collection(self, root: Path, data_pool: Optional[Dict[str, Any]]) -> None:
        dc_dir = root / "01_data_collection"

        if data_pool is None:
            _write_json(dc_dir / "stock_data.json", {"note": "数据池未提供"})
            return

        _write_json(dc_dir / "stock_data.json", data_pool.get("stock_data", ""))
        _write_json(dc_dir / "indicators.json", data_pool.get("indicators", {}))

        # Fundamentals group
        fundamentals = {
            "fundamentals": data_pool.get("fundamentals", ""),
            "balance_sheet": data_pool.get("balance_sheet", ""),
            "cashflow": data_pool.get("cashflow", ""),
            "income_statement": data_pool.get("income_statement", ""),
        }
        _write_json(dc_dir / "fundamentals.json", fundamentals)

        # News group
        news = {
            "news": data_pool.get("news", ""),
            "global_news": data_pool.get("global_news", ""),
            "insider_transactions": data_pool.get("insider_transactions", ""),
        }
        _write_json(dc_dir / "news.json", news)

        # Fund flow
        fund_flow = {
            "board": data_pool.get("fund_flow_board", ""),
            "individual": data_pool.get("fund_flow_individual", ""),
        }
        _write_json(dc_dir / "fund_flow.json", fund_flow)

        _write_json(dc_dir / "vpa.json", data_pool.get("vpa_indicators", ""))

        # Misc
        misc = {
            "lhb": data_pool.get("lhb", ""),
            "zt_pool": data_pool.get("zt_pool", ""),
            "hot_stocks": data_pool.get("hot_stocks", ""),
        }
        _write_json(dc_dir / "misc.json", misc)

    # ── 02_analysts ──────────────────────────────────────────

    def _write_analysts(self, root: Path, final_state: Dict[str, Any]) -> None:
        a_dir = root / "02_analysts"

        _write_md(a_dir / "market.md", final_state.get("market_report", ""))
        _write_md(a_dir / "sentiment.md", final_state.get("sentiment_report", ""))
        _write_md(a_dir / "news.md", final_state.get("news_report", ""))
        _write_md(a_dir / "fundamentals.md", final_state.get("fundamentals_report", ""))
        _write_md(a_dir / "macro.md", final_state.get("macro_report", ""))
        _write_md(a_dir / "smart_money.md", final_state.get("smart_money_report", ""))
        _write_md(a_dir / "volume_price.md", final_state.get("volume_price_report", ""))
        _write_json(a_dir / "traces.json", final_state.get("analyst_traces", []))

    # ── 03_debate ────────────────────────────────────────────

    def _write_debate(self, root: Path, final_state: Dict[str, Any]) -> None:
        d_dir = root / "03_debate"

        debate_state = final_state.get("investment_debate_state", {})
        if not debate_state:
            debate_state = {}

        # Bull arguments — combine initial, rebuttal, and history
        bull_parts = []
        for key in ("bull_initial", "bull_rebuttal", "bull_history"):
            val = debate_state.get(key, "")
            if val and val.strip():
                label = {"bull_initial": "初始观点", "bull_rebuttal": "反驳论据", "bull_history": "辩论历史"}
                bull_parts.append(f"## {label.get(key, key)}\n\n{val}")
        _write_md(d_dir / "bull.md", "\n\n---\n\n".join(bull_parts))

        # Bear arguments
        bear_parts = []
        for key in ("bear_initial", "bear_rebuttal", "bear_history"):
            val = debate_state.get(key, "")
            if val and val.strip():
                label = {"bear_initial": "初始观点", "bear_rebuttal": "反驳论据", "bear_history": "辩论历史"}
                bear_parts.append(f"## {label.get(key, key)}\n\n{val}")
        _write_md(d_dir / "bear.md", "\n\n---\n\n".join(bear_parts))

        _write_json(d_dir / "debate_state.json", debate_state)

    # ── 04_research_manager ──────────────────────────────────

    def _write_research_manager(self, root: Path, final_state: Dict[str, Any]) -> None:
        rm_dir = root / "04_research_manager"
        _write_md(rm_dir / "investment_plan.md", final_state.get("investment_plan", ""))

    # ── 05_trader ────────────────────────────────────────────

    def _write_trader(self, root: Path, final_state: Dict[str, Any]) -> None:
        t_dir = root / "05_trader"
        _write_md(t_dir / "trader_plan.md", final_state.get("trader_investment_plan", ""))

    # ── 06_risk_debate ───────────────────────────────────────

    def _write_risk_debate(self, root: Path, final_state: Dict[str, Any]) -> None:
        rd_dir = root / "06_risk_debate"

        risk_state = final_state.get("risk_debate_state", {})
        if not risk_state:
            risk_state = {}

        _write_md(rd_dir / "aggressive.md", risk_state.get("aggressive_history", ""))
        _write_md(rd_dir / "conservative.md", risk_state.get("conservative_history", ""))
        _write_md(rd_dir / "neutral.md", risk_state.get("neutral_history", ""))
        _write_json(rd_dir / "risk_debate_state.json", risk_state)

    # ── 07_risk_judge ────────────────────────────────────────

    def _write_risk_judge(self, root: Path, final_state: Dict[str, Any]) -> None:
        rj_dir = root / "07_risk_judge"

        _write_md(rj_dir / "final_decision.md", final_state.get("final_trade_decision", ""))

        risk_feedback = final_state.get("risk_feedback_state", {})
        _write_json(rj_dir / "risk_feedback.json", risk_feedback if risk_feedback else {})

    # ── summary.md ───────────────────────────────────────────

    def _write_summary(self, root: Path, final_state: Dict[str, Any]) -> None:
        decision = final_state.get("final_trade_decision", "")
        plan = final_state.get("investment_plan", "")
        trader_plan = final_state.get("trader_investment_plan", "")

        parts = []
        if decision:
            parts.append(f"## 最终交易决策\n\n{decision}")
        if plan:
            parts.append(f"## 投资计划\n\n{plan}")
        if trader_plan:
            parts.append(f"## 交易员方案\n\n{trader_plan}")

        # Analyst one-line verdicts from traces
        traces = final_state.get("analyst_traces", [])
        if traces:
            lines = ["## 分析师观点速览\n"]
            lines.append("| Agent | Verdict | Key Finding |")
            lines.append("|-------|---------|-------------|")
            for t in traces:
                lines.append(
                    f"| {t.get('agent', '')} | {t.get('verdict', '')} | {t.get('key_finding', '')[:60]} |"
                )
            parts.append("\n".join(lines))

        summary = "\n\n---\n\n".join(parts) if parts else "(无分析结果)"
        _write_md(root / "summary.md", summary)