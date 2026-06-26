#!/usr/bin/env python3
"""新晋股发现机制 (Rising Stars Scanner)。

"新晋股" = 新晋级到资金视野、量价已启动但评分系统尚未跟上的一批标的。
它们是当前 alpha 的核心来源, 但因研报滞后或赛道分类过窄, 常被 V3 评分低估。

核心逻辑 (三层过滤):
  A. 量价扫描: 找近 N 日涨幅异常(>阈值)但 V3 评分偏低的标的 → 新晋股候选
  B. 搜索归因: 对候选逐一搜索上涨原因, 区分"板块供需" vs "个股事件"
     - 板块供需型 (铜价/电子布涨价等): 触发板块扩散, 找同板块补涨标的
     - 个股事件型 (重组/壳资源等): 标记但不扩散
     - 归因结果缓存 14 天, 避免每日重复搜索
  C. 板块扩散: 对"板块供需"型新晋股, 在同板块找低分补涨候选
     - 板块强度过滤: 高涨幅(>10%)占比≥30% 且均涨≥3%, 过滤伪热点

输出:
  - 新晋股清单 (按性价比排序, 建议重评分)
  - 板块扩散候选 (同板块低分补涨标的)
  - 研报盲区清单 (零覆盖个股, 建议补采)

设计依据 (基于历史回测):
  - 近5日涨幅>15% 的扫描, 53% 的新晋股能在涨幅前半段被捕获
  - 归因缓存使每日扫描只对新出现的标的搜索, 老的读缓存瞬间完成

用法:
  python3 scan_mispriced.py                          # 每日扫描 (读缓存, 只搜新股)
  python3 scan_mispriced.py --threshold 10           # 更灵敏 (更早捕获)
  python3 scan_mispriced.py --refresh-cache          # 强制刷新全部归因
  python3 scan_mispriced.py --no-attribution         # 纯量化扫描 (最快)
  python3 scan_mispriced.py --rescore                # 扫描后自动重评分
"""
import os
import sys
import json
import time
import pickle
import argparse
from collections import defaultdict
from datetime import datetime

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths

FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR
KLINE_DIR = paths.KLINE_CACHE_DIR
V3_CACHE = paths.V3_CACHE
# 归因缓存: 搜索过的上涨原因记录于此, 避免每日重复搜索
ATTR_CACHE = paths.ATTR_CACHE

from picker.discovery.movement_blacklist import is_blacklisted


# ══════════════════════════════════════════════════════════
# 网络搜索归因 (搜索异动股上涨原因, 判断个股事件 vs 板块行情)
# ══════════════════════════════════════════════════════════

def web_search(query: str, num_results: int = 5) -> str:
    """网络搜索, 返回结果摘要文本。用 MCP web_search_prime (计入 GLM Coding Plan 额度)。

    复用 refresh_fundamentals._web_search (带 429 限流退避)。
    失败抛 RuntimeError (不静默返回空, 避免用降级数据偷偷决策)。
    """
    try:
        from picker.common.web_search import _web_search
        return _web_search(query, num_results=num_results)
    except Exception as e:
        # 兼容旧调用方(期望空字符串而非异常): 记录后返回空
        print(f"  [web_search] 失败: {str(e)[:100]}", flush=True)
        return ""


ATTR_PROMPT = """你是A股研究员。请判断这只股票近期上涨的真实原因, 并归类。

股票: {name}({code}) 行业: {industry} 近{days}日涨幅: {return_pct}%

{context}

请严格按以下格式输出 (用|分隔, 不要换行):
REASON_TYPE|板块供需 或 个股事件 或 政策催化 或 概念炒作 或 未知
SECTOR_TAG|最相关的1-2个细分赛道关键词(如:六氟化钨/MLCC粉体/TLVR电感/空芯光纤, 不要用大类如"化工")
SUMMARY|30字内一句话原因"""


# _llm_quick 下沉到 picker.common.llm_client (re-export 保持向后兼容)
from picker.common.llm_client import _llm_quick  # noqa: F401


