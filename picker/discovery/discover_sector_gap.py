#!/usr/bin/env python3
"""板块缺口驱动的股票发现 — 检测"研报热但候选池无覆盖"的细分主题, 主动找股入池。

场景:
  研报捕获到某细分主题利好(如"金刚石散热用于AI芯片"), 但候选池里没有相关股票 →
  capital 机制找不到池内股承接, 整条催化被浪费。本模块填补这个缺口。

与现有发现机制的分工 (三者互补, 覆盖不同盲区):
  - get_dark_horse_stocks: 研报【明确点名】的股 → 保送入池
  - scan_mispriced:        【已经放量上涨】的股 → 量价归因保送
  - discover_sector_gap:   研报【没点名】+【还没涨】但主题热 → 缺口发现 (本模块)

流程:
  1. 从近期 bullish sector_knowledge 提取细分主题词 (LLM, 非大类板块名)
  2. 查每个主题在候选池(fundamentals)的覆盖度 → 0/低覆盖 = 缺口主题
  3. 对缺口主题: MCP web_search_prime 搜 "{主题} A股 龙头" → LLM 抽取股票代码
  4. 校验代码存在(腾讯实时行情拿 name+mcap) → refresh_one 生成 fundamentals → V3 评分
  5. V3 达标(默认 sector_score>=8.0) → 写入 V3 cache 正式入池; 否则删除 fundamentals 文件

安全设计:
  - 用 web search 找股(真实公司), 不靠 LLM 回忆(防幻觉代码)
  - 发现的股走完整 V3 评分, 不是免费加分; 分低直接淘汰(不入池)
  - 只对"热但缺覆盖"的主题触发, 不盲目扩池

用法:
  python3 picker/discovery/discover_sector_gap.py                  # 跑缺口发现
  python3 picker/discovery/discover_sector_gap.py --dry-run        # 只列缺口主题+候选, 不入池
  python3 picker/discovery/discover_sector_gap.py --theme 金刚石散热  # 指定主题跑
"""
import os, sys, json, sqlite3, time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths

FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR
COLD_DIR = paths.COLD_FUNDAMENTALS_DIR
V3_CACHE = paths.V3_CACHE
RESEARCH_DB = paths.RESEARCH_DB


# ══════════════════════════════════════════════════════════
# 1. 提取热点细分主题 (从近期 bullish sector_knowledge)
# ══════════════════════════════════════════════════════════

def _recent_bullish_viewpoints(days=20, limit=80):
    """取近 N 天 bullish 的 sector_knowledge 观点文本。

    days 默认 20 (≈14个交易日)。返回按时间倒序。
    """
    if not os.path.exists(RESEARCH_DB):
        return []
    conn = sqlite3.connect(RESEARCH_DB)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT sector, viewpoint, created_at FROM sector_knowledge "
        "WHERE sentiment='bullish' AND created_at >= ? AND viewpoint != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    conn.close()
    return [{"sector": r[0], "viewpoint": r[1], "date": (r[2] or "")[:10]} for r in rows]


