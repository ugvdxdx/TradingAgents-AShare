#!/usr/bin/env python3
"""
模拟 2026年5月3日的选股分析（截止日 = 4月30日，五一前最后一个交易日）
用 4/30 之前的数据选股，用 4/30 → 6/3 的真实数据验证
"""

import json
import sys
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_cache import KlineCache
from run_debate_picker import (
    DebateArg, DebateRecord, DebateExchange,
    check_surrender, refute_args, _format_statement, _format_rebuttals,
    gather_stock_info, score_v7,
    _add_fundamental_bull, _add_wk_bull, _add_tech_bull, _add_valuation_bull,
    _add_fundamental_bear, _add_wk_bear, _add_tech_bear, _add_valuation_bear,
    get_world_knowledge,
    CACHE_DIR, WHITELIST_FILE,
    print_debate_process, debate_round,
)
from ai_knowledge_base import lookup_knowledge
from fundamental_scorer import compute_fundamental_knowledge
from tech_analysis import compute_tech_score, TechScore

CACHE = KlineCache(CACHE_DIR)
CUTOFF_DATE = "2026-04-30"
SIM_DATE = "2026-05-03"

with open(WHITELIST_FILE, 'r') as f:
    WHITELIST = json.load(f)


def _get_kline_for_date(stock_code: str, cutoff_date: str) -> pd.DataFrame:
    suffix = '.SH' if stock_code.startswith('6') else '.SZ'
    df = CACHE.get(f"{stock_code}{suffix}")
    if df is None or len(df) == 0:
        return None
    df = df[df['trade_date'] <= cutoff_date].copy()
    if len(df) < 20:
        return None
    df = df.set_index('trade_date')
    return df


def run_full_debate(stocks_info: List[Dict]) -> Tuple[List[List[DebateRecord]], List[Dict]]:
    """运行完整四轮辩论流水线"""
    survivors = stocks_info
    all_records = []

    for focus, target, name in [
        ('fundamental', 50, "第一轮"),
        ('moat_growth', 30, "第二轮"),
        ('tech', 20, "第三轮"),
        ('final', 10, "第四轮"),
    ]:
        records = debate_round(survivors, focus, target, name)
        all_records.append(records)
        survivors = [s for s, r in zip(survivors, records) if not r.eliminated]

    return all_records, survivors


