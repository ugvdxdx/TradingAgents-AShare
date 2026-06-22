#!/usr/bin/env python3
"""构建 price_factor 历史快照 (14 个变体 + 基线, 无前视, 供回测批量对比)。

price_factor 是 capital 的个股量价乘子 (0.6~1.3):
    capital = base_capital(板块动量) × price_factor(个股量价)

当前生产只用了 r5/r20 (close 的 2 个点)。本脚本实现 14 个更丰富的变体,
覆盖量价(量能/振幅/影线)、个股资金流(净流入/加速/占比)、资金流层结构(背离/主导)、
行业资金流(共振/相对强度), 一次性生成全部版本供回测筛选。

输出: data/caches/price_factor_history.json
    {cutoff: {variant_name: {code: factor}}}
    增量: 已算的 (cutoff, variant) 跳过。

⚠ 数据依赖:
    - K线: kline_cache/*.pkl (已补到 300 根)
    - 个股资金流: .mf_cache/mf.pkl {code: [daily_rows]} (纯 code key)
    - 行业资金流: .mf_cache/board_flow_history.pkl {date: [{industry, main_net_yi}]}
    资金流/行业资金流缺失时, 对应变体自动跳过 (不报错), 该 variant 返回空。

用法:
    uv run python3 scripts/build_price_factor_history.py                  # 全部
    uv run python3 scripts/build_price_factor_history.py --step 5         # 每周一个 cutoff
    uv run python3 scripts/build_price_factor_history.py --start 2025-04  # 从该日起
    uv run python3 scripts/build_price_factor_history.py --variants A1,A2  # 指定变体
"""
import argparse
import glob
import json
import os
import pickle
import statistics
import sys
import time
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths

KLINE_DIR = paths.KLINE_CACHE_DIR
MF_DIR = paths.MF_CACHE_DIR
V3 = json.load(open(paths.V3_CACHE))
OUTPUT = os.path.join(paths.CACHES_DIR, "price_factor_history.json")
# 行业资金流历史: 用户从个股资金流汇总, 格式 {date(无横线): [{industry, main_net_yi}]}
BOARD_FLOW_HISTORY = os.path.join(MF_DIR, "board_flow_history.pkl")

CLAMP_MIN, CLAMP_MAX = 0.6, 1.3


def clamp(x: float) -> float:
    return round(max(CLAMP_MIN, min(CLAMP_MAX, x)), 3)


# ══════════════════════════════════════════════════════════
# 数据读取 (全部 cutoff 化, 无前视)
# ══════════════════════════════════════════════════════════

def load_kline_cut(code: str, cutoff: str) -> Optional[pd.DataFrame]:
    """读 K 线并截断到 cutoff, 返回 DataFrame 或 None(数据不足)。"""
    suffix = "_SH.pkl" if code.startswith("6") else "_SZ.pkl"
    p = os.path.join(KLINE_DIR, f"{code}{suffix}")
    if not os.path.exists(p):
        return None
    try:
        df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
        df = df[df["trade_date"] <= cutoff]
        if len(df) < 21:
            return None
        return df
    except Exception:
        return None


# 资金流缓存: 支持 per-stock pkl 和聚合 mf.pkl (dict[code, list])
_MF_AGG_CACHE: Optional[dict] = None  # 聚合文件一次性加载
_MF_PERSTOCK_CACHE: Dict[str, list] = {}


def _load_mf_aggregate() -> dict:
    """加载聚合资金流文件 mf.pkl → {code: list[record]}。单次加载全局缓存。"""
    global _MF_AGG_CACHE
    if _MF_AGG_CACHE is not None:
        return _MF_AGG_CACHE
    _MF_AGG_CACHE = {}
    # 优先读 mf.pkl (聚合), 也兼容 dated snapshot
    cands = sorted(glob.glob(os.path.join(MF_DIR, "mf_*.pkl")), reverse=True)
    agg_path = os.path.join(MF_DIR, "mf.pkl")
    if os.path.exists(agg_path):
        cands = [agg_path] + cands
    for p in cands:
        try:
            data = pickle.load(open(p, "rb"))
            if isinstance(data, dict):
                # 合并多个文件: 同一 code 取最长的 list (历史最全)
                for k, v in data.items():
                    if not isinstance(v, list):
                        continue
                    # key 可能是 "000001" 或 "000001_60"
                    code = k.rsplit("_", 1)[0] if "_" in k else k
                    if code not in _MF_AGG_CACHE or len(v) > len(_MF_AGG_CACHE[code]):
                        _MF_AGG_CACHE[code] = v
        except Exception:
            continue
    return _MF_AGG_CACHE


