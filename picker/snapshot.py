"""每日快照读取工具 (回测用)。

快照由 picker_graph._save_daily_snapshot 在实盘选股后存档:
  data/caches/v3_snapshots/YYYY-MM-DD.json
  {date, scores: {code: {chain, surge, capital}}, ranking: [...]}

回测时按 cutoff 取 ≤ cutoff 的最近快照, 消除 chain/surge 前视偏差。
若某 cutoff 无快照 (历史未存档), 回退到当前 V3 cache (已知近似)。
"""
import json
import os
from typing import Dict, Optional, Tuple

from picker.paths import V3_CACHE, V3_SNAPSHOT_DIR

# 模块级缓存: {cutoff: (scores_dict, source_label)}
_SNAPSHOT_CACHE: Dict[str, Tuple[dict, str]] = {}


def get_snapshot_at(cutoff: str) -> Tuple[dict, str]:
    """取 ≤ cutoff 的最近快照 scores。

    Returns:
        (scores, source): scores={code:{chain,surge,capital}},
                          source="snapshot:YYYY-MM-DD" 或 "v3_cache(无快照,回退)"
    """
    if cutoff in _SNAPSHOT_CACHE:
        return _SNAPSHOT_CACHE[cutoff]

    scores = None
    source = ""

    if os.path.isdir(V3_SNAPSHOT_DIR):
        # 列出所有快照日期, 取 ≤ cutoff 的最近一个
        files = [f[:-5] for f in os.listdir(V3_SNAPSHOT_DIR) if f.endswith(".json")]
        valid = sorted(d for d in files if d <= cutoff)
        if valid:
            latest = valid[-1]
            path = os.path.join(V3_SNAPSHOT_DIR, f"{latest}.json")
            try:
                snap = json.load(open(path, encoding="utf-8"))
                scores = snap.get("scores", {})
                source = f"snapshot:{latest}"
            except Exception:
                pass

    # 无快照: 回退到当前 V3 cache (前视近似)
    if scores is None:
        try:
            cache = json.load(open(V3_CACHE, encoding="utf-8"))
            scores = {c: {"chain": v.get("chain", 0), "surge": v.get("surge", 0),
                          "capital": v.get("capital", 0)}
                      for c, v in cache.items() if isinstance(v, dict) and "chain" in v}
            source = "v3_cache(无快照,回退)"
        except Exception:
            scores = {}
            source = "空(读取失败)"

    _SNAPSHOT_CACHE[cutoff] = (scores, source)
    return scores, source


def clear_cache():
    """清空模块级缓存 (测试用)。"""
    _SNAPSHOT_CACHE.clear()


def snapshot_coverage() -> Tuple[str, str, int]:
    """快照覆盖范围: (最早日期, 最晚日期, 数量)。无快照返回 ("", "", 0)。"""
    if not os.path.isdir(V3_SNAPSHOT_DIR):
        return ("", "", 0)
    files = sorted(f[:-5] for f in os.listdir(V3_SNAPSHOT_DIR) if f.endswith(".json"))
    return (files[0], files[-1], len(files)) if files else ("", "", 0)
