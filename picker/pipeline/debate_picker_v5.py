#!/usr/bin/env python3
"""
J-TradingAgents 30天涨幅竞争辩论系统 v5

基于 LangGraph 编排的 7 阶段多智能体辩论:
  数据采集 → 三分析师并行(技术/资金/基本面催化)
          → 分组海选 Map-Reduce (50→20)
          → claim 驱动交叉辩论 (多头↔空头, 最多3轮)
          → 终极 PK (10→最终排名 + 置信度 + 风险标签)
          → 风控复核 (可信度评估 + 风险提示)
          → 终端富文本报告 + 全过程落盘

相比 v4 的升级:
  - 用 LangGraph 替代手写线程池, 与项目主体一致
  - claim 跟踪机制: 每个排名可追溯到带证据的结构化论点
  - 可信度评估 + 风险标签 + 术语解释, 面向量化小白
  - 中间过程全部落盘到 results/picker_v5/

用法:
  uv run python3 debate_picker_v5.py                      # 实盘 (今日)
  uv run python3 debate_picker_v5.py --date 2026-06-10    # 指定日期
  uv run python3 debate_picker_v5.py --rounds 2           # 辩论轮次上限
  uv run python3 debate_picker_v5.py --top-n 50           # 候选规模
  uv run python3 debate_picker_v5.py --dry-run            # 跳过LLM(验证管道)
"""
import argparse
import os
import sys

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv(override=True)

from tradingagents.agents.picker.picker_graph import PickerGraph


def main():
    ap = argparse.ArgumentParser(description="30天涨幅竞争辩论系统 v5 (LangGraph)")
    ap.add_argument("--date", type=str, default="", help="交易日 (默认今日)")
    ap.add_argument("--rounds", type=int, default=3, help="claim 驱动交叉辩论轮次上限")
    ap.add_argument("--top-n", type=int, default=50, help="候选股规模")
    ap.add_argument("--dry-run", action="store_true", help="跳过 LLM, 仅验证数据管道")
    args = ap.parse_args()

    g = PickerGraph(max_debate_rounds=args.rounds, top_n=args.top_n)
    g.run(trade_date=args.date or None, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
