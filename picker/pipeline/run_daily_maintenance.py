#!/usr/bin/env python3
"""每日维护统一编排器 — 集中管理 fundamentals 更新体系的所有步骤。

替代原先分散在多个脚本中的操作（run_daily_update.py / scan_mispriced.py /
update_fundamentals_from_research.py / v3_full_score.py 等各自独立执行），
提供一个统一的入口按序编排，每步独立可重试。

执行步骤（按依赖顺序）：
  Step 1: 研报采集        (ResearchCollector → raw_feeds)
  Step 2: 知识提取        (LLM extract → research.db)
  Step 2.5: 板块缺口发现  (热但池未覆盖的主题 → web search找股 → 生成+V3评分入池)
  Step 3: 研报触发刷新    (refresh_fundamentals.py — Web+Tushare+研报 → 彻底重写)
  Step 4: capital 更新    (纯量化, 0 LLM)
  Step 5: 过热股检测      (高分滞涨搜索验证)
  Step 6: 冷股激活检查    (r5>15% → 移回 hot)
  Step 7: K 线增量更新    (update_klines_daily)
  Step 8: 世界知识更新    (update_world_knowledge)
  Step 9: 每日快照        (snapshot)

用法:
  uv run python3 run_daily_maintenance.py                  # 全部步骤
  uv run python3 run_daily_maintenance.py --step 1         # 只跑采集
  uv run python3 run_daily_maintenance.py --step 3         # 只刷新 fundamentals
  uv run python3 run_daily_maintenance.py --from 2026-06-18 --to 2026-06-21  # 指定日期范围
  uv run python3 run_daily_maintenance.py --skip-research  # 跳过研报采集/提取
  uv run python3 run_daily_maintenance.py --skip-discovery # 跳过板块缺口发现
  uv run python3 run_daily_maintenance.py --capital-mode G # 使用G模式
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths


def step1_collect(date_from: str, date_to: str):
    """Step 1: 增量采集圈子数据 → raw_feeds"""
    print('=' * 60)
    print(f'Step 1: 增量采集圈子数据 ({date_from} ~ {date_to})')
    print('=' * 60)

    cookie = os.getenv('XIAOE_COOKIE', '').strip()
    if not cookie:
        print('✗ 未设置 XIAOE_COOKIE，跳过研报采集')
        return False

    from tradingagents.research.collector import ResearchCollector
    collector = ResearchCollector(db_path=paths.RESEARCH_DB)
    result = collector.collect(
        cookie=cookie, max_pages=20, incremental=True,
        date_from=date_from, date_to=date_to,
    )
    print(f'采集完成: new={result["new"]}, updated={result["updated"]}, errors={result["errors"]}')
    if result.get('cookie_expired'):
        print('✗ Cookie 已过期!')
        collector.close()
        sys.exit(3)

    db = collector._get_db()
    unprocessed = db.execute('SELECT count(*) FROM raw_feeds WHERE is_processed = 0').fetchone()[0]
    failed_retry = db.execute('SELECT count(*) FROM raw_feeds WHERE is_processed = 2').fetchone()[0]
    print(f'未处理: {unprocessed}, 失败待重试: {failed_retry}')
    collector.close()
    return result.get('new', 0) > 0 or result.get('updated', 0) > 0


def step2_extract():
    """Step 2: 清洗 + LLM 知识提取 → research.db"""
    print('\n' + '=' * 60)
    print('Step 2: 清洗 + LLM 知识提取')
    print('=' * 60)

    from tradingagents.research.collector import ResearchCollector
    from tradingagents.research.cleaner import ResearchCleaner
    from tradingagents.research.extractor import KnowledgeExtractor
    from tradingagents.research.store import KnowledgeStore

    collector = ResearchCollector(db_path=paths.RESEARCH_DB)
    cleaner = ResearchCleaner()
    extractor = KnowledgeExtractor()
    store = KnowledgeStore(db_path=paths.RESEARCH_DB)

    db = collector._get_db()
    rows = db.execute("""
        SELECT feed_id, text, title, created_at, author_name
        FROM raw_feeds
        WHERE is_processed = 0 AND text IS NOT NULL AND length(text) > 10
        ORDER BY created_at ASC
    """).fetchall()

    total = len(rows)
    print(f'待处理: {total} 条')

    if total == 0:
        print('无新帖子，跳过')
        collector.close()
        store.close()
        return False

    success = fail = 0
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
            if (i + 1) % 20 == 0 or i == total - 1:
                print(f'  [{i+1}/{total}] {raw["created_at"][:10]} | {knowledge.summary[:50]}...')
        except Exception as e:
            fail += 1
            print(f'  [{i+1}/{total}] 失败: {raw["feed_id"]} - {e}')
            db.execute('UPDATE raw_feeds SET is_processed = 2 WHERE feed_id = ?', (raw['feed_id'],))
            db.commit()

    print(f'提取完成: success={success}, fail={fail}')

    # 每日知识快照
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        store.create_snapshot(today, snap_type='daily')
        print(f'已创建每日知识快照: {today}')
    except Exception as e:
        print(f'⚠ 快照创建失败: {e}')

    collector.close()
    store.close()
    return success > 0


def step2b_discover_gap(v3_threshold: float = 8.0):
    """Step 2.5: 板块缺口发现 — 研报热但池未覆盖的主题, web search 找股入池。

    依赖 Step 2 产出的新鲜 sector_knowledge 主题; 产出的新股会被后续 Step 3/4
    (refresh_fundamentals / capital) 自然覆盖。默认阈值 V3>=8.0 入池。
    """
    print('\n' + '=' * 60)
    print(f'Step 2.5: 板块缺口发现 (V3>={v3_threshold} 入池)')
    print('=' * 60)

    from picker.discovery.discover_sector_gap import discover
    admitted = discover(v3_threshold=v3_threshold, days=14,
                        coverage_threshold=2, max_themes=8, max_per_theme=5)
    print(f'缺口发现完成: 入池 {len(admitted)} 只')
    return len(admitted) > 0


def step3_refresh_fundamentals(date_from: str, dry_run: bool = False):
    """Step 3: 研报触发 fundamentals 彻底重写（替代旧增量追加）"""
    print('\n' + '=' * 60)
    print(f'Step 3: 研报触发 fundamentals 彻底重写 (since {date_from})')
    print('=' * 60)

    from picker.pipeline.refresh_fundamentals import refresh_from_research

    days = (datetime.now() - datetime.strptime(date_from, '%Y-%m-%d')).days
    result = refresh_from_research(days=days, dry_run=dry_run)
    print(f'刷新完成: 更新 {result["updated"]}, 失败 {result["failed"]}')
    return result['updated'] > 0


def step4_capital_update(capital_mode: str = "G"):
    """Step 4: capital 动态更新 (纯量化, 0 LLM)"""
    print('\n' + '=' * 60)
    print(f'Step 4: capital 动态更新 (模式{capital_mode})')
    print('=' * 60)

    from picker.scoring.v3_full_score import update_capital
    cache = update_capital(mode=capital_mode, persist=True)
    return cache is not None


def step5_overheated_detect():
    """Step 5: 过热股检测"""
    print('\n' + '=' * 60)
    print('Step 5: 过热股检测')
    print('=' * 60)

    from picker.scoring.v3_full_score import V3_CACHE, detect_overheated
    if not os.path.exists(V3_CACHE):
        print('V3_CACHE 不存在，跳过')
        return False

    import json
    cache = json.load(open(V3_CACHE))
    detect_overheated(cache)
    return True


def step6_cold_reactivate():
    """Step 6: 冷股激活检查"""
    print('\n' + '=' * 60)
    print('Step 6: 冷股激活检查')
    print('=' * 60)

    from picker.discovery.scan_mispriced import _reactivate_cold_stocks
    _reactivate_cold_stocks()
    return True


def step7_update_klines():
    """Step 7: K 线增量更新"""
    print('\n' + '=' * 60)
    print('Step 7: K 线增量更新')
    print('=' * 60)

    try:
        from picker.pipeline.update_klines_daily import main as update_klines_main
        update_klines_main()
        return True
    except SystemExit as e:
        print(f'K 线更新完成 (exit={e.code})')
        return e.code == 0
    except Exception as e:
        print(f'⚠ K 线更新失败: {e}')
        return False


def step8_world_knowledge():
    """Step 8: 世界知识更新"""
    print('\n' + '=' * 60)
    print('Step 8: 世界知识更新')
    print('=' * 60)

    try:
        from picker.pipeline.update_world_knowledge import main as update_wk
        update_wk()
        return True
    except Exception as e:
        print(f'⚠ 世界知识更新失败: {e}')
        return False


def step9_snapshot():
    """Step 9: 创建每日评分快照"""
    print('\n' + '=' * 60)
    print('Step 9: 每日评分快照')
    print('=' * 60)

    try:
        from picker.scoring.v3_full_score import main as v3_main
        v3_main()
        return True
    except Exception as e:
        print(f'⚠ 快照失败: {e}')
        return False


def main():
    parser = argparse.ArgumentParser(description='每日维护统一编排器')
    parser.add_argument('--step', type=int, default=0, help='只执行指定步骤 (1-9, 0=全部)')
    parser.add_argument('--from', dest='date_from', default='', help='采集起始日 (YYYY-MM-DD，默认近3天)')
    parser.add_argument('--to', dest='date_to', default='', help='采集结束日 (YYYY-MM-DD，默认今天)')
    parser.add_argument('--capital-mode', dest='cap_mode', default='G', help='capital 模式 (G/D/A)')
    parser.add_argument('--skip-research', action='store_true', help='跳过研报采集/提取 (step1-3)')
    parser.add_argument('--skip-discovery', action='store_true', help='跳过板块缺口发现 (step2.5)')
    parser.add_argument('--discover-threshold', dest='discover_threshold', type=float, default=8.0,
                        help='缺口发现入池 V3 阈值 (默认 8.0)')
    parser.add_argument('--dry-run', action='store_true', help='只输出不写文件')
    args = parser.parse_args()

    today = datetime.now().strftime('%Y-%m-%d')
    default_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    date_from = args.date_from or default_from
    date_to = args.date_to or today

    step = args.step
    results = {}

    t0 = time.time()

    def _run(s, fn, *a, **kw):
        if step in (0, s):
            try:
                results[s] = fn(*a, **kw)
            except Exception as e:
                print(f'\n✗ Step {s} 异常: {type(e).__name__}: {e}')
                import traceback
                traceback.print_exc()
                results[s] = False

    # ── 研报链路 (Step 1-3) ──
    if not args.skip_research:
        _run(1, step1_collect, date_from, date_to)
        _run(2, step2_extract)
        # Step 2.5: 板块缺口发现 (依赖 step2 的新鲜研报主题; 只在全跑或指定时执行)
        if not args.skip_discovery and step in (0,):
            try:
                results['2.5'] = step2b_discover_gap(args.discover_threshold)
            except Exception as e:
                print(f'\n✗ Step 2.5 异常: {type(e).__name__}: {e}')
                import traceback
                traceback.print_exc()
                results['2.5'] = False
        _run(3, step3_refresh_fundamentals, date_from, args.dry_run)

    # ── 量化链路 (Step 4-6) ──
    _run(4, step4_capital_update, args.cap_mode)
    _run(5, step5_overheated_detect)
    _run(6, step6_cold_reactivate)

    # ── 数据链路 (Step 7-9) ──
    _run(7, step7_update_klines)
    _run(8, step8_world_knowledge)
    # _run(9, step9_snapshot)  # V3 全量评分需较长时间，默认每天选股时才跑

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"每日维护完成 ({elapsed/60:.1f}min)")
    success_count = sum(1 for v in results.values() if v)
    print(f"成功: {success_count}/{len(results)} 步骤")
    for s, ok in sorted(results.items(), key=lambda x: str(x[0])):
        status = '✓' if ok else '✗'
        print(f"  Step {s}: {status}")


if __name__ == '__main__':
    main()
