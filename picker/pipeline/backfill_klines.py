#!/usr/bin/env python3
"""K线历史回溯 (补采到指定根数, 增量合并)。

现状: 每只股 K 线 ~60-90 根 (~3-4个月)。本脚本把每只股补到目标根数,
供 capital 历史重建 (price_factor 需要 r5/r20) 和长窗口回测使用。

增量逻辑 (不重跑已有的):
  - 现有 pkl 行数 >= count: 跳过 (已足够长)
  - 现有 pkl 行数 < count: 重新拉 count 根, 与旧数据按 trade_date 合并去重
    (保留最长的时间范围, 不丢失任何已有行)

用法:
  uv run python3 picker/pipeline/backfill_klines.py                    # 默认补到 300 根
  uv run python3 picker/pipeline/backfill_klines.py --count 300        # ~14个月
  uv run python3 picker/pipeline/backfill_klines.py --codes 600519,000001  # 指定个股
"""
import argparse
import os
import pickle
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from picker import paths

KLINE_DIR = paths.KLINE_CACHE_DIR


def _kline_path(code: str) -> str:
    suffix = "_SH.pkl" if code.startswith("6") else "_SZ.pkl"
    return os.path.join(KLINE_DIR, f"{code}{suffix}")


def _load_existing(code: str):
    """读现有 pkl, 不存在返回 None。"""
    p = _kline_path(code)
    if not os.path.exists(p):
        return None
    try:
        return pickle.load(open(p, "rb"))
    except Exception:
        return None


def _merge(old, new):
    """按 trade_date 合并去重 (new 覆盖同日期的 old, 保留两边独有的)。"""
    if old is None or len(old) == 0:
        return new
    if new is None or len(new) == 0:
        return old
    merged = pd.concat([old, new], ignore_index=True)
    merged = merged.drop_duplicates(subset=["trade_date"], keep="last")
    return merged.sort_values("trade_date").reset_index(drop=True)


def backfill_one(code: str, count: int, tf) -> str:
    """补采单只。返回状态: skip/done/fail。"""
    sym = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    old = _load_existing(code)
    old_len = len(old) if old is not None else 0

    # 增量: 已足够长则跳过
    if old_len >= count:
        return "skip"

    # 拉长历史
    try:
        dfs = tf.klines.batch([sym], period="1d", count=count, as_dataframe=True)
        new = dfs.get(sym)
        if new is None or len(new) == 0:
            return "fail"
    except Exception as e:
        return f"fail:{e}"

    merged = _merge(old, new)
    # 落盘
    p = _kline_path(code)
    merged.to_pickle(p)
    return f"done:{old_len}→{len(merged)}"


def get_all_codes():
    """只补采有 fundamentals JSON 的股 (无基本面的股不需要 K 线回溯)。

    同时要求该股在 V3 池里 (有评分, 才会进入回测)。
    """
    import json
    v3 = json.load(open(paths.V3_CACHE))
    v3_codes = {c for c, v in v3.items() if isinstance(v, dict) and "sector_score" in v}
    # 取 fundamentals 目录下有 JSON 的
    fund_codes = set()
    for d in (paths.FUNDAMENTALS_DIR, paths.FUNDAMENTALS_COLD_DIR):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".json"):
                    fund_codes.add(f[:-5])
    return sorted(v3_codes & fund_codes)


def main():
    parser = argparse.ArgumentParser(description="K线历史回溯 (增量补采)")
    parser.add_argument("--count", type=int, default=300,
                        help="目标根数 (默认300 ≈ 14个月)")
    parser.add_argument("--codes", default="", help="指定个股 (逗号分隔, 默认全部V3池)")
    args = parser.parse_args()

    print("═" * 60)
    print(f"  K线历史回溯 (目标 {args.count} 根 ≈ {args.count//21}个月)")
    print("═" * 60)

    from tickflow import TickFlow
    tf = TickFlow.free()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else get_all_codes()
    print(f"  待处理: {len(codes)} 只")

    done = skip = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        # 先批量探测哪些需要补 (避免逐只调 batch 的开销)
        result = backfill_one(code, args.count, tf)
        if result == "skip":
            skip += 1
        elif result.startswith("done"):
            done += 1
        else:
            fail += 1

        if i % 20 == 0 or i == len(codes):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(codes) - i) / rate if rate > 0 else 0
            print(f"  [{i}/{len(codes)}] 补采{done} 跳过{skip} 失败{fail} | "
                  f"{rate:.1f}只/s ETA {eta:.0f}s")

        # tickflow batch 内部已限速, 但逐只调要加间隔
        time.sleep(0.3)

    elapsed = time.time() - t0
    print(f"\n✓ 完成: 补采 {done} 只, 跳过 {skip} 只(已足够长), 失败 {fail} 只, 耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    main()
