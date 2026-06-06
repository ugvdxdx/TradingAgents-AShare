#!/usr/bin/env python3
"""
技术分析模块 —— 参考Vibe-Trading的技术指标体系

核心指标：
  1. 趋势判断：MA排列、MACD、Bollinger位置
  2. 动量：RSI、成交量变化率
  3. 形态：底部放量、突破均线
  4. 资金：成交量异动检测
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional, Tuple
from dataclasses import dataclass


@dataclass
class TechScore:
    """技术分析综合得分"""
    trend: float = 0       # 趋势得分 (0-35)
    momentum: float = 0    # 动量得分 (0-30)
    volume: float = 0      # 量能得分 (0-20)
    pattern: float = 0     # 形态得分 (0-15)
    total: float = 0


def calc_ma(df: pd.DataFrame, periods: list = [5, 10, 20, 60]) -> pd.DataFrame:
    """计算移动平均线"""
    for p in periods:
        if len(df) >= p:
            df[f'ma{p}'] = df['close'].rolling(window=p).mean()
    return df


def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """计算MACD"""
    if len(df) < slow:
        return df
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    df['macd_dif'] = ema_fast - ema_slow
    df['macd_dea'] = df['macd_dif'].ewm(span=signal, adjust=False).mean()
    df['macd_hist'] = 2 * (df['macd_dif'] - df['macd_dea'])
    return df


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算RSI"""
    if len(df) < period + 1:
        df['rsi'] = 50
        return df
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    return df


def calc_bollinger(df: pd.DataFrame, period: int = 20, std: int = 2) -> pd.DataFrame:
    """计算布林带"""
    if len(df) < period:
        return df
    df['bb_mid'] = df['close'].rolling(window=period).mean()
    bb_std = df['close'].rolling(window=period).std()
    df['bb_up'] = df['bb_mid'] + std * bb_std
    df['bb_low'] = df['bb_mid'] - std * bb_std
    df['bb_width'] = (df['bb_up'] - df['bb_low']) / (df['bb_mid'] + 1e-10)
    return df


def calc_volume_ratio(df: pd.DataFrame, period: int = 5) -> pd.DataFrame:
    """计算量比"""
    if len(df) < period:
        return df
    df['vol_ma5'] = df['volume'].rolling(window=period).mean()
    df['vol_ratio'] = df['volume'] / (df['vol_ma5'] + 1e-10)
    return df


def analyze_trend(df: pd.DataFrame) -> Tuple[float, str]:
    """
    趋势分析 (0-35分)
    
    判断标准：
    - MA多头排列（短>中>长）
    - 价格在MA上方
    - MACD金叉/死叉
    """
    if df is None or len(df) < 20:
        return 10, "数据不足"

    df = calc_ma(df, [5, 10, 20, 60])
    df = calc_macd(df)
    latest = df.iloc[-1]

    score = 17.5  # 基准

    # 1. MA多头排列判断 (12分)
    ma_bullish = 0
    if 'ma5' in df.columns and 'ma10' in df.columns:
        if latest['ma5'] > latest['ma10']:
            ma_bullish += 3
    if 'ma10' in df.columns and 'ma20' in df.columns:
        if latest['ma10'] > latest['ma20']:
            ma_bullish += 3
    if 'ma20' in df.columns and 'ma60' in df.columns:
        if latest['ma20'] > latest['ma60']:
            ma_bullish += 3
    if latest['close'] > latest.get('ma20', latest['close']):
        ma_bullish += 3
    score += (ma_bullish - 6)  # 调整范围 -6 ~ +6

    # 2. MACD状态 (10分)
    dif = latest.get('macd_dif', 0)
    dea = latest.get('macd_dea', 0)
    hist = latest.get('macd_hist', 0)

    if dif > dea and hist > 0:
        score += 5  # 金叉强势
    elif dif > dea and hist < 0:
        score += 2  # 收敛中
    elif dif < dea and hist < 0:
        score -= 3  # 死叉
    else:
        score -= 1  # 弱势

    # 3. 布林带位置 (8分)
    df = calc_bollinger(df)
    close = latest['close']
    bb_up = latest.get('bb_up', close * 1.1)
    bb_mid = latest.get('bb_mid', close)
    bb_low = latest.get('bb_low', close * 0.9)
    bb_width = latest.get('bb_width', 0.1)

    if close > bb_mid:
        score += 2  # 多头
    if close >= bb_up * 0.98:
        score += 1  # 强势突破
    if bb_width < 0.05:
        score += 2  # 收窄蓄势
    elif bb_width > 0.15:
        score -= 2  # 宽幅震荡

    # 4. 近20日趋势方向 (5分)
    if len(df) >= 20:
        close_20d_ago = df['close'].iloc[-20]
        if close > close_20d_ago * 1.05:
            score += 3
        elif close > close_20d_ago * 1.02:
            score += 2
        elif close > close_20d_ago * 0.98:
            score += 0
        elif close > close_20d_ago * 0.95:
            score -= 2
        else:
            score -= 3

    # 5. 均线乖离惩罚：短期偏离均线过大 → 回归风险
    if len(df) >= 20 and 'ma20' in df.columns:
        ma20 = latest['ma20']
        if ma20 > 0:
            deviation = (close - ma20) / ma20
            if deviation > 0.30:
                score -= 6   # 乖离30%+，严重超涨
            elif deviation > 0.20:
                score -= 3   # 乖离20%+，明显超涨
            elif deviation > 0.12:
                score -= 1   # 乖离12%+，偏离均值

    score = max(0, min(35, score))

    # 描述
    if score >= 28:
        desc = "多头强势"
    elif score >= 21:
        desc = "震荡偏多"
    elif score >= 14:
        desc = "震荡"
    elif score >= 7:
        desc = "震荡偏空"
    else:
        desc = "空头"

    return score, desc


