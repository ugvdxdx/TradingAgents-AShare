#!/usr/bin/env python3
"""
增量拉取资金流数据到磁盘缓存。
策略：优先使用 Tushare（付费稳定），东方财富作为 fallback（免费但不稳定）。

增量逻辑：
  - 读取最新 .mf_cache/mf_*.pkl，找到每只股票的最新日期
  - 只从 Tushare 拉取缺失的交易日数据
  - 合并后写入当天新缓存文件
  - 最后自动更新板块资金流缓存

用法:
    uv run python3 fetch_money_flow_all.py              # 增量更新（默认）
    uv run python3 fetch_money_flow_all.py --full        # 全量拉取（向后兼容）
    uv run python3 fetch_money_flow_all.py --source tushare
"""
import json
import sys
import argparse
import os
import glob
import pickle
import time

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
from picker import paths

load_dotenv(os.path.join(paths.PROJECT_ROOT, '.env'))

from picker.data import money_flow


def _find_latest_mf_cache() -> tuple:
    """找到最新的资金流缓存文件，返回 (path, data_dict)。"""
    cache_dir = money_flow._DISK_CACHE_DIR
    if not os.path.exists(cache_dir):
        return None, {}
    files = sorted(glob.glob(os.path.join(cache_dir, "mf_*.pkl")), reverse=True)
    for fp in files:
        try:
            with open(fp, "rb") as f:
                data = pickle.load(f)
            if data and not any(v is None for v in list(data.values())[:5]):
                return fp, data
        except Exception:
            continue
    return None, {}


def _incr_update_tushare(codes: list, existing: dict) -> dict:
    """增量更新：从 Tushare 只拉缺失日期。"""
    from datetime import date, timedelta
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        print("TUSHARE_TOKEN 未配置")
        return existing

    pro = ts.pro_api(token)
    today = date.today().strftime("%Y%m%d")
    # 往前推 60 个自然日
    start_full = (date.today() - timedelta(days=90)).strftime("%Y%m%d")
    days = 60

    updated = 0
    uptodate = 0
    failed = 0

    for i, code in enumerate(codes):
        cache_key = f"{code}_{days}"

        # 找现有数据的最新日期
        old = existing.get(cache_key)
        latest_cached = None
        if old and isinstance(old, list) and len(old) > 0:
            latest_cached = str(old[-1].get("date", "")).replace("-", "")

        # 如果已有今天的数据，跳过
        if latest_cached and latest_cached >= today:
            uptodate += 1
            continue

        # 决定起始日期
        if latest_cached:
            start = str(int(latest_cached) + 1)
        else:
            start = start_full

        try:
            ts_code = money_flow._tushare_ts_code(code)
            df = pro.moneyflow(ts_code=ts_code, start_date=start, end_date=today)
            if df is None or df.empty:
                uptodate += 1
                continue

            new_data = []
            for _, row in df.iterrows():
                super_large_wan = (row.get("buy_elg_amount", 0) or 0) - (row.get("sell_elg_amount", 0) or 0)
                large_wan = (row.get("buy_lg_amount", 0) or 0) - (row.get("sell_lg_amount", 0) or 0)
                medium_wan = (row.get("buy_md_amount", 0) or 0) - (row.get("sell_md_amount", 0) or 0)
                small_wan = (row.get("buy_sm_amount", 0) or 0) - (row.get("sell_sm_amount", 0) or 0)
                main_force_wan = super_large_wan + large_wan
                buy_total_wan = (row.get("buy_elg_amount", 0) or 0) + (row.get("buy_lg_amount", 0) or 0) + (row.get("buy_md_amount", 0) or 0) + (row.get("buy_sm_amount", 0) or 0)
                sell_total_wan = (row.get("sell_elg_amount", 0) or 0) + (row.get("sell_lg_amount", 0) or 0) + (row.get("sell_md_amount", 0) or 0) + (row.get("sell_sm_amount", 0) or 0)
                turnover_wan = buy_total_wan + sell_total_wan
                main_pct = (main_force_wan / turnover_wan * 100) if turnover_wan > 0 else 0.0

                new_data.append({
                    "date": str(row["trade_date"]),
                    "main_net": float(main_force_wan * 1e4),
                    "super_large": float(super_large_wan * 1e4),
                    "large": float(large_wan * 1e4),
                    "medium": float(medium_wan * 1e4),
                    "small": float(small_wan * 1e4),
                    "main_pct": float(main_pct),
                })

            new_data.sort(key=lambda x: x["date"])

            # 合并
            merged = (old or []) + new_data
            # 去重
            seen = set()
            deduped = []
            for r in reversed(merged):
                dd = r["date"].replace("-", "")
                if dd not in seen:
                    seen.add(dd)
                    deduped.append(r)
            deduped.reverse()

            # 只保留最近 60 条
            if len(deduped) > 60:
                deduped = deduped[-60:]

            existing[cache_key] = deduped
            updated += 1
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f"  失败 {code}: {e}")

        # Tushare 限速 500/min → 0.12s
        time.sleep(0.12)

        if (i + 1) % 500 == 0:
            pct = (i + 1) / len(codes) * 100
            print(f"  [{pct:5.1f}%] {i+1}/{len(codes)} 新增={updated} 最新={uptodate} 失败={failed}")

    print(f"增量完成: 新增={updated} 最新={uptodate} 失败={failed}")
    return existing


