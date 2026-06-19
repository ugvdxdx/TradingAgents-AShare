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

from picker import paths

# 路径统一经 picker.paths 解析 (原 4 层 dirname 回溯已废弃)
V3_CACHE = paths.V3_CACHE
FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR
KLINE_CACHE_DIR = paths.KLINE_CACHE_DIR
MF_CACHE_DIR = paths.MF_CACHE_DIR


# ══════════════════════════════════════════════════════════
# V3 基本面 (打分 + essence)
# ══════════════════════════════════════════════════════════

# 强制纳入候选池的股票 (无论 V3 排名如何, 均参与辩论排序)
FORCE_INCLUDE_CODES: List[str] = ["001309", "600522"]

# 新晋股归因缓存路径 (scan_mispriced.py 产出)
ATTR_CACHE = paths.ATTR_CACHE
# 新晋股保送上限 (避免过多冲淡候选池, 但要覆盖主要热点板块)
MAX_RISING_STAR_INCLUDE = 15


def _rising_star_trend_ok(code: str) -> bool:
    """检查新晋股当前量价趋势是否仍支持 (趋势完整性三条件取一)。

    防止已暴跌的过期归因股被保送进候选池。
    """
    try:
        df = _read_kline_raw(code)
        if df is None or len(df) < 21:
            return True  # K线不足, 不拦截
        df = df.sort_values("trade_date").reset_index(drop=True)
        close = df["close"]
        last, ma5, ma20 = close.iloc[-1], close.iloc[-5:].mean(), close.iloc[-20:].mean()
        high20, low20 = close.iloc[-20:].max(), close.iloc[-20:].min()
        # 三条件取一: 均线多头 / 高位区间 / 未创新低
        return (ma5 >= ma20 * 0.97) or (last >= high20 * 0.80) or (last >= low20 * 1.05)
    except Exception:
        return True