def _load_mf_perstock(code: str) -> list:
    """读单股资金流历史。优先 per-stock 文件, 回退聚合 mf.pkl。"""
    if code in _MF_PERSTOCK_CACHE:
        return _MF_PERSTOCK_CACHE[code]
    rows = []
    # 1. per-stock 文件
    for cand in [f"{code}.pkl", f"{code}_300.pkl", f"{code}_60.pkl"]:
        p = os.path.join(MF_DIR, cand)
        if os.path.exists(p):
            try:
                data = pickle.load(open(p, "rb"))
                if isinstance(data, list):
                    rows = data
                elif isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, list) and len(v) > len(rows):
                            rows = v
                break
            except Exception:
                pass
    # 2. 回退聚合文件
    if not rows:
        agg = _load_mf_aggregate()
        rows = agg.get(code, [])
    _MF_PERSTOCK_CACHE[code] = rows
    return rows


def load_mf_cut(code: str, cutoff: str) -> Optional[list]:
    """读个股资金流并截断到 cutoff。返回 list[record] 或 None。"""
    rows = _load_mf_perstock(code)
    if not rows:
        return None
    cutoff_c = cutoff.replace("-", "")
    out = []
    for r in rows:
        d = str(r.get("date", ""))
        dc = d.replace("-", "") if "-" in d else d
        if dc <= cutoff_c:
            out.append({**r, "date_c": dc})
    return out if len(out) >= 10 else None


# 行业资金流历史
_BOARD_FLOW_CACHE: Optional[dict] = None


def load_board_flow_at(cutoff: str) -> Dict[str, float]:
    """读 cutoff 当天的行业资金流 → {sector_name: main_net_yi}。

    数据源 .mf_cache/board_flow_history.pkl, 格式 {date: [{industry, main_net_yi}]}。
    转成 {industry: main_net_yi} 返回; 查 cutoff 当天或最近的一天 ≤ cutoff。
    """
    global _BOARD_FLOW_CACHE
    if _BOARD_FLOW_CACHE is None:
        _BOARD_FLOW_CACHE = {}
        if os.path.exists(BOARD_FLOW_HISTORY):
            try:
                raw = pickle.load(open(BOARD_FLOW_HISTORY, "rb"))
                # {date: [{industry, main_net_yi}]} → {date: {industry: main_net_yi}}
                for d, rows in raw.items():
                    if isinstance(rows, list):
                        _BOARD_FLOW_CACHE[d] = {
                            r["industry"]: r["main_net_yi"] for r in rows
                            if isinstance(r, dict) and "industry" in r
                        }
            except Exception:
                pass
    # 查 cutoff 当天 (或最近的一天 ≤ cutoff)
    cutoff_c = cutoff.replace("-", "")
    best_date, best = "", {}
    for d, sectors in _BOARD_FLOW_CACHE.items():
        dc = d.replace("-", "")
        if dc <= cutoff_c and dc > best_date:
            best_date, best = dc, sectors
    return best


# 行业归类 (code → 标准板块名 / 原始 industry 名)
_KW_INDEX = None
_INDUSTRY_MAP: Dict[str, str] = {}       # code → 标准板块名 (keyword归类)
_INDUSTRY_RAW_MAP: Dict[str, str] = {}   # code → 原始 industry 字段 (匹配 board_flow)


