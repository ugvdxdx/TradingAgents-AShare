"""TradingAgents CLI — typer-based command-line interface.

Usage:
    tradingagents analyze 600519.SH --date 2026-05-26
    tradingagents analyze 贵州茅台
    tradingagents watchlist list
    tradingagents watchlist add 600519.SH
    tradingagents scheduled list
    tradingagents scheduled add 600519.SH --time 20:00

Zero imports from api/ — all functionality uses tradingagents core + cli/local_store.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(
    name="tradingagents",
    help="TradingAgents — 量化多 Agent 分析框架",
    no_args_is_help=True,
)

watchlist_app = typer.Typer(name="watchlist", help="自选股管理")
scheduled_app = typer.Typer(name="scheduled", help="定时任务管理")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(scheduled_app, name="scheduled")


# ── Analyze command ─────────────────────────────────────────────


@app.command()
def analyze(
    symbol: str = typer.Argument(help="股票代码或名称，如 600519.SH 或 贵州茅台"),
    date: Optional[str] = typer.Option(None, "--date", "-d", help="交易日期 YYYY-MM-DD，默认今天"),
    horizon: str = typer.Option("short", "--horizon", "-H", help="分析周期: short/medium/long"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="自然语言查询"),
    quick: bool = typer.Option(False, "--quick", help="使用快速模型 (quick_think_llm)"),
):
    """对单只股票进行多 Agent 综合分析."""
    from tradingagents.stock_utils import normalize_symbol, search_cn_stock_by_name
    from tradingagents.dataflows.trade_calendar import cn_today_str

    resolved = _resolve_symbol(symbol, normalize_symbol, search_cn_stock_by_name)
    if resolved is None:
        typer.echo(f"无法识别股票: {symbol}", err=True)
        raise typer.Exit(1)

    trade_date = date or cn_today_str()

    typer.echo(f"分析 {resolved} | 日期 {trade_date} | 周期 {horizon}")

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = dict(DEFAULT_CONFIG)
    if quick:
        config["deep_think_llm"] = config.get("quick_think_llm", config["deep_think_llm"])

    ta = TradingAgentsGraph(config=config)

    asyncio.run(_run_analysis(ta, resolved, trade_date, horizon, query))


async def _run_analysis(
    ta,
    ticker: str,
    trade_date: str,
    horizon: str,
    query: Optional[str],
):
    """Stream analysis using TradingAgentsGraph's astream pattern."""
    import time
    from tradingagents.dataflows.interface import set_trace_collector

    # Set trace collector for this analysis
    trace_collector: list[str] = []
    set_trace_collector(trace_collector)

    # Data collection
    typer.echo(f"\n=== 数据采集: {ticker} {trade_date} ===", err=True)
    await asyncio.to_thread(ta.data_collector.collect, ticker, trade_date)
    typer.echo("=== 数据采集完成 ===\n", err=True)

    # Intent parsing
    from tradingagents.graph.intent_parser import parse_intent
    fallback_query = f"分析{ticker}{horizon}趋势"
    user_intent = await asyncio.to_thread(
        parse_intent,
        query or fallback_query,
        ta.quick_thinking_llm,
        fallback_ticker=ticker,
    )

    # Build initial state
    state = ta.propagator.create_initial_state(
        ticker, trade_date, user_intent=user_intent, horizon=horizon,
    )
    thread_config = {
        "configurable": {"thread_id": f"{ticker}_{trade_date}"},
        "recursion_limit": 100,
    }

    typer.echo("=== 14 个 Agent 开始协作分析 ===\n", err=True)

    start_time = time.time()

    # Stream node updates
    async for chunk in ta.graph.astream(state, config=thread_config, stream_mode="updates"):
        for node_name, node_state in chunk.items():
            if node_state is None:
                typer.echo(f"\n>>> {node_name} 完成", err=True)
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
                typer.echo(
                    f"\n>>> {node_name} 完成 | 已产出: {', '.join(filled)}",
                    err=True,
                )
            else:
                extras = []
                decision = node_state.get("final_trade_decision", "")
                plan = node_state.get("investment_plan", "")
                trader_plan = node_state.get("trader_investment_plan", "")
                if decision:
                    extras.append(f"决策: {decision[:200]}")
                if plan:
                    extras.append(f"投资计划: {plan[:100]}")
                if trader_plan:
                    extras.append(f"交易方案: {trader_plan[:100]}")

                debate_info = ""
                invest_debate = node_state.get("investment_debate_state", {})
                risk_debate = node_state.get("risk_debate_state", {})
                if invest_debate and invest_debate.get("judge_decision"):
                    debate_info = f" | 裁决: {invest_debate['judge_decision'][:80]}"
                if risk_debate and risk_debate.get("judge_decision"):
                    debate_info = f" | 裁决: {risk_debate['judge_decision'][:80]}"

                extra_str = " | " + "; ".join(extras) if extras else debate_info
                typer.echo(f"\n>>> {node_name} 完成{extra_str}", err=True)

    # Final state
    graph_state = ta.graph.get_state(thread_config)
    final = graph_state.values if graph_state else {}

    typer.echo(f"\n{'='*50}")
    typer.echo("=== 分析完成 ===")
    typer.echo(f"{'='*50}")

    _print_summary(final)

    # Archive report to file tree (before data eviction)
    duration = time.time() - start_time
    data_pool = ta.data_collector.get(ticker, trade_date)
    try:
        archive_path = ta.archive_report(
            ticker, trade_date, final,
            duration_seconds=duration,
            data_pool=data_pool,
        )
        typer.echo(f"\n研报存档: {archive_path}", err=True)
    except Exception as e:
        typer.echo(f"\n研报存档失败 (不影响分析结果): {e}", err=True)

    # Cleanup
    await asyncio.to_thread(ta.data_collector.evict, ticker, trade_date)


