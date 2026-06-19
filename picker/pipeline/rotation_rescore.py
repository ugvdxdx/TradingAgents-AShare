#!/usr/bin/env python3
"""轮动触发式 V3 重评 (快慢结合架构的"触发器")。

解决: V3 半月才更新一次, 热门行业切换时, 新主线的个股因 sector_score 还是旧低分,
进不了 Top50 候选池, 辩论阶段根本看不到。

本脚本每日可跑:
  1. 拉取板块资金流排名, 找出"资金净流入但未被当前 Top50 覆盖"的热门板块 (主线切换预警)。
  2. 拉取这些热门板块的成分股龙头。
  3. 对其中"已有 fundamentals JSON 但 V3 分偏低 / 或还没 fundamentals"的标的, 输出待重评名单。
  4. --apply: 自动触发 V3 重评 (有 fundamentals 的直接重打分; 没有的提示先生成 fundamentals)。

用法:
  uv run python3 _rotation_rescore.py                 # 只检测+输出名单 (不改缓存)
  uv run python3 _rotation_rescore.py --apply         # 检测并触发 V3 重评
  uv run python3 _rotation_rescore.py --top-board 8   # 取资金流前8板块
"""
import argparse
import json
import os
import sys

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(override=True)

from picker import paths
from tradingagents.agents.picker import data_io, rotation as rot

V3_CACHE = paths.V3_CACHE
FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR


def _load_v3() -> dict:
    if os.path.exists(V3_CACHE):
        with open(V3_CACHE) as f:
            return json.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser(description="轮动触发式 V3 重评")
    ap.add_argument("--top-board", type=int, default=8, help="取资金流前N板块")
    ap.add_argument("--top-n", type=int, default=50, help="当前候选池规模 (判断覆盖)")
    ap.add_argument("--cons", type=int, default=8, help="每个热门板块取前N成分股")
    ap.add_argument("--apply", action="store_true", help="触发 V3 重评 (默认只输出名单)")
    args = ap.parse_args()

    # 1. 当前候选池 (Top50)
    pool = data_io.load_top_n(args.top_n)
    print(f"当前候选池 Top{args.top_n}: V3 {pool[0]['v3']:.1f} ~ {pool[-1]['v3']:.1f}")

    # 2. 板块资金流 + 轮动检测
    _, board_rows = rot.get_board_flow_ranking(top_n=20)
    if not board_rows:
        print("⚠️ 板块资金流获取失败, 退出。")
        return
    rotation = rot.detect_rotation(pool, board_rows, top_k=args.top_board)
    uncovered = rotation.get("uncovered", [])
    if not uncovered:
        print("✅ 资金净流入的热门板块均已被候选池覆盖, 无需重评。")
        return

    print(f"\n⚠️ 检测到 {len(uncovered)} 个净流入但未覆盖的板块 (主线切换预警):")
    for r in uncovered:
        print(f"  {r['name']} 主力净额+{r['main_net_yi']:.1f}亿 涨{r['change_pct']:+.1f}%")

    # 3. 拉取热门板块成分股龙头
    v3 = _load_v3()
    existing_fund = {f[:-5] for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith(".json")}
    rescore_codes = []   # 有 fundamentals, 可直接重评
    missing_codes = []   # 无 fundamentals, 需先生成
    for board in uncovered:
        cons = rot.get_industry_constituents(board["name"], top_n=args.cons)
        for c in cons:
            code = c["code"]
            if code in existing_fund:
                # 仅对当前 V3 分偏低 (不在候选池) 的才重评
                if code not in {s["code"] for s in pool}:
                    rescore_codes.append((code, c["name"], board["name"]))
            else:
                missing_codes.append((code, c["name"], board["name"]))

    # 去重
    rescore_codes = list(dict.fromkeys(rescore_codes))
    missing_codes = list(dict.fromkeys(missing_codes))

    print(f"\n📋 待重评 (已有fundamentals, V3分偏低): {len(rescore_codes)} 只")
    for code, name, board in rescore_codes:
        old = v3.get(code, {}).get("sector_score", "?")
        print(f"  {code} {name} [{board}] 当前V3={old}")
    print(f"\n📋 缺 fundamentals (需先生成): {len(missing_codes)} 只")
    for code, name, board in missing_codes:
        print(f"  {code} {name} [{board}]")

    if not args.apply:
        print("\n(只检测模式。加 --apply 触发 V3 重评)")
        if missing_codes:
            codes = ",".join(c for c, _, _ in missing_codes)
            print(f"\n生成缺失 fundamentals:\n  uv run python3 _gen_top500_fundamentals.py --codes {codes}")
        return

    # 4. 触发 V3 重评 (复用 _v3_full_score 的打分逻辑)
    if rescore_codes:
        print(f"\n🔄 触发 V3 重评 {len(rescore_codes)} 只...")
        import picker.scoring.v3_full_score as v3s
        cache = _load_v3()
        for code, name, board in rescore_codes:
            _, r, dt = v3s._call(code)
            if r and "sector_score" in r:
                cache[code] = r
                print(f"  ✓ {code} {name} V3={r['sector_score']:.1f} ({dt:.0f}s)")
            else:
                print(f"  ✗ {code} {name} 重评失败")
        json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)
        print(f"  已写回 {V3_CACHE}")

    if missing_codes:
        codes = ",".join(c for c, _, _ in missing_codes)
        print(f"\n⚠️ 以下标的缺 fundamentals, 请先生成再重评:\n"
              f"  uv run python3 _gen_top500_fundamentals.py --codes {codes}")


if __name__ == "__main__":
    main()