def _read_industry(code: str) -> str:
    """读个股 fundamentals 的原始 industry 字段 (在 business_overview.industry 下)。"""
    for d in (paths.FUNDAMENTALS_DIR, paths.FUNDAMENTALS_COLD_DIR):
        p = os.path.join(d, f"{code}.json")
        if os.path.exists(p):
            try:
                fd = json.load(open(p, encoding="utf-8"))
                return (fd.get("industry", "") or
                        fd.get("business_overview", {}).get("industry", ""))
            except Exception:
                pass
            break
    return ""


def get_sector(code: str) -> str:
    """个股 fundamentals.industry → 标准板块 (复用 keyword index)。"""
    global _KW_INDEX
    if code in _INDUSTRY_MAP:
        return _INDUSTRY_MAP[code]
    if _KW_INDEX is None:
        try:
            from tradingagents.research.normalize import get_sector_keyword_index
            _KW_INDEX = get_sector_keyword_index()
        except Exception:
            _KW_INDEX = {}
    industry = _read_industry(code)
    _INDUSTRY_RAW_MAP[code] = industry  # 顺带缓存原始名
    best, best_hit, best_kw_len = "", 0, 0
    for sec, kws in _KW_INDEX.items():
        matched = [k for k in kws if k in industry]
        h = len(matched)
        if h <= 0:
            continue
        max_kw_len = max(len(k) for k in matched)
        if h > best_hit or (h == best_hit and max_kw_len > best_kw_len):
            best_hit, best_kw_len, best = h, max_kw_len, sec
    _INDUSTRY_MAP[code] = best
    return best


def get_industry_raw(code: str) -> str:
    """个股的原始 industry 名 (与 board_flow_history 的 industry 字段精确匹配)。"""
    if code not in _INDUSTRY_RAW_MAP:
        _INDUSTRY_RAW_MAP[code] = _read_industry(code)
    return _INDUSTRY_RAW_MAP[code]


# ══════════════════════════════════════════════════════════
# price_factor 变体定义
# 每个变体: fn(df_kline, mf_rows, board_flow, code) → float (0.6~1.3)
# df_kline/mf_rows/board_flow 为 None 时该变体返回 None (跳过)
# ══════════════════════════════════════════════════════════

def _r5r20(df) -> Optional[tuple]:
    """基础 r5/r20 计算, 供多个变体复用。"""
    if df is None or len(df) < 21:
        return None
    close = df["close"]
    r20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
    r5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0
    return r5, r20


# ── 基线 ──
def f_baseline(df, mf, bf, code):
    """当前生产公式: r5/r20 双窗口。"""
    rv = _r5r20(df)
    if rv is None:
        return None
    r5, r20 = rv
    if r20 > 20:
        return 1.3 if r5 > 5 else (0.9 if r5 < -5 else 1.1)
    elif r20 > 0:
        return clamp(1.0 + r20 * 0.01) if r5 > 0 else 0.9
    elif r20 > -10:
        return 0.9 if r5 > 0 else 0.7
    else:
        return 0.6


# ── A 组: 纯量价增强 ──
def f_A1_vol_confirm(df, mf, bf, code):
    """A1 量价同向: 价涨 + 5日均量/20日均量>1.2 → 加成; 价涨量缩 → 减分。"""
    rv = _r5r20(df)
    if rv is None or "volume" not in df.columns:
        return None
    r5, r20 = rv
    vol5 = df["volume"].iloc[-5:].mean()
    vol20 = df["volume"].iloc[-20:].mean()
    vol_ratio = vol5 / vol20 if vol20 > 0 else 1.0
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if r20 > 0:  # 价涨
        return clamp(base * (1.15 if vol_ratio > 1.2 else (0.9 if vol_ratio < 0.8 else 1.0)))
    return base


