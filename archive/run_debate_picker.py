#!/usr/bin/env python3
"""
多轮辩论选股系统 v3.0
═══════════════════════════════════════════════════════════
流水线：
  召回 (v7 评分 Top100)
    → 第一轮辩论: 100 → 50 (行业分散 + 基本面过滤)
    → 第二轮辩论: 50 → 30 (竞争壁垒 + 成长性)
    → 第三轮辩论: 30 → 20 (技术面 + 估值合理性)
    → 第四轮辩论: 20 → 10 (综合博弈 → 最终推荐)

验证：10 个交易日后结算收益

辩论机制 v3：
  - 交互式辩论：Bull陈述→Bear反驳→Bear陈述→Bull反驳
  - 反驳机制：语义对立 + 数据对比 + 权重衰减
  - 投降机制：核心论据压制 / 连续被反驳 / 信息缺失 / 权重碾压
  - 世界知识深度引用：区分真实数据与模板填充
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_knowledge_base import lookup_knowledge
from data_cache import KlineCache
from fundamental_scorer import compute_fundamental_knowledge
from tech_analysis import TechScore, compute_tech_score

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

CACHE_DIR = "kline_cache"
WHITELIST_FILE = "stock_whitelist.json"
FUNDAMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fundamentals')


# ══════════════════════════════════════════════════════════
# 1. 世界知识
# ══════════════════════════════════════════════════════════

_WORLD_KNOWLEDGE: Optional[Dict] = None

def get_world_knowledge() -> Dict:
    global _WORLD_KNOWLEDGE
    if _WORLD_KNOWLEDGE is None:
        from world_knowledge import BUSINESS_WORLD_KNOWLEDGE
        _WORLD_KNOWLEDGE = BUSINESS_WORLD_KNOWLEDGE
    return _WORLD_KNOWLEDGE


# ══════════════════════════════════════════════════════════
# 2. 数据结构
# ══════════════════════════════════════════════════════════

@dataclass
class DebateArg:
    """一条辩论论据"""
    point: str              # 论点
    evidence: str           # 证据/数据支撑
    weight: float           # 权重 (0-10)
    source: str = ""        # fundamentals / world_knowledge / tech / valuation
    has_data: bool = False  # 是否包含量化数据
    refuted: bool = False
    refutation: str = ""
    refuted_by: str = ""
    weight_after_refute: float = 0

@dataclass
class DebateExchange:
    """一轮交互式辩论"""
    round_num: int
    bull_statement: str = ""
    bear_rebuttal: str = ""
    bear_statement: str = ""
    bull_rebuttal: str = ""
    bear_surrenders: bool = False
    bull_surrenders: bool = False
    surrender_reason: str = ""

@dataclass
class DebateRecord:
    """单只股票的完整辩论记录"""
    code: str
    name: str
    bull_args: List[DebateArg] = field(default_factory=list)
    bear_args: List[DebateArg] = field(default_factory=list)
    exchanges: List[DebateExchange] = field(default_factory=list)
    bull_surrendered: bool = False
    bear_surrendered: bool = False
    surrender_reason: str = ""
    bull_score: float = 0
    bear_score: float = 0
    judge_score: float = 0
    judge_verdict: str = ""
    eliminated: bool = False
    eliminate_reason: str = ""


# ══════════════════════════════════════════════════════════
# 3. 工具函数
# ══════════════════════════════════════════════════════════

_QUANT_PATTERNS = [
    r'\d+\.?\d*%', r'\d+\.?\d*亿', r'\d+\.?\d*万', r'\d+\.?\d*倍',
    r'\d+\.?\d*元', r'增长\d+', r'突破\d+', r'超\d+', r'\d+\.?\d*pp',
]

def has_quantitative_data(text: str) -> bool:
    return any(re.search(p, text) for p in _QUANT_PATTERNS)

def is_template_text(text: str, name: str) -> bool:
    prefixes = [f"{name}{s}" for s in
                ["行业竞争激烈", "受宏观经济", "原材料成本", "技术迭代",
                 "受益国产替代", "海外市场", "技术创新", "产业链整合"]]
    return any(text.startswith(p) for p in prefixes)

def weight_for_wk(text: str, name: str, base_with_data: int, base_no_data: int) -> float:
    """世界知识论据权重：有量化数据高权重，模板填充极低权重"""
    tmpl = is_template_text(text, name)
    hd = has_quantitative_data(text)
    if tmpl and not hd:
        return 1
    if hd:
        return base_with_data
    if tmpl:
        return 2
    return base_no_data


# ══════════════════════════════════════════════════════════
# 4. 股票信息聚合
# ══════════════════════════════════════════════════════════

def gather_stock_info(stock: Dict) -> Dict:
    code = stock['code']
    info = {
        'code': code, 'name': stock['name'],
        'pe': stock.get('pe_ttm'), 'mcap': stock.get('mcap_yi', 0) or 0,
        'market': stock.get('market', 'mainboard'),
        'industries': stock.get('industries', []),
        'industry_score': stock.get('industry_score', 0),
        'know_score': stock.get('_know_score', 0),
        'know_source': stock.get('_know_source', ''),
        'total_score': stock.get('total_score', 0),
        'tech': stock.get('tech', TechScore()),
        'fundamentals': None, 'world_knowledge': None,
        '_kline': stock.get('_kline'),  # 趋势调节需要
    }
    fund_path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if os.path.exists(fund_path):
        try:
            with open(fund_path, 'r', encoding='utf-8') as f:
                info['fundamentals'] = json.load(f)
        except Exception:
            pass
    wk = get_world_knowledge()
    if code in wk:
        info['world_knowledge'] = wk[code]
    return info


# ══════════════════════════════════════════════════════════
# 5. 召回 — v7 评分选出 TopN
# ══════════════════════════════════════════════════════════

def score_v7(stock: Dict, tech: TechScore) -> float:
    pe = stock.get('pe_ttm')
    market = stock.get('market', 'mainboard')

    fund_score = compute_fundamental_knowledge(stock['code'], stock.get('name', ''))
    if fund_score is not None:
        know_score = fund_score
        stock['_know_source'] = 'fundamentals'
    else:
        is_score = stock.get('industry_score', 0)
        know_score = max(4, min(16, int(is_score * 1.6))) if is_score >= 4 else 4
        stock['_know_source'] = 'industry'

    stock['_know_score'] = know_score
    tech_score = tech.total * 0.30

    if pe and 15 <= pe <= 80: pe_score = 18
    elif pe and 0 < pe < 15: pe_score = 10
    elif pe and 80 < pe <= 200: pe_score = 14
    elif pe and pe > 200: pe_score = 8
    elif pe is not None and pe < 0: pe_score = 5
    else: pe_score = 10

    market_bonus = 10 if market == 'star' else (6 if market == 'gem' else 0)
    return know_score + tech_score + pe_score + market_bonus


def recall_top_n(n: int = 100) -> List[Dict]:
    logger.info(f"═══ 阶段0: 召回 Top{n} ═══")
    with open(WHITELIST_FILE, 'r') as f:
        whitelist = json.load(f)
    logger.info(f"白名单: {len(whitelist)} 只")

    for stock in whitelist:
        industries, score = lookup_knowledge(stock['code'], stock.get('name', ''))
        stock['industries'] = industries
        stock['industry_score'] = score

    cache = KlineCache(CACHE_DIR)
    symbols = [f"{s['code']}.SH" if s['code'].startswith('6') else f"{s['code']}.SZ"
               for s in whitelist]
    logger.info("  预取K线...")
    kline_data = cache.batch_fetch(symbols, count=60)

    for stock in whitelist:
        code = stock['code']
        suffix = '.SH' if code.startswith('6') else '.SZ'
        df = kline_data.get(f"{code}{suffix}")
        if df is not None and len(df) >= 20:
            stock['tech'] = compute_tech_score(df)
            stock['_kline'] = df
        else:
            stock['tech'] = TechScore()
            stock['_kline'] = None
        stock['total_score'] = score_v7(stock, stock['tech'])

    top = sorted(whitelist, key=lambda x: x.get('total_score', 0), reverse=True)[:n]
    logger.info(f"  Top{n} 评分范围: {top[0].get('total_score', 0):.1f} ~ {top[-1].get('total_score', 0):.1f}")
    return top


# ══════════════════════════════════════════════════════════
# 6. 论据构建
# ══════════════════════════════════════════════════════════

def _add_fundamental_bull(args: List[DebateArg], fund: Dict):
    comp = fund.get('competitive_analysis', {})
    fin = fund.get('financial_health', {})
    growth = fund.get('growth_assessment', {})
    geo = fund.get('geopolitical_assessment', {})
    metrics = fin.get('key_metrics', {})

    moat = comp.get('moat_level', '窄')
    if moat == '宽':
        args.append(DebateArg("宽护城河", "护城河等级为'宽'，竞争优势难以复制", 9, source='fundamentals'))
    elif moat == '中':
        args.append(DebateArg("中等护城河", "护城河等级为'中'，具备一定竞争壁垒", 6, source='fundamentals'))

    for i, s in enumerate(comp.get('strengths', [])[:3]):
        hd = has_quantitative_data(s)
        args.append(DebateArg(f"竞争优势{i+1}", s, 7 if hd else 4, source='fundamentals', has_data=hd))

    health = fin.get('health_rating', '一般')
    if health in ('健康', '良好'):
        roe = metrics.get('roe_pct', 0) or 0
        args.append(DebateArg(f"财务{health}", f"财务健康评级'{health}'，ROE={roe}%", 7, source='fundamentals', has_data=True))

    for name, key, threshold, label, w in [
        ('高净利率', 'net_margin_pct', 20, '盈利能力强', 6),
        ('高毛利率', 'gross_margin_pct', 40, '定价能力强', 5),
    ]:
        val = metrics.get(key, 0) or 0
        if val > threshold:
            args.append(DebateArg(name, f"{name.replace('高','')}{val:.1f}%，{label}", w, source='fundamentals', has_data=True))

    for i, d in enumerate(growth.get('growth_drivers', [])[:3]):
        has_mom = any(kw in d for kw in ('爆发', '加速', '突破', '翻倍', '量产', '放量'))
        hd = has_quantitative_data(d)
        args.append(DebateArg(f"增长驱动{i+1}", d, 7 if (has_mom or hd) else 4, source='fundamentals', has_data=hd or has_mom))

    for i, o in enumerate(geo.get('opportunities', [])[:2]):
        hd = has_quantitative_data(o)
        args.append(DebateArg(f"地缘机遇{i+1}", o, 4 if hd else 2, source='fundamentals', has_data=hd))


def _add_fundamental_bear(args: List[DebateArg], fund: Dict):
    comp = fund.get('competitive_analysis', {})
    fin = fund.get('financial_health', {})
    growth = fund.get('growth_assessment', {})
    geo = fund.get('geopolitical_assessment', {})
    metrics = fin.get('key_metrics', {})

    moat = comp.get('moat_level', '窄')
    if moat == '窄':
        args.append(DebateArg("护城河窄", "护城河等级为'窄'，竞争优势容易被侵蚀", 8, source='fundamentals'))
    elif moat == '中':
        args.append(DebateArg("护城河一般", "护城河等级为'中'，壁垒不够坚固", 4, source='fundamentals'))

    for i, w in enumerate(comp.get('weaknesses', [])[:3]):
        hd = has_quantitative_data(w)
        args.append(DebateArg(f"竞争劣势{i+1}", w, 6 if hd else 3, source='fundamentals', has_data=hd))

    health = fin.get('health_rating', '一般')
    if health == '较差':
        args.append(DebateArg("财务较差", f"财务健康评级'{health}'，存在财务风险", 8, source='fundamentals', has_data=True))
    elif health == '一般':
        args.append(DebateArg("财务一般", f"财务健康评级'一般'，无突出优势", 4, source='fundamentals', has_data=True))

    net_margin = metrics.get('net_margin_pct', 0) or 0
    if net_margin < 0:
        args.append(DebateArg("亏损", f"净利率{net_margin:.1f}%，公司处于亏损状态", 9, source='fundamentals', has_data=True))
    elif net_margin < 5:
        args.append(DebateArg("利润微薄", f"净利率仅{net_margin:.1f}%，盈利能力弱", 5, source='fundamentals', has_data=True))

    debt = metrics.get('debt_ratio_pct', 0) or 0
    if debt > 60:
        args.append(DebateArg("高负债", f"负债率{debt:.0f}%，财务杠杆风险", 5, source='fundamentals', has_data=True))

    for i, h in enumerate(growth.get('headwinds', [])[:3]):
        hd = has_quantitative_data(h)
        args.append(DebateArg(f"增长逆风{i+1}", h, 5 if hd else 3, source='fundamentals', has_data=hd))

    for i, r in enumerate(geo.get('risks', [])[:2]):
        hd = has_quantitative_data(r)
        args.append(DebateArg(f"地缘风险{i+1}", r, 4 if hd else 2, source='fundamentals', has_data=hd))


def _add_wk_bull(args: List[DebateArg], wk: Dict, name: str):
    for i, s in enumerate(wk.get('strengths', [])[:3]):
        w = weight_for_wk(s, name, 7, 4)
        args.append(DebateArg(f"行业优势{i+1}", s, w, source='world_knowledge', has_data=has_quantitative_data(s)))
    for i, d in enumerate(wk.get('growth_drivers', [])[:2]):
        w = weight_for_wk(d, name, 6, 3)
        args.append(DebateArg(f"行业增长{i+1}", d, w, source='world_knowledge', has_data=has_quantitative_data(d)))
    for i, o in enumerate(wk.get('geopolitical_opportunities', [])[:1]):
        hd = has_quantitative_data(o)
        args.append(DebateArg(f"行业地缘机遇{i+1}", o, 3 if hd else 1, source='world_knowledge', has_data=hd))


def _add_wk_bear(args: List[DebateArg], wk: Dict, name: str):
    for i, w in enumerate(wk.get('weaknesses', [])[:3]):
        wt = weight_for_wk(w, name, 6, 4)
        args.append(DebateArg(f"行业劣势{i+1}", w, wt, source='world_knowledge', has_data=has_quantitative_data(w)))
    for i, h in enumerate(wk.get('headwinds', [])[:2]):
        wt = weight_for_wk(h, name, 5, 3)
        args.append(DebateArg(f"行业逆风{i+1}", h, wt, source='world_knowledge', has_data=has_quantitative_data(h)))
    for i, r in enumerate(wk.get('geopolitical_risks', [])[:1]):
        hd = has_quantitative_data(r)
        args.append(DebateArg(f"行业地缘风险{i+1}", r, 3 if hd else 1, source='world_knowledge', has_data=hd))


def _add_tech_bull(args: List[DebateArg], tech: TechScore):
    if tech.trend >= 28:
        args.append(DebateArg("多头强势", f"趋势得分{tech.trend:.0f}/35，均线多头排列", 8, source='tech', has_data=True))
    elif tech.trend >= 21:
        args.append(DebateArg("震荡偏多", f"趋势得分{tech.trend:.0f}/35，偏多格局", 5, source='tech', has_data=True))
    if tech.momentum >= 24:
        args.append(DebateArg("动量强劲", f"动量得分{tech.momentum:.0f}/30，RSI健康区间", 7, source='tech', has_data=True))
    elif tech.momentum >= 18:
        args.append(DebateArg("动量偏多", f"动量得分{tech.momentum:.0f}/30", 4, source='tech', has_data=True))
    if tech.volume >= 15:
        args.append(DebateArg("放量活跃", f"量能得分{tech.volume:.0f}/20，资金关注", 6, source='tech', has_data=True))
    elif tech.volume >= 11:
        args.append(DebateArg("量能正常", f"量能得分{tech.volume:.0f}/20", 3, source='tech', has_data=True))
    if tech.pattern >= 12:
        args.append(DebateArg("突破形态", f"形态得分{tech.pattern:.0f}/15", 5, source='tech', has_data=True))


def _add_tech_bear(args: List[DebateArg], tech: TechScore):
    if tech.trend < 14:
        args.append(DebateArg("趋势弱势", f"趋势得分{tech.trend:.0f}/35，空头格局", 7, source='tech', has_data=True))
    elif tech.trend < 21:
        args.append(DebateArg("趋势不明", f"趋势得分{tech.trend:.0f}/35，方向不明确", 3, source='tech', has_data=True))
    if tech.momentum < 10:
        args.append(DebateArg("动量衰竭", f"动量得分{tech.momentum:.0f}/30，上涨动力不足", 6, source='tech', has_data=True))
    elif tech.momentum < 18:
        args.append(DebateArg("动量偏弱", f"动量得分{tech.momentum:.0f}/30", 3, source='tech', has_data=True))
    if tech.volume < 7:
        args.append(DebateArg("交投冷清", f"量能得分{tech.volume:.0f}/20，市场关注度低", 5, source='tech', has_data=True))


def _add_valuation_bull(args: List[DebateArg], pe, market: str):
    if pe and 15 <= pe <= 80:
        args.append(DebateArg("估值合理", f"PE={pe:.0f}，处于合理区间(15-80)", 6, source='valuation', has_data=True))
    elif pe and 5 <= pe < 15:
        args.append(DebateArg("低估值", f"PE={pe:.0f}，可能被低估", 4, source='valuation', has_data=True))
    if market == 'star':
        args.append(DebateArg("科创板溢价", "科创板标的享受成长性溢价", 3, source='valuation'))
    elif market == 'gem':
        args.append(DebateArg("创业板溢价", "创业板标的享受成长溢价", 2, source='valuation'))


def _add_valuation_bear(args: List[DebateArg], pe):
    if pe is not None and pe < 0:
        args.append(DebateArg("亏损股", f"PE={pe:.0f}，公司亏损", 8, source='valuation', has_data=True))
    elif pe and pe > 200:
        args.append(DebateArg("估值过高", f"PE={pe:.0f}，远超合理区间", 6, source='valuation', has_data=True))
    elif pe and 80 < pe <= 200:
        args.append(DebateArg("估值偏高", f"PE={pe:.0f}，高于合理区间", 3, source='valuation', has_data=True))


def build_bull_args(info: Dict, focus: str) -> List[DebateArg]:
    args = []
    fund = info.get('fundamentals')
    wk = info.get('world_knowledge')
    tech = info.get('tech', TechScore())
    pe = info.get('pe')
    market = info.get('market', 'mainboard')
    know_score = info.get('know_score', 0)
    name = info.get('name', '')

    if fund:
        _add_fundamental_bull(args, fund)
    if wk:
        _add_wk_bull(args, wk, name)
    if focus in ('tech', 'final'):
        _add_tech_bull(args, tech)
        _add_valuation_bull(args, pe, market)
    if know_score >= 30:
        args.append(DebateArg("知识分优秀", f"基本面知识分{know_score}/40，深度认知", 5, source='fundamentals', has_data=True))
    elif know_score >= 20:
        args.append(DebateArg("知识分良好", f"基本面知识分{know_score}/40", 3, source='fundamentals', has_data=True))

    for a in args:
        a.weight_after_refute = a.weight
    return args


def build_bear_args(info: Dict, focus: str) -> List[DebateArg]:
    args = []
    fund = info.get('fundamentals')
    wk = info.get('world_knowledge')
    tech = info.get('tech', TechScore())
    pe = info.get('pe')
    know_score = info.get('know_score', 0)
    name = info.get('name', '')

    if fund:
        _add_fundamental_bear(args, fund)
    if wk:
        _add_wk_bear(args, wk, name)
    if not fund and not wk:
        args.append(DebateArg("信息不透明", "无基本面数据也无世界知识，信息严重不足", 9, source='fundamentals'))
    elif not fund:
        args.append(DebateArg("基本面缺失", "无详细基本面数据，财务状况不明", 5, source='fundamentals'))
    elif not wk:
        args.append(DebateArg("行业认知缺失", "无世界知识补充，行业深度不足", 3, source='world_knowledge'))
    if focus in ('tech', 'final'):
        _add_tech_bear(args, tech)
        _add_valuation_bear(args, pe)
    if know_score < 15:
        args.append(DebateArg("知识分低", f"基本面知识分仅{know_score}/40，认知不足", 5, source='fundamentals', has_data=True))

    for a in args:
        a.weight_after_refute = a.weight
    return args


# ══════════════════════════════════════════════════════════
# 7. 反驳机制
# ══════════════════════════════════════════════════════════

OPPOSITE_PAIRS = [
    ('宽护城河', '护城河窄'), ('护城河窄', '宽护城河'),
    ('宽护城河', '护城河一般'), ('中等护城河', '护城河窄'),
    ('财务健康', '财务较差'), ('财务较差', '财务健康'),
    ('财务良好', '财务较差'), ('财务较差', '财务良好'),
    ('高净利率', '亏损'), ('亏损', '高净利率'),
    ('高净利率', '利润微薄'), ('利润微薄', '高净利率'),
    ('高毛利率', '利润微薄'),
    ('多头强势', '趋势弱势'), ('趋势弱势', '多头强势'),
    ('多头强势', '趋势不明'), ('震荡偏多', '趋势弱势'),
    ('动量强劲', '动量衰竭'), ('动量衰竭', '动量强劲'),
    ('动量强劲', '动量偏弱'), ('动量偏多', '动量衰竭'),
    ('放量活跃', '交投冷清'), ('交投冷清', '放量活跃'),
    ('估值合理', '估值过高'), ('估值过高', '估值合理'),
    ('估值合理', '估值偏高'), ('低估值', '估值过高'),
    ('低估值', '亏损股'), ('估值合理', '亏损股'),
    ('知识分优秀', '知识分低'), ('知识分良好', '知识分低'),
    ('信息不透明', '宽护城河'), ('基本面缺失', '宽护城河'),
    ('行业认知缺失', '知识分优秀'),
]

DOMAIN_KEYWORDS = {
    'moat': ['护城河', '竞争优势', '竞争壁垒', '竞争劣势', '壁垒'],
    'finance': ['财务', '净利率', '毛利率', 'ROE', '负债', '亏损', '利润', '盈利'],
    'growth': ['增长', '驱动', '逆风', '爆发', '加速', '放量'],
    'tech': ['趋势', '动量', '量能', '形态', '多头', '空头', 'RSI'],
    'valuation': ['PE', '估值', '溢价'],
    'info': ['信息', '知识分', '认知', '基本面'],
}


def _get_domains(arg: DebateArg) -> set:
    domains = set()
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(kw in arg.point or kw in arg.evidence for kw in keywords):
            domains.add(domain)
    return domains


def refute_args(attacker_args: List[DebateArg], defender_args: List[DebateArg]) -> List[DebateArg]:
    """反驳机制：语义对立 + 数据对比 + 权重衰减"""
    for d_arg in defender_args:
        if d_arg.refuted:
            continue
        best_refutation, best_refuter, best_strength = None, None, 0

        for a_arg in attacker_args:
            # 语义对立
            for pos, neg in OPPOSITE_PAIRS:
                if pos in d_arg.point and neg in a_arg.point:
                    if 1.0 > best_strength:
                        best_strength = 1.0
                        best_refutation = f"语义对立：{a_arg.point}↔{d_arg.point}"
                        best_refuter = a_arg.point
                    break
            if best_strength >= 1.0:
                continue

            # 同领域数据优势
            common = _get_domains(d_arg) & _get_domains(a_arg)
            if common:
                if a_arg.has_data and not d_arg.has_data and 0.7 > best_strength:
                    best_strength = 0.7
                    best_refutation = f"实据压制：{a_arg.point}有数据推翻{d_arg.point}定性判断"
                    best_refuter = a_arg.point
                elif a_arg.weight > d_arg.weight * 1.5 and 0.5 > best_strength:
                    best_strength = 0.5
                    best_refutation = f"权重压制：{a_arg.point}(w{a_arg.weight})碾压{d_arg.point}(w{d_arg.weight})"
                    best_refuter = a_arg.point

        if best_refutation and best_strength > 0:
            d_arg.refuted = True
            d_arg.refutation = best_refutation
            d_arg.refuted_by = best_refuter or ""
            d_arg.weight_after_refute = d_arg.weight * (1 - best_strength * 0.7)

    return defender_args


# ══════════════════════════════════════════════════════════
# 8. 投降机制
# ══════════════════════════════════════════════════════════

def check_surrender(bull_args: List[DebateArg], bear_args: List[DebateArg]) -> Tuple[bool, bool, str]:
    """多路径投降检查：仅在一方被彻底压制时才触发"""
    bull_core = [a for a in bull_args if a.weight >= 7 and not a.refuted]
    bear_core = [a for a in bear_args if a.weight >= 7 and not a.refuted]
    bull_w = sum(a.weight_after_refute for a in bull_args)
    bear_w = sum(a.weight_after_refute for a in bear_args)
    bull_n, bear_n = max(len(bull_args), 1), max(len(bear_args), 1)

    # 路径1: 核心论据压制（2+核心且对方无核心且权重2.5倍）
    if len(bull_core) >= 2 and len(bear_core) == 0 and bull_w >= bear_w * 2.5:
        return False, True, f"Bull核心论据{len(bull_core)}项且Bear无核心反驳(权重比{bull_w:.0f}:{bear_w:.0f})，Bear投降"
    if len(bear_core) >= 2 and len(bull_core) == 0 and bear_w >= bull_w * 2.5:
        return True, False, f"Bear核心论据{len(bear_core)}项且Bull无核心反驳(权重比{bear_w:.0f}:{bull_w:.0f})，Bull投降"

    # 路径2: 超过70%论据被反驳且对方几乎未被反驳
    bull_rr = sum(1 for a in bull_args if a.refuted) / bull_n
    bear_rr = sum(1 for a in bear_args if a.refuted) / bear_n
    if bull_rr > 0.7 and bear_rr < 0.15 and len(bull_args) >= 4:
        return True, False, f"Bull {int(bull_rr*len(bull_args))}/{len(bull_args)}条论据被反驳，无力反击，Bull投降"
    if bear_rr > 0.7 and bull_rr < 0.15 and len(bear_args) >= 4:
        return False, True, f"Bear {int(bear_rr*len(bear_args))}/{len(bear_args)}条论据被反驳，无力反击，Bear投降"

    # 路径3: 信息严重缺失（无数据>90%且对方有4+数据且权重差3倍）
    bull_data = sum(1 for a in bull_args if a.has_data and not a.refuted)
    bear_data = sum(1 for a in bear_args if a.has_data and not a.refuted)
    bull_nd = sum(1 for a in bull_args if not a.has_data) / bull_n
    bear_nd = sum(1 for a in bear_args if not a.has_data) / bear_n
    if bear_nd > 0.9 and bull_data >= 4 and bear_w < bull_w * 0.33:
        return False, True, f"Bear无数据论据占比{bear_nd:.0%}且Bull有{bull_data}条数据论据，Bear信息不足投降"
    if bull_nd > 0.9 and bear_data >= 4 and bull_w < bear_w * 0.33:
        return True, False, f"Bull无数据论据占比{bull_nd:.0%}且Bear有{bear_data}条数据论据，Bull信息不足投降"

    # 路径4: 权重4倍碾压
    if bull_w >= bear_w * 4 and bear_w > 0:
        return False, True, f"Bull有效权重{bull_w:.0f}碾压Bear{bear_w:.0f}(4倍+)，Bear投降"
    if bear_w >= bull_w * 4 and bull_w > 0:
        return True, False, f"Bear有效权重{bear_w:.0f}碾压Bull{bull_w:.0f}(4倍+)，Bull投降"

    return False, False, ""


# ══════════════════════════════════════════════════════════
# 9. 辩论引擎
# ══════════════════════════════════════════════════════════

def _format_statement(args: List[DebateArg], side: str, top_n: int = 6) -> str:
    """将论据展开为 ≥150 字的辩论陈述"""
    core = sorted(args, key=lambda a: a.weight, reverse=True)[:top_n]
    lines = []
    for i, a in enumerate(core, 1):
        tag = "实据" if a.has_data else "定性"
        ev = a.evidence.strip()
        lines.append(f"{i}）{a.point}（{tag}）：{ev}")
    body = "；".join(lines)
    if side == "bull":
        return f"看多逻辑：{body}。"
    else:
        return f"看空逻辑：{body}。"


def _format_rebuttals(args: List[DebateArg]) -> str:
    """格式化反驳摘要，同一个反驳论点聚合显示"""
    if not any(a.refuted for a in args):
        return "无法有效反驳"

    # 按 refuted_by 聚合
    groups: dict = {}
    for a in args:
        if a.refuted:
            key = (a.refuted_by or a.refutation, a.refutation.split('：')[0] if a.refutation else '')
            groups.setdefault(key, []).append(a.point)

    parts = []
    for (ref_by, mechanism), targets in groups.items():
        if len(targets) == 1:
            parts.append(f"{mechanism}：{ref_by}→{targets[0]}")
        else:
            parts.append(f"{mechanism}：{ref_by}→{','.join(targets[:3])}{'等' if len(targets)>3 else ''}{len(targets)}项")
    return "  |  ".join(parts)


# 资金流缓存（一次运行只查一次）
_MF_CACHE: Dict[str, float] = {}
_MF_CHECKED: bool = False

def _ensure_mf_probed():
    """确保资金流模块已被探测过（仅探测一次）"""
    global _MF_CHECKED
    if _MF_CHECKED:
        return
    _MF_CHECKED = True
    try:
        from money_flow import probe_availability
        ok = probe_availability()
        if ok:
            logger.info("资金流模块已启用")
        else:
            logger.info("资金流模块禁用（API不可用），所有股票统一无资金流加分")
    except Exception:
        logger.info("资金流模块无法加载，跳过")

def _get_money_flow_multiplier(code: str, mcap: float) -> float:
    """获取资金流乘性调节因子 (0.85~1.15)。优先用缓存"""
    if not code:
        return 1.0
    if code in _MF_CACHE:
        return _MF_CACHE[code]
    try:
        from money_flow import compute_money_flow_score
        s = compute_money_flow_score(code, mcap)
        _MF_CACHE[code] = s.multiplier
        return s.multiplier
    except Exception:
        _MF_CACHE[code] = 1.0
        return 1.0


def run_debate(info: Dict, focus: str) -> DebateRecord:
    """对单只股票运行交互式辩论：Bull陈述→Bear反驳→Bear陈述→Bull反驳→投降检查→裁决"""
    record = DebateRecord(code=info['code'], name=info['name'])
    bull_args = build_bull_args(info, focus)
    bear_args = build_bear_args(info, focus)

    # 第一轮：Bull陈述 → Bear反驳
    ex1 = DebateExchange(round_num=1, bull_statement=_format_statement(bull_args, "bull"))
    bull_args = refute_args(bear_args, bull_args)
    ex1.bear_rebuttal = _format_rebuttals(bull_args)

    # 第二轮：Bear陈述 → Bull反驳
    ex2 = DebateExchange(round_num=2, bear_statement=_format_statement(bear_args, "bear"))
    bear_args = refute_args(bull_args, bear_args)
    ex2.bull_rebuttal = _format_rebuttals(bear_args)

    # 投降检查
    bull_surr, bear_surr, reason = check_surrender(bull_args, bear_args)
    record.bull_surrendered, record.bear_surrendered, record.surrender_reason = bull_surr, bear_surr, reason
    if bear_surr:
        ex2.bear_surrenders, ex2.surrender_reason = True, reason
    elif bull_surr:
        ex2.bull_surrenders, ex2.surrender_reason = True, reason
    record.exchanges = [ex1, ex2]

    # 计算得分
    bull_w = sum(a.weight_after_refute for a in bull_args)
    bear_w = sum(a.weight_after_refute for a in bear_args)
    if bear_surr: bull_w *= 1.5
    if bull_surr: bear_w *= 1.5
    record.bull_score, record.bear_score = bull_w, bear_w
    record.bull_args, record.bear_args = bull_args, bear_args

    # Judge 裁决
    total_score = info.get('total_score', 0)
    know_score = info.get('know_score', 0)

    # 趋势调节因子：近期横盘/下跌打折，健康上涨不动，避免追高过热
    trend_adj = 1.0
    df = info.get('_kline')
    if df is not None and len(df) >= 21:
        close_20 = float(df['close'].iloc[-21])
        close_1 = float(df['close'].iloc[-1])
        ret_20 = (close_1 - close_20) / close_20 * 100 if close_20 > 0 else 0
        if ret_20 < -15:
            trend_adj = 0.35   # 近20日大跌超15%，大幅砍分
        elif ret_20 < -5:
            trend_adj = 0.60   # 近20日跌5~15%
        elif ret_20 < 0:
            trend_adj = 0.75   # 近20日微跌
        elif ret_20 < 5:
            trend_adj = 0.85   # 近20日横盘（0~5%）—— 打死不涨类
        # 5~15% → 1.0 健康上涨，技术评分本身会做超买/乖离判断
        # >15% → 1.0 不额外加分，让技术子项独立裁决
    record._trend_adj = trend_adj  # 调试用

    # 资金流调节：乘性因子 (0.85~1.15)，作用于最终判官分
    mf_mult = _get_money_flow_multiplier(info.get('code', ''), info.get('mcap', 0))

    if bear_surr:
        record.judge_score = (bull_w * 1.2 + total_score * 0.1) * trend_adj * mf_mult
        record.judge_verdict = f"Bear投降，Bull胜出。{reason}"
    elif bull_surr:
        record.judge_score = (-bear_w * 0.5 + total_score * 0.05) * trend_adj * mf_mult
        record.judge_verdict = f"Bull投降，Bear胜出。{reason}"
    else:
        record.judge_score = (bull_w * 0.6 - bear_w * 0.3 + total_score * 0.1 + know_score * 0.2) * trend_adj * mf_mult
        net = bull_w - bear_w
        br = sum(1 for a in bull_args if a.refuted)
        brr = sum(1 for a in bear_args if a.refuted)
        ref_info = f"[Bull被反驳{br}条,Bear被反驳{brr}条]"
        if net > 10:   record.judge_verdict = f"Bull优势明显(净权重+{net:.0f}){ref_info}"
        elif net > 0:  record.judge_verdict = f"Bull小幅领先(净权重+{net:.0f}){ref_info}"
        elif net > -10:record.judge_verdict = f"Bear小幅领先(净权重{net:.0f}){ref_info}"
        else:          record.judge_verdict = f"Bear优势明显(净权重{net:.0f}){ref_info}"

    return record


# ══════════════════════════════════════════════════════════
# 10. 辩论轮次
# ══════════════════════════════════════════════════════════

def debate_round(stocks: List[Dict], focus: str, target_n: int, round_name: str) -> List[DebateRecord]:
    logger.info(f"═══ {round_name} → Top{target_n} ═══")
    records = [run_debate(gather_stock_info(s), focus) for s in stocks]

    # 行业分散（仅第一轮）
    if focus == 'fundamental':
        industry_counts: Dict[str, int] = {}
        for r in records:
            stock = next((s for s in stocks if s['code'] == r.code), None)
            if stock:
                primary = stock.get('industries', ['未分类'])[0] if stock.get('industries') else '未分类'
                industry_counts[primary] = industry_counts.get(primary, 0) + 1
        n_industries = max(len(industry_counts), 1)
        quota = max(2, (target_n + n_industries - 1) // n_industries)
        industry_selected: Dict[str, int] = {}
        records.sort(key=lambda r: r.judge_score, reverse=True)
        for r in records:
            stock = next((s for s in stocks if s['code'] == r.code), None)
            if stock:
                primary = stock.get('industries', ['未分类'])[0] if stock.get('industries') else '未分类'
                if industry_selected.get(primary, 0) >= quota:
                    r.eliminated = True
                    r.eliminate_reason = f"行业'{primary}'超额"
                else:
                    industry_selected[primary] = industry_selected.get(primary, 0) + 1
        selected = [r for r in records if not r.eliminated]
        if len(selected) > target_n:
            selected.sort(key=lambda r: r.judge_score, reverse=True)
            for r in selected[target_n:]:
                r.eliminated = True
                r.eliminate_reason = "行业分散后仍超出名额"
    else:
        records.sort(key=lambda r: r.judge_score, reverse=True)
        for r in records[target_n:]:
            r.eliminated = True
            r.eliminate_reason = f"排名未进Top{target_n}"

    selected = [r for r in records if not r.eliminated]
    elim = [r for r in records if r.eliminated]
    bear_surr = sum(1 for r in selected if r.bear_surrendered)
    bull_surr = sum(1 for r in selected if r.bull_surrendered)
    logger.info(f"  淘汰{len(elim)}只 保留{len(selected)}只 | Bear投降{bear_surr}次 Bull投降{bull_surr}次")
    return records


# ══════════════════════════════════════════════════════════
# 11. 辩论过程打印
# ══════════════════════════════════════════════════════════

_SRC_LABELS = {"fundamentals": "基本面", "world_knowledge": "世界知识", "tech": "技术面", "valuation": "估值"}

def print_debate_process(round_name: str, records: List[DebateRecord], stocks: List[Dict]):
    stock_map = {s['code']: s for s in stocks}
    print(f"\n{'═'*120}")
    print(f"  {round_name} — 辩论过程")
    print(f"{'═'*120}")

    for i, r in enumerate(records):
        if r.eliminated:
            continue
        stock = stock_map.get(r.code, {})
        inds = ','.join(stock.get('industries', [])[:2]) or '未分类'
        pe = stock.get('pe_ttm', '-')
        mcap = stock.get('mcap_yi', '-')
        print(f"\n  ┌─ [{i+1}] {r.code} {r.name} │ PE:{pe} 市值:{mcap}亿 │ {inds}")
        print(f"  │")

        for ex in r.exchanges:
            print(f"  │ ┌─ 交互第{ex.round_num}轮 ─────────────────────────────────")
            if ex.round_num == 1:
                print(f"  │ │ 🐂 Bull陈述: {ex.bull_statement}")
                print(f"  │ │ 🐻 Bear反驳: {ex.bear_rebuttal}")
            else:
                print(f"  │ │ 🐻 Bear陈述: {ex.bear_statement}")
                print(f"  │ │ 🐂 Bull反驳: {ex.bull_rebuttal}")
            if ex.bear_surrenders:
                print(f"  │ │ ⚠️  Bear投降! {ex.surrender_reason}")
            if ex.bull_surrenders:
                print(f"  │ │ ⚠️  Bull投降! {ex.surrender_reason}")
            print(f"  │ └──────────────────────────────────────────────────")
        print(f"  │")

        for side, label, emoji, score, surrendered in [
            (r.bull_args, "Bull", "🐂", r.bull_score, r.bull_surrendered),
            (r.bear_args, "Bear", "🐻", r.bear_score, r.bear_surrendered),
        ]:
            if surrendered:
                print(f"  │ {emoji} {label}: 【投降】{r.surrender_reason}")
            else:
                print(f"  │ {emoji} {label} (有效权重{score:.0f}):")
                for a in side:
                    status = f" [已反驳→{a.weight_after_refute:.1f}]" if a.refuted else ""
                    data_tag = "📊" if a.has_data else "💬"
                    src = _SRC_LABELS.get(a.source, "")
                    print(f"  │   {data_tag} {a.point}(w{a.weight}{src}): {a.evidence}{status}")
            print(f"  │")

        surr_tag = " [Bull投降]" if r.bull_surrendered else (" [Bear投降]" if r.bear_surrendered else "")
        print(f"  │ ⚖️  Judge: {r.judge_verdict}{surr_tag} → 裁判分{r.judge_score:.1f}")
        print(f"  └{'─'*116}")


# ══════════════════════════════════════════════════════════
# 12. 验证：10 交易日后结算
# ══════════════════════════════════════════════════════════

def validate_after_30days(stocks: List[Dict], records: List[DebateRecord]) -> Dict:
    logger.info("═══ 验证: 30 交易日后结算 ═══")
    results = []
    for r in records:
        if r.eliminated:
            continue
        stock = next((s for s in stocks if s['code'] == r.code), None)
        if not stock:
            continue
        df = stock.get('_kline')
        if df is None or len(df) < 30:
            continue
        close_start = df['close'].iloc[-31] if len(df) >= 31 else df['close'].iloc[0]
        close_end = df['close'].iloc[-1]
        ret = (close_end - close_start) / close_start * 100
        results.append({
            'code': r.code, 'name': r.name,
            'judge_score': r.judge_score, 'bull_score': r.bull_score, 'bear_score': r.bear_score,
            'bear_surrendered': r.bear_surrendered, 'bull_surrendered': r.bull_surrendered,
            'judge_verdict': r.judge_verdict, 'return_pct': round(ret, 2),
            'close_start': round(float(close_start), 2), 'close_end': round(float(close_end), 2),
        })
    results.sort(key=lambda x: x['return_pct'], reverse=True)
    avg_ret = np.mean([r['return_pct'] for r in results]) if results else 0
    pos_rate = sum(1 for r in results if r['return_pct'] > 0) / len(results) * 100 if results else 0
    return {'results': results, 'avg_return': round(avg_ret, 2), 'positive_rate': round(pos_rate, 1), 'count': len(results)}


# ══════════════════════════════════════════════════════════
# 13. 最终报告
# ══════════════════════════════════════════════════════════

def print_final_report(stocks: List[Dict], all_records: List[List[DebateRecord]], validation: Dict = None):
    now = datetime.now()
    final_records = all_records[-1]
    selected = [r for r in final_records if not r.eliminated]
    stock_map = {s['code']: s for s in stocks}

    # 计算每只股票的 30 日收益，用于排序
    ranked = []
    for r in selected:
        s = stock_map.get(r.code, {})
        df = s.get('_kline')
        ret30 = 0.0
        if df is not None and len(df) >= 31:
            ret30 = (df['close'].iloc[-1] - df['close'].iloc[-31]) / df['close'].iloc[-31] * 100
        ranked.append((ret30, r, s))
    ranked.sort(key=lambda x: -x[0])  # 按30日收益降序

    print(f"""
