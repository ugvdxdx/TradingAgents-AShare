"""Multi-agent sector analysis graph — 8-stage / 9-agent workflow with debate + archiving."""

import asyncio
import json
import os
import time
import textwrap
from datetime import date as _date
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from tradingagents.dataflows.providers.astock_provider import AstockProvider
from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
from tradingagents.llm_clients import create_llm_client

from .prompts import SECTOR_PROMPTS

# ═══════════════════════════════════════════════════════════════
# SectorState
# ═══════════════════════════════════════════════════════════════

class SectorDebateState(TypedDict, total=False):
    round: int
    max_rounds: int
    bull_history: List[str]
    bear_history: List[str]
    consensus: str
    finished: bool


class SectorState(TypedDict, total=False):
    keyword: str
    trade_date: str
    sector_data: str
    policy_analyst_report: str
    fund_analyst_report: str
    sentiment_analyst_report: str
    debate_state: SectorDebateState
    bull_report: str
    bear_report: str
    research_manager_report: str
    risk_judge_report: str
    final_verdict: Dict[str, Any]


# ═══════════════════════════════════════════════════════════════
# SectorAnalysisGraph
# ═══════════════════════════════════════════════════════════════

class SectorAnalysisGraph:
    """8-stage / 9-agent sector analysis workflow.

    Stages:
        1. collect_data   — sector search + ranking + fund flow + hot stocks
        2. policy_analyst — policy/strategy analysis
        3. fund_analyst   — capital flow & chip structure
        4. sentiment_analyst — market sentiment & heat
        5. bull_researcher — construct bullish argument
        6. bear_researcher — construct bearish argument
        7. research_manager — synthesize debate → research report
        8. risk_judge     — risk adjudication → final verdict
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.deep_llm = None
        self.quick_llm = None
        self._init_llms()
        self.graph = self._build_graph()
        self.astock = AstockProvider()
        self.akshare = CnAkshareProvider()

    def _init_llms(self):
        backend = self.config.get("backend_url") or os.getenv("TA_BASE_URL", "https://api.openai.com/v1")
        deep_model = self.config.get("deep_think_llm") or os.getenv("TA_LLM_DEEP", "gpt-4o")
        quick_model = self.config.get("quick_think_llm") or os.getenv("TA_LLM_QUICK", "gpt-4o-mini")
        api_key = self.config.get("api_key") or os.getenv("TA_API_KEY", "")

        print(f"  [LLM] Deep model: {deep_model}")
        print(f"  [LLM] Quick model: {quick_model}")
        print(f"  [LLM] Backend: {backend}")

        provider = self.config.get("llm_provider", "openai")

        deep_client = create_llm_client(
            provider=provider,
            model=deep_model,
            base_url=backend,
            api_key=api_key,
            temperature=0.7,
        )
        quick_client = create_llm_client(
            provider=provider,
            model=quick_model,
            base_url=backend,
            api_key=api_key,
            temperature=0.5,
        )
        self.deep_llm = deep_client.get_llm()
        self.quick_llm = quick_client.get_llm()

    # ── LLM Streaming Helper with Retry ──

    def _stream_llm(self, llm, system_msg: str, human_msg: str, max_retries: int = 3, max_chars: int = None) -> str:
        messages = [
            SystemMessage(content=system_msg),
            HumanMessage(content=human_msg),
        ]
        for attempt in range(max_retries):
            try:
                full_content = ""
                for chunk in llm.stream(messages):
                    content = chunk.content if hasattr(chunk, "content") else str(chunk)
                    full_content += content
                    # Early stop if max_chars exceeded
                    if max_chars and len(full_content) >= max_chars:
                        full_content = full_content[:max_chars]
                        break
                return full_content
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt * 5
                    print(f"  [LLM] 调用失败 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")
                    print(f"  [LLM] 等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"  [LLM] 调用失败 (已达最大重试次数 {max_retries}): {type(e).__name__}: {e}")
                    raise

    def _validate_time_dimension(self, content: str) -> bool:
        """Check if content contains day/week/month predictions."""
        time_indicators = ['天', '周', '月', '短期', '中期', '长期']
        found = sum(1 for indicator in time_indicators if indicator in content)
        return found >= 3

    # ── Stage 1: Data Collection ──

    def _collect_data(self, state: SectorState) -> SectorState:
        keyword = state["keyword"]
        trade_date = state["trade_date"]
        print(f"\n{'='*60}")
        print(f"📡 [阶段 1/8] 数据采集 — 关键词: {keyword}")
        print(f"{'='*60}")

        parts = []

        # 1a. Search concept boards
        print(f"\n  ▶ [1a] 搜索概念板块: {keyword}")
        try:
            search_result = self.astock.search_concept_board(keyword)
            if "暂不可用" in search_result or search_result.strip() == "":
                search_result = self.akshare.search_concept_board(keyword)
            print(f"  ✓ 板块搜索完成")
            parts.append(f"【板块搜索结果】\n{search_result}")
        except Exception as e:
            print(f"  ⚠ 板块搜索失败: {e}")
            parts.append(f"【板块搜索结果】搜索失败: {e}")

        # 1b. Concept board ranking
        print(f"  ▶ [1b] 获取概念板块排名")
        try:
            ranking = self.astock.get_concept_boards(top_n=10)
            if "暂不可用" in ranking or ranking.strip() == "":
                ranking = self.akshare.get_concept_boards(top_n=10)
            print(f"  ✓ 板块排名获取完成")
            parts.append(f"【概念板块排名 TOP10】\n{ranking}")
        except Exception as e:
            print(f"  ⚠ 板块排名获取失败: {e}")

        # 1c. Board fund flow
        print(f"  ▶ [1c] 获取板块资金流")
        try:
            fund_flow = self.astock.get_board_fund_flow()
            if "暂不可用" in fund_flow or fund_flow.strip() == "":
                fund_flow = self.akshare.get_board_fund_flow()
            print(f"  ✓ 板块资金流获取完成")
            parts.append(f"【板块资金流】\n{fund_flow}")
        except Exception as e:
            print(f"  ⚠ 板块资金流获取失败: {e}")

        # 1d. Global news
        print(f"  ▶ [1d] 获取宏观新闻")
        try:
            news = self.astock.get_global_news()
            if "暂不可用" in news or news.strip() == "":
                news = self.akshare.get_global_news()
            print(f"  ✓ 宏观新闻获取完成")
            parts.append(f"【宏观新闻】\n{news}")
        except Exception as e:
            print(f"  ⚠ 宏观新闻获取失败: {e}")

        # 1e. Hot stocks
        print(f"  ▶ [1e] 获取热门个股")
        try:
            hot = self.astock.get_hot_stocks_xq()
            if "暂不可用" in hot or hot.strip() == "":
                hot = self.akshare.get_hot_stocks_xq()
            print(f"  ✓ 热门个股获取完成")
            parts.append(f"【热门个股】\n{hot}")
        except Exception as e:
            print(f"  ⚠ 热门个股获取失败: {e}")

        # 1f. Keyword news
        print(f"  ▶ [1f] 获取行业新闻: {keyword}")
        try:
            kw_news = self.astock.get_news(f"{keyword} 板块")
            if "暂不可用" in kw_news or kw_news.strip() == "":
                kw_news = self.akshare.get_news(f"{keyword} 板块")
            print(f"  ✓ 行业新闻获取完成")
            parts.append(f"【{keyword}板块新闻】\n{kw_news}")
        except Exception as e:
            print(f"  ⚠ 行业新闻获取失败: {e}")

        combined = "\n\n".join(parts)
        print(f"\n  ✓ 数据采集完成，共 {len(parts)} 个数据源")
        return {"sector_data": combined}

    # ── Stage 2: Policy Analyst ──

    def _policy_analyst(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"🔬 [阶段 2/8] 政策分析师 — {state['keyword']} 产业政策与战略分析")
        print(f"{'='*60}")

        prompt = SECTOR_PROMPTS["policy_analyst_system"]
        human = f"""请对以下板块进行政策维度的深度分析。

