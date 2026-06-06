#!/usr/bin/env python3
"""Run commercial aerospace sector deep analysis."""
import asyncio
import sys
import os

# Load .env FIRST, before any project imports
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)
print(f"Loaded .env: TA_BASE_URL={os.getenv('TA_BASE_URL', 'NOT SET')}")
print(f"  TA_LLM_DEEP={os.getenv('TA_LLM_DEEP', 'NOT SET')}")
print(f"  TA_LLM_QUICK={os.getenv('TA_LLM_QUICK', 'NOT SET')}")

sys.path.insert(0, '/Users/bilibili/Desktop/J-TradingAgents')

from tradingagents.agents.sector.sector_graph import SectorAnalysisGraph
from tradingagents.default_config import DEFAULT_CONFIG

async def run_deep():
    print("初始化分析引擎...")
    config = dict(DEFAULT_CONFIG)
    graph = SectorAnalysisGraph(config=config)

    print("开始商业航天板块深度分析...")
    result = await graph.run("商业航天", "2026-05-27")

    verdict = result.get("final_verdict", {})
    print(f"\n=== 分析完成 ===")
    print(f"方向: {verdict.get('direction', 'N/A')}")
    print(f"置信度: {verdict.get('confidence', 'N/A')}%")
    print(f"短期: {verdict.get('short_term', 'N/A')}")
    print(f"中期: {verdict.get('mid_term', 'N/A')}")
    print(f"长期: {verdict.get('long_term', 'N/A')}")
    print(f"核心结论: {verdict.get('reason', 'N/A')}")
    print(f"核心风险: {verdict.get('key_risk', 'N/A')}")

    # Check results directory
    results_dir = "/Users/bilibili/Desktop/J-TradingAgents/results"
    if os.path.exists(results_dir):
        print(f"\n研报归档目录内容:")
        for item in os.listdir(results_dir):
            print(f"  {item}")

if __name__ == "__main__":
    asyncio.run(run_deep())