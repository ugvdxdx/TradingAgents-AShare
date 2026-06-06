#!/usr/bin/env python3
"""LLM 驱动的 fundamentals 生成器

基于 _world_knowledge_2026_06.md 世界知识 + _regen_fundamentals.py 中的 STOCK_KNOWLEDGE，
调用 LLM 生成高质量、有深度的股票基本面世界知识 JSON 文件。

用法:
  uv run python3 _gen_fundamentals_llm.py                      # 生成所有缺失的
  uv run python3 _gen_fundamentals_llm.py --codes 002594,300750  # 只生成指定股票
  uv run python3 _gen_fundamentals_llm.py --force                # 强制重新生成
  uv run python3 _gen_fundamentals_llm.py --count 20             # 只生成前N只
  uv run python3 _gen_fundamentals_llm.py --dry-run              # 只打印要生成的列表
"""
import json
import os
import re
import sys
import time
import logging
from datetime import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDAMENTALS_DIR = os.path.join(SCRIPT_DIR, "fundamentals")
WORLD_KNOWLEDGE_FILE = os.path.join(SCRIPT_DIR, "_world_knowledge_2026_06.md")
STOCK_WHITELIST_FILE = os.path.join(SCRIPT_DIR, "stock_whitelist.json")

# ========== 加载世界知识 ==========