def f_A2_vol_price_breakout(df, mf, bf, code):
    """A2 放量突破: 近5日amount均/20日均 + close创20日新高 → 判主升。"""
    rv = _r5r20(df)
    if rv is None or "amount" not in df.columns:
        return None
    r5, r20 = rv
    amt5 = df["amount"].iloc[-5:].mean()
    amt20 = df["amount"].iloc[-20:].mean()
    amt_ratio = amt5 / amt20 if amt20 > 0 else 1.0
    close = df["close"]
    # 创新高: 当前 close >= 前20根(不含当前)的最高价 × 0.98
    prior_high = close.iloc[-21:-1].max() if len(close) >= 21 else close.iloc[:-1].max()
    is_new_high = close.iloc[-1] >= prior_high * 0.98
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if amt_ratio > 1.3 and is_new_high:
        return clamp(base * 1.2)  # 放量创新高, 强主升
    elif amt_ratio < 0.7 and r20 > 10:
        return clamp(base * 0.85)  # 缩量上涨, 虚胖
    return base


def f_A3_range_quality(df, mf, bf, code):
    """A3 振幅健康度: 近5日(high-low)/close 波动率, 回撤小=健康, 宽震荡=不稳。"""
    rv = _r5r20(df)
    if rv is None or "high" not in df.columns or "low" not in df.columns:
        return None
    r5, r20 = rv
    last5 = df.iloc[-5:]
    ranges = (last5["high"] - last5["low"]) / last5["close"] * 100
    avg_range = ranges.mean()
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if r20 > 0:  # 涨势中
        if avg_range < 3:        # 回撤小, 健康
            return clamp(base * 1.1)
        elif avg_range > 8:      # 宽震荡, 不稳
            return clamp(base * 0.85)
    return base


def f_A4_candle_strength(df, mf, bf, code):
    """A4 K线实体强度: (close-open)/open 近5日累计, 正实体多=多方主导。"""
    rv = _r5r20(df)
    if rv is None or "open" not in df.columns:
        return None
    r5, r20 = rv
    last5 = df.iloc[-5:]
    bodies = (last5["close"] - last5["open"]) / last5["open"] * 100
    pos_count = (bodies > 0).sum()
    body_sum = bodies.sum()
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if pos_count >= 4 and body_sum > 2:   # 多数阳线且累计实体强
        return clamp(base * 1.12)
    elif pos_count <= 1 and body_sum < -2:  # 多数阴线, 空方主导
        return clamp(base * 0.85)
    return base


# ── B 组: 个股资金流确认 (main_net 层面) ──
def _mf_summary(mf):
    """资金流摘要: main_net近10日累计 / 近5日均 / 近20日均 / main_pct均值。"""
    if mf is None or len(mf) < 10:
        return None
    mn10 = sum(r.get("main_net", 0) for r in mf[-10:])
    mn5_avg = sum(r.get("main_net", 0) for r in mf[-5:]) / 5
    mn20_avg = sum(r.get("main_net", 0) for r in mf[-20:]) / min(20, len(mf))
    pct_avg = sum(r.get("main_pct", 0) for r in mf[-10:]) / 10
    return {"mn10": mn10, "mn5_avg": mn5_avg, "mn20_avg": mn20_avg, "pct_avg": pct_avg}


def f_B1_inflow_confirm(df, mf, bf, code):
    """B1 主力净流入确认: 价涨 + main_net近10日>0 → 加成; 价涨但流出 → 减分。"""
    rv = _r5r20(df)
    ms = _mf_summary(mf)
    if rv is None or ms is None:
        return None
    r5, r20 = rv
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if r20 > 0:  # 价涨时资金流才有确认意义
        if ms["mn10"] > 0:      # 价涨 + 主力流入 = 量价资金共振
            return clamp(base * 1.15)
        else:                   # 价涨但主力流出 = 出货 (上次验证 +8.1pp 分化)
            return clamp(base * 0.85)
    return base


def f_B2_inflow_momentum(df, mf, bf, code):
    """B2 资金流入加速: 近5日main_net均 > 近20日均 → 加速进场。"""
    rv = _r5r20(df)
    ms = _mf_summary(mf)
    if rv is None or ms is None:
        return None
    r5, r20 = rv
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    # 近5日均显著大于近20日均 = 资金加速 (相对变化>50%)
    if ms["mn20_avg"] != 0:
        accel_ratio = ms["mn5_avg"] / ms["mn20_avg"]
    else:
        accel_ratio = 2.0 if ms["mn5_avg"] > 0 else 0.0
    if accel_ratio > 1.5 and ms["mn5_avg"] > 0:
        return clamp(base * 1.15)
    elif accel_ratio < 0.3 and ms["mn5_avg"] < 0:
        return clamp(base * 0.85)
    return base


