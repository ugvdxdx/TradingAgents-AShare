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


# ══════════════════════════════════════════════════════════
# 网络搜索归因 (搜索异动股上涨原因, 判断个股事件 vs 板块行情)
# ══════════════════════════════════════════════════════════

def web_search(query: str, num_results: int = 5) -> str:
    """网络搜索, 返回结果摘要文本。用智谱 web-search-pro (jina.ai 已失效)。

    复用 refresh_fundamentals._web_search (带 429 限流退避)。
    失败抛 RuntimeError (不静默返回空, 避免用降级数据偷偷决策)。
    """
    try:
        from picker.pipeline.refresh_fundamentals import _web_search
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


def _llm_quick(prompt: str) -> str:
    """快速 LLM 调用 (复用 _v3_full_score 的 client)。"""
    import threading
    if not hasattr(_llm_quick, "_client"):
        from openai import OpenAI
        _llm_quick._client = OpenAI(
            api_key=os.environ.get("TA_API_KEY", ""),
            base_url=os.environ.get("TA_BASE_URL", ""),
        )
        _llm_quick._model = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"
    try:
        resp = _llm_quick._client.chat.completions.create(
            model=_llm_quick._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=300, timeout=60,
        )
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


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
    """对一只异动股做归因 (研报上下文 + LLM 判断, 可选网络搜索)。

    带缓存: 首次搜索后记录原因, 后续 N 天内直接读缓存, 不重复搜索。

    Args:
        cutoff_date: 回测截止日。非空时进入【回溯模式】:
            - 跳过网络搜索 (搜索返回的是当前信息, 有前视偏差)
            - 仅用 cutoff_date 前的研报上下文 + LLM 行业知识判断
            - 不读写实时归因缓存 (避免污染)

    Returns:
        {reason_type, sector_tag, summary, is_sector_wide, cached, cached_date}
    """
    is_backtest = bool(cutoff_date)

    # 0. 读缓存 (仅实盘; 回测模式不读缓存, 每次重新判断)
    if use_cache and not is_backtest:
        cache = _load_attr_cache()
        entry = cache.get(code)
        if entry and entry.get("cached_date"):
            cached_dt = datetime.strptime(entry["cached_date"], "%Y-%m-%d")
            age = (datetime.now() - cached_dt).days
            if age < ATTR_CACHE_TTL_DAYS:
                entry["cached"] = True
                return entry

    # 1. 研报上下文 (回测模式按 cutoff_date 截断)
    research_ctx = _get_research_context(code, name, cutoff_date=cutoff_date)

    # 2. 网络搜索 (回测模式跳过, 避免前视偏差)
    context_parts = []
    if research_ctx:
        context_parts.append(research_ctx)
    if not is_backtest:
        search_text = web_search(f"{name} {code} 股价上涨原因 {datetime.now().strftime('%Y年%m月')}")
        if search_text and len(search_text) > 50:
            context_parts.append(f"网络搜索:\n{search_text[:1500]}")
    context = "\n\n".join(context_parts) if context_parts else (
        f"(回测模式: 无{cutoff_date}前的研报记录, 请基于行业知识判断该股"
        f"近{days}日涨幅{return_pct:.0f}%的原因)" if is_backtest
        else "(无额外信息, 请基于行业知识判断)"
    )

    # 3. LLM 归因
    prompt = ATTR_PROMPT.format(
        name=name, code=code, industry=industry, days=days,
        return_pct=f"{return_pct:.0f}%", context=context,
    )
    result = _llm_quick(prompt)

    parsed = {"reason_type": "未知", "sector_tag": "", "summary": "", "is_sector_wide": False}
    for line in result.strip().split("\n"):
        line = line.strip()
        if line.startswith("REASON_TYPE|"):
            rt = line.split("|", 1)[1].strip()
            parsed["reason_type"] = rt
            parsed["is_sector_wide"] = rt in ("板块供需", "政策催化")
        elif line.startswith("SECTOR_TAG|"):
            parsed["sector_tag"] = line.split("|", 1)[1].strip()
        elif line.startswith("SUMMARY|"):
            parsed["summary"] = line.split("|", 1)[1].strip()

    parsed["cached"] = False
    parsed["cached_date"] = cutoff_date or datetime.now().strftime("%Y-%m-%d")
    parsed["name"] = name
    # 回测模式不写实时缓存 (避免污染); 实盘才写
    if use_cache and not is_backtest:
        cache = _load_attr_cache()
        cache[code] = parsed
        _save_attr_cache(cache)

    return parsed


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
    score_cutoff: float = 15.0,
) -> list:
    """扫描量价异动但评分偏低的标的。

    Args:
        days: 回看天数 (近 N 日涨幅)
        threshold: 涨幅触发阈值 (%)
        score_cutoff: V3 分低于此值才算"低估"

    Returns:
        新晋股列表, 按"涨幅/评分比"降序 (性价比最高的在前)
    """
    gems = []
    for code, v in scores.items():
        if not isinstance(v, dict) or "sector_score" not in v:
            continue
        score = v["sector_score"]
        if score >= score_cutoff:
            continue  # 分数够高, 不算新晋股

        df = load_kline(code)
        r = calc_returns(df, days)
        if r is None or r < threshold:
            continue

        name = meta.get(code, {}).get("name", "")
        industry = meta.get(code, {}).get("industry", "")
        gems.append({
            "code": code,
            "name": name,
            "score": score,
            "chain": v.get("chain", 0),
            "delivery": v.get("delivery", 0),
            "capital": v.get("capital", 0),
            f"r{days}": r,
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
        if score >= 15.0:
            continue
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
    parser.add_argument("--threshold", type=float, default=15.0, help="涨幅触发阈值%% (默认15)")
    parser.add_argument("--score-cutoff", type=float, default=15.0, help="V3分低于此值算低估 (默认15)")
    parser.add_argument("--expansion-threshold", type=float, default=10.0, help="板块扩散涨幅阈值%% (默认10)")
    parser.add_argument("--rescore", action="store_true", help="扫描后自动重评分新晋股")
    parser.add_argument("--top", type=int, default=20, help="最多展示新晋股数 (默认20)")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略归因缓存, 强制重新搜索")
    parser.add_argument("--no-attribution", action="store_true", help="跳过搜索归因 (纯量化扫描)")
    args = parser.parse_args()

    print("═" * 70)
    print(f"新晋股扫描 (近{args.days}日涨幅>{args.threshold}% & V3<{args.score_cutoff})")
    print("═" * 70)

    scores = load_v3_scores()
    meta = load_fundamentals_meta()
    print(f"  评分库: {len(scores)} 只, fundamentals: {len(meta)} 只")

    # A. 量价扫描
    gems = scan_price_momentum(scores, meta, args.days, args.threshold, args.score_cutoff)
    print(f"\n══ A. 新晋股候选: {len(gems)} 只 ══")
    print(f"{'#':>3} {'code':7} {'name':10} {'V3':>5} {'chain':>5} {'r'+str(args.days):>7} {'性价比':>6}  {'行业':24}")
    print("-" * 90)
    for i, g in enumerate(gems[:args.top], 1):
        print(f"{i:>3} {g['code']:7} {g['name']:10} {g['score']:5.1f} {g['chain']:5.1f} "
              f"{g[f'r{args.days}']:+6.1f}% {g['misprice_ratio']:6.1f}  {g['industry'][:22]}")

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
    # 更新细分赛道拆分表 (供 capital 模式D 使用)
    if gems:
        _update_sub_sector_override(gems, meta)
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


def _update_sub_sector_override(gems: list, meta: dict):
    """发现"大类热门但个股滞涨"的细分赛道时, 更新拆分表。

    逻辑: 从 V3 cache 中找"chain 高(基本面强) + 近20日跌幅大"的股票,
    如果它们被归入热门大类板块(如光通信/AI算力), 说明该细分赛道已降温,
    在拆分表中标记低 capital。

    与 _load_rising_stars 互补: 新晋股逻辑找"低分但涨得好"的,
    本函数找"高分但滞涨"的, 两者共同修正评分与市场的偏差。

    输出: .sub_sector_override.json (供 _v3_full_score.py 模式D 加载)
    """
    override_path = paths.SUB_SECTOR_OVERRIDE_PATH
    # 加载现有 (从默认值开始)
    try:
        from picker.scoring.v3_full_score import _SUB_SECTOR_OVERRIDE_DEFAULT
        override = dict(_SUB_SECTOR_OVERRIDE_DEFAULT)
    except Exception:
        override = {}
    if os.path.exists(override_path):
        try:
            saved = json.load(open(override_path))
            override.update(saved)  # 用户/之前保存的覆盖默认
        except Exception:
            pass

    # 获取热门大类板块
    from tradingagents.research.normalize import get_sector_keyword_index
    kw_index = get_sector_keyword_index()
    HOT_SECTORS = set()
    try:
        from tradingagents.research.consumer import get_sector_momentum
        momentum = get_sector_momentum(days=14)
        HOT_SECTORS = {s["sector"] for s in momentum.get("hot_sectors", [])}
    except Exception:
        pass
    if not HOT_SECTORS:
        print(f"  [拆分表] 无板块动量数据, 跳过")
        return

    # 从 V3 cache 找滞涨股: chain>=6 (基本面强) + r20<-5 (明显下跌)
    v3_path = paths.V3_CACHE
    if not os.path.exists(v3_path):
        return
    v3 = json.load(open(v3_path))

    def classify(industry):
        if not industry: return ""
        best, h = "", 0
        for sec, kws in kw_index.items():
            hits = sum(1 for k in kws if k in industry)
            if hits > h: h, best = hits, sec
        return best

    new_overrides = {}
    # 统计每个细分标签下的滞涨股数量 (避免个别股拖累整个标签)
    tag_laggard_count = {}
    tag_industries = {}  # tag → [industry样本] (调试用)
    for code, entry in v3.items():
        if not isinstance(entry, dict) or entry.get("chain", 0) < 6:
            continue
        industry = meta.get(code, {}).get("industry", "")
        if not industry:
            continue
        big_sector = classify(industry)
        if big_sector not in HOT_SECTORS:
            continue
        df = load_kline(code)
        if df is None or len(df) < 21:
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        r20 = (df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100
        if r20 > -5:
            continue
        # 提取细分标签
        tag = ""
        if "（" in industry:
            tag = industry.split("（")[1].split("）")[0][:8]
        elif "(" in industry:
            tag = industry.split("(")[1].split(")")[0][:8]
        if not tag or len(tag) < 2:
            tag = industry[:8].strip()
        # 过滤太泛的标签
        if tag in ("半导体", "通信设备", "电子元器件", "元器件", "化工", "印制电路板"):
            continue
        tag_laggard_count[tag] = tag_laggard_count.get(tag, 0) + 1
        tag_industries.setdefault(tag, []).append(industry[:20])

    # 只有≥3只滞涨股的标签才算群体性降温 (避免个股拖累)
    for tag, count in tag_laggard_count.items():
        if count >= 3 and tag not in override:
            new_overrides[tag] = 1.5
            override[tag] = 1.5

    if new_overrides:
        json.dump(override, open(override_path, "w"), ensure_ascii=False, indent=1)
        print(f"  [拆分表] 新增 {len(new_overrides)} 个降温细分赛道: {list(new_overrides.keys())}")
    else:
        print(f"  [拆分表] 无新增 (现有 {len(override)} 个)")


if __name__ == "__main__":
    main()
