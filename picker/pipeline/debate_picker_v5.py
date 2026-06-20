#!/usr/bin/env python3
"""
J-TradingAgents 量化选股系统

纯量化排序 (无LLM辩论), 按锚分 chain+capital×2-delivery×0.5 选 TOP10。
回测验证: 21期×530只×30日 Spearman=+0.555, 20/20期正相关。

流程: collect_data → quantum_rank → risk_review → report_render

用法:
  uv run python3 debate_picker_v5.py                      # 实盘 (今日)
  uv run python3 debate_picker_v5.py --date 2026-04-24    # 指定日期/回测
  uv run python3 debate_picker_v5.py --top-n 50           # 候选池规模
  uv run python3 debate_picker_v5.py --top-k 20           # 输出 TOP20
  uv run python3 debate_picker_v5.py --dry-run            # 跳过网络, 验证管道
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
    ap = argparse.ArgumentParser(description="量化选股系统 (锚分排序)")
    ap.add_argument("--date", type=str, default="", help="交易日 (默认今日; 回测传截止日)")
    ap.add_argument("--top-n", type=int, default=50, help="候选股规模 (V3 Top-N)")
    ap.add_argument("--top-k", type=int, default=10, help="最终排名规模")
    ap.add_argument("--dry-run", action="store_true", help="跳过网络请求, 仅验证管道")
    args = ap.parse_args()

    g = PickerGraph(top_n=args.top_n, debate_top_k=args.top_k)
    g.run(trade_date=args.date or None, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
