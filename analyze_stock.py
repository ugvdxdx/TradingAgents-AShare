#!/usr/bin/env python3
"""
单股深度分析工具 —— 复用选股系统完全一致的评分和辩论逻辑
用法: uv run python3 analyze_stock.py <股票代码>
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_cache import KlineCache
from fundamental_scorer import compute_fundamental_knowledge
from tech_analysis import TechScore, compute_tech_score, analyze_trend, analyze_momentum, analyze_volume, analyze_pattern

# ── 复用 run_debate_picker 的核心函数 ──
from run_debate_picker import (
    gather_stock_info,
    run_debate,
    build_bull_args,
    build_bear_args,
    _format_statement,
    _format_rebuttals,
    refute_args,
    DebateRecord, DebateArg, DebateExchange,
)

CACHE = KlineCache()

# ────────────────────────────────────────────────
# 1. 加载股价数据
# ────────────────────────────────────────────────

def load_stock_data(code: str) -> Dict:
    """加载单只股票的全部数据"""
    suffix = '.SH' if code.startswith('6') else '.SZ'
    kline = CACHE.get(f"{code}{suffix}")
    if kline is None or len(kline) == 0:
        print(f"❌ 未找到 {code} 的行情数据")
        sys.exit(1)

    kline = kline.sort_values('trade_date').set_index('trade_date')

    # 基本面
    from run_debate_picker import FUNDAMENTALS_DIR
    fund_path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    fundamentals = None
    if os.path.exists(fund_path):
        with open(fund_path, 'r', encoding='utf-8') as f:
            fundamentals = json.load(f)

    return {'code': code, 'kline': kline, 'fundamentals': fundamentals}


# ────────────────────────────────────────────────
# 2. 实时信息叠加
# ────────────────────────────────────────────────

def print_realtime_overlay(df: pd.DataFrame):
    """打印实时交易叠加信息"""
    if len(df) < 2:
        return

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    latest_date = str(df.index[-1])[:10]

    print(f"\n{'═'*60}")
    print(f"  📡 实时行情  {latest_date}")
    print(f"{'═'*60}")
    print(f"  {'开盘':>6} {'收盘':>6} {'最高':>6} {'最低':>6} {'涨跌%':>7}")
    print(f"  {latest.get('open',0):6.1f} {latest.get('close',0):6.1f} {latest.get('high',0):6.1f} {latest.get('low',0):6.1f} {(latest.close/prev.close-1)*100:+6.1f}%")

    # 盘中动态
    op = float(latest.get('open', 0))
    hi = float(latest.get('high', 0))
    lo = float(latest.get('low', 0))
    cl = float(latest.get('close', 0))
    if op > 0:
        print(f"  日内振幅: {(hi - lo) / op * 100:.1f}%")
        if hi > cl * 1.01:
            print(f"  ⚠️ 冲高回落: 最高 {hi:.1f} → 收 {cl:.1f}，从高点回落 {(hi-cl)/hi*100:.1f}%")

    # 成交量
    vol_latest = float(latest.get('volume', 0))
    if len(df) >= 6:
        vol_avg5 = float(df['volume'].iloc[-6:-1].mean())
        if vol_avg5 > 0:
            vol_ratio = vol_latest / vol_avg5
            tag = "🔥 显著放量" if vol_ratio > 2.0 else ("📈 温和放量" if vol_ratio > 1.5 else ("📉 缩量" if vol_ratio < 0.5 else "→ 正常"))
            print(f"  量比(5日均): {vol_ratio:.1f}x  {tag}")


# ────────────────────────────────────────────────
# 3. 技术评分
# ────────────────────────────────────────────────

def print_tech_analysis(df: pd.DataFrame):
    """打印与系统完全相同的技术评分"""
    print(f"\n{'═'*60}")
    print(f"  📊 技术评分 (与选股流水线一致)")
    print(f"{'═'*60}")

    trend, t_desc = analyze_trend(df)
    momentum, m_desc = analyze_momentum(df)
    volume, v_desc = analyze_volume(df)
    pattern, p_desc = analyze_pattern(df)
    ts = compute_tech_score(df)

    rows = [
        ("趋势", trend, 35, t_desc),
        ("动量", momentum, 30, m_desc),
        ("量能", volume, 20, v_desc),
        ("形态", pattern, 15, p_desc),
        ("合计", ts.total, 100, ""),
    ]
    for label, val, mx, desc in rows:
        bar = "█" * int(val / mx * 20) if mx > 0 else ""
        extra = f" — {desc}" if desc else ""
        print(f"  {label:<6} {val:5.1f}/{mx:<4} {bar} {extra}")

    # 辅助指标
    latest = df.iloc[-1]
    print()
    print(f"  {'指标':<12} {'数值':>8}  {'判断'}")
    print(f"  {'─'*40}")
    rsi14 = latest.get('rsi', 50)
    if rsi14 > 80:    rsi_tag = "🔴 极度超买"
    elif rsi14 > 75:  rsi_tag = "🟠 明显超买"
    elif rsi14 > 65:  rsi_tag = "🟡 偏强"
    elif rsi14 >= 45: rsi_tag = "🟢 健康区间"
    elif rsi14 >= 40: rsi_tag = "🟡 偏弱"
    else:             rsi_tag = "🔴 弱势"
    print(f"  {'RSI(14)':<12} {rsi14:8.1f}  {rsi_tag}")

    ma20 = df['close'].rolling(20).mean().iloc[-1]
    if ma20 > 0:
        dev = (latest['close'] - ma20) / ma20 * 100
        dtag = "🔴 严重乖离" if dev > 25 else ("🟠 明显偏离" if dev > 15 else ("🟡 轻微偏离" if dev > 8 else "🟢 正常"))
        print(f"  {'MA20乖离':<12} {dev:+7.1f}% {dtag}")

    # 近5/10/20日收益
    for n, name in [(5, "5日涨幅"), (10, "10日涨幅"), (20, "20日涨幅")]:
        if len(df) > n:
            ret = (latest['close'] - df['close'].iloc[-n-1]) / df['close'].iloc[-n-1] * 100
            print(f"  {name:<12} {ret:+7.1f}%")


# ────────────────────────────────────────────────
# 4. 辩论分析
# ────────────────────────────────────────────────

def print_debate(info: Dict):
    """运行辩论并详细展示"""
    print(f"\n{'═'*60}")
    print(f"  ⚔️ 多空辩论")
    print(f"{'═'*60}")

    # 构建论据
    focus_map = {
        1: "universal",
        2: "moat_growth",
        3: "tech_valuation",
        4: "universal",
    }
    focus = focus_map.get(1, "universal")

    bull_args = build_bull_args(info, focus)
    bear_args = build_bear_args(info, focus)

    # 打印 Bull 陈述
    print(f"\n  🐂 看多论据 ({len(bull_args)}条)")
    print(f"  {'─'*55}")
    for a in sorted(bull_args, key=lambda x: -x.weight_after_refute):
        ref = " ⚡已反驳" if a.refuted else ""
        tag = "实据" if a.has_data else "定性"
        print(f"  │ {tag:>4} w{a.weight_after_refute:4.0f}  {a.point}{ref}")

    # 打印 Bear 陈述
    print(f"\n  🐻 看空论据 ({len(bear_args)}条)")
    print(f"  {'─'*55}")
    for a in sorted(bear_args, key=lambda x: x.weight_after_refute):
        ref = " ⚡已反驳" if a.refuted else ""
        tag = "实据" if a.has_data else "定性"
        print(f"  │ {tag:>4} w{a.weight_after_refute:4.0f}  {a.point}{ref}")

    # 第一轮：Bull 陈述 → Bear 反驳
    print(f"\n  🔁 第一轮：Bull 陈述 → Bear 反驳")
    bull_args = refute_args(bear_args, bull_args)
    print(f"  │ Bear反驳: {_format_rebuttals(bull_args)}")

    # 第二轮：Bear 陈述 → Bull 反驳
    print(f"\n  🔁 第二轮：Bear 陈述 → Bull 反驳")
    bear_args = refute_args(bull_args, bear_args)
    print(f"  │ Bull反驳: {_format_rebuttals(bear_args)}")

    # 权重汇总
    bull_w = sum(a.weight_after_refute for a in bull_args)
    bear_w = sum(a.weight_after_refute for a in bear_args)
    net = bull_w - bear_w

    print(f"\n  {'─'*55}")
    print(f"  │ Bull总权重: {bull_w:6.0f}")
    print(f"  │ Bear总权重: {bear_w:6.0f}")
    print(f"  │ 净权重差:   {net:+6.0f}")

    # 判断
    if net > 30:
        verdict = "🐂 Bull压倒性优势"
    elif net > 10:
        verdict = "🐂 Bull优势明显"
    elif net > 0:
        verdict = "🐂 Bull小幅领先"
    elif net > -10:
        verdict = "🐻 Bear小幅领先"
    elif net > -30:
        verdict = "🐻 Bear优势明显"
    else:
        verdict = "🐻 Bear压倒性优势"
    print(f"  │ 裁决: {verdict}")


# ────────────────────────────────────────────────
# 5. 资金流分析
# ────────────────────────────────────────────────

def print_money_flow(code: str, mcap_yi: float = 0):
    """资金流尾部风险检测"""
    from money_flow import compute_money_flow_score
    s = compute_money_flow_score(code, mcap_yi)
    if s.consecutive_out == 0:
        return

    print(f"\n{'═'*60}")
    print(f"  💰 资金流尾部风险")
    print(f"{'═'*60}")
    print(f"  主力连续流出: {s.consecutive_out}日")
    if s.consecutive_out >= 10:
        print(f"  ⚠️ 硬过滤风险：连续流出 ≥10日")


# 5. 基本面摘要
# ────────────────────────────────────────────────

def print_fundamentals(fund: Dict):
    """打印基本面关键信息"""
    if not fund:
        return
    print(f"\n{'═'*60}")
    print(f"  📋 基本面")
    print(f"{'═'*60}")
    # 提取顶层常见字段
    simple_fields = {
        'name': '名称', 'pe_ttm': 'PE(TTM)', 'roe': 'ROE(%)',
        'mcap_yi': '市值(亿)', 'industries': '行业',
    }
    for k, label in simple_fields.items():
        v = fund.get(k)
        if v is not None and v != '' and v != []:
            if isinstance(v, list):
                v = ' / '.join(str(x) for x in v)
            print(f"  {label:<14} {str(v)[:50]}")

    # 从嵌套结构中提取
    fh = fund.get('financial_health', {})
    if isinstance(fh, dict):
        for sub_k, label in [('rating', '财务评级'), ('debt_ratio', '负债率')]:
            v = fh.get(sub_k)
            if v is not None:
                print(f"  {label:<14} {str(v)[:50]}")

    moat = fund.get('moat', {}) or fund.get('moat_rating', '')
    if isinstance(moat, dict):
        moat = moat.get('rating', str(moat))
    if moat:
        print(f"  {'护城河':<14} {str(moat)[:50]}")


# ────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='单股深度分析 — 与选股系统完全对齐')
    parser.add_argument('code', type=str, help='股票代码，如 688630')
    args = parser.parse_args()

    code = args.code
    data = load_stock_data(code)
    df = data['kline']

    # 名称
    name = data.get('fundamentals', {}).get('name', code) if data.get('fundamentals') else code
    print(f"\n{'█'*60}")
    print(f"  🔍 {code} {name}  单股深度分析")
    print(f"  （评分体系与选股流水线完全一致）")
    print(f"{'█'*60}")

    # 1. 实时行情
    print_realtime_overlay(df)

    # 2. 技术评分
    print_tech_analysis(df)

    # 3. 基本面
    print_fundamentals(data.get('fundamentals', {}))

    # 3.5. 资金流
    mcap = data.get('fundamentals', {}).get('mcap_yi', 0) if data.get('fundamentals') else 0
    print_money_flow(code, mcap)

    # 4. 辩论
    # 构建 info 字典（与 run_debate_picker 一致）
    info = {
        'code': code,
        'name': name,
        'pe': data.get('fundamentals', {}).get('pe_ttm') if data.get('fundamentals') else None,
        'mcap': data.get('fundamentals', {}).get('mcap_yi', 0) if data.get('fundamentals') else 0,
        'market': 'mainboard',
        'industries': data.get('fundamentals', {}).get('industries', []) if data.get('fundamentals') else [],
        'industry_score': 0,
        'know_score': 0,
        'know_source': '',
        'total_score': compute_tech_score(df).total,
        'tech': compute_tech_score(df),
        'fundamentals': data.get('fundamentals'),
        'world_knowledge': None,
        '_kline': df,
    }
    # 世界知识库（与选股系统使用同一来源）
    try:
        from run_debate_picker import get_world_knowledge
        wk = get_world_knowledge()
        if code in wk:
            info['world_knowledge'] = wk[code]
    except Exception:
        pass

    print_debate(info)

    print(f"\n{'─'*60}")
    print(f"  ⚠️ 以上分析基于量化评分和辩论逻辑")
    print(f"  ⚠️ 评分体系与 run_debate_picker 选股流水线完全一致")
    print(f"  ⚠️ 不构成任何投资建议")
    print(f"{'─'*60}\n")


if __name__ == '__main__':
    main()
