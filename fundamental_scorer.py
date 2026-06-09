#!/usr/bin/env python3
"""
基本面知识评分 —— LLM 直读 fundamentals JSON 全文打分

两套 Prompt 并存：
  V1 (SCORING_PROMPT)    — AI 主线匹配度评分（0-50），偏市场主线
  V2 (SCORING_PROMPT_V2) — 双轨制评分（基本面25 + 赛道25），均衡评估

退避链：
  1. 读取 fundamentals/{code}.json
  2. 发送给 LLM 综合评分
  3. LLM 不可用时退回到规则引擎
  4. fundamentals 无文件 → None → 调用方退回到行业分

API 协议：自动适配 OpenAI Chat Completions 和 Anthropic Messages 两种协议
"""
import json
import logging
import os
import re
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

FUNDAMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fundamentals')
LLM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fundamental_llm_scores.json')

# ============ V1: AI 主线匹配度 Prompt ============

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

# ============ V2: 双轨制 Prompt（基本面 25 + 赛道 25）============

SCORING_PROMPT_V2 = """你是A股量化研究员，需要对一只股票进行双维度评分。

## 回测验证结论
对539只A股2025.12-2026.06的实证回测表明：
- **赛道动量分**与半年涨幅的Spearman秩相关达0.56（p<0.001），是唯一有效的选股信号
- **基本面质量分**与半年涨幅无显著相关（ρ=0.04），但可用作风险底线——排除亏损/财务危机/纯概念炒作股
- 赛道动量五等分，Q1（最低）→Q5（最高）涨幅单调递增：-1.6% → +1.1% → +13.8% → +61.8% → +134.8%

## 评分逻辑

你的核心任务是对**赛道动量**进行精确评分（这是Alpha信号）。
基本面评分的任务是**识别需要排除的垃圾股**（这是风控底线）。

### 第一部分：基本面质量（0-25分）— 风控底线
评估公司是否会暴雷，而非能否涨：

1. 是否有硬伤？(0-10分)：利润为负 = 0分；ROE<3% → 上限4分；净利率<5%且营收>千亿 → 上限5分。反之ROE>20%且净利率>20% → 8-10分
2. 护城河 (0-8分)：真龙头有定价权 → 6-8分；细分龙头 → 3-5分；无优势 → 0-2分
3. 财务安全 (0-7分)：低负债+现金流好 → 5-7分；合理 → 2-4分；危险 → 0-1分

**关键红线**：基本面<10分的股票应被排除，不参与赛道排名。

### 第二部分：赛道动量（0-25分）— Alpha信号
这是唯一有效的选股信号，需要最精细的判断：

1. 产业链位置 (0-10分)：从what_they_do和strengths判断真实业务，不要只看industry标签
   - AI算力核心供应商（光模块/PCB/先进封装/AI芯片/存储/HBM）→ 7-10分
   - 间接配套/消费电子/汽车电子 → 4-6分
   - 产业链外但独立成长逻辑清晰 → 1-3分
   - 传统行业无催化/旧赛道退潮（锂电/白酒/地产/矿业）→ 0分
2. 业绩兑现度 (0-10分)：
   - 有明确大客户（英伟达/华为/苹果/特斯拉等）+ 产能扩张+业绩高增 → 7-10分
   - 有客户但未放量/业绩增速一般 → 3-6分
   - 只有概念无订单/"国产替代/一带一路/政策红利"等空话 → 0-2分
3. 资金关注度 (0-5分)：
   - AI算力/光模块/先进封装/存储 → 4-5分
   - 消费电子复苏/汽车电子 → 2-3分
   - 冷门/资金流出 → 0-1分

注意：赛道分需要拉开区分度。全给10分或全给0分都没意义。光模块核心供应商给8-10分，PCB给6-8分，半导体设备给5-7分，边缘配套给2-4分。

非AI链优质公司（创新药、高端消费）如果独立成长逻辑清晰，产业链给1-3分，资金关注给1-2分。
旧赛道退潮品种（锂电/白酒/地产/传统矿业）产业链和资金关注度诚实给0分。

请输出严格JSON格式，不要解释：
{"fundamental_score": 整数0-25, "sector_score": 整数0-25, "brief": "一句话理由（中文，40字以内）"}

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
    """解析 LLM 响应，自动适配 V1 和 V2 两种 JSON 格式"""
    text = content.strip() if content and content.strip() else ""
    if not text and reasoning:
        m = re.search(r'\{[^{}]*"(?:score|total)"[^{}]*\}', reasoning)
        if m:
            text = m.group()
    if not text:
        return None

    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return None

    # 自动识别 V1 还是 V2 格式
    if "fundamental_score" in result or "sector_score" in result:
        # V2 格式
        return {
            "fundamental_score": result.get("fundamental_score"),
            "sector_score": result.get("sector_score"),
            "total": result.get("total", 0),
            "brief": result.get("brief", ""),
        }
    else:
        # V1 格式
        score = int(result.get("score", 0))
        return {"score": max(0, min(50, score)), "brief": result.get("brief", "")}


def _detect_api_protocol(base_url: str) -> str:
    """检测 API 协议类型"""
    if not base_url:
        return "openai"
    if "anthropic" in base_url.lower():
        return "anthropic"
    return "openai"


def _call_anthropic_messages(api_key: str, base_url: str, model: str, prompt: str) -> Optional[str]:
    """Anthropic Messages API 协议"""
    import urllib.request
    for url_tail in ["/messages", "/v1/messages"]:
        url = f"{base_url.rstrip('/')}{url_tail}"
        req_data = json.dumps({
            "model": model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=req_data, headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read())
                content = ""
                for block in raw.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                if not content:
                    choices = raw.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                return content
        except Exception as e:
            err = str(e)[:80]
            if "404" not in err and "405" not in err:
                logger.debug(f"Anthropic {url_tail}: {err}")
            continue
    return None


def _call_openai_chat(api_key: str, base_url: str, model: str, prompt: str) -> Optional[str]:
    """OpenAI Chat Completions 协议"""
    import urllib.request
    url = f"{base_url.rstrip('/')}/chat/completions"
    req_data = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 2048,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=req_data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read())
            msg = raw["choices"][0]["message"]
            content = msg.get("content", "") or ""
            return content
    except Exception as e:
        logger.debug(f"OpenAI: {e}")
        return None


def _call_llm_api(prompt_text: str) -> Optional[str]:
    """
    调用 LLM 返回原始文本内容。
    自动检测 API 协议（Anthropic / OpenAI），先试 openai 库再试 urllib。
    """
    api_key = os.environ.get("TA_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = (os.environ.get("TA_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    model = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "gpt-4o-mini"

    if not api_key:
        return None

    protocol = _detect_api_protocol(base_url)
    prompt = prompt_text[:12000]

    # 方式1：openai 库（OpenAI 协议）
    if protocol == "openai":
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
            if content:
                return content
            reasoning = getattr(msg, "reasoning_content", "") or ""
            if reasoning:
                return reasoning
        except ImportError:
            logger.debug("openai 库不可用，降级到 urllib")
        except Exception as e:
            logger.debug(f"openai 库调用失败: {e}，降级到 urllib")

    # 方式2：urllib 直调
    if protocol == "anthropic":
        return _call_anthropic_messages(api_key, base_url, model, prompt)
    else:
        return _call_openai_chat(api_key, base_url, model, prompt)


def _call_llm_score(data_json: str) -> Optional[dict]:
    """[V1] 调用 LLM 打分，返回 {"score": int, "brief": str} 或 None"""
    content = _call_llm_api(SCORING_PROMPT + data_json[:8000])
    if not content:
        return None
    result = _parse_llm_response(content)
    if result and "score" in result:
        return result
    return None


def _call_llm_score_v2(data_json: str) -> Optional[dict]:
    """[V2] 调用 LLM 打分，返回 {"fundamental_score", "sector_score", "total", "brief"} 或 None"""
    content = _call_llm_api(SCORING_PROMPT_V2 + data_json[:8000])
    if not content:
        return None
    result = _parse_llm_response(content)
    if result and "fundamental_score" in result:
        return result
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
    [V1] LLM 直读 fundamentals JSON，综合算出 0-50 的知识分。
    无文件返回 None，LLM 失败退回规则引擎。
    """
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None

    cache = _load_llm_cache()
    cache_key = f"{code}_{os.path.getmtime(path):.0f}_v4"
    if cache_key in cache:
        return cache[cache_key]

    stock_json = _build_stock_json(code)
    if stock_json:
        llm_result = _call_llm_score(stock_json)
        if llm_result and llm_result.get("score", 0) > 0:
            logger.debug(f"[V1 LLM] {code} score={llm_result['score']} brief={llm_result['brief']}")
            cache[cache_key] = llm_result["score"]
            _save_llm_cache(cache)
            return llm_result["score"]

    return _rule_based_score(code)