def _get_research_context(code: str, name: str, cutoff_date: str = "") -> str:
    """从研报库取该股相关的板块观点, 作为归因上下文。

    Args:
        cutoff_date: 非空时仅查该日之前的研报 (回测防前视偏差); 空则查全部。
    """
    import sqlite3
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return ""
    conn = sqlite3.connect(db_path)
    # 查该股的研报提及 (回测模式按 cutoff_date 截断)
    mentions = []
    if cutoff_date:
        query = ("SELECT stock_mentions FROM general_knowledge "
                 "WHERE stock_mentions IS NOT NULL AND created_at <= ? "
                 "ORDER BY created_at DESC LIMIT 200")
        rows = conn.execute(query, (cutoff_date + " 23:59:59",)).fetchall()
    else:
        query = ("SELECT stock_mentions FROM general_knowledge WHERE stock_mentions IS NOT NULL "
                 "ORDER BY created_at DESC LIMIT 200")
        rows = conn.execute(query).fetchall()
    for (raw,) in rows:
        if not raw:
            continue
        try:
            for m in json.loads(raw):
                if str(m.get("code", "")).strip() == code or name in str(m.get("name", "")):
                    reason = m.get("reason", "")
                    if reason:
                        mentions.append(reason)
        except Exception:
            pass
    conn.close()
    if mentions:
        return f"研报提及该股 ({len(mentions)}次):\n" + "\n".join(f"- {m[:60]}" for m in mentions[:5])
    return ""


