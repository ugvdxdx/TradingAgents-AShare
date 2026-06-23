#!/usr/bin/env python3
"""研报触发式 fundamentals 彻底重写 (替代旧增量追加)。

核心设计：研报提及 → Web Search + Tushare + 研报全文 → LLM 完整重写 JSON（覆盖）
不再做追列式的增量追加，旧信息自然淘汰。

更新链路：
  refresh_one(code) → Web Search → Tushare 财报 → 研报提及 → LLM 重写 fundamentals JSON
                    → trigger_v3_rescore(code) → 更新 V3_CACHE

用法:
  cd /path/to/J-TradingAgents
  uv run python3 refresh_fundamentals.py                        # 对近期有研报提及的个股批量刷新
  uv run python3 refresh_fundamentals.py --stock 300308         # 只刷新指定个股
  uv run python3 refresh_fundamentals.py --stock 300308 --no-v3 # 只刷新 fundamentals, 不触发 V3 重评
  uv run python3 refresh_fundamentals.py --days 7               # 只处理近7天有研报的个股
  uv run python3 refresh_fundamentals.py --dry-run              # 只看不写
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 并发安全: V3_CACHE 读-改-写 & LLM client 初始化的全局锁
_V3_LOCK = threading.Lock()
_LLM_LOCK = threading.Lock()

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker import paths


# ═══════════════════════════════════════════════════════════
# Web Search (复用 scan_mispriced 的实现)
# ═══════════════════════════════════════════════════════════

def _is_rate_limited(e: Exception) -> bool:
    """判断异常是否为 429 速率限制。

    覆盖: urllib HTTPError(code=429) / openai RateLimitError / 智谱 code 1302。
    """
    if getattr(e, "code", None) == 429:
        return True
    if type(e).__name__ == "RateLimitError":
        return True
    s = str(e)
    return "429" in s or "速率限制" in s or "1302" in s


def _rate_limit_wait(attempt: int) -> int:
    """429 退避秒数: 30→60→90→120 (封顶120)。"""
    return min(30 * (attempt + 1), 120)


class _TokenBucket:
    """全局令牌桶: 连续两个请求至少间隔 min_interval 秒, 串行化请求发起、平滑爆发。

    主动速率控制 (事前限流), 配合 _is_rate_limited 的 429 退避 (事后兜底)。
    持锁 sleep 确保严格间隔 —— 多线程同时请求时排队, 每隔 min_interval 放行一个。
    min_interval 由环境变量 RATE_LIMIT_INTERVAL 配置 (秒, 默认0.5≈2请求/秒)。
    """
    def __init__(self, min_interval: float = 0.5):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()


# 智谱 API 全局速率限制器 (跨 _call_llm / _web_search 共享, 一个 key 一个桶)
_ZHIPU_LIMITER = _TokenBucket(float(os.environ.get("RATE_LIMIT_INTERVAL", "0.5")))


def _web_search(query: str, num_results: int = 5) -> str:
    """智谱 web-search-pro 联网搜索，返回结果摘要文本。

    复用 TA_API_KEY（LLM 即智谱 GLM，同一 key），端点 open.bigmodel.cn（国内可达）。
    429 限速自动退避重试（最长等 120s）。
    网络故障必须报错暴露（不静默跳过、不用降级数据偷偷生成）。
    只有"连接成功但无结果"才视为正常返回空。
    """
    import urllib.request
    import uuid
    api_key = os.environ.get("TA_API_KEY", "")
    if not api_key:
        raise RuntimeError("Web Search 需要 TA_API_KEY (智谱)")

    url = "https://open.bigmodel.cn/api/paas/v4/tools"
    payload = json.dumps({
        "request_id": str(uuid.uuid4()),
        "tool": "web-search-pro",
        "stream": False,
        "messages": [{"role": "user", "content": query}],
    }).encode("utf-8")

    _ZHIPU_LIMITER.acquire()  # 主动速率控制 (事前限流, 平滑爆发)
    last_err = None
    for attempt in range(6):  # 429 退避最多 6 次
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={
                    "Authorization": api_key,  # 智谱 v4 直接传 api_key（无 Bearer 前缀）
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            # 提取 search_result 里的 title + content
            results = []
            for choice in body.get("choices", []):
                msg = choice.get("message", {})
                for tc in msg.get("tool_calls", []):
                    if tc.get("type") == "search_result":
                        for r in tc.get("search_result", []):
                            t = (r.get("title", "") or "").strip()
                            c = (r.get("content", "") or "").strip()
                            if t or c:
                                results.append(f"[{t}] {c}")
            if not results:
                return ""  # 连上了但无结果 → 正常
            return "\n".join(results)[:3000]
        except Exception as e:
            last_err = e
            if _is_rate_limited(e):
                wait = _rate_limit_wait(attempt)
                print(f"  [WebSearch] 429 限速, 等待{wait}s 后重试 ({attempt+1}/6)", flush=True)
                time.sleep(wait)
                continue
            # 非限速瞬时错误: 短重试 1 次
            if attempt < 1:
                time.sleep(1)
                continue
            break
    # 重试仍失败 → 报错，不返回空
    raise RuntimeError(
        f"Web Search 失败 (智谱 web-search-pro): {type(last_err).__name__}: {last_err}"
    ) from last_err


# ═══════════════════════════════════════════════════════════
# 研报提及提取
# ═══════════════════════════════════════════════════════════

def _get_stock_research_mentions(code: str, name: str, days: int = 90) -> list:
    """从 research.db 提取该股所有研报提及（个股 + 行业关联）。

    Returns:
        [{source, date, sentiment, reason, type (stock/sector)}]
    """
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    mentions = []

    # 1. 个股直接提及
    try:
        rows = conn.execute("""
            SELECT stock_mentions, summary, key_insights, created_at, info_type
            FROM general_knowledge
            WHERE stock_mentions IS NOT NULL AND stock_mentions != '[]'
            ORDER BY created_at DESC
            LIMIT 500
        """).fetchall()

        for row in rows:
            try:
                stock_list = json.loads(row['stock_mentions'])
            except Exception:
                continue
            for m in stock_list:
                m_code = str(m.get('code', '')).strip()
                m_name = str(m.get('name', ''))
                if m_code == code or name in m_name:
                    mentions.append({
                        'source': 'stock_mention',
                        'date': (row['created_at'] or '')[:10],
                        'sentiment': m.get('sentiment', 'neutral'),
                        'reason': m.get('reason', ''),
                        'info_type': row.get('info_type', ''),
                        'summary': (row.get('summary') or '')[:200],
                    })
    except Exception:
        pass

    # 2. 行业观点（通过 feed_id 关联）
    try:
        # 先找到提到该股的 feed_id
        feed_ids = set()
        rows = conn.execute("""
            SELECT feed_id, stock_mentions FROM general_knowledge
            WHERE stock_mentions IS NOT NULL AND stock_mentions != '[]'
            ORDER BY created_at DESC LIMIT 500
        """).fetchall()
        for row in rows:
            try:
                for m in json.loads(row['stock_mentions']):
                    if str(m.get('code', '')).strip() == code or name in str(m.get('name', '')):
                        feed_ids.add(row['feed_id'])
            except Exception:
                pass

        if feed_ids:
            placeholders = ','.join(['?'] * len(feed_ids))
            sector_rows = conn.execute(f"""
                SELECT sector, viewpoint, sentiment, logic_chain, key_data, created_at
                FROM sector_knowledge
                WHERE feed_id IN ({placeholders})
                ORDER BY created_at DESC
            """, list(feed_ids)).fetchall()

            for sr in sector_rows:
                mentions.append({
                    'source': 'sector_view',
                    'date': (sr['created_at'] or '')[:10],
                    'sentiment': sr.get('sentiment', 'neutral'),
                    'reason': f"[{sr['sector']}] {sr['viewpoint'][:80]}",
                    'info_type': 'sector',
                    'summary': '',
                })
    except Exception:
        pass

    conn.close()

    # 按日期倒序
    mentions.sort(key=lambda x: x.get('date', ''), reverse=True)
    return mentions


def _get_industry_research_text(code: str, name: str) -> str:
    """获取该股所在行业的研报摘要文本，用于注入 prompt。"""
    try:
        from tradingagents.research.consumer import get_industry_research_brief
        return get_industry_research_brief(f"{name} {code}") or ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════
# Prompt 构建（综合 Web + Tushare + 研报 + 世界知识）
# ═══════════════════════════════════════════════════════════

REFRESH_SYSTEM_PROMPT = """你是资深A股研究员，负责为一只股票生成完整、最新、数据驱动的基本面分析JSON。

