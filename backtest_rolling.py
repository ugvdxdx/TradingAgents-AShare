#!/usr/bin/env python3
"""
滚动回测脚本：使用 n 天前的数据做选股，验证未来 10 个交易日的真实收益。
无数据穿越 —— 每轮只用截止日之前的数据做评分和辩论。

用法：uv run python3 backtest_rolling.py
"""

import json
import sys
import os
import types
import time
import pickle
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
)
from ai_knowledge_base import lookup_knowledge
from fundamental_scorer import compute_fundamental_knowledge
from tech_analysis import compute_tech_score, TechScore

# 资金流（可选）
_MF_AVAILABLE = False
try:
    from money_flow import compute_money_flow_score as _mf_compute
    _MF_AVAILABLE = True
except Exception:
    pass

CACHE = KlineCache(CACHE_DIR)

# ═══════════════════════════════════════════════
# 1. 加载白名单
# ═══════════════════════════════════════════════

with open(WHITELIST_FILE, 'r') as f:
    WHITELIST = json.load(f)


def _read_kline_raw(stock_code: str) -> pd.DataFrame:
    """直接读 pkl 文件，绕过过期检查（回测需要历史数据）"""
    suffix = '.SH' if stock_code.startswith('6') else '.SZ'
    safe_name = f"{stock_code}{suffix}".replace('.', '_')
    path = os.path.join(CACHE_DIR, f"{safe_name}.pkl")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            df = pickle.load(f)
        return df if isinstance(df, pd.DataFrame) and len(df) > 0 else None
    except:
        return None


def _get_kline_for_date(stock_code: str, cutoff_date: str) -> pd.DataFrame:
    """获取截止到 cutoff_date（含）的 K 线数据，剔除未来数据"""
    df = _read_kline_raw(stock_code)
    if df is None or len(df) == 0:
        return None
    # 只保留截止日（含）之前的数据
    df = df[df['trade_date'] <= cutoff_date].copy()
    if len(df) < 20:
        return None
    # 重置索引为 trade_date 方便技术分析
    df = df.set_index('trade_date')
    return df


# ═══════════════════════════════════════════════
# 2. 单窗口选股
# ═══════════════════════════════════════════════

def run_debate_for_window(stocks_info: List[Dict]) -> Tuple[List[DebateRecord], List[DebateRecord]]:
    """执行四轮辩论，返回 (round4_top10, round3_top20)"""
    survivors = stocks_info
    saved_top20 = []

    for _round_idx, (focus, target) in enumerate([
        ('fundamental', 50),
        ('moat_growth', 30),
        ('tech', 20),
        ('final', 10),
    ]):
        records = []
        for stock in survivors:
            bull_args, bear_args = [], []
            fund = stock.get('fundamentals')
            wk = stock.get('world_knowledge')
            tech = stock.get('tech', TechScore())
            code = stock['code']

            # === Bull 论据 ===
            if fund:
                _add_fundamental_bull(bull_args, fund)
            if wk:
                _add_wk_bull(bull_args, wk, stock['name'])
            if tech and tech.total > 0:
                _add_tech_bull(bull_args, tech)
            _add_valuation_bull(bull_args, stock.get('pe_ttm'), stock.get('market', 'mainboard'))

            # === Bear 论据 ===
            if fund:
                _add_fundamental_bear(bear_args, fund)
            if wk:
                _add_wk_bear(bear_args, wk, stock['name'])
            if tech and tech.total > 0:
                _add_tech_bear(bear_args, tech)
            _add_valuation_bear(bear_args, stock.get('pe_ttm'))

            # === 两轮交互辩论 ===
            ex1 = DebateExchange(round_num=1, bull_statement=_format_statement(bull_args, "bull"))
            bull_args = refute_args(bear_args, bull_args)
            ex1.bear_rebuttal = _format_rebuttals(bull_args)

            ex2 = DebateExchange(round_num=2, bear_statement=_format_statement(bear_args, "bear"))
            bear_args = refute_args(bull_args, bear_args)
            ex2.bull_rebuttal = _format_rebuttals(bear_args)

            # === 投降判断 ===
            bull_sur, bear_sur, sur_reason = check_surrender(bull_args, bear_args)

            # === 裁判评分 ===
            bull_w = sum(a.weight_after_refute for a in bull_args)
            bear_w = sum(a.weight_after_refute for a in bear_args)
            judge_score = bull_w * 1.8 - bear_w
            if bull_sur:
                judge_score = bear_w * 1.8 - bull_w
            if bear_sur:
                judge_score = bull_w * 1.8 - bear_w + 40

            # 资金流乘性调节
            mf_mult = stock.get('_mf_multiplier', 1.0)
            judge_score *= mf_mult

            record = DebateRecord(
                code=stock['code'], name=stock['name'],
                bull_args=bull_args, bear_args=bear_args,
                exchanges=[ex1, ex2],
                bull_score=bull_w, bear_score=bear_w,
                judge_score=judge_score,
                bull_surrendered=bull_sur, bear_surrendered=bear_sur,
                surrender_reason=sur_reason,
                judge_verdict=sur_reason,
                eliminated=False,
            )
            records.append(record)

        # 按 judge_score 降序保留 target 只
        records.sort(key=lambda r: r.judge_score, reverse=True)
        # 第 3 轮（target=20）后保存 Top20 供分析
        if target == 20:
            saved_top20 = records[:20].copy()
        for r in records[target:]:
            r.eliminated = True
        survivors = [s for s, r in zip(survivors, records) if not r.eliminated]

    top10 = [r for r in records if not r.eliminated]
    return top10, saved_top20


