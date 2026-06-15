"""debate_picker v5 — 数据采集层 (M2)。

整合 V3 基本面打分 + essence、K线技术面、资金流，支持实盘与回测两种模式。
回测模式按 cutoff_date 截断所有数据，防止未来函数。
"""
from __future__ import annotations

import glob
import json
import os
import pickle
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

V3_CACHE = os.path.join(ROOT, ".fundamental_v3_scores.json")
FUNDAMENTALS_DIR = os.path.join(ROOT, "fundamentals")
KLINE_CACHE_DIR = os.path.join(ROOT, "kline_cache")
MF_CACHE_DIR = os.path.join(ROOT, ".mf_cache")


# ══════════════════════════════════════════════════════════
# V3 基本面 (打分 + essence)
# ══════════════════════════════════════════════════════════

# 强制纳入候选池的股票 (无论 V3 排名如何, 均参与辩论排序)
FORCE_INCLUDE_CODES: List[str] = ["001309", "600522"]


def _build_stock(code: str, v: Dict[str, Any]) -> Dict[str, Any]:
    name = ""
    try:
        with open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json")) as f:
            name = json.load(f).get("name", "")
    except Exception:
        pass
    return {
        "code": code,
        "name": name,
        "v3": v.get("sector_score", 0),
        "chain": v.get("chain", 0),
        "delivery": v.get("delivery", 0),
        "capital": v.get("capital", 0),
        "essence": v.get("essence", {}),
        "brief": v.get("brief", ""),
    }


def load_top_n(n: int = 50, include_codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """加载 V3 Top-N, 附带 essence(定性精华) + brief + name。

    include_codes: 强制纳入的股票代码 (即使不在 Top-N 内), 用于人工指定关注标的。
    """
    with open(V3_CACHE) as f:
        d = json.load(f)
    scored = [(c, v) for c, v in d.items() if "sector_score" in v]
    scored.sort(key=lambda x: -x[1]["sector_score"])
    stocks: List[Dict[str, Any]] = [_build_stock(code, v) for code, v in scored[:n]]

    # 强制纳入 (去重)
    force = list(FORCE_INCLUDE_CODES) + list(include_codes or [])
    present = {s["code"] for s in stocks}
    dmap = dict(scored)
    for code in force:
        if code in present:
            continue
        v = dmap.get(code)
        if v is None:
            # V3 缓存里没有该股, 仍以最小信息纳入 (name 来自 fundamentals)
            v = {"sector_score": 0}
        stocks.append(_build_stock(code, v))
        present.add(code)
    return stocks


# ══════════════════════════════════════════════════════════
# K 线 (技术面)
# ══════════════════════════════════════════════════════════

def _read_kline_raw(code: str):
    suffix = ".SH" if code.startswith("6") else ".SZ"
    path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}".replace(".", "_") + ".pkl")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        return df if hasattr(df, "__len__") and len(df) > 0 else None
    except Exception:
        return None


def load_kline(code: str, cutoff_date: Optional[str] = None):
    """读取 K 线; cutoff_date 非空时截断到该日(含), trade_date 作为 index。

    返回 None 表示数据不足 (< 20 根)。
    """
    df = _read_kline_raw(code)
    if df is None or len(df) == 0:
        return None
    if cutoff_date:
        df = df[df["trade_date"] <= cutoff_date].copy()
    if len(df) < 20:
        return None
    return df


# ══════════════════════════════════════════════════════════
# 资金流 (5 日主力净流入)
# ══════════════════════════════════════════════════════════

_MF_CACHE: Optional[Dict[str, list]] = None


def load_mf_cache() -> Dict[str, list]:
    """加载最新的 money_flow pickle 缓存 → {code: [daily_rows]}。"""
    if not os.path.exists(MF_CACHE_DIR):
        return {}
    files = sorted(glob.glob(os.path.join(MF_CACHE_DIR, "mf_*.pkl")), reverse=True)
    for fp in files:
        try:
            with open(fp, "rb") as f:
                raw = pickle.load(f)
            out: Dict[str, list] = {}
            for k, v in raw.items():
                if isinstance(v, list) and len(v) >= 5:
                    code = k.rsplit("_", 1)[0]
                    if code not in out or len(v) > len(out[code]):
                        out[code] = v
            if out:
                return out
        except Exception:
            continue
    return {}


def fund_flow_5d(mf_cache: Dict[str, list], code: str,
                 cutoff_date: Optional[str] = None) -> Optional[float]:
    """近 5 日主力净流入(亿)。cutoff_date 非空时只取该日前的数据。

    返回 None 表示资金流数据缺失。
    """
    rows = mf_cache.get(code)
    if not rows:
        return None
    if cutoff_date:
        cutoff_compact = cutoff_date.replace("-", "")
        rows = [r for r in rows if r.get("date", "") <= cutoff_compact]
    if len(rows) < 5:
        return None
    return round(sum(r.get("main_net", 0) for r in rows[-5:]) / 1e8, 1)


# ══════════════════════════════════════════════════════════
# 分组 (蛇形, 避免强弱扎堆)
# ══════════════════════════════════════════════════════════

def snake_split(items: List[Any], n_groups: int) -> List[List[Any]]:
    """蛇形分组 (1-2-3-...-n-n-...-3-2-1), 让强弱均匀分布到各组。"""
    groups: List[List[Any]] = [[] for _ in range(n_groups)]
    for i, it in enumerate(items):
        cycle = i // n_groups
        idx = i % n_groups
        if cycle % 2 == 1:
            idx = n_groups - 1 - idx
        groups[idx].append(it)
    return groups