你的输出将直接替换该股票当前的基本面文件（覆盖写），因此必须重新综合所有信源，而非在旧版本上增量追加。

核心要求：
1. 从Web搜索结果中提取最新的事实（订单/客户/产能/价格/政策），并标注信源
2. 财务数据必须来自权威财报（已提供），不得修改或推测
3. 行业趋势和竞争格局要结合世界知识
4. 严禁把"送样/测试/规划"写成已确定事实
5. 旧信息自然淘汰 — 过时的优势/风险/驱动不再写入

【信源可信度分级】
- [信源:高] = 公司公告/财报/券商深度研报/权威媒体 — 可作为硬事实
- [信源:中] = 行业媒体/产业数据库/券商晨会 — 可信但需交叉验证
- [信源:低] = 雪球/股吧/自媒体/博主观点 — 仅有参考价值，不得作为核心论据

供应链/客户/份额类强断言（含'一供/独家/锁定/份额XX%'等词）必须开头标注信源等级。
信源低且与高信源矛盾 → 删除。送样测试阶段写成"已锁定一供" → 删除。"""


def _build_refresh_prompt(code: str, name: str, industry: str,
                           existing_data: dict,
                           web_result: str,
                           real_financials: Optional[dict],
                           research_mentions: list,
                           world_knowledge: str) -> str:
    """构建彻底重写的 prompt，综合所有信源。"""

    # ── 现有数据摘要（供 LLM 参考，但不做增量追加） ──
    old_summary = ""
    if existing_data:
        bo = existing_data.get('business_overview', {})
        old_summary += f"旧文件行业: {bo.get('industry', '')} | {bo.get('what_they_do', '')[:150]}\n"
        ca = existing_data.get('competitive_analysis', {})
        old_summary += f"旧优势: {', '.join(ca.get('strengths', [])[:3])}\n"
        old_summary += f"旧劣势: {', '.join(ca.get('weaknesses', [])[:3])}\n"

    # ── Web 搜索结果 ──
    web_section = ""
    if web_result and len(web_result) > 50:
        web_section = f"""