╔════════════════════════════════════════════════════════════════════════╗
║              J-TradingAgents 多轮辩论选股报告 v3.0                  ║
╠════════════════════════════════════════════════════════════════════════╣
║ 报告编号: JTA-DEBATE-{now.strftime('%Y%m%d')}-001                              ║
║ 生成日期: {now.strftime('%Y-%m-%d %H:%M:%S')}                                   ║
║ 流水线:   Top100 → 辩论1(50) → 辩论2(30) → 辩论3(20) → 辩论4(10)   ║
║ 特色:     交互辩论 · 世界知识 · 投降机制 · 反驳衰减                ║
╚════════════════════════════════════════════════════════════════════════╝
""")

    round_names = [
        "第一轮: 100→50 (行业分散+基本面)",
        "第二轮: 50→30 (竞争壁垒+成长性)",
        "第三轮: 30→20 (技术面+估值)",
        "第四轮: 20→10 (综合博弈)"
    ]
    for records, name in zip(all_records, round_names):
        sel = [r for r in records if not r.eliminated]
        elim = [r for r in records if r.eliminated]
        bs = sum(1 for r in sel if r.bear_surrendered)
        bus = sum(1 for r in sel if r.bull_surrendered)
        print(f"  {name}: 淘汰{len(elim)}只 保留{len(sel)}只 | Bear投降{bs}次 Bull投降{bus}次")

    print(f"\n{'═'*140}")
    print("  最终推荐 TOP10（按30日收益排序）")
    print(f"{'═'*140}")
    print(f"{'#':>3} {'代码':>8} {'名称':>10} {'30日收益':>8} {'Judge':>7} {'Bull':>6} {'Bear':>6} {'v7':>6} {'知识':>5} {'技术':>5} {'PE':>8} {'行业':>14} {'裁决':>20}")
    print('-' * 140)
    for i, (ret30, r, s) in enumerate(ranked):
        tech = s.get('tech', TechScore())
        surr = " [Bear投降]" if r.bear_surrendered else (" [Bull投降]" if r.bull_surrendered else "")
        print(f"{i+1:>3} {r.code:>8} {r.name[:8]:>10} {ret30:>+7.1f}% "
              f"{r.judge_score:>7.1f} {r.bull_score:>6.0f} "
              f"{r.bear_score:>6.0f} {s.get('total_score',0):>6.1f} {s.get('_know_score',0):>5.0f} {tech.total:>5.0f} "
              f"{str(s.get('pe_ttm','-')):>8} {','.join(s.get('industries',[])[:2])[:12]:>14} {r.judge_verdict[:18]+surr:>20}")

    if validation and validation.get('results'):
        print(f"\n{'═'*140}")
        print(f"  30 交易日后验证")
        print(f"{'═'*140}")
        print(f"{'#':>3} {'代码':>8} {'名称':>10} {'Judge':>7} {'收益%':>8} {'起价':>8} {'终价':>8} {'裁决':>20}")
        print('-' * 100)
        for i, r in enumerate(validation['results']):
            surr = " [Bear投降]" if r.get('bear_surrendered') else (" [Bull投降]" if r.get('bull_surrendered') else "")
            print(f"{i+1:>3} {r['code']:>8} {r['name'][:8]:>10} {r['judge_score']:>7.1f} "
                  f"{r['return_pct']:>+8.2f} {r['close_start']:>8.2f} {r['close_end']:>8.2f} {r.get('judge_verdict','')[:18]+surr:>20}")
        print(f"\n  平均收益: {validation['avg_return']:+.2f}%")
        print(f"  正收益率: {validation['positive_rate']:.0f}%")

    print(f"\n  【风险提示】本报告仅供参考，不构成投资建议")


# ══════════════════════════════════════════════════════════
# 14. 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='多轮辩论选股系统 v3.0')
    parser.add_argument('--top-n', type=int, default=100, help='召回数量 (默认100)')
    parser.add_argument('--no-validate', action='store_true', help='跳过30日验证')
    parser.add_argument('--force-refresh', action='store_true', help='强制刷新K线缓存')
    args = parser.parse_args()

    # 强制刷新缓存
    if args.force_refresh:
        import shutil
        if os.path.exists(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
            logger.info("已清除K线缓存，将重新拉取最新数据")

    print(f"""
