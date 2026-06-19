#!/usr/bin/env python3
"""世界知识定期更新 + 精简脚本。

作为 run_daily_update.py 的 step 4，每日研报采集→提取→基本面更新后自动执行。
基于研报 sector_momentum + market_sentiment 的冷热信号，用 LLM 驱动：
  A. 宏观 _world_knowledge_2026_06.md 的精简/更新（中等力度，目标 ~300 行）
  B. 个股 world_knowledge.py 的热门个股字段更新（仅 hot/emerging 板块个股）

三原则：
  1. 对股价影响不大的内容删除
  2. 冷门板块精简到 2-3 条核心，足以判断大趋势
  3. 热门/新兴板块补充研报新信息（不随意发挥）

用法:
  python3 update_world_knowledge.py                 # 更新宏观 + 个股
  python3 update_world_knowledge.py --macro-only    # 仅宏观 .md
  python3 update_world_knowledge.py --stocks-only   # 仅个股
  python3 update_world_knowledge.py --init-slim     # 首次全量精简(仅宏观)
  python3 update_world_knowledge.py --dry-run       # 预览不写文件
"""
import os
import sys
import ast
import json
import time
import shutil
import argparse
from datetime import datetime, timedelta

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from tradingagents.research.consumer import (
    get_sector_momentum, get_market_sentiment, get_stock_research_signal,
)

from picker import paths

WK_MD_PATH = paths.WORLD_KNOWLEDGE_MD
# world_knowledge.py 源码 (原根目录, 迁移后位于 picker/knowledge/)
WK_PY_PATH = os.path.join(paths.PROJECT_ROOT, "picker", "knowledge", "world_knowledge.py")

# ══════════════════════════════════════════════════════════
# LLM 直连 (复用 _v3_full_score 同款, 带重试)
# ══════════════════════════════════════════════════════════
import threading

_CLIENT_LOCAL = threading.local()
_API_KEY = os.environ.get("TA_API_KEY") or ""
_BASE_URL = os.environ.get("TA_BASE_URL") or ""
_MODEL = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"


def _client():
    if not hasattr(_CLIENT_LOCAL, "c"):
        from openai import OpenAI
        _CLIENT_LOCAL.c = OpenAI(api_key=_API_KEY, base_url=_BASE_URL)
    return _CLIENT_LOCAL.c


def _llm(prompt: str, max_tokens: int = 4096) -> str:
    """调用 LLM, 带自动重试 (3 次)。"""
    last_err = None
    for attempt in range(3):
        try:
            resp = _client().chat.completions.create(
                model=_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max_tokens,
                timeout=180,
            )
            msg = resp.choices[0].message
            content = (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")
            if content:
                return content
            last_err = "empty content"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    print(f"    [LLM] 放弃: {last_err}")
    return ""


# ══════════════════════════════════════════════════════════
# Step A: 采集冷热信号
# ══════════════════════════════════════════════════════════

def collect_signals(days_momentum: int = 14, days_sentiment: int = 7) -> dict:
    """从研报库采集板块冷热信号 + 宏观事件素材。"""
    print("═" * 60)
    print("Step A: 采集研报冷热信号")
    print("═" * 60)

    momentum = get_sector_momentum(days=days_momentum)
    sentiment = get_market_sentiment(days=days_sentiment)

    hot = momentum.get("hot_sectors", [])
    cold = momentum.get("cold_sectors", [])
    emerging = momentum.get("emerging_sectors", [])

    print(f"  热门板块({len(hot)}): {[s['sector'] for s in hot[:8]]}")
    print(f"  冷门板块({len(cold)}): {[s['sector'] for s in cold[:8]]}")
    print(f"  新兴板块({len(emerging)}): {[s['sector'] for s in emerging[:5]]}")
    print(f"  市场情绪: {sentiment.get('sentiment', 'N/A')}")

    return {
        "hot_sectors": hot,
        "cold_sectors": cold,
        "emerging_sectors": emerging,
        "market_sentiment": sentiment,
    }


def collect_macro_events(days: int = 7) -> str:
    """从研报 market_overview / key_insights 提取最近 N 天宏观事件素材。"""
    import sqlite3
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return ""
    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT created_at, market_overview, key_insights FROM general_knowledge "
        "WHERE created_at >= ? AND market_overview IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 15",
        (since,),
    ).fetchall()
    conn.close()

    events = []
    for created_at, overview, insights_raw in rows:
        date = created_at[:10]
        if overview:
            events.append(f"[{date}] {overview[:200]}")
        if insights_raw:
            try:
                for ins in json.loads(insights_raw)[:3]:
                    events.append(f"[{date}] {str(ins)[:100]}")
            except Exception:
                pass
    # 去重
    seen = set()
    uniq = []
    for e in events:
        key = e[11:]
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return "\n".join(uniq[:20])