def analyze_momentum(df: pd.DataFrame) -> Tuple[float, str]:
    """
    动量分析 (0-30分)
    
    判断标准：
    - RSI位置（不超买不超卖最佳）
    - 近N日涨幅加速度
    """
    if df is None or len(df) < 14:
        return 15, "数据不足"

    df = calc_rsi(df)
    latest = df.iloc[-1]

    score = 15  # 基准
    rsi = latest.get('rsi', 50)

    # 1. RSI评分 (15分) —— 渐进式超买/超卖惩罚
    if 45 <= rsi <= 65:
        score += 8  # 健康区间，趋势延续
    elif 65 < rsi <= 75:
        score += 5  # 偏强但未超买
    elif 40 <= rsi < 45:
        score += 3  # 偏弱
    elif 75 < rsi <= 80:
        score -= 3  # 明显超买
    elif 80 < rsi <= 85:
        score -= 6  # 严重超买，追高风险大
    elif rsi > 85:
        score -= 10  # 极度超买，历史高位风险
    elif rsi < 30:
        score -= 4  # 超卖弱势
    else:
        score += 0  # 正常

    # 2. 近期涨幅加速度 (10分)
    if len(df) >= 15:
        ret_5d = (latest['close'] - df['close'].iloc[-6]) / df['close'].iloc[-6]
        ret_10d = (latest['close'] - df['close'].iloc[-11]) / df['close'].iloc[-11]
        ret_15d = (latest['close'] - df['close'].iloc[-16]) / df['close'].iloc[-16]

        # 加速上涨 >> 匀速上涨 > 减速（但超买区加速不奖励）
        if ret_5d > ret_10d > ret_15d:
            if rsi < 75:
                score += 3  # 健康加速
            elif rsi < 80:
                score += 0  # 超买区加速，不加分
            else:
                score -= 2  # 过热区加速，警示追尾
        elif ret_5d > ret_10d:
            score += 1
        elif ret_5d * 2 < ret_10d and ret_5d < 0:
            score -= 3  # 加速下跌

    # 3. MACD柱子方向 (5分)
    if len(df) >= 5:
        hist_now = latest.get('macd_hist', 0)
        hist_5d = df.iloc[-6].get('macd_hist', 0) if len(df) >= 6 else 0
        if hist_now > hist_5d and hist_now > 0:
            score += 3  # 红柱放大
        elif hist_now < hist_5d and hist_now < 0:
            score -= 2  # 绿柱放大

    score = max(0, min(30, score))

    if score >= 24:
        desc = "动量强劲"
    elif score >= 18:
        desc = "动量偏多"
    elif score >= 10:
        desc = "动量中性"
    elif score >= 6:
        desc = "动量偏弱"
    else:
        desc = "动量衰竭"

    return score, desc


