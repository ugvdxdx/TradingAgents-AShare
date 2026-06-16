#!/usr/bin/env python3
"""增量采集最新研报数据并更新到 fundamentals。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.research.collector import ResearchCollector
from tradingagents.research.cleaner import ResearchCleaner
from tradingagents.research.extractor import KnowledgeExtractor
from tradingagents.research.store import KnowledgeStore

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
    """Step 1: 增量采集最新数据"""
    print('=' * 60)
    print('Step 1: 增量采集最新圈子数据')
    print('=' * 60)

    collector = ResearchCollector(db_path=DB_PATH)
    result = collector.collect(
        cookie=COOKIE,
        max_pages=20,
        incremental=True,
        date_from='2026-06-15',
        date_to='2026-06-16',
    )
    print(f'采集完成: new={result["new"]}, updated={result["updated"]}, errors={result["errors"]}')
    print(f'最新帖子时间: {result.get("last_created_at", "N/A")}')

    # 查看未处理数量
    db = collector._get_db()
    unprocessed = db.execute('SELECT count(*) FROM raw_feeds WHERE is_processed = 0').fetchone()[0]
    print(f'未处理帖子: {unprocessed} 条')
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

    db = collector._get_db()
    rows = db.execute("""
        SELECT feed_id, text, title, created_at, author_name
        FROM raw_feeds
        WHERE is_processed = 0 AND text IS NOT NULL AND length(text) > 10
        ORDER BY created_at ASC
    """).fetchall()

    total = len(rows)
    print(f'待处理帖子: {total} 条')

    if total == 0:
        print('无需处理')
        collector.close()
        store.close()
        return

    success = 0
    fail = 0
    for i, r in enumerate(rows):
        raw = dict(r)
        try:
            cleaned = cleaner.clean(raw)
            knowledge = extractor.extract(cleaned)
            knowledge.created_at = cleaned.created_at
            store.save(knowledge)
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()
            success += 1
            print(f'  [{i+1}/{total}] {raw["created_at"][:10]} | {knowledge.summary[:50]}... | sectors={len(knowledge.sector_views)} stocks={len(knowledge.stock_mentions)}')
        except Exception as e:
            fail += 1
            print(f'  [{i+1}/{total}] 失败: {raw["feed_id"]} - {e}')
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()

    print(f'\n提取完成: success={success}, fail={fail}')
    collector.close()
    store.close()


def step3_update_fundamentals():
    """Step 3: 更新 fundamentals JSON"""
    print('\n' + '=' * 60)
    print('Step 3: 更新 fundamentals JSON')
    print('=' * 60)

    # 直接调用 update_fundamentals_from_research 的 main
    from update_fundamentals_from_research import (
        load_fundamentals, build_name_to_code_map,
        extract_stock_knowledge, get_llm_helper,
        match_stock_to_fundamental, process_stock,
    )

    project_dir = os.path.dirname(os.path.abspath(__file__))
    fundamentals_dir = os.path.join(project_dir, 'fundamentals')

    fundamentals = load_fundamentals(fundamentals_dir)
    name_to_code = build_name_to_code_map(fundamentals)
    stock_knowledge = extract_stock_knowledge(DB_PATH)
    llm = get_llm_helper()

    # 库中最新研报日期（research_catalysts 回看基准）
    latest_db_date = ''
    for kn in stock_knowledge.values():
        for m in kn.get('mentions', []):
            if m.get('date', '') > latest_db_date:
                latest_db_date = m['date']

    # 只处理有新提及的个股
    matched_stocks = []
    for name, knowledge in stock_knowledge.items():
        matched_code = match_stock_to_fundamental(name, knowledge, fundamentals, name_to_code)
        if matched_code:
            matched_stocks.append((name, matched_code, knowledge))

    # 过滤出有近期提及的（6月15日之后）
    recent_stocks = []
    for name, code, knowledge in matched_stocks:
        mentions = knowledge.get('mentions', [])
        has_recent = any(m.get('date', '') >= '2026-06-15' for m in mentions)
        has_recent_sector = any(sv.get('date', '') >= '2026-06-15' for sv in knowledge.get('sector_views', []))
        if has_recent or has_recent_sector:
            recent_stocks.append((name, code, knowledge))

    print(f'近期有新提及的个股: {len(recent_stocks)} 个')

    updated_count = 0
    for i, (name, code, knowledge) in enumerate(recent_stocks, 1):
        print(f'  [{i}/{len(recent_stocks)}] {name} ({code})')
        try:
            success = process_stock(llm, name, code, fundamentals, knowledge, fundamentals_dir,
                                     latest_db_date=latest_db_date)
            if success:
                updated_count += 1
                print(f'    ✓ 已更新')
            else:
                print(f'    - 无增量信息')
        except Exception as e:
            print(f'    ✗ 失败: {e}')

    print(f'\n更新完成: {updated_count}/{len(recent_stocks)}')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--step', type=int, default=0, help='1=采集, 2=提取, 3=更新fundamentals, 0=全部')
    args = parser.parse_args()

    if args.step == 0 or args.step == 1:
        step1_collect()
    if args.step == 0 or args.step == 2:
        step2_extract()
    if args.step == 0 or args.step == 3:
        step3_update_fundamentals()
