#!/usr/bin/env python3
"""
基本面知识评分 —— LLM 直读 fundamentals JSON 全文打分

退避链：
  1. 读取 fundamentals/{code}.json
  2. 发送给 LLM 综合评分 → 返回 0-50 分
  3. LLM 不可用时退回到规则引擎
  4. fundamentals 无文件 → None → 调用方退回到行业分
"""
import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

FUNDAMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fundamentals')
LLM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fundamental_llm_scores.json')

# ============ LLM 评分 Prompt ============

SCORING_PROMPT = """你是A股量化研究员，需要评估一只股票的"世界知识分"——即该公司基本面中能预测未来30天股价走势的信息含量。

## 当前市场大逻辑
2025-2026年A股核心交易逻辑是**AI算力产业链业绩兑现**：
- 主线：光模块/PCB/先进封装/存储/AI芯片 → 业绩正在加速兑现，资金持续流入
- 副线：消费电子复苏+AI端侧落地 → 有实际订单支撑
- 退潮线：锂电/白酒/地产/传统矿业 → 旧赛道资金流出，即使基本面不差
- 冷门线：银行/电力/公用事业 → 无催化，随大盘

因此，打分的核心不是"这家公司好不好"，而是"这家公司是否在当前市场主线上"。

## 评分维度（满分50）

1. **产业链位置 (0-18分)**：该公司是否在AI算力/半导体/消费电子产业链的关键节点？
   - 核心供应商（如光模块→中际旭创、PCB→生益科技）= 高分
   - 边缘供应商/配套服务 = 中分
   - 产业链外/传统行业 = 低分
   - 注意：必须从what_they_do和strengths判断真实业务，不要只看industry标签（很多标签不准确）

2. **业绩兑现度 (0-16分)**：该公司的AI/半导体业务是否有具体的订单/产能/客户验证？
   - 有明确大客户（英伟达/华为/苹果/特斯拉等）+ 产能扩张中 = 高分
   - 有客户但未放量 = 中分
   - 只有概念无订单 = 低分
   - growth_drivers中"国产替代/一带一路/政策红利"等空话不算业绩验证

3. **行业动量 (0-10分)**：该行业当前是否处于资金关注焦点？
   - AI算力/光模块/PCB/先进封装 = 高动量
   - 消费电子/汽车电子 = 中动量
   - 传统行业/旧赛道 = 低动量或负动量

4. **旧赛道惩罚 (0-6分)**：该公司是否属于正在退潮的旧赛道？
   - 锂电/白酒/地产/传统矿业/银行 → 此项0分且总分扣5分
   - 与旧赛道无关 → 此项6分

## 打分校验规则

1. 行业标签为"其他"时，必须从what_they_do和strengths提取真实行业再打分
2. growth_drivers全是"国产替代/一带一路/政策红利"等模板句 → 说明无真实催化，业绩兑现度不超过5分
3. strengths中声称"龙头/前三"但无具体市占率数据或大客户名 → 不可信，产业链位置不超过10分
4. ROE<3% → 扣5分；净利润为负 → 扣8分
5. 属于旧赛道退潮品种（锂电/白酒/地产/传统矿业）→ 额外扣5分
6. 业绩兑现度要看利润质量：净利率<5%说明公司只是代工/组装，即使营收增长也不等于业绩兑现，业绩兑现度不超过8分。真正的高质量业绩兑现是净利率15%+且营收加速增长

请输出严格JSON格式，不要解释：
{"score": 整数0-50, "brief": "一句话理由（中文，30字以内），说明产业链位置和是否有业绩兑现"}

股票数据：
"""


def _load_llm_cache() -> dict:
    try:
        with open(LLM_CACHE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}


def _save_llm_cache(cache: dict):
    try:
        with open(LLM_CACHE_FILE, 'w') as f:
            json.dump(cache, f, ensure_ascii=False)
    except:
        pass


