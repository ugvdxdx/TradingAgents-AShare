#!/usr/bin/env python3
"""历史研报回填脚本 (用于 capital 历史重建的回测准备)。

⚠️ 安全护栏 (区别于 run_daily_update.py):
  本脚本【只做采集 + 提取入库】, 绝不执行:
    ❌ Step 3: update_fundamentals  (会用当前认知重写 fundamentals JSON → 前视偏差)
    ❌ Step 4: update_world_knowledge
    ❌ 不创建当日知识快照 (快照日期会是今天, 污染回测)

回填的研报 created_at = 帖子真实发布时间 (cleaned.created_at),
供 get_sector_momentum(cutoff_date=历史日期) 正确取到那段时间的板块动量。

流程:
  1. collect:  用 Cookie 采集 [date_from, date_to] 的圈子帖子 → raw_feeds
  2. extract:  并行 LLM 提取 (8线程) → sector_knowledge / general_knowledge
               存储串行 (sqlite 单连接写, 避免锁冲突)

用法:
  uv run python3 picker/pipeline/backfill_research.py --from 2025-06-01 --to 2026-03-19
  uv run python3 picker/pipeline/backfill_research.py --step 1 --from 2025-06-01   # 只采集
  uv run python3 picker/pipeline/backfill_research.py --step 2                      # 只提取(已采集的)
  uv run python3 picker/pipeline/backfill_research.py --workers 4                   # 指定并行度
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths

DB_PATH = paths.RESEARCH_DB


# ══════════════════════════════════════════════════════════
# Step 1: 采集 (链式往回翻页)
# ══════════════════════════════════════════════════════════

def step_collect(cookie, date_from, max_pages=500):
    """采集 date_from 之后的圈子帖子 → raw_feeds。

    ⚠️ 关键: 圈子 API 的默认翻页(从最新往老)翻不动(第二页就空),
    实测发现正确姿势是【链式往回翻】: 用 db 里最早的 feed_id 作 cursor,
    API 返回比它更早的帖子, 拿到新一批里最老的 feed_id 再作 cursor,
    依此类推直到 date_from 或翻到底。

    Args:
        cookie: 登录 Cookie
        date_from: 采集到该日期为止 (YYYY-MM-DD), 早于此的停止
        max_pages: 最大翻页数 (防失控, 每页50帖)
    """
    from tradingagents.research.collector import ResearchCollector

    print('=' * 64)
    print(f'Step 1: 链式采集历史帖子 (回溯到 {date_from})')
    print('=' * 64)

    collector = ResearchCollector(db_path=DB_PATH)
    db = collector._get_db()

    # 找当前 db 里最早的 feed_id 作为链式起点
    # (如果 db 为空, 先拉一屏最新帖作为种子)
    row = db.execute(
        'SELECT feed_id FROM raw_feeds ORDER BY created_at ASC LIMIT 1'
    ).fetchone()
    if row:
        cursor = row['feed_id']
        print(f'  起点: db 现有最早帖 {cursor}')
    else:
        # db 空, 先拿最新一屏
        print(f'  db 为空, 先拉最新一屏作为起点...')
        data = collector._fetch_page(cookie, cursor='', page_size=50)
        if data.get('code') == 23:
            print('✗ Cookie 已过期'); collector.close(); sys.exit(3)
        feeds = data.get('data', {}).get('list', [])
        _persist_feeds(db, feeds)
        cursor = data.get('data', {}).get('cursor', '')
        if not cursor or not feeds:
            print('  无帖子可采集'); collector.close(); return

    date_from_ts = date_from + ' 00:00:00'
    new_count = 0
    page = 0

    print(f"{'page':>5} {'本页最早':>12} {'本页帖数':>8} {'累计新增':>8}")
    print('-' * 40)

    while page < max_pages:
        page += 1
        try:
            data = collector._fetch_page(cookie, cursor=cursor, page_size=50)
        except Exception as e:
            print(f'  [page {page}] 请求失败: {e}'); time.sleep(2); continue

        code = data.get('code', -1)
        if code == 23:
            print('✗ Cookie 已过期, 请重新获取 XIAOE_COOKIE')
            collector.close(); sys.exit(3)
        if code != 0:
            print(f'  [page {page}] API错误 code={code} msg={data.get("msg","")}')
            time.sleep(1); continue

        feeds = data.get('data', {}).get('list', [])
        next_cursor = data.get('data', {}).get('cursor', '')

        if not feeds:
            print(f'  [page {page}] 无帖子, 翻到底了')
            break

        # 过滤掉晚于 date_from 之后已采集的 (date_to 边界由 db 去重保证)
        # 写入 db (INSERT OR IGNORE 去重)
        added = _persist_feeds(db, feeds)
        new_count += added

        dates = sorted([f.get('created_at', '')[:10] for f in feeds if f.get('created_at')])
        page_earliest = dates[0] if dates else '?'
        if page % 5 == 0 or page_earliest <= date_from or not next_cursor:
            print(f'{page:>5} {page_earliest:>12} {len(feeds):>8} {new_count:>8}')

        # 早于 date_from 则停止
        if page_earliest != '?' and page_earliest < date_from:
            print(f'  已回溯到 {date_from}, 停止')
            break

        if not next_cursor or next_cursor == cursor:
            print(f'  cursor 不再变化, 到底了')
            break
        cursor = next_cursor
        time.sleep(0.4)  # 限速

    db.commit()
    unprocessed = db.execute(
        'SELECT count(*) FROM raw_feeds WHERE is_processed = 0'
    ).fetchone()[0]
    # 覆盖范围
    span = db.execute(
        'SELECT MIN(created_at), MAX(created_at) FROM raw_feeds'
    ).fetchone()
    print(f'\n采集完成: 新增 {new_count} 帖, 未处理 {unprocessed} 条')
    print(f'  raw_feeds 时间范围: {span[0][:10]} ~ {span[1][:10]}')
    collector.close()


def _persist_feeds(db, feeds) -> int:
    """把 API 返回的 feeds 写入 raw_feeds (INSERT OR IGNORE 去重)。

    确保表有 is_processed 列 (老库可能没有, 兼容处理)。
    返回新增条数。
    """
    added = 0
    for feed in feeds:
        feed_id = feed.get('id', '')
        if not feed_id:
            continue
        created_at = feed.get('created_at', '')
        content = feed.get('content', {})
        text = ''
        if isinstance(content, dict):
            text = content.get('text', '') or ''
        elif isinstance(content, list) and content:
            text = str(content[0])
        author = feed.get('author', {})
        author_name = (author.get('nickname', '') if isinstance(author, dict)
                       else str(author))
        cur = db.execute(
            'INSERT OR IGNORE INTO raw_feeds '
            '(feed_id, community_id, author_name, title, content, text, created_at, is_processed) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, 0)',
            (feed_id, 'c_62a95f0db904a_yYyOAuyh3445', author_name,
             feed.get('title', ''), json.dumps(content, ensure_ascii=False),
             text, created_at),
        )
        if cur.rowcount > 0:
            added += 1
    db.commit()
    return added


# ══════════════════════════════════════════════════════════
# Step 2: 提取 (并行 LLM + 串行存储)
# ══════════════════════════════════════════════════════════

def step_extract(workers=8, limit=0, time_limit=0):
    """并行提取 + 串行存储。

    Args:
        workers: 提取并行度 (LLM 调用并发)
        limit: 最多处理多少条 (0=全部)
    """
    from tradingagents.research.cleaner import ResearchCleaner
    from tradingagents.research.extractor import KnowledgeExtractor
    from tradingagents.research.store import KnowledgeStore

    print('\n' + '=' * 64)
    tl_msg = f', 限时{time_limit}s' if time_limit > 0 else ''
    print(f'Step 2: 并行提取 + 串行存储 (workers={workers}{tl_msg})')
    print('=' * 64)

    # 取所有未处理帖子 (按时间正序, 与历史时序一致)
    collector_db = __import__('sqlite3').connect(DB_PATH)
    collector_db.row_factory = __import__('sqlite3').Row
    query = ('SELECT feed_id, text, title, created_at, author_name '
             'FROM raw_feeds WHERE is_processed = 0 '
             'AND text IS NOT NULL AND length(text) > 10 '
             'ORDER BY created_at ASC')
    if limit > 0:
        query += f' LIMIT {limit}'
    rows = collector_db.execute(query).fetchall()
    total = len(rows)
    print(f'待处理帖子: {total} 条')
    if total == 0:
        print('无需处理')
        collector_db.close()
        return

    # 预初始化 extractor + cleaner (cleaner 无状态, extractor 预热 LLM client)
    cleaner = ResearchCleaner()
    extractor = KnowledgeExtractor()
    # 预热: 触发 LLM client 懒加载, 避免 worker 并发首次初始化竞态
    try:
        extractor._get_llm()
    except Exception:
        pass

    store = KnowledgeStore(db_path=DB_PATH)

    # 并行提取, 串行存储
    # worker 函数: 只做 clean + extract (无副作用, 不碰 db), 返回 (feed_id, knowledge或None, error)
    def _work(row):
        raw = dict(row)
        try:
            cleaned = cleaner.clean(raw)
            knowledge = extractor.extract(cleaned)
            knowledge.created_at = cleaned.created_at  # ⚠️ 用帖子真实时间, 不是今天
            return (raw['feed_id'], knowledge, None)
        except Exception as e:
            return (raw['feed_id'], None, str(e))

    success = 0
    fail = 0
    t0 = time.time()

    ex = ThreadPoolExecutor(max_workers=workers)
    futures = {ex.submit(_work, row): row for row in rows}
    timed_out = False
    try:
        for i, fut in enumerate(as_completed(futures), 1):
            feed_id, knowledge, error = fut.result()
            if error is not None:
                fail += 1
                collector_db.execute(
                    'UPDATE raw_feeds SET is_processed = 2 WHERE feed_id = ?',
                    (feed_id,))
                collector_db.commit()
                print(f'  [{i}/{total}] ✗ {feed_id}: {error[:80]}')
            else:
                try:
                    store.save(knowledge)
                    collector_db.execute(
                        'UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?',
                        (feed_id,))
                    collector_db.commit()
                    success += 1
                    if i % 20 == 0 or i == total:
                        elapsed = time.time() - t0
                        rate = i / elapsed if elapsed > 0 else 0
                        eta = (total - i) / rate if rate > 0 else 0
                        print(f'  [{i}/{total}] ✓ {knowledge.created_at[:10]} | '
                              f'{knowledge.summary[:40]}... | '
                              f'{success}成功 {fail}失败 | {rate:.1f}帖/s ETA {eta:.0f}s')
                except Exception as e:
                    fail += 1
                    collector_db.execute(
                        'UPDATE raw_feeds SET is_processed = 2 WHERE feed_id = ?',
                        (feed_id,))
                    collector_db.commit()
                    print(f'  [{i}/{total}] ✗ 存储失败 {feed_id}: {e}')
            # 超时检查: 达到 time_limit 则停止取新结果 (已提交的未完成 future 丢弃, 下次续跑)
            if time_limit > 0 and (time.time() - t0) > time_limit:
                print(f'  ⏰ 达到限时 {time_limit}s, 停止本轮 (已处理 {success+fail}/{total})')
                timed_out = True
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    elapsed = time.time() - t0
    done = success + fail
    rate = done / elapsed if elapsed > 0 else 0
    msg = f' (限时退出, 剩余下轮续跑)' if timed_out else ''
    print(f'\n提取完成: success={success}, fail={fail}, 耗时 {elapsed:.0f}s '
          f'({rate:.1f}帖/s){msg}')
    # ⛔ 不创建快照 (回填专用, 快照日期会是今天, 污染回测)
    print('  (回填模式: 不创建知识快照, 不更新 fundamentals)')

    collector_db.close()
    store.close()


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='历史研报回填 (只采集+提取, 不碰 fundamentals)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
⚠️ 安全说明:
  本脚本【绝不】执行 update_fundamentals / update_world_knowledge,
  也不会创建当日知识快照。回填的研报用帖子真实发布时间,
  供 capital 历史重建使用。

  对比 run_daily_update.py (日常增量, 会改 fundamentals):
    正常:  run_daily_update.py            # step 0 = 全部4步
    回填:  backfill_research.py           # 只 step 1+2
        """,
    )
    parser.add_argument('--step', type=int, default=0,
                        help='1=采集, 2=提取, 0=采集+提取 (默认0)')
    parser.add_argument('--from', dest='date_from', required=False, default='',
                        help='回溯到该日期为止 YYYY-MM-DD (step 1 需要, 如 2025-06-01)')
    parser.add_argument('--workers', type=int, default=8, help='提取并行度 (默认8)')
    parser.add_argument('--limit', type=int, default=0, help='提取最多处理N条 (0=全部, 调试用)')
    parser.add_argument('--time-limit', type=int, default=0,
                        help='提取限时秒数 (0=不限; 建议480=8分钟, 配合外部续跑)')
    args = parser.parse_args()

    print('═' * 64)
    print('  历史研报回填 (只采集 + 提取, ⛔ 不更新 fundamentals)')
    print('═' * 64)

    if args.step in (0, 1):
        if not args.date_from:
            print('✗ step 1 需要 --from 参数 (回溯目标日期, 如 2025-06-01)')
            sys.exit(1)
        cookie = os.getenv('XIAOE_COOKIE', '').strip()
        if not cookie:
            print('✗ 未设置 XIAOE_COOKIE, 请在 .env 中配置')
            sys.exit(2)
        step_collect(cookie, args.date_from)

    if args.step in (0, 2):
        step_extract(workers=args.workers, limit=args.limit, time_limit=args.time_limit)

    print('\n✓ 回填完成。')
    print('  验证: 检查 research.db 的 sector_knowledge 时间范围是否覆盖目标时段')
    print('    uv run python3 -c "import sqlite3; print(sqlite3.connect(\'' + DB_PATH + '\').execute(\'SELECT MIN(created_at),MAX(created_at),COUNT(*) FROM sector_knowledge\').fetchone())"')


if __name__ == '__main__':
    main()