def f_B3_inflow_pct(df, mf, bf, code):
    """B3 主力净占比: main_pct 高=主力主导行情(跨市值可比)。"""
    rv = _r5r20(df)
    ms = _mf_summary(mf)
    if rv is None or ms is None:
        return None
    r5, r20 = rv
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    pct = ms["pct_avg"]
    if r20 > 0:  # 涨势中主力占比才有意义
        if pct > 5:           # 主力强力主导
            return clamp(base * 1.15)
        elif pct < -3:        # 主力明显流出占比
            return clamp(base * 0.85)
    return base


# ── C 组: 资金流层背离 (5档结构) ──
def f_C1_tier_divergence(df, mf, bf, code):
    """C1 主力散户背离: (super_large+large)与(medium+small)符号相反。"""
    rv = _r5r20(df)
    if rv is None or mf is None or len(mf) < 10:
        return None
    r5, r20 = rv
    recent = mf[-10:]
    inst_net = sum(r.get("super_large", 0) + r.get("large", 0) for r in recent)
    retail_net = sum(r.get("medium", 0) + r.get("small", 0) for r in recent)
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if inst_net > 0 and retail_net < 0:
        # 主力买散户卖 = 主力吸筹 (机会)
        return clamp(base * 1.15)
    elif inst_net < 0 and retail_net > 0:
        # 主力卖散户买 = 主力出货 (危险)
        return clamp(base * 0.8)
    return base


def f_C2_super_large_dominance(df, mf, bf, code):
    """C2 超大单主导度: super_large/main_net 占比高=机构主导(持续性强)。"""
    rv = _r5r20(df)
    if rv is None or mf is None or len(mf) < 10:
        return None
    r5, r20 = rv
    recent = mf[-10:]
    main_net = sum(r.get("main_net", 0) for r in recent)
    super_large = sum(r.get("super_large", 0) for r in recent)
    base = f_baseline(df, mf, bf, code)
    if base is None or main_net == 0:
        return base
    sl_ratio = super_large / main_net
    if sl_ratio > 0.7 and main_net > 0:
        # 超大单(机构)主导净流入, 持续性强
        return clamp(base * 1.12)
    elif sl_ratio < 0.2 and main_net > 0:
        # 净流入但超大单占比低 (游资/大户主导, 持续性弱)
        return clamp(base * 0.92)
    return base


# ── D 组: 行业资金流共振 ──
def f_D1_sector_resonance(df, mf, bf, code):
    """D1 个股-行业共振: 行业流入 + 个股流入 → 强共振加成。

    board_flow 的 industry 是 fundamentals 原始行业名 (如"通信设备（数通光模块）"),
    用 get_industry_raw 精确匹配 (而非 keyword 归类的标准板块名)。
    """
    rv = _r5r20(df)
    ms = _mf_summary(mf)
    industry = get_industry_raw(code)
    if rv is None or ms is None or not industry or not bf:
        return None
    r5, r20 = rv
    sector_net = bf.get(industry, 0)
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    if sector_net > 0 and ms["mn10"] > 0:
        # 行业资金流入 + 个股资金流入 = 强共振
        return clamp(base * 1.18)
    elif sector_net > 0 and ms["mn10"] < 0:
        # 行业流入但个股流出 = 弱势股 (跑输板块)
        return clamp(base * 0.82)
    return base


