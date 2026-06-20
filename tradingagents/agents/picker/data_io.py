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
# 新晋股全部纳入候选池 (不限上限; 海选辩论能消化更多候选)。
# 旧值15会截断r20排名靠后的合格新晋股, 与"新晋股全部进入辩论"的设计相悖。
MAX_RISING_STAR_INCLUDE = None


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


def _backtest_rising_stars(cutoff_date: str) -> List[Dict[str, Any]]:
    """回测模式: 现场发现并归因新晋股 (无前视偏差)。

    实盘模式读归因缓存(当前快照), 回测模式必须现场重建:
      1. 用截止日截断的K线扫描量价异动股 (涨幅>阈值 且 V3分偏低)
      2. 调 attribute_stock(cutoff_date=...) 用研报+LLM现场归因
         (跳过网络搜索, 避免返回当前信息造成前视偏差)
      3. 只保留板块供需型(is_sector_wide=True)

    复用 scan_mispriced 的发现+归因逻辑, 但全程基于截断数据。
    """
    from picker.discovery.scan_mispriced import (
        scan_price_momentum, attribute_stock, load_v3_scores, load_fundamentals_meta,
    )

    print(f"  [回测新晋股] 扫描 {cutoff_date} 前的量价异动股...")
    scores = load_v3_scores()
    meta = load_fundamentals_meta()

    # 1. 扫描异动股: 近20日涨幅>15% 且 V3<15 (被低估的强势股)
    #    scan_price_momentum 用的是 load_kline(无cutoff), 这里需要截断
    #    直接内联扫描逻辑, 用截断K线
    gems = []
    for code, v in scores.items():
        if not isinstance(v, dict) or "sector_score" not in v:
            continue
        score = v["sector_score"]
        if score >= 15.0:
            continue
        df = load_kline(code, cutoff_date)  # 截断到cutoff_date
        if df is None or len(df) < 21:
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        r20 = (df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100
        if r20 < 15.0:
            continue
        m = meta.get(code, {})
        gems.append({"code": code, "name": m.get("name", ""),
                     "score": score, "r20": r20,
                     "industry": m.get("industry", ""),
                     "chain": v.get("chain", 0)})

    gems.sort(key=lambda x: -x["r20"])
    top_gems = gems[:15]
    print(f"  [回测新晋股] 发现 {len(gems)} 只异动股, 对前{len(top_gems)}只并发归因...")

    # 2. 现场归因 (并发, 跳过网络搜索, 用研报+LLM)
    from concurrent.futures import ThreadPoolExecutor

    def _attr_one(g):
        attr = attribute_stock(
            g["code"], g["name"], g["r20"], 20, g["industry"],
            use_cache=False, cutoff_date=cutoff_date,
        )
        return g, attr

    sector_wide = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for g, attr in ex.map(_attr_one, top_gems):
            # 回测模式研报覆盖不足, 归因常为"未知"。放宽: 板块供需型 OR 未知
            # (宁可多进候选池, 由海选辩论竞争筛选; 排除明确的"个股事件/概念炒作")
            rt = attr.get("reason_type", "未知")
            if rt in ("板块供需", "政策催化", "未知"):
                g["attribution"] = attr
                sector_wide.append(g)

    print(f"  [回测新晋股] 归因完成: {len(sector_wide)}/{len(top_gems)} 只入选 "
          f"(板块供需+未知, 排除个股事件/炒作)")

    # 3. 构造 rising_star 结构 (与实盘 _load_rising_stars 输出一致)
    try:
        v3_cache_data = json.load(open(V3_CACHE))
    except Exception:
        v3_cache_data = {}

    stars: List[Dict[str, Any]] = []
    for g in sector_wide[:MAX_RISING_STAR_INCLUDE]:
        code = g["code"]
        v3_entry = v3_cache_data.get(code, {})
        attr = g["attribution"]
        real_chain = v3_entry.get("chain", 0)
        real_essence = v3_entry.get("essence", {})

        if real_chain > 0 and real_essence:
            essence = dict(real_essence)
            essence["chain_position"] = f"{essence.get('chain_position','')} + 回测归因: {attr.get('sector_tag','')}"
            star = {
                "code": code, "name": g["name"],
                "v3": v3_entry.get("sector_score", 0),
                "chain": real_chain,
                "delivery": v3_entry.get("delivery", 0),
                "capital": v3_entry.get("capital", 0),
                "brief": v3_entry.get("brief", "") or attr.get("summary", ""),
                "essence": essence,
            }
        else:
            star = {
                "code": code, "name": g["name"],
                "v3": 0, "chain": 0, "delivery": 0, "capital": 0,
                "brief": attr.get("summary", ""),
                "essence": {
                    "chain_position": f"回测新晋股: {attr.get('sector_tag','')}",
                    "core_catalyst": attr.get("summary", ""),
                    "biggest_bull": attr.get("summary", ""),
                    "biggest_bear": f"板块供需逻辑待验证, {attr.get('sector_tag','')}",
                    "quality_redline": "业绩兑现度待验证",
                    "catalyst_horizon": "near",
                },
            }
        star["screen_reason"] = "回测新晋股(现场归因)"
        star["_rising_star"] = True
        stars.append(star)
    return stars


def _load_rising_stars(cutoff_date: str = "") -> List[Dict[str, Any]]:
    """从归因缓存加载量价新晋股 (板块供需型), 保送进入候选池。

    排序: 按近20日涨幅降序 (优先保送最强势的), 截断到 MAX_RISING_STAR_INCLUDE。

    Args:
        cutoff_date: 回测截止日。非空时进入【回溯模式】: 不读实时归因缓存
            (那是当前快照有前视偏差), 而是现场扫描截断K线发现异动股,
            并调 attribute_stock(cutoff_date=...) 用研报+LLM现场归因。
    """
    if cutoff_date:
        return _backtest_rising_stars(cutoff_date)

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


# 研报热门股 (近期博主多次看多但不在 V3 Top50 的个股) 全部纳入 (不限上限)。
MAX_RESEARCH_HOT_INCLUDE = None


def _load_research_hot_stocks(existing_codes: Optional[List[str]] = None,
                              cutoff_date: str = "") -> List[Dict[str, Any]]:
    """加载研报热门股 (近14天 bullish 提及≥2 但不在现有候选池的个股)。

    从 research.db 聚合近期博主看多的个股, 保送进入候选池 (与 V3/新晋股并列)。
    保留真实 V3 分数 (若 V3 cache 有评分), 仅在缺失时用研报模板。
    与新晋股一样: 身份靠 _research_hot 标记, 不靠 v3 清零区分。
    """
    try:
        from tradingagents.research.consumer import get_dark_horse_stocks
        dark_horses = get_dark_horse_stocks(
            cutoff_date=cutoff_date, days=14,
            existing_codes=existing_codes, min_bullish=2,
        )
    except Exception:
        return []

    if not dark_horses:
        return []

    # 加载 V3 cache (研报股可能有真实评分)
    try:
        v3_cache_data = json.load(open(V3_CACHE))
    except Exception:
        v3_cache_data = {}

    present = set(existing_codes or [])
    out: List[Dict[str, Any]] = []
    for dh in dark_horses[:MAX_RESEARCH_HOT_INCLUDE]:
        code = dh["code"]
        if code in present:
            continue
        present.add(code)
        v3_entry = v3_cache_data.get(code, {})
        real_chain = v3_entry.get("chain", 0)
        real_essence = v3_entry.get("essence", {})

        if real_chain > 0 and real_essence:
            # V3 有评分 — 用真实 essence + 研报催化, 保留真实分
            essence = dict(real_essence)
            essence["chain_position"] = f"{essence.get('chain_position','')} + 研报{dh.get('bullish_count',0)}次看多"
            out.append({
                "code": code, "name": dh.get("name", ""),
                "v3": v3_entry.get("sector_score", 0),  # 保留真实分
                "chain": real_chain,
                "delivery": v3_entry.get("delivery", 0),
                "capital": v3_entry.get("capital", 0),
                "brief": v3_entry.get("brief", "") or "; ".join(dh.get("reasons", [])[:2]),
                "essence": essence,
                "screen_reason": "研报热门股",
                "_research_hot": True,
            })
        else:
            # V3 无评分 — 用研报模板 (v3=0, 仅靠身份标记区分)
            out.append({
                "code": code, "name": dh.get("name", ""),
                "v3": 0, "chain": 0, "delivery": 0, "capital": 0,
                "brief": "; ".join(dh.get("reasons", [])[:2]),
                "essence": {
                    "chain_position": f"研报黑马: {dh.get('reasons', [''])[0][:20]}",
                    "core_catalyst": "; ".join(dh.get("reasons", [])[:2]),
                    "biggest_bull": f"研报{dh.get('bullish_count',0)}次看多",
                    "biggest_bear": "研报看多但缺乏V3基本面验证",
                    "quality_redline": "基本面数据待补充",
                    "catalyst_horizon": "near",
                },
                "screen_reason": "研报热门股",
                "_research_hot": True,
            })
    return out


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
               v3_cache: Optional[dict] = None,
               cutoff_date: str = "") -> List[Dict[str, Any]]:
    """加载候选池, 附带 essence(定性精华) + brief + name。

    四路数据来源统一在 stage1 汇入 (此前三路入口分散在 stage1/stage3, 且保送/注入时
    v3 被清零导致排序垫底; 现统一保留真实 v3, 靠身份标记区分):
      1. V3 Top-N (按 sector_score 降序) — 季度级静态锚
      2. 强制纳入 (FORCE_INCLUDE + include_codes)
      3. 量价新晋股 (板块供需型归因, 全部≤15只, 保留真实v3)
      4. 研报热门股 (近14天博主多次看多但不在Top50, 全部≤15只, 保留真实v3)

    行业动量调整: 加载后对每只股按所属板块近5日动量微调 V3 (±10%),
    使每日排序反映最新行业热度。V3 本身不变。

    Args:
        v3_cache: 外部传入的 V3 cache (含 capital 动态更新), 避免读文件竞争。
                  为 None 则从文件读取。
        cutoff_date: 回测模式截止日 (传给研报查询); 实盘留空。
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

    # 量价新晋股保送 (实盘读归因缓存; 回测现场扫描+归因, cutoff_date传入)
    rising_stars = _load_rising_stars(cutoff_date=cutoff_date)
    for star in rising_stars:
        if star["code"] not in present:
            stocks.append(star)
            present.add(star["code"])

    # 研报热门股保送 (近14天博主多次看多但不在候选池的个股)
    research_hots = _load_research_hot_stocks(
        existing_codes=list(present), cutoff_date=cutoff_date)
    for rh in research_hots:
        if rh["code"] not in present:
            stocks.append(rh)
            present.add(rh["code"])

    # 行业动量调整 (方案3: 每日实时行业动量微调 V3)
    for s in stocks:
        # 身份标记股 (新晋股/研报股) 若有真实 V3 分应参与动量微调;
        # 仅 v3=0 的模板型 (无V3数据) 跳过 (无基准分可调)。
        if (s.get("_rising_star") or s.get("_research_hot")) and s.get("v3", 0) == 0:
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
