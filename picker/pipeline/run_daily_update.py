#!/usr/bin/env python3
"""增量采集最新研报数据并更新到 fundamentals。

变更要点 (vs 旧版):
  - Cookie 改从环境变量 XIAOE_COOKIE 读取, 不再硬编码
  - date_from/date_to 默认动态计算 (近 3 天 ~ 今天), 不再写死日期
  - 提取失败标记 is_processed=2 (可重试), 不再永久跳过
  - Step 2 结束后自动创建每日知识快照 (回测用)
  - Cookie 过期时以非零退出码退出

使用:
  uv run python3 run_daily_update.py                  # 默认: 近3天增量+提取+更新fundamentals
  uv run python3 run_daily_update.py --step 1         # 只采集
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from tradingagents.research.collector import ResearchCollector
from tradingagents.research.cleaner import ResearchCleaner
from tradingagents.research.extractor import KnowledgeExtractor
from tradingagents.research.store import KnowledgeStore

from picker import paths

DB_PATH = paths.RESEARCH_DB


def get_cookie():
    """从环境变量读取 Cookie, 缺失则报错退出。"""
    cookie = os.getenv('XIAOE_COOKIE', '').strip()
    if not cookie:
        print('✗ 未设置环境变量 XIAOE_COOKIE, 请在 .env 中配置后重试。')
        sys.exit(2)
    return cookie


def step1_collect(cookie, date_from, date_to):
    """Step 1: 增量采集最新数据"""
    print('=' * 60)
    print(f'Step 1: 增量采集圈子数据 ({date_from} ~ {date_to})')
    print('=' * 60)

    collector = ResearchCollector(db_path=DB_PATH)
    result = collector.collect(
        cookie=cookie,
        max_pages=20,
        incremental=True,
        date_from=date_from,
        date_to=date_to,
    )
    print(f'采集完成: new={result["new"]}, updated={result["updated"]}, errors={result["errors"]}')
    print(f'最新帖子时间: {result.get("last_created_at", "N/A")}')

    # Cookie 过期感知
    if result.get('cookie_expired'):
        print('✗ Cookie 已过期, 请重新获取 XIAOE_COOKIE 后重试。')
        collector.close()
        sys.exit(3)

    # 查看未处理数量
    db = collector._get_db()
    unprocessed = db.execute('SELECT count(*) FROM raw_feeds WHERE is_processed = 0').fetchone()[0]
    failed = db.execute('SELECT count(*) FROM raw_feeds WHERE is_processed = 2').fetchone()[0]
    print(f'未处理帖子: {unprocessed} 条, 失败待重试: {failed} 条')
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
            # 失败标记为 is_processed=2, 可重试
            db.execute('UPDATE raw_feeds SET is_processed = 2 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()

    print(f'\n提取完成: success={success}, fail={fail}')

    # 创建每日知识快照 (回测用)
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        store.create_snapshot(today, snap_type='daily')
        print(f'已创建每日知识快照: {today}')
    except Exception as e:
        print(f'⚠ 创建快照失败 (不影响主流程): {e}')

    collector.close()
    store.close()


def step3_update_fundamentals(date_from_recent):
    """Step 3: 更新 fundamentals JSON

    注意：此函数调用旧的增量追加逻辑 (update_fundamentals_from_research)。
    推荐使用 run_daily_maintenance.py step3 走新的彻底重写逻辑 (refresh_fundamentals.py)。

    Args:
        date_from_recent: 只处理该日期之后有新提及的个股 (YYYY-MM-DD)
    """
    print('\n' + '=' * 60)
    print('Step 3: 更新 fundamentals JSON (旧·增量追加)')
    print('=' * 60)

    # 直接调用 update_fundamentals_from_research 的 main
    from picker.pipeline.update_fundamentals_from_research import (
        load_fundamentals, build_name_to_code_map,
        extract_stock_knowledge, get_llm_helper,
        match_stock_to_fundamental, process_stock,
    )

    project_dir = paths.PROJECT_ROOT
    fundamentals_dir = paths.FUNDAMENTALS_DIR

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

    # 只处理有近期提及的个股
    matched_stocks = []
    for name, knowledge in stock_knowledge.items():
        matched_code = match_stock_to_fundamental(name, knowledge, fundamentals, name_to_code)
        if matched_code:
            matched_stocks.append((name, matched_code, knowledge))

    # 过滤出有近期提及的个股
    recent_stocks = []
    for name, code, knowledge in matched_stocks:
        mentions = knowledge.get('mentions', [])
        has_recent = any(m.get('date', '') >= date_from_recent for m in mentions)
        has_recent_sector = any(sv.get('date', '') >= date_from_recent for sv in knowledge.get('sector_views', []))
        if has_recent or has_recent_sector:
            recent_stocks.append((name, code, knowledge))

    print(f'近期({date_from_recent}后)有新提及的个股: {len(recent_stocks)} 个')

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
    parser.add_argument('--step', type=int, default=0, help='1=采集, 2=提取, 3=更新fundamentals, 4=更新世界知识, 0=全部')
    parser.add_argument('--from', dest='date_from', default='', help='采集起始日期 YYYY-MM-DD (默认近3天)')
    parser.add_argument('--to', dest='date_to', default='', help='采集结束日期 YYYY-MM-DD (默认今天)')
    args = parser.parse_args()

    # 动态默认日期: 近 3 天 ~ 今天 (增量更新窗口)
    today = datetime.now().strftime('%Y-%m-%d')
    default_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    date_from = args.date_from or default_from
    date_to = args.date_to or today

    if args.step == 0 or args.step == 1:
        cookie = get_cookie()
        step1_collect(cookie, date_from, date_to)
    if args.step == 0 or args.step == 2:
        step2_extract()
    if args.step == 0 or args.step == 3:
        step3_update_fundamentals(date_from_recent=date_from)
    if args.step == 0 or args.step == 4:
        print('\n' + '=' * 60)
        print('Step 4: 更新世界知识 (宏观 .md + 热门个股)')
        print('=' * 60)
        try:
            from picker.pipeline.update_world_knowledge import main as update_wk
            update_wk()
        except Exception as e:
            print(f'  ⚠ 世界知识更新失败 (不影响主流程): {e}')
