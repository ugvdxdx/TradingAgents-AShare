#!/usr/bin/env python3
"""双 Prompt A/B 回测：V1 旧（AI主线匹配度） vs V2 新（基本面25 + 赛道25）"""
import json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fundamental_scorer import _rule_based_score, _build_stock_json, _parse_llm_response, SCORING_PROMPT

STOCKS = [
    ("600519", "贵州茅台"),
    ("300750", "宁德时代"),
    ("688981", "中芯国际"),
    ("601939", "建设银行"),
    ("601138", "工业富联"),
    ("600276", "恒瑞医药"),
    ("002594", "比亚迪"),
    ("601857", "中国石油"),
    ("300502", "新易盛"),
    ("000858", "五粮液"),
]

FUNDAMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fundamentals")

# ====== V2: 双轨制 Prompt ======
SCORING_PROMPT_V2 = """你是A股量化研究员，需要对一只股票进行双维度评分。

## 当前市场背景
2025-2026年A股核心交易逻辑是AI算力产业链业绩兑现。但评分需要同时考虑：
- 公司自身的基本面质量（不因行业偏见否定优质公司）
- 当前市场主线中的位置（AI产业链给予合理溢价）

## 评分维度（满分50 = 25基本面质量 + 25赛道动量）

### 第一部分：基本面质量（0-25分）
评估公司自身的经营质量，与当前市场风口无关：

1. 盈利能力 (0-10分)：ROE、净利率、毛利率综合评估
   - ROE>20%且净利率>20% → 8-10分
   - ROE>10%且净利率>10% → 5-7分
   - ROE>5% → 2-4分
   - 其余0-1分
2. 护城河与竞争地位 (0-8分)：从moat、strengths、行业地位判断
   - 真正的行业龙头，有定价权 → 6-8分
   - 细分龙头，竞争格局良好 → 3-5分
   - 竞争激烈，无明显优势 → 0-2分
3. 财务健康度 (0-7分)：负债率、现金流、资产质量
   - 低负债+现金流充裕+资产优质 → 5-7分
   - 财务结构合理 → 2-4分
   - 财务有压力 → 0-1分

校验规则：
- ROE<3% → 盈利能力上限4分
- 净利润为负 → 盈利能力0分
- 净利率<5%且营收>千亿 → 偏代工属性，盈利能力上限5分

### 第二部分：赛道动量（0-25分）
评估公司是否在AI算力/半导体/消费电子产业链上，以及业绩兑现程度：

1. 产业链位置 (0-10分)：
   - 核心供应商（光模块/PCB/先进封装/AI芯片/存储）→ 7-10分
   - 间接配套/消费电子/汽车电子 → 4-6分
   - 产业链外但有自己的独立成长逻辑 → 1-3分
   - 传统行业无催化 → 0分

2. 业绩兑现度 (0-10分)：是否有具体订单/产能/大客户验证？
   - 明确大客户（英伟达/华为/苹果/特斯拉等）+ 产能扩张 → 7-10分
   - 有客户但未放量 → 3-6分
   - 只有概念无订单 → 0-2分

3. 资金关注度 (0-5分)：当前是否处于资金流入方向？
   - AI算力/光模块/先进封装 → 4-5分
   - 消费电子复苏/汽车电子 → 2-3分
   - 冷门/资金流出 → 0-1分

注意：非AI产业链的优质公司（如创新药、高端消费），如果自身成长逻辑清晰（GLP-1出海、消费升级等），产业链位置给1-3分而非0分，资金关注度给1-2分而非0分。旧赛道退潮品种（锂电/白酒/地产/传统矿业）产业链和资金关注度给0分。

请输出严格JSON格式，不要解释：
{"fundamental_score": 整数0-25, "sector_score": 整数0-25, "total": 整数0-50, "brief": "一句话理由（中文，40字以内）"}

股票数据：
"""


def load_fundamentals(code):
    path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _call_anthropic_api(api_key, base_url, model, prompt, v2_mode=False):
    for url_tail in ["/messages", "/v1/messages"]:
        url = f"{base_url}{url_tail}"
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
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = json.loads(resp.read())
                content = ""
                for block in raw.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                if not content:
                    choices = raw.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                if v2_mode:
                    return _parse_v2_response(content)
                return _parse_llm_response(content, "")
        except Exception as e:
            err = str(e)[:80]
            if "404" not in err and "405" not in err:
                print(f"    [API] {url_tail}: {err}")
            continue
    return None


def _parse_v2_response(content: str):
    """解析 V2 的三段式 JSON：fundamental_score / sector_score / total"""
    text = content.strip() if content else ""
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        result = json.loads(text)
        return {
            "fundamental_score": result.get("fundamental_score"),
            "sector_score": result.get("sector_score"),
            "total": result.get("total", 0),
            "brief": result.get("brief", ""),
        }
    except json.JSONDecodeError:
        return None


def llm_score(code, data_json, prompt_label, prompt_text, v2_mode=False):
    api_key = os.environ.get("TA_API_KEY")
    base_url = (os.environ.get("TA_BASE_URL") or "").rstrip("/")
    model = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"
    if not api_key:
        return None
    prompt = prompt_text + data_json[:8000]
    return _call_anthropic_api(api_key, base_url, model, prompt, v2_mode=v2_mode)