def _parse_llm_response(content: str, reasoning: str = "") -> Optional[dict]:
    """解析 LLM 响应中的 JSON 评分，支持 reasoning 模型"""
    # 优先用 content，空则从 reasoning_content 提取
    text = content.strip() if content and content.strip() else ""
    if not text and reasoning:
        m = re.search(r'\{[^{}]*"score"[^{}]*\}', reasoning)
        if m:
            text = m.group()
    if not text:
        return None

    # 清理 markdown ```json 包裹
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        result = json.loads(text)
        score = int(result.get("score", 0))
        return {"score": max(0, min(50, score)), "brief": result.get("brief", "")}
    except json.JSONDecodeError:
        return None


def _call_llm_score(data_json: str) -> Optional[dict]:
    """调用 LLM 打分，返回 {"score": int, "brief": str} 或 None
    优先级：openai 库 > urllib 直调 > 退回规则引擎"""
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("TA_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("TA_BASE_URL")
    model = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "gpt-4o-mini"

    if not api_key:
        logger.debug("无 LLM API key，退回规则引擎")
        return None

    prompt = SCORING_PROMPT + data_json[:8000]

    # 方式1：openai 库
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url or None)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2048,
            timeout=30,
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", "") or ""
        result = _parse_llm_response(content, reasoning)
        if result:
            return result
    except ImportError:
        logger.debug("openai 库不可用，降级到 urllib")
    except Exception as e:
        logger.warning(f"openai 调用失败: {e}，降级到 urllib")

    # 方式2：urllib 直调（无需 openai 库）
    try:
        import urllib.request
        req_data = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 2048,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read())
            msg = raw["choices"][0]["message"]
            content = msg.get("content", "") or ""
            reasoning = msg.get("reasoning_content", "") or ""
            result = _parse_llm_response(content, reasoning)
            if result:
                return result
    except Exception as e:
        logger.warning(f"urllib 调用失败: {e}")

    return None


def _build_stock_json(code: str) -> Optional[str]:
    """加载并压缩 fundamentals JSON 为 LLM 可读文本"""
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        return None

    # 提取关键字段，精简输出（灌水字段已删除）
    comp = data.get('competitive_analysis', {})
    fin = data.get('financial_health', {})
    metrics = fin.get('key_metrics', {})
    growth = data.get('growth_assessment', {})
    geo = data.get('geopolitical_assessment', {})
    biz = data.get('business_overview', {})

    return json.dumps({
        "code": data.get("code"),
        "name": data.get("name"),
        "industry": biz.get("industry", ""),
        "what_they_do": biz.get("what_they_do", ""),
        "moat": comp.get("moat_level", "窄"),
        "strengths": comp.get("strengths", [])[:3],
        "financial": {
            "revenue_yi": metrics.get("revenue_yi"),
            "net_profit_yi": metrics.get("net_profit_yi"),
            "roe_pct": metrics.get("roe_pct"),
            "gross_margin_pct": metrics.get("gross_margin_pct"),
            "net_margin_pct": metrics.get("net_margin_pct"),
            "rd_ratio_pct": metrics.get("rd_ratio_pct"),
            "debt_ratio_pct": metrics.get("debt_ratio_pct"),
            "health": fin.get("health_rating", ""),
        },
        "growth_drivers": growth.get("growth_drivers", [])[:3],
        "momentum": geo.get("industry_momentum", []),
    }, ensure_ascii=False, indent=1)


def compute_fundamental_knowledge(code: str, name: str = None) -> Optional[float]:
    """
    LLM 直读 fundamentals JSON，综合算出 0-50 的知识分。
    无文件返回 None，LLM 失败退回规则引擎。
    """
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None

    # === 优先 LLM 打分 ===
    cache = _load_llm_cache()
    cache_key = f"{code}_{os.path.getmtime(path):.0f}_v4"
    if cache_key in cache:
        return cache[cache_key]

    stock_json = _build_stock_json(code)
    if stock_json:
        llm_result = _call_llm_score(stock_json)
        if llm_result and llm_result.get("score", 0) > 0:
            logger.debug(f"[LLM] {code} score={llm_result['score']} brief={llm_result['brief']}")
            cache[cache_key] = llm_result["score"]
            _save_llm_cache(cache)
            return llm_result["score"]

    # === 退回到规则引擎 ===
    return _rule_based_score(code)


