"""每日 TOP50 得分复盘记录。

每次跑完 debate_picker_v5, 把当天全池(或候选池)按锚分排序的 TOP50 落盘到一个
累计历史文件, 方便以后横向复盘 (某只股的历史得分变化、某天的完整排名等)。

存储: data/caches/daily_top50_review.json
  {
    "2026-06-21": [
      {"rank":1, "code":"688256", "name":"寒武纪", "anchor":22.0, "chain":9.8, ...},
      ...
      {"rank":50, ...}
    ],
    "2026-06-20": [...],
  }
  保留最近 365 天。
"""
import json
import os
from typing import Any, Dict, List

REVIEW_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "data", "caches", "daily_top50_review.json")


def _anchor_score(c: Dict[str, Any]) -> float:
    """薄封装, 真相源在 data_io.anchor_score (避免公式两处维护)。"""
    from .data_io import anchor_score
    return anchor_score(c)


def log_top50(trade_date: str, candidates: List[Dict[str, Any]]) -> List[Dict]:
    """记录当天全量候选股得分到历史文件 (按锚分排序, 不截断)。

    Args:
        trade_date: 交易日
        candidates: 候选股列表 (collect_data 的输出, 含全部得分维度)

    Returns:
        当天记录的全量得分列表
    """
    # 按锚分排序, 不截断 — 全量记录
    scored = sorted(candidates, key=lambda c: -_anchor_score(c))
    records = []
    for rank, c in enumerate(scored, 1):
        records.append({
            "rank": rank,
            "code": c.get("code", ""),
            "name": c.get("name", "")[:10],
            "anchor": round(_anchor_score(c), 1),
            "chain": c.get("chain", 0),
            "surge": c.get("surge", 0),
            "capital": c.get("capital", 0),
            "v3": c.get("v3", 0),
            "tech_total": round(c.get("tech_total", 0), 1),
            "fund_5d": round(c.get("fund_5d", 0), 1),
            "r5": c.get("r5"),
            "r20": c.get("r20"),
            "dist_high": c.get("dist_high"),
            "momentum_factor": c.get("momentum_factor"),
        })

    # 加载历史
    history = {}
    if os.path.exists(REVIEW_PATH):
        try:
            history = json.load(open(REVIEW_PATH, encoding="utf-8"))
        except Exception:
            history = {}

    # 记录当天
    history[trade_date] = records

    # 保留最近 180 天 (全量~94只/天, 180天≈1.7万条, JSON约20MB)
    sorted_dates = sorted(history.keys(), reverse=True)
    if len(sorted_dates) > 180:
        history = {d: history[d] for d in sorted_dates[:180]}

    # 落盘
    os.makedirs(os.path.dirname(REVIEW_PATH), exist_ok=True)
    json.dump(history, open(REVIEW_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    return records
