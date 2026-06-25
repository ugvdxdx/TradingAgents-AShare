#!/usr/bin/env python3
"""统一异动归因模块 (Surge/Attribution 合并)。

合并原两套语义重叠的异动归因:
  - scan_mispriced.attribute_stock        : 结构化分类 (REASON_TYPE/SECTOR_TAG/SUMMARY),
                                            单向(上涨), r5>15% 触发, 14天TTL,
                                            供板块扩散/世界知识/保送新晋股
  - v3_full_score._search_movement_driver : 一句话 driver, 双向(涨/跌), r20>25% 触发,
                                            7天TTL, 供 fundamentals 回流/tier price_confirmed

统一为: 双向归因 + 结构化分类 + 统一 schema + 单一缓存 (mispriced_attribution_cache.json)。
消除对大涨股的双重 web search, 节省 GLM Coding Plan 搜索额度。

依赖 (单向, 无循环):
  - picker.common.llm_client._llm_quick  : LLM 归因
  - picker.common.web_search._web_search : 联网搜索
  - picker.paths                         : UNIFIED_ATTR_CACHE / RESEARCH_DB / KLINE_CACHE_DIR

统一 schema:
  {reason_type, sector_tag, summary, is_sector_wide, direction, r20, r5, cached_date, name}

数据流:
  生产端 (研报更新阶段):
    - scan.sector_expansion → attribute_stock_unified (薄封装) 对 r5>15% 新晋股归因
    - run_daily_maintenance Step2.7 → precompute_pool_attribution 对 r20>25% 异动股批量归因
  消费端 (读缓存):
    - refresh_fundamentals → get_attribution_for_code 注入 fundamentals
    - chain_tiers → price_confirmed 读统一缓存字段
    - update_world_knowledge / data_io → 读字段 (schema 兼容, 无需改)
"""
import os
import json
import pickle
import sqlite3
from datetime import datetime

from picker import paths
from picker.common.llm_client import _llm_quick
from picker.common.web_search import _web_search

# ── 缓存与 TTL ──
ATTR_CACHE_PATH = paths.UNIFIED_ATTR_CACHE          # = mispriced_attribution_cache.json
ATTR_TTL_DAYS = 14                                   # 统一 TTL (世界知识/保送需较长窗口; 比 surge 7d 更省搜索)
SECTOR_WIDE_TYPES = ("板块供需", "政策催化")          # 板块行情类 (驱动板块扩散); 下跌类不归此

# ── 异动判定阈值 (从 v3_full_score 下沉; r5>15% 的新晋股由 scan 调薄封装覆盖) ──
SURGE_UP_THRESHOLD = 25.0      # 大涨异动 (r20 >= 此值)
SURGE_DOWN_THRESHOLD = -18.0   # 大跌异动 (r20 <= 此值)
SURGE_R5_CONFIRM = 5.0         # |r5| > 此值确认非单日脉冲

KLINE_CACHE_DIR = paths.KLINE_CACHE_DIR


ATTR_PROMPT_UNIFIED = """你是A股研究员。请判断这只股票近期{direction}的真实原因, 并归类。

股票: {name}({code}) 行业: {industry}
近20日涨幅: {r20}% (近5日{r5}%) 方向: {direction}

{context}

请严格按以下格式输出 (用|分隔, 不要换行):
REASON_TYPE|{reason_options}
SECTOR_TAG|最相关的1-2个细分赛道关键词(如:六氟化钨/MLCC粉体/TLVR电感/空芯光纤, 不要用大类如"化工"; 若无明显赛道填"无")
SUMMARY|30字内一句话原因"""

_UP_OPTIONS = "板块供需 或 个股事件 或 政策催化 或 概念炒作 或 未知"
_DOWN_OPTIONS = "基本面恶化 或 技术回调 或 特定风险 或 估值杀跌 或 未知"


# ══════════════════════════════════════════════════════════
# 缓存读写
# ══════════════════════════════════════════════════════════

