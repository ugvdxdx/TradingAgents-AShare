#!/usr/bin/env python3
"""K线每日增量更新 (按最新交易日判定, 非行数)。

与 backfill_klines.py 的分工:
  - backfill_klines: 历史【回溯】, 按"行数 < count"判定, 把短 K线补长 (负责长度)。
  - 本脚本:          每日【增量】, 按"最新日期落后"判定, 刷新已有 K线到最新交易日 (负责新鲜度)。

根因: KlineCache.get() 过期策略已作废 (文件存在即返回), backfill 也只判行数,
      导致已有 pkl 永远不被刷新 → K线滞后, r5/r20/capital 失真。本脚本修复此问题。

核心逻辑:
  1. 拉参考股 (默认 000001) 确认最新交易日 (探测法, 不依赖 akshare calendar)。
  2. 对每只热股 (fundamentals/{code}.json 存在):
     - 读现有 pkl 的最新 trade_date
     - 若 < 参考最新日 → 拉增量根数 (缺口天数+余量), 与旧数据按 trade_date 合并去重
     - 若 >= 参考最新日 → skip (已是最新)
  3. 退出码: 失败率 > 10% 返回 1 (供编排脚本判定"K线必须成功")。

用法:
  uv run python3 picker/pipeline/update_klines_daily.py               # 默认: 热股增量
  uv run python3 picker/pipeline/update_klines_daily.py --codes 600519,000001
  uv run python3 picker/pipeline/update_klines_daily.py --count 120    # 增量根数
  uv run python3 picker/pipeline/update_klines_daily.py --reference 600519
"""
import argparse
import os
import pickle
import sys
import time
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths

KLINE_DIR = paths.KLINE_CACHE_DIR
# 失败率超过此阈值 → 退出码 1 (编排脚本据此判定 K线刷新失败, 终止选股)
FAIL_RATE_THRESHOLD = 0.10


def _kline_path(code: str) -> str:
    """与 backfill_klines / KlineCache 一致的落盘命名: 600519_SH.pkl / 000001_SZ.pkl。"""
    suffix = "_SH.pkl" if code.startswith("6") else "_SZ.pkl"
    return os.path.join(KLINE_DIR, f"{code}{suffix}")


def _sym(code: str) -> str:
    """tickflow 用的 symbol: 600519.SH / 000001.SZ。"""
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _load_existing(code: str):
    """读现有 pkl, 不存在/损坏返回 None。"""
    p = _kline_path(code)
    if not os.path.exists(p):
        return None
    try:
        df = pickle.load(open(p, "rb"))
        if df is None or len(df) == 0:
            return None
        return df
    except Exception:
        return None


def _latest_date(df) -> str:
    """取 K线 df 的最新 trade_date (str YYYY-MM-DD), 无则返回 ''。"""
    try:
        if df is not None and "trade_date" in df.columns and len(df) > 0:
            return str(df["trade_date"].max())
    except Exception:
        pass
    return ""


def _merge(old, new):
    """按 trade_date 合并去重 (new 覆盖同日期的 old, 保留两边独有的)。

    复用 backfill_klines._merge 的逻辑, 保证两脚本合并语义一致。
    """
    if old is None or len(old) == 0:
        return new
    if new is None or len(new) == 0:
        return old
    merged = pd.concat([old, new], ignore_index=True)
    merged = merged.drop_duplicates(subset=["trade_date"], keep="last")
    return merged.sort_values("trade_date").reset_index(drop=True)


def detect_latest_trade_date(tf, reference_code: str = "000001", count: int = 10) -> str:
    """探测最新交易日: 拉参考股最近 count 根, 取最新 trade_date。

    用探测法而非 calendar: tickflow batch 已返回实际数据日, 比依赖 akshare 节假日表更可靠
    (akshare 不可用时 trade_calendar 会 fallback 到 weekend 规则, 漏判节假日)。
    """
    sym = _sym(reference_code)
    try:
        dfs = tf.klines.batch([sym], period="1d", count=count, as_dataframe=True)
        df = dfs.get(sym)
        if df is None or len(df) == 0:
            return ""
        return _latest_date(df)
    except Exception as e:
        print(f"  ⚠ 探测参考股 {reference_code} 失败: {e}")
        return ""


def get_hot_codes():
    """热股池 = fundamentals/ 目录下所有股 (唯一真相源)。

    以 fundamentals/ 为准, 不再用 V3池 ∩ fundamentals: 刚进 fundamentals 但还没评
    V3 的新股每天也要检测并补齐 K线 (用户要求: fundamentals 文件夹中才是全部热股)。
    新股 (无 pkl) 会被 update_one 拉 count 根 (默认90 ≈ 4.5个月, 超过近两个月要求)。
    """
    fdir = paths.FUNDAMENTALS_DIR
    if not os.path.isdir(fdir):
        return []
    return sorted(f[:-5] for f in os.listdir(fdir) if f.endswith(".json"))