## 网络搜索结果（最新动态，{datetime.now().strftime('%Y-%m-%d')}）
{web_result[:2000]}

注意：搜索结果可能包含自媒体/论坛等低可信度信源，请按信源分级规则处理。
"""

    # ── 权威财报 ──
    fin_section = ""
    if real_financials:
        ann = real_financials.get('_ann_period', '')
        fin_clean = {k: v for k, v in real_financials.items() if not k.startswith('_')}
        fin_json = json.dumps(fin_clean, ensure_ascii=False, indent=2)
        fin_section = f"""
## ⚠️ 权威财报数据（Tushare，财报期 {ann}）
以下数据【必须原样填入 financial_health.key_metrics】，不得修改：
```json
{fin_json}
```
"""

    # ── 研报提及 ──
    research_section = ""
    if research_mentions:
        # 去重 & 去噪音
        seen = set()
        unique = []
        noise_pattern = re.compile(
            r'^(涨停|跌停|涨超|跌超|涨逾|跌逾|大涨|大跌|冲高|回落|拉升|跳水|封板|开板)'
        )
        for m in research_mentions:
            reason = m.get('reason', '')[:80]
            if noise_pattern.match(reason) and len(reason) <= 15:
                continue
            key = reason[:40]
            if key not in seen:
                seen.add(key)
                unique.append(m)

        lines = []
        # 最近 45 天用 30 条
        recent = [m for m in unique if m.get('date', '') >= (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')]
        for m in recent[:15]:
            sentiment_icon = {'bullish': '📈', 'bearish': '📉', 'neutral': '➖'}.get(m.get('sentiment', ''), '')
            lines.append(f"- {m['date']} {sentiment_icon} [{m['source']}] {m['reason'][:80]}")
            if m.get('summary'):
                lines.append(f"  摘要: {m['summary'][:100]}")

        if lines:
            research_section = f"""## 研报知识（来自 research.db，近 45 天）
{chr(10).join(lines)}

注：研报来源为财经博主圈子，信源可信度：中。可参考其行业趋势/数据，
但不得据此虚构该股的份额/认证/订单等个股级强断言。"""

    # ── 世界知识 ──
    wk_text = (world_knowledge or "")[:4000]

    prompt = f"""请为以下股票重新生成完整的基本面分析 JSON（覆盖旧文件，非增量追加）。

