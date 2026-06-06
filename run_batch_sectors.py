#!/usr/bin/env python3
"""批量板块深度分析脚本 — 含历史复盘"""
import asyncio
import sys
import os
from datetime import date as _date

from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

sys.path.insert(0, '/Users/bilibili/Desktop/J-TradingAgents')

from tradingagents.agents.sector.sector_graph import SectorAnalysisGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.history_reviewer import create_sector_review_context

SECTORS = ["商业航天", "半导体", "军工"]

async def analyze_sector(graph, keyword: str, trade_date: str):
    print(f"\n{'#'*60}")
    print(f"# 开始分析: {keyword}")
    print(f"{'#'*60}\n")

    # 加载历史复盘上下文
    rev_ctx = create_sector_review_context(keyword)
    if rev_ctx:
        print(f"📜 找到历史分析记录，已加载复盘上下文")
    else:
        print(f"ℹ️ 未找到历史分析记录（首次分析）")

    try:
        result = await graph.run(keyword, trade_date, review_context=rev_ctx)
        verdict = result.get("final_verdict", {})
        print(f"\n{'='*60}")
        print(f"  {keyword} 分析完成")
        print(f"{'='*60}")
        direction = verdict.get('direction', 'N/A')
        print(f"  方向: {direction}")
        print(f"  置信度: {verdict.get('confidence', 'N/A')}%")
        print(f"  短期(天): {verdict.get('short_term', 'N/A')}")
        print(f"  中期(周): {verdict.get('mid_term', 'N/A')}")
        print(f"  长期(月): {verdict.get('long_term', 'N/A')}")
        print(f"  仓位建议: {verdict.get('position', 'N/A')}")
        print(f"  核心结论: {verdict.get('reason', 'N/A')}")
        print(f"  核心风险: {verdict.get('key_risk', 'N/A')}")
        print(f"{'='*60}\n")

        # 输出历史复盘对比
        if rev_ctx:
            print(f"{'─'*60}")
            print(f"📜 历史复盘对比")
            print(f"{'─'*60}")
            rev_lines = rev_ctx.split("\n")
            for line in rev_lines[:20]:
                if line.strip():
                    print(f"  {line.strip()}")
            print()

        return result
    except Exception as e:
        print(f"\n  ❌ {keyword} 分析失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None

async def main():
    config = dict(DEFAULT_CONFIG)
    trade_date = _date.today().strftime("%Y-%m-%d")

    print(f"{'='*60}")
    print(f"  批量板块深度分析（含历史复盘）")
    print(f"  日期: {trade_date}")
    print(f"  板块列表: {', '.join(SECTORS)}")
    print(f"{'='*60}\n")

    print("初始化分析引擎...")
    graph = SectorAnalysisGraph(config=config)

    results = {}
    for sector in SECTORS:
        result = await analyze_sector(graph, sector, trade_date)
        results[sector] = result

    # 汇总对比
    print(f"\n{'='*60}")
    print(f"  三大板块分析对比汇总")
    print(f"{'='*60}")
    print(f"{'板块':<12} {'方向':<18} {'置信度':<8} {'仓位':<10} {'短期(天)':<20}")
    print(f"{'─'*60}")
    for sector in SECTORS:
        r = results.get(sector)
        if r:
            v = r.get("final_verdict", {})
            print(f"{sector:<12} {v.get('direction','N/A'):<18} {v.get('confidence','N/A')+'%':<8} {v.get('position','N/A'):<10} {v.get('short_term','N/A')[:18]:<20}")
            print(f"  {'':>12} {'核心':<8} {v.get('reason','N/A')[:60]}")
            print(f"  {'':>12} {'风险':<8} {v.get('key_risk','N/A')[:60]}")
            print()

if __name__ == "__main__":
    asyncio.run(main())