def collect_attribution_knowledge() -> str:
    """从新晋股归因缓存提取板块供需知识, 作为世界知识更新素材。

    只提取"板块供需/政策催化"类 (已通过网络搜索确认的真实逻辑),
    按细分赛道聚类去重, 输出结构化文本供 LLM 注入世界知识。
    """
    attr_path = paths.ATTR_CACHE
    if not os.path.exists(attr_path):
        return ""
    try:
        cache = json.load(open(attr_path, encoding="utf-8"))
    except Exception:
        return ""

    # 过滤: 只取板块供需类 + 14天内有效
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    sector_knowledge = {}  # {细分赛道: [归因摘要列表]}
    for code, entry in cache.items():
        if not entry.get("is_sector_wide"):
            continue
        cached_date = entry.get("cached_date", "")
        if cached_date < cutoff:
            continue  # 过期归因不注入
        tag = entry.get("sector_tag", "其他")
        summary = entry.get("summary", "")
        reason = entry.get("reason_type", "")
        if summary:
            sector_knowledge.setdefault(tag, []).append(f"[{reason}] {summary}")

    if not sector_knowledge:
        return ""

    lines = ["新晋股板块供需归因 (已通过网络搜索确认):"]
    for tag, summaries in sorted(sector_knowledge.items(), key=lambda x: -len(x[1])):
        # 去重
        seen = set()
        uniq = []
        for s in summaries:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        lines.append(f"\n【{tag}】({len(uniq)}只新晋股)")
        for s in uniq[:3]:
            lines.append(f"  - {s}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# Step B: 宏观 .md 精简/更新
# ══════════════════════════════════════════════════════════

MACRO_PROMPT = """你是A股投研的世界知识维护者。请基于最新研报信号和新晋股归因，更新并精简这份世界知识文档。

【最新研报冷热信号】
{signals}

【最近宏观事件素材】
{macro_events}

【新晋股板块供需归因 (已通过网络搜索确认的真实上涨逻辑)】
{attribution}

【当前世界知识全文】
{current_md}

【更新原则 (严格执行)】
1. 对股价影响不大的内容删除
2. 冷门板块({cold})：精简到2-3条核心，足以判断大趋势即可
3. 热门/新兴板块({hot})：补充研报中的新信息，但不要随意发挥，不要虚构数据
4. **新晋股归因中的板块供需逻辑必须补充进世界知识**——这些是已确认的市场热点(如六氟化钨供给缺口、AI铜箔涨价、MLCC用量爆发等)，若文档中没有则新增，已有则更新
5. **过期清理 (关键)**：
   - 检查文档中每条信息是否仍然有效。判断标准: 该板块/事件是否仍是市场热点？数据是否已过时？
   - 已不再被市场关注的板块(如归因缓存中已无新晋股触发、研报中也无提及)，其详细内容应精简或删除
   - 已结束的事件(如"伊朗战争进行中"应改为"已结束")必须更新现状
   - 过时的价格数据(如标注了具体价格但已大幅变化的)删除具体数字，保留趋势判断
   - 当前日期是 {today}，超过2个月未被研报提及且无新晋股归因的板块视为过期
6. 目标：总行数控制在约150行（当前{cur_lines}行）

【格式要求】
- 保持 ## 一级章节 + ### 子标题 + - 列表 的 Markdown 结构
- 冷门板块章节可合并到"其他重要趋势"
- 热门板块可新增独立章节
- 第1行保持标题 "# 2026年6月 世界知识缓存"
- 第3行更新时间改为 "{today}"
- 每条信息尽量标注时点或趋势状态，便于后续判断是否过期

直接输出完整的新版 Markdown，不要输出任何解释说明。"""


def update_macro_md(signals: dict, dry_run: bool = False, init_slim: bool = False) -> bool:
    """LLM 精简/更新宏观 .md 文件。"""
    print("\n" + "═" * 60)
    print(f"Step B: {'首次全量精简' if init_slim else '更新'}宏观世界知识 .md")
    print("═" * 60)

    with open(WK_MD_PATH, "r", encoding="utf-8") as f:
        current_md = f.read()
    cur_lines = current_md.count("\n")
    print(f"  当前: {cur_lines} 行")

    hot_names = ", ".join(s["sector"] for s in signals.get("hot_sectors", [])[:8])
    cold_names = ", ".join(s["sector"] for s in signals.get("cold_sectors", [])[:8])
    macro_events = collect_macro_events(days=7)
    attribution = collect_attribution_knowledge()
    today = datetime.now().strftime("%Y-%m-%d")

    if attribution:
        print(f"  归因知识: {len(attribution)} 字符 (板块供需类)")

    signals_text = (
        f"市场情绪: {signals['market_sentiment'].get('sentiment', 'N/A')}\n"
        f"热门板块: {hot_names}\n"
        f"新兴板块: {', '.join(s['sector'] for s in signals.get('emerging_sectors', [])[:5])}\n"
        f"冷门板块: {cold_names}\n"
        f"核心洞察: {'; '.join(signals['market_sentiment'].get('key_insights', [])[:3])}"
    )

    prompt = MACRO_PROMPT.format(
        signals=signals_text,
        macro_events=macro_events or "(无)",
        attribution=attribution or "(无新晋股归因)",
        current_md=current_md,
        cold=cold_names or "房地产/白酒/银行/钢铁/建材",
        hot=hot_names or "AI算力/光通信/半导体/创新药",
        cur_lines=cur_lines,
        today=today,
    )

    print("  调用 LLM 精简中...")
    new_md = _llm(prompt, max_tokens=8192)
    if not new_md:
        print("  ✗ LLM 返回空，跳过")
        return False

    new_md = new_md.strip()
    if new_md.startswith("```"):
        new_md = new_md.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    new_lines = new_md.count("\n") + 1
    print(f"  新版: {new_lines} 行 (精简 {cur_lines - new_lines} 行, {(cur_lines-new_lines)*100//cur_lines}%)")

    if new_lines > cur_lines:
        print(f"  ⚠ 新版比旧版更长({new_lines}>{cur_lines})，可能未充分精简")

    if dry_run:
        print("  [DRY-RUN] 未写入，前30行预览:")
        print("\n".join(new_md.split("\n")[:30]))
        return True

    # 备份 + 写入
    bak = WK_MD_PATH + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(WK_MD_PATH, bak)
    with open(WK_MD_PATH, "w", encoding="utf-8") as f:
        f.write(new_md + "\n")
    print(f"  ✓ 已写入 {WK_MD_PATH}")
    print(f"  ✓ 备份 {bak}")
    return True


# ══════════════════════════════════════════════════════════
# Step C: 个股 world_knowledge.py 更新 (仅热门板块)
# ══════════════════════════════════════════════════════════

STOCK_PROMPT = """你是A股个股研究员。请基于最新研报信号，更新这只股票的世界知识画像。

【股票】{name}({code}) 行业:{industry}

【最新研报信号】
{research_signal}

【当前画像】
 strengths(优势): {cur_strengths}
 weaknesses(劣势): {cur_weaknesses}
 growth_drivers(增长驱动): {cur_growth}
 headwinds(逆风): {cur_headwinds}

【要求】
1. 基于研报信号更新上述4个字段，保留仍有效的旧内容，补充研报中的新信息
2. 每个字段保持3-5条，简洁有数据支撑，不要虚构
3. 删除已过时的内容（如已结束的事件、过期的财报数据）

请直接输出更新后的4个字段，每个字段一行，用 | 分隔多条。格式严格如下（不要输出分析过程）：
STRENGTHS|条目1|条目2|条目3
WEAKNESSES|条目1|条目2
GROWTH|条目1|条目2|条目3
HEADWINDS|条目1|条目2"""


def _parse_wk_py() -> tuple:
    """AST 解析 world_knowledge.py，返回 (tree, dict_node, source)。
    返回 BUSINESS_WORLD_KNOWLEDGE 对应的 ast.Dict 节点。
    """
    with open(WK_PY_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "BUSINESS_WORLD_KNOWLEDGE":
                    if isinstance(node.value, ast.Dict):
                        return tree, node.value, source
    raise RuntimeError("未找到 BUSINESS_WORLD_KNOWLEDGE dict")


def _ast_dict_get(dct_node: ast.Dict, key: str):
    """从 ast.Dict 节点取指定 key 的 value 节点。"""
    for k, v in zip(dct_node.keys, dct_node.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _ast_str_list(node) -> list:
    """从 ast 节点提取字符串列表。"""
    if isinstance(node, ast.List):
        result = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant):
                result.append(elt.value)
        return result
    return []


def collect_hot_stocks(signals: dict, max_stocks: int = 15) -> list:
    """从热门板块中找研报提及最多的个股。"""
    import sqlite3
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return []

    hot_sectors = {s["sector"] for s in signals.get("hot_sectors", [])}
    emerging = {s["sector"] for s in signals.get("emerging_sectors", [])}
    target_sectors = hot_sectors | emerging
    if not target_sectors:
        return []

    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    # 从 sector_knowledge 找热门板块下的研报 feed_id，再 JOIN stock_mentions
    rows = conn.execute(
        "SELECT sector, feed_id FROM sector_knowledge "
        "WHERE sector IN (%s) AND created_at >= ?" % ",".join("?" * len(target_sectors)),
        (*target_sectors, since),
    ).fetchall()
    feed_ids = {r[1] for r in rows}
    if not feed_ids:
        conn.close()
        return []

    # 从 general_knowledge 的 stock_mentions 提取个股
    from collections import Counter
    stock_counter = Counter()
    placeholders = ",".join("?" * min(len(feed_ids), 200))
    fid_list = list(feed_ids)[:200]
    for (mentions_raw,) in conn.execute(
        f"SELECT stock_mentions FROM general_knowledge WHERE feed_id IN ({placeholders})",
        fid_list,
    ).fetchall():
        if not mentions_raw:
            continue
        try:
            for m in json.loads(mentions_raw):
                code = (m.get("code") or "").strip()
                name = (m.get("name") or "").strip()
                if code and len(code) == 6:
                    stock_counter[(code, name)] += 1
        except Exception:
            pass
    conn.close()

    return [{"code": c, "name": n, "mentions": cnt}
            for (c, n), cnt in stock_counter.most_common(max_stocks)]


def update_stocks(signals: dict, dry_run: bool = False) -> list:
    """更新 world_knowledge.py 中热门个股的字段。返回更新过的 code 列表。"""
    print("\n" + "═" * 60)
    print("Step C: 更新个股世界知识 (仅热门板块)")
    print("═" * 60)

    hot_stocks = collect_hot_stocks(signals, max_stocks=15)
    print(f"  热门板块提及个股: {len(hot_stocks)} 只")

    if not hot_stocks:
        print("  无热门个股，跳过")
        return []

    try:
        tree, wk_dict, source = _parse_wk_py()
    except Exception as e:
        print(f"  ✗ AST 解析失败: {e}")
        return []

    # 当前 wk 中已有的 code 集合
    existing_codes = set()
    for k in wk_dict.keys:
        if isinstance(k, ast.Constant):
            existing_codes.add(k.value)

    updated_codes = []
    new_source = source

    for stock in hot_stocks:
        code = stock["code"]
        name = stock["name"]
        if code not in existing_codes:
            print(f"  - {code} {name}: 不在 world_knowledge.py 中，跳过")
            continue

        # 取当前画像
        stock_node = _ast_dict_get(wk_dict, code)
        if not isinstance(stock_node, ast.Dict):
            continue
        cur_strengths = _ast_str_list(_ast_dict_get(stock_node, "strengths"))
        cur_weaknesses = _ast_str_list(_ast_dict_get(stock_node, "weaknesses"))
        cur_growth = _ast_str_list(_ast_dict_get(stock_node, "growth_drivers"))
        cur_headwinds = _ast_str_list(_ast_dict_get(stock_node, "headwinds"))
        industry = ""
        ind_node = _ast_dict_get(stock_node, "industry")
        if isinstance(ind_node, ast.Constant):
            industry = ind_node.value

        # 取研报信号
        signal = get_stock_research_signal(code, days=14)
        if not signal or signal.get("mention_count", 0) == 0:
            print(f"  - {code} {name}: 无近期研报信号，跳过")
            continue

        # LLM 更新
        prompt = STOCK_PROMPT.format(
            name=name, code=code, industry=industry or "未知",
            research_signal=json.dumps(signal, ensure_ascii=False, default=str)[:800],
            cur_strengths=cur_strengths, cur_weaknesses=cur_weaknesses,
            cur_growth=cur_growth, cur_headwinds=cur_headwinds,
        )
        result = _llm(prompt, max_tokens=1500)
        if not result:
            print(f"  ✗ {code} {name}: LLM 返回空")
            continue

        # 解析 (STRENGTHS|条目1|条目2 格式, 比 JSON 更抗 reasoning 干扰)
        result = result.strip()
        new_fields = _parse_stock_output(result)
        if not new_fields:
            print(f"  ✗ {code} {name}: 输出解析失败")
            print(f"    末尾: {repr(result[-120:])}")
            continue

        # 用字符串替换更新 (安全：定位 code 块内的字段)
        replaced = _replace_stock_fields(new_source, code, new_fields)
        if replaced:
            new_source = replaced
            updated_codes.append(code)
            print(f"  ✓ {code} {name}: 已更新 strengths={len(new_fields.get('strengths', []))} "
                  f"growth={len(new_fields.get('growth_drivers', []))}")
        else:
            print(f"  - {code} {name}: 字段替换未命中，跳过")

    if updated_codes and not dry_run:
        bak = WK_PY_PATH + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(WK_PY_PATH, bak)
        with open(WK_PY_PATH, "w", encoding="utf-8") as f:
            f.write(new_source)
        print(f"\n  ✓ 已写入 {WK_PY_PATH}，更新 {len(updated_codes)} 只")
        print(f"  ✓ 备份 {bak}")
        print(f"  ⚠ 以下个股 fundamentals JSON 需删除以触发重生成:")
        for c in updated_codes:
            print(f"      rm fundamentals/{c}.json")
    elif updated_codes and dry_run:
        print(f"\n  [DRY-RUN] 将更新 {len(updated_codes)} 只: {updated_codes}")

    return updated_codes


def _parse_stock_output(text: str) -> dict:
    """解析 LLM 输出, 提取4个字段。抗 reasoning 干扰。

    支持多种格式: STRENGTHS|... 管道格式, 或中文标题 + 编号列表。
    """
    import re
    fields = {}
    tag_map = {
        "STRENGTHS": "strengths",
        "WEAKNESSES": "weaknesses",
        "GROWTH": "growth_drivers",
        "HEADWINDS": "headwinds",
    }
    # 中文标题映射 (LLM 常用)
    cn_map = {
        "优势": "strengths", "strengths": "strengths",
        "劣势": "weaknesses", "weaknesses": "weaknesses",
        "增长驱动": "growth_drivers", "growth_drivers": "growth_drivers", "增长": "growth_drivers",
        "逆风": "headwinds", "headwinds": "headwinds", "风险": "headwinds",
    }

    lines = text.split("\n")
    current_tag = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 匹配管道格式: STRENGTHS|条目1|条目2
        m = re.match(r"^(STRENGTHS|WEAKNESSES|GROWTH|HEADWINDS)[\|：:\s]*(.+)", stripped, re.IGNORECASE)
        if m:
            current_tag = tag_map[m.group(1).upper()]
            rest = m.group(2)
            items = [s.strip().lstrip("0123456789.、）) ") for s in rest.split("|")]
            items = [s for s in items if s and len(s) > 3]
            if items:
                fields.setdefault(current_tag, []).extend(items[:5])
            continue

        # 匹配中文标题: **优势** / ### 优势 / 优势：等
        m = re.match(r"^[\*\#]*\s*[\*\[]*(优势|劣势|增长驱动|增长|逆风|风险|strengths|weaknesses|growth_drivers|headwinds)[\*\]\s]*[：:]", stripped, re.IGNORECASE)
        if m:
            key = m.group(1).lower().replace("**", "")
            current_tag = cn_map.get(key)
            continue

        # 匹配编号列表项 (属于当前 tag): "1. xxx" / "- xxx" / "* xxx"
        if current_tag:
            m = re.match(r"^[\*\-]\s+(.+)|^\d+[\.\)、]\s*(.+)", stripped)
            if m:
                item = (m.group(1) or m.group(2) or "").strip()
                # 去掉 markdown 加粗
                item = re.sub(r"^\*\*|\*\*$", "", item).strip()
                if item and len(item) > 3:
                    fields.setdefault(current_tag, []).append(item[:200])

    # 截断到 5 条
    for k in fields:
        fields[k] = fields[k][:5]

    if "strengths" in fields and fields["strengths"]:
        for k in ["weaknesses", "growth_drivers", "headwinds"]:
            fields.setdefault(k, [])
        return fields
    return None


def _replace_stock_fields(source: str, code: str, new_fields: dict) -> str:
    """安全替换 world_knowledge.py 中指定 code 的 4 个字段列表。

    用 AST 定位行范围后做字符串替换，避免正则误伤。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""

    # 找到目标 code 的 dict 节点行范围
    target_lines = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "BUSINESS_WORLD_KNOWLEDGE":
                    main_dict = node.value
                    if isinstance(main_dict, ast.Dict):
                        for k, v in zip(main_dict.keys, main_dict.values):
                            if isinstance(k, ast.Constant) and k.value == code:
                                target_lines = (v.lineno, v.end_lineno)
                                break
    if not target_lines:
        return ""

    lines = source.split("\n")
    block = "\n".join(lines[target_lines[0] - 1: target_lines[1]])

    # 逐字段替换 (strengths/weaknesses/growth_drivers/headwinds)
    field_map = {
        "strengths": new_fields.get("strengths"),
        "weaknesses": new_fields.get("weaknesses"),
        "growth_drivers": new_fields.get("growth_drivers"),
        "headwinds": new_fields.get("headwinds"),
    }
    import re
    changed = False
    for field, new_vals in field_map.items():
        if not new_vals or not isinstance(new_vals, list):
            continue
        # 构建 Python list 字符串 (保持缩进)
        items = ",\n".join(f'            "{v}"' for v in new_vals[:5])
        new_list = f"[\n{items},\n        ]"
        # 匹配 "field": [ ... ] (多行)
        pattern = rf'("{field}":\s*)\[.*?\]'
        new_block, n = re.subn(rf'("{field}":\s*)\[.*?\]', rf'\g<1>{new_list}', block, count=1, flags=re.DOTALL)
        if n > 0:
            block = new_block
            changed = True

    if not changed:
        return ""

    lines[target_lines[0] - 1: target_lines[1]] = block.split("\n")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="世界知识定期更新 + 精简")
    parser.add_argument("--macro-only", action="store_true", help="仅更新宏观 .md")
    parser.add_argument("--stocks-only", action="store_true", help="仅更新个股 world_knowledge.py")
    parser.add_argument("--init-slim", action="store_true", help="首次全量精简 (仅宏观)")
    parser.add_argument("--dry-run", action="store_true", help="预览不写文件")
    args = parser.parse_args()

    do_macro = not args.stocks_only
    do_stocks = not args.macro_only and not args.init_slim

    # Step A: 采集信号
    signals = collect_signals()

    # Step B: 宏观 .md
    if do_macro:
        update_macro_md(signals, dry_run=args.dry_run, init_slim=args.init_slim)

    # Step C: 个股
    if do_stocks:
        update_stocks(signals, dry_run=args.dry_run)

    print("\n" + "═" * 60)
    print("世界知识更新完成")
    print("═" * 60)


if __name__ == "__main__":
    main()