def load_world_knowledge() -> str:
    """读取世界知识文档"""
    if not os.path.exists(WORLD_KNOWLEDGE_FILE):
        logger.warning(f"世界知识文件不存在: {WORLD_KNOWLEDGE_FILE}")
        return ""
    with open(WORLD_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
        return f.read()


def load_stock_knowledge() -> dict:
    """从 _regen_fundamentals.py 中提取 STOCK_KNOWLEDGE 字典"""
    try:
        path = os.path.join(SCRIPT_DIR, "_regen_fundamentals.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        # 找到 STOCK_KNOWLEDGE = { ... } 的位置
        start = source.find("STOCK_KNOWLEDGE = {")
        if start < 0:
            logger.warning("未找到 STOCK_KNOWLEDGE 定义")
            return {}

        # 提取从 { 开始的完整字典文本
        brace_start = source.index("{", start)
        depth = 0
        i = brace_start
        while i < len(source):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1

        dict_text = source[brace_start:i + 1]

        # 用 eval 解析（仅包含字面量，安全）
        stock_knowledge = eval(dict_text)
        logger.info(f"从源码提取 STOCK_KNOWLEDGE: {len(stock_knowledge)} 只股票")
        return stock_knowledge
    except Exception as e:
        logger.warning(f"提取 STOCK_KNOWLEDGE 失败: {e}")
        return {}


def load_stock_whitelist() -> list:
    """加载股票白名单"""
    if not os.path.exists(STOCK_WHITELIST_FILE):
        return []
    with open(STOCK_WHITELIST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ========== LLM 调用 ==========

def _get_llm_config() -> tuple:
    """获取 LLM 配置: (api_key, base_url, model)"""
    api_key = os.environ.get("TA_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("TA_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("TA_LLM_DEEP") or os.environ.get("TA_LLM_QUICK") or "gpt-4o"
    return api_key, base_url, model


def call_llm(prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> Optional[str]:
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
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=120,
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
            "messages": [{"role": "user", "content": prompt}],
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read())
            return raw["choices"][0]["message"].get("content", "").strip()
    except Exception as e:
        logger.error(f"urllib 调用也失败: {e}")
        return None


# ========== JSON 清理 ==========

def clean_json_text(text: str) -> str:
    """清理 LLM 输出中的 JSON 文本"""
    # 去掉 markdown 代码块包裹
    if "```" in text:
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1)

    # 替换中文引号为英文单引号（避免 JSON 解析错误）
    text = text.replace("\u201c", "'").replace("\u201d", "'")  # "" → ''
    text = text.replace("\u2018", "'").replace("\u2019", "'")  # '' → '
    text = text.replace("\u300a", "<").replace("\u300b", ">")  # 《》 → <>

    return text.strip()


def parse_json_response(text: str) -> Optional[dict]:
    """解析 LLM 响应为 JSON dict"""
    text = clean_json_text(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试修复常见的 JSON 错误
        # 去掉尾部逗号
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

    # 检查关键子字段
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
        return False

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
- 输出严格的 JSON 格式，不要有其他文字"""


def build_prompt(code: str, name: str, industry: str, stock_knowledge: dict,
                 world_knowledge: str, total_mv: float = 0) -> str:
    """构建生成 fundamentals 的 prompt"""

    sk = stock_knowledge.get(code, {})
    sk_industry = sk.get("industry", industry) or industry
    what_they_do = sk.get("what_they_do", "")

    # 截取世界知识（避免 prompt 过长）
    wk_text = world_knowledge[:6000] if len(world_knowledge) > 6000 else world_knowledge

    prompt = f"""请为以下股票生成完整的基本面世界知识 JSON 文件。

## 股票信息
- 代码: {code}
- 名称: {name}
- 行业: {sk_industry}
- 市值: {total_mv/10000:.0f}亿元（如有）
- 已知业务描述: {what_they_do or '未知，请根据行业和公司名推断'}

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
    "what_they_do": "该公司真正在做什么业务，核心产品/服务，主要客户，技术特点。200-400字，要具体",
    "industry": "行业分类",
    "industry_position": "在行业中的真实地位，用数据和事实说话"
  }},
  "competitive_analysis": {{
    "strengths": ["优势1（具体数据支撑）", "优势2", "优势3", "优势4", "优势5"],
    "weaknesses": ["劣势1（诚实客观）", "劣势2", "劣势3", "劣势4"],
    "moat_level": "宽/中/窄"
  }},
  "financial_health": {{
    "key_metrics": {{
      "revenue_yi": null,
      "net_profit_yi": null,
      "gross_margin_pct": null,
      "net_margin_pct": null,
      "roe_pct": null,
      "debt_ratio_pct": null,
      "rd_ratio_pct": null,
      "rd_expense_yi": null,
      "operating_cf_yi": null,
      "cf_to_profit": null
    }},
    "health_rating": "健康/一般/差",
    "benchmark_ref": "行业平均",
    "highlights": ["财务亮点1", "财务亮点2"],
    "risks": ["财务风险1", "财务风险2"]
  }},
  "growth_assessment": {{
    "growth_score": 7.0,
    "growth_drivers": ["增长驱动1（结合世界局势）", "增长驱动2", "增长驱动3"],
    "headwinds": ["逆风因素1", "逆风因素2", "逆风因素3"]
  }},
  "geopolitical_assessment": {{
    "risks": ["地缘风险1（具体到该公司）", "地缘风险2", "地缘风险3"],
    "opportunities": ["地缘机遇1（具体到该公司）", "地缘机遇2", "地缘机遇3"],
    "industry_momentum": ["行业动量1", "行业动量2", "行业动量3"]
  }},
  "summary": "一句话总结：核心优势+主要风险+30日展望，100-200字"
}}
```

## 关键要求
1. what_they_do 必须具体：写出该公司真正做什么，核心产品/技术/客户，不要写空话
2. strengths 必须有数据支撑：如市占率、客户名、技术指标、财务数据
3. weaknesses 必须诚实：写出真实的劣势和风险，不要回避
4. geopolitical_assessment 必须结合世界知识文档中的伊朗战争、AI革命、中美贸易战等，具体到该公司如何受影响
5. growth_score 评分参考：AI算力/光模块/半导体设备 8-9分，新能源/军工/医药 7-8分，消费/银行/电力 5-6分，旧赛道退潮(白酒/地产/光伏) 4-5分
6. moat_level 评判标准：ROE>15%+市占率领先+品牌/技术壁垒=宽，ROE 10-15%+有一定优势=中，其他=窄
7. key_metrics 中的数值如果不确定可以填 null，不要编造
8. 绝对不要使用中文引号""''，如需引用请用英文单引号''

请直接输出 JSON，不要有任何其他文字。"""

    return prompt


# ========== 主流程 ==========

def generate_one(code: str, name: str, industry: str, stock_knowledge: dict,
                 world_knowledge: str, total_mv: float = 0,
                 max_retries: int = 2) -> Optional[dict]:
    """生成单只股票的 fundamentals"""
    prompt = build_prompt(code, name, industry, stock_knowledge, world_knowledge, total_mv)

    for attempt in range(max_retries + 1):
        logger.info(f"  调用 LLM 生成 {code} {name} (尝试 {attempt+1}/{max_retries+1})")
        response = call_llm(prompt, max_tokens=4096, temperature=0.3)
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
        data.setdefault("fetch_date", datetime.now().isoformat())
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


def load_industry_map() -> dict:
    """加载 StockAPI 行业缓存"""
    cache_path = os.path.join(SCRIPT_DIR, ".cache", "stockapi_industry.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for code, gl in raw.items():
            result[code] = gl.split("-")[0] if "-" in gl else gl
        return result
    return {}


def load_need_generate() -> list:
    """从 .need_generate.json 加载待生成列表"""
    path = os.path.join(SCRIPT_DIR, ".need_generate.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def main():
    # 解析参数
    codes_arg = None
    force = False
    count = 0
    dry_run = False
    use_need_file = False

    for arg in sys.argv[1:]:
        if arg.startswith("--codes="):
            codes_arg = [c.strip() for c in arg.split("=", 1)[1].split(",") if c.strip()]
        elif arg == "--codes" and sys.argv.index(arg) + 1 < len(sys.argv):
            codes_arg = [c.strip() for c in sys.argv[sys.argv.index(arg) + 1].split(",")]
        elif arg == "--force":
            force = True
        elif arg.startswith("--count="):
            count = int(arg.split("=", 1)[1])
        elif arg == "--count" and sys.argv.index(arg) + 1 < len(sys.argv):
            count = int(sys.argv[sys.argv.index(arg) + 1])
        elif arg == "--dry-run":
            dry_run = True
        elif arg in ("--need-file", "--top500"):
            use_need_file = True

    # 加载资源
    logger.info("加载世界知识...")
    world_knowledge = load_world_knowledge()
    logger.info(f"  世界知识: {len(world_knowledge)} 字符")

    logger.info("加载 STOCK_KNOWLEDGE...")
    stock_knowledge = load_stock_knowledge()
    logger.info(f"  STOCK_KNOWLEDGE: {len(stock_knowledge)} 只股票")

    logger.info("加载行业缓存...")
    industry_map = load_industry_map()
    logger.info(f"  行业缓存: {len(industry_map)} 只股票")

    # 确定要生成的股票列表
    if codes_arg:
        whitelist = load_stock_whitelist()
        code_map = {s["code"]: s for s in whitelist}
        stocks = []
        for c in codes_arg:
            if c in code_map:
                s = code_map[c].copy()
                s["industry"] = industry_map.get(c, "其他")
                stocks.append(s)
            else:
                sk = stock_knowledge.get(c, {})
                stocks.append({
                    "code": c,
                    "name": sk.get("name", c),
                    "industry": industry_map.get(c, sk.get("industry", "其他")),
                    "mcap_yi": 0,
                })
    elif use_need_file:
        stocks = load_need_generate()
        if not stocks:
            logger.error(".need_generate.json 为空或不存在，先运行 _build_target_list.py")
            sys.exit(1)
        logger.info(f"从 .need_generate.json 加载 {len(stocks)} 只股票")
    else:
        whitelist = load_stock_whitelist()
        whitelist.sort(key=lambda x: x.get("mcap_yi", 0), reverse=True)
        stocks = []
        for s in whitelist:
            s_copy = s.copy()
            s_copy["industry"] = industry_map.get(s["code"], "其他")
            stocks.append(s_copy)

    if count > 0:
        stocks = stocks[:count]

    # 过滤已存在的
    os.makedirs(FUNDAMENTALS_DIR, exist_ok=True)
    existing = set()
    if not force:
        for f in os.listdir(FUNDAMENTALS_DIR):
            if f.endswith(".json"):
                existing.add(f.replace(".json", ""))

    need_generate = []
    for s in stocks:
        code = s["code"]
        if force or code not in existing:
            need_generate.append(s)

    logger.info(f"待生成: {len(need_generate)} 只 (已有: {len(stocks) - len(need_generate)})")

    if dry_run:
        for s in need_generate[:50]:
            sk = stock_knowledge.get(s["code"], {})
            print(f"  {s['code']} {s.get('name', '?'):8s} {sk.get('industry', s.get('industry', '?'))}")
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

    # 逐个生成
    success = 0
    fail = 0
    start_time = time.time()

    for i, stock in enumerate(need_generate):
        code = stock["code"]
        name = stock.get("name", "")
        industry = stock.get("industry", "")
        total_mv = stock.get("mcap_yi", 0) or stock.get("mcap", 0) or 0

        elapsed = time.time() - start_time
        avg = elapsed / (success + fail) if (success + fail) > 0 else 0
        remaining = (len(need_generate) - i) * avg
        logger.info(f"[{i+1}/{len(need_generate)}] {code} {name:8s} {industry} | "
                     f"成功:{success} 失败:{fail} | 预计剩余:{remaining/60:.1f}min")

        try:
            data = generate_one(code, name, industry, stock_knowledge,
                                world_knowledge, total_mv)
            if data:
                path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                success += 1
                logger.info(f"  ✓ {code} {name}")
            else:
                fail += 1
                logger.warning(f"  ✗ {code} {name} LLM 调用失败")
        except Exception as e:
            fail += 1
            logger.error(f"  ✗ {code} {name} 异常: {e}")

        # 限速
        time.sleep(0.5)

    elapsed = time.time() - start_time
    logger.info(f"\n=== 完成 ===")
    logger.info(f"成功: {success}, 失败: {fail}")
    logger.info(f"总耗时: {elapsed/60:.1f} 分钟")


if __name__ == "__main__":
    main()
