#!/usr/bin/env python3
"""
全量拉取资金流数据到磁盘缓存。
策略：优先使用 Tushare（付费稳定），东方财富作为 fallback（免费但不稳定）。

用法:
    uv run python3 fetch_money_flow_all.py
    uv run python3 fetch_money_flow_all.py --source tushare   # 强制 Tushare
    uv run python3 fetch_money_flow_all.py --source eastmoney # 强制东方财富
"""
import json
import sys
import argparse
import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import money_flow


def main():
    parser = argparse.ArgumentParser(description="全量拉取资金流数据")
    parser.add_argument("--source", choices=["tushare", "eastmoney", "auto"], default="auto",
                        help="数据源: tushare(付费稳定) / eastmoney(免费但不稳定) / auto(自动探测)")
    args = parser.parse_args()

    # 重置状态
    money_flow._ENABLED = None
    money_flow._ACTIVE_SOURCE = None
    money_flow._CACHE.clear()
    # 清除旧磁盘缓存中的 None 值（之前东方财富失败写入的脏数据）
    disk_cache = money_flow._load_disk_cache()
    dirty = sum(1 for v in disk_cache.values() if v is None)
    if dirty > 0:
        print(f"清除旧缓存中 {dirty} 个无效(None)记录...")
        disk_cache = {k: v for k, v in disk_cache.items() if v is not None}
        # 回写清理后的缓存
        import pickle
        p = money_flow._disk_cache_path()
        with open(p, "wb") as f:
            pickle.dump(disk_cache, f)
    money_flow._CACHE.clear()
    # 关掉限速，全速冲刺
    money_flow._MIN_INTERVAL = 0.0

    with open("stock_whitelist.json") as f:
        wl = json.load(f)
    codes = [s["code"] for s in wl]

    # ── 确定数据源 ──
    source = args.source
    if source == "auto":
        # 自动探测
        ok = money_flow.probe_availability()
        if not ok:
            print("所有数据源不可用（东方财富+Tushare），退出。")
            sys.exit(1)
        source = money_flow._ACTIVE_SOURCE
        print(f"自动探测: 使用 {source} 数据源")
    elif source == "tushare":
        # 强制 Tushare
        if not money_flow._TUSHARE_TOKEN:
            print("TUSHARE_TOKEN 未配置，请检查 .env 文件。")
            sys.exit(1)
        money_flow._ENABLED = True
        money_flow._ACTIVE_SOURCE = "tushare"
        print(f"强制使用 Tushare 数据源，白名单: {len(codes)} 只")
    elif source == "eastmoney":
        # 强制东方财富
        ok = money_flow.probe_availability()
        if not ok:
            print("东方财富 API 不可用，退出。请在家里的网络环境运行。")
            sys.exit(1)
        if money_flow._ACTIVE_SOURCE != "eastmoney":
            print("东方财富探测失败，但 Tushare 可用。建议改用 --source tushare。")
            sys.exit(1)
        print(f"强制使用东方财富数据源，白名单: {len(codes)} 只")

    print(f"数据源: {source}，白名单: {len(codes)} 只")

    # Tushare 频率限制: 200次/分钟，设最小间隔 0.3s（约200次/分钟）
    if source == "tushare":
        money_flow._MIN_INTERVAL = 0.3
        print("Tushare 模式: 限速 200次/分钟 (间隔 0.3s)")
    else:
        # 东方财富全速冲刺
        money_flow._MIN_INTERVAL = 0.0

    success = 0
    fail = 0
    for i, code in enumerate(codes):
        if fail >= 15:
            print(f"\n连续失败 {fail} 次，停止。已成功 {success}")
            money_flow._save_disk_cache()
            sys.exit(1)

        try:
            r = money_flow.fetch_fund_flow(code, 60)
            if r is not None:
                success += 1
                fail = 0
            else:
                fail += 1
                # Tushare 频率限制时，暂停60秒再继续
                if source == "tushare" and fail >= 3:
                    print(f"  连续 {fail} 次失败(可能是频率限制)，暂停 60 秒...")
                    import time
                    time.sleep(60)
                    fail = 0
        except Exception:
            fail += 1

        # 进度条
        if (i + 1) % 100 == 0:
            pct = (i + 1) / len(codes) * 100
            print(f"  [{pct:5.1f}%] {i+1}/{len(codes)} 成功={success}")

        # 增量保存
        if (i + 1) % 500 == 0:
            money_flow._save_disk_cache()

    money_flow._save_disk_cache()
    print(f"\n完成！成功 {success}/{len(codes)}，缓存 {len(money_flow._CACHE)} 项")


if __name__ == "__main__":
    main()