## 股票信息
- 代码: {code}
- 名称: {name}
- 行业: {industry}

## 旧文件摘要（仅供参考，请根据最新信源重新判断）
{old_summary}
{web_section}{fin_section}{research_section}
## 当前世界知识（2026年6月）
{wk_text}

## 输出格式
严格输出完整 JSON（所有字段必填）：

```json
{{
  "code": "{code}",
  "name": "{name}",
  "fetch_date": "{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
  "market": "{"沪市" if code.startswith("6") else "深市"}",
  "business_overview": {{
    "what_they_do": "该公司真正的业务，核心产品/服务，主要客户，技术特点，含财务数据（营收/增速/占比）。200-400字",
    "industry": "细分行业",
    "industry_position": "行业地位，含市占率/排名/与竞争对手对比"
  }},
  "competitive_analysis": {{
    "strengths": ["5条具体优势，含数据支撑。供应链断言开头标[信源:高/中/低]"],
    "weaknesses": ["5条具体劣势，含数据"],
    "moat_level": "低/中/中高/高"
  }},
  "financial_health": {{
    "key_metrics": {{
      "revenue_yi": 0.0, "net_profit_yi": 0.0, "gross_margin_pct": 0.0,
      "net_margin_pct": 0.0, "roe_pct": 0.0, "debt_ratio_pct": 0.0,
      "rd_ratio_pct": 0.0, "rd_expense_yi": 0.0, "operating_cf_yi": 0.0,
      "cf_to_profit": 0.0
    }},
    "health_rating": "健康/一般/较差",
    "benchmark_ref": "行业基准",
    "highlights": ["4条财务亮点，含数据"],
    "risks": ["4条财务风险，含数据"]
  }},
  "growth_assessment": {{
    "growth_score": 0.0,
    "growth_drivers": ["5条增长驱动，结合Web搜索结果和世界知识"],
    "headwinds": ["5条增长阻力，含具体数据"]
  }},
  "geopolitical_assessment": {{
    "risks": ["4条地缘风险，引用世界知识数据"],
    "opportunities": ["4条地缘机会，引用世界知识数据"],
    "industry_momentum": ["3条行业趋势"]
  }},
  "summary": "200-300字总结，格式：<公司>是<定位>。<核心财务>。优势：①②③。风险：①②③。<展望>。"
}}
```

## 关键质量要求
1. **财务数据精确**：用上面提供的权威数据，不要推测
2. **Web搜索结果要审慎使用**：低信源信息不作为核心论据
3. **不重复旧文件的错误**：旧文件的分类/断言如有误，请在本次修正
4. **供应链断言防污染**：注意信源分级，送样/测试 ≠ 已锁定
5. **宁缺毋滥**：无法确认的强断言宁可删除

请直接输出 JSON，不要有其他文字。"""

    return prompt


# ═══════════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════════

def _get_llm():
    """获取 LLM client（懒加载，线程安全初始化）。"""
    if not hasattr(_get_llm, '_client'):
        with _LLM_LOCK:
            # double-checked: 拿到锁后再查一次，避免重复初始化
            if not hasattr(_get_llm, '_client'):
                from openai import OpenAI
                _get_llm._client = OpenAI(
                    api_key=os.environ.get("TA_API_KEY", ""),
                    base_url=os.environ.get("TA_BASE_URL", ""),
                )
                _get_llm._model = os.environ.get("TA_LLM_DEEP") or os.environ.get("TA_LLM_QUICK") or "deepseek-v4-pro"
    return _get_llm._client, _get_llm._model


def _call_llm(system_msg: str, user_msg: str, max_tokens: int = 8192) -> Optional[str]:
    """调用 LLM，返回文本响应。主动限速 + 429 退避重试。"""
    _ZHIPU_LIMITER.acquire()  # 主动速率控制 (事前限流, 平滑爆发)
    client, model = _get_llm()
    max_attempts = 6  # 429 退避最多 6 次
    for attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=max_tokens,
                timeout=180,
            )
            content = resp.choices[0].message.content or ""
            return content.strip()
        except Exception as e:
            if _is_rate_limited(e):
                wait = _rate_limit_wait(attempt)
                print(f"  [LLM] 429 限速, 等待{wait}s 后重试 ({attempt+1}/{max_attempts})", flush=True)
                time.sleep(wait)
                continue
            # 非限速错误: 短重试 2 次
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  [LLM] 调用失败: {type(e).__name__}: {e}")
            return None
    print(f"  [LLM] 429 退避 {max_attempts} 次仍限速, 放弃", flush=True)
    return None


def _parse_json(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON。"""
    if not text:
        return None
    # 提取 ```json ... ``` 块
    m = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if m:
        text = m.group(1)
    # 直接找最外层 {}
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        text = re.sub(r',\s*([}\]])', r'\1', m.group())
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