def _full_update(codes: list) -> dict:
    """全量拉取（原逻辑）。"""
    money_flow._ENABLED = None
    money_flow._ACTIVE_SOURCE = None
    money_flow._CACHE.clear()

    # 清除旧缓存中的 None
    disk_cache = money_flow._load_disk_cache()
    dirty = sum(1 for v in disk_cache.values() if v is None)
    if dirty:
        print(f"清除旧缓存中 {dirty} 个无效(None)记录...")
        disk_cache = {k: v for k, v in disk_cache.items() if v is not None}
        p = money_flow._disk_cache_path()
        with open(p, "wb") as f:
            pickle.dump(disk_cache, f)

    money_flow._MIN_INTERVAL = 0.3
    money_flow._ENABLED = True
    money_flow._ACTIVE_SOURCE = "tushare"
    print("Tushare 模式: 限速 200次/分钟 (间隔 0.3s)")

    success = 0
    fail = 0
    for i, code in enumerate(codes):
        if fail >= 15:
            print(f"\n连续失败 {fail} 次，停止。已成功 {success}")
            money_flow._save_disk_cache()
            return money_flow._CACHE

        try:
            r = money_flow.fetch_fund_flow(code, 60)
            if r is not None:
                success += 1
                fail = 0
            else:
                fail += 1
                if fail >= 3:
                    print(f"  连续 {fail} 次失败，暂停 60 秒...")
                    time.sleep(60)
                    fail = 0
        except Exception:
            fail += 1

        if (i + 1) % 100 == 0:
            pct = (i + 1) / len(codes) * 100
            print(f"  [{pct:5.1f}%] {i+1}/{len(codes)} 成功={success}")

        if (i + 1) % 500 == 0:
            money_flow._save_disk_cache()

    money_flow._save_disk_cache()
    print(f"\n全量完成！成功 {success}/{len(codes)}，缓存 {len(money_flow._CACHE)} 项")
    return money_flow._CACHE


def _refresh_board_flow():
    """刷新板块资金流缓存。"""
    print("\n🔄 刷新板块资金流...")
    try:
        from tradingagents.agents.picker import rotation as rot
        # 删除旧缓存强制重拉
        cache_path = paths.BOARD_FLOW_CACHE
        if os.path.exists(cache_path):
            os.remove(cache_path)
        txt, rows = rot.get_board_flow_ranking(top_n=15)
        print(f"  {txt} | 共 {len(rows)} 个板块")
        for r in rows[:5]:
            print(f"    {r['rank']}. {r['name']:20s} 主力净流入: {r['main_net_yi']:+.2f}亿")
    except Exception as e:
        print(f"  板块资金流刷新失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="增量/全量拉取资金流数据")
    parser.add_argument("--source", choices=["tushare", "eastmoney", "auto"], default="tushare",
                        help="数据源，默认 tushare")
    parser.add_argument("--full", action="store_true",
                        help="全量拉取（默认增量）")
    parser.add_argument("--no-board", action="store_true",
                        help="跳过板块资金流刷新")
    args = parser.parse_args()

    with open(paths.STOCK_WHITELIST) as f:
        wl = json.load(f)
    codes = [s["code"] for s in wl]
    print(f"白名单: {len(codes)} 只")

    if args.full:
        print("模式: 全量拉取")
        result = _full_update(codes)
    else:
        print("模式: 增量更新")
        # 加载最新缓存
        latest_path, existing = _find_latest_mf_cache()
        if existing:
            print(f"  基准缓存: {os.path.basename(latest_path)} ({len(existing)} 条)")
        else:
            print("  无现有缓存，转为全量拉取")

        result = _incr_update_tushare(codes, existing)

        # 保存到当天文件
        money_flow._CACHE = result
        money_flow._save_disk_cache()
        print(f"  已保存到 {money_flow._disk_cache_path()}")

    # 刷新板块资金流
    if not args.no_board:
        _refresh_board_flow()

    print("\n全部完成。")


if __name__ == "__main__":
    main()
