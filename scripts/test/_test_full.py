#!/usr/bin/env python3
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from picker.pipeline.gen_fundamentals import generate_one, load_world_knowledge, load_reference_fundamentals
from picker import paths

# 测试生成 fundamentals
tests = [
    ('000333', '美的集团'),
    ('600000', '浦发银行'),
    ('002475', '立讯精密'),
    ('601728', '中国电信'),
]
wk = load_world_knowledge()
ref = load_reference_fundamentals()
for code, name in tests:
    r = generate_one(code, name, '未知', 0, wk, ref)
    if not r:
        print(f"{code} {name} 生成失败")
        continue
    ov = r.get('business_overview', {})
    desc = ov.get('what_they_do', '')
    pos = ov.get('industry_position', '')
    print(f"{code} {name}")
    print(f"  描述: {desc[:100] if desc else '(空)'}")
    print(f"  行业地位: {pos[:60] if pos else '(空)'}")
    print(f"  总结: {r.get('summary','')[:140]}")
    print()