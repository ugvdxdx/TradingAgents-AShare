#!/usr/bin/env python3
"""批量更新 top1500 股票的基本面数据

按市值从大到小，更新白名单中前1500只股票的 fundamentals。
支持断点续传：已更新的跳过，中断后重新运行即可继续。

用法:
  uv run python3 _batch_update_fundamentals.py           # 更新 top1500
  uv run python3 _batch_update_fundamentals.py --count 50  # 只更新前50只
  uv run python3 _batch_update_fundamentals.py --force     # 强制重新生成（不跳过已有）
"""
import json
import os
import sys
import time
import traceback

import fundamental_agent


def load_top_stocks(count=1500):
    """按市值排序，取前N只股票"""
    with open("stock_whitelist.json", "r", encoding="utf-8") as f:
        wl = json.load(f)
    # 按市值降序
    wl.sort(key=lambda x: x.get("mcap_yi", 0), reverse=True)
    return wl[:count]


def main():
    # 解析参数
    count = 1500
    force = False
    for arg in sys.argv[1:]:
        if arg.startswith("--count="):
            count = int(arg.split("=")[1])
        elif arg == "--count" and sys.argv.index(arg) + 1 < len(sys.argv):
            count = int(sys.argv[sys.argv.index(arg) + 1])
        elif arg == "--force":
            force = True

    stocks = load_top_stocks(count)
    total = len(stocks)

    # 统计已有
    existing = set()
    if not force:
        for f in os.listdir("fundamentals"):
            if f.endswith(".json"):
                existing.add(f.replace(".json", ""))

    need_update = []
    for s in stocks:
        if force or s["code"] not in existing:
            need_update.append(s)

    print(f"Top{count} 股票: {total} 只, 需更新: {len(need_update)} 只, 已有: {total - len(need_update)} 只")

    if not need_update:
        print("全部已是最新，无需更新")
        return

    # 进度文件
    progress_file = ".cache/batch_progress.json"
    os.makedirs(".cache", exist_ok=True)

    # 加载进度（断点续传）
    done_set = set()
    if os.path.exists(progress_file) and not force:
        try:
            with open(progress_file, "r") as f:
                done_list = json.load(f)
                done_set = set(done_list)
        except:
            pass

    success = 0
    fail = 0
    skip = 0
    start_time = time.time()

    for i, stock in enumerate(need_update):
        code = stock["code"]
        name = stock["name"]

        # 跳过已完成的
        if code in done_set:
            skip += 1
            continue

        try:
            result = fundamental_agent.analyze_one(code, name, force=True)
            success += 1

            # 记录进度
            done_set.add(code)
            if success % 10 == 0:
                with open(progress_file, "w") as f:
                    json.dump(list(done_set), f)

            # 进度输出
            elapsed = time.time() - start_time
            avg_time = elapsed / (success + fail) if (success + fail) > 0 else 0
            remaining = (len(need_update) - i - 1) * avg_time
            print(f"[{i+1}/{len(need_update)}] {code} {name} OK | "
                  f"成功:{success} 失败:{fail} | "
                  f"耗时:{elapsed:.0f}s 剩余:{remaining/60:.1f}min")

        except Exception as e:
            fail += 1
            print(f"[{i+1}/{len(need_update)}] {code} {name} FAIL: {str(e)[:80]}")
            # 短暂暂停，避免连续失败时刷屏
            time.sleep(2)

        # 限速：每次请求间隔0.5秒，避免触发新浪API限流
        time.sleep(0.5)

    # 保存最终进度
    with open(progress_file, "w") as f:
        json.dump(list(done_set), f)

    elapsed = time.time() - start_time
    print(f"\n=== 完成 ===")
    print(f"成功: {success}, 失败: {fail}, 跳过: {skip}")
    print(f"总耗时: {elapsed/60:.1f} 分钟")


if __name__ == "__main__":
    main()
