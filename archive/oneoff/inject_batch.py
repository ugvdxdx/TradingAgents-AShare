#!/usr/bin/env python3
"""Inject ALL batch JSON files into world_knowledge.py (from clean state)"""
import json, glob, os

ALL = {}
for fname in sorted(glob.glob('batch*_output.json')):
    with open(fname) as f:
        batch = json.load(f)
    for code, entry in batch.items():
        ALL[code] = entry
    print(f"  Loaded {fname}: {len(batch)} entries")

# Also try to save current state then restore
os.system('cp world_knowledge.py world_knowledge.py.current')

# Restore from clean backup (54 entries only)
os.system('cp world_knowledge.py.bak world_knowledge.py')

# Verify clean state
with open('world_knowledge.py.bak') as f:
    content = f.read()
found = content.count('"name":')  
# Actually check with regex
import re
codes = set(re.findall(r'    "(\d{6})":', content))
print(f"Clean backup has {len(codes)} entries")

# Now inject
marker = "}\n\n\ndef get_business_intelligence"
assert marker in content, "Cannot find insertion point"

entries = []
for code in sorted(ALL.keys()):
    entry = ALL[code]
    entry_str = f'    "{code}": ' + json.dumps(entry, ensure_ascii=False, indent=8)
    entry_str = entry_str.replace('\n        ', '\n            ')
    entries.append(entry_str)

insert_str = "\n" + ",\n".join(entries) + ",\n"
content = content.replace(marker, insert_str + marker)

# Validate syntax
import ast
try:
    ast.parse(content)
    print("Syntax: OK")
except SyntaxError as e:
    print(f"Syntax ERROR: {e}")
    exit(1)

with open('world_knowledge.py', 'w') as f:
    f.write(content)

# Final count
codes = set(re.findall(r'    "(\d{6})":', content))
print(f"Total entries after injection: {len(codes)}")
print(f"To add: {len(ALL)}, In file: {len(codes)}")

# Quick import test
import importlib, world_knowledge
importlib.reload(world_knowledge)
from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B
print(f"Module entries: {len(B)}")