#!/usr/bin/env python3
"""探测小鹅通圈子 API 能翻多深的历史帖子 (只读, 不写库)。

目的: 判断能否回填 research.db 历史数据 (capital 重建的前提)。
方法: 用 cursor 持续翻页, 记录每页的帖子时间, 直到翻不动或超 max_pages。

用法:
  uv run python3 scripts/probe_xiaoe_history.py
  uv run python3 scripts/probe_xiaoe_history.py --max-pages 200
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(override=True)

from tradingagents.research.collector import ResearchCollector


def main():
    parser = argparse.ArgumentParser(description="探测圈子 API 历史深度")
    parser.add_argument("--max-pages", type=int, default=300, help="最大翻页数")
    args = parser.parse_args()

    cookie = os.getenv("XIAOE_COOKIE", "").strip()
    if not cookie:
        print("✗ 未设置 XIAOE_COOKIE, 请在 .env 中配置")
        sys.exit(1)

    collector = ResearchCollector()
    cursor = ""
    all_dates = []
    page = 0
    cookie_expired = False
    earliest = None
    latest = None

    print(f"{'='*70}")
    print(f"  探测小鹅通圈子 API 历史深度 (最多翻 {args.max_pages} 页, 每页10帖)")
    print(f"{'='*70}")
    print(f"{'page':>5} {'本页最早':>20} {'本页最晚':>20} {'累计帖数':>8}")
    print("-" * 65)

    for page in range(1, args.max_pages + 1):
        try:
            data = collector._fetch_page(cookie, cursor=cursor, page_size=10)
        except Exception as e:
            print(f"  [page {page}] 请求失败: {e}")
            time.sleep(1)
            continue

        code = data.get("code", -1)
        if code != 0:
            msg = data.get("msg", "")
            print(f"  [page {page}] API错误 code={code} msg={msg}")
            if code == 23:
                print("  ✗ Cookie 已过期, 请重新获取 XIAOE_COOKIE")
                cookie_expired = True
                break
            time.sleep(1)
            continue

        feeds = data.get("data", {}).get("list", [])
        next_cursor = data.get("data", {}).get("cursor", "")

        if not feeds:
            print(f"  [page {page}] 无帖子, 翻到底了")
            break

        page_dates = []
        for feed in feeds:
            ca = feed.get("created_at", "")
            if ca:
                page_dates.append(ca[:10])
                all_dates.append(ca[:10])

        page_earliest = min(page_dates) if page_dates else "?"
        page_latest = max(page_dates) if page_dates else "?"
        if page_dates:
            if earliest is None or page_earliest < earliest:
                earliest = page_earliest
            if latest is None or page_latest > latest:
                latest = page_latest

        # 每10页或最后页打印
        if page % 10 == 0 or page == args.max_pages or not next_cursor:
            print(f"{page:>5} {page_earliest:>20} {page_latest:>20} {len(all_dates):>8}")

        if not next_cursor:
            print(f"  [page {page}] 无 cursor, 翻到底了")
            break

        cursor = next_cursor
        time.sleep(0.3)  # 限速

    # 汇总
    print(f"\n{'='*70}")
    print(f"  探测结果")
    print(f"{'='*70}")
    if cookie_expired:
        print(f"  ⚠ Cookie 已过期, 结果不完整。")
    if not all_dates:
        print(f"  未获取到任何帖子。")
        return

    from collections import Counter
    month_dist = Counter(d[:7] for d in all_dates)
    print(f"  总帖数: {len(all_dates)} (翻 {page} 页)")
    print(f"  时间跨度: {earliest} ~ {latest}")
    print(f"\n  按月分布 (去重前, 同帖可能重复):")
    for m in sorted(month_dist.keys()):
        bar = "█" * (month_dist[m] // 5)
        print(f"    {m}: {month_dist[m]:>4} {bar}")

    # 判断能否回填到 1 年前
    print(f"\n  ── 判断 ──")
    if earliest <= "2025-06":
        print(f"  ✅ 能回填到 1 年前 ({earliest}), capital 重建可行。")
    elif earliest <= "2026-01":
        print(f"  ⚠ 最早到 {earliest}, 覆盖 ~6 个月, 可做半年回测。")
    else:
        print(f"  ❌ 最早只到 {earliest}, 历史回填受限 (平台可能只保留近期帖子)。")
        print(f"     可尝试增大 --max-pages 继续翻。")


if __name__ == "__main__":
    main()