def _load_rising_stars() -> List[Dict[str, Any]]:
    """从归因缓存加载量价新晋股 (板块供需型), 保送进入候选池。

    排序: 按近20日涨幅降序 (优先保送最强势的), 截断到 MAX_RISING_STAR_INCLUDE。
    """
    if not os.path.exists(ATTR_CACHE):
        return []
    try:
        cache = json.load(open(ATTR_CACHE))
    except Exception:
        return []

    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    candidates = []
    for code, entry in cache.items():
        if not entry.get("is_sector_wide"):
            continue
        if entry.get("cached_date", "") < cutoff:
            continue
        if not _rising_star_trend_ok(code):
            continue
        name = entry.get("name", "")
        if not name:
            try:
                name = json.load(open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json"))).get("name", "")
            except Exception:
                pass
        # 算近20日涨幅用于排序
        r20 = 0.0
        try:
            df = _read_kline_raw(code)
            if df is not None and len(df) >= 21:
                df = df.sort_values("trade_date").reset_index(drop=True)
                r20 = (df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100
        except Exception:
            pass
        candidates.append((r20, code, name, entry))

    candidates.sort(key=lambda x: -x[0])
    stars = []
    # 加载 V3 cache (新晋股可能有真实评分)
    try:
        v3_cache_data = json.load(open(V3_CACHE))
    except Exception:
        v3_cache_data = {}

    for r20, code, name, entry in candidates[:MAX_RISING_STAR_INCLUDE]:
        v3_entry = v3_cache_data.get(code, {})
        # 优先用 V3 真实评分 (chain/delivery/essence), 归因作为补充
        real_chain = v3_entry.get("chain", 0)
        real_essence = v3_entry.get("essence", {})

        if real_chain > 0 and real_essence:
            # V3 有评分 — 用真实 essence, 追加归因标记
            essence = dict(real_essence)
            essence["chain_position"] = f"{essence.get('chain_position','')} + 量价归因: {entry.get('sector_tag','')}"
            # 保留真实 V3 分 (sector_score) 作为 v3, 仅靠 _rising_star 标记身份。
            # 此前把 v3 清零会导致: 保送失败(未进前3)的新晋股 v3=0 沉到候选池底部,
            # 既无法被分组海选选中, 也不能参与正常排序竞争 → 彻底沦为废票。
            real_sector_score = v3_entry.get("sector_score", 0)
            star = {
                "code": code, "name": name,
                "v3": real_sector_score,  # 保留真实分, 参与正常竞争
                "chain": real_chain,
                "delivery": v3_entry.get("delivery", 0),
                "capital": v3_entry.get("capital", 0),
                "brief": v3_entry.get("brief", "") or entry.get("summary", ""),
                "essence": essence,
            }
        else:
            # V3 无评分 — 用归因模板 (无真实分, 仅靠 _rising_star 保送机制)
            star = {
                "code": code, "name": name,
                "v3": 0, "chain": 0, "delivery": 0, "capital": 0,
                "brief": entry.get("summary", ""),
                "essence": {
                    "chain_position": f"新晋股: {entry.get('sector_tag', '')} (板块供需缺口驱动)",
                    "core_catalyst": entry.get("summary", ""),
                    "biggest_bull": entry.get("summary", ""),
                    "biggest_bear": f"板块供需逻辑待验证, {entry.get('sector_tag', '')}涨价持续性存疑",
                    "quality_redline": "业绩兑现度待中报验证",
                    "catalyst_horizon": "near",
                },
            }
        star["screen_reason"] = "量价新晋股保送"
        star["_rising_star"] = True
        stars.append(star)
    return stars


# 行业动量缓存 (避免每只股票都查一次数据库)
_SECTOR_MOMENTUM_CACHE: Optional[dict] = None
_KW_INDEX_CACHE = None


def _get_sector_momentum_cached() -> dict:
    """获取板块动量 (单次查询, 全局缓存)。"""
    global _SECTOR_MOMENTUM_CACHE
    if _SECTOR_MOMENTUM_CACHE is not None:
        return _SECTOR_MOMENTUM_CACHE
    try:
        from tradingagents.research.consumer import get_sector_momentum
        _SECTOR_MOMENTUM_CACHE = get_sector_momentum(days=5)
    except Exception:
        _SECTOR_MOMENTUM_CACHE = {}
    return _SECTOR_MOMENTUM_CACHE


def _get_kw_index_cached():
    """获取板块关键词索引 (单次构建, 全局缓存)。"""
    global _KW_INDEX_CACHE
    if _KW_INDEX_CACHE is None:
        try:
            from tradingagents.research.normalize import get_sector_keyword_index
            _KW_INDEX_CACHE = get_sector_keyword_index()
        except Exception:
            _KW_INDEX_CACHE = {}
    return _KW_INDEX_CACHE


def _compute_sector_momentum_factor(industry: str) -> float:
    """根据个股所属行业的近5日动量, 计算调整因子 (0.90~1.10)。

    热门板块加分(最高+10%), 冷门板块减分(最低-10%), 中性不变。
    每天选股时实时计算, 叠加到 V3 基准分上。
    """
    try:
        momentum = _get_sector_momentum_cached()
        if not momentum.get("hot_sectors"):
            return 1.0  # 无研报数据, 不调整

        # 归类个股到标准板块
        kw_index = _get_kw_index_cached()
        best_sector, best_hits = "", 0
        for sector, keywords in kw_index.items():
            hits = sum(1 for kw in keywords if kw in (industry or ""))
            if hits > best_hits:
                best_hits, best_sector = hits, sector
        if not best_sector:
            return 1.0

        # 热门板块 → 加分, 冷门板块 → 减分
        hot_sectors = {s["sector"] for s in momentum.get("hot_sectors", [])}
        cold_sectors = {s["sector"] for s in momentum.get("cold_sectors", [])}
        emerging = {s["sector"] for s in momentum.get("emerging_sectors", [])}

        if best_sector in hot_sectors:
            # 热门度越高加分越多 (根据 hot_sectors 排名)
            hot_list = [s["sector"] for s in momentum.get("hot_sectors", [])]
            rank = hot_list.index(best_sector) if best_sector in hot_list else 5
            return 1.10 - rank * 0.005  # Top1=1.10, Top2=1.095, ... 递减
        elif best_sector in emerging:
            return 1.05
        elif best_sector in cold_sectors:
            return 0.92
        return 1.0
    except Exception:
        return 1.0  # 出错不调整


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


def load_top_n(n: int = 50, include_codes: Optional[List[str]] = None,
               v3_cache: Optional[dict] = None) -> List[Dict[str, Any]]:
    """加载 V3 Top-N, 附带 essence(定性精华) + brief + name。

    三层数据来源:
      1. V3 Top-N (按 sector_score 降序) — 季度级静态锚
      2. 强制纳入 (FORCE_INCLUDE + include_codes)
      3. 量价新晋股保送 (板块供需型归因, 最多15只)

    行业动量调整: 加载后对每只股按所属板块近5日动量微调 V3 (±10%),
    使每日排序反映最新行业热度。V3 本身不变。

    Args:
        v3_cache: 外部传入的 V3 cache (含 capital 动态更新), 避免读文件竞争。
                  为 None 则从文件读取。
    """
    d = v3_cache if v3_cache is not None else json.load(open(V3_CACHE))
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
            v = {"sector_score": 0}
        stocks.append(_build_stock(code, v))
        present.add(code)

    # 量价新晋股保送 (板块供需型归因, scan_mispriced.py 产出)
    rising_stars = _load_rising_stars()
    for star in rising_stars:
        if star["code"] not in present:
            stocks.append(star)
            present.add(star["code"])

    # 行业动量调整 (方案3: 每日实时行业动量微调 V3)
    for s in stocks:
        # 新晋股若有真实 V3 分 (sector_score>0) 应参与动量微调;
        # 仅 v3=0 的归因模板型新晋股跳过 (无基准分可调)。
        if s.get("_rising_star") and s.get("v3", 0) == 0:
            continue
        # 只读一次 JSON 取 industry
        industry = ""
        try:
            with open(os.path.join(FUNDAMENTALS_DIR, f"{s['code']}.json")) as f:
                fd = json.load(f)
            industry = fd.get("industry", "") or fd.get("business_overview", {}).get("industry", "")
        except Exception:
            pass
        factor = _compute_sector_momentum_factor(industry)
        s["v3_original"] = s["v3"]
        s["v3"] = round(s["v3"] * factor, 1)
        s["momentum_factor"] = round(factor, 3)

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