╔════════════════════════════════════════════════════════════════════════╗
║              J-TradingAgents 多轮辩论选股系统 v3.0                  ║
╠════════════════════════════════════════════════════════════════════════╣
║ 流水线: Top{args.top_n} → 辩论1(50) → 辩论2(30) → 辩论3(20) → 辩论4(10)  ║
║ 特色:   交互辩论 · 世界知识 · 投降机制 · 反驳衰减                    ║
║ 时间:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                                         ║
╚════════════════════════════════════════════════════════════════════════╝
""")

    logger.info(f"世界知识库: {len(get_world_knowledge())} 条")

    # 召回
    top_stocks = recall_top_n(args.top_n)

    # 四轮辩论
    pipeline = [
        ('fundamental', 50, "第一轮辩论: 100→50 (行业分散+基本面)"),
        ('moat_growth', 30, "第二轮辩论: 50→30 (竞争壁垒+成长性)"),
        ('tech',        20, "第三轮辩论: 30→20 (技术面+估值)"),
        ('final',       10, "第四轮辩论: 20→10 (综合博弈)"),
    ]

    all_records = []
    survivors = top_stocks
    for focus, target, name in pipeline:
        records = debate_round(survivors, focus, target, name)
        print_debate_process(name, records, survivors)
        all_records.append(records)
        survivors = [s for s, r in zip(survivors, records) if not r.eliminated]

    # 验证
    validation = None if args.no_validate else validate_after_30days(survivors, all_records[-1])

    # 报告
    print_final_report(top_stocks, all_records, validation)

    # 保存JSON
    output = {'timestamp': datetime.now().isoformat(), 'version': '3.0',
              'pipeline': f'Top{args.top_n} → 50 → 30 → 20 → 10', 'rounds': []}
    for i, records in enumerate(all_records):
        output['rounds'].append({
            'round': i + 1,
            'selected': [{
                'code': r.code, 'name': r.name,
                'bull_score': r.bull_score, 'bear_score': r.bear_score,
                'judge_score': r.judge_score, 'judge_verdict': r.judge_verdict,
                'bull_surrendered': r.bull_surrendered, 'bear_surrendered': r.bear_surrendered,
                'surrender_reason': r.surrender_reason,
                'exchanges': [{'round_num': ex.round_num, 'bull_statement': ex.bull_statement,
                                'bear_rebuttal': ex.bear_rebuttal, 'bear_statement': ex.bear_statement,
                                'bull_rebuttal': ex.bull_rebuttal,
                                'bear_surrenders': ex.bear_surrenders, 'bull_surrenders': ex.bull_surrenders,
                                'surrender_reason': ex.surrender_reason} for ex in r.exchanges],
                'bull_args': [{'point': a.point, 'evidence': a.evidence, 'weight': a.weight,
                                'source': a.source, 'has_data': a.has_data, 'refuted': a.refuted,
                                'weight_after_refute': round(a.weight_after_refute, 1)} for a in r.bull_args],
                'bear_args': [{'point': a.point, 'evidence': a.evidence, 'weight': a.weight,
                                'source': a.source, 'has_data': a.has_data, 'refuted': a.refuted,
                                'weight_after_refute': round(a.weight_after_refute, 1)} for a in r.bear_args],
            } for r in records if not r.eliminated],
        })
    if validation:
        output['validation'] = validation

    report_file = f"debate_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"报告已保存: {report_file}")


if __name__ == "__main__":
    main()
