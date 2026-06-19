#!/usr/bin/env python3
"""测试反爬增强版百科"""
import sys, time, random
sys.path.insert(0, '.')
from picker.knowledge.fundamental_agent import _fetch_baike_summary

for name in ['比亚迪', '中国石油', '中芯国际', '工商银行', '海康威视']:
    time.sleep(1 + random.random())
    desc = _fetch_baike_summary(name)
    print(f"{name}: {'✅' if desc else '❌'} {desc[:80] if desc else ''}")