def update_one(code: str, ref_latest: str, count: int, tf) -> str:
    """增量刷新单只热股 K线。返回状态: skip/done/uptodate/fail[:msg]。

    Args:
        ref_latest: 参考股最新交易日 (YYYY-MM-DD)。空则强制拉增量。
        count: 增量拉取根数 (缺口+余量)。
    """
    old = _load_existing(code)
    old_latest = _latest_date(old)

    # 已是最新 (>= 参考股最新日) → 跳过
    if ref_latest and old_latest and old_latest >= ref_latest:
        return "uptodate"

    sym = _sym(code)
    try:
        dfs = tf.klines.batch([sym], period="1d", count=count, as_dataframe=True)
        new = dfs.get(sym)
        if new is None or len(new) == 0:
            return "fail:空数据"
    except Exception as e:
        return f"fail:{e}"

    merged = _merge(old, new)
    p = _kline_path(code)
    merged.to_pickle(p)
    old_len = len(old) if old is not None else 0
    return f"done:{old_latest or '无'}→{_latest_date(merged)} ({old_len}→{len(merged)})"


def main():
    parser = argparse.ArgumentParser(description="K线每日增量更新 (按最新交易日判定)")
    parser.add_argument("--count", type=int, default=90,
                        help="增量拉取根数 (默认90, 覆盖缺口+余量)")
    parser.add_argument("--codes", default="",
                        help="指定个股 (逗号分隔, 默认全部热股)")
    parser.add_argument("--reference", default="000001",
                        help="参考股代码 (用于探测最新交易日, 默认 000001 平安银行)")
    args = parser.parse_args()

    print("═" * 60)
    print(f"  K线每日增量更新 (按最新交易日判定)")
    print(f"  增量根数: {args.count} | 参考股: {args.reference}")
    print("═" * 60)

    from tickflow import TickFlow
    tf = TickFlow.free()

    # 1. 探测最新交易日
    ref_latest = detect_latest_trade_date(tf, args.reference, count=args.count)
    if not ref_latest:
        print(f"  ✗ 无法探测最新交易日 (参考股 {args.reference} 拉取失败)")
        print("  ✗ 终止: K线刷新失败 (退出码 1)")
        sys.exit(1)
    print(f"  参考股 {args.reference} 最新交易日: {ref_latest}")

    # 2. 确定待处理热股
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = get_hot_codes()
    print(f"  待处理热股: {len(codes)} 只")

    # 3. 逐只增量刷新
    done = uptodate = fail = 0
    fail_details = []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        result = update_one(code, ref_latest, args.count, tf)
        if result.startswith("done"):
            done += 1
        elif result == "uptodate":
            uptodate += 1
        else:
            fail += 1
            if len(fail_details) < 10:
                fail_details.append((code, result))

        if i % 20 == 0 or i == len(codes):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(codes) - i) / rate if rate > 0 else 0
            print(f"  [{i}/{len(codes)}] 更新{done} 已最新{uptodate} 失败{fail} | "
                  f"{rate:.1f}只/s ETA {eta:.0f}s", flush=True)

        # tickflow free tier 限速 60/min → 间隔须 >= 1.0s; 取 1.1s 留余量 (旧 0.3s=200/min 触发 ~8% 限流失败)
        # 只对真正发请求的 (done/fail) 限速; uptodate 未联网, 跳过 sleep (日常维护多数已最新)
        if result != "uptodate":
            time.sleep(1.1)

    elapsed = time.time() - t0
    fail_rate = fail / len(codes) if codes else 0

    print()
    print(f"✓ 完成: 更新 {done} 只, 已最新 {uptodate} 只, 失败 {fail} 只, 耗时 {elapsed:.0f}s")
    if fail_details:
        print("  失败明细 (前10):")
        for code, msg in fail_details:
            print(f"    {code}: {msg}")

    # 退出码: 失败率 > 阈值 → 1 (编排脚本据此终止选股)
    if fail_rate > FAIL_RATE_THRESHOLD:
        print(f"  ✗ 失败率 {fail_rate:.0%} > {FAIL_RATE_THRESHOLD:.0%}, 退出码 1")
        sys.exit(1)
    print(f"  ✓ 失败率 {fail_rate:.0%} <= {FAIL_RATE_THRESHOLD:.0%}, 退出码 0")


if __name__ == "__main__":
    main()
