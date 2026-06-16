#!/usr/bin/env python3
"""测试 Cookie 是否有效"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.research.collector import ResearchCollector

COOKIE = (
    'sensorsdata2015jssdkcross=%7B%22%24device_id%22%3A%2219ecb3aed7c132c-04d46ebd3def7c-'
    '7e433c49-2073600-19ecb3aed7d212%22%7D; app_id=appv5zuapfz7716; '
    'activity_id=appv5zuapfz7716-c_62a95f0db904a_yYyOAuyh3445; '
    'last_created_token_app_id=appv5zuapfz7716; '
    'pc_token_appv5zuapfz7716=6b3535c4136351bbe4313ed547d8e815; '
    'user_id_appv5zuapfz7716=u_6a2febec326f6_forCo1x2NO; '
    'union_id=oTHW5v8aXlUQ_ZGErBxa4ut-gR9g; '
    'sa_jssdk_2015_quanzi_xiaoe-tech_com=%7B%22distinct_id%22%3A%2219ecb3aed7c132c-'
    '04d46ebd3def7c-7e433c49-2073600-19ecb3aed7d212%22%2C%22first_id%22%3A%22%22%2C'
    '%22props%22%3A%7B%22%24latest_traffic_source_type%22%3A%22%E7%9B%B4%E6%8E%A5%E6%B5%81'
    '%E9%87%8F%22%2C%22%24latest_search_keyword%22%3A%22%E6%9C%AA%E5%8F%96%E5%88%B0%E5%80%BC_'
    '%E7%9B%B4%E6%8E%A5%E6%89%93%E5%BC%80%22%2C%22%24latest_referrer%22%3A%22%22%7D%7D'
)

c = ResearchCollector(db_path='/tmp/test_cookie2.db')

# 测试 cursor 分页 - 继续翻更多页
import json
cursor = ''
for page in range(1, 20):
    data = c._fetch_page(cookie=COOKIE, cursor=cursor, page_size=10)
    feeds = data.get('data', {}).get('list', [])
    next_cursor = data.get('data', {}).get('cursor', '')
    print(f'page={page}: feeds={len(feeds)}, next_cursor={next_cursor[:30] if next_cursor else "None"}')
    for f in feeds:
        text = f.get('content', {}).get('text', '') if isinstance(f.get('content'), dict) else ''
        print(f'  {f["id"]} | {f.get("created_at", "")} | text_len={len(text)}')
    if not next_cursor:
        print('  No more cursor, stopping')
        break
    cursor = next_cursor

c.close()