板块关键词：{state['keyword']}
交易日：{state['trade_date']}

板块数据：
{state['sector_data'][:3000]}

请输出完整的政策分析报告（500字以内，必须包含天/周/月三级走势预测）。"""
        print(f"  ▶ LLM 推理中（政策分析），请稍候...")
        result = self._stream_llm(self.deep_llm, prompt, human, max_chars=600)
        print(f"\n  ✓ 政策分析师报告完成 ({len(result)} 字符)")
        print(f"\n{'─'*50}")
        print(f"【政策分析师报告】")
        print(f"{'─'*50}")
        print(result)
        print(f"{'─'*50}\n")
        return {"policy_analyst_report": result}

    # ── Stage 3: Fund Analyst ──

    def _fund_analyst(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"💰 [阶段 3/8] 资金分析师 — {state['keyword']} 资金流动与筹码分析")
        print(f"{'='*60}")

        prompt = SECTOR_PROMPTS["fund_analyst_system"]
        human = f"""请对以下板块进行资金维度的深度分析。

板块关键词：{state['keyword']}
交易日：{state['trade_date']}

板块数据：
{state['sector_data'][:3000]}

请输出完整的资金分析报告（500字以内，必须包含天/周/月三级走势预测）。"""
        print(f"  ▶ LLM 推理中（资金分析），请稍候...")
        result = self._stream_llm(self.deep_llm, prompt, human, max_chars=600)
        print(f"\n  ✓ 资金分析师报告完成 ({len(result)} 字符)")
        print(f"\n{'─'*50}")
        print(f"【资金分析师报告】")
        print(f"{'─'*50}")
        print(result)
        print(f"{'─'*50}\n")
        return {"fund_analyst_report": result}

    # ── Stage 4: Sentiment Analyst ──

    def _sentiment_analyst(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"🔥 [阶段 4/8] 情绪分析师 — {state['keyword']} 市场情绪与热度分析")
        print(f"{'='*60}")

        prompt = SECTOR_PROMPTS["sentiment_analyst_system"]
        human = f"""请对以下板块进行情绪维度的深度分析。

