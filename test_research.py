#!/usr/bin/env python3
"""研报知识系统集成测试。

用已有的 xiaoe_feed_data.json 数据验证全流水线。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.research.cleaner import ResearchCleaner
from tradingagents.research.extractor import KnowledgeExtractor
from tradingagents.research.store import KnowledgeStore
from tradingagents.research.service import ResearchService


def test_cleaner():
    """测试清洗层。"""
    print('=' * 60)
    print('L2 清洗层测试')
    print('=' * 60)

    with open('xiaoe_feed_data.json', 'r') as f:
        data = json.load(f)

    cleaner = ResearchCleaner()
    for feed in data['feeds'][:3]:
        raw = {
            'feed_id': feed['id'],
            'text': feed.get('content', {}).get('text', '') if isinstance(feed.get('content'), dict) else '',
            'title': feed.get('title', ''),
            'created_at': feed.get('created_at', ''),
            'author_name': feed.get('author', {}).get('nickname', '') if isinstance(feed.get('author'), dict) else '',
        }
        cleaned = cleaner.clean(raw)
        print(f'\n  feed_id: {cleaned.feed_id}')
        print(f'  info_type: {cleaned.info_type.value}')
        print(f'  sectors: {cleaned.sectors}')
        print(f'  word_count: {cleaned.word_count}')
        print(f'  segments: {len(cleaned.segments)}')
        print(f'  summary: {cleaned.text[:80]}...')


def test_extractor():
    """测试提取层 (规则回退模式)。"""
    print('\n' + '=' * 60)
    print('L3 提取层测试 (规则回退)')
    print('=' * 60)

    with open('xiaoe_feed_data.json', 'r') as f:
        data = json.load(f)

    cleaner = ResearchCleaner()
    extractor = KnowledgeExtractor(llm_helper=None)  # 不调 LLM, 用规则回退

    for feed in data['feeds'][:3]:
        raw = {
            'feed_id': feed['id'],
            'text': feed.get('content', {}).get('text', '') if isinstance(feed.get('content'), dict) else '',
            'title': feed.get('title', ''),
            'created_at': feed.get('created_at', ''),
            'author_name': '',
        }
        cleaned = cleaner.clean(raw)
        knowledge = extractor.extract(cleaned)
        print(f'\n  feed_id: {knowledge.feed_id}')
        print(f'  info_type: {knowledge.info_type}')
        print(f'  summary: {knowledge.summary}')
        print(f'  sector_views: {len(knowledge.sector_views)}')
        for sv in knowledge.sector_views:
            print(f'    - {sv.sector}: {sv.viewpoint[:60]} ({sv.sentiment})')
        print(f'  key_insights: {len(knowledge.key_insights)}')
        print(f'  risk_warnings: {len(knowledge.risk_warnings)}')


def test_store():
    """测试存储层。"""
    print('\n' + '=' * 60)
    print('L4 存储层测试')
    print('=' * 60)

    db_path = '/tmp/test_research.db'
    if os.path.exists(db_path):
        os.remove(db_path)

    with open('xiaoe_feed_data.json', 'r') as f:
        data = json.load(f)

    cleaner = ResearchCleaner()
    extractor = KnowledgeExtractor(llm_helper=None)
    store = KnowledgeStore(db_path=db_path)

    for feed in data['feeds']:
        raw = {
            'feed_id': feed['id'],
            'text': feed.get('content', {}).get('text', '') if isinstance(feed.get('content'), dict) else '',
            'title': feed.get('title', ''),
            'created_at': feed.get('created_at', ''),
            'author_name': '',
        }
        cleaned = cleaner.clean(raw)
        knowledge = extractor.extract(cleaned)
        knowledge.created_at = cleaned.created_at
        result = store.save(knowledge)
        print(f'  {knowledge.feed_id}: {"新增" if result else "已存在"}')

    stats = store.stats()
    print(f'\n  统计: {json.dumps(stats, ensure_ascii=False, indent=2)}')

    # 测试查询
    print('\n  --- 查询测试 ---')
    sectors = store.get_all_sectors()
    print(f'  行业列表: {sectors}')
    for sector in sectors[:3]:
        rows = store.query_by_sector(sector, days=365)
        print(f'  {sector}: {len(rows)} 条知识')

    # 测试快照
    snap_id = store.create_snapshot('2026-06-15')
    print(f'\n  快照创建: id={snap_id}')
    snap = store.get_snapshot('2026-06-15')
    print(f'  快照内容: {snap["feed_count"]} 条知识')

    store.close()


def test_service():
    """测试服务层。"""
    print('\n' + '=' * 60)
    print('L5 服务层测试')
    print('=' * 60)

    db_path = '/tmp/test_research.db'
    svc = ResearchService(db_path=db_path)

    # 测试辩论查询
    knowledge = svc.query_for_debate(sector='光通信', stock_name='中际旭创', days=365)
    print(f'\n  辩论查询 (光通信):')
    print(f'    sector_knowledge: {len(knowledge["sector_knowledge"])} 条')
    print(f'    stock_knowledge: {len(knowledge["stock_knowledge"])} 条')
    print(f'    recent_insights: {len(knowledge["recent_insights"])} 条')
    print(f'    risk_warnings: {len(knowledge["risk_warnings"])} 条')

    # 格式化为 prompt
    prompt_text = svc.format_knowledge_for_prompt(knowledge)
    print(f'\n  Prompt 格式化输出:')
    print(prompt_text[:500])

    # 测试每日复盘
    review = svc.get_daily_review('2026-06-15')
    print(f'\n  每日复盘 (2026-06-15):')
    print(f'    market_overview: {review.get("market_overview", "")[:80]}')
    print(f'    sector_views: {review.get("sector_views", [])}')
    print(f'    key_insights: {len(review.get("key_insights", []))} 条')

    # 测试回测
    bt = svc.backtest_compare('2026-06-15', [
        {'code': '300308', 'name': '中际旭创', 'return_pct': 5.2},
        {'code': '300502', 'name': '新易盛', 'return_pct': 3.1},
    ])
    print(f'\n  回测对比: hit_rate={bt["hit_rate"]}%, covered_stocks={bt["covered_stocks"]}')

    svc.close()


if __name__ == '__main__':
    test_cleaner()
    test_extractor()
    test_store()
    test_service()
    print('\n✅ 全部测试通过')
