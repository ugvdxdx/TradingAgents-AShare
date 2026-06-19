#!/usr/bin/env python3
"""Inject new entries into world_knowledge.py"""
import json

# Load new entries
with open('batch1_output.json') as f:
    NEW = json.load(f)

# Read world_knowledge.py
with open('world_knowledge.py', 'r') as f:
    content = f.read()

# Find the closing } of BUSINESS_WORLD_KNOWLEDGE dict (before the function)
# Pattern: last "    },\n}\n\n\ndef get_business_intelligence"
marker = "\n}\n\n\ndef get_business_intelligence"
if marker not in content:
    print("ERROR: marker not found")
    # Try alternative
    marker = "}\n\n\ndef get_business_intelligence"

assert marker in content, f"Cannot find insertion point in world_knowledge.py"

# Build insertion string
entries = []
for code, entry in NEW.items():
    entry_str = f'    "{code}": ' + json.dumps(entry, ensure_ascii=False, indent=8)
    # Fix indent: json.dumps uses 4-space indent, we need 12-space (4 + 8)
    entry_str = entry_str.replace('\n        ', '\n            ')
    entries.append(entry_str)

insert_str = "\n" + ",\n".join(entries) + ",\n"

# Replace marker with new entries + marker
content = content.replace(marker, insert_str + marker)

with open('world_knowledge.py', 'w') as f:
    f.write(content)

print(f"Injected {len(NEW)} entries into world_knowledge.py")

# Verify
from world_knowledge import BUSINESS_WORLD_KNOWLEDGE as B
print(f"Total entries now: {len(B)}")
# Check a few
for code in list(NEW.keys())[:5]:
    if code in B:
        print(f"  {code} {B[code]['name']}: strengths={len(B[code].get('strengths',[]))}")
    else:
        print(f"  {code} MISSING!")