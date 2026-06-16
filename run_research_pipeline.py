#!/usr/bin/env python3
"""全量采集 4月1日-6月15日 小鹅通圈子数据，并执行 LLM 知识提取。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.research.collector import ResearchCollector
from tradingagents.research.cleaner import ResearchCleaner
from tradingagents.research.extractor import KnowledgeExtractor
from tradingagents.research.store import KnowledgeStore

# Cookie (从用户提供的 cURL 中提取)
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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'research.db')

def step1_collect():
    """Step 1: 采集原始数据"""
    print('=' * 60)
    print('Step 1: 采集 2026-04-01 ~ 2026-06-15 圈子数据')
    print('=' * 60)

    collector = ResearchCollector(db_path=DB_PATH)
    result = collector.collect(
        cookie=COOKIE,
        max_pages=500,
        incremental=False,
        date_from='2026-04-01',
        date_to='2026-06-15',
    )
    print(f'\n采集完成: new={result["new"]}, updated={result["updated"]}, errors={result["errors"]}')
    print(f'最新帖子时间: {result.get("last_created_at", "N/A")}')

    # 查看统计
    db = collector._get_db()
    total = db.execute('SELECT count(*) FROM raw_feeds').fetchone()[0]
    date_range = db.execute('SELECT min(created_at), max(created_at) FROM raw_feeds').fetchone()
    print(f'数据库总计: {total} 条帖子')
    print(f'日期范围: {date_range[0]} ~ {date_range[1]}')

    collector.close()
    return result

def step2_extract():
    """Step 2: 清洗 + LLM 知识提取 + 存储"""
    print('\n' + '=' * 60)
    print('Step 2: 清洗 + LLM 知识提取 + 存储')
    print('=' * 60)

    collector = ResearchCollector(db_path=DB_PATH)
    cleaner = ResearchCleaner()
    extractor = KnowledgeExtractor()
    store = KnowledgeStore(db_path=DB_PATH)

    # 获取所有未处理的帖子
    db = collector._get_db()
    rows = db.execute("""
        SELECT feed_id, text, title, created_at, author_name
        FROM raw_feeds
        WHERE is_processed = 0 AND text IS NOT NULL AND length(text) > 10
        ORDER BY created_at ASC
    """).fetchall()

    total = len(rows)
    print(f'待处理帖子: {total} 条')

    success = 0
    fail = 0
    for i, r in enumerate(rows):
        raw = dict(r)
        try:
            # 清洗
            cleaned = cleaner.clean(raw)
            # 提取
            knowledge = extractor.extract(cleaned)
            knowledge.created_at = cleaned.created_at
            # 存储
            store.save(knowledge)
            # 标记已处理
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()
            success += 1
            if (i + 1) % 5 == 0 or (i + 1) == total:
                print(f'  [{i+1}/{total}] {raw["created_at"][:10]} | {knowledge.summary[:40]}... | sectors={len(knowledge.sector_views)} stocks={len(knowledge.stock_mentions)}')
        except Exception as e:
            fail += 1
            print(f'  [{i+1}/{total}] 失败: {raw["feed_id"]} - {e}')
            # 标记为已处理避免重复
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()

    print(f'\n提取完成: success={success}, fail={fail}')

    # 统计
    stats = store.stats()
    print(f'\n知识库统计:')
    print(f'  通用知识: {stats["general_count"]} 条')
    print(f'  行业知识: {stats["sector_count"]} 条')
    print(f'  每日复盘: {stats["daily_review_count"]} 条')
    print(f'  行业列表: {stats["sectors"]}')
    print(f'  日期范围: {stats["date_range"]}')

    collector.close()
    store.close()

def step3_verify():
    """Step 3: 验证知识库完整性"""
    print('\n' + '=' * 60)
    print('Step 3: 验证知识库完整性')
    print('=' * 60)

    store = KnowledgeStore(db_path=DB_PATH)
    stats = store.stats()

    # 按日期统计
    db = store._get_db()
    date_stats = db.execute("""
        SELECT substr(created_at, 1, 10) as date, count(*) as cnt
        FROM general_knowledge
        GROUP BY date
        ORDER BY date
    """).fetchall()

    print(f'\n按日期分布:')
    for r in date_stats:
        print(f'  {r[0]}: {r[1]} 条')

    # 按行业统计
    sector_stats = db.execute("""
        SELECT sector, count(*) as cnt
        FROM sector_knowledge
        GROUP BY sector
        ORDER BY cnt DESC
    """).fetchall()

    print(f'\n按行业分布:')
    for r in sector_stats:
        print(f'  {r[0]}: {r[1]} 条')

    # 按信息类型统计
    type_stats = db.execute("""
        SELECT info_type, count(*) as cnt
        FROM general_knowledge
        GROUP BY info_type
        ORDER BY cnt DESC
    """).fetchall()

    print(f'\n按信息类型分布:')
    for r in type_stats:
        print(f'  {r[0]}: {r[1]} 条')

    store.close()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--step', type=int, default=0, help='1=采集, 2=提取, 3=验证, 0=全部')
    args = parser.parse_args()

    if args.step == 0 or args.step == 1:
        step1_collect()
    if args.step == 0 or args.step == 2:
        step2_extract()
    if args.step == 0 or args.step == 3:
        step3_verify()