def main():
    results = []

    for code, name in STOCKS:
        print(f"\n{'='*60}")
        print(f"正在打分: {code} {name}")
        print(f"{'='*60}")

        data = load_fundamentals(code)
        stock_json = _build_stock_json(code)
        if not stock_json:
            print("  ❌ 无法构建 JSON，跳过")
            continue

        # V1：旧 Prompt（AI 主线匹配）
        v1 = llm_score(code, stock_json, "v1", SCORING_PROMPT, v2_mode=False)
        v1_score = v1["score"] if v1 else None
        v1_brief = v1["brief"] if v1 else ""

        # V2：新 Prompt（双轨制）
        v2 = llm_score(code, stock_json, "v2", SCORING_PROMPT_V2, v2_mode=True)

        # 规则引擎
        rule = _rule_based_score(code)

        biz = data.get("business_overview", {})
        fin = data.get("financial_health", {})
        metrics = fin.get("key_metrics", {})
        comp = data.get("competitive_analysis", {})

        v1_s = f"{v1_score}" if v1_score is not None else "N/A"
        v2_t = f"{v2['total']}" if v2 and v2['total'] is not None else "N/A"
        v2_f = f"{v2['fundamental_score']}" if v2 and v2.get('fundamental_score') is not None else "-"
        v2_s = f"{v2['sector_score']}" if v2 and v2.get('sector_score') is not None else "-"
        rule_s = f"{rule}" if rule is not None else "N/A"

        print(f"  V1={v1_s}  V2={v2_t} (基本{v2_f}+赛道{v2_s})  Rule={rule_s}")
        if v1_brief:
            print(f"  V1: {v1_brief}")
        if v2 and v2.get("brief"):
            print(f"  V2: {v2['brief']}")

        results.append({
            "code": code,
            "name": data.get("name", name),
            "industry": biz.get("industry", ""),
            "moat": comp.get("moat_level", ""),
            "health": fin.get("health_rating", ""),
            "roe": metrics.get("roe_pct"),
            "net_margin": metrics.get("net_margin_pct"),
            "v1_score": v1_score,
            "v1_brief": v1_brief,
            "v2_fundamental": v2.get("fundamental_score") if v2 else None,
            "v2_sector": v2.get("sector_score") if v2 else None,
            "v2_total": v2.get("total") if v2 else None,
            "v2_brief": v2.get("brief") if v2 else "",
            "rule_score": rule,
        })

    # ============ 汇总 ============
    print(f"\n\n{'='*110}")
    print("                    📊 A/B 回测对比：V1 (AI主线匹配) vs V2 (基本面+赛道)")
    print(f"{'='*110}")
    h = f"{'代码':<8} {'名称':<8} {'行业':<14} {'ROE%':<7} {'净利率%':<7} | {'V1':>4} | {'V2基':>4} {'V2赛':>4} {'V2总':>4} | {'规则':>4}"
    print(h)
    print(f"{'-'*110}")

    # 按 V1 排名
    sorted_v1 = sorted([r for r in results if r["v1_score"] is not None], key=lambda x: x["v1_score"], reverse=True)
    sorted_v2 = sorted([r for r in results if r["v2_total"] is not None], key=lambda x: x["v2_total"], reverse=True)

    for r in sorted_v1:
        roe = f"{r['roe']:.1f}" if r['roe'] else "N/A"
        nm = f"{r['net_margin']:.1f}" if r['net_margin'] else "N/A"
        v1 = f"{r['v1_score']}" if r['v1_score'] is not None else "N/A"
        v2f = f"{r['v2_fundamental']}" if r['v2_fundamental'] is not None else "-"
        v2s = f"{r['v2_sector']}" if r['v2_sector'] is not None else "-"
        v2t = f"{r['v2_total']}" if r['v2_total'] is not None else "N/A"
        rule = f"{r['rule_score']}" if r['rule_score'] is not None else "N/A"
        print(f"{r['code']:<8} {r['name']:<8} {r['industry']:<14} {roe:<7} {nm:<7} | {v1:>4} | {v2f:>4} {v2s:>4} {v2t:>4} | {rule:>4}")

    print(f"{'='*110}")

    # V1 vs V2 排名变化
    print(f"\n📊 排名对比：")
    v1_rank = {r["code"]: i+1 for i, r in enumerate(sorted_v1)}
    v2_rank = {r["code"]: i+1 for i, r in enumerate(sorted_v2)}
    print(f"{'代码':<8} {'名称':<8} {'V1排名':<7} {'V2排名':<7} {'变化':<6}")
    print(f"{'-'*40}")
    for code, _ in STOCKS:
        r1 = v1_rank.get(code, "-")
        r2 = v2_rank.get(code, "-")
        if isinstance(r1, int) and isinstance(r2, int):
            delta = r1 - r2
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "—")
            change = f"{arrow}{abs(delta)}"
        else:
            change = "-"
        print(f"{code:<8} {dict(STOCKS)[code]:<8} {str(r1):<7} {str(r2):<7} {change:<6}")

    # V2 维度详情
    print(f"\n📝 V2 双维度详情 + 对比 V1：")
    for r in sorted_v2:
        v1_s = f"V1={r['v1_score']}" if r['v1_score'] is not None else "V1=N/A"
        print(f"  {r['code']} {r['name']}: 基本面{r['v2_fundamental']} + 赛道{r['v2_sector']} = {r['v2_total']}  ({v1_s})")
        if r.get("v2_brief"):
            print(f"    V2: {r['v2_brief']}")
        if r.get("v1_brief"):
            print(f"    V1: {r['v1_brief']}")

if __name__ == "__main__":
    main()