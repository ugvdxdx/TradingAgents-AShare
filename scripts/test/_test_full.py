#!/usr/bin/env python3
import sys, json
sys.path.insert(0, '.')
from fundamental_agent import analyze_one

# 测试无手工画像的股票
tests = [
    ('000333', '美的集团'),
    ('600000', '浦发银行'),
    ('002475', '立讯精密'),
    ('601728', '中国电信'),
]
for code, name in tests:
    r = analyze_one(code, name, force=True)
    ov = r.get('business_overview', {})
    desc = ov.get('what_they_do', '')
    pos = ov.get('industry_position', '')
    print(f"{code} {name}")
    print(f"  描述: {desc[:100] if desc else '(空)'}")
    print(f"  行业地位: {pos[:60] if pos else '(空)'}")
    print(f"  评级: {r.get('overall_rating')}")
    print(f"  总结: {r.get('overall_summary','')[:140]}")
    print()