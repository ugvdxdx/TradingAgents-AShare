#!/usr/bin/env python3
"""Inject all batch JSON files into world_knowledge.py"""
import json, glob

ALL = {}
for fname in sorted(glob.glob('batch*_output.json')):
    with open(fname) as f:
        batch = json.load(f)
    for code, entry in batch.items():
        if code not in ALL:
            ALL[code] = entry
    print(f"  Loaded {fname}: {len(batch)} entries")

print(f"Total unique: {len(ALL)}")

# Restore clean state from backup
import os
os.system('cp world_knowledge.py.bak world_knowledge.py')

# Read world_knowledge.py
with open('world_knowledge.py', 'r') as f:
    content = f.read()

marker = "}\n\n\ndef get_business_intelligence"
assert marker in content, f"Cannot find insertion point"

# Build insertion string
entries = []
for code, entry in ALL.items():
    entry_str = f'    "{code}": ' + json.dumps(entry, ensure_ascii=False, indent=8)
    entry_str = entry_str.replace('\n        ', '\n            ')
    entries.append(entry_str)

insert_str = "\n" + ",\n".join(entries) + ",\n"
content = content.replace(marker, insert_str + marker)

with open('world_knowledge.py', 'w') as f:
    f.write(content)

# Verify
from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B
print(f"Total entries now: {len(B)}")
# Sample check
for code in list(ALL.keys())[:5]:
    if code in B:
        print(f"  {code} {B[code]['name']}: OK")
    else:
        print(f"  {code}: MISSING!")