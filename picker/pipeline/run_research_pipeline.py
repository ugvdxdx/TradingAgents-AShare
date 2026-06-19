#!/usr/bin/env python3
"""采集圈子数据并执行 LLM 知识提取 (全量/增量均可)。

变更要点 (vs 旧版):
  - Cookie 改从环境变量 XIAOE_COOKIE 读取, 不再硬编码进源码
  - date_from/date_to 默认动态计算 (近 90 天 ~ 今天), 不再写死日期
  - 提取失败的帖子标记 is_processed=2 (而非永久跳过), 可用 --retry-failed 重试
  - Cookie 过期时以非零退出码退出, 便于 cron/调度感知

使用:
  uv run python3 run_research_pipeline.py                 # 默认: 近90天全量采集+提取
  uv run python3 run_research_pipeline.py --step 2        # 只跑提取
  uv run python3 run_research_pipeline.py --retry-failed  # 重试提取失败的帖子
  uv run python3 run_research_pipeline.py --from 2026-04-01 --to 2026-06-15
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
    """Step 1: 采集原始数据"""
    print('=' * 60)
    print(f'Step 1: 采集 {date_from} ~ {date_to} 圈子数据')
    print('=' * 60)

    collector = ResearchCollector(db_path=DB_PATH)
    result = collector.collect(
        cookie=cookie,
        max_pages=500,
        incremental=False,
        date_from=date_from,
        date_to=date_to,
    )
    print(f'\n采集完成: new={result["new"]}, updated={result["updated"]}, errors={result["errors"]}')
    print(f'最新帖子时间: {result.get("last_created_at", "N/A")}')

    # Cookie 过期感知
    if result.get('cookie_expired'):
        print('✗ Cookie 已过期, 请重新获取 XIAOE_COOKIE 后重试。')
        collector.close()
        sys.exit(3)

    # 查看统计
    db = collector._get_db()
    total = db.execute('SELECT count(*) FROM raw_feeds').fetchone()[0]
    date_range = db.execute('SELECT min(created_at), max(created_at) FROM raw_feeds').fetchone()
    print(f'数据库总计: {total} 条帖子')
    print(f'日期范围: {date_range[0]} ~ {date_range[1]}')

    collector.close()
    return result


def step2_extract(retry_failed=False):
    """Step 2: 清洗 + LLM 知识提取 + 存储

    Args:
        retry_failed: True=只重试提取失败的帖子(is_processed=2)
    """
    print('\n' + '=' * 60)
    print(f'Step 2: 清洗 + LLM 知识提取 + 存储{" (重试失败态)" if retry_failed else ""}')
    print('=' * 60)

    collector = ResearchCollector(db_path=DB_PATH)
    cleaner = ResearchCleaner()
    extractor = KnowledgeExtractor()
    store = KnowledgeStore(db_path=DB_PATH)

    # 获取待处理帖子: 失败重试模式只取 is_processed=2, 否则取 is_processed=0
    db = collector._get_db()
    if retry_failed:
        rows = db.execute("""
            SELECT feed_id, text, title, created_at, author_name
            FROM raw_feeds
            WHERE is_processed = 2 AND text IS NOT NULL AND length(text) > 10
            ORDER BY created_at ASC
        """).fetchall()
    else:
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
            # 清洗
            cleaned = cleaner.clean(raw)
            # 提取
            knowledge = extractor.extract(cleaned)
            knowledge.created_at = cleaned.created_at
            # 存储
            store.save(knowledge)
            # 标记成功
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()
            success += 1
            if (i + 1) % 5 == 0 or (i + 1) == total:
                print(f'  [{i+1}/{total}] {raw["created_at"][:10]} | {knowledge.summary[:40]}... | sectors={len(knowledge.sector_views)} stocks={len(knowledge.stock_mentions)}')
        except Exception as e:
            fail += 1
            print(f'  [{i+1}/{total}] 失败: {raw["feed_id"]} - {e}')
            # 标记为失败态(is_processed=2), 可后续重试, 不再永久跳过
            db.execute('UPDATE raw_feeds SET is_processed = 2 WHERE feed_id = ?', (raw['feed_id'],))
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
    parser.add_argument('--retry-failed', action='store_true', help='重试提取失败的帖子 (仅对 step 2 生效)')
    parser.add_argument('--from', dest='date_from', default='', help='起始日期 YYYY-MM-DD (默认近90天)')
    parser.add_argument('--to', dest='date_to', default='', help='结束日期 YYYY-MM-DD (默认今天)')
    args = parser.parse_args()

    # 动态默认日期: 近 90 天 ~ 今天
    today = datetime.now().strftime('%Y-%m-%d')
    default_from = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    date_from = args.date_from or default_from
    date_to = args.date_to or today

    if args.step == 0 or args.step == 1:
        cookie = get_cookie()
        step1_collect(cookie, date_from, date_to)
    if args.step == 0 or args.step == 2:
        step2_extract(retry_failed=args.retry_failed)
    if args.step == 0 or args.step == 3:
        step3_verify()