# ═══════════════════════════════════════════════
# 3. 滚动回测主逻辑
# ═══════════════════════════════════════════════

def get_available_cutoff_dates() -> List[Tuple[str, int]]:
    """从缓存中找出所有可用的截止日期及对应的验证天数

    Returns:
        List of (cutoff_date, future_days) tuples
        例如60天数据: [(date30, 30), (date40, 20), (date50, 10)]
    """
    sample_code = WHITELIST[0]['code']
    df = _read_kline_raw(sample_code)
    if df is None:
        return []
    dates = sorted(df['trade_date'].unique())
    n = len(dates)

    min_history = 20  # 最少需要20天历史做技术分析
    min_future = 10   # 最少验证10天
    windows = []

    # 从最早可用截止日到最晚，每5个交易日一个窗口
    for i in range(min_history, n - min_future + 1, 5):
        future_days = n - i - 1  # 剩余天数作为验证窗口
        if future_days >= min_future:
            windows.append((dates[i], future_days))

    return windows  # 从早到晚排列，验证天数递减


def run_backtest():
    windows = get_available_cutoff_dates()
    if not windows:
        print("缓存中没有足够的历史数据（需要至少 30 个交易日）")
        return

    print(f"滚动回测窗口数: {len(windows)}")
    print(f"日期范围: {windows[0][0]} ~ {windows[-1][0]}")
    print(f"验证天数: {windows[0][1]} ~ {windows[-1][1]}")
    print(f"每个窗口：用截止日之前数据选股，计算未来可用的真实收益")
    print()

    all_results = []

    for wi, (cutoff_date, future_days) in enumerate(windows):
        t0 = time.time()
        print(f"\n{'='*70}")
        print(f"窗口 {wi+1}/{len(windows)}: 截止日 = {cutoff_date}, 验证天数 = {future_days}")
        print(f"{'='*70}")

        # --- 评分阶段：只用截止日之前的数据 ---
        scored_stocks = []
        for stock in WHITELIST:
            code = stock['code']
            df = _get_kline_for_date(code, cutoff_date)
            if df is None:
                continue

            # 技术面评分（只用历史数据）
            tech = compute_tech_score(df)

            # 知识分
            fund_score = compute_fundamental_knowledge(code, stock.get('name', ''))
            industries, industry_score = lookup_knowledge(code, stock.get('name', ''))
            if fund_score is not None:
                know_score = fund_score
            else:
                know_score = max(4, min(16, int(industry_score * 1.6))) if industry_score >= 4 else 4

            # v7 评分
            stock_copy = dict(stock)
            stock_copy['_know_score'] = know_score
            stock_copy['tech'] = tech
            stock_copy['pe_ttm'] = stock.get('pe_ttm')
            stock_copy['market'] = stock.get('market', 'mainboard')
            total_score = score_v7(stock_copy, tech)
            stock_copy['total_score'] = total_score
            stock_copy['industries'] = industries
            stock_copy['_kline'] = df
            scored_stocks.append(stock_copy)

        # 取 Top 100
        scored_stocks.sort(key=lambda x: x.get('total_score', 0), reverse=True)
        top100 = scored_stocks[:100]
        print(f"  召回 Top100: 评分范围 {top100[0].get('total_score',0):.0f} ~ {top100[-1].get('total_score',0):.0f}")

        # --- 加载世界知识 ---
        for s in top100:
            s['fundamentals'] = gather_stock_info(s).get('fundamentals')
            s['world_knowledge'] = gather_stock_info(s).get('world_knowledge')

        # --- 资金流尾部风险过滤 ---
        mf_filtered = 0
        if _MF_AVAILABLE:
            for s in top100:
                code = s['code']
                try:
                    mf = _mf_compute(code, s.get('mcap_yi', 0), cutoff_date=cutoff_date)
                    s['_mf_multiplier'] = mf.multiplier
                    s['_mf_consecutive_out'] = mf.consecutive_out
                except Exception:
                    s['_mf_multiplier'] = 1.0
                    s['_mf_consecutive_out'] = 0
            # 硬过滤：连续流出 10 天以上（匹配中期策略）直接踢出
            top100 = [s for s in top100 if s.get('_mf_consecutive_out', 0) < 10]
            mf_filtered = 100 - len(top100)
            if mf_filtered > 0:
                print(f"  资金流硬过滤: 剔除 {mf_filtered} 只（连续流出≥10天），剩余 {len(top100)} 只")
        else:
            for s in top100:
                s['_mf_multiplier'] = 1.0

        # --- 辩论选股 ---
        top10_records, top20_records = run_debate_for_window(top100)

        # --- 验证函数：计算给定股票列表的未来真实收益 ---
        def validate_stocks(recs: List[DebateRecord], verify_days: int) -> List[Dict]:
            results = []
            min_verify = 10  # 最少验证10天才有意义
            for rec in recs:
                code = rec.code
                df_full = _read_kline_raw(code)
                if df_full is None:
                    continue
                cutoff_idx = df_full[df_full['trade_date'] <= cutoff_date].index
                if len(cutoff_idx) == 0:
                    continue
                cutoff_pos = cutoff_idx[-1]
                future = df_full.iloc[cutoff_pos+1:cutoff_pos+1+verify_days]
                actual_days = len(future)
                if actual_days < min_verify:
                    continue
                close_start = df_full.iloc[cutoff_pos]['close']
                close_end = future.iloc[-1]['close']
                ret = (close_end - close_start) / close_start * 100
                results.append({
                    'code': code, 'name': rec.name,
                    'judge_score': rec.judge_score,
                    'cutoff_date': cutoff_date,
                    'return_pct': round(ret, 2),
                    'future_days': actual_days,
                    'verify_target': verify_days,
                })
            return results

        top10_results = validate_stocks(top10_records, future_days)
        top20_results = validate_stocks(top20_records, future_days)

        if top10_results:
            top10_ret = np.mean([r['return_pct'] for r in top10_results])
            top20_ret = np.mean([r['return_pct'] for r in top20_results]) if top20_results else 0
            top5_ret = np.mean([r['return_pct'] for r in sorted(top10_results, key=lambda x: x['judge_score'], reverse=True)[:5]])
            top10_pr = sum(1 for r in top10_results if r['return_pct'] > 0) / len(top10_results) * 100
            top20_pr = sum(1 for r in top20_results if r['return_pct'] > 0) / len(top20_results) * 100 if top20_results else 0
            top5_pr = sum(1 for r in sorted(top10_results, key=lambda x: x['judge_score'], reverse=True)[:5] if r['return_pct'] > 0) / min(5, len(top10_results)) * 100
            print(f"  Top5: {top5_ret:+.2f}% ({top5_pr:.0f}%) | Top10: {top10_ret:+.2f}% ({top10_pr:.0f}%) | Top20: {top20_ret:+.2f}% ({top20_pr:.0f}%) [验证{future_days}日]")
            for wr in top10_results[:3]:
                print(f"    {wr['code']} {wr['name']:<8s}  {wr['return_pct']:+.1f}% ({wr['future_days']}日)")
            all_results.append({
                'window': wi + 1, 'cutoff_date': cutoff_date, 'future_days': future_days,
                'top5_return': round(top5_ret, 2), 'top5_positive': round(top5_pr, 1),
                'top10_return': round(top10_ret, 2), 'top10_positive': round(top10_pr, 1),
                'top20_return': round(top20_ret, 2), 'top20_positive': round(top20_pr, 1),
                'top10_results': top10_results, 'top20_results': top20_results,
            })
        else:
            print(f"  无有效验证数据")

        elapsed = time.time() - t0
        print(f"  耗时: {elapsed:.1f}s")

    # --- 汇总 ---
    print(f"\n\n{'='*70}")
    print("滚动回测汇总（Top5 / Top10 / Top20）")
    print(f"{'='*70}")
    print(f"{'窗口':>4} {'截止日期':>12} {'Top5':>8} {'Top10':>8} {'Top20':>8} {'Top5胜率':>8} {'Top10胜率':>8} {'Top20胜率':>8}")
    print('-' * 80)
    all_top5_ret, all_top10_ret, all_top20_ret = [], [], []
    for r in all_results:
        print(f"{r['window']:4d} {r['cutoff_date']:>12} {r['top5_return']:+7.2f}% {r['top10_return']:+7.2f}% {r['top20_return']:+7.2f}% {r['top5_positive']:7.1f}% {r['top10_positive']:7.1f}% {r['top20_positive']:7.1f}%")
        for s in r.get('top10_results', []):
            all_top10_ret.append(s['return_pct'])
        for s in sorted(r.get('top10_results', []), key=lambda x: -x['judge_score'])[:5]:
            all_top5_ret.append(s['return_pct'])
        for s in r.get('top20_results', []):
            all_top20_ret.append(s['return_pct'])

    if all_top10_ret:
        print('-' * 80)
        print(f"{'总计':>4} {'':>12} {np.mean(all_top5_ret):+7.2f}% {np.mean(all_top10_ret):+7.2f}% {np.mean(all_top20_ret):+7.2f}% {sum(1 for r in all_top5_ret if r>0)/len(all_top5_ret)*100:7.1f}% {sum(1 for r in all_top10_ret if r>0)/len(all_top10_ret)*100:7.1f}% {sum(1 for r in all_top20_ret if r>0)/len(all_top20_ret)*100:7.1f}%")

    # 保存报告
    report = {
        'timestamp': datetime.now().isoformat(),
        'windows': len(all_results),
        'date_range': f"{windows[0][0]} ~ {windows[-1][0]}",
        'overall_top5_avg': round(np.mean(all_top5_ret), 2) if all_top5_ret else 0,
        'overall_top5_pos': round(sum(1 for r in all_top5_ret if r>0)/len(all_top5_ret)*100, 1) if all_top5_ret else 0,
        'overall_top10_avg': round(np.mean(all_top10_ret), 2) if all_top10_ret else 0,
        'overall_top10_pos': round(sum(1 for r in all_top10_ret if r>0)/len(all_top10_ret)*100, 1) if all_top10_ret else 0,
        'overall_top20_avg': round(np.mean(all_top20_ret), 2) if all_top20_ret else 0,
        'overall_top20_pos': round(sum(1 for r in all_top20_ret if r>0)/len(all_top20_ret)*100, 1) if all_top20_ret else 0,
        'results': all_results,
    }
    report_file = f"backtest_rolling_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_file}")


if __name__ == '__main__':
    # 加载 .env（确保 LLM 打分可用）
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith('#') and '=' in _line:
                    _k, _v = _line.split('=', 1)
                    os.environ.setdefault(_k.strip(), _v.strip().split('#')[0].strip())
    run_backtest()
