#!/usr/bin/env python3
"""Inject batch2 only into current world_knowledge.py (already has batch1)"""
import json

# Load batch2
with open('batch2_output.json') as f:
    batch2 = json.load(f)

# Load current world_knowledge
from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B
existing_codes = set(B.keys())
new_codes = [c for c in batch2 if c not in existing_codes]
print(f"Batch2 total: {len(batch2)}, already in: {len(batch2)-len(new_codes)}, to add: {len(new_codes)}")

if not new_codes:
    print("Nothing new to add")
    exit()

NEW = {c: batch2[c] for c in new_codes}

# Read world_knowledge.py
with open('world_knowledge.py', 'r') as f:
    content = f.read()

# } followed by two newlines then def
marker = "\n}\n\n\ndef get_business_intelligence"
if marker not in content:
    print("ERROR: marker not found, trying alternative...")
    marker = "}\n\n\ndef get_business_intelligence"

assert marker in content, f"Cannot find insertion point"

# Build insertion
entries = []
for code, entry in NEW.items():
    entry_str = f'    "{code}": ' + json.dumps(entry, ensure_ascii=False, indent=8)
    entry_str = entry_str.replace('\n        ', '\n            ')
    entries.append(entry_str)

insert_str = "\n" + ",\n".join(entries) + ",\n"
content = content.replace(marker, insert_str + marker)

with open('world_knowledge.py', 'w') as f:
    f.write(content)

# Verify using fresh import
import importlib, world_knowledge
importlib.reload(world_knowledge)
from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B2
print(f"Total entries after injection: {len(B2)}")
for code in list(NEW.keys())[:5]:
    if code in B2:
        print(f"  {code} {B2[code]['name']}: OK")
    else:
        print(f"  {code}: MISSING (will be in file, module cached?)")
        import ast
        ast.parse(open('world_knowledge.py').read())
        print("  File syntax: OK")