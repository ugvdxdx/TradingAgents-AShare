#!/usr/bin/env python3
"""Inject ALL batch JSONs into world_knowledge.py - exact format match"""
import json, glob, sys

ALL_NEW = {}
for fname in sorted(glob.glob('batch*_output.json')):
    with open(fname) as f:
        batch = json.load(f)
    ALL_NEW.update(batch)

from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B
existing = set(B.keys())
to_add = {c: ALL_NEW[c] for c in ALL_NEW if c not in existing}
print(f"To add: {len(to_add)}")
if not to_add:
    sys.exit(0)

with open('world_knowledge.py', 'r') as f:
    content = f.read()

marker = "}\n\n\ndef get_business_intelligence"
assert marker in content

def format_entry(code, data):
    """Format exactly matching original style: 4-space base, 8-space keys, 12-space list items"""
    lines = []
    lines.append(f'    "{code}": {{')
    lines.append(f'        "name": {json.dumps(data["name"], ensure_ascii=False)},')
    lines.append(f'        "industry": {json.dumps(data["industry"], ensure_ascii=False)},')
    for field in ['strengths', 'weaknesses', 'growth_drivers', 'headwinds', 'geopolitical_risks', 'geopolitical_opportunities']:
        items = data.get(field, [])
        lines.append(f'        "{field}": [')
        for idx, item in enumerate(items):
            comma = "," if idx < len(items) - 1 else ""
            # 12-space indent for list items
            lines.append(f'            {json.dumps(item, ensure_ascii=False)}{comma}')
        lines.append(f'        ],')
    # Remove trailing comma from last field, add closing brace
    # Actually the original has trailing comma on last field too, then },
    lines.append(f'    }},')
    return '\n'.join(lines)

entries = []
for code in sorted(to_add.keys()):
    entries.append(format_entry(code, to_add[code]))

insert_str = "\n" + "\n".join(entries) + "\n"
content = content.replace(marker, insert_str + marker)

import ast
ast.parse(content)

with open('world_knowledge.py', 'w') as f:
    f.write(content)

import re
codes = set(re.findall(r'    "(\d{6})":', content))
print(f"Total entries: {len(codes)}")

import importlib, world_knowledge
importlib.reload(world_knowledge)
print(f"Module: {len(world_knowledge.BUSINESS_WORLD_KNOWLEDGE)}")