#!/usr/bin/env python3
"""每日维护统一编排器 — 集中管理 fundamentals 更新体系的所有步骤。

替代原先分散在多个脚本中的操作（run_daily_update.py / scan_mispriced.py /
update_fundamentals_from_research.py / v3_full_score.py 等各自独立执行），
提供一个统一的入口按序编排，每步独立可重试。

执行步骤（按依赖顺序）：
  Step 1: 研报采集        (ResearchCollector → raw_feeds)
  Step 2: 知识提取        (LLM extract → research.db)
  Step 2.5: 板块缺口发现  (热但池未覆盖的主题 → web search找股 → 生成+V3评分入池) [池子边界: 加热]
  Step 2.6: chain tier更新 (用最新研报调赛道→热度档映射, 6档可重叠骨架不变; manual/auto)
  Step 3: 研报触发刷新    (refresh_fundamentals.py — Web+Tushare+研报 → 彻底重写)
  Step 4: capital 更新    (纯量化, 0 LLM)
  Step 5: 过热股检测      (高分滞涨搜索验证)
  Step 6: 冷股激活检查    (r5>15% → 移回 hot) [池子边界: 冷→热]
  Step 6.5: 冷门清理      (V3<7+chain<4+cap<3+r20<5+无研报 → 移入冷池) [池子边界: 热→冷]
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
import subprocess
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


def step2c_update_chain_tiers(mode: str = "manual"):
    """Step 2.6: chain tier_map 更新 — 融合研报+异动+缺口三信号调整热度档位。

    依赖 Step 2 的 research.db + Step 2.7 异动缓存(price-confirmed) + Step 2.5 缺口主题。
    保持 6 档可重叠热度骨架不变, LLM 按【赛道当前热度】(含异动验证)调整 sectors 归属。
    在异动分析+缺口发现之后执行 — 三信号融合让热度档位更准确。
    manual 模式只输出 diff; auto 模式 diff有变化即写入(归档可回滚)。
    """
    print('\n' + '=' * 60)
    print(f'Step 2.6: chain tier_map 更新 (mode={mode})')
    print('=' * 60)

    from picker.scoring.chain_tiers import update_chain_tiers
    _candidate, _diff, applied = update_chain_tiers(mode=mode)
    return applied


def step2d_movement_analysis():
    """Step 2.7: 异动分析 — 扫描全池异动股, 预填movement driver缓存。

    异动条件: 涨跌不对称(r20>=25%大涨 or r20<=-18%大跌) + |r5|>=5%趋势确认。
    对异动股web search涨跌原因, 缓存供评分(_call)直接读(快, 避免inline搜索)。
    避免重复: 缓存有效(7d)+方向一致 → 跳过。
    退场: 清理过期/非池/不再异动条目。
    治本: 纠正fundamentals滞后(传统主业标签盖住新热门暴露, 如中天科技海缆→实际AI光纤)。
    """
    print('\n' + '=' * 60)
    print('Step 2.7: 异动分析 (预填movement driver缓存)')
    print('=' * 60)

    from picker.scoring.v3_full_score import precompute_movement_drivers
    result = precompute_movement_drivers()
    return result.get("searched", 0) > 0 or result.get("skipped", 0) > 0


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


def step3_refresh_fundamentals(date_from: str, dry_run: bool = False, workers: int = 5):
    """Step 3: 研报触发 fundamentals 彻底重写（替代旧增量追加）

    Args:
        workers: 并发线程数 (LLM 为 IO 密集, 线程池并行刷新; 默认 5, 串行用 1)。
    """
    print('\n' + '=' * 60)
    print(f'Step 3: 研报触发 fundamentals 彻底重写 (since {date_from}, workers={workers})')
    print('=' * 60)

    from picker.pipeline.refresh_fundamentals import refresh_from_research

    days = (datetime.now() - datetime.strptime(date_from, '%Y-%m-%d')).days
    result = refresh_from_research(days=days, dry_run=dry_run, workers=workers)
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
    """Step 6: 冷股激活检查 (冷→热: 量价异动激活)"""
    print('\n' + '=' * 60)
    print('Step 6: 冷股激活检查 (冷→热)')
    print('=' * 60)

    from picker.discovery.scan_mispriced import _reactivate_cold_stocks
    _reactivate_cold_stocks()
    return True


def step6b_cleanup_cold(min_score: float = 7.0):
    """Step 6.5: 冷门清理 (热→冷: 无催化的垫底股移入冷池)

    与 Step 6 (冷→热激活) 对称。三条池子边界管理:
      Step 2.5 缺口补充(加热) / Step 6 冷股激活(冷→热) / Step 6.5 冷门清理(热→冷)
    判定: V3<7 + chain<4 + capital<3 + r20<5 + 无研报提及 (全满足才移)。
    """
    print('\n' + '=' * 60)
    print('Step 6.5: 冷门清理 (热→冷)')
    print('=' * 60)

    from picker.discovery.scan_mispriced import cleanup_to_cold_stocks
    cleaned = cleanup_to_cold_stocks(min_score=min_score)
    return len(cleaned) > 0


def step7_update_klines():
    """Step 7: K 线增量更新 (兼容旧调用; 并行模式下由 _collect_data 驱动)"""
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


# ══════════════════════════════════════════════════════════
# 并行数据采集 (研报 + K线 + 资金流)
# 三者都是独立网络 I/O, 子进程并行最快。更新前做新鲜度预检, 已最新则跳过。
# ══════════════════════════════════════════════════════════

def _probe_latest_trade_date():
    """探测最新交易日 (建 mootdx 连接, 拉参考股000001)。失败返回 ''。"""
    try:
        from tickflow import TickFlow
        from picker.pipeline.update_klines_daily import detect_latest_trade_date
        tf = TickFlow.free()
        return detect_latest_trade_date(tf, "000001", count=10) or ''
    except Exception as e:
        print(f'  [新鲜度] 探测失败: {type(e).__name__}: {str(e)[:80]}', flush=True)
        return ''


def _klines_cache_latest():
    """K线缓存里参考股的最新交易日。"""
    import pickle
    for suf in ['_SZ.pkl', '_SH.pkl']:
        p = os.path.join(paths.KLINE_CACHE_DIR, f'000001{suf}')
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, 'rb'))
                return sorted(df['trade_date'].unique())[-1]
            except Exception:
                pass
    return ''


def check_data_freshness():
    """检测 K线/资金流是否已是最新交易日。返回 dict (True=已最新可跳过)。

    探测最新交易日 (1次网络), 与缓存最新日比较。
    """
    ref = _probe_latest_trade_date()
    if not ref:
        return {'latest_date': '?', 'klines_fresh': False, 'moneyflow_fresh': False}
    kl_latest = _klines_cache_latest()
    klines_fresh = bool(kl_latest) and kl_latest >= ref
    # 资金流新鲜度: 与K线同一交易日, K线最新则资金流大概率也最新
    return {'latest_date': ref, 'klines_cache': kl_latest, 'klines_fresh': klines_fresh,
            'moneyflow_fresh': klines_fresh}  # 近似: 同周期更新


def _launch_data_subprocs(do_klines, do_moneyflow):
    """启动 K线/资金流 子进程 (并行), 返回 {name: Popen}。"""
    import subprocess
    procs = {}
    if do_klines:
        procs['K线'] = subprocess.Popen(
            [sys.executable, 'picker/pipeline/update_klines_daily.py'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=paths.PROJECT_ROOT,
        )
    if do_moneyflow:
        procs['资金流'] = subprocess.Popen(
            [sys.executable, 'picker/pipeline/fetch_money_flow_all.py'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=paths.PROJECT_ROOT,
        )
    return procs


def _collect_data_subprocs(procs, timeout=3600):
    """等待所有子进程完成, 顺序打印输出, 返回 {name: ok}。"""
    results = {}
    for name, p in procs.items():
        try:
            out, _ = p.communicate(timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            rc = -1
            out = f'(超时{timeout}s被杀)\n' + (out or '')
        results[name] = (rc == 0)
        tail = (out or '')[-1500:]
        print(f'\n{"─"*60}\n  [{name}] 子进程输出 (exit={rc}):\n{tail}')
    return results


def run_data_collection(do_klines=True, do_moneyflow=True, fresh_check=True):
    """并行采集 K线+资金流 (带新鲜度预检)。供 --data-only 和并行采集阶段调用。"""
    print('\n' + '=' * 60)
    print(f'数据采集: K线 + 资金流 {"(并行)" if do_klines and do_moneyflow else ""}')
    print('=' * 60)

    # 新鲜度预检
    skip_klines, skip_mf = False, False
    if fresh_check:
        f = check_data_freshness()
        print(f'  最新交易日: {f["latest_date"]} | K线缓存: {f.get("klines_cache","?")}'
              f' | K线{"✓最新" if f["klines_fresh"] else "✗落后"}'
              f' | 资金流{"✓最新" if f["moneyflow_fresh"] else "✗落后"}')
        if do_klines and f['klines_fresh']:
            print('  → K线已是最新, 跳过')
            skip_klines = True
        if do_moneyflow and f['moneyflow_fresh']:
            print('  → 资金流已是最新, 跳过 (近似判定; 脚本内部仍会逐只确认)')
            # 资金流不硬跳过 (内部逐只skip更准), 仅提示; K线硬跳过
    if skip_klines and skip_mf:
        print('  全部已最新, 无需采集')
        return {'K线': True, '资金流': True}

    procs = _launch_data_subprocs(
        do_klines=do_klines and not skip_klines,
        do_moneyflow=do_moneyflow,
    )
    return _collect_data_subprocs(procs)


def step8_world_knowledge():
    """Step 8: 世界知识更新"""
    print('\n' + '=' * 60)
    print('Step 8: 世界知识更新')
    print('=' * 60)

    try:
        # update_world_knowledge.main() 内部用 argparse 读 sys.argv,
        # 会把本脚本的 --skip-* 等参数误当自己的 → 调用前清空 argv 隔离
        import sys as _sys
        _orig_argv = _sys.argv[:]
        _sys.argv = [_sys.argv[0]]
        from picker.pipeline.update_world_knowledge import main as update_wk
        update_wk()
        _sys.argv = _orig_argv
        return True
    except Exception as e:
        _sys.argv = _orig_argv
        print(f'⚠ 世界知识更新失败: {e}')
        return False


def step9_rescore():
    """Step 9: V3 评分缓存刷新 (needs_run 的 chain/delivery/essence 重评)。

    注意: 这里刷新的是评分缓存 fundamental_v3_scores.json (供选股直接用),
    不是 v3_snapshots/ 快照 —— 后者由选股流水线 debate_picker_v5 选股时自动写。
    """
    print('\n' + '=' * 60)
    print('Step 9: V3 评分缓存刷新 (needs_run 重评)')
    print('=' * 60)

    try:
        from picker.scoring.v3_full_score import main as v3_main
        v3_main()
        return True
    except Exception as e:
        print(f'⚠ 评分刷新失败: {e}')
        return False


def main():
    parser = argparse.ArgumentParser(description='每日维护统一编排器')
    parser.add_argument('--step', type=int, default=0, help='只执行指定步骤 (1-9, 0=全部)')
    parser.add_argument('--from', dest='date_from', default='', help='采集起始日 (YYYY-MM-DD，默认近3天)')
    parser.add_argument('--to', dest='date_to', default='', help='采集结束日 (YYYY-MM-DD，默认今天)')
    parser.add_argument('--capital-mode', dest='cap_mode', default='G', help='capital 模式 (G/D/A)')
    parser.add_argument('--skip-research', action='store_true', help='跳过整个研报链路 (step1-3 含缺口发现)')
    parser.add_argument('--skip-collect', action='store_true', help='只跳采集+提取 (step1-2), 保留缺口发现+刷新 (今天无新帖时用)')
    parser.add_argument('--skip-discovery', action='store_true', help='跳过板块缺口发现 (step2.5)')
    parser.add_argument('--skip-movement', action='store_true', help='跳过异动分析 (step2.7)')
    parser.add_argument('--skip-chain-tiers', action='store_true', help='跳过 chain tier_map 更新 (step2.6)')
    parser.add_argument('--chain-tiers-mode', dest='chain_tiers_mode', default='manual',
                        choices=['manual', 'auto'],
                        help='chain tier 更新模式: manual=只输出diff不写 / auto=diff有变化即写入(归档可回滚)')
    parser.add_argument('--discover-threshold', dest='discover_threshold', type=float, default=8.0,
                        help='缺口发现入池 V3 阈值 (默认 8.0)')
    parser.add_argument('--skip-cleanup', action='store_true', help='跳过冷门清理 (step6.5)')
    parser.add_argument('--cleanup-threshold', dest='cleanup_threshold', type=float, default=7.0,
                        help='冷门清理 V3 阈值 (低于此值+其他条件 → 移入冷池, 默认 7.0)')
    parser.add_argument('--skip-data', action='store_true', help='跳过 K线+资金流采集')
    parser.add_argument('--skip-klines', action='store_true', help='跳过 K线采集 (保留资金流)')
    parser.add_argument('--skip-moneyflow', action='store_true', help='跳过资金流采集 (保留K线)')
    parser.add_argument('--no-fresh-check', dest='fresh_check', action='store_false',
                        help='跳过新鲜度预检 (强制采集)')
    parser.add_argument('--data-only', action='store_true', help='只采集 K线+资金流 (带新鲜度预检), 不跑研报/评分')
    parser.add_argument('--dry-run', action='store_true', help='只输出不写文件')
    parser.add_argument('--workers', '-w', type=int, default=5,
                        help='Step 3 fundamentals 刷新并发线程数 (默认5; LLM为IO密集, 串行用1)')
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

    # ── --data-only: 只采集 K线+资金流 (带新鲜度预检), 不跑研报/评分 ──
    if args.data_only:
        print('═' * 60)
        print('  数据采集模式 (只更新 K线 + 资金流)')
        print('═' * 60)
        res = run_data_collection(
            do_klines=not args.skip_klines,
            do_moneyflow=not args.skip_moneyflow,
            fresh_check=args.fresh_check,
        )
        print(f"\n{'='*60}\n数据采集完成 ({(time.time()-t0)/60:.1f}min)")
        for k, ok in res.items():
            print(f"  {k}: {'✓' if ok else '✗'}")
        return

    # ── 并行采集阶段: 启动 K线+资金流 子进程 (与研报采集并行, 非阻塞) ──
    data_procs = {}
    do_klines = (not args.skip_data) and (not args.skip_klines)
    do_moneyflow = (not args.skip_data) and (not args.skip_moneyflow)
    if (do_klines or do_moneyflow) and step in (0,):
        print('\n' + '=' * 60)
        print('并行采集: 启动 K线+资金流 子进程 (与研报采集并行)')
        print('=' * 60)
        if args.fresh_check:
            f = check_data_freshness()
            print(f'  最新交易日: {f["latest_date"]} | K线缓存: {f.get("klines_cache","?")} '
                  f'| K线{"✓最新" if f["klines_fresh"] else "✗落后"}')
            if do_klines and f['klines_fresh']:
                print('  → K线已是最新, 跳过启动')
                do_klines = False
            # 资金流不硬跳过 (内部逐只skip更准)
        data_procs = _launch_data_subprocs(do_klines=do_klines, do_moneyflow=do_moneyflow)

    # ── 研报链路 (Step 1-3, 主进程; 与数据子进程并行) ──
    # skip_research: 跳整个链路 (1-3); skip_collect: 只跳采集+提取 (1-2), 保留缺口发现+刷新
    if not args.skip_research:
        if not args.skip_collect:
            _run(1, step1_collect, date_from, date_to)
            _run(2, step2_extract)
        # Step 2.7: 异动分析 (先跑 — 产出price-confirmed热度供tier更新用)
        if not args.skip_movement and step in (0,):
            try:
                results['2.7'] = step2d_movement_analysis()
            except Exception as e:
                print(f'\n✗ Step 2.7 异常: {type(e).__name__}: {e}')
                import traceback
                traceback.print_exc()
                results['2.7'] = False
        # Step 2.5: 板块缺口发现 (先跑 — 产出新兴缺口主题供tier更新参考)
        if not args.skip_discovery and step in (0,):
            try:
                results['2.5'] = step2b_discover_gap(args.discover_threshold)
            except Exception as e:
                print(f'\n✗ Step 2.5 异常: {type(e).__name__}: {e}')
                import traceback
                traceback.print_exc()
                results['2.5'] = False
        # Step 2.6: chain tier_map 更新 (最后跑 — 融合研报+异动+缺口三信号, 更新热度档位)
        if not args.skip_chain_tiers and step in (0,):
            try:
                results['2.6'] = step2c_update_chain_tiers(args.chain_tiers_mode)
            except Exception as e:
                print(f'\n✗ Step 2.6 异常: {type(e).__name__}: {e}')
                import traceback
                traceback.print_exc()
                results['2.6'] = False
                results['2.5'] = False
        _run(3, step3_refresh_fundamentals, date_from, args.dry_run, args.workers)

    # ── 收集数据采集子进程结果 (研报跑完后join) ──
    if data_procs:
        data_res = _collect_data_subprocs(data_procs)
        for k, ok in data_res.items():
            results[f'数据-{k}'] = ok

    # ── 量化链路 (Step 4-6) ──
    _run(4, step4_capital_update, args.cap_mode)
    _run(5, step5_overheated_detect)
    _run(6, step6_cold_reactivate)
    # Step 6.5: 冷门清理 (热→冷, 与 Step 6 对称; 只在全跑时执行)
    if step in (0,) and not args.skip_cleanup:
        try:
            results['6.5'] = step6b_cleanup_cold(args.cleanup_threshold)
        except Exception as e:
            print(f'\n✗ Step 6.5 异常: {type(e).__name__}: {e}')
            import traceback
            traceback.print_exc()
            results['6.5'] = False

    # ── 世界知识 (Step 8); K线已前移到并行采集阶段 ──
    _run(8, step8_world_knowledge)
    _run(9, step9_rescore)  # V3 评分缓存刷新 (needs_run 重评); 快照由选股时写

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
