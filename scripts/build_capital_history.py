#!/usr/bin/env python3
"""构建 capital 历史快照 (回测专用, 无前视)。

为什么需要: capital 在 V3 cache 里是【当前快照】, 用它回测历史 = 偷看未来。
本脚本对每个 cutoff 用【该时点的数据】重建 capital, 存成历史快照, 供回测调用。

capital = base_capital(板块动量) × price_factor(个股量价), 两者 cutoff 化:
  - base_capital: get_sector_momentum(cutoff_date=)  (consumer.py 已支持)
  - price_factor: 截断 K 线算 r5/r20 (复用 v3_full_score 逻辑)

存储格式: data/caches/capital_history.json
  {
    "2025-03-03": {"600519": 2.3, "000001": 1.8, ...},   # 该日每只股的 capital
    "2025-03-05": {...},
    ...
  }
  增量: 已存在的 cutoff 跳过, 断点续跑。

用法:
  uv run python3 scripts/build_capital_history.py                       # 默认每周一个 cutoff
  uv run python3 scripts/build_capital_history.py --step 2              # 每2个交易日
  uv run python3 scripts/build_capital_history.py --start 2025-03-01    # 从该日起
"""
import argparse
import json
import os
import pickle
import sys
import time
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picker import paths
from picker.scoring.v3_full_score import (
    KLINE_CACHE_DIR, _compute_capital_from_momentum, _get_industry,
    _load_sub_sector_override,
)

HISTORY_PATH = os.path.join(paths.CACHES_DIR, "capital_history.json")


# ══════════════════════════════════════════════════════════
# cutoff 化的 price_factor (复用 v3_full_score 逻辑但截断 K 线)
# ══════════════════════════════════════════════════════════

def price_factor_at(code: str, cutoff: str) -> float:
    """用 cutoff 截断的 K 线算 price_factor (r5/r20 双窗口)。

    与 v3_full_score._compute_price_factor 等价, 区别是按 cutoff 截断而非用最新。
    """
    for suffix in ["_SH.pkl", "_SZ.pkl"]:
        path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
        if not os.path.exists(path):
            continue
        try:
            df = pickle.load(open(path, "rb"))
            df = df.sort_values("trade_date").reset_index(drop=True)
            df = df[df["trade_date"] <= cutoff]
            if len(df) < 21:
                return 1.0
            close = df["close"]
            r20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
            r5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0
            if r20 > 20:
                return 1.3 if r5 > 5 else (0.9 if r5 < -5 else 1.1)
            elif r20 > 0:
                return (1.0 + r20 * 0.01) if r5 > 0 else 0.9
            elif r20 > -10:
                return 0.9 if r5 > 0 else 0.7
            else:
                return 0.6
        except Exception:
            return 1.0
    return 1.0


# ══════════════════════════════════════════════════════════
# 单个 cutoff 的 capital 全量重建
# ══════════════════════════════════════════════════════════

def build_capital_at(cutoff: str, v3: dict, kw_index: dict,
                     override_sorted: list) -> Dict[str, float]:
    """重建 cutoff 当天全池的 capital。

    返回 {code: capital}, 仅含有 fundamentals 的股 (与生产 compute_capital_updates 一致)。
    """
    from tradingagents.research.consumer import get_sector_momentum

    # cutoff 化的板块动量
    try:
        momentum = get_sector_momentum(cutoff_date=cutoff, days=14)
    except Exception:
        return {}
    if not momentum.get("hot_sectors"):
        return {}

    def classify(industry):
        # 平局裁决: 命中数相同时取命中关键词最长的板块 (与 v3_full_score._classify_sector 一致)
        if not industry:
            return ""
        best, best_hit, best_kw_len = "", 0, 0
        for sec, kws in kw_index.items():
            matched = [k for k in kws if k in industry]
            h = len(matched)
            if h <= 0:
                continue
            max_kw_len = max(len(k) for k in matched)
            if h > best_hit or (h == best_hit and max_kw_len > best_kw_len):
                best_hit, best_kw_len, best = h, max_kw_len, sec
        return best

    result = {}
    for code, entry in v3.items():
        if not isinstance(entry, dict) or "chain" not in entry:
            continue
        industry = _get_industry(code)
        sector = classify(industry)
        if not sector:
            continue
        base_capital = _compute_capital_from_momentum(sector, momentum)
        for keyword, cap_val in override_sorted:
            if keyword in industry:
                base_capital = cap_val
                break
        pf = price_factor_at(code, cutoff)
        result[code] = round(max(0, min(5.0, base_capital * pf)), 1)
    return result


