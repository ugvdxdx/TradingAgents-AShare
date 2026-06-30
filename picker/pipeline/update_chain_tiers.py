#!/usr/bin/env python3
"""chain 分档映射 (赛道→6档热度带) 动态更新器 — thin wrapper 调库 chain_tiers.update_chain_tiers。

实际逻辑在 picker.scoring.chain_tiers: 6档可重叠热度带 + 6类信号融合(研报板块动量+异动归因+
缺口发现+世界知识+板块量价动量+板块主力资金流) + 骨架校验。本脚本只是 CLI 入口,
避免维护两套(旧 8 档实现已废, 与生产 6 档分叉会污染 tier_map)。

用法:
  python3 picker/pipeline/update_chain_tiers.py --mode manual   # 生成候选+diff, 不写 (预览)
  python3 picker/pipeline/update_chain_tiers.py --mode auto     # diff有变化即写入(归档可回滚)
  # 每日维护入口(同款): run_daily_maintenance.py --chain-tiers-mode auto
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker.scoring import chain_tiers as ct


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="chain 分档映射动态更新器 — 调库 chain_tiers (6档可重叠热度带, 含量价+资金流信号)")
    ap.add_argument("--mode", choices=["manual", "auto"], default="manual",
                    help="manual=只出diff不写(预览) / auto=diff有变化即写入(归档可回滚)")
    ap.add_argument("--days", type=int, default=14, help="研报信号回看天数")
    args = ap.parse_args()

    print("═" * 60)
    print(f"chain tier_map 更新 (mode={args.mode}, days={args.days}) — 调库 chain_tiers (6档)")
    print("═" * 60)
    # 库函数内部: build_candidate(6类信号融合) + diff + 按 mode 应用(manual只打印/auto写入归档)
    cand, diff, applied = ct.update_chain_tiers(mode=args.mode, days=args.days)
    if applied:
        print(f"\n✓ 已写入应用 (version={(cand or {}).get('version', '?')}, 旧版已归档可回滚)")
        print("  注: 受影响股票的 chain 将在次日盘后全量重评时自然刷新 (TTL=1);")
        print("      如需立即生效, 手动跑 v3_full_score 全量重评 (--step 9)。")
    elif cand:
        print(f"\n[manual] 仅预览, 未写入。用 --mode auto 应用。")


if __name__ == "__main__":
    main()