板块关键词：{state['keyword']}
交易日：{state['trade_date']}

板块数据：
{state['sector_data'][:3000]}

请输出完整的情绪分析报告（500字以内，必须包含天/周/月三级走势预测）。"""
        print(f"  ▶ LLM 推理中（情绪分析），请稍候...")
        result = self._stream_llm(self.deep_llm, prompt, human, max_chars=600)
        print(f"\n  ✓ 情绪分析师报告完成 ({len(result)} 字符)")
        print(f"\n{'─'*50}")
        print(f"【情绪分析师报告】")
        print(f"{'─'*50}")
        print(result)
        print(f"{'─'*50}\n")
        return {"sentiment_analyst_report": result}

    # ── Stage 5: Bull Researcher (多头) ──

    def _bull_researcher(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"🟢 [阶段 5/8] 多头研究员 — 构建{state['keyword']}看多论证")
        print(f"{'='*60}")

        # Check debate state for continuing debate
        debate = state.get("debate_state", {})
        round_num = debate.get("round", 1)
        max_rounds = debate.get("max_rounds", 3)
        bear_history = debate.get("bear_history", [])

        print(f"  ▶ 辩论第 {round_num}/{max_rounds} 轮")

        if round_num == 1:
            human = f"""请为 {state['keyword']} 板块构建看多论证。

板块关键词：{state['keyword']}
交易日：{state['trade_date']}

政策分析师报告：
{state.get('policy_analyst_report', '无')}

资金分析师报告：
{state.get('fund_analyst_report', '无')}

情绪分析师报告：
{state.get('sentiment_analyst_report', '无')}

要求：700字以内，必须包含天/周/月三级目标及依据，提出新角度而非重复已有论点。"""
        else:
            bear_arg = bear_history[-1] if bear_history else "无空方论证"
            human = f"""这是辩论第 {round_num} 轮。

空头研究员的最新论证：
{bear_arg[:500]}

请针对上述空方观点进行反驳，并提出新角度强化看多论证。

