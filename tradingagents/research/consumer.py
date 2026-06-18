"""研报知识消费层 — 统一封装 research.db 的消费逻辑。

解决研报知识"生产多、消费少"的问题:
  - stock_mentions (~2000条): 只取了 reason, 时间/情绪/频次全丢
  - sector_views (682条): 完全未消费
  - key_insights/risk_warnings: 完全未消费
  - research_catalysts/geopolitical: 写入 JSON 但辩论不读取

提供五个消费接口, 供 picker 各节点按需调用:
  ① get_stock_research_signal()   — 个股研报信号 (辩论+增量)
  ② get_sector_momentum()         — 行业研报动量 (分析师+轮动)
  ③ get_market_sentiment()        — 市场情绪 (分析师)
  ④ get_dark_horse_stocks()       — 研报黑马 (海选保送)
  ⑤ get_research_risk_signals()   — 研报风险 (海选排雷)
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════
# 行业名称归一化 (复用 normalize.py 共享模块, 单一真相源)
# ══════════════════════════════════════════════════════════
from .normalize import normalize_sector  # noqa: F401  (re-export for callers)


# ══════════════════════════════════════════════════════════
# DB 连接
# ══════════════════════════════════════════════════════════

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))),
    "research.db",
)


def _get_conn():
    """获取 research.db 连接。"""
    import sqlite3
    if not os.path.exists(_DB_PATH):
        return None
    return sqlite3.connect(_DB_PATH)


def _parse_json(val):
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except Exception:
        return []


def _is_bullish(sentiment: str) -> bool:
    """判断情绪是否为看多 (bullish + positive)。"""
    return sentiment in ("bullish", "positive")


def _is_bearish(sentiment: str) -> bool:
    """判断情绪是否为看空 (bearish + negative)。"""
    return sentiment in ("bearish", "negative")


# ══════════════════════════════════════════════════════════
# ① 个股研报信号
# ══════════════════════════════════════════════════════════

def get_stock_research_signal(
    code: str,
    cutoff_date: str = "",
    days: int = 30,
) -> Dict[str, Any]:
    """获取某只个股近 N 天的研报信号。

    返回:
      mention_count, bullish_count, bearish_count,
      latest_bullish, top_reasons, sentiment_trend,
      research_catalysts (从 fundamentals JSON 读取)
    """
    conn = _get_conn()
    if not conn:
        return {}

    try:
        # 计算时间窗口
        if cutoff_date:
            base = datetime.strptime(cutoff_date, "%Y-%m-%d")
        else:
            # 取 DB 中最新日期
            row = conn.execute(
                "SELECT MAX(created_at) FROM general_knowledge"
            ).fetchone()
            base = datetime.strptime(row[0][:10], "%Y-%m-%d") if row and row[0] else datetime.now()
        since = (base - timedelta(days=days)).strftime("%Y-%m-%d")

        # 查询所有包含该 code 的帖子
        rows = conn.execute(
            "SELECT stock_mentions, created_at FROM general_knowledge "
            "WHERE created_at >= ? AND stock_mentions IS NOT NULL "
            "ORDER BY created_at DESC",
            (since,),
        ).fetchall()

        bullish_reasons = []
        bearish_reasons = []
        bullish_dates = []
        bearish_dates = []

        for raw_mentions, created_at in rows:
            mentions = _parse_json(raw_mentions)
            for m in mentions:
                m_code = str(m.get("code", "")).strip()
                # 匹配: 精确匹配 code, 或前6位匹配
                if m_code != code and not (m_code and code and m_code[:6] == code[:6]):
                    continue
                sentiment = str(m.get("sentiment", "")).lower()
                reason = str(m.get("reason", "")).strip()
                date_str = created_at[:10] if created_at else ""

                if _is_bullish(sentiment):
                    bullish_reasons.append((date_str, reason))
                    bullish_dates.append(date_str)
                elif _is_bearish(sentiment):
                    bearish_reasons.append((date_str, reason))
                    bearish_dates.append(date_str)

        # 情绪趋势: 近1/3 vs 前2/3
        sentiment_trend = "stable"
        total = len(bullish_reasons) + len(bearish_reasons)
        if total >= 4:
            mid = len(bullish_dates) // 2
            # bullish_dates 已按时间倒序, 前半段=近期
            recent_bull = len(bullish_dates[:mid]) if mid > 0 else 0
            older_bull = len(bullish_dates[mid:])
            if recent_bull > older_bull + 1:
                sentiment_trend = "strengthening"
            elif recent_bull + 1 < older_bull:
                sentiment_trend = "weakening"

        # 去重核心看多理由 (按 reason 文本去重, 保留最新日期)
        seen_reasons = set()
        unique_bullish = []
        for date, reason in bullish_reasons:
            # 简单去重: 取 reason 前30字符作为 key
            key = reason[:30]
            if key not in seen_reasons:
                seen_reasons.add(key)
                unique_bullish.append({"date": date, "reason": reason})

        # 读取 fundamentals JSON 中的 research_catalysts
        research_catalysts = {}
        fdir = os.path.join(os.path.dirname(_DB_PATH), "fundamentals")
        fpath = os.path.join(fdir, f"{code}.json")
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    d = json.load(f)
                research_catalysts = d.get("research_catalysts", {})
            except Exception:
                pass

        result: Dict[str, Any] = {
            "mention_count": total,
            "bullish_count": len(bullish_reasons),
            "bearish_count": len(bearish_reasons),
            "latest_bullish": bullish_dates[0] if bullish_dates else "",
            "top_reasons": unique_bullish[:3],
            "sentiment_trend": sentiment_trend,
        }
        if research_catalysts:
            result["research_catalysts"] = research_catalysts

        return result if total > 0 else {}

    finally:
        conn.close()


def fmt_stock_research_signal(signal: Dict[str, Any]) -> str:
    """格式化个股研报信号为可读文本。"""
    if not signal:
        return ""
    lines = []
    n = signal.get("mention_count", 0)
    bull = signal.get("bullish_count", 0)
    bear = signal.get("bearish_count", 0)
    trend = signal.get("sentiment_trend", "stable")
    trend_cn = {"strengthening": "强化↑", "weakening": "弱化↓"}.get(trend, "稳定→")
    lines.append(f"研报提及{n}次(多{bull}/空{bear}), 情绪{trend_cn}")

    if signal.get("latest_bullish"):
        lines.append(f"  最近看多: {signal['latest_bullish']}")

    for r in signal.get("top_reasons", []):
        lines.append(f"  看多理由[{r.get('date','')[5:]}]: {r.get('reason','')}")

    rc = signal.get("research_catalysts", {})
    if rc:
        exp = rc.get("high_momentum_exposure", 0)
        tags = rc.get("catalyst_tags", [])
        evi = rc.get("evidence", [])
        if exp > 0:
            lines.append(f"  高动量催化: exposure={exp}/5, tags={','.join(tags[:3])}")
        for e in evi[:2]:
            lines.append(f"    催化证据: {e}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# ② 行业研报动量
# ══════════════════════════════════════════════════════════

def get_sector_momentum(
    cutoff_date: str = "",
    days: int = 14,
    top_n: int = 10,
) -> Dict[str, Any]:
    """获取近 N 天行业研报动量 (bullish/bearish 观点聚合)。

    返回:
      hot_sectors:    近期 bullish 最密集的赛道
      cold_sectors:   近期 bearish 最密集的赛道
      emerging_sectors: 近7天新出现 bullish 的赛道
    """
    conn = _get_conn()
    if not conn:
        return {"hot_sectors": [], "cold_sectors": [], "emerging_sectors": []}

    try:
        if cutoff_date:
            base = datetime.strptime(cutoff_date, "%Y-%m-%d")
        else:
            row = conn.execute(
                "SELECT MAX(created_at) FROM sector_knowledge"
            ).fetchone()
            base = datetime.strptime(row[0][:10], "%Y-%m-%d") if row and row[0] else datetime.now()

        since = (base - timedelta(days=days)).strftime("%Y-%m-%d")
        since_7d = (base - timedelta(days=7)).strftime("%Y-%m-%d")

        rows = conn.execute(
            "SELECT sector, viewpoint, sentiment, created_at FROM sector_knowledge "
            "WHERE created_at >= ? ORDER BY created_at DESC",
            (since,),
        ).fetchall()

        # 归一化后聚合
        bull_counter: Counter = Counter()
        bear_counter: Counter = Counter()
        bull_7d_counter: Counter = Counter()
        sector_views: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

        for raw_sector, viewpoint, sentiment, created_at in rows:
            norm = normalize_sector(raw_sector)
            if not norm:
                continue
            date_str = created_at[:10] if created_at else ""
            sent = str(sentiment or "").lower()
            if _is_bullish(sent):
                bull_counter[norm] += 1
                if date_str >= since_7d:
                    bull_7d_counter[norm] += 1
            elif _is_bearish(sent):
                bear_counter[norm] += 1
            sector_views[norm].append((viewpoint or "", date_str))

        # 热门赛道: bullish 最密集
        hot = []
        for sector, cnt in bull_counter.most_common(top_n):
            views = sector_views.get(sector, [])
            # 取最新一条代表性观点
            key_view = views[0][0][:80] if views else ""
            hot.append({
                "sector": sector,
                "bullish_count": cnt,
                "bearish_count": bear_counter.get(sector, 0),
                "key_view": key_view,
            })

        # 冷门赛道: bearish 最密集
        cold = []
        for sector, cnt in bear_counter.most_common(5):
            if cnt >= 2:  # 至少2次看空才列入
                views = sector_views.get(sector, [])
                key_view = views[0][0][:80] if views else ""
                cold.append({
                    "sector": sector,
                    "bearish_count": cnt,
                    "key_view": key_view,
                })

        # 新兴赛道: 近7天有 bullish 但之前没有
        emerging = []
        for sector, cnt_7d in bull_7d_counter.most_common(5):
            cnt_14d = bull_counter.get(sector, 0)
            # 近7天占比高 = 新出现的
            if cnt_7d >= 2 and cnt_7d >= cnt_14d * 0.6:
                views = sector_views.get(sector, [])
                key_view = views[0][0][:80] if views else ""
                emerging.append({
                    "sector": sector,
                    "bullish_count": cnt_7d,
                    "key_view": key_view,
                })

        return {
            "hot_sectors": hot,
            "cold_sectors": cold,
            "emerging_sectors": emerging,
        }

    finally:
        conn.close()


def fmt_sector_momentum(momentum: Dict[str, Any]) -> str:
    """格式化行业研报动量为可读文本。"""
    lines = []

    hot = momentum.get("hot_sectors", [])
    if hot:
        lines.append("【研报热门赛道 (近14天bullish观点最密集)】")
        for s in hot[:8]:
            bear = s.get("bearish_count", 0)
            bear_str = f"/空{bear}" if bear else ""
            lines.append(f"  {s['sector']}: 多{s['bullish_count']}{bear_str} — {s.get('key_view','')}")

    cold = momentum.get("cold_sectors", [])
    if cold:
        lines.append("【研报冷门赛道 (近14天bearish观点最密集)】")
        for s in cold[:3]:
            lines.append(f"  {s['sector']}: 空{s['bearish_count']} — {s.get('key_view','')}")

    emerging = momentum.get("emerging_sectors", [])
    if emerging:
        lines.append("【研报新兴赛道 (近7天新出现bullish观点)】")
        for s in emerging[:3]:
            lines.append(f"  {s['sector']}: 多{s['bullish_count']} — {s.get('key_view','')}")

    return "\n".join(lines) if lines else ""


# ══════════════════════════════════════════════════════════
# ③ 市场情绪
# ══════════════════════════════════════════════════════════

def get_market_sentiment(
    cutoff_date: str = "",
    days: int = 7,
) -> Dict[str, Any]:
    """获取近 N 天研报市场情绪。

    返回:
      sentiment:   cautiously_optimistic / optimistic / cautious / bearish
      summary:     最近一条市场概况
      key_insights: 核心洞察 (去重, ≤5条)
      risk_warnings: 风险预警 (≤3条)
    """
    conn = _get_conn()
    if not conn:
        return {}

    try:
        if cutoff_date:
            base = datetime.strptime(cutoff_date, "%Y-%m-%d")
        else:
            row = conn.execute(
                "SELECT MAX(created_at) FROM general_knowledge"
            ).fetchone()
            base = datetime.strptime(row[0][:10], "%Y-%m-%d") if row and row[0] else datetime.now()

        since = (base - timedelta(days=days)).strftime("%Y-%m-%d")

        rows = conn.execute(
            "SELECT summary, market_overview, key_insights, risk_warnings, created_at "
            "FROM general_knowledge WHERE created_at >= ? ORDER BY created_at DESC",
            (since,),
        ).fetchall()

        if not rows:
            return {}

        # 汇总
        all_insights = []
        all_risks = []
        latest_overview = ""
        bull_count = 0
        bear_count = 0

        for summary, overview, insights_raw, risks_raw, created_at in rows:
            if overview and not latest_overview:
                latest_overview = overview[:200]
            insights = _parse_json(insights_raw)
            for ins in insights:
                text = str(ins).strip()
                if text and text not in all_insights:
                    all_insights.append(text)
            risks = _parse_json(risks_raw)
            for r in risks:
                text = str(r).strip()
                if text and text not in all_risks:
                    all_risks.append(text)

        # 情绪判断: 基于 sector_knowledge 的 bullish/bearish 比例
        since_sk = (base - timedelta(days=3)).strftime("%Y-%m-%d")
        sk_rows = conn.execute(
            "SELECT sentiment FROM sector_knowledge WHERE created_at >= ?",
            (since_sk,),
        ).fetchall()
        for (sent,) in sk_rows:
            s = str(sent or "").lower()
            if _is_bullish(s):
                bull_count += 1
            elif _is_bearish(s):
                bear_count += 1

        total_sk = bull_count + bear_count
        if total_sk == 0:
            sentiment = "neutral"
        elif bull_count > bear_count * 2:
            sentiment = "optimistic"
        elif bull_count > bear_count:
            sentiment = "cautiously_optimistic"
        elif bear_count > bull_count * 2:
            sentiment = "bearish"
        else:
            sentiment = "cautious"

        return {
            "sentiment": sentiment,
            "summary": latest_overview,
            "key_insights": all_insights[:5],
            "risk_warnings": all_risks[:3],
            "bull_count": bull_count,
            "bear_count": bear_count,
        }

    finally:
        conn.close()


def fmt_market_sentiment(ms: Dict[str, Any]) -> str:
    """格式化市场情绪为可读文本。"""
    if not ms:
        return ""
    lines = []
    sent_cn = {
        "optimistic": "乐观", "cautiously_optimistic": "谨慎乐观",
        "cautious": "谨慎", "bearish": "看空", "neutral": "中性",
    }
    lines.append(f"【研报市场情绪: {sent_cn.get(ms.get('sentiment',''), '未知')}】")
    if ms.get("summary"):
        lines.append(f"  概况: {ms['summary']}")
    for ins in ms.get("key_insights", []):
        lines.append(f"  洞察: {ins[:100]}")
    for r in ms.get("risk_warnings", []):
        lines.append(f"  风险: {r[:80]}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# ④ 研报黑马
# ══════════════════════════════════════════════════════════

def get_dark_horse_stocks(
    cutoff_date: str = "",
    days: int = 14,
    existing_codes: Optional[List[str]] = None,
    min_bullish: int = 2,
) -> List[Dict[str, Any]]:
    """获取近 N 天有 bullish 催化但不在现有候选池的个股 (研报黑马)。

    返回: [{name, code, bullish_count, reasons}]
    """
    conn = _get_conn()
    if not conn:
        return []

    try:
        if cutoff_date:
            base = datetime.strptime(cutoff_date, "%Y-%m-%d")
        else:
            row = conn.execute(
                "SELECT MAX(created_at) FROM general_knowledge"
            ).fetchone()
            base = datetime.strptime(row[0][:10], "%Y-%m-%d") if row and row[0] else datetime.now()

        since = (base - timedelta(days=days)).strftime("%Y-%m-%d")
        existing = set(existing_codes or [])

        rows = conn.execute(
            "SELECT stock_mentions, created_at FROM general_knowledge "
            "WHERE created_at >= ? AND stock_mentions IS NOT NULL "
            "ORDER BY created_at DESC",
            (since,),
        ).fetchall()

        # 聚合个股 bullish 提及
        stock_bull: Dict[str, Dict[str, Any]] = {}  # code → {name, count, reasons}

        for raw_mentions, created_at in rows:
            mentions = _parse_json(raw_mentions)
            for m in mentions:
                code = str(m.get("code", "")).strip()
                if not code or code in existing:
                    continue
                sentiment = str(m.get("sentiment", "")).lower()
                if not _is_bullish(sentiment):
                    continue
                name = str(m.get("name", "")).strip()
                reason = str(m.get("reason", "")).strip()
                if code not in stock_bull:
                    stock_bull[code] = {"name": name, "code": code,
                                        "bullish_count": 0, "reasons": []}
                stock_bull[code]["bullish_count"] += 1
                if reason and reason not in [r for r in stock_bull[code]["reasons"]]:
                    stock_bull[code]["reasons"].append(reason)

        # 过滤: 至少 min_bullish 次 bullish 提及
        dark_horses = [
            v for v in stock_bull.values()
            if v["bullish_count"] >= min_bullish
        ]
        dark_horses.sort(key=lambda x: -x["bullish_count"])
        return dark_horses[:10]

    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# ⑤ 研报风险信号
# ══════════════════════════════════════════════════════════

def get_research_risk_signals(
    cutoff_date: str = "",
    days: int = 14,
) -> Dict[str, Any]:
    """获取近 N 天研报风险信号。

    返回:
      bearish_stocks:  被看空的个股
      systemic_risks:  系统性风险预警
    """
    conn = _get_conn()
    if not conn:
        return {"bearish_stocks": [], "systemic_risks": []}

    try:
        if cutoff_date:
            base = datetime.strptime(cutoff_date, "%Y-%m-%d")
        else:
            row = conn.execute(
                "SELECT MAX(created_at) FROM general_knowledge"
            ).fetchone()
            base = datetime.strptime(row[0][:10], "%Y-%m-%d") if row and row[0] else datetime.now()

        since = (base - timedelta(days=days)).strftime("%Y-%m-%d")

        # 个股级 bearish
        rows = conn.execute(
            "SELECT stock_mentions, created_at FROM general_knowledge "
            "WHERE created_at >= ? AND stock_mentions IS NOT NULL "
            "ORDER BY created_at DESC",
            (since,),
        ).fetchall()

        stock_bear: Dict[str, Dict[str, Any]] = {}

        for raw_mentions, created_at in rows:
            mentions = _parse_json(raw_mentions)
            for m in mentions:
                code = str(m.get("code", "")).strip()
                if not code:
                    continue
                sentiment = str(m.get("sentiment", "")).lower()
                if not _is_bearish(sentiment):
                    continue
                name = str(m.get("name", "")).strip()
                reason = str(m.get("reason", "")).strip()
                if code not in stock_bear:
                    stock_bear[code] = {"name": name, "code": code,
                                        "bearish_count": 0, "reasons": []}
                stock_bear[code]["bearish_count"] += 1
                if reason and reason not in stock_bear[code]["reasons"]:
                    stock_bear[code]["reasons"].append(reason)

        bearish_stocks = sorted(stock_bear.values(), key=lambda x: -x["bearish_count"])[:10]

        # 系统性风险预警
        risk_rows = conn.execute(
            "SELECT risk_warnings FROM general_knowledge WHERE created_at >= ? "
            "AND risk_warnings IS NOT NULL",
            (since,),
        ).fetchall()

        systemic_risks = []
        for (raw,) in risk_rows:
            for r in _parse_json(raw):
                text = str(r).strip()
                if text and text not in systemic_risks:
                    systemic_risks.append(text)

        return {
            "bearish_stocks": bearish_stocks,
            "systemic_risks": systemic_risks[:5],
        }

    finally:
        conn.close()


def fmt_research_risk_signals(risks: Dict[str, Any]) -> str:
    """格式化研报风险信号为可读文本。"""
    if not risks:
        return ""
    lines = []

    bear_stocks = risks.get("bearish_stocks", [])
    if bear_stocks:
        lines.append("【研报看空个股】")
        for s in bear_stocks[:5]:
            reasons_str = "; ".join(s.get("reasons", [])[:2])
            lines.append(f"  {s.get('code','')} {s.get('name','')}: 空{s.get('bearish_count',0)}次 — {reasons_str}")

    sys_risks = risks.get("systemic_risks", [])
    if sys_risks:
        lines.append("【研报系统性风险预警】")
        for r in sys_risks[:3]:
            lines.append(f"  ⚠ {r[:100]}")

    return "\n".join(lines) if lines else ""


# ══════════════════════════════════════════════════════════
# ⑥ 板块研报摘要 (供 fundamentals 生成注入, 板块级信号)
# ══════════════════════════════════════════════════════════

# industry 字段常见关键词 → 板块匹配关键词
# 用于把 fundamentals.business_overview.industry (如"元器件（印制电路板PCB）")
# 映射到可在 sector_knowledge.sector 上 LIKE 匹配的关键词。
#
# 自动从 normalize.py 生成 (单一真相源), 覆盖全部 27 个标准赛道。
# 早先版本只有手写 7 个板块, 导致电子特气/MLCC/钨/铜箔/电感等 AI 上游材料
# 无法匹配到板块研报 → 基本面生成时缺失研报上下文 → V3 chain 评分偏低。
from .normalize import get_sector_keyword_index as _build_keyword_map
_INDUSTRY_KEYWORD_MAP: Dict[str, List[str]] = _build_keyword_map()


def _extract_sector_keywords(industry_text: str) -> List[str]:
    """从 industry 文本提取板块匹配关键词。

    优先用 normalize_sector 归一化; 同时做子串匹配补充。
    返回去重的关键词列表 (如 ['PCB', '覆铜板', 'CCL', ...])。

    注: 入参 industry_text 可能是 "粗industry + 股票name + 细industry" 的组合
    (由 generate_one 拼接), 故能匹配到 name 含的板块线索 (如"景旺电子"→虽不含
    PCB, 但增量场景会带上细industry"印制电路板PCB")。
    """
    if not industry_text:
        return []
    text = industry_text
    kws: List[str] = []
    matched_sectors = set()
    # 1. 反向查 INDUSTRY_KEYWORD_MAP: 若 industry 含某板块的关键词, 收集该板块所有关键词
    for sector, words in _INDUSTRY_KEYWORD_MAP.items():
        if any(w in text for w in words):
            if sector not in matched_sectors:
                matched_sectors.add(sector)
                kws.extend(words)
    # 2. 去重, 保留长度>=2 的 (避免单字误匹配)
    seen = set()
    out = []
    for k in kws:
        if len(k) >= 2 and k not in seen:
            seen.add(k)
            out.append(k)
    return out


# name 启发式: 首次生成 fundamentals 时只有粗industry+name, 用已知龙头name补匹配。
# 增量场景下细industry会命中, 此处仅兜底首次生成。误注入风险由"板块级·信源中"标注 + 防污染规则控制。
_NAME_SECTOR_HINTS: Dict[str, List[str]] = {
    "PCB/CCL": ["景旺", "沪电", "深南电路", "胜宏", "鹏鼎", "东山精密", "生益",
                "超声", "崇达", "奥士", "博敏", "兴森", "方邦", "天津普林"],
}


def get_industry_research_brief(industry_text: str, top_n: int = 8,
                                days: int = 60) -> str:
    """按个股所属行业, 从 sector_knowledge 取板块研报文本摘要。

    用于 fundamentals 生成时注入 prompt, 让冷门股(未被 stock_mentions 直接点名的)
    也能获得所在板块的研报视角。明确标注 [信源:中·板块级], 提醒这是板块信号非个股
    直接证据, 需与财报/防污染规则交叉验证。

    Args:
        industry_text: fundamentals.business_overview.industry 字段值
            (generate_one 会拼接 "粗industry + name + 细industry" 提升召回)
        top_n: 最多返回的研报条数
        days: 回看天数 (默认60天, 比 picker 运行时的30天更宽, 因 fundamentals 是周期性生成)

    Returns:
        格式化的板块研报文本。无匹配时返回空字符串。
    """
    text = industry_text or ""
    extra_kws: List[str] = []
    for sector, names in _NAME_SECTOR_HINTS.items():
        if any(n in text for n in names):
            extra_kws.extend(_INDUSTRY_KEYWORD_MAP.get(sector, []))

    kws = _extract_sector_keywords(industry_text) + extra_kws
    if not kws:
        return ""

    conn = _get_conn()
    if not conn:
        return ""
    try:
        # 计算时间窗口
        row = conn.execute("SELECT MAX(created_at) FROM sector_knowledge").fetchone()
        if row and row[0]:
            base = datetime.strptime(row[0][:10], "%Y-%m-%d")
        else:
            base = datetime.now()
        since = (base - timedelta(days=days)).strftime("%Y-%m-%d")

        # 用关键词在 sector 字段做 OR LIKE 匹配
        like_clauses = " OR ".join(["sector LIKE ?" for _ in kws])
        params = [f"%{k}%" for k in kws] + [since]
        rows = conn.execute(
            f"SELECT sector, sentiment, viewpoint, key_data, created_at "
            f"FROM sector_knowledge WHERE ({like_clauses}) AND created_at >= ? "
            f"ORDER BY created_at DESC LIMIT ?",
            params + [top_n * 3],  # 多取些再按归一化赛道过滤
        ).fetchall()

        if not rows:
            return ""

        # 归一化过滤: 只保留 normalize_sector 能映射到匹配赛道的记录 (降低误安风险)
        matched_norm_sectors = set()
        for sector, words in _INDUSTRY_KEYWORD_MAP.items():
            if any(w in industry_text for w in words):
                matched_norm_sectors.add(sector)
        # name 启发式命中的赛道也纳入 (首次生成兜底)
        for sector, names in _NAME_SECTOR_HINTS.items():
            if any(n in (industry_text or "") for n in names):
                matched_norm_sectors.add(sector)

        filtered = []
        for sector, sentiment, viewpoint, key_data, created_at in rows:
            norm = normalize_sector(sector)
            # 归一化后的赛道必须在匹配集合内, 否则跳过 (避免碎片 sector 误匹配)
            if norm and norm in matched_norm_sectors:
                filtered.append((sector, sentiment, viewpoint, key_data, created_at))
            if len(filtered) >= top_n:
                break

        if not filtered:
            return ""

        # 格式化输出
        sector_label = " / ".join(sorted(matched_norm_sectors))
        lines = [
            f"【博主研报·板块级信号 {sector_label}】"
            f"(注: 以下为板块整体视角, 非该股直接点名; 信源可信度: 中, 需与财报交叉验证)"
        ]
        for sector, sentiment, viewpoint, key_data, created_at in filtered:
            date_str = created_at[:10] if created_at else ""
            sent_tag = {"bullish": "看多", "positive": "看多",
                        "bearish": "看空", "negative": "看空",
                        "neutral": "中性"}.get(str(sentiment).lower(), str(sentiment))
            vp = (viewpoint or "").strip()[:120]
            lines.append(f"- [{date_str} {sent_tag}] {vp}")
            kd_list = _parse_json(key_data)
            if kd_list:
                kd_str = "; ".join(str(k)[:60] for k in kd_list[:3])
                lines.append(f"  数据: {kd_str}")

        return "\n".join(lines)

    except Exception:
        return ""
    finally:
        conn.close()
