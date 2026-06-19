#!/usr/bin/env python3
"""从 _top500_and_leaders.txt 批量生成基本面文件

用法:
  uv run python3 _gen_top500_fundamentals.py                    # 生成所有未完成
  uv run python3 _gen_top500_fundamentals.py --codes 600019,600188  # 只生成指定股票
  uv run python3 _gen_top500_fundamentals.py --count 10         # 只生成前N只
  uv run python3 _gen_top500_fundamentals.py --dry-run          # 只打印要生成的列表
  uv run python3 _gen_top500_fundamentals.py --force            # 强制重新生成（覆盖已有文件）
  uv run python3 _gen_top500_fundamentals.py --start-from 600019  # 从指定代码开始（跳过之前的）
  uv run python3 _gen_top500_fundamentals.py --force --workers 4  # 并行4线程 (LLM为IO密集, 适合线程池)
"""
import json
import os
import re
import sys
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, List, Dict, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from picker import paths

FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR
WORLD_KNOWLEDGE_FILE = paths.WORLD_KNOWLEDGE_MD
TOP500_FILE = paths.TOP500_AND_LEADERS

# 并行写 _top500_and_leaders.txt (mark_stock_done) 的文件锁
_DONE_LOCK = threading.Lock()


# ========== 解析 _top500_and_leaders.txt ==========

def parse_top500_file(filepath: str) -> List[Dict]:
    """解析 _top500_and_leaders.txt，返回股票列表
    
    每行格式: "    1. 601939 建设银行       银行       (   26736亿) [DONE]"
    """
    stocks = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 匹配: 序号. 代码 名称 行业 (市值) [DONE]
            m = re.match(
                r'\d+\.\s+(\d{6})\s+(\S+)\s+(\S+)\s+\(\s*(\d+)亿\)\s*(\[DONE\])?',
                line
            )
            if m:
                code = m.group(1)
                name = m.group(2)
                industry = m.group(3)
                mcap = int(m.group(4))
                done = m.group(5) is not None
                stocks.append({
                    "code": code,
                    "name": name,
                    "industry": industry,
                    "mcap_yi": mcap,
                    "done": done,
                })
    return stocks


def mark_stock_done(filepath: str, code: str) -> bool:
    """在 _top500_and_leaders.txt 中标记某股票为 [DONE]

    并行安全: 用全局 _DONE_LOCK 保护读-改-写操作。
    """
    with _DONE_LOCK:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # 查找该股票的行，在行尾添加 [DONE]
        # 匹配: 代码 名称 ... (市值) 后面没有 [DONE] 的情况
        pattern = rf'({code}\s+\S+\s+\S+\s+\(\s*\d+亿\))\s*$'
        replacement = rf'\1 [DONE]'

        new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)

        if count > 0:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)
            return True
        else:
            # 可能已经有 [DONE] 了
            return False


# ========== 加载世界知识 ==========

def load_world_knowledge() -> str:
    """读取世界知识文档"""
    if not os.path.exists(WORLD_KNOWLEDGE_FILE):
        logger.warning(f"世界知识文件不存在: {WORLD_KNOWLEDGE_FILE}")
        return ""
    with open(WORLD_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
        return f.read()


# ========== 加载参考文件 ==========

def load_reference_fundamentals() -> str:
    """加载已完成的参考基本面文件，作为 prompt 中的示例"""
    ref_codes = ["002281", "605117"]  # 高质量参考文件
    refs = []
    for code in ref_codes:
        path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            refs.append(f"## 参考示例: {data.get('name', code)} ({code})\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)[:3000]}\n```")
    return "\n\n".join(refs)


# ========== LLM 调用 ==========

def _get_llm_config() -> Tuple[str, str, str]:
    """获取 LLM 配置: (api_key, base_url, model)"""
    api_key = os.environ.get("TA_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("TA_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("TA_LLM_DEEP") or os.environ.get("TA_LLM_QUICK") or "gpt-4o"
    return api_key, base_url, model


def call_llm(prompt: str, max_tokens: int = 8192, temperature: float = 0.3) -> Optional[str]:
    """调用 LLM，返回文本响应"""
    api_key, base_url, model = _get_llm_config()
    if not api_key:
        logger.error("无 LLM API key，请设置 TA_API_KEY 或 OPENAI_API_KEY")
        return None

    # 方式1: openai 库
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url or None)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=180,
        )
        content = resp.choices[0].message.content or ""
        return content.strip()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"openai 库调用失败: {e}，尝试 urllib")

    # 方式2: urllib 直调
    try:
        import urllib.request
        req_data = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = json.loads(resp.read())
            return raw["choices"][0]["message"].get("content", "").strip()
    except Exception as e:
        logger.error(f"urllib 调用也失败: {e}")
        return None