def main():
    print(f"\n{'='*70}")
    print(f"  模拟日期: {SIM_DATE}（截止日: {CUTOFF_DATE}，五一前最后交易日）")
    print(f"{'='*70}\n")

    # --- 阶段0: 召回 ---
    print("[阶段0] 评分召回...")
    scored_stocks = []
    for stock in WHITELIST:
        code = stock['code']
        df = _get_kline_for_date(code, CUTOFF_DATE)
        if df is None:
            continue

        tech = compute_tech_score(df)
        fund_score = compute_fundamental_knowledge(code, stock.get('name', ''))
        industries, industry_score = lookup_knowledge(code, stock.get('name', ''))
        if fund_score is not None:
            know_score = fund_score
        else:
            know_score = max(4, min(16, int(industry_score * 1.6))) if industry_score >= 4 else 4

        stock_copy = dict(stock)
        stock_copy['_know_score'] = know_score
        stock_copy['tech'] = tech
        stock_copy['pe_ttm'] = stock.get('pe_ttm')
        stock_copy['market'] = stock.get('market', 'mainboard')
        stock_copy['total_score'] = score_v7(stock_copy, tech)
        stock_copy['industries'] = industries
        stock_copy['_kline_for_cutoff'] = df  # 截止日的 K 线
        scored_stocks.append(stock_copy)

    scored_stocks.sort(key=lambda x: x.get('total_score', 0), reverse=True)
    top100 = scored_stocks[:100]
    print(f"  白名单 {len(WHITELIST)} → 有效 {len(scored_stocks)} → Top100")
    print(f"  评分范围: {top100[0].get('total_score',0):.0f} ~ {top100[-1].get('total_score',0):.0f}")

    # 加载基本面和世界知识
    for s in top100:
        info = gather_stock_info(s)
        s['fundamentals'] = info.get('fundamentals')
        s['world_knowledge'] = info.get('world_knowledge')

    # --- 阶段1-4: 辩论 ---
    print("\n[阶段1-4] 四轮辩论...")
    all_records, survivors = run_full_debate(top100)

    for i, records in enumerate(all_records):
        sel = [r for r in records if not r.eliminated]
        elim = [r for r in records if r.eliminated]
        bs = sum(1 for r in sel if r.bear_surrendered)
        bus = sum(1 for r in sel if r.bull_surrendered)
        print(f"  第{i+1}轮: 淘汰{len(elim)}只 保留{len(sel)}只 | Bear投降{bs}次 Bull投降{bus}次")

    # --- 最终推荐 ---
    final_records = all_records[-1]
    top10 = [r for r in final_records if not r.eliminated]
    stock_map = {s['code']: s for s in top100}

    # --- 真实验证: 4/30 → 6/3 ---
    print(f"\n[验证] {CUTOFF_DATE} → 2026-06-03 真实收益（约 20 个交易日）")
    validation = []
    for r in top10:
        code = r.code
        suffix = '.SH' if code.startswith('6') else '.SZ'
        df_full = CACHE.get(f"{code}{suffix}")
        if df_full is None:
            continue

        # 找截止日位置
        cutoff_idx = df_full[df_full['trade_date'] <= CUTOFF_DATE].index
        if len(cutoff_idx) == 0:
            continue
        cutoff_pos = cutoff_idx[-1]

        close_start = float(df_full.iloc[cutoff_pos]['close'])
        close_end = float(df_full.iloc[-1]['close'])
        ret = (close_end - close_start) / close_start * 100

        # 计算未来交易天数
        future_days = len(df_full) - cutoff_pos - 1

        # 中间最高最低
        future = df_full.iloc[cutoff_pos+1:]
        max_close = float(future['close'].max()) if len(future) > 0 else close_end
        min_close = float(future['close'].min()) if len(future) > 0 else close_end
        max_ret = (max_close - close_start) / close_start * 100
        max_dd = (min_close - close_start) / close_start * 100

        s = stock_map.get(code, {})
        tech = s.get('tech', TechScore())

        validation.append({
            'code': code, 'name': r.name,
            'judge_score': r.judge_score,
            'bull_score': r.bull_score,
            'bear_score': r.bear_score,
            'total_score': s.get('total_score', 0),
            'know_score': s.get('_know_score', 0),
            'tech_total': tech.total,
            'pe_ttm': s.get('pe_ttm'),
            'industries': s.get('industries', [])[:3],
            'bear_surrendered': r.bear_surrendered,
            'bull_surrendered': r.bull_surrendered,
            'judge_verdict': r.judge_verdict,
            'return_pct': round(ret, 2),
            'max_return': round(max_ret, 2),
            'max_drawdown': round(max_dd, 2),
            'close_start': round(close_start, 2),
            'close_end': round(close_end, 2),
            'future_days': future_days,
            'bull_args': [{'point': a.point, 'evidence': a.evidence, 'weight': a.weight} for a in r.bull_args[:6]],
            'bear_args': [{'point': a.point, 'evidence': a.evidence, 'weight': a.weight} for a in r.bear_args[:6]],
            'exchanges': [{
                'round_num': ex.round_num,
                'bull_statement': ex.bull_statement[:200] if ex.bull_statement else '',
                'bear_rebuttal': ex.bear_rebuttal,
                'bear_statement': ex.bear_statement[:200] if ex.bear_statement else '',
                'bull_rebuttal': ex.bull_rebuttal,
            } for ex in r.exchanges],
        })

    # 按收益排序
    validation.sort(key=lambda x: x['return_pct'], reverse=True)
    avg_ret = np.mean([v['return_pct'] for v in validation])
    pos_rate = sum(1 for v in validation if v['return_pct'] > 0) / len(validation) * 100

    # --- 输出 ---
    print(f"\n{'='*100}")
    print(f"  {SIM_DATE} 推荐 TOP10（截止日 {CUTOFF_DATE}，收益统计 4/30→6/3）")
    print(f"{'='*100}")
    print(f"{'#':>3} {'代码':>8} {'名称':>8} {'收益':>8} {'最高':>8} {'最低':>8} {'天数':>5} {'Judge':>7} {'行业':>20}")
    print('-' * 100)
    for i, v in enumerate(validation):
        industries = ','.join(v['industries'][:2])[:18]
        print(f"{i+1:3d} {v['code']:>8} {v['name']:<8} {v['return_pct']:+7.2f}% {v['max_return']:+7.2f}% {v['max_drawdown']:+7.2f}% {v['future_days']:5d} {v['judge_score']:7.1f} {industries:<20}")

    print(f"\n  平均收益: {avg_ret:+.2f}%")
    print(f"  正收益率: {pos_rate:.0f}%")

    # --- 辩论详情摘要 ---
    print(f"\n{'='*100}")
    print(f"  个股辩论摘要")
    print(f"{'='*100}")
    for v in validation:
        surr = "Bear投降" if v['bear_surrendered'] else ("Bull投降" if v['bull_surrendered'] else "正常辩论")
        print(f"\n  {v['code']} {v['name']}  [{surr}]  收益: {v['return_pct']:+.2f}%")
        print(f"  Bull核心论据: {', '.join(a['point'] for a in v['bull_args'][:4])}")
        print(f"  Bear核心论据: {', '.join(a['point'] for a in v['bear_args'][:4])}")
        # 辩论交互
        for ex in v['exchanges']:
            bs = ex['bull_statement']
            bb = ex['bear_statement']
            br = ex['bear_rebuttal']
            bur = ex['bull_rebuttal']
            if bs:
                print(f"    Round{ex['round_num']} Bull: {bs[:100]}...")
            if br:
                print(f"    Round{ex['round_num']} Bear反驳: {br}")
            if bb:
                print(f"    Round{ex['round_num']} Bear: {bb[:100]}...")
            if bur:
                print(f"    Round{ex['round_num']} Bull反驳: {bur}")

    # --- 保存 ---
    report = {
        'sim_date': SIM_DATE,
        'cutoff_date': CUTOFF_DATE,
        'generated_at': datetime.now().isoformat(),
        'pipeline': 'Top100 → 辩论1(50) → 辩论2(30) → 辩论3(20) → 辩论4(10)',
        'rounds_summary': [
            {'round': i+1, 'kept': len([r for r in rec if not r.eliminated]),
             'eliminated': len([r for r in rec if r.eliminated]),
             'bear_surr': sum(1 for r in rec if r.bear_surrendered),
             'bull_surr': sum(1 for r in rec if r.bull_surrendered)}
            for i, rec in enumerate(all_records)
        ],
        'top10': validation,
        'avg_return': round(avg_ret, 2),
        'positive_rate': round(pos_rate, 1),
    }

    report_file = f"backtest_sim_{SIM_DATE.replace('-','')}.json"
    # Convert numpy types to native Python
    def convert(obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=convert)
    print(f"\n报告已保存: {report_file}")

    # --- 返回数据供文档使用 ---
    return report


if __name__ == '__main__':
    result = main()
    print(f"\nDone. {len(result['top10'])} stocks analyzed.")
