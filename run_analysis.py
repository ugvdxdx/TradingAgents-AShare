"""运行分析脚本 - 带流式输出"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import sys
from tradingagents.graph.trading_graph import TradingAgentsGraph

async def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-05-26"
    query = sys.argv[3] if len(sys.argv) > 3 else f"分析{ticker}短线趋势"

    print(f"=== 初始化分析引擎 ===", flush=True)
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

    final_state = None
    async for chunk in ta.graph.astream(state, config=config, stream_mode="updates"):
        for node_name, node_state in chunk.items():
            if node_state is None:
                print(f"\n>>> {node_name} 完成 ✓", flush=True)
                continue
            # 提取关键信息
            reports = {
                "Market Analyst": node_state.get("market_report", ""),
                "Fundamentals Analyst": node_state.get("fundamentals_report", ""),
                "Social Media Analyst": node_state.get("sentiment_report", ""),
                "News Analyst": node_state.get("news_report", ""),
                "Macro Analyst": node_state.get("macro_report", ""),
                "Smart Money Analyst": node_state.get("smart_money_report", ""),
                "Volume Price Analyst": node_state.get("volume_price_report", ""),
            }

            # 判断节点类型
            if any(reports.values()):
                filled = [k for k, v in reports.items() if v]
                if filled:
                    print(f"\n>>> {node_name} 完成 ✓ 已产出: {', '.join(filled)}", flush=True)
                else:
                    print(f"\n>>> {node_name} 完成 ✓", flush=True)
            else:
                # 辩论/决策节点
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

    # 从最后一次输出提取最终状态
    print(f"\n\n{'='*50}", flush=True)
    print(f"=== 分析完成 ===", flush=True)
    print(f"{'='*50}", flush=True)

    # 重新获取最终状态
    graph_state = ta.graph.get_state(config)
    final = graph_state.values if graph_state else {}

    print(f"\n【最终交易决策】", flush=True)
    print(final.get("final_trade_decision", "无"), flush=True)

    print(f"\n【投资计划】", flush=True)
    print(final.get("investment_plan", "无"), flush=True)

    print(f"\n【交易员方案】", flush=True)
    print(final.get("trader_investment_plan", "无"), flush=True)

    ta.data_collector.evict(ticker, date)

asyncio.run(main())