# ========== JSON 清理 ==========

def clean_json_text(text: str) -> str:
    """清理 LLM 输出中的 JSON 文本"""
    if "```" in text:
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1)
    text = text.replace("\u201c", "'").replace("\u201d", "'")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u300a", "<").replace("\u300b", ">")
    return text.strip()


def parse_json_response(text: str) -> Optional[dict]:
    """解析 LLM 响应为 JSON dict"""
    text = clean_json_text(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败，前200字符: {text[:200]}")
            return None


def validate_fundamental(data: dict) -> bool:
    """验证 fundamentals JSON 结构完整性"""
    required_keys = ["code", "name", "business_overview", "competitive_analysis",
                     "financial_health", "growth_assessment", "geopolitical_assessment", "summary"]
    for key in required_keys:
        if key not in data:
            logger.warning(f"缺少字段: {key}")
            return False

    bo = data.get("business_overview", {})
    if not bo.get("what_they_do"):
        logger.warning("business_overview.what_they_do 为空")
        return False

    ca = data.get("competitive_analysis", {})
    if not ca.get("strengths") or not ca.get("weaknesses"):
        logger.warning("competitive_analysis.strengths 或 weaknesses 为空")
        return False

    # 检查中文引号残留
    text = json.dumps(data, ensure_ascii=False)
    if "\u201c" in text or "\u201d" in text:
        logger.warning("JSON 中包含中文引号，需要清理")
        # 自动清理
        data_str = json.dumps(data, ensure_ascii=False)
        data_str = data_str.replace("\u201c", "'").replace("\u201d", "'")
        data.update(json.loads(data_str))

    return True


# ========== Prompt 构建 ==========

SYSTEM_PROMPT = """你是一位资深的A股研究总监，擅长将世界知识、行业趋势、财务数据和地缘政治分析融合为结构化的股票基本面世界知识。

你的输出将用于辅助量化系统对未来30天股价走势的判断，因此必须：
1. 具体而非泛泛：用数据、客户名、市占率、价格等具体事实，而非'行业领先'等空话
2. 前瞻而非回顾：重点分析未来30天可能影响股价的催化因素和风险
3. 结合世界局势：将伊朗战争、AI革命、中美贸易战等宏观事件与个股具体业务关联
4. 诚实客观：劣势和风险必须真实，不能只写好话

重要格式要求：
- 所有文本使用中文
- 不要使用中文引号（""''），如需引用请用英文单引号''
- 输出严格的 JSON 格式，不要有其他文字
- 财务数据必须精确（如"营收119.3亿+44.2%"），不要用"约"、"超"等模糊词
- 竞争对手必须具名并含市占率/排名
- 风险因素必须含具体数据点和潜在影响

【信源可信度分级与防污染规则 — 强制执行】
你掌握的信息信源参差不齐，必须对每条"强断言"标注信源可信度，并按规则处理，防止低质信源污染基本面数据。

信源分级标准：
- [信源:高] = 公司公告/财报/招股书、券商深度研报、权威媒体(上证报/证券时报/路透/彭博)、监管文件。这类数据可作为硬事实。
- [信源:中] = 行业媒体/产业数据库(Prismark/TrendForce等)、券商晨会简报、公司投资者关系记录。可信但需交叉验证。
- [信源:低] = 雪球/股吧/东方财富号/今日头条等自媒体、博主个人观点、网络传言、你基于行业常识的推测。

强断言的标注与处理规则（覆盖 strengths/growth_drivers/summary 等所有字段）：
1. 凡含以下"强断言触发词"的表述，必须在该条【开头】标注 [信源:高/中/低]：
   触发词：'一供/一供份额/独家供应/锁定/已锁定/已认证/份额XX%/唯一供应商/独家/首批/核心供应商/驻场'。
   示例："[信源:低]锁定英伟达Rubin Midplane一供份额40%+"、"[信源:高]2025年营收153.08亿(+20.92%)"。
2. 【删除规则 — 严格执行】若一条强断言满足以下任一条件，必须从输出中【整条删除】，不得保留、不得降级表述：
   (a) 信源为"低"，且与更高信源(高/中)的事实存在矛盾或被其削弱；
   (b) 信源为"低"，且该断言是公司核心多头逻辑的关键支点(一旦失实会显著扭曲基本面判断)；
   (c) 该事件当前仍处于"送样测试/验证/规划/预期"阶段，却被表述为已确定事实(如把"送样测试中"写成"已锁定一供")。
3. 财务数据(营收/净利/毛利率/ROE等出自财报)默认 [信源:高]，无需每条都标，但 strengths/weaknesses/growth_drivers 中的非财报强断言必须标注。
4. 交叉验证意识：当不同信源对同一事实(如份额/排名/认证状态)给出不同数字时，以更高信源为准；若高信源与低信源冲突，按规则2删除低信源断言，不得折中。
5. 宁缺毋滥：无法判定信源或信源可疑的强断言，宁可删除也不要写入。基本面数据被"乐观但失实"的自媒体叙事污染，比信息略少危害更大。"""


def build_prompt(code: str, name: str, industry: str, mcap_yi: float,
                 world_knowledge: str, reference: str,
                 real_financials: dict = None, industry_research: str = "") -> str:
    """构建生成 fundamentals 的 prompt

    新增两源注入 (解决财务靠LLM回忆不准 + 研报完全未注入的问题):
    - real_financials: Tushare 真实财报 dict, 直接填 key_metrics, 不让LLM推测
    - industry_research: research.db 板块研报文本, 标注[信源:中·板块级]供LLM参考
    """
    
    # 截取世界知识（避免 prompt 过长）
    wk_text = world_knowledge[:8000] if len(world_knowledge) > 8000 else world_knowledge

    # ── 权威财报段 (Tushare, 直接填入) ──
    fin_section = ""
    if real_financials:
        ann = real_financials.get("_ann_period", "")
        fin_clean = {k: v for k, v in real_financials.items() if not k.startswith("_")}
        fin_json = json.dumps(fin_clean, ensure_ascii=False, indent=2)
        fin_section = f"""
## ⚠️ 权威财报数据 (来自Tushare, 财报期 {ann})
以下财务数据【必须原样填入 financial_health.key_metrics】, 不得修改、不得用你的记忆替换、不得推测。
```json
{fin_json}
```
（revenue_yi/net_profit_yi/operating_cf_yi 单位:亿元; 比率类为百分比。rd_ratio_pct/rd_expense_yi Tushare未提供, 可填null。)
"""

    # ── 板块研报段 (research.db, 信源中) ──
    research_section = ""
    if industry_research:
        research_section = f"""
## 博主研报信号 (板块级)
{industry_research}
（以上是该公司所在板块的研报观点, 信源可信度: 中。可参考其行业趋势/数据, 但【不得】据此虚构该股的份额/认证/订单等个股级强断言; 个股级强断言仍须遵循信源分级与防污染规则。)
"""

    prompt = f"""请为以下股票生成完整的基本面世界知识 JSON 文件。

## 股票信息
- 代码: {code}
- 名称: {name}
- 行业: {industry}
- 市值: {mcap_yi}亿元
{fin_section}{research_section}
## 当前世界知识（2026年6月）
{wk_text}

## 输出格式要求
请输出严格 JSON，结构如下（所有字段必填）：

```json
{{
  "code": "{code}",
  "name": "{name}",
  "fetch_date": "{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
  "market": "{"沪市" if code.startswith("6") else "深市"}",
  "business_overview": {{
    "what_they_do": "该公司真正在做什么业务，核心产品/服务，主要客户，技术特点，含具体财务数据（营收/增速/占比）。200-400字",
    "industry": "行业分类（细分行业）",
    "industry_position": "在行业中的真实地位，用市占率/排名/与竞争对手对比的数据说话"
  }},
  "competitive_analysis": {{
    "strengths": ["5条具体优势，每条含数据支撑（市占率/客户名/技术指标/财务数据）。含'锁定/一供/份额/独家/唯一'等强断言的条目，开头必须加[信源:高/中/低]标注"],
    "weaknesses": ["5条具体劣势，每条含数据（毛利率差距/客户集中度/技术短板等）"],
    "moat_level": "低/中/中高/高"
  }},
  "financial_health": {{
    "key_metrics": {{
      "revenue_yi": 0.0,
      "net_profit_yi": 0.0,
      "gross_margin_pct": 0.0,
      "net_margin_pct": 0.0,
      "roe_pct": 0.0,
      "debt_ratio_pct": 0.0,
      "rd_ratio_pct": 0.0,
      "rd_expense_yi": 0.0,
      "operating_cf_yi": 0.0,
      "cf_to_profit": 0.0
    }},
    "health_rating": "健康/一般/较差",
    "benchmark_ref": "行业基准",
    "highlights": ["4条财务亮点，含具体数据"],
    "risks": ["4条财务风险，含具体数据"]
  }},
  "growth_assessment": {{
    "growth_score": 0.0,
    "growth_drivers": ["5条增长驱动，结合世界局势。含'锁定/一供/份额/独家/量产'等强断言的条目，开头必须加[信源:高/中/低]标注；信源低且与他源矛盾者删除"],
    "headwinds": ["5条增长阻力，含具体数据"]
  }},
  "geopolitical_assessment": {{
    "risks": ["4条地缘风险，必须引用世界知识中的具体数据"],
    "opportunities": ["4条地缘机会，必须引用世界知识中的具体数据"],
    "industry_momentum": ["3条行业趋势"]
  }},
  "summary": "200-300字总结，格式：<公司>是<定位>。<核心财务数据>。核心优势：①...②...③...。主要风险：①...②...③...。<增长展望>。"
}}
```

## 关键质量要求（不可妥协）
1. **财务数据必须精确**：如"2025年营收119.3亿（+44.2%）"、"毛利率51.1%"，不要用"约"、"超"等模糊词
2. **竞争对手必须具名**：如"与中际旭创（全球第一28%）、新易盛（全球第二15-18%）同列第一阵营"，不要写"行业竞争激烈"
3. **风险因素必须个性化**：如"存货74.8亿激增（+30%），跌价准备3.1亿，若需求放缓风险巨大"，不要写"行业竞争加剧"
4. **地缘评估必须引用世界知识**：伊朗战争（布伦特$93.09/桶）、中美贸易战（加权平均实际税率21.6%）、AI革命（全球数据中心投资7880亿美元+56%）、新能源（中国渗透率62.5%）、稀土管制（对日断供，日本库存8-10月见底）
5. **业务概述必须数据丰富**：含分业务营收/增速/占比/毛利率
6. **增长评分参考**：AI算力/光模块/半导体设备 8-9分，新能源/军工/医药 7-8分，消费/银行/电力 5-6分，旧赛道退潮 4-5分
7. **供应链份额/认证类断言必须防污染**（极易出错的高危领域，强制执行信源规则）：
   - 供应链地位、份额、认证状态的信源质量差异极大，必须严格区分。券商供应链调查/分析师(如郭明錤)纪要 > 公司公告 > 行业媒体 > 雪球/股吧/自媒体。
   - 特别警惕"把送样/测试/规划阶段写成已确定事实"：如"正在送样测试中"不可写成"已锁定一供"，"预计26Q1量产"不可写成"26Q1大规模量产已确认"。
   - 特别警惕"份额数字单一信源"：若"份额40%+"只来自雪球/自媒体而无券商研报佐证，且券商调查给出更小份额(如10%)，必须删除高份额断言。
   - 若某条供应链断言是公司核心多头逻辑的支点，且信源低或与他源矛盾，宁可删除也不要保留——一个失实的"一供"支点会严重扭曲量化判断。

{reference}

请直接输出 JSON，不要有任何其他文字。"""

    return prompt


# ========== 主流程 ==========

def generate_one(code: str, name: str, industry: str, mcap_yi: float,
                 world_knowledge: str, reference: str,
                 max_retries: int = 2) -> Optional[dict]:
    """生成单只股票的 fundamentals

    生成前先拉两源真实数据注入 prompt:
    - Tushare 真实财报 (financial_health.key_metrics 准确性保障)
    - research.db 板块研报 (冷门股也能获得板块视角)
    拉取失败时回退到纯LLM生成 (退化到原行为, 不会崩)。
    """
    # ── 拉取真实财报 (Tushare) ──
    real_financials = None
    try:
        from picker.data.fundamentals_data import fetch_real_financials
        real_financials = fetch_real_financials(code)
        if real_financials:
            logger.info(f"  ✓ Tushare 财报已注入 (营收{real_financials.get('revenue_yi')}亿)")
    except Exception as e:
        logger.warning(f"  Tushare 财报拉取失败, 回退LLM填: {type(e).__name__}")

    # ── 拉取板块研报 (research.db) ──
    # industry 来自 _top500 粗分类(如"元器件"), 太粗无法匹配细分板块(如PCB)。
    # 故用 name + 已有 fundamentals 的细 industry 组合作为匹配文本, 提升召回。
    industry_research = ""
    try:
        from tradingagents.research.consumer import get_industry_research_brief
        match_text = industry or ""
        if name:
            match_text = f"{match_text} {name}"
        # 若 fundamentals 已存在(增量更新场景), 读取其细 industry 补充
        exist_path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
        if os.path.exists(exist_path):
            try:
                with open(exist_path) as f:
                    ed = json.load(f)
                fine_ind = ed.get("business_overview", {}).get("industry", "")
                if fine_ind:
                    match_text = f"{match_text} {fine_ind}"
            except Exception:
                pass
        industry_research = get_industry_research_brief(match_text)
        if industry_research:
            logger.info(f"  ✓ 板块研报已注入 ({len(industry_research)}字符)")
    except Exception as e:
        logger.warning(f"  板块研报拉取失败: {type(e).__name__}")

    prompt = build_prompt(code, name, industry, mcap_yi, world_knowledge, reference,
                          real_financials=real_financials,
                          industry_research=industry_research)

    for attempt in range(max_retries + 1):
        logger.info(f"  调用 LLM 生成 {code} {name} (尝试 {attempt+1}/{max_retries+1})")
        response = call_llm(prompt, max_tokens=8192, temperature=0.3)
        if not response:
            logger.warning(f"  LLM 无响应")
            continue

        data = parse_json_response(response)
        if not data:
            logger.warning(f"  JSON 解析失败")
            continue

        # 补全必要字段
        data.setdefault("code", code)
        data.setdefault("name", name)
        data.setdefault("fetch_date", datetime.now().strftime('%Y-%m-%dT%H:%M:%S'))
        data.setdefault("market", "沪市" if code.startswith("6") else "深市")

        # 确保 financial_health.key_metrics 存在
        fh = data.setdefault("financial_health", {})
        km = fh.setdefault("key_metrics", {})
        for k in ["revenue_yi", "net_profit_yi", "gross_margin_pct", "net_margin_pct",
                   "roe_pct", "debt_ratio_pct", "rd_ratio_pct", "rd_expense_yi",
                   "operating_cf_yi", "cf_to_profit"]:
            km.setdefault(k, None)

        if validate_fundamental(data):
            return data
        else:
            logger.warning(f"  验证失败，重试...")

    logger.error(f"  {code} {name} 生成失败（已重试 {max_retries+1} 次）")
    return None


def _build_fh_prompt(code: str, name: str, old_fh: dict) -> str:
    """构建只更新 financial_health 的 prompt (慢层增量, 比全量便宜很多)。"""
    import json as _json
    return f"""请更新以下股票的最新财务健康数据 (financial_health)。

## 股票
- 代码: {code}
- 名称: {name}

## 当前已有的 financial_health (可能已过时, 请基于你掌握的最新财报数据更新)
{_json.dumps(old_fh, ensure_ascii=False, indent=2)}

## 要求
- 只输出 financial_health 一个对象, 不要输出其他字段。
- key_metrics 用最新一期财报数据 (营收/净利/毛利率/净利率/ROE/负债率/研发/经营现金流等)。
- highlights/risks 各 4 条, 必须含具体数据。
- 数据未知用 null, 不要编造。

严格输出 JSON (只含 financial_health 对象):
```json
{{
  "key_metrics": {{
    "revenue_yi": 0.0, "net_profit_yi": 0.0, "gross_margin_pct": 0.0,
    "net_margin_pct": 0.0, "roe_pct": 0.0, "debt_ratio_pct": 0.0,
    "rd_ratio_pct": 0.0, "rd_expense_yi": 0.0, "operating_cf_yi": 0.0,
    "cf_to_profit": 0.0, "eps": 0.0
  }},
  "health_rating": "健康/一般/较差",
  "benchmark_ref": "行业基准",
  "highlights": ["4条财务亮点, 含数据"],
  "risks": ["4条财务风险, 含数据"]
}}
```
"""


def _run_fh_only(codes_arg, count: int, dry_run: bool):
    """只刷新 fundamentals JSON 的 financial_health 字段, 保留其余部分不变。

    用于慢层定期增量更新财务数据 (全量重写仍走默认流程, 手动执行)。
    """
    files = sorted(f[:-5] for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith(".json"))
    if codes_arg:
        files = [c for c in files if c in codes_arg]
    if count > 0:
        files = files[:count]
    logger.info(f"[fh-only] 待刷新 financial_health: {len(files)} 只")

    if dry_run:
        for c in files[:50]:
            print(f"  {c}")
        if len(files) > 50:
            print(f"  ... 还有 {len(files) - 50} 只")
        return

    api_key, _, model = _get_llm_config()
    if not api_key:
        logger.error("未配置 LLM API key")
        sys.exit(1)

    success = fail = 0
    for i, code in enumerate(files):
        path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            fail += 1
            logger.warning(f"  ✗ {code} 读取失败: {e}")
            continue

        name = data.get("name", "")
        old_fh = data.get("financial_health", {}) or {}
        logger.info(f"[{i+1}/{len(files)}] {code} {name} 刷新 financial_health | "
                     f"成功:{success} 失败:{fail}")

        resp = call_llm(_build_fh_prompt(code, name, old_fh), max_tokens=4096)
        new_fh = parse_json_response(resp) if resp else None
        # 容错: LLM 可能把对象包在 financial_health 键里
        if isinstance(new_fh, dict) and "financial_health" in new_fh:
            new_fh = new_fh["financial_health"]
        if not isinstance(new_fh, dict) or "key_metrics" not in new_fh:
            fail += 1
            logger.warning(f"  ✗ {code} 解析失败, 保留原数据")
            continue

        data["financial_health"] = new_fh
        data["fh_updated"] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        success += 1
        logger.info(f"  ✓ {code} financial_health 已更新")
        time.sleep(0.5)

    logger.info(f"\n[fh-only] 完成: 成功 {success}, 失败 {fail}")


def main():
    # 解析参数
    codes_arg = None
    force = False
    count = 0
    dry_run = False
    start_from = None
    fh_only = False
    workers = 1

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("--codes", "--code") and i + 1 < len(sys.argv):
            i += 1
            codes_arg = [c.strip() for c in sys.argv[i].split(",") if c.strip()]
        elif arg.startswith("--codes="):
            codes_arg = [c.strip() for c in arg.split("=", 1)[1].split(",") if c.strip()]
        elif arg == "--force":
            force = True
        elif arg == "--fh-only":
            fh_only = True
        elif arg in ("--count", "-n") and i + 1 < len(sys.argv):
            i += 1
            count = int(sys.argv[i])
        elif arg.startswith("--count="):
            count = int(arg.split("=", 1)[1])
        elif arg == "--dry-run":
            dry_run = True
        elif arg in ("--start-from",) and i + 1 < len(sys.argv):
            i += 1
            start_from = sys.argv[i]
        elif arg.startswith("--start-from="):
            start_from = arg.split("=", 1)[1]
        elif arg in ("--workers", "-w") and i + 1 < len(sys.argv):
            i += 1
            workers = max(1, int(sys.argv[i]))
        elif arg.startswith("--workers="):
            workers = max(1, int(arg.split("=", 1)[1]))
        i += 1

    # ── fh-only: 只刷新已有 fundamentals 的 financial_health 部分 (慢层增量更新) ──
    if fh_only:
        return _run_fh_only(codes_arg, count, dry_run)


    # 加载资源
    logger.info("解析 _top500_and_leaders.txt...")
    all_stocks = parse_top500_file(TOP500_FILE)
    logger.info(f"  总计: {len(all_stocks)} 只股票, 已完成: {sum(1 for s in all_stocks if s['done'])} 只")

    logger.info("加载世界知识...")
    world_knowledge = load_world_knowledge()
    logger.info(f"  世界知识: {len(world_knowledge)} 字符")

    logger.info("加载参考文件...")
    reference = load_reference_fundamentals()
    logger.info(f"  参考文件: {len(reference)} 字符")

    # 确定要生成的股票列表
    if codes_arg:
        stocks = [s for s in all_stocks if s["code"] in codes_arg]
        if not stocks:
            logger.error(f"未找到指定代码: {codes_arg}")
            sys.exit(1)
    else:
        stocks = all_stocks

    # start_from: 跳过指定代码之前的所有股票
    if start_from:
        found = False
        for idx, s in enumerate(stocks):
            if s["code"] == start_from:
                stocks = stocks[idx:]
                found = True
                break
        if not found:
            logger.warning(f"未找到 start-from 代码: {start_from}")

    # 过滤已完成的（除非 --force）
    os.makedirs(FUNDAMENTALS_DIR, exist_ok=True)
    existing_files = set()
    if not force:
        for f in os.listdir(FUNDAMENTALS_DIR):
            if f.endswith(".json"):
                existing_files.add(f.replace(".json", ""))

    need_generate = []
    for s in stocks:
        code = s["code"]
        if force or (not s["done"] and code not in existing_files):
            need_generate.append(s)

    logger.info(f"待生成: {len(need_generate)} 只 (已完成: {len(stocks) - len(need_generate)})")

    if dry_run:
        for s in need_generate[:50]:
            print(f"  {s['code']} {s['name']:10s} {s['industry']:10s} ({s['mcap_yi']}亿)")
        if len(need_generate) > 50:
            print(f"  ... 还有 {len(need_generate) - 50} 只")
        return

    if not need_generate:
        logger.info("全部已是最新，无需生成")
        return

    # 检查 LLM 配置
    api_key, base_url, model = _get_llm_config()
    if not api_key:
        logger.error("未配置 LLM API key，请设置 TA_API_KEY 或 OPENAI_API_KEY")
        sys.exit(1)
    logger.info(f"LLM 配置: model={model}, base_url={base_url or 'default'}")

    # 限制数量
    if count > 0:
        need_generate = need_generate[:count]
        logger.info(f"限制生成数量: {count}")

    # 逐个生成 (串行 / 并行)
    success = 0
    fail = 0
    start_time = time.time()
    n_total = len(need_generate)

    # 单只生成的 worker 函数 (无共享状态, 线程安全)
    def _gen(stock):
        code = stock["code"]
        name = stock["name"]
        industry = stock["industry"]
        mcap_yi = stock["mcap_yi"]
        data = generate_one(code, name, industry, mcap_yi, world_knowledge, reference)
        if data:
            # 写入 JSON 文件 (按 code 隔离, 无冲突)
            path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            mark_stock_done(TOP500_FILE, code)  # 内部有 _DONE_LOCK
        return code, name, data

    if workers <= 1:
        # ── 串行模式 (原逻辑) ──
        for i, stock in enumerate(need_generate):
            code = stock["code"]; name = stock["name"]
            elapsed = time.time() - start_time
            avg = elapsed / (success + fail) if (success + fail) > 0 else 0
            remaining = (n_total - i) * avg
            logger.info(f"[{i+1}/{n_total}] {code} {name:10s} | "
                        f"成功:{success} 失败:{fail} | 预计剩余:{remaining/60:.1f}min")
            try:
                _, _, data = _gen(stock)
                if data:
                    success += 1
                    logger.info(f"  ✓ {code} {name} 已生成并标记 [DONE]")
                else:
                    fail += 1
                    logger.warning(f"  ✗ {code} {name} 生成失败")
            except Exception as e:
                fail += 1
                logger.error(f"  ✗ {code} {name} 异常: {e}")
            time.sleep(0.5)
    else:
        # ── 并行模式 (ThreadPoolExecutor, LLM 为 IO 密集) ──
        logger.info(f"并行模式: {workers} workers")
        done_cnt = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_gen, s): s for s in need_generate}
            for fut in as_completed(futures):
                stock = futures[fut]
                code = stock["code"]; name = stock["name"]
                done_cnt += 1
                elapsed = time.time() - start_time
                avg = elapsed / done_cnt
                remaining = (n_total - done_cnt) * avg / workers
                try:
                    _, _, data = fut.result()
                    if data:
                        success += 1
                        logger.info(f"[{done_cnt}/{n_total}] ✓ {code} {name:10s} | "
                                    f"成功:{success} 失败:{fail} | 预计剩余:{remaining/60:.1f}min")
                    else:
                        fail += 1
                        logger.warning(f"[{done_cnt}/{n_total}] ✗ {code} {name:10s} 生成失败")
                except Exception as e:
                    fail += 1
                    logger.error(f"[{done_cnt}/{n_total}] ✗ {code} {name:10s} 异常: {e}")

    elapsed = time.time() - start_time
    logger.info(f"\n=== 完成 ===")
    logger.info(f"成功: {success}, 失败: {fail}")
    logger.info(f"总耗时: {elapsed/60:.1f} 分钟")


if __name__ == "__main__":
    main()