# ═══════════════════════════════════════════════════════════
# 核心函数：单股刷新 + V3 重评
# ═══════════════════════════════════════════════════════════

def _load_world_knowledge() -> str:
    """加载世界知识。"""
    path = paths.WORLD_KNOWLEDGE_MD
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return ""


def refresh_one(code: str, world_knowledge: str = "",
                do_web_search: bool = True,
                do_v3_rescore: bool = True) -> Optional[dict]:
    """对单只股票完整重新生成 fundamentals JSON（覆盖写）。

    步骤：
      1. 读取现有 fundamentals（提取 name/industry）
      2. Web Search 最新动态
      3. Tushare 拉取最新财报
      4. 从 research.db 拉取研报提及
      5. LLM 完整重写 JSON → 写入
      6. 触发 V3 评分

    Args:
        code: 6 位股票代码
        world_knowledge: 世界知识文本（为空则自动加载）
        do_web_search: 是否执行网络搜索
        do_v3_rescore: 是否在刷新后触发 V3 重评

    Returns:
        新的 fundamentals JSON dict，失败返回 None
    """
    # ── 1. 读取现有数据 ──
    fund_path = os.path.join(paths.FUNDAMENTALS_DIR, f"{code}.json")
    existing_data = {}
    name = ""
    industry = ""

    if os.path.exists(fund_path):
        try:
            with open(fund_path, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
            name = existing_data.get('name', '')
            # 旧数据可能 name==code (历史 bug 污染), 这种 name 不要传给 prompt (会误导LLM)
            if not name or name == code:
                name = ''
            industry = existing_data.get('business_overview', {}).get('industry', '')
        except Exception:
            pass

    if not name:
        # 尝试冷股池
        cold_path = os.path.join(paths.COLD_FUNDAMENTALS_DIR, f"{code}.json")
        if os.path.exists(cold_path):
            try:
                with open(cold_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
                name = existing_data.get('name', '')
                industry = existing_data.get('business_overview', {}).get('industry', '')
            except Exception:
                pass

    if not name:
        name = code
        industry = "未知"

    print(f"  [{code}] {name} ({industry})")

    # ── 1.5. 基础信息新鲜度检查: fetch_date <7天则跳过web search (信息没变, 省时间) ──
    # 异动分析web search不动(在v3评分层), 这里只管"基础信息"web search
    _FRESH_THRESHOLD_DAYS = 7
    if do_web_search and existing_data:
        fd = existing_data.get("fetch_date", "")[:10]  # YYYY-MM-DD
        if fd:
            try:
                from datetime import datetime as _dt
                age = (_dt.now() - _dt.strptime(fd, "%Y-%m-%d")).days
                if age < _FRESH_THRESHOLD_DAYS:
                    do_web_search = False
                    print(f"    基础信息新鲜 (<{_FRESH_THRESHOLD_DAYS}天, fetch_date={fd}), 跳过web search")
            except Exception:
                pass

    # ── 2. Web Search ──
    web_result = ""
    if do_web_search:
        query = f"{name} {code} 股票 最新消息 产品 订单 2026"
        web_result = _web_search(query)
        if web_result:
            print(f"    Web Search: {len(web_result)} 字符")
        else:
            print(f"    Web Search: 无结果")

    # ── 3. Tushare 财报 ──
    real_financials = None
    try:
        from picker.data.fundamentals_data import fetch_real_financials
        real_financials = fetch_real_financials(code)
        if real_financials:
            print(f"    Tushare: 营收{real_financials.get('revenue_yi')}亿 (财报期{real_financials.get('_ann_period','')})")
    except Exception as e:
        print(f"    Tushare: 拉取失败 ({type(e).__name__})")

    # ── 4. 研报提及 ──
    mentions = _get_stock_research_mentions(code, name)
    if mentions:
        print(f"    研报提及: {len(mentions)} 条")

    # ── 5. LLM 重写 ──
    if not world_knowledge:
        world_knowledge = _load_world_knowledge()

    prompt = _build_refresh_prompt(
        code, name, industry,
        existing_data,
        web_result,
        real_financials,
        mentions,
        world_knowledge,
    )

    print(f"    LLM 生成中... (prompt {len(prompt)} 字符)")
    response = _call_llm(REFRESH_SYSTEM_PROMPT, prompt, max_tokens=8192)
    if not response:
        print(f"    ✗ LLM 无响应")
        return None

    new_data = _parse_json(response)
    if not new_data:
        print(f"    ✗ JSON 解析失败")
        return None

    # 补全字段 — code/name 用权威覆盖 (非 setdefault)
    # 原因: LLM 偶尔回显 code 当 name; 且旧 fundamentals 若已被污染(name==code),
    # 上游 name=existing_data.get('name') 会把 code 传进 prompt 形成恶性循环。
    new_data['code'] = code
    # name 若为空或==code (历史污染), 查腾讯行情拿真名
    real_name = name
    if not real_name or real_name == code:
        try:
            from tradingagents.dataflows.providers.astock_provider import tencent_quote
            q = tencent_quote([code]).get(code, {})
            if q.get("name"):
                real_name = q["name"]
        except Exception:
            pass
    new_data['name'] = real_name or code
    new_data.setdefault('fetch_date', datetime.now().strftime('%Y-%m-%dT%H:%M:%S'))
    new_data.setdefault('market', "沪市" if code.startswith("6") else "深市")

    # 确保 financial_health.key_metrics 存在
    fh = new_data.setdefault('financial_health', {})
    km = fh.setdefault('key_metrics', {})
    for k in ["revenue_yi", "net_profit_yi", "gross_margin_pct", "net_margin_pct",
               "roe_pct", "debt_ratio_pct", "rd_ratio_pct", "rd_expense_yi",
               "operating_cf_yi", "cf_to_profit"]:
        km.setdefault(k, None)

    # 写入文件（覆盖）
    os.makedirs(paths.FUNDAMENTALS_DIR, exist_ok=True)
    with open(fund_path, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    print(f"    ✓ 已写入 fundamentals/")

    # ── 6. V3 重评 ──
    if do_v3_rescore:
        try:
            _trigger_v3_rescore(code, new_data)
            print(f"    ✓ V3 已重评")
        except Exception as e:
            print(f"    ⚠ V3 重评失败: {e}")

    return new_data


def _trigger_v3_rescore(code: str, fund_data: dict):
    """触发单只股票的 V3 评分更新（链式调用 v3_full_score）。"""
    from picker.scoring import v3_full_score as v3

    prompt = v3.get_chain_prompt() + "\n" + json.dumps(fund_data, ensure_ascii=False, indent=2)
    _ZHIPU_LIMITER.acquire()  # V3 重评也限速 (补全所有智谱调用点, 避免此点爆发连累全局)
    content = v3._llm(prompt)
    if not content:
        return

    result = v3._parse(content)
    if not result:
        return

    # 写入 V3_CACHE（全局锁 + 原子写：写 .tmp 再 rename，防多线程并发损坏文件）
    with _V3_LOCK:
        cache = {}
        if os.path.exists(v3.V3_CACHE):
            try:
                cache = json.load(open(v3.V3_CACHE))
            except Exception:
                cache = {}
        cache[code] = result
        tmp = v3.V3_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, v3.V3_CACHE)  # 原子替换


# ═══════════════════════════════════════════════════════════
# 批量刷新：基于研报提及
# ═══════════════════════════════════════════════════════════

def _get_stocks_with_recent_research(days: int = 3) -> List[Tuple[str, str]]:
    """从 research.db 提取近 N 天有研报提及的个股列表。

    Returns:
        [(code, name), ...] 去重列表
    """
    db_path = paths.RESEARCH_DB
    if not os.path.exists(db_path):
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    stocks = {}  # code → name
    try:
        rows = conn.execute("""
            SELECT stock_mentions, created_at
            FROM general_knowledge
            WHERE stock_mentions IS NOT NULL AND stock_mentions != '[]'
              AND created_at >= ?
            ORDER BY created_at DESC
        """, (cutoff,)).fetchall()

        for row in rows:
            try:
                for m in json.loads(row['stock_mentions']):
                    code = str(m.get('code', '')).strip()
                    name = str(m.get('name', ''))
                    if code and len(code) == 6:
                        stocks[code] = name or code
            except Exception:
                pass
    except Exception:
        pass
    finally:
        conn.close()

    return [(code, name) for code, name in stocks.items()]


def refresh_from_research(days: int = 3, dry_run: bool = False,
                          max_stocks: int = 0,
                          do_web_search: bool = True,
                          do_v3_rescore: bool = True,
                          workers: int = 1) -> dict:
    """对近期有研报提及的个股批量刷新 fundamentals。

    Args:
        workers: 并发线程数（LLM 为 IO 密集，线程池即可）。>1 时并行刷新。
                 V3_CACHE 写入已用全局锁+原子写保证并发安全。

    Returns:
        {updated: int, failed: int, stocks: [(code, name, success)]}
    """
    stocks = _get_stocks_with_recent_research(days)
    if not stocks:
        print(f"近 {days} 天无研报提及个股，跳过")
        return {'updated': 0, 'failed': 0, 'stocks': []}

    print(f"近 {days} 天研报提及个股: {len(stocks)} 只")
    if dry_run:
        for code, name in stocks[:max_stocks or len(stocks)]:
            print(f"  {code} {name}")
        print(f"\n[DRY-RUN] 共 {len(stocks)} 只，未写入")
        return {'updated': 0, 'failed': 0, 'stocks': [(c, n, False) for c, n in stocks]}

    todo = stocks[:max_stocks] if max_stocks > 0 else stocks
    world_knowledge = _load_world_knowledge()
    n_total = len(todo)

    if workers > 1:
        return _refresh_parallel(todo, world_knowledge, do_web_search, do_v3_rescore, workers)

    # ── 串行模式 ──
    updated = failed = 0
    results = []
    for i, (code, name) in enumerate(todo, 1):
        print(f"\n[{i}/{n_total}] {code} {name}")
        try:
            result = refresh_one(code, world_knowledge,
                                do_web_search=do_web_search,
                                do_v3_rescore=do_v3_rescore)
            if result:
                updated += 1
                results.append((code, name, True))
            else:
                failed += 1
                results.append((code, name, False))
        except Exception as e:
            failed += 1
            print(f"  ✗ 异常: {type(e).__name__}: {e}")
            results.append((code, name, False))
        if i < n_total:
            time.sleep(1)

    print(f"\n{'='*60}")
    print(f"刷新完成: 成功 {updated}, 失败 {failed}, 共 {n_total}")
    return {'updated': updated, 'failed': failed, 'stocks': results}


def _refresh_parallel(todo, world_knowledge, do_web_search, do_v3_rescore, workers):
    """多线程并行刷新。每只股票互相独立，V3_CACHE 写入靠全局锁串行化。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    n_total = len(todo)
    print(f"并行模式: {workers} 线程, 共 {n_total} 只\n")

    done = [0]
    updated = [0]
    failed = [0]
    results = []
    count_lock = threading.Lock()

    def _worker(code, name):
        try:
            result = refresh_one(code, world_knowledge,
                                do_web_search=do_web_search,
                                do_v3_rescore=do_v3_rescore)
            ok = bool(result)
        except Exception as e:
            print(f"  [{code}] ✗ 异常: {type(e).__name__}: {e}")
            ok = False
        with count_lock:
            done[0] += 1
            if ok:
                updated[0] += 1
            else:
                failed[0] += 1
            n = done[0]
            tag = "✓" if ok else "✗"
            # 里程碑输出: 每25只 / 失败 / 最后一只 (避免537只日志爆炸)
            if n % 25 == 0 or not ok or n == n_total:
                print(f"  >> [{n}/{n_total}] {code} {name} {tag}  (成功{updated[0]} 失败{failed[0]})", flush=True)
        return (code, name, ok)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_worker, code, name) for code, name in todo]
        for fut in as_completed(futures):
            results.append(fut.result())

    print(f"\n{'='*60}")
    print(f"刷新完成: 成功 {updated[0]}, 失败 {failed[0]}, 共 {n_total}")
    return {'updated': updated[0], 'failed': failed[0], 'stocks': results}


def refresh_all(workers: int = 5, do_web_search: bool = True,
                do_v3_rescore: bool = True, skip_recent_hours: int = 0) -> dict:
    """全量重写所有热股 fundamentals (~537只)。

    新逻辑上线后的一次性全量刷新。仅遍历 fundamentals/ 热股目录;
    冷股池(cold_fundamentals/)不碰, 保持冬眠 (激活时再单独刷新)。

    Args:
        skip_recent_hours: >0 时跳过 fetch_date 在最近N小时内的 (避免重复刷新刚跑完的)。
    """
    fund_dir = paths.FUNDAMENTALS_DIR
    cutoff = (datetime.now() - timedelta(hours=skip_recent_hours)
              if skip_recent_hours > 0 else None)
    todo = []
    skipped = 0
    for f in sorted(os.listdir(fund_dir)):
        if not f.endswith(".json"):
            continue
        code = f[:-5]
        try:
            data = json.load(open(os.path.join(fund_dir, f), encoding="utf-8"))
            name = data.get("name", code)
            if cutoff:
                fd = data.get("fetch_date", "")
                if fd:
                    try:
                        if datetime.fromisoformat(fd.split(".")[0]) > cutoff:
                            skipped += 1
                            continue
                    except Exception:
                        pass
        except Exception:
            name = code
        todo.append((code, name))
    msg = f"全量重写: {len(todo)} 只热股 (workers={workers})"
    if skipped:
        msg += f" [跳过{skipped}只近{skip_recent_hours}h已刷新]"
    print(msg, flush=True)
    if not todo:
        return {'updated': 0, 'failed': 0, 'stocks': []}
    world_knowledge = _load_world_knowledge()
    if workers > 1:
        return _refresh_parallel(todo, world_knowledge, do_web_search, do_v3_rescore, workers)
    updated = failed = 0
    results = []
    for i, (code, name) in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {code} {name}")
        try:
            r = refresh_one(code, world_knowledge, do_web_search, do_v3_rescore)
            if r:
                updated += 1
                results.append((code, name, True))
            else:
                failed += 1
                results.append((code, name, False))
        except Exception as e:
            failed += 1
            print(f"  ✗ {e}")
            results.append((code, name, False))
    print(f"\n{'='*60}\n全量完成: 成功 {updated}, 失败 {failed}, 共 {len(todo)}")
    return {'updated': updated, 'failed': failed, 'stocks': results}


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='研报触发式 fundamentals 彻底重写（覆盖，非增量追加）'
    )
    parser.add_argument('--stock', type=str, help='只刷新指定代码的个股 (如 300308)')
    parser.add_argument('--all', action='store_true', help='全量重写所有热股 (~537只)')
    parser.add_argument('--skip-recent-hours', type=int, default=0,
                        help='跳过最近N小时已刷新的 (避免重复, 配合 --all 断点续跑)')
    parser.add_argument('--days', type=int, default=3, help='近N天有研报提及才刷新 (默认3)')
    parser.add_argument('--max', type=int, default=0, help='最多刷新N只 (0=全部)')
    parser.add_argument('--no-web', action='store_true', help='跳过网络搜索')
    parser.add_argument('--no-v3', action='store_true', help='不触发 V3 重评')
    parser.add_argument('--dry-run', action='store_true', help='只看不写')
    parser.add_argument('--workers', '-w', type=int, default=1, help='并发线程数 (默认1串行, LLM为IO密集建议5)')
    args = parser.parse_args()

    if args.stock:
        # 单股模式
        wk = _load_world_knowledge()
        result = refresh_one(
            args.stock, wk,
            do_web_search=not args.no_web,
            do_v3_rescore=not args.no_v3,
        )
        if result:
            print(f"\n✓ {args.stock} 已刷新")
        else:
            print(f"\n✗ {args.stock} 刷新失败")
            sys.exit(1)
    elif args.all:
        # 全量模式
        refresh_all(
            workers=max(1, args.workers),
            do_web_search=not args.no_web,
            do_v3_rescore=not args.no_v3,
            skip_recent_hours=args.skip_recent_hours,
        )
    else:
        # 研报触发批量模式
        refresh_from_research(
            days=args.days,
            dry_run=args.dry_run,
            max_stocks=args.max,
            do_web_search=not args.no_web,
            do_v3_rescore=not args.no_v3,
            workers=max(1, args.workers),
        )


if __name__ == '__main__':
    main()
