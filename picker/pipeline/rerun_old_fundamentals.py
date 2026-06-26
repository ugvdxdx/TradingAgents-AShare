#!/usr/bin/env python3
"""一次性脚本: 全量重写 fetch_date <= 给定日期 的 fundamentals。

refresh_fundamentals.py 的 --skip-recent-hours 按"最近N小时"窗口过滤,
无法精确表达"按绝对日期重跑 6/25 那批"。本脚本按 fetch_date 绝对日期筛选,
复用 _refresh_parallel 并行刷新。

用法:
  uv run python3 picker/pipeline/rerun_old_fundamentals.py            # 默认重跑 fetch_date<=2026-06-25
  uv run python3 picker/pipeline/rerun_old_fundamentals.py --before 2026-06-25
  uv run python3 picker/pipeline/rerun_old_fundamentals.py --workers 5
"""
import os, sys, json, glob, argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths
from picker.pipeline.refresh_fundamentals import _refresh_parallel, _load_world_knowledge


def collect_todo(before_date: str):
    """收集 fetch_date <= before_date 的 (code, name) 列表。"""
    fund_dir = paths.FUNDAMENTALS_DIR
    todo = []
    skipped = 0
    for f in sorted(os.listdir(fund_dir)):
        if not f.endswith(".json"):
            continue
        code = f[:-5]
        try:
            data = json.load(open(os.path.join(fund_dir, f), encoding="utf-8"))
        except Exception:
            continue
        fd = (data.get("fetch_date", "") or "")[:10]  # YYYY-MM-DD
        if fd and fd <= before_date:
            todo.append((code, data.get("name", code)))
        else:
            skipped += 1
    return todo, skipped


def main():
    ap = argparse.ArgumentParser(description="按 fetch_date 绝对日期重跑老版 fundamentals")
    ap.add_argument("--before", default="2026-06-25",
                    help="重跑 fetch_date <= 此日期 (YYYY-MM-DD, 默认 2026-06-25)")
    ap.add_argument("--workers", "-w", type=int, default=5, help="并发线程数 (默认5)")
    ap.add_argument("--no-web", action="store_true", help="跳过网络搜索")
    ap.add_argument("--no-v3", action="store_true", help="不触发 V3 重评")
    args = ap.parse_args()

    todo, skipped = collect_todo(args.before)
    print(f"重跑 fetch_date <= {args.before}: {len(todo)} 只 (跳过 {skipped} 只较新)", flush=True)
    if not todo:
        print("无待重跑, 退出")
        return

    world_knowledge = _load_world_knowledge()
    result = _refresh_parallel(
        todo, world_knowledge,
        do_web_search=not args.no_web,
        do_v3_rescore=not args.no_v3,
        workers=args.workers,
    )
    print(f"\n完成: 成功 {result['updated']}, 失败 {result['failed']}, 共 {len(todo)}")


if __name__ == "__main__":
    main()