要求：700字以内，必须包含天/周/月三级目标及依据，避免围绕单点反复拉锯。"""

        print(f"  ▶ LLM 推理中（构建看多论证），请稍候...")
        result = self._stream_llm(self.deep_llm, SECTOR_PROMPTS["sector_bull_system"], human, max_chars=800)
        print(f"\n  ✓ 多头研究员论证完成 ({len(result)} 字符)")
        print(f"\n{'─'*50}")
        print(f"【多头研究员报告 — 第 {round_num} 轮】")
        print(f"{'─'*50}")
        print(result)
        print(f"{'─'*50}\n")

        # Update debate state
        bull_history = debate.get("bull_history", [])
        bull_history.append(result)

        return {
            "bull_report": result,
            "debate_state": {
                "round": round_num,
                "max_rounds": max_rounds,
                "bull_history": bull_history,
                "bear_history": bear_history,
                "finished": round_num >= max_rounds,
            },
        }

    # ── Stage 6: Bear Researcher (空头) ──

    def _bear_researcher(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"🔴 [阶段 6/8] 空头研究员 — 构建{state['keyword']}看空论证")
        print(f"{'='*60}")

        debate = state.get("debate_state", {})
        round_num = debate.get("round", 1)
        max_rounds = debate.get("max_rounds", 3)
        bull_history = debate.get("bull_history", [])

        bull_arg = bull_history[-1] if bull_history else "无多方论证"

        human = f"""这是辩论第 {round_num} 轮。

多头研究员的最新论证：
{bull_arg[:500]}

请针对上述多方观点进行反驳，构建你的看空论证。

要求：700字以内，必须包含天/周/月三级目标及依据，提出新角度而非重复已有论点，避免围绕单点反复拉锯。"""

        print(f"  ▶ LLM 推理中（构建看空论证），请稍候...")
        result = self._stream_llm(self.deep_llm, SECTOR_PROMPTS["sector_bear_system"], human, max_chars=800)
        print(f"\n  ✓ 空头研究员论证完成 ({len(result)} 字符)")
        print(f"\n{'─'*50}")
        print(f"【空头研究员报告 — 第 {round_num} 轮】")
        print(f"{'─'*50}")
        print(result)
        print(f"{'─'*50}\n")

        bear_history = debate.get("bear_history", [])
        bear_history.append(result)
        next_round = round_num + 1

        return {
            "bear_report": result,
            "debate_state": {
                "round": next_round,
                "max_rounds": max_rounds,
                "bull_history": bull_history,
                "bear_history": bear_history,
                "finished": next_round > max_rounds,
            },
        }

    # ── Debate continuation logic ──

    def _check_convinced(self, state: SectorState, side: str) -> bool:
        """Check if a side has been convinced based on their latest argument."""
        debate = state.get("debate_state", {})
        history_key = "bull_history" if side == "bull" else "bear_history"
        history = debate.get(history_key, [])
        
        if len(history) >= 2:
            latest = history[-1]
            convinced_keywords = [
                "承认", "认同", "接受", "赞同", "同意", "说服",
                "我承认", "我认同", "我接受", "我赞同", "我同意",
                "被说服", "观点正确", "有道理", "确实如此",
                "无法反驳", "难以反驳", "无力反驳"
            ]
            for keyword in convinced_keywords:
                if keyword in latest:
                    print(f"  ✅ 检测到{side}方被说服: {keyword}")
                    return True
        return False

    def _should_continue_debate(self, state: SectorState) -> str:
        debate = state.get("debate_state", {})
        finished = debate.get("finished", False)
        
        # Check if bear was convinced
        if self._check_convinced(state, "bear"):
            print(f"\n  ✓ 空头被说服，辩论提前结束，共 {debate.get('round', 0)} 轮")
            return "research_manager"
            
        if finished:
            print(f"\n  ✓ 多空辩论完成，共 {debate.get('round', 0)} 轮")
            return "research_manager"
        else:
            print(f"\n  🔄 进入下一轮辩论 (第 {debate.get('round', 1)} 轮)")
            return "continue_debate"

    def _should_continue_debate_from_bear(self, state: SectorState) -> str:
        """Conditional edge from bear_researcher: continue to bull or finish."""
        debate = state.get("debate_state", {})
        finished = debate.get("finished", False)
        
        # Check if bull was convinced
        if self._check_convinced(state, "bull"):
            print(f"\n  ✓ 多头被说服，辩论提前结束，共 {debate.get('round', 0)} 轮")
            return "research_manager"
            
        if finished:
            print(f"\n  ✓ 多空辩论完成，共 {debate.get('round', 0)} 轮")
            return "research_manager"
        else:
            print(f"\n  🔄 进入下一轮辩论 (第 {debate.get('round', 1)} 轮)")
            return "bull_researcher"

    # ── Stage 7: Research Manager ──

    def _research_manager(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"📊 [阶段 7/8] 研究经理 — 综合研判与投资报告")
        print(f"{'='*60}")
        print(f"  ▶ 输入: 3份分析师报告 + {len(state.get('debate_state',{}).get('bull_history',[]))}轮多空辩论")
        print(f"{'='*60}\n")

        prompt = SECTOR_PROMPTS["research_manager_system"]
        debate = state.get("debate_state", {})
        bull_history = debate.get("bull_history", [])
        bear_history = debate.get("bear_history", [])

        debate_summary = ""
        for i, (bull, bear) in enumerate(zip(bull_history, bear_history)):
            debate_summary += f"\n--- 辩论第 {i+1} 轮 ---\n"
            debate_summary += f"多方核心: {bull[:500]}\n"
            debate_summary += f"空方核心: {bear[:500]}\n"

        human = f"""请综合以下所有分析，形成最终投资研判。