# 基本面风控底线：基本面分低于此值的股票不参与赛道排名
FUNDAMENTAL_MIN_THRESHOLD = 10


def compute_fundamental_knowledge_v2(code: str, name: str = None) -> Optional[Dict[str, Any]]:
    """
    [V2] 双轨制评分，返回 {"fundamental_score", "sector_score", "brief", "filter_pass"}。
    - sector_score (0-25): Alpha 信号，赛道动量，与涨幅显著正相关（ρ=0.56）
    - fundamental_score (0-25): 风控底线，<10 的股票建议排除
    - filter_pass: bool，基本面是否 ≥ 阈值（默认10）
    - 无文件返回 None，LLM 失败退回规则引擎。
    """
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None

    cache = _load_llm_cache()
    cache_key = f"{code}_{os.path.getmtime(path):.0f}_v3"
    if cache_key in cache:
        cached = cache[cache_key]
        if isinstance(cached, dict) and "sector_score" in cached:
            return cached

    stock_json = _build_stock_json(code)
    if stock_json:
        llm_result = _call_llm_score_v2(stock_json)
        if llm_result and (llm_result.get("sector_score") or 0) >= 0:
            fund_score = llm_result.get("fundamental_score") or 0
            sect_score = llm_result.get("sector_score") or 0
            result = {
                "fundamental_score": fund_score,
                "sector_score": sect_score,
                "brief": llm_result.get("brief", ""),
                "filter_pass": fund_score >= FUNDAMENTAL_MIN_THRESHOLD,
            }
            logger.debug(
                f"[V2 LLM] {code} fundamental={fund_score} sector={sect_score} "
                f"filter={'PASS' if result['filter_pass'] else 'FAIL'}"
            )
            cache[cache_key] = result
            _save_llm_cache(cache)
            return result

    # 回退：规则引擎只有 total，无法拆分
    rule_total = _rule_based_score(code)
    if rule_total is not None:
        return {"fundamental_score": None, "sector_score": None,
                "brief": "规则引擎（LLM 不可用）",
                "filter_pass": True}  # 规则引擎不排除
    return None