# ══════════════════════════════════════════════════════════
# 生成 cutoff 列表 (从 K 线交易日中采样)
# ══════════════════════════════════════════════════════════

def get_cutoff_dates(step: int, start: str = "") -> list:
    """从基准 K 线采样交易日作为 cutoff (每 step 个交易日一个)。"""
    df = pickle.load(open(os.path.join(KLINE_CACHE_DIR, "300308_SZ.pkl"), "rb"))
    dates = sorted(df["trade_date"].unique())
    cutoffs = [d for d in dates if not start or d >= start]
    # 每 step 个一个 (保证有足够前向窗口: 30 日验证)
    valid = [d for d in cutoffs if dates.index(d) >= 20 and dates.index(d) <= len(dates) - 31]
    return valid[::step]


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="构建 capital 历史快照")
    parser.add_argument("--step", type=int, default=5, help="cutoff 采样步长(交易日, 默认5≈每周)")
    parser.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    args = parser.parse_args()

    print("=" * 64)
    print("  构建 capital 历史快照 (无前视, 供回测)")
    print("=" * 64)

    v3 = json.load(open(paths.V3_CACHE))
    cutoffs = get_cutoff_dates(args.step, args.start)
    print(f"  cutoff 数: {len(cutoffs)} (步长 {args.step} 交易日, 范围 {cutoffs[0]}~{cutoffs[-1]})")

    # 增量: 加载已有快照
    history = {}
    if os.path.exists(HISTORY_PATH):
        try:
            history = json.load(open(HISTORY_PATH, encoding="utf-8"))
        except Exception:
            history = {}
    done = {c for c in history.keys() if history.get(c)}
    todo = [c for c in cutoffs if c not in done]
    print(f"  已有: {len(done)} | 待算: {len(todo)}")
    if not todo:
        print("  ✓ 全部已存在, 无需计算")
        return

    # 预加载共享资源
    from tradingagents.research.normalize import get_sector_keyword_index
    kw_index = get_sector_keyword_index()
    override_sorted = sorted(_load_sub_sector_override().items(), key=lambda x: -len(x[0]))

    print(f"  {'cutoff':>12} {'板块数':>6} {'个股数':>6} {'hot板块示例':>30}")
    print("  " + "-" * 60)
    t0 = time.time()
    for i, cutoff in enumerate(todo, 1):
        caps = build_capital_at(cutoff, v3, kw_index, override_sorted)
        history[cutoff] = caps
        # 进度
        hot = ""
        try:
            from tradingagents.research.consumer import get_sector_momentum
            m = get_sector_momentum(cutoff_date=cutoff, days=14)
            hot = ",".join(s["sector"] for s in m.get("hot_sectors", [])[:3])
        except Exception:
            pass
        print(f"  {cutoff:>12} {len(set(caps.values())):>6} {len(caps):>6} {hot[:30]:>30}  [{i}/{len(todo)}]")

        # 每 10 个 cutoff 落盘一次 (断点续跑)
        if i % 10 == 0:
            json.dump(history, open(HISTORY_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False)

    # 最终落盘
    json.dump(history, open(HISTORY_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    elapsed = time.time() - t0
    print(f"\n  ✓ 完成: {len(todo)} 个 cutoff, 耗时 {elapsed:.0f}s")
    print(f"  存储: {HISTORY_PATH}")
    print(f"  覆盖范围: {min(history)} ~ {max(history)} | 每期平均 {len(history[min(history)])} 只股")

    # 抽样验证: 与 V3 cache 当前 capital 对比
    print(f"\n  ── 抽样验证 (最新 cutoff vs V3 cache) ──")
    latest = max(history.keys())
    diffs = []
    for code, cap_hist in history[latest].items():
        cap_cache = v3.get(code, {}).get("capital", 0)
        if cap_cache:
            diffs.append(abs(cap_hist - cap_cache))
    if diffs:
        import statistics
        print(f"  最新 cutoff {latest}: {len(diffs)} 只")
        print(f"  |重建 - cache| 均值: {statistics.mean(diffs):.2f} | 中位: {statistics.median(diffs):.2f}")
        print(f"  (差异来自 price_factor 用截断K线 vs 最新K线 + 板块动量时点不同)")


if __name__ == "__main__":
    main()