板块：{state['keyword']}
日期：{state['trade_date']}

政策分析师报告：
{state.get('policy_analyst_report', '无')}

资金分析师报告：
{state.get('fund_analyst_report', '无')}

情绪分析师报告：
{state.get('sentiment_analyst_report', '无')}

多空辩论记录：
{debate_summary}

要求：1000字以内，言之有物，必须包含天/周/月三级走势预测及具体依据，所有判断要有数据或逻辑支撑。"""

        print(f"  ▶ LLM 推理中（综合研判），请稍候...")
        result = self._stream_llm(self.deep_llm, prompt, human, max_chars=1100)
        print(f"\n  ✓ 研究经理报告完成 ({len(result)} 字符)")
        print(f"\n{'━'*60}")
        print(f"【研究经理综合研判报告】")
        print(f"{'━'*60}")
        print(result)
        print(f"{'━'*60}\n")
        return {"research_manager_report": result}

    # ── Stage 8: Risk Judge ──

    def _risk_judge(self, state: SectorState) -> SectorState:
        print(f"\n{'='*60}")
        print(f"⚖️  [阶段 8/8] 风险裁判 — 最终裁决")
        print(f"{'='*60}")
        print(f"  ▶ 审查研究经理报告 + 分析师原始报告 + 多空辩论记录")
        print(f"{'='*60}\n")

        prompt = SECTOR_PROMPTS["risk_judge_system"]
        debate = state.get("debate_state", {})

        human = f"""请对以下分析进行风险审查并做出最终裁决。

板块：{state['keyword']}
日期：{state['trade_date']}

研究经理报告：
{state.get('research_manager_report', '无')}

多空辩论轮次：{debate.get('round', 0)} 轮

