#!/usr/bin/env python3
"""提取 fundamentals JSON 关键信息为紧凑格式，供模型直读评分"""
import json, os

FUNDAMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fundamentals')
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_fundamentals_compact.jsonl')

def extract(code):
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return None
    comp = d.get('competitive_analysis', {})
    fin = d.get('financial_health', {})
    m = fin.get('key_metrics', {})
    growth = d.get('growth_assessment', {})
    geo = d.get('geopolitical_assessment', {})
    biz = d.get('business_overview', {})
    return {
        'c': d.get('code',''),
        'n': d.get('name',''),
        'ind': biz.get('industry',''),
        'do': biz.get('what_they_do','')[:300],
        'moat': comp.get('moat_level',''),
        's': comp.get('strengths',[])[:3],
        'w': comp.get('weaknesses',[])[:2],
        'rev': m.get('revenue_yi'),
        'np': m.get('net_profit_yi'),
        'roe': m.get('roe_pct'),
        'gm': m.get('gross_margin_pct'),
        'nm': m.get('net_margin_pct'),
        'dr': m.get('debt_ratio_pct'),
        'cf2p': m.get('cf_to_profit'),
        'hr': fin.get('health_rating',''),
        'gs': growth.get('growth_score'),
        'gd': growth.get('growth_drivers',[])[:3],
        'gh': growth.get('headwinds',[])[:2],
        'opp': geo.get('opportunities',[])[:2],
        'mom': geo.get('industry_momentum',[])[:2],
    }

files = sorted(f for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith('.json'))
with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
    for fname in files:
        code = fname.replace('.json','')
        row = extract(code)
        if row:
            out.write(json.dumps(row, ensure_ascii=False) + '\n')
print(f'Done: {len(files)} stocks -> {OUTPUT_FILE}')