def f_D2_sector_relative(df, mf, bf, code):
    """D2 行业相对强度: 个股 r20 vs 同行业个股的 r20 中位数。

    不依赖 board_flow 的字段(避免 schema 不匹配), 而是从 V3 池里同行业股现算中位。
    个股显著强于行业中位 = 独立 alpha; 显著弱 = 跟涨乏力。
    """
    rv = _r5r20(df)
    sector = get_sector(code)
    if rv is None or not sector:
        return None
    r5, r20 = rv
    base = f_baseline(df, mf, bf, code)
    if base is None:
        return None
    # 同行业其他股的 r20 中位数 (用模块级缓存避免重复算)
    sector_median_r20 = _sector_median_r20(sector, "")  # cutoff 仅用于日志, 实际从缓存读
    if sector_median_r20 is None:
        return base
    if r20 > sector_median_r20 + 15:
        return clamp(base * 1.15)  # 显著强于行业 = 独立 alpha
    elif r20 < sector_median_r20 - 10:
        return clamp(base * 0.85)  # 跑输行业
    return base


# D2 辅助: 每期每行业 r20 中位数缓存 (随 cutoff 刷新)
_SECTOR_R20_CACHE: Dict[str, float] = {}


def _sector_median_r20(sector: str, cutoff: str) -> Optional[float]:
    """计算该行业个股在 cutoff 的 r20 中位数 (用 _SECTOR_R20_CACHE, 主循环按 cutoff 刷新)。"""
    return _SECTOR_R20_CACHE.get(sector)


def _build_sector_r20_cache(cutoff: str):
    """主循环每个 cutoff 调一次: 预算所有行业的 r20 中位数。"""
    _SECTOR_R20_CACHE.clear()
    sector_r20s: Dict[str, list] = {}
    for code, v in V3.items():
        if not isinstance(v, dict) or "chain" not in v:
            continue
        sec = get_sector(code)
        if not sec:
            continue
        df = load_kline_cut(code, cutoff)
        rv = _r5r20(df) if df is not None else None
        if rv is None:
            continue
        sector_r20s.setdefault(sec, []).append(rv[1])
    for sec, vals in sector_r20s.items():
        if vals:
            _SECTOR_R20_CACHE[sec] = statistics.median(vals)


# 变体注册表
VARIANTS = {
    "baseline_r5r20": (f_baseline, "基线: r5/r20双窗口"),
    "A1_vol_confirm": (f_A1_vol_confirm, "量价同向"),
    "A2_vol_price_breakout": (f_A2_vol_price_breakout, "放量突破"),
    "A3_range_quality": (f_A3_range_quality, "振幅健康度"),
    "A4_candle_strength": (f_A4_candle_strength, "K线实体强度"),
    "B1_inflow_confirm": (f_B1_inflow_confirm, "主力净流入确认"),
    "B2_inflow_momentum": (f_B2_inflow_momentum, "资金流入加速"),
    "B3_inflow_pct": (f_B3_inflow_pct, "主力净占比"),
    "C1_tier_divergence": (f_C1_tier_divergence, "主力散户背离"),
    "C2_super_large_dominance": (f_C2_super_large_dominance, "超大单主导度"),
    "D1_sector_resonance": (f_D1_sector_resonance, "个股-行业共振"),
    "D2_sector_relative": (f_D2_sector_relative, "行业相对强度"),
}

# build_capital_at 需要的共享资源 (懒加载, 避免每次 cutoff 重建)
_KW_INDEX_FOR_BUILD = None
_OVERRIDE_SORTED_FOR_BUILD = None


def _get_kw_index_for_build():
    global _KW_INDEX_FOR_BUILD
    if _KW_INDEX_FOR_BUILD is None:
        try:
            from tradingagents.research.normalize import get_sector_keyword_index
            _KW_INDEX_FOR_BUILD = get_sector_keyword_index()
        except Exception:
            _KW_INDEX_FOR_BUILD = {}
    return _KW_INDEX_FOR_BUILD


def _get_override_sorted():
    global _OVERRIDE_SORTED_FOR_BUILD
    if _OVERRIDE_SORTED_FOR_BUILD is None:
        try:
            from picker.scoring.v3_full_score import _load_sub_sector_override
            _OVERRIDE_SORTED_FOR_BUILD = sorted(
                _load_sub_sector_override().items(), key=lambda x: -len(x[0]))
        except Exception:
            _OVERRIDE_SORTED_FOR_BUILD = []
    return _OVERRIDE_SORTED_FOR_BUILD