要求：800字以内，必须包含天/周/月三级走势预测及依据，所有判断要有坚实依据。"""

        print(f"  ▶ LLM 推理中（风险裁决），请稍候...")
        result = self._stream_llm(self.deep_llm, prompt, human, max_chars=900)
        print(f"\n  ✓ 风险裁判裁决完成 ({len(result)} 字符)")
        print(f"\n{'━'*60}")
        print(f"【风险裁判最终裁决】")
        print(f"{'━'*60}")
        print(result)

        # Parse final verdict
        print(f"\n  ▶ 解析最终裁决...")
        verdict = self._parse_verdict(result)
        print(f"{'━'*60}")
        print(f"  方向: {verdict.get('direction', 'N/A')}")
        print(f"  置信度: {verdict.get('confidence', 'N/A')}%")
        print(f"  短期(天): {verdict.get('short_term', 'N/A')}")
        print(f"  中期(周): {verdict.get('mid_term', 'N/A')}")
        print(f"  长期(月): {verdict.get('long_term', 'N/A')}")
        print(f"  仓位建议: {verdict.get('position', 'N/A')}")
        print(f"  核心结论: {verdict.get('reason', 'N/A')}")
        print(f"  核心风险: {verdict.get('key_risk', 'N/A')}")
        print(f"{'━'*60}\n")

        return {"risk_judge_report": result, "final_verdict": verdict}

    def _parse_verdict(self, text: str) -> Dict[str, str]:
        """Parse final verdict from risk judge output."""
        text_lower = text.lower()
        verdict: Dict[str, str] = {}

        # Direction (A股语境：看多=建议买入/建仓，看空=建议回避/观望)
        if any(w in text for w in ["看多", "偏多", "做多", "乐观"]):
            verdict["direction"] = "看多"
        elif any(w in text for w in ["看空", "偏空", "做空", "悲观"]):
            verdict["direction"] = "看空（回避/观望）"
        else:
            verdict["direction"] = "中性（观望/持有）"

        # Confidence (look for number near % or 置信度)
        import re
        conf_match = re.search(r'(\d+)\s*%', text)
        if conf_match:
            verdict["confidence"] = conf_match.group(1)
        else:
            verdict["confidence"] = "50"

        # Short/Medium/Long term
        for term, label in [("短期", "short_term"), ("近日", "short_term"), ("天", "short_term"),
                             ("中期", "mid_term"), ("近周", "mid_term"), ("周", "mid_term"),
                             ("长期", "long_term"), ("月", "long_term")]:
            # Find sentences containing the term indicator
            for line in text.split("\n"):
                if term in line and len(line) < 200:
                    verdict[label] = line.strip()
                    break

        # Position
        if "重仓" in text:
            verdict["position"] = "重仓"
        elif "半仓" in text or "中等" in text:
            verdict["position"] = "半仓"
        elif "轻仓" in text:
            verdict["position"] = "轻仓"
        elif "空仓" in text or "观望" in text:
            verdict["position"] = "空仓/观望"
        else:
            verdict["position"] = "轻仓"

        # Core conclusion — first line after key indicator
        for i, line in enumerate(text.split("\n")):
            if "核心结论" in line and i + 1 < len(text.split("\n")):
                verdict["reason"] = text.split("\n")[i + 1].strip()
                break
        if "reason" not in verdict:
            verdict["reason"] = text[:200]

        # Core risk
        for i, line in enumerate(text.split("\n")):
            if "核心风险" in line and i + 1 < len(text.split("\n")):
                verdict["key_risk"] = text.split("\n")[i + 1].strip()
                break
        if "key_risk" not in verdict:
            verdict["key_risk"] = "请参阅完整报告"

        return verdict

    # ── Graph Builder ──

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(SectorState)

        graph.add_node("collect_data", self._collect_data)
        graph.add_node("policy_analyst", self._policy_analyst)
        graph.add_node("fund_analyst", self._fund_analyst)
        graph.add_node("sentiment_analyst", self._sentiment_analyst)
        graph.add_node("bull_researcher", self._bull_researcher)
        graph.add_node("bear_researcher", self._bear_researcher)
        graph.add_node("research_manager", self._research_manager)
        graph.add_node("risk_judge", self._risk_judge)

        graph.set_entry_point("collect_data")

        # Data → 3 parallel analysts
        graph.add_edge("collect_data", "policy_analyst")
        graph.add_edge("collect_data", "fund_analyst")
        graph.add_edge("collect_data", "sentiment_analyst")

        # All 3 analysts → bull researcher (sequential debate start)
        graph.add_edge("policy_analyst", "bull_researcher")
        graph.add_edge("fund_analyst", "bull_researcher")
        graph.add_edge("sentiment_analyst", "bull_researcher")

        # Debate loop: bull → bear → (bull or manager)
        graph.add_conditional_edges(
            "bull_researcher",
            self._should_continue_debate,
            {"continue_debate": "bear_researcher", "research_manager": "research_manager"},
        )
        graph.add_conditional_edges(
            "bear_researcher",
            self._should_continue_debate_from_bear,
            {"bull_researcher": "bull_researcher", "research_manager": "research_manager"},
        )

        # Manager → Risk Judge → END
        graph.add_edge("research_manager", "risk_judge")
        graph.add_edge("risk_judge", END)

        return graph.compile()

    # ── Run ──

    async def run(self, keyword: str, trade_date: str = None, review_context: str = "") -> Dict[str, Any]:
        if trade_date is None:
            trade_date = _date.today().strftime("%Y-%m-%d")

        print(f"{'='*60}")
        print(f"  板块多智能体深度分析启动")
        print(f"  关键词: {keyword}")
        print(f"  日期: {trade_date}")
        if review_context:
            print(f"  📜 已加载历史复盘上下文")
        print(f"  架构: 8阶段 / 9智能体 (3分析师 + 2辩论 + 1经理 + 1风险官)")
        print(f"{'='*60}\n")

        init_state: SectorState = {
            "keyword": keyword,
            "trade_date": trade_date,
            "sector_data": review_context,  # inject review context into sector_data
            "debate_state": {
                "round": 1,
                "max_rounds": 3,
                "bull_history": [],
                "bear_history": [],
                "finished": False,
            },
        }

        print(f"  ▶ 图执行开始...\n")
        result = self.graph.invoke(init_state)
        print(f"\n  ▶ 图执行完成")

        # ── Archive Report ──
        self._archive_report(keyword, trade_date, result)

        return result

    # ── Report Archiving ──

    def _archive_report(self, keyword: str, trade_date: str, result: Dict[str, Any]):
        safe_name = keyword.replace(" ", "_").replace("/", "_")
        archive_dir = f"results/{safe_name}/{trade_date}"
        os.makedirs(archive_dir, exist_ok=True)
        print(f"\n{'='*60}")
        print(f"📁 研报归档 — {archive_dir}")
        print(f"{'='*60}")

        sections = [
            ("sector_data.txt", "板块原始数据", result.get("sector_data", "")),
            ("policy_analyst.txt", "政策分析报告", result.get("policy_analyst_report", "")),
            ("fund_analyst.txt", "资金分析报告", result.get("fund_analyst_report", "")),
            ("sentiment_analyst.txt", "情绪分析报告", result.get("sentiment_analyst_report", "")),
            ("bull_researcher.txt", "多头论证报告", result.get("bull_report", "")),
            ("bear_researcher.txt", "空头论证报告", result.get("bear_report", "")),
            ("research_manager.txt", "研究经理综合报告", result.get("research_manager_report", "")),
            ("risk_judge.txt", "风险裁判裁决报告", result.get("risk_judge_report", "")),
        ]

        for filename, label, content in sections:
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"【{label}】\n")
                f.write(f"板块: {keyword}\n日期: {trade_date}\n\n")
                f.write(content)
            size = len(content)
            print(f"  ✓ {filename} ({size} 字符)")

        # Write summary JSON
        verdict = result.get("final_verdict", {})
        summary = {
            "keyword": keyword,
            "trade_date": trade_date,
            "direction": verdict.get("direction", ""),
            "confidence": verdict.get("confidence", ""),
            "short_term": verdict.get("short_term", ""),
            "mid_term": verdict.get("mid_term", ""),
            "long_term": verdict.get("long_term", ""),
            "position": verdict.get("position", ""),
            "reason": verdict.get("reason", ""),
            "key_risk": verdict.get("key_risk", ""),
        }
        summary_path = os.path.join(archive_dir, "summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"  ✓ summary.json (研报摘要)")

        print(f"\n  📍 归档路径: {os.path.abspath(archive_dir)}")
        print(f"{'='*60}\n")