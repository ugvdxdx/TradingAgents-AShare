"""debate_picker v5 — 增量信息采集层。

核心问题: V3 打分是季度更新的静态快照, 辩论阶段需要 V3 没有的增量信息。
本模块从四个维度补充信息增益:
  1. 实时财务摘要 (akshare get_fundamentals: 最新财报数据, 比 fundamentals JSON 更新)
  2. 近期新闻 (akshare get_news: 最近30天新闻, V3 完全没有)
  3. 量化差分信号 (K线动量加速/量能异动/新高突破/资金流趋势)
  4. LLM 近期事件摘要 (用 LLM 知识补充 akshare 无法覆盖的事件)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import data_io
from .llm_helper import LLMHelper
from .picker_state import PickerState

# ══════════════════════════════════════════════════════════
# akshare 数据接口 (通过 dataflows 路由)
# ══════════════════════════════════════════════════════════

def _get_provider():
    """懒加载 cn_akshare provider。"""
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor


def _fetch_fundamentals(code: str) -> str:
    """拉取实时财务摘要 (akshare stock_financial_abstract)。"""
    try:
        route = _get_provider()
        return route("get_fundamentals", code)
    except Exception as e:
        return f"(财务数据获取失败: {type(e).__name__})"


def _load_news_cache() -> Dict[str, str]:
    """加载预缓存新闻 (由 WebSearch 等工具预填充)。"""
    cache_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "news_cache.json")
    cache_path = os.path.normpath(cache_path)
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            return json.load(f)
    return {}


def _fetch_news_by_name(name: str, cutoff_date: str, days: int = 30) -> str:
    """按公司名称搜索新闻 (东方财富搜索 API, 比 stock_news_em 按代码搜索更精准)。"""
    try:
        import requests as req
        param = json.dumps({
            "uid": "",
            "keyword": name,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "ft",          # 按相关度排序, 避免成交额类泛资讯刷屏
                    "pageIndex": 1,
                    "pageSize": 15,
                    "preTag": "",
                    "postTag": ""
                }
            }
        }, ensure_ascii=False)
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Referer": "https://so.eastmoney.com/",
            "Accept": "*/*",
        }
        resp = req.get(url, params={"cb": "cb", "param": param}, headers=headers, timeout=10)
        text = resp.text
        json_str = text[text.index("(") + 1:text.rindex(")")]
        data = json.loads(json_str)
        raw = data.get("result", {}).get("cmsArticleWebOld", [])
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("list", [])
        else:
            items = []
        if not items:
            return ""
        # 过滤截止日期后的新闻, 过滤成交额/换手率类泛资讯
        cutoff_compact = cutoff_date.replace("-", "")
        skip_patterns = ["成交额", "换手率", "融资买入", "融资余额", "龙虎榜"]
        filtered = []
        for item in items[:15]:
            title = item.get("title", "").replace("<em>", "").replace("</em>", "")
            # 跳过成交额/换手率等泛资讯
            if any(p in title for p in skip_patterns):
                continue
            date = (item.get("date", "") or "")[:10]
            content = (item.get("content", "") or "").replace("<em>", "").replace("</em>", "")[:150]
            source = item.get("mediaName", "")
            if date.replace("-", "") > cutoff_compact:
                continue
            filtered.append((date, title, content, source))
        # 按时间线排序: 最新在前 (方便回测按时间定位)
        filtered.sort(key=lambda x: x[0], reverse=True)
        lines = []
        for date, title, content, source in filtered:
            lines.append(f"- [{date}] {title} ({source})")
            if content:
                lines.append(f"  {content}")
            if len(lines) >= 12:  # 最多6条新闻(每条2行)
                break
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════
# 基本面深度数据 (fundamentals JSON → 结构化摘要, 作为 akshare 的补充)
# ══════════════════════════════════════════════════════════

def _load_fundamental_detail(code: str) -> Optional[Dict]:
    """加载 fundamentals JSON, 提取竞争分析/增长评估等 akshare 没有的定性信息。"""
    path = os.path.join(data_io.FUNDAMENTALS_DIR, f"{code}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return None

    ga = d.get("growth_assessment", {})
    ca = d.get("competitive_analysis", {})
    fh = d.get("financial_health", {})
    km = fh.get("key_metrics", {})
    bo = d.get("business_overview", {})

    return {
        "summary": d.get("summary", ""),
        "business_overview": bo.get("what_they_do", "")[:300],
        # 财务增长指标 (营收/净利/毛利率 + 同比增速)
        "revenue_yi": km.get("revenue_yi"),
        "net_profit_yi": km.get("net_profit_yi"),
        "gross_margin_pct": km.get("gross_margin_pct"),
        "net_margin_pct": km.get("net_margin_pct"),
        "roe_pct": km.get("roe_pct"),
        "operating_cf_yi": km.get("operating_cf_yi"),
        "health_rating": fh.get("health_rating", ""),
        "financial_highlights": fh.get("highlights", [])[:3],
        "financial_risks": fh.get("risks", [])[:2],
        # 增长评估 (akshare 没有的定性判断)
        "growth_score": ga.get("growth_score"),
        "growth_drivers": ga.get("growth_drivers", []),
        "headwinds": ga.get("headwinds", []),
        # 竞争分析 (akshare 没有的定性判断)
        "moat_level": ca.get("moat_level", ""),
        "industry_position": bo.get("industry_position", "")[:200],
        "strengths": ca.get("strengths", []),
        "weaknesses": ca.get("weaknesses", []),
    }


def _fmt_fundamental_detail(fd: Dict) -> str:
    """格式化基本面深度数据为可读文本。"""
    if not fd:
        return ""

    lines = []

    # 业务概览 (含增长数据)
    if fd.get("business_overview"):
        lines.append(f"业务: {fd['business_overview']}")

    # 行业地位
    if fd.get("industry_position"):
        lines.append(f"行业地位: {fd['industry_position']}")

    # 财务核心指标 (营收/净利/毛利率/ROE/现金流)
    fin_parts = []
    if fd.get("revenue_yi"):
        fin_parts.append(f"营收{fd['revenue_yi']}亿")
    if fd.get("net_profit_yi"):
        fin_parts.append(f"净利{fd['net_profit_yi']}亿")
    if fd.get("gross_margin_pct"):
        fin_parts.append(f"毛利率{fd['gross_margin_pct']}%")
    if fd.get("net_margin_pct"):
        fin_parts.append(f"净利率{fd['net_margin_pct']}%")
    if fd.get("roe_pct"):
        fin_parts.append(f"ROE{fd['roe_pct']}%")
    if fd.get("operating_cf_yi"):
        fin_parts.append(f"经营CF{fd['operating_cf_yi']}亿")
    if fd.get("health_rating"):
        fin_parts.append(f"健康度:{fd['health_rating']}")
    if fin_parts:
        lines.append("财务: " + " | ".join(fin_parts))

    # 财务亮点 (含同比增速等关键增长数据)
    if fd.get("financial_highlights"):
        for h in fd["financial_highlights"]:
            lines.append(f"  ★ {str(h)[:100]}")

    # 财务风险
    if fd.get("financial_risks"):
        for r in fd["financial_risks"]:
            lines.append(f"  ⚠ {str(r)[:80]}")

    # 护城河 + 增长评分
    if fd.get("moat_level"):
        lines.append(f"护城河: {fd['moat_level']}")
    if fd.get("growth_score"):
        lines.append(f"增长评分: {fd['growth_score']}/10")

    # 增长驱动力 (完整输出, 不截断关键数据)
    if fd.get("growth_drivers"):
        for i, d in enumerate(fd["growth_drivers"][:5], 1):
            lines.append(f"  驱动{i}: {str(d)[:120]}")
    if fd.get("headwinds"):
        for i, h in enumerate(fd["headwinds"][:3], 1):
            lines.append(f"  逆风{i}: {str(h)[:100]}")

    # 竞争优劣势
    if fd.get("strengths"):
        for s in fd["strengths"][:3]:
            lines.append(f"  优势: {str(s)[:100]}")
    if fd.get("weaknesses"):
        for w in fd["weaknesses"][:2]:
            lines.append(f"  劣势: {str(w)[:80]}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 量化差分信号 (K线 + 资金流 → V3 没有的动态信号)
# ══════════════════════════════════════════════════════════

def _compute_signals(code: str, cutoff_date: Optional[str],
                     mf_cache: Dict) -> Dict[str, Any]:
    """从 K线和资金流计算 V3 没有捕捉的动态信号。"""
    sig: Dict[str, Any] = {}
    df = data_io.load_kline(code, cutoff_date)

    if df is not None and len(df) >= 20:
        close = df["close"].values
        vol = df["volume"].values
        high = df["high"].values
        low = df["low"].values
        n = len(df)

        # 动量加速: 5日涨幅 vs 20日涨幅
        ret_5d = (close[-1] / close[-6] - 1) * 100 if n >= 6 else 0
        ret_20d = (close[-1] / close[-21] - 1) * 100 if n >= 21 else 0
        sig["ret_5d"] = round(ret_5d, 1)
        sig["ret_20d"] = round(ret_20d, 1)
        sig["momentum_accel"] = round(ret_5d - ret_20d / 4, 1)  # 正=加速

        # 量能异动: 最近5日均量 vs 20日均量
        vol_5d = vol[-5:].mean()
        vol_20d = vol[-20:].mean() if n >= 20 else vol_5d
        sig["vol_surge"] = round(vol_5d / vol_20d, 2) if vol_20d > 0 else 1.0  # >1.5=异动

        # 新高突破
        high_20d = high[-20:].max()
        sig["at_20d_high"] = close[-1] >= high_20d * 0.98  # 距20日高点2%以内
        if n >= 60:
            high_60d = high[-60:].max()
            sig["at_60d_high"] = close[-1] >= high_60d * 0.98
        else:
            sig["at_60d_high"] = False

        # 波动率 (20日)
        import numpy as np
        rets = np.diff(close[-21:]) / close[-21:-1]
        sig["volatility_20d"] = round(float(rets.std() * 100), 2)

        # K线走势摘要: 近10日收盘价 + 涨跌幅, 供辩论看趋势形态
        kline_days = min(10, n)
        kline_lines = []
        for j in range(-kline_days, 0):
            d = df.index[j] if hasattr(df.index[j], "strftime") else j
            date_str = d.strftime("%m-%d") if hasattr(d, "strftime") else str(d)
            chg = (close[j] / close[j-1] - 1) * 100 if j > -n else 0
            chg_str = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            kline_lines.append(f"{date_str}:{close[j]:.2f}({chg_str})")
        sig["kline_10d"] = " → ".join(kline_lines)

        # 关键价位: 近20日最高/最低, 当前价距高低点幅度
        sig["price_now"] = round(float(close[-1]), 2)
        sig["high_20d"] = round(float(high_20d), 2)
        sig["low_20d"] = round(float(low[-20:].min()), 2)
        sig["dist_from_high"] = round((close[-1] / high_20d - 1) * 100, 1)
        sig["dist_from_low"] = round((close[-1] / low[-20:].min() - 1) * 100, 1)

    # 资金流趋势
    rows = mf_cache.get(code, [])
    if cutoff_date:
        cc = cutoff_date.replace("-", "")
        rows = [r for r in rows if r.get("date", "") <= cc]
    if len(rows) >= 10:
        # 近5日 vs 前5日 主力净流入趋势
        recent_5 = sum(r.get("main_net", 0) for r in rows[-5:]) / 1e8
        prior_5 = sum(r.get("main_net", 0) for r in rows[-10:-5]) / 1e8
        sig["fund_5d"] = round(recent_5, 2)
        sig["fund_prev5d"] = round(prior_5, 2)
        sig["fund_trend"] = "改善" if recent_5 > prior_5 else ("恶化" if recent_5 < prior_5 else "持平")
        sig["fund_delta"] = round(recent_5 - prior_5, 2)
    elif len(rows) >= 5:
        sig["fund_5d"] = round(sum(r.get("main_net", 0) for r in rows[-5:]) / 1e8, 2)

    # 资金流明细: 近5日每日主力净流入 (供辩论看持续性)
    if len(rows) >= 5:
        mf_lines = []
        for r in rows[-5:]:
            d = r.get("date", "")
            d_short = d[5:] if len(d) >= 10 else d  # MM-DD
            mn = r.get("main_net", 0) / 1e8
            mn_str = f"+{mn:.2f}亿" if mn >= 0 else f"{mn:.2f}亿"
            mf_lines.append(f"{d_short}:{mn_str}")
        sig["mf_5d_detail"] = " | ".join(mf_lines)

    return sig


def _fmt_signals(sig: Dict) -> str:
    """格式化量化差分信号。"""
    if not sig:
        return "(数据不足)"

    parts = []
    if "ret_5d" in sig:
        parts.append(f"5日涨幅{sig['ret_5d']}%")
    if "ret_20d" in sig:
        parts.append(f"20日涨幅{sig['ret_20d']}%")
    if "momentum_accel" in sig:
        accel = sig["momentum_accel"]
        tag = "加速↑" if accel > 2 else ("减速↓" if accel < -2 else "匀速→")
        parts.append(f"动量{tag}({accel})")
    if "vol_surge" in sig:
        tag = "放量" if sig["vol_surge"] > 1.5 else ("缩量" if sig["vol_surge"] < 0.7 else "正常")
        parts.append(f"量能{tag}({sig['vol_surge']}x)")
    if sig.get("at_20d_high"):
        parts.append("★近20日新高")
    if sig.get("at_60d_high"):
        parts.append("★★近60日新高")
    if "fund_trend" in sig:
        parts.append(f"资金流{sig['fund_trend']}(Δ{sig.get('fund_delta', 0)}亿)")
    elif "fund_5d" in sig:
        parts.append(f"5日主力{sig['fund_5d']}亿")
    if "volatility_20d" in sig:
        parts.append(f"波动率{sig['volatility_20d']}%")

    summary = " | ".join(parts) if parts else "(无显著信号)"

    # K线走势明细
    detail_lines = []
    if sig.get("kline_10d"):
        detail_lines.append(f"近10日走势: {sig['kline_10d']}")
    if sig.get("price_now"):
        dist_h = sig.get("dist_from_high", 0)
        dist_l = sig.get("dist_from_low", 0)
        detail_lines.append(
            f"当前{sig['price_now']}元, 20日高{sig.get('high_20d','?')}(距{dist_h}%) "
            f"低{sig.get('low_20d','?')}(距+{dist_l}%)"
        )
    if sig.get("mf_5d_detail"):
        detail_lines.append(f"近5日主力: {sig['mf_5d_detail']}")

    if detail_lines:
        return summary + "\n" + "\n".join(detail_lines)
    return summary


# ══════════════════════════════════════════════════════════
# LLM 近期事件摘要 (补充 akshare 新闻无法覆盖的事件)
# ══════════════════════════════════════════════════════════

_INCREMENTAL_SYSTEM = """你是一位 A 股 AI/半导体产业链研究员。你的任务是为候选股生成近期关键事件摘要。

