#!/usr/bin/env python3
"""
J-TradingAgents 量化选股系统

纯量化排序 (无LLM辩论), 按锚分 chain+capital×2+surge×SURGE_WEIGHT 选 TOP10。
回测验证: 21期×530只×30日 Spearman=+0.555, 20/20期正相关。

流程: collect_data → quantum_rank → risk_review → report_render

用法:
  uv run python3 debate_picker_v5.py                      # 实盘 (今日)
  uv run python3 debate_picker_v5.py --date 2026-04-24    # 指定日期/回测
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
    ap.add_argument("--date", type=str, default="",
                    help="交易日 (默认今日实盘; 传非今日日期则进入回测模式, 截断K线/资金流到该日)")
    ap.add_argument("--top-k", type=int, default=5, help="最终排名规模 (默认5, 策略回测最优)")
    ap.add_argument("--dry-run", action="store_true", help="跳过网络请求, 仅验证管道")
    args = ap.parse_args()

    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    if args.date and args.date != today:
        # 非今日 → 回测模式: trade_date=该日, cutoff_date=该日 (截断数据, 跳过capital重算)
        g = PickerGraph(debate_top_k=args.top_k)
        g.run(trade_date=args.date, cutoff_date=args.date, dry_run=args.dry_run)
    else:
        g = PickerGraph(debate_top_k=args.top_k)
        g.run(trade_date=args.date or None, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