# ══════════════════════════════════════════════════════════
# 生成 cutoff 列表
# ══════════════════════════════════════════════════════════

def get_cutoff_dates(step: int, start: str = "") -> list:
    df = pickle.load(open(os.path.join(KLINE_DIR, "300308_SZ.pkl"), "rb"))
    dates = sorted(df["trade_date"].unique())
    cutoffs = [d for d in dates if not start or d >= start]
    valid = [d for d in cutoffs if 20 <= dates.index(d) <= len(dates) - 31]
    return valid[::step]


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="构建 price_factor 历史快照 (14变体+基线)")
    parser.add_argument("--step", type=int, default=5, help="cutoff采样步长(交易日)")
    parser.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--variants", default="", help="指定变体(逗号分隔, 默认全部)")
    args = parser.parse_args()

    active_variants = (args.variants.split(",") if args.variants
                       else list(VARIANTS.keys()))

    print("=" * 64)
    print(f"  price_factor 历史快照 ({len(active_variants)} 个变体)")
    print("=" * 64)
    print(f"  变体: {active_variants}")

    cutoffs = get_cutoff_dates(args.step, args.start)
    print(f"  cutoff 数: {len(cutoffs)} ({cutoffs[0]}~{cutoffs[-1]})")

    # 增量: 加载已有
    history = {}
    if os.path.exists(OUTPUT):
        try:
            history = json.load(open(OUTPUT, encoding="utf-8"))
        except Exception:
            history = {}

    codes = [c for c, v in V3.items() if isinstance(v, dict) and "chain" in v]
    print(f"  股票数: {len(codes)}")

    t0 = time.time()
    for ci, cutoff in enumerate(cutoffs, 1):
        # 跳过已完整的 cutoff (所有变体 + base_capital 都算过)
        existing = history.get(cutoff, {})
        if all(v in existing and existing[v] for v in active_variants) and "_base_capital" in existing:
            continue

        bf = load_board_flow_at(cutoff)
        # 预算 D2 的行业 r20 中位数缓存 (按 cutoff)
        _build_sector_r20_cache(cutoff)
        # 同时算 base_capital (剥离 price_factor 的纯板块动量分), 供 eval 用
        # 复用 build_capital_history 的逻辑: base_capital 不含 price_factor
        from scripts.build_capital_history import build_capital_at
        base_caps = build_capital_at(cutoff, V3, _get_kw_index_for_build(), _get_override_sorted())
        per_variant: Dict[str, Dict[str, float]] = {v: {} for v in active_variants}
        per_variant["_base_capital"] = base_caps  # 特殊 key, eval 用它 × pf_variant
        for code in codes:
            df = load_kline_cut(code, cutoff)
            mf = load_mf_cut(code, cutoff)
            for vname in active_variants:
                fn = VARIANTS[vname][0]
                try:
                    factor = fn(df, mf, bf, code)
                except Exception:
                    factor = None
                if factor is not None:
                    per_variant[vname][code] = factor

        history[cutoff] = {**existing, **per_variant}
        # 进度 + 各变体覆盖率
        coverage = {v: len(per_variant[v]) for v in active_variants}
        print(f"  [{ci}/{len(cutoffs)}] {cutoff} | base_cap {len(base_caps)}只 | 变体覆盖: {coverage}")

        # 每 5 个 cutoff 落盘
        if ci % 5 == 0:
            json.dump(history, open(OUTPUT, "w", encoding="utf-8"))

    json.dump(history, open(OUTPUT, "w", encoding="utf-8"))
    elapsed = time.time() - t0
    print(f"\n  ✓ 完成: {len(cutoffs)} cutoff × {len(active_variants)} 变体, 耗时 {elapsed:.0f}s")
    print(f"  存储: {OUTPUT}")


if __name__ == "__main__":
    main()
