"""运行分析脚本 - 带流式输出 + 历史复盘"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import sys
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.agents.utils.history_reviewer import (
    HistoryReviewer, create_stock_review_context
)
from tradingagents.llm_clients import create_llm_client
from langchain_core.messages import HumanMessage, SystemMessage

async def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-05-26"
    query = sys.argv[3] if len(sys.argv) > 3 else f"分析{ticker}短线趋势"

    print(f"{'='*60}", flush=True)
    print(f"  多智能体分析启动 — {ticker}")
    print(f"  日期: {date}", flush=True)
    print(f"{'='*60}", flush=True)

    # ── 1. 加载历史复盘上下文 ──
    print(f"\n📜 加载历史分析记录...", flush=True)
    rev_ctx = create_stock_review_context(ticker)
    if rev_ctx:
        print(f"  ✓ 找到历史分析记录，已加载复盘上下文", flush=True)
        # Append history context to the query so it flows into the analysis
        query = f"{query}\n\n{rev_ctx}"
    else:
        print(f"  ℹ️ 未找到历史分析记录（首次分析）", flush=True)

    # ── 2. 初始化引擎 ──
    print(f"\n=== 初始化分析引擎 ===", flush=True)
    ta = TradingAgentsGraph()

    print(f"\n=== 数据采集: {ticker} {date} ===", flush=True)
    ta.data_collector.collect(ticker, date)
    print(f"=== 数据采集完成 ===\n", flush=True)

    from tradingagents.graph.intent_parser import parse_intent
    user_intent = parse_intent(query, ta.quick_thinking_llm, fallback_ticker=ticker)

    state = ta.propagator.create_initial_state(
        ticker, date, user_intent=user_intent, horizon="short"
    )

    config = {
        "configurable": {"thread_id": f"{ticker}_{date}"},
        "recursion_limit": 100,
    }

    print(f"=== 14 个 Agent 开始协作分析 ===\n", flush=True)

    # ── 3. 运行多智能体分析 ──
    async for chunk in ta.graph.astream(state, config=config, stream_mode="updates"):
        for node_name, node_state in chunk.items():
            if node_state is None:
                print(f"\n>>> {node_name} 完成 ✓", flush=True)
                continue
            reports = {
                "Market Analyst": node_state.get("market_report", ""),
                "Fundamentals Analyst": node_state.get("fundamentals_report", ""),
                "Social Media Analyst": node_state.get("sentiment_report", ""),
                "News Analyst": node_state.get("news_report", ""),
                "Macro Analyst": node_state.get("macro_report", ""),
                "Smart Money Analyst": node_state.get("smart_money_report", ""),
                "Volume Price Analyst": node_state.get("volume_price_report", ""),
            }

            if any(reports.values()):
                filled = [k for k, v in reports.items() if v]
                if filled:
                    print(f"\n>>> {node_name} 完成 ✓ 已产出: {', '.join(filled)}", flush=True)
                else:
                    print(f"\n>>> {node_name} 完成 ✓", flush=True)
            else:
                debate_info = ""
                invest_debate = node_state.get("investment_debate_state", {})
                risk_debate = node_state.get("risk_debate_state", {})
                if invest_debate and invest_debate.get("judge_decision"):
                    debate_info = f" | 裁决: {invest_debate['judge_decision'][:80]}"
                if risk_debate and risk_debate.get("judge_decision"):
                    debate_info = f" | 裁决: {risk_debate['judge_decision'][:80]}"

                decision = node_state.get("final_trade_decision", "")
                plan = node_state.get("investment_plan", "")
                trader_plan = node_state.get("trader_investment_plan", "")

                extras = []
                if decision:
                    extras.append(f"决策: {decision[:200]}")
                if plan:
                    extras.append(f"投资计划: {plan[:100]}")
                if trader_plan:
                    extras.append(f"交易方案: {trader_plan[:100]}")

                extra_str = " | " + "; ".join(extras) if extras else debate_info
                print(f"\n>>> {node_name} 完成 ✓{extra_str}", flush=True)

            sys.stdout.flush()

    # ── 4. 提取最终结果 ──
    print(f"\n\n{'='*50}", flush=True)
    print(f"=== 分析完成 ===", flush=True)
    print(f"{'='*50}", flush=True)

    graph_state = ta.graph.get_state(config)
    final = graph_state.values if graph_state else {}

    decision_raw = final.get("final_trade_decision", "无")
    processed = ta.process_signal(decision_raw)

    action_map = {"BUY": "🟢 建议买入/建仓", "SELL": "🔴 建议卖出/回避", "HOLD": "⚖️ 建议观望持有"}
    action = action_map.get(processed, f"({processed})")

    print(f"\n【最终交易决策】", flush=True)
    print(f"决策信号: {processed} {action}", flush=True)
    print(f"{'─'*40}", flush=True)
    print(decision_raw[:500], flush=True)
    if len(decision_raw) > 500:
        print(f"... (共 {len(decision_raw)} 字符)", flush=True)

    print(f"\n【投资计划】", flush=True)
    plan = final.get("investment_plan", "无")
    print(plan[:500], flush=True)
    if len(plan) > 500:
        print(f"... (共 {len(plan)} 字符)", flush=True)

    print(f"\n【交易员方案】", flush=True)
    trader_plan = final.get("trader_investment_plan", "无")
    print(trader_plan[:500], flush=True)
    if len(trader_plan) > 500:
        print(f"... (共 {len(trader_plan)} 字符)", flush=True)

    print(f"\n{'='*50}", flush=True)
    print(f"分析结果已自动归档至 results/{ticker}/{date}/", flush=True)
    print(f"{'='*50}", flush=True)

asyncio.run(main())