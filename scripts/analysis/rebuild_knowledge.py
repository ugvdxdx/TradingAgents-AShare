#!/usr/bin/env python3
"""Rebuild world_knowledge.py from current module + batch JSONs"""
import json, re, glob, ast

# load batches
batches = {}
for fname in sorted(glob.glob('batch*_output.json')):
    with open(fname) as f:
        b = json.load(f)
    batches.update(b)

# load current entries from module
from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B
merged = dict(B)
for code, entry in batches.items():
    if code not in merged:
        merged[code] = entry

print(f"Batches: {len(batches)}, Merged total: {len(merged)}")

def fmt(data):
    return json.dumps(data, ensure_ascii=False)

entry_lines = []
for code in sorted(merged.keys()):
    d = merged[code]
    lines = [f'    "{code}": {{']
    lines.append(f'        "name": {fmt(d["name"])},')
    lines.append(f'        "industry": {fmt(d["industry"])},')
    for fld in ['strengths','weaknesses','growth_drivers','headwinds','geopolitical_risks','geopolitical_opportunities']:
        items = d.get(fld, [])
        lines.append(f'        "{fld}": [')
        for i, item in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            lines.append(f'            {fmt(item)}{comma}')
        lines.append(f'        ],')
    lines.append(f'    }},')
    entry_lines.append('\n'.join(lines))

dict_part = "BUSINESS_WORLD_KNOWLEDGE = {\n" + "\n".join(entry_lines) + "\n}"
func_part = '''


def get_business_intelligence(code: str, name: str) -> dict:
    """获取企业世界知识"""
    if code in BUSINESS_WORLD_KNOWLEDGE:
        return BUSINESS_WORLD_KNOWLEDGE[code]
    for key, val in BUSINESS_WORLD_KNOWLEDGE.items():
        if val.get("name") == name:
            return val
    return {}
'''

new_content = dict_part + func_part
ast.parse(new_content)

with open('world_knowledge.py', 'w') as f:
    f.write(new_content)

print(f"Written {len(merged)} entries successfully")

# Verify
import importlib, world_knowledge
importlib.reload(world_knowledge)
print(f"Verify: {len(world_knowledge.BUSINESS_WORLD_KNOWLEDGE)} entries")