重要规则:
1. 只列出你确信在截止日期之前已发生的事件, 不要编造
2. 聚焦影响未来30天涨幅的事件: 订单变化、认证进展、产品发布、业绩预告、行业政策等
3. 对每只股标注事件对涨幅的影响方向: 正面↑ / 负面↓ / 中性→
4. 如果你对某只股没有确信的近期事件信息, 直接写"无确信近期事件", 不要编造
5. 严格按 JSON 数组格式输出, 每只股一条

输出格式:
```json
[
  {"code": "300308", "events": "1. 1.6T光模块4月批量交付↑ 2. 英伟达B200供应链验证通过↑", "net_bias": "bullish"},
  {"code": "688820", "events": "无确信近期事件", "net_bias": "neutral"}
]
```"""

_INCREMENTAL_HUMAN = """截止日期: {cutoff_date}

候选股:
{stock_list}

请为每只股生成近期关键事件摘要 (截止日期前已发生的事件)。"""


def _fetch_events(llm: LLMHelper, candidates: List[Dict], cutoff_date: str) -> Dict[str, str]:
    """用 LLM 批量生成近期事件摘要。返回 {code: events_text}。"""
    stock_list = "\n".join(
        f"- {c['code']} {c['name']} (V3={c.get('v3',0)}) "
        f"卡位:{c.get('essence',{}).get('chain_position','')} "
        f"催化:{c.get('essence',{}).get('core_catalyst','')}"
        for c in candidates
    )
    human = _INCREMENTAL_HUMAN.format(cutoff_date=cutoff_date, stock_list=stock_list)
    raw = llm.call(_INCREMENTAL_SYSTEM, human, deep=False, max_chars=3000)
    items = llm.extract_json_array(raw)

    result: Dict[str, str] = {}
    for item in items:
        code = str(item.get("code", "")).strip()
        events = item.get("events", "无确信近期事件")
        bias = item.get("net_bias", "neutral")
        if code:
            prefix = {"bullish": "【偏多】", "bearish": "【偏空】"}.get(bias, "【中性】")
            result[code] = f"{prefix}{events}"
    return result


# ══════════════════════════════════════════════════════════
# 节点工厂
# ══════════════════════════════════════════════════════════

def make_incremental_info(llm: LLMHelper):
    """增量信息采集节点: 实时财务 + 新闻 + 量化信号 + LLM事件摘要。"""

    def node(state: PickerState) -> Dict[str, Any]:
        candidates = state.get("candidates", [])
        cutoff = state.get("cutoff_date") or state.get("trade_date")
        is_backtest = bool(state.get("cutoff_date"))
        mf_cache = data_io.load_mf_cache()

        print(f"\n{'='*60}\n📡 [阶段 1.5/7] 增量信息采集"
              f"{' (回测模式: 仅量化信号+本地数据)' if is_backtest else ''}\n{'='*60}")

        briefs: Dict[str, str] = {}

        for i, c in enumerate(candidates):
            code = c["code"]
            parts = [f"── {code} {c['name']} ──"]

            if not is_backtest:
                # 实盘模式: 拉取实时数据 (akshare)
                # 1a. 实时财务摘要
                print(f"  📊 [{i+1}/{len(candidates)}] {code} {c['name']}: 拉取财务...")
                fin = _fetch_fundamentals(code)
                if fin and "获取失败" not in fin:
                    if len(fin) > 800:
                        fin = fin[:800] + "\n...(截断)"
                    parts.append(f"【最新财务摘要】\n{fin}")

                # 1b. 近期新闻 (优先读缓存, 缓存由 WebSearch 预填充; 回退到东方财富 API)
                if cutoff:
                    news_cache = _load_news_cache()
                    cached = news_cache.get(code, "")
                    if cached:
                        print(f"  📰 [{i+1}/{len(candidates)}] {code} {c['name']}: 使用缓存新闻")
                        parts.append(f"【近期新闻(WebSearch缓存)】\n{cached}")
                    else:
                        print(f"  📰 [{i+1}/{len(candidates)}] {code} {c['name']}: 搜索新闻(API)...")
                        news = _fetch_news_by_name(c["name"], cutoff)
                        if news:
                            parts.append(f"【近30天新闻(按名称搜索)】\n{news}")

            # 1c. 竞争分析/增长评估 (fundamentals JSON, 无未来函数风险)
            fd = _load_fundamental_detail(code)
            if fd:
                detail = _fmt_fundamental_detail(fd)
                if detail:
                    parts.append(f"【竞争与增长】\n{detail}")

            # 1d. 量化差分信号 (K线+资金流, 按 cutoff 截断, 无未来函数)
            sig = _compute_signals(code, cutoff, mf_cache)
            if sig:
                parts.append(f"【动态信号】{_fmt_signals(sig)}")

            briefs[code] = "\n".join(parts)

        # 2. LLM 近期事件摘要 (一次批量调用, 补充 akshare 无法覆盖的事件)
        try:
            print(f"  🤖 LLM事件摘要: 批量生成...")
            events = _fetch_events(llm, candidates, cutoff or "")
            for code, evt in events.items():
                if code in briefs:
                    briefs[code] += f"\n【近期事件(LLM)】{evt}"
        except Exception as e:
            print(f"  ⚠ LLM事件摘要失败: {e}")

        # 3. 行业轮动感知 (实盘才采集, 回测跳过避免未来函数)
        rotation_context = ""
        if not is_backtest:
            try:
                from . import rotation as rot
                print(f"  🔄 板块资金流 + 轮动信号...")
                _, board_rows = rot.get_board_flow_ranking(top_n=15)
                if board_rows:
                    rotation = rot.detect_rotation(candidates, board_rows, top_k=10)
                    rotation_context = rot.build_rotation_context(board_rows, rotation, top_n=10)
                    uncovered = rotation.get("uncovered", [])
                    if uncovered:
                        print(f"  ⚠ 主线切换预警: {len(uncovered)}个净流入板块未被候选池覆盖")
            except Exception as e:
                print(f"  ⚠ 轮动信号获取失败: {e}")


        # 落盘
        run_dir = state.get("run_dir", "")
        if run_dir:
            os.makedirs(run_dir, exist_ok=True)
            path = os.path.join(run_dir, "01b_incremental.json")
            with open(path, "w") as f:
                json.dump(briefs, f, ensure_ascii=False, indent=2)
            if rotation_context:
                with open(os.path.join(run_dir, "01c_rotation.txt"), "w") as f:
                    f.write(rotation_context)

        n_with_news = sum(1 for b in briefs.values() if "新闻" in b)
        n_with_events = sum(1 for b in briefs.values() if "近期事件" in b)
        print(f"  ✅ 采集完成: {len(briefs)}只, "
              f"{n_with_news}只有新闻, {n_with_events}只有LLM事件"
              f"{', 含轮动信号' if rotation_context else ''}")

        return {
            "incremental_briefs": briefs,
            "rotation_context": rotation_context,
            "trace": [{"node": "incremental_info",
                       "note": f"{len(briefs)}只增量简报, {n_with_news}新闻, {n_with_events}事件",
                       "ts": datetime.now().isoformat()}],
        }

    return node