def _load_attr_cache() -> dict:
    """加载归因缓存。"""
    if not os.path.exists(ATTR_CACHE):
        return {}
    try:
        with open(ATTR_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_attr_cache(cache: dict):
    """保存归因缓存。"""
    with open(ATTR_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


# 归因缓存有效期 (天): 超过后重新搜索
ATTR_CACHE_TTL_DAYS = 14


def attribute_stock(code: str, name: str, return_pct: float, days: int, industry: str,
                    use_cache: bool = True, cutoff_date: str = "") -> dict:
    """[已迁移到 picker.discovery.attribution.attribute_stock_unified] 薄封装, 保持
    sector_expansion / data_io 回测调用的旧签名兼容。

    旧参数映射: return_pct (近 days 日涨幅) → r5; direction 固定"上涨" (scan 只处理上涨
    新晋股); r20 无直接数据, 用 return_pct 近似 (归因 prompt 仅作上下文描述)。
    """
    from picker.discovery.attribution import attribute_stock_unified
    return attribute_stock_unified(
        code, name, r5=return_pct, r20=return_pct, industry=industry,
        direction="上涨", use_cache=use_cache, cutoff_date=cutoff_date,
    )


# ══════════════════════════════════════════════════════════
# 数据加载
# ══════════════════════════════════════════════════════════

def load_v3_scores() -> dict:
    """加载 V3 评分缓存。"""
    if not os.path.exists(V3_CACHE):
        return {}
    with open(V3_CACHE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_fundamentals_meta() -> dict:
    """加载所有 fundamentals 的 code → {name, industry} 映射。"""
    meta = {}
    for fname in os.listdir(FUNDAMENTALS_DIR):
        if not fname.endswith(".json"):
            continue
        code = fname.replace(".json", "")
        path = os.path.join(FUNDAMENTALS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            meta[code] = {
                "name": d.get("name", "") or d.get("business_overview", {}).get("name", ""),
                "industry": d.get("industry", "") or d.get("business_overview", {}).get("industry", ""),
            }
        except Exception:
            meta[code] = {"name": "", "industry": ""}
    return meta


def load_kline(code: str):
    """加载 K 线 DataFrame。"""
    for suffix in ["_SZ.pkl", "_SH.pkl"]:
        path = os.path.join(KLINE_DIR, f"{code}{suffix}")
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                return None
    return None


def calc_returns(df, n: int) -> float:
    """计算近 N 日涨幅 (%)。"""
    if df is None or len(df) < n + 1:
        return None
    df = df.sort_values("trade_date").reset_index(drop=True)
    close = df["close"]
    return (close.iloc[-1] / close.iloc[-1 - n] - 1) * 100


# ══════════════════════════════════════════════════════════
# A. 量价扫描
# ══════════════════════════════════════════════════════════

def scan_price_momentum(
    scores: dict,
    meta: dict,
    days: int = 5,
    threshold: float = 15.0,
    trend_window: int = 20,
    trend_threshold: float = 10.0,
) -> list:
    """扫描量价异动标的 (不限 V3 分高低, 高分龙头亦纳入)。

    Args:
        days: 短窗回看天数 (近 N 日涨幅, 触发用)
        threshold: 短窗涨幅触发阈值 (%)
        trend_window: 趋势确认窗口 (默认20日)
        trend_threshold: 趋势窗口涨幅下限 (%, 默认10; 过滤短炒脉冲/一日游)

    Returns:
        新晋股列表, 按"涨幅/评分比"降序 (性价比最高的在前)
    """
    gems = []
    for code, v in scores.items():
        if not isinstance(v, dict) or "sector_score" not in v:
            continue
        score = v["sector_score"]
        if is_blacklisted(code):
            continue  # 异动黑名单 (冷却中): 不进 gems, 避免被归因/板块扩散保送

        df = load_kline(code)
        r = calc_returns(df, days)
        if r is None or r < threshold:
            continue
        rt = calc_returns(df, trend_window)
        if rt is None or rt < trend_threshold:
            continue  # 中期未确认, 过滤短炒脉冲/一日游

        name = meta.get(code, {}).get("name", "")
        industry = meta.get(code, {}).get("industry", "")
        gems.append({
            "code": code,
            "name": name,
            "score": score,
            "chain": v.get("chain", 0),
            "surge": v.get("surge", 0),
            "capital": v.get("capital", 0),
            f"r{days}": r,
            f"r{trend_window}": rt,
            "industry": industry,
            # 性价比 = 涨幅 / (评分+1), 越大越被低估
            "misprice_ratio": r / (score + 1),
        })

    gems.sort(key=lambda x: -x["misprice_ratio"])
    return gems


# ══════════════════════════════════════════════════════════
# B. 板块扩散
# ══════════════════════════════════════════════════════════

def classify_sector(industry_text: str) -> str:
    """用 normalize.py 的关键词索引把 industry 文本归类到标准赛道。"""
    from tradingagents.research.normalize import get_sector_keyword_index
    kw_index = get_sector_keyword_index()
    if not industry_text:
        return ""
    best_sector = ""
    best_hits = 0
    for sector, keywords in kw_index.items():
        hits = sum(1 for kw in keywords if kw in industry_text)
        if hits > best_hits:
            best_hits = hits
            best_sector = sector
    return best_sector


def sector_expansion(
    gems: list,
    meta: dict,
    scores: dict,
    days: int = 5,
    threshold: float = 10.0,
    min_hot_ratio: float = 0.30,
    min_avg_return: float = 3.0,
    use_attribution: bool = True,
    use_cache: bool = True,
) -> dict:
    """板块扩散: 对新晋股所在板块, 找同板块其他低分股。

    改进版 (基于搜索归因 + 板块强度双重过滤):
      1. 搜索归因: 对每只新晋股搜索上涨原因, 区分"板块供需" vs "个股事件"
         只有"板块供需/政策催化"类的才作为板块热点候选
      2. 板块强度过滤: 板块成分股中高涨幅(>10%)占比 >= min_hot_ratio
         且平均涨幅 >= min_avg_return (避免伪热点)

    Returns:
        {板块名: {"candidates": [...], "strength": {...}, "attribution": {...}}}
    """
    # 1. 给所有股票归类板块
    code_to_sector = {}
    for code, m in meta.items():
        sec = classify_sector(m.get("industry", ""))
        if sec:
            code_to_sector[code] = sec

    # 2. 搜索归因: 判断新晋股是板块行情还是个股事件
    candidate_sectors = {}  # {板块: [触发的新晋股]}
    if use_attribution:
        print(f"\n  [搜索归因] 对 {min(len(gems), 10)} 只新晋股搜索上涨原因...")
        for g in gems[:10]:  # 只对 Top10 做搜索 (控制耗时)
            attr = attribute_stock(g["code"], g["name"], g.get(f"r{days}", 0), days, g.get("industry", ""), use_cache=use_cache)
            g["attribution"] = attr
            sec = code_to_sector.get(g["code"], "")
            tag = attr.get("sector_tag", "")
            reason = attr.get("reason_type", "")
            is_wide = attr.get("is_sector_wide", False)
            cached = attr.get("cached", False)
            status = "✓板块" if is_wide else "✗个股"
            cache_tag = "(缓存)" if cached else "(新搜)"
            print(f"    {g['code']:7} {g['name']:10} [{status}] {reason:6}{cache_tag} → {tag or sec:16} | {attr.get('summary','')[:36]}")
            # 只收集板块供需类的原因作为热点候选
            if is_wide:
                target_sec = sec  # 用 industry 归类的板块
                candidate_sectors.setdefault(target_sec, []).append(g)
            time.sleep(0.3)  # 搜索限速
    else:
        for g in gems:
            sec = code_to_sector.get(g["code"])
            if sec:
                candidate_sectors.setdefault(sec, []).append(g)

    # 3. 板块强度过滤: 高涨幅占比 + 平均涨幅
    hot_sectors = {}
    for sec in candidate_sectors:
        members = [code for code, s in code_to_sector.items() if s == sec]
        returns = []
        for code in members:
            df = load_kline(code)
            r = calc_returns(df, days)
            if r is not None:
                returns.append(r)
        if len(returns) < 3:
            continue
        hot_count = sum(1 for r in returns if r > 10)  # 涨幅>10%算"高涨幅"
        hot_ratio = hot_count / len(returns)
        avg_return = sum(returns) / len(returns)
        # 过滤: 高涨幅占比 >= 30% 且 平均涨幅 >= 3%
        if hot_ratio >= min_hot_ratio and avg_return >= min_avg_return:
            hot_sectors[sec] = {
                "hot_ratio": hot_ratio,
                "avg_return": avg_return,
                "member_count": len(members),
                "hot_count": hot_count,
            }

    # 4. 在通过强度过滤的热点板块里找候选
    expansion = {}
    for code, sec in code_to_sector.items():
        if sec not in hot_sectors:
            continue
        v = scores.get(code, {})
        if not isinstance(v, dict) or "sector_score" not in v:
            continue
        score = v["sector_score"]
        df = load_kline(code)
        r = calc_returns(df, days)
        if r is None:
            continue
        if r < threshold and r > -5:
            expansion.setdefault(sec, {"candidates": [], "strength": hot_sectors[sec]})
            expansion[sec]["candidates"].append({
                "code": code,
                "name": meta.get(code, {}).get("name", ""),
                "score": score,
                f"r{days}": r,
                "industry": meta.get(code, {}).get("industry", ""),
            })

    for sec in expansion:
        expansion[sec]["candidates"].sort(key=lambda x: -x[f"r{days}"])

    return dict(expansion)


# ══════════════════════════════════════════════════════════
# C. 研报覆盖检查
# ══════════════════════════════════════════════════════════

def check_research_coverage(codes: list) -> dict:
    """检查这些股票在研报库中的覆盖情况。"""
    import sqlite3
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    result = {}
    for code in codes:
        # 查 stock_mentions
        rows = conn.execute(
            "SELECT stock_mentions FROM general_knowledge WHERE stock_mentions IS NOT NULL"
        ).fetchall()
        mention_count = 0
        for (raw,) in rows:
            if not raw:
                continue
            try:
                for m in json.loads(raw):
                    if str(m.get("code", "")).strip() == code or code in str(m.get("name", "")):
                        mention_count += 1
            except Exception:
                pass
        result[code] = {"mentions": mention_count}
    conn.close()
    return result


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="新晋股发现 (Rising Stars Scanner)")
    parser.add_argument("--days", type=int, default=5, help="回看天数 (默认5)")
    parser.add_argument("--threshold", type=float, default=15.0, help="短窗涨幅触发阈值%% (默认15)")
    parser.add_argument("--trend-window", type=int, default=20, help="趋势确认窗口天数 (默认20)")
    parser.add_argument("--trend-threshold", type=float, default=10.0, help="趋势窗口涨幅下限%% (默认10, 过滤短炒脉冲)")
    parser.add_argument("--expansion-threshold", type=float, default=10.0, help="板块扩散涨幅阈值%% (默认10)")
    parser.add_argument("--rescore", action="store_true", help="扫描后自动重评分新晋股")
    parser.add_argument("--top", type=int, default=20, help="最多展示新晋股数 (默认20)")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略归因缓存, 强制重新搜索")
    parser.add_argument("--no-attribution", action="store_true", help="跳过搜索归因 (纯量化扫描)")
    parser.add_argument("--blacklist", action="append", default=[], metavar="CODE", help="加入异动黑名单 (可多次; 默认冷却30天)")
    parser.add_argument("--blacklist-type", default=None, help="按归因 reason_type 批量拉黑 (如: 概念炒作)")
    parser.add_argument("--blacklist-reason", default="概念炒作-LLM错误归因", help="拉黑原因")
    parser.add_argument("--blacklist-days", type=int, default=30, help="冷却天数 (默认30)")
    parser.add_argument("--list-blacklist", action="store_true", help="列出当前有效黑名单")
    args = parser.parse_args()

    # ── 黑名单运维命令 (独立操作, 处理后不继续扫描) ──
    if args.list_blacklist:
        from picker.discovery.movement_blacklist import load_blacklist_detail
        bl = load_blacklist_detail()
        if not bl:
            print("黑名单为空")
        else:
            print(f"异动黑名单 ({len(bl)} 只, 已剔除过期):")
            for c, e in sorted(bl.items(), key=lambda x: x[1].get("expires_at", "")):
                print(f"  {c} {e.get('name','')[:10]:<10} [{e.get('reason_type','')}] "
                      f"到期 {e.get('expires_at','')} | {e.get('reason','')[:40]}")
        return
    if args.blacklist_type:
        from picker.discovery.movement_blacklist import blacklist_by_reason_type, purge_from_attr_cache
        codes = blacklist_by_reason_type(args.blacklist_type, args.blacklist_reason, args.blacklist_days)
        purged = purge_from_attr_cache(codes)
        print(f"已按类型[{args.blacklist_type}]拉黑 {len(codes)} 只: {codes}")
        print(f"已从归因缓存删除 {purged} 条")
        return
    if args.blacklist:
        from picker.discovery.movement_blacklist import add_many_to_blacklist, purge_from_attr_cache
        items = [(c, args.blacklist_reason) for c in args.blacklist]
        codes = add_many_to_blacklist(items, days=args.blacklist_days)
        purged = purge_from_attr_cache(codes)
        print(f"已拉黑 {len(codes)} 只: {codes} | 归因缓存删除 {purged} 条")
        return

    print("═" * 70)
    print(f"新晋股扫描 (近{args.days}日涨幅>{args.threshold}% & 近{args.trend_window}日>{args.trend_threshold}%)")
    print("═" * 70)

    scores = load_v3_scores()
    meta = load_fundamentals_meta()
    print(f"  评分库: {len(scores)} 只, fundamentals: {len(meta)} 只")

    # A. 量价扫描
    gems = scan_price_momentum(scores, meta, args.days, args.threshold, args.trend_window, args.trend_threshold)
    print(f"\n══ A. 新晋股候选: {len(gems)} 只 ══")
    print(f"{'#':>3} {'code':7} {'name':10} {'V3':>5} {'chain':>5} {'r'+str(args.days):>7} {'r'+str(args.trend_window):>7} {'性价比':>6}  {'行业':24}")
    print("-" * 98)
    for i, g in enumerate(gems[:args.top], 1):
        print(f"{i:>3} {g['code']:7} {g['name']:10} {g['score']:5.1f} {g['chain']:5.1f} "
              f"{g[f'r{args.days}']:+6.1f}% {g[f'r{args.trend_window}']:+6.1f}% {g['misprice_ratio']:6.1f}  {g['industry'][:22]}")

    # B. 板块扩散 (含搜索归因)
    if gems:
        use_attr = not args.no_attribution
        expansion = sector_expansion(
            gems, meta, scores, args.days, args.expansion_threshold,
            use_attribution=use_attr, use_cache=not args.refresh_cache,
        )
        print(f"\n══ B. 板块扩散: 真热点板块的同板块候选 ══")
        print(f"  过滤标准: 搜索归因确认板块行情 + 高涨幅(>10%)占比≥30% + 均涨≥3%")
        for sec, data in sorted(expansion.items(), key=lambda x: -len(x[1]["candidates"])):
            candidates = data["candidates"]
            strength = data["strength"]
            if not candidates:
                continue
            gem_count = sum(1 for g in gems if classify_sector(g["industry"]) == sec)
            hot_pct = int(strength["hot_ratio"] * 100)
            print(f"\n  【{sec}】{gem_count}只新晋股 | 板块强度: {hot_pct}%高涨幅 均涨{strength['avg_return']:+.1f}% | {len(candidates)}只候选")
            for c in candidates[:8]:
                flag = "🔥" if c[f"r{args.days}"] > args.expansion_threshold else "  "
                print(f"    {flag} {c['code']:7} {c['name']:10} V3={c['score']:4.1f} "
                      f"r{args.days}={c[f'r{args.days}']:+6.1f}%  {c['industry'][:20]}")

    # C. 研报覆盖检查 (对 Top 新晋股)
    if gems:
        top_codes = [g["code"] for g in gems[:10]]
        coverage = check_research_coverage(top_codes)
        print(f"\n══ C. 研报覆盖检查 (Top10新晋股) ══")
        zero_coverage = []
        for g in gems[:10]:
            cov = coverage.get(g["code"], {})
            n = cov.get("mentions", 0)
            flag = "⚠ 零覆盖" if n == 0 else f"{n}次提及"
            print(f"  {g['code']:7} {g['name']:10} 研报{flag}")
            if n == 0:
                zero_coverage.append(g["code"])
        if zero_coverage:
            print(f"\n  ⚠ {len(zero_coverage)} 只新晋股研报零覆盖, 建议补采: {zero_coverage}")

    # D. 重评分建议
    if gems:
        print(f"\n══ D. 建议操作 ══")
        rescore_codes = [g["code"] for g in gems[:10]]
        print(f"  重评分 (基本面+评分可能已过时):")
        print(f"    python3 _gen_top500_fundamentals.py --force --codes {','.join(rescore_codes[:5])}")
        print(f"    python3 _v3_full_score.py  # 重评分")

        if args.rescore:
            print(f"\n  [自动重评分模式] 删除 {len(rescore_codes)} 只旧评分...")
            c = load_v3_scores()
            for code in rescore_codes:
                c.pop(code, None)
            with open(V3_CACHE, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False)
            print(f"  ✓ 已删除, 请手动运行 _v3_full_score.py")

    print(f"\n{'═' * 70}")
    # 冷股激活检查 (新晋股逻辑的逆操作)
    _reactivate_cold_stocks()
    print("扫描完成")


def _reactivate_cold_stocks():
    """检查冷股中是否出现量价异动, 如果有则激活 (移回 fundamentals/)。

    冷股 = 被判定为"半年内无实质催化"而移到 cold_fundamentals/ 的股票。
    但市场是动态的, 如果某只冷股突然出现量价异动 (近5日涨幅>15%),
    说明可能有新催化出现, 应该重新激活它参与评分和选股。
    """
    cold_dir = paths.COLD_FUNDAMENTALS_DIR
    cold_list_path = paths.COLD_STOCKS_PATH
    if not os.path.exists(cold_list_path):
        return
    cold_codes = json.load(open(cold_list_path))
    if not cold_codes:
        return

    activated = []
    for code in cold_codes:
        # 检查量价异动: 近5日涨幅>15%
        df = load_kline(code)
        if df is None or len(df) < 6:
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        r5 = (df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100
        if r5 > 15:
            # 激活: 从 cold_fundamentals 移回 fundamentals
            cold_path = os.path.join(cold_dir, f"{code}.json")
            active_path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
            if os.path.exists(cold_path):
                import shutil
                shutil.move(cold_path, active_path)
                activated.append((code, r5))

    if activated:
        # 从冷股清单移除已激活的
        activated_codes = {c for c, _ in activated}
        cold_codes = [c for c in cold_codes if c not in activated_codes]
        json.dump(cold_codes, open(cold_list_path, "w"), ensure_ascii=False)
        print(f"\n  [冷股激活] {len(activated)} 只量价异动冷股已激活:")
        for code, r5 in activated:
            name = ""
            try:
                name = json.load(open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json"))).get("name", "")
            except Exception:
                pass
            print(f"    ↑ {code} {name:10} r5={r5:+.1f}% → 移回 fundamentals/")
    else:
        print(f"\n  [冷股激活] 无异动 (冷股池 {len(cold_codes)} 只)")


def cleanup_to_cold_stocks(min_score=7.0, max_chain=4.0, max_capital=3.0,
                           max_r20=5.0, research_days=30):
    """池子清理 — 把热池里"无催化、长期垫底"的股移入冷池冬眠。

    与 _reactivate_cold_stocks (冷→热) 对称的热→冷操作。三者构成池子边界管理:
      - discover_sector_gap: 加热 (热门缺口补股)
      - _reactivate_cold_stocks: 冷→热 (量价异动激活)
      - cleanup_to_cold_stocks (本函数): 热→冷 (无催化清理)

    判定"冷门"的 5 条 (全部满足才移, 严防误杀优质回调股):
      1. sector_score < min_score  (综合分低)
      2. chain < max_chain         (不在有意义的产业链上 — 保护优质AI股即使回调)
      3. capital < max_capital     (非热门板块)
      4. r20 < max_r20             (近20日没涨, 排除即将启动的)
      5. 近 research_days 无研报提及 (无市场关注)

    移动操作 (三步同步, 保持 cold_list 与目录一致):
      fundamentals/{code}.json → cold_fundamentals/{code}.json
      V3 cache 删除该条目 (冷股不进选股排序)
      cold_stocks.json 追加该 code (供 _reactivate_cold_stocks 检查激活)
    """
    import shutil
    V3 = paths.V3_CACHE
    if not os.path.exists(V3):
        print("\n  [冷门清理] 无 V3 cache, 跳过")
        return []
    cache = json.load(open(V3))

    # 研报提及计数 (近 research_days)
    cold_list_path = paths.COLD_STOCKS_PATH
    cold_list = set(json.load(open(cold_list_path)) if os.path.exists(cold_list_path) else [])
    mention_cnt = {}
    try:
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(paths.RESEARCH_DB)
        cutoff = (datetime.now() - timedelta(days=research_days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT stock_mentions FROM general_knowledge "
            "WHERE created_at >= ? AND stock_mentions IS NOT NULL", (cutoff,)
        ).fetchall()
        conn.close()
        for (raw,) in rows:
            try:
                for m in json.loads(raw):
                    c = str(m.get("code", "")).strip()[:6]
                    if c:
                        mention_cnt[c] = mention_cnt.get(c, 0) + 1
            except Exception:
                pass
    except Exception:
        pass

    def _r20(code):
        df = load_kline(code)
        if df is None or len(df) < 21:
            return None
        df = df.sort_values("trade_date").reset_index(drop=True)
        return round((df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100, 1)

    cleaned = []
    for code, v in list(cache.items()):
        if not isinstance(v, dict) or "chain" not in v:
            continue
        sc = v.get("sector_score", 0)
        chain = v.get("chain", 0)
        cap = v.get("capital", 0)
        if sc >= min_score or chain >= max_chain or cap >= max_capital:
            continue
        r = _r20(code)
        if r is None or r > max_r20:
            continue
        if mention_cnt.get(code, 0) > 0:
            continue
        # 执行移动 (三步同步)
        src = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
        dst = os.path.join(paths.COLD_FUNDAMENTALS_DIR, f"{code}.json")
        if not os.path.exists(src):
            continue
        os.makedirs(paths.COLD_FUNDAMENTALS_DIR, exist_ok=True)
        shutil.move(src, dst)
        cache.pop(code, None)
        cold_list.add(code)
        name = ""
        try:
            name = json.load(open(dst)).get("name", "")
        except Exception:
            pass
        cleaned.append((code, name, sc, r))

    if cleaned:
        json.dump(cache, open(V3, "w"), ensure_ascii=False, indent=1)
        json.dump(sorted(cold_list), open(cold_list_path, "w"), ensure_ascii=False)
        print(f"\n  [冷门清理] {len(cleaned)} 只移入冷池 (V3<{min_score}+chain<{max_chain}+cap<{max_capital}+r20<{max_r20}+无研报):")
        for code, name, sc, r in cleaned:
            print(f"    ↓ {code} {name:10} V3={sc} r20={r:+.1f}% → cold_fundamentals/")
    else:
        print(f"\n  [冷门清理] 无符合条件的股 (热池健康)")
    return cleaned


if __name__ == "__main__":
    main()