def analyze_volume(df: pd.DataFrame) -> Tuple[float, str]:
    """
    量能分析 (0-20分)
    
    判断标准：
    - 成交量异动（量比显著放大）
    - 价量配合（放量上涨最佳）
    - 缩量调整
    """
    if df is None or len(df) < 10:
        return 10, "数据不足"

    df = calc_volume_ratio(df)
    latest = df.iloc[-1]

    score = 10  # 基准

    vol_ratio = latest.get('vol_ratio', 1.0)
    close = latest['close']

    # 1. 量比分析 (10分)
    if vol_ratio > 2.0:
        score += 4  # 显著放量
    elif vol_ratio > 1.5:
        score += 2  # 温和放量
    elif vol_ratio > 0.8:
        score += 0  # 正常
    elif vol_ratio > 0.5:
        score -= 1  # 缩量
    else:
        score -= 2  # 极度缩量

    # 2. 价量配合 (7分)
    if len(df) >= 2:
        price_up = close > df.iloc[-2]['close']
        vol_up = vol_ratio > 1.2
        if price_up and vol_up:
            score += 4  # 放量上涨最佳
        elif price_up and not vol_up:
            score += 1  # 缩量上涨
        elif not price_up and not vol_up:
            score += 0  # 缩量调整
        elif not price_up and vol_up:
            score -= 2  # 放量下跌

    # 3. 近5日成交量趋势 (3分)
    if len(df) >= 5:
        vol_5d_ago = df['volume'].iloc[-6]
        vol_avg = df['volume'].iloc[-6:].mean()
        if latest['volume'] > vol_avg * 1.3:
            score += 1
        elif latest['volume'] < vol_avg * 0.5:
            score -= 1

    score = max(0, min(20, score))

    if score >= 15:
        desc = "放量活跃"
    elif score >= 11:
        desc = "量能正常"
    elif score >= 7:
        desc = "缩量"
    else:
        desc = "交投冷清"

    return score, desc


def analyze_pattern(df: pd.DataFrame) -> Tuple[float, str]:
    """
    形态分析 (0-15分)
    
    判断标准：
    - 底部放量突破特征
    - 连续阳线
    - 均线粘合后突破
    """
    if df is None or len(df) < 20:
        return 7, "数据不足"

    score = 7  # 基准

    # 1. 近5日涨跌比 (5分)
    if len(df) >= 5:
        recent = df.iloc[-5:]
        up_days = sum(1 for _, r in recent.iterrows() if r['close'] > r['open'])
        if up_days == 5:
            score += 4  # 连阳
        elif up_days == 4:
            score += 2
        elif up_days == 1:
            score -= 1
        elif up_days == 0:
            score -= 2

    # 2. 均线粘合度 (5分) —— 粘合后容易出方向
    df = calc_ma(df, [5, 10, 20])
    if all(col in df.columns for col in ['ma5', 'ma10', 'ma20']):
        latest = df.iloc[-1]
        ma_max = max(latest['ma5'], latest['ma10'], latest['ma20'])
        ma_min = min(latest['ma5'], latest['ma10'], latest['ma20'])
        ma_spread = (ma_max - ma_min) / (ma_min + 1e-10)

        if ma_spread < 0.02:
            score += 2  # 均线粘合，即将选择方向
        elif ma_spread > 0.15:
            score -= 1  # 均线发散

    # 3. 底部特征 (5分) —— 前期大跌后企稳
    if len(df) >= 40:
        ret_20d = (df['close'].iloc[-1] - df['close'].iloc[-21]) / df['close'].iloc[-21]
        ret_20d_ago = (df['close'].iloc[-20] - df['close'].iloc[-40]) / df['close'].iloc[-40]
        if ret_20d_ago < -0.10 and ret_20d > 0:
            score += 3  # 前期大跌+近期企稳
        elif ret_20d_ago < -0.05 and ret_20d > 0.02:
            score += 1

    score = max(0, min(15, score))

    if score >= 12:
        desc = "突破形态"
    elif score >= 8:
        desc = "形态良好"
    elif score >= 5:
        desc = "形态一般"
    else:
        desc = "形态弱势"

    return score, desc


def compute_tech_score(df: pd.DataFrame) -> TechScore:
    """综合技术评分 (满分100)"""
    if df is None or len(df) < 20:
        return TechScore()

    trend_score, trend_desc = analyze_trend(df)
    momentum_score, mom_desc = analyze_momentum(df)
    volume_score, vol_desc = analyze_volume(df)
    pattern_score, pat_desc = analyze_pattern(df)

    total = trend_score + momentum_score + volume_score + pattern_score

    return TechScore(
        trend=trend_score,
        momentum=momentum_score,
        volume=volume_score,
        pattern=pattern_score,
        total=round(total, 1),
    )


def batch_tech_analysis(kline_data: Dict[str, pd.DataFrame]) -> Dict[str, TechScore]:
    """批量技术分析"""
    results = {}
    for symbol, df in kline_data.items():
        try:
            results[symbol] = compute_tech_score(df)
        except Exception as e:
            results[symbol] = TechScore()
    return results


if __name__ == "__main__":
    # 测试
    from data_cache import KlineCache
    cache = KlineCache("kline_cache")
    klines = cache.batch_fetch(["000636.SZ", "002281.SZ", "300456.SZ", "600519.SH", "688697.SH"],
                               count=60)

    for sym, df in klines.items():
        ts = compute_tech_score(df)
        print(f"\n{sym}: 趋势{ts.trend}/35 动量{ts.momentum}/30 量能{ts.volume}/20 形态{ts.pattern}/15 = 总分{ts.total}/100")