#!/usr/bin/env python3
"""Fundamentals JSON 加载器：为 V3 评分注入精简的 fundamentals 文本。

历史：原 fundamental_scorer.py（V1/V2 基本面评分器）于 2026-06 废弃，V3 替代。
本文件仅保留 _build_stock_json——fundamentals JSON → V3 评分 prompt 的唯一注入
入口（v3_full_score._call 调用）。V1/V2 评分代码（compute_fundamental_knowledge/
v2、compute_sector_alpha、_rule_based_score、SCORING_PROMPT 等）已删。
"""
import json
import os
from typing import Optional

from picker import paths

FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR


def _build_stock_json(code: str) -> Optional[str]:
    """加载并压缩 fundamentals JSON 为 LLM 可读文本。

    V3 评分(v3_full_score._call)唯一调用此函数注入 fundamentals。注入全部字段：
    business_overview(what_they_do/industry/industry_position) +
    competitive(strengths/weaknesses/moat) + financial_health(key_metrics 10项 /
    health_rating / benchmark_ref / highlights / risks) + growth(growth_score /
    drivers / headwinds) + geopolitical(opportunities / risks / momentum) + summary。
    现金流(operating_cf_yi/cf_to_profit)优先读 fundamentals 缓存，缺失 Tushare 兜底
    (支撑 v3_full_score.py:110 现金流红线)。
    """
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None

    # 提取关键字段，精简输出（灌水字段已删除）
    comp = data.get('competitive_analysis', {})
    fin = data.get('financial_health', {})
    metrics = fin.get('key_metrics', {})
    growth = data.get('growth_assessment', {})
    geo = data.get('geopolitical_assessment', {})
    biz = data.get('business_overview', {})

    # 现金流: 优先读 fundamentals 缓存; 缺失则 Tushare 兜底
    # (V3 现金流红线 v3_full_score.py:110 依赖 cf_to_profit/经营现金流判断需求性质)
    ocf = metrics.get("operating_cf_yi")
    c2p = metrics.get("cf_to_profit")
    if ocf is None:
        try:
            from picker.data.fundamentals_data import fetch_real_financials
            rt = fetch_real_financials(code)
            if rt:
                ocf = rt.get("operating_cf_yi")
                if c2p is None:
                    c2p = rt.get("cf_to_profit")
        except Exception:
            pass  # 兜底失败不阻断评分, V3 按无现金流数据处理

    return json.dumps({
        "code": data.get("code"),
        "name": data.get("name"),
        "industry": biz.get("industry", ""),
        "what_they_do": biz.get("what_they_do", ""),
        "industry_position": biz.get("industry_position", ""),
        "moat": comp.get("moat_level", "窄"),
        "strengths": comp.get("strengths", [])[:5],
        "weaknesses": comp.get("weaknesses", [])[:5],
        "financial": {
            "revenue_yi": metrics.get("revenue_yi"),
            "net_profit_yi": metrics.get("net_profit_yi"),
            "roe_pct": metrics.get("roe_pct"),
            "gross_margin_pct": metrics.get("gross_margin_pct"),
            "net_margin_pct": metrics.get("net_margin_pct"),
            "rd_ratio_pct": metrics.get("rd_ratio_pct"),
            "rd_expense_yi": metrics.get("rd_expense_yi"),
            "debt_ratio_pct": metrics.get("debt_ratio_pct"),
            "operating_cf_yi": ocf,
            "cf_to_profit": c2p,
            "health": fin.get("health_rating", ""),
            "benchmark_ref": fin.get("benchmark_ref", ""),
            "highlights": fin.get("highlights", [])[:2],
            "risks": fin.get("risks", [])[:2],
        },
        "growth_score": growth.get("growth_score"),
        "growth_drivers": growth.get("growth_drivers", [])[:5],
        "headwinds": growth.get("headwinds", [])[:5],
        "geo_opportunities": geo.get("opportunities", [])[:4],
        "geo_risks": geo.get("risks", [])[:4],
        "momentum": geo.get("industry_momentum", []),
        "summary": (data.get("summary", "") or "")[:600],
    }, ensure_ascii=False, indent=1)