def _rule_based_score(code: str) -> Optional[float]:
    """规则引擎评分（LLM 不可用时的退避方案）"""
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        return None

    comp = data.get('competitive_analysis', {})
    fin = data.get('financial_health', {})
    growth = data.get('growth_assessment', {})
    geo = data.get('geopolitical_assessment', {})
    biz = data.get('business_overview', {})
    metrics = fin.get('key_metrics', {})

    score = 0
    strengths = comp.get('strengths', [])
    weaknesses = comp.get('weaknesses', [])
    all_text = ' '.join(strengths)

    # 龙头关键词
    LEADERS = [r'龙头', r'第一', r'唯一', r'首家', r'全球.*前三', r'国内.*第一']
    TECH_MOAT = [r'自主可控', r'国产替代', r'央企', r'全产业链', r'IDM', r'垂直整合']
    CLIENTS = [r'谷歌', r'亚马逊', r'微软', r'华为', r'阿里', r'腾讯', r'特斯拉', r'英伟达', r'苹果']
    HOT = [r'AI芯片', r'半导体', r'创新药', r'新能源', r'光模块', r'数据中心', r'算力', r'机器人']

    def _match(pat_list):
        for p in pat_list:
            if re.search(p, all_text):
                return True
        return False

    # 1. 竞争优势 (0-18)
    moat_map = {'宽': 8, '中': 5, '窄': 3}
    score += moat_map.get(comp.get('moat_level', '窄'), 3)

    if _match(LEADERS):
        g = any(re.search(p, all_text) for p in [r'全球.*龙头', r'全球.*第一'])
        d = any(re.search(p, all_text) for p in [r'国内.*龙头', r'中国.*龙头', r'国内.*第一', r'国内.*唯一'])
        score += 4 if (g or d) else 3
    if _match(TECH_MOAT):
        score += 2
    if _match(CLIENTS):
        score += 1
    diff = len(strengths) - len(weaknesses)
    score += 2 if diff >= 2 else (1 if diff >= 0 else 0)
    score += 1 if sum(1 for s in strengths if any(c.isdigit() for c in s)) >= 3 else 0

    # 2. 财务质量 (0-14)
    score += {'健康': 6, '良好': 4, '一般': 2, '较差': 1}.get(fin.get('health_rating', '一般'), 2)

    roe = metrics.get('roe_pct', 0) or 0
    score += 3 if roe > 15 else (2 if roe > 10 else (1 if roe > 5 else (-1 if roe < 0 else 0)))
    nm = metrics.get('net_margin_pct', 0) or 0
    score += 2 if nm > 30 else (1 if nm > 10 else (-1 if nm < 0 else 0))
    gm = metrics.get('gross_margin_pct', 0) or 0
    score += 2 if gm > 60 else (1 if gm > 40 else 0)
    rd = metrics.get('rd_ratio_pct', 0) or 0
    score += 3 if rd > 15 else (2 if rd > 8 else (1 if rd > 3 else 0))
    rev = metrics.get('revenue_yi', 0) or 0
    score += 2 if rev > 200 else (1 if rev > 50 else 0)

    # 3. 成长性 (0-10)
    drivers = growth.get('growth_drivers', [])
    headwinds = growth.get('headwinds', [])
    score += 4 if len(drivers) > len(headwinds) else (3 if len(drivers) == len(headwinds) else 2)
    score += 2 if len(drivers) >= 3 else (1 if len(drivers) >= 1 else 0)
    momentum_kw = ('爆发', '加速', '突破', '翻倍', '量产', '放量', '激增')
    spec = sum(1 for d in drivers if any(kw in d for kw in momentum_kw))
    score += 2 if spec >= 2 else (1 if spec >= 1 else 0)
    gs = growth.get('growth_score', 5) or 5
    score += min(gs * 0.4, 2)

    # 4. 地缘/赛道 (0-8)
    ops = geo.get('opportunities', [])
    rks = geo.get('risks', [])
    score += 3 if len(ops) > len(rks) else (2 if len(ops) == len(rks) else 1)
    score += 1 if len(ops) >= 3 else 0
    score += 1 if sum(1 for o in ops if any(kw in o for kw in momentum_kw)) >= 1 else 0
    if _match(HOT):
        score += 2
    score += 1 if len(geo.get('industry_momentum', [])) >= 3 else 0

    return min(round(score), 50)