def _load_attr_cache():
    if not os.path.exists(ATTR_CACHE_PATH):
        return {}
    try:
        with open(ATTR_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_attr_cache(cache):
    os.makedirs(os.path.dirname(ATTR_CACHE_PATH), exist_ok=True)
    with open(ATTR_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def _parse_attribution(result):
    """解析 LLM 结构化输出 → 归因字段 dict。"""
    parsed = {"reason_type": "未知", "sector_tag": "", "summary": "", "is_sector_wide": False}
    for line in (result or "").strip().split("\n"):
        line = line.strip()
        if line.startswith("REASON_TYPE|"):
            rt = line.split("|", 1)[1].strip()
            parsed["reason_type"] = rt
            parsed["is_sector_wide"] = rt in SECTOR_WIDE_TYPES
        elif line.startswith("SECTOR_TAG|"):
            tag = line.split("|", 1)[1].strip()
            if tag and tag != "无":
                parsed["sector_tag"] = tag
        elif line.startswith("SUMMARY|"):
            parsed["summary"] = line.split("|", 1)[1].strip()
    return parsed


# ══════════════════════════════════════════════════════════
# 研报上下文 / web search (归因输入)
# ══════════════════════════════════════════════════════════

def _get_research_context(code, name, cutoff_date=""):
    """从研报库取该股相关的板块观点, 作为归因上下文。

    cutoff_date 非空时仅查该日之前的研报 (回测防前视偏差)。
    复刻自 scan_mispriced (避免 import scan 造成循环)。
    """
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return ""
    conn = sqlite3.connect(db_path)
    mentions = []
    try:
        if cutoff_date:
            rows = conn.execute(
                "SELECT stock_mentions FROM general_knowledge "
                "WHERE stock_mentions IS NOT NULL AND created_at <= ? "
                "ORDER BY created_at DESC LIMIT 200",
                (cutoff_date + " 23:59:59",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT stock_mentions FROM general_knowledge WHERE stock_mentions IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 200").fetchall()
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
    finally:
        conn.close()
    if mentions:
        return f"研报提及该股 ({len(mentions)}次):\n" + "\n".join(f"- {m[:60]}" for m in mentions[:5])
    return ""


def _safe_web_search(query):
    """web search, 失败返回空串 (归因任务降级为纯研报/行业知识判断, 不阻断主流程)。"""
    try:
        return _web_search(query)
    except Exception as e:
        print(f"  [attribution web_search] 失败: {str(e)[:100]}", flush=True)
        return ""


# ══════════════════════════════════════════════════════════
# 量价计算 + 异动判定 (从 v3_full_score 下沉)
# ══════════════════════════════════════════════════════════

def _compute_r20(code):
    """近20日涨幅%, 无K线返回None。"""
    for suf in ("_SH.pkl", "_SZ.pkl"):
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suf}")
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                if len(df) >= 21:
                    return round((df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100, 1)
            except Exception:
                pass
    return None


def _compute_r5(code):
    """近5日涨幅%, 无K线返回None。"""
    for suf in ("_SH.pkl", "_SZ.pkl"):
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suf}")
        if os.path.exists(p):
            try:
                df = pickle.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                if len(df) >= 6:
                    return round((df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100, 1)
            except Exception:
                pass
    return None


def is_movement_surging(code):
    """异动判断: 涨跌不对称 + r5确认非单日脉冲。

    条件 (同时满足): 大涨 r20>=25% 或 大跌 r20<=-18%; 且 |r5|>=5%。
    Returns: (is_surging, r20, r5) or (False, r20_or_None, r5_or_None)
    """
    r20 = _compute_r20(code)
    if r20 is None:
        return False, None, None
    if not (r20 >= SURGE_UP_THRESHOLD or r20 <= SURGE_DOWN_THRESHOLD):
        return False, r20, None
    r5 = _compute_r5(code)
    if r5 is None or abs(r5) < SURGE_R5_CONFIRM:
        return False, r20, r5
    return True, r20, r5


# ══════════════════════════════════════════════════════════
# fundamentals 查找 (扫池/读 name/industry)
# ══════════════════════════════════════════════════════════

def _find_fundamental(code):
    """在主目录或冷股目录中查找 fundamentals JSON, 返回路径或 None。"""
    for d in (paths.FUNDAMENTALS_DIR, paths.COLD_FUNDAMENTALS_DIR):
        p = os.path.join(d, f"{code}.json")
        if os.path.exists(p):
            return p
    return None


def _get_stock_name(code):
    p = _find_fundamental(code)
    if not p:
        return ""
    try:
        return json.load(open(p)).get("name", "") or code
    except Exception:
        return code


def _get_industry(code):
    p = _find_fundamental(code)
    if not p:
        return ""
    try:
        return json.load(open(p)).get("business_overview", {}).get("industry", "") or ""
    except Exception:
        return ""


def _list_all_fundamental_codes():
    """列出主目录+冷股目录的全部 fundamentals 代码。"""
    codes = set()
    for d in (paths.FUNDAMENTALS_DIR, paths.COLD_FUNDAMENTALS_DIR):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".json"):
                    codes.add(f[:-5])
    return sorted(codes)


# ══════════════════════════════════════════════════════════
# 生产端: 单只归因
# ══════════════════════════════════════════════════════════

def attribute_stock_unified(code, name, r5, r20, industry, direction,
                            use_cache=True, cutoff_date=""):
    """对一只异动股做统一归因 (研报上下文 + web search + LLM 结构化分类)。

    合并 scan attribute_stock (结构化分类) + surge (双向)。带缓存, TTL=14天。

    Args:
        direction: "上涨" 或 "下跌"。
        cutoff_date: 非空时进入回测模式 — 跳过 web search (前视偏差), 仅用 cutoff 前研报
                     + 行业知识判断, 不读写实时缓存。

    Returns:
        统一 schema dict {reason_type, sector_tag, summary, is_sector_wide,
                          direction, r20, r5, cached_date, name, cached}
    """
    is_backtest = bool(cutoff_date)

    # 0. 读缓存 (仅实盘; 回测每次重新判断)
    if use_cache and not is_backtest:
        entry = _load_attr_cache().get(code)
        if entry and entry.get("cached_date"):
            try:
                age = (datetime.now() - datetime.strptime(entry["cached_date"], "%Y-%m-%d")).days
            except Exception:
                age = ATTR_TTL_DAYS + 1
            if age < ATTR_TTL_DAYS and entry.get("summary"):  # 空壳(summary空)=无效, 需重新归因
                entry["cached"] = True
                return entry

    # 1. 研报上下文 (回测按 cutoff_date 截断)
    research_ctx = _get_research_context(code, name, cutoff_date=cutoff_date)

    # 2. web search (双向; 回测跳过避免前视)
    context_parts = []
    if research_ctx:
        context_parts.append(research_ctx)
    if not is_backtest:
        query = f"{name} {code} {direction}原因 {datetime.now().strftime('%Y年%m月')}"
        search_text = _safe_web_search(query)
        if search_text and len(search_text) > 50:
            context_parts.append(f"网络搜索:\n{search_text[:1500]}")
    if context_parts:
        context = "\n\n".join(context_parts)
    elif is_backtest:
        context = (f"(回测模式: 无{cutoff_date}前的研报记录, 请基于行业知识判断该股"
                   f"近20日{direction}{r20:+.0f}%的原因)")
    else:
        context = "(无额外信息, 请基于行业知识判断)"

    # 3. LLM 归因 (结构化; 涨/跌给不同 reason 选项)
    prompt = ATTR_PROMPT_UNIFIED.format(
        name=name, code=code, industry=industry, direction=direction,
        r20=f"{r20:+.0f}", r5=f"{r5:+.0f}", context=context,
        reason_options=(_UP_OPTIONS if direction == "上涨" else _DOWN_OPTIONS),
    )
    parsed = _parse_attribution(_llm_quick(prompt))
    parsed["direction"] = direction
    parsed["r20"] = r20
    parsed["r5"] = r5
    parsed["cached_date"] = cutoff_date or datetime.now().strftime("%Y-%m-%d")
    parsed["name"] = name
    parsed["cached"] = False

    # 4. 写缓存 (仅实盘; 不存运行期 cached 标记)
    if use_cache and not is_backtest:
        cache = _load_attr_cache()
        cache[code] = {k: v for k, v in parsed.items() if k != "cached"}
        _save_attr_cache(cache)

    return parsed


# ══════════════════════════════════════════════════════════
# 生产端: 扫池预填 (每日维护 Step2.7)
# ══════════════════════════════════════════════════════════

def precompute_pool_attribution(max_searches=None, verbose=True):
    """维护步骤: 扫描全池异动股, 预填统一归因缓存。

    供每日维护 Step 2.7。异动条件: 涨跌不对称(r20>=25% 或 <=-18%) + |r5|>=5%。
    去重: 缓存有效(14d)+方向一致 → 跳过。预填后, refresh_fundamentals/chain_tiers 直接读缓存。
    """
    if max_searches is None:
        max_searches = int(os.environ.get("ATTRIBUTION_MAX_SEARCHES", "30"))

    pool_codes = _list_all_fundamental_codes()
    cache = _load_attr_cache()
    from picker.discovery.movement_blacklist import is_blacklisted  # 局部 import 避免循环依赖

    # 1. 扫描异动股 (按异动幅度降序)
    surging = []
    for code in pool_codes:
        if is_blacklisted(code):
            continue  # 异动黑名单 (冷却中): 不预填归因, 缓存不会被重新写入
        is_s, r20, r5 = is_movement_surging(code)
        if is_s:
            surging.append((code, r20, r5))
    surging.sort(key=lambda x: -abs(x[1]))

    # 2. 预填: 跳过已缓存有效(有summary)+方向一致的 (空壳=无效, 需重新归因)
    searched = skipped = 0
    for code, r20, r5 in surging:
        direction = "上涨" if r20 > 0 else "下跌"
        entry = cache.get(code)
        if entry:
            try:
                cd = entry.get("cached_date") or entry.get("date", "")
                age = (datetime.now() - datetime.strptime(cd[:10], "%Y-%m-%d")).days
                if age <= ATTR_TTL_DAYS and entry.get("direction") == direction and entry.get("summary"):
                    skipped += 1
                    continue
            except Exception:
                pass
        if searched >= max_searches:
            break
        name = _get_stock_name(code)
        industry = _get_industry(code)
        attribute_stock_unified(code, name or code, r5, r20, industry, direction, use_cache=True)
        cache = _load_attr_cache()  # attribute_stock_unified 已写盘, 重载拿最新
        searched += 1
        if verbose:
            new_e = cache.get(code, {})
            print(f"    {code} {(name or code)[:8]:<8} r20={r20:+.0f}% [{direction}] → "
                  f"{new_e.get('summary', '')[:40]}", flush=True)

    if verbose:
        print(f"  [异动归因] 池内异动 {len(surging)} 只 | 新搜 {searched} | 缓存命中跳过 {skipped}", flush=True)
    return {"surging": len(surging), "searched": searched, "skipped": skipped}


# ══════════════════════════════════════════════════════════
# 消费端: 读缓存 / 渲染注入段
# ══════════════════════════════════════════════════════════

def get_attribution_for_code(code):
    """读取该股的归因结论 (供 fundamentals 生成/刷新链路消费)。TTL=14天。

    替代 v3.get_surge_driver_for_code。兼容老 surge 缓存的 date/driver 字段。
    Returns: 统一 schema dict, 无/过期返回 None。
    """
    entry = _load_attr_cache().get(code)
    if not entry:
        return None
    # summary (统一) 或 driver (老 surge) 任一存在即可
    summary = entry.get("summary") or entry.get("driver", "")
    if not summary:
        return None
    cd = entry.get("cached_date") or entry.get("date", "")
    try:
        age = (datetime.now() - datetime.strptime(cd[:10], "%Y-%m-%d")).days
    except Exception:
        return None
    if age > ATTR_TTL_DAYS:
        return None
    # 归一化老 surge entry (driver→summary, 缺字段补默认)
    entry["summary"] = summary
    entry.setdefault("direction", "上涨")
    entry.setdefault("reason_type", "未知")
    entry.setdefault("sector_tag", "")
    entry.setdefault("is_sector_wide", False)
    return entry


def build_attribution_section(attr):
    """渲染归因结论为 fundamentals 生成 prompt 的注入段。

    替代 v3.build_surge_fundamentals_section。用结构化字段(summary/sector_tag/reason_type)
    指示 LLM 写入 fundamentals 的 what_they_do/growth_drivers/strengths。
    """
    if not attr:
        return ""
    summary = attr.get("summary", "")
    if not summary:
        return ""
    direction = attr.get("direction", "上涨")
    reason_type = attr.get("reason_type", "")
    sector_tag = attr.get("sector_tag", "")
    parts = [f"该股近期有明显异动({direction})"]
    if reason_type and reason_type != "未知":
        parts.append(f"归因类型: {reason_type}")
    if sector_tag:
        parts.append(f"细分赛道: {sector_tag}")
    parts.append(f"核心驱动: {summary}")
    return (
        "\n## ⚡ 近期异动分析结论（实时web search归因）\n"
        f"{', '.join(parts)}。\n"
        "**重要**: 请在 what_they_do、growth_drivers、strengths 中【充分反映】上述驱动信息。\n"
        "这是当前市场对该股的真实认知，即使旧文件或行业标签未充分体现，也必须写入。"
    )