def _theme_heat(theme, lookback_days=20, recent_days=5):
    """主题热度时效检查 — 区分"持续热度"与"已降温"。

    在 lookback_days (≈14交易日) 内查含 theme 的 bullish 观点, 拆分:
      - total:  回看窗内总提及数 (热度强度)
      - recent: 近 recent_days 内提及数 (是否仍持续)
      - latest_date: 最近一次提及日期
      - still_hot: 近 recent_days 内仍有提及 (未降温)

    为什么需要这个: 回看14交易日会发现"10天前热过、现已冷却"的主题,
    为已降温主题扩池会买在尾部。只对 still_hot 的主题发现股票。
    """
    if not os.path.exists(RESEARCH_DB) or not theme:
        return {"total": 0, "recent": 0, "latest_date": "", "still_hot": False}
    conn = sqlite3.connect(RESEARCH_DB)
    base = datetime.now()
    lookback_cutoff = (base - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    recent_cutoff = (base - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT created_at FROM sector_knowledge "
        "WHERE sentiment='bullish' AND created_at >= ? AND viewpoint LIKE ?",
        (lookback_cutoff, f"%{theme}%"),
    ).fetchall()
    conn.close()
    total = len(rows)
    recent = sum(1 for (ca,) in rows if (ca or "")[:10] >= recent_cutoff)
    latest = max(((ca or "")[:10] for (ca,) in rows), default="")
    return {"total": total, "recent": recent, "latest_date": latest,
            "still_hot": recent >= 1}


def extract_hot_themes(days=20, max_themes=8):
    """LLM 从近期 bullish 观点中提取具体的细分主题词。

    返回 [{theme, related_sector, evidence}] — theme 是具体的(如"金刚石散热""PCIe Retimer"),
    不是大类(如"AI电源")。days 默认 20 (≈14交易日)。

    用简化 prompt (字符串数组格式) — 复杂 object 格式易导致 LLM 照抄模板/返回空。
    """
    from picker.scoring.v3_full_score import _llm  # 带 429 退避, 比 _llm_quick 稳
    views = _recent_bullish_viewpoints(days=days)
    if not views:
        return []
    views_text = "\n".join(f"- [{v['sector']}] {v['viewpoint']}" for v in views[:25])

    prompt = f"""从研报观点提取{max_themes}个具体的细分主题词(产品/材料/技术级, 如金刚石散热/玻璃基板/PCIe Retimer, 不要大类板块名)。

研报观点:
{views_text}

第一行就直接输出JSON, 不要任何推理过程, 不要解释:
{{"themes":["主题1","主题2"]}}
"""
    raw = _llm(prompt)
    if not raw:
        return []
    import re
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    # themes 可能是 ["str",...] 或 [{"theme":...}]
    raw_themes = data.get("themes", [])[:max_themes]
    _PLACEHOLDER = {"主题词", "具体主题词", "大类板块", "主题A", "主题B", "主题C",
                    "主题1", "主题2", "主题3", "热度依据", "依据",
                    "related_sector", "theme", ""}
    out = []
    for t in raw_themes:
        if isinstance(t, str):
            theme = t.strip()
        elif isinstance(t, dict):
            theme = t.get("theme", "").strip()
        else:
            continue
        if theme in _PLACEHOLDER or len(theme) < 2:
            continue
        # 严格校验: theme 必须在源观点文本里真实出现 (防 LLM 限流/偷懒时照抄模板或编造)
        if theme not in views_text:
            continue
        out.append({"theme": theme, "related_sector": "", "evidence": ""})
    return out


# ══════════════════════════════════════════════════════════
# 2. 候选池覆盖度检查
# ══════════════════════════════════════════════════════════

_POOL_TEXT_INDEX = None  # {code: "name industry what_they_do"}


def _build_pool_text_index():
    """构建池内所有股的文本索引 (只读一次文件)。"""
    global _POOL_TEXT_INDEX
    if _POOL_TEXT_INDEX is not None:
        return _POOL_TEXT_INDEX
    idx = {}
    for d in [FUNDAMENTALS_DIR, COLD_DIR]:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            try:
                data = json.load(open(os.path.join(d, f)))
            except Exception:
                continue
            code = f[:-5]
            biz = data.get("business_overview", {}) or {}
            text = " ".join([
                data.get("name", ""),
                biz.get("industry", ""),
                biz.get("what_they_do", ""),
                biz.get("industry_position", ""),
            ])
            idx[code] = text
    _POOL_TEXT_INDEX = idx
    return idx


def count_pool_coverage(theme, min_len=2):
    """主题词在池内命中的股票数 (text 含 theme 即算)。"""
    if not theme or len(theme) < min_len:
        return 0, []
    idx = _build_pool_text_index()
    hits = [c for c, txt in idx.items() if theme in txt]
    return len(hits), hits


# ══════════════════════════════════════════════════════════
# 3. web search 找股 + LLM 抽取代码
# ══════════════════════════════════════════════════════════

def web_search_stocks(theme, num_candidates=6):
    """web search "{theme} A股龙头" → LLM 抽取 [{code, name}]。"""
    from picker.pipeline.refresh_fundamentals import _web_search
    from picker.scoring.v3_full_score import _llm  # 带 429 退避
    try:
        raw = _web_search(f"{theme} A股 龙头股 上市公司 代码", num_results=5)
    except Exception as e:
        print(f"  [web_search] {theme} 失败: {str(e)[:80]}", flush=True)
        return []
    if not raw or len(raw) < 50:
        return []
    prompt = f"""从下面搜索结果中提取与"{theme}"最相关的A股上市公司。

搜索结果:
{raw[:2500]}

要求:
- 只提取搜索结果中【真实出现】的公司, 不要编造, 不要照抄示例
- code 必须是搜索结果里出现的真实6位数字股票代码
- name 是公司简称
- 按相关度排序, 最多{num_candidates}个; 没有就返回空数组

只输出JSON, 格式如下(把方括号内容替换为真实值):
{{"stocks":[{{"code":代码,"name":名称,"why":依据}}]}}

示例(仅供参考格式, 不要照抄这些公司):
{{"stocks":[{{"code":"300861","name":"美畅股份","why":"电镀金刚线龙头"}}]}}
"""
    out = _llm(prompt)
    if not out:
        return []
    import re
    m = re.search(r'\{.*\}', out, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    # 严格校验: code 必须是 6 位纯数字 (过滤 LLM 照抄模板的脏值如"6位代码")
    code_re = re.compile(r'^\d{6}$')
    out2 = []
    for s in data.get("stocks", [])[:num_candidates]:
        code = str(s.get("code", "")).strip()
        if not code_re.match(code):
            continue
        out2.append({"code": code,
                     "name": str(s.get("name", "")).strip(),
                     "why": str(s.get("why", "")).strip()})
    return out2


def _validate_and_get_info(code):
    """用腾讯实时行情校验代码存在, 顺带拿 name + mcap_yi。返回 dict 或 None。"""
    try:
        from tradingagents.dataflows.providers.astock_provider import tencent_quote
        q = tencent_quote([code])
        info = q.get(code)
        if not info or not info.get("name"):
            return None
        return {"name": info["name"], "mcap_yi": info.get("mcap_yi", 0)}
    except Exception:
        return None


# ══════════════════════════════════════════════════════════
# 4. 生成 fundamentals + V3 评分 + 入池决策
# ══════════════════════════════════════════════════════════

def _generate_and_score(code, name, industry_hint, mcap_yi):
    """生成 fundamentals + V3 评分。返回 (fundamentals_data, v3_score_dict) 或 (None, None)。"""
    # refresh_one 自带 Web+Tushare+研报+LLM 重写并写入 fundamentals/; name/industry 经 hint 传入
    # (全新股票无现有 JSON, 靠 hint 兜底而非退化为 code/"未知")。
    # do_v3_rescore=False: V3 由下方 v3._call 显式触发并取回 score, 避免重复评分。
    from picker.pipeline.refresh_fundamentals import refresh_one
    data = refresh_one(code, name_hint=name, industry_hint=industry_hint,
                        mcap_yi=mcap_yi, do_v3_rescore=False)
    if not data:
        return None, None
    # V3 评分 (复用带 4 层失败防御的 _call)
    from picker.scoring import v3_full_score as v3
    _code, score, _dt = v3._call(code)
    return data, score


def discover(v3_threshold=8.0, days=20, max_themes=8, coverage_threshold=2,
             max_per_theme=5, dry_run=False, only_theme=None):
    """主入口: 检测缺口主题 → 找股 → 评分 → 入池。

    Args:
        v3_threshold: 入池 V3 sector_score 阈值 (低于则丢弃 fundamentals)
        coverage_threshold: 池覆盖 < 此值视为缺口主题
        max_per_theme: 每个缺口主题最多找/入池多少只
        dry_run: 只列缺口主题+候选股, 不生成不入池
        only_theme: 指定主题跑 (跳过主题提取)
    """
    print(f"{'═'*70}")
    print(f"  板块缺口驱动的股票发现")
    print(f"  回看{days}天(≈14交易日) | 覆盖<{coverage_threshold}为缺口 | 近5天无提及=已降温跳过")
    print(f"  V3>={v3_threshold} 入池")
    print(f"{'═'*70}")

    # ── 1. 主题 ──
    if only_theme:
        themes = [{"theme": only_theme, "related_sector": "", "evidence": "手动指定"}]
        skip_recency = True  # 手动指定主题不校验时效 (用户已确认)
    else:
        print("\n[1/5] 提取热点细分主题 (优先近5天仍活跃)...", flush=True)
        themes = extract_hot_themes(days=days, max_themes=max_themes)
        print(f"  提取到 {len(themes)} 个主题: {[t['theme'] for t in themes]}", flush=True)
        skip_recency = False

    if not themes:
        print("  无主题, 退出")
        return []

    # ── 2. 热度时效过滤 (剔除已降温主题) ──
    if not skip_recency:
        print(f"\n[2/5] 热度时效检查 (近5天无提及=已降温)...", flush=True)
        active = []
        for t in themes:
            heat = _theme_heat(t["theme"], lookback_days=days, recent_days=5)
            t["heat"] = heat
            if heat["still_hot"]:
                active.append(t)
                print(f"  🔥 [{t['theme']}] 回看{heat['total']}次 近5天{heat['recent']}次 (最近{heat['latest_date']}) — 持续热门", flush=True)
            else:
                print(f"  ○ [{t['theme']}] 回看{heat['total']}次 近5天0次 (最近{heat['latest_date']}) — 已降温, 跳过", flush=True)
        themes = active
        if not themes:
            print("\n  所有主题均已降温, 无持续热门主题。退出。")
            return []
    else:
        for t in themes:
            t["heat"] = {"still_hot": True, "total": -1, "recent": -1, "latest_date": ""}

    # ── 3. 缺口检测 ──
    print(f"\n[3/5] 检测池覆盖缺口 (阈值<{coverage_threshold})...", flush=True)
    gap_themes = []
    for t in themes:
        cnt, hits = count_pool_coverage(t["theme"])
        t["pool_count"] = cnt
        if cnt < coverage_threshold:
            gap_themes.append(t)
            print(f"  ✗ 缺口 [{t['theme']}] 池内仅 {cnt} 只 — {t.get('evidence','')}", flush=True)
        else:
            print(f"  ✓ 已覆盖 [{t['theme']}] 池内 {cnt} 只", flush=True)

    if not gap_themes:
        print("\n  无缺口主题, 候选池覆盖良好。退出。")
        return []

    # 写 gap themes 缓存 (供 tier 更新 Step 2.6 读取 — 三信号融合)
    try:
        gap_cache_path = os.path.join(paths.DATA_DIR, "caches", "gap_themes_cache.json")
        gap_cache = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "themes": [{"theme": t["theme"], "pool_count": t.get("pool_count", 0),
                        "heat": t.get("heat", {})} for t in gap_themes],
        }
        json.dump(gap_cache, open(gap_cache_path, "w"), ensure_ascii=False, indent=1)
    except Exception:
        pass

    # ── 4. web search 找股 ──
    print(f"\n[4/5] 为 {len(gap_themes)} 个缺口主题找候选股 (web search)...", flush=True)
    all_candidates = []  # [(theme, {code,name,why,mcap})]
    seen_codes = set()
    for t in gap_themes:
        stocks = web_search_stocks(t["theme"], num_candidates=max_per_theme)
        print(f"  [{t['theme']}] 找到 {len(stocks)} 只: {[(s['code'],s['name']) for s in stocks]}", flush=True)
        for s in stocks:
            code = s["code"]
            if code in seen_codes:
                continue
            # 跳过已在池内的
            if os.path.exists(os.path.join(FUNDAMENTALS_DIR, f"{code}.json")) or \
               os.path.exists(os.path.join(COLD_DIR, f"{code}.json")):
                continue
            seen_codes.add(code)
            info = _validate_and_get_info(code)
            if not info:
                print(f"    {code} {s['name']} 代码校验失败/不存在, 跳过", flush=True)
                continue
            s["mcap_yi"] = info["mcap_yi"]
            s["name"] = info["name"]  # 用行情返回的权威名称
            s["theme"] = t["theme"]
            all_candidates.append(s)

    if not all_candidates:
        print("\n  无有效候选股。退出。")
        return []

    print(f"\n  去重+校验后 {len(all_candidates)} 只有效候选", flush=True)

    if dry_run:
        print(f"\n[dry-run] 候选股 (不生成不入池):")
        for s in all_candidates:
            print(f"  {s['code']} {s['name']:<10} ({s['mcap_yi']:.0f}亿) [{s['theme']}] {s.get('why','')}")
        return all_candidates

    # ── 5. 生成 + 评分 + 入池决策 ──
    print(f"\n[5/5] 生成 fundamentals + V3 评分 (阈值>={v3_threshold})...", flush=True)
    admitted = []
    rejected = []
    cache = json.load(open(V3_CACHE)) if os.path.exists(V3_CACHE) else {}

    for s in all_candidates:
        code, name = s["code"], s["name"]
        t0 = time.time()
        data, score = _generate_and_score(code, name, s["theme"], s["mcap_yi"])
        dt = time.time() - t0
        if not score:
            print(f"  {code} {name:<10} 评分失败 ({dt:.0f}s), 删除 fundamentals", flush=True)
            try: os.remove(os.path.join(FUNDAMENTALS_DIR, f"{code}.json"))
            except: pass
            rejected.append((s, "score_fail"))
            continue
        sc = score.get("sector_score", 0)
        if sc >= v3_threshold:
            cache[code] = score
            admitted.append((s, score))
            print(f"  ✓ {code} {name:<10} V3={sc} [{score['chain']}+{score['surge']}+{score['capital']}] "
                  f"({dt:.0f}s) 入池", flush=True)
        else:
            try: os.remove(os.path.join(FUNDAMENTALS_DIR, f"{code}.json"))
            except: pass
            rejected.append((s, f"low_score_{sc}"))
            print(f"  ✗ {code} {name:<10} V3={sc} ({dt:.0f}s) <{v3_threshold}, 丢弃", flush=True)

    # 写回 V3 cache (加入达标的)
    if admitted:
        json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)

    # 汇总
    print(f"\n{'═'*70}")
    print(f"  完成: 入池 {len(admitted)} 只 | 丢弃 {len(rejected)} 只")
    print(f"{'═'*70}")
    for s, score in admitted:
        print(f"  ✓ {s['code']} {s['name']:<10} V3={score['sector_score']} [{s['theme']}] "
              f"| {score['essence'].get('core_catalyst','')[:30]}")
    return admitted


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=20, help="回看天数 (≈14交易日, 默认20)")
    p.add_argument("--threshold", type=float, default=8.0, help="V3 sector_score 入池阈值")
    p.add_argument("--coverage", type=int, default=2, help="池覆盖<此值视为缺口")
    p.add_argument("--max-themes", type=int, default=8)
    p.add_argument("--max-per-theme", type=int, default=5)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--theme", type=str, default=None, help="指定主题跑(跳过提取)")
    args = p.parse_args()
    discover(v3_threshold=args.threshold, days=args.days, coverage_threshold=args.coverage,
             max_themes=args.max_themes, max_per_theme=args.max_per_theme,
             dry_run=args.dry_run, only_theme=args.theme)


if __name__ == "__main__":
    main()