def _print_summary(final: dict) -> None:
    """Print the final analysis summary to stdout (not stderr)."""
    decision = final.get("final_trade_decision", "无")
    plan = final.get("investment_plan", "无")
    trader_plan = final.get("trader_investment_plan", "无")

    typer.echo(f"\n【最终交易决策】")
    typer.echo(decision)
    typer.echo(f"\n【投资计划】")
    typer.echo(plan)
    typer.echo(f"\n【交易员方案】")
    typer.echo(trader_plan)


# ── Watchlist commands ──────────────────────────────────────────

@watchlist_app.command("list")
def watchlist_list():
    """列出所有自选股."""
    from cli.local_store import list_watchlist

    items = list_watchlist()
    if not items:
        typer.echo("自选股为空")
        return
    for item in items:
        tag = " [定时]" if item.get("has_scheduled") else ""
        typer.echo(f"  {item['symbol']}{tag}  (id: {item['id']})")


@watchlist_app.command("add")
def watchlist_add(
    symbols: list[str] = typer.Argument(help="股票代码或名称"),
):
    """添加自选股."""
    from cli.local_store import add_watchlist_items
    from tradingagents.stock_utils import normalize_symbol, search_cn_stock_by_name

    resolved = [_resolve_symbol(s, normalize_symbol, search_cn_stock_by_name) or s for s in symbols]
    items = add_watchlist_items(resolved)
    added = [r for r in items if r["status"] == "added"]
    typer.echo(f"已添加 {len(added)} 只股票")


@watchlist_app.command("remove")
def watchlist_remove(
    item_id: str = typer.Argument(help="自选股 item ID"),
):
    """删除自选股."""
    from cli.local_store import delete_watchlist_item

    success = delete_watchlist_item(item_id)
    if success:
        typer.echo("已删除")
    else:
        typer.echo("未找到该条目", err=True)


# ── Scheduled commands ──────────────────────────────────────────

@scheduled_app.command("list")
def scheduled_list():
    """列出所有定时分析任务."""
    from cli.local_store import list_scheduled

    items = list_scheduled()
    if not items:
        typer.echo("定时任务为空")
        return
    for item in items:
        status = "活跃" if item.get("is_active") else "暂停"
        typer.echo(f"  {item['symbol']} | {item['horizon']} | {item['trigger_time']} | {status}  (id: {item['id']})")


@scheduled_app.command("add")
def scheduled_add(
    symbol: str = typer.Argument(help="股票代码或名称"),
    time: str = typer.Option("20:00", "--time", "-t", help="触发时间 HH:MM"),
    horizon: str = typer.Option("short", "--horizon", "-H", help="分析周期"),
):
    """添加定时分析任务."""
    from cli.local_store import create_scheduled
    from tradingagents.stock_utils import normalize_symbol, search_cn_stock_by_name

    resolved = _resolve_symbol(symbol, normalize_symbol, search_cn_stock_by_name) or symbol
    item = create_scheduled(resolved, horizon, time)
    typer.echo(f"已添加定时任务: {item['symbol']} | {time} | {horizon} (id: {item['id']})")


@scheduled_app.command("remove")
def scheduled_remove(
    item_id: str = typer.Argument(help="定时任务 ID"),
):
    """删除定时分析任务."""
    from cli.local_store import delete_scheduled

    success = delete_scheduled(item_id)
    if success:
        typer.echo("已删除")
    else:
        typer.echo("未找到该任务", err=True)


# ── Helper ──────────────────────────────────────────────────────


def _resolve_symbol(
    raw: str,
    normalize_fn,
    search_fn,
) -> Optional[str]:
    """Resolve stock name → code, or normalize 6-digit code.

    Args:
        raw: User input (code, name, or ticker).
        normalize_fn: tradingagents.stock_utils.normalize_symbol
        search_fn: tradingagents.stock_utils.search_cn_stock_by_name
    """
    normalized = normalize_fn(raw.strip())
    # If normalize_symbol couldn't resolve (returns raw unchanged for unknown),
    # try name-based search
    if normalized == raw.strip().upper() and not raw.strip().isdigit():
        result = search_fn(raw.strip())
        if result:
            return result
    return normalized if normalized else None


if __name__ == "__main__":
    app()