def compute_sector_alpha(code: str, name: str = None,
                         min_fundamental: int = None) -> Optional[Dict[str, Any]]:
    """
    赛道Alpha选股 —— 回测验证的推荐用法。

    逻辑：
    1. 用 V2 Prompt 评分
    2. 按 sector_score 排名（这是唯一有效的 Alpha 信号，ρ=0.56）
    3. fundamental_score < 阈值的排除（风控底线）
    4. 返回 sector_score 作为排名依据

    参数:
        min_fundamental: 基本面最低阈值，默认 FUNDAMENTAL_MIN_THRESHOLD (10)

    返回:
        {
            "sector_score": int,        # Alpha 信号 (0-25)，按此排名
            "fundamental_score": int,   # 风控分数 (0-25)
            "filter_pass": bool,        # 是否通过风控
            "brief": str,               # LLM 理由
            "recommendation": str,      # "BUY" | "WATCH" | "PASS"
        }
    """
    if min_fundamental is None:
        min_fundamental = FUNDAMENTAL_MIN_THRESHOLD

    v2 = compute_fundamental_knowledge_v2(code, name)
    if v2 is None:
        return None

    fund_score = v2.get("fundamental_score") or 0
    sect_score = v2.get("sector_score") or 0
    filter_pass = fund_score >= min_fundamental

    if not filter_pass:
        recommendation = "PASS"  # 基本面不达标，不参与
    elif sect_score >= 15:
        recommendation = "BUY"   # 高赛道动量
    elif sect_score >= 8:
        recommendation = "WATCH" # 中等赛道，关注
    else:
        recommendation = "PASS"  # 赛道动量不足

    return {
        "sector_score": sect_score,
        "fundamental_score": fund_score,
        "filter_pass": filter_pass,
        "brief": v2.get("brief", ""),
        "recommendation": recommendation,
    }


def compute_fundamental_knowledge_both(code: str, name: str = None) -> Optional[Dict[str, Any]]:
    """
    同时执行 V1 和 V2 评分，返回完整对比结果。

    返回格式:
    {
        "v1": {"score": int, "brief": str},
        "v2": {"fundamental_score", "sector_score", "brief", "filter_pass"},
        "rule": int,
        "recommendation": "BUY" | "WATCH" | "PASS",  # 基于赛道Alpha + 基本面过滤
    }
    """
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None

    stock_json = _build_stock_json(code)
    if not stock_json:
        return None

    v1_result = _call_llm_score(stock_json)
    v2_result = _call_llm_score_v2(stock_json)
    rule_score = _rule_based_score(code)

    # 从 V2 计算推荐
    if v2_result:
        fund = v2_result.get("fundamental_score") or 0
        sect = v2_result.get("sector_score") or 0
        if fund < FUNDAMENTAL_MIN_THRESHOLD:
            rec = "PASS"
        elif sect >= 15:
            rec = "BUY"
        elif sect >= 8:
            rec = "WATCH"
        else:
            rec = "PASS"
        filter_pass = fund >= FUNDAMENTAL_MIN_THRESHOLD
    else:
        rec = "WATCH"
        filter_pass = True

    return {
        "v1": v1_result if v1_result else {"score": rule_score, "brief": "LLM 不可用，回退规则引擎"},
        "v2": v2_result if v2_result else {
            "fundamental_score": None, "sector_score": None,
            "brief": "LLM 不可用，回退规则引擎", "filter_pass": True,
        },
        "rule": rule_score,
        "recommendation": rec,
    }


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
    moat_map = {'宽': 8, '高': 8, '中': 5, '窄': 3}
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
