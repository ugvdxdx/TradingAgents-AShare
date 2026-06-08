#!/usr/bin/env python3
"""批量基本面打分脚本 —— 对 10 支股票进行 LLM + 规则引擎评分
支持 OpenAI 和 Anthropic 两种 API 协议，自动适配 DeepSeek / Anthropic 端点"""
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fundamental_scorer import (
    _rule_based_score, _build_stock_json, _parse_llm_response, SCORING_PROMPT
)

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
LLM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fundamental_llm_scores.json")


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


def call_llm_score(code: str, data_json: str) -> dict | None:
    """
    调用 LLM 打分。自动检测 API 协议：
    - 如果 TA_BASE_URL 含 'anthropic' → 用 Anthropic Messages API
    - 否则 → 用 OpenAI Chat Completions API
    """
    api_key = os.environ.get("TA_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = (os.environ.get("TA_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    model = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"

    if not api_key:
        return None

    prompt = SCORING_PROMPT + data_json[:8000]

    # 检查缓存
    path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
    cache = _load_llm_cache()
    if os.path.exists(path):
        cache_key = f"{code}_{os.path.getmtime(path):.0f}_v5"
        if cache_key in cache:
            cached = cache[cache_key]
            if isinstance(cached, dict):
                return cached
            return {"score": cached, "brief": ""}

    # 判断 API 协议
    is_anthropic = "anthropic" in base_url.lower()

    if is_anthropic:
        result = _call_anthropic_api(api_key, base_url, model, prompt)
    else:
        result = _call_openai_api(api_key, base_url, model, prompt)

    # 写入缓存
    if result and result.get("score", 0) > 0:
        cache_key = f"{code}_{os.path.getmtime(path):.0f}_v5" if os.path.exists(path) else f"{code}_v5"
        cache[cache_key] = result
        _save_llm_cache(cache)

    return result


def _call_openai_api(api_key: str, base_url: str, model: str, prompt: str) -> dict | None:
    """OpenAI Chat Completions 协议"""
    url = f"{base_url}/chat/completions"
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
            reasoning = msg.get("reasoning_content", "") or ""
            return _parse_llm_response(content, reasoning)
    except Exception as e:
        print(f"  [OpenAI] 调用失败: {e}")
        return None


def _call_anthropic_api(api_key: str, base_url: str, model: str, prompt: str) -> dict | None:
    """Anthropic Messages API 协议（DeepSeek / Anthropic 兼容端点）"""
    # Anthropic Messages API: POST /v1/messages
    # DeepSeek 的实现是: POST {base_url}/messages（不带 /v1）
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read())
                # Anthropic 响应格式: content[0].text
                content = ""
                for block in raw.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                # 也尝试兼容 OpenAI 格式的响应（有些代理同时支持两种）
                if not content:
                    choices = raw.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                return _parse_llm_response(content, "")
        except Exception as e:
            err = str(e)[:100]
            if "404" not in err and "405" not in err:
                print(f"  [Anthropic] {url_tail} 失败: {err}")
            continue

    print(f"  [Anthropic] 所有路径均失败")
    return None


def load_fundamentals(code):
    path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_key_info(data):
    biz = data.get("business_overview", {})
    fin = data.get("financial_health", {})
    metrics = fin.get("key_metrics", {})
    comp = data.get("competitive_analysis", {})

    return {
        "name": data.get("name", ""),
        "industry": biz.get("industry", ""),
        "moat": comp.get("moat_level", ""),
        "health": fin.get("health_rating", ""),
        "revenue_yi": metrics.get("revenue_yi"),
        "net_profit_yi": metrics.get("net_profit_yi"),
        "roe_pct": metrics.get("roe_pct"),
        "gross_margin_pct": metrics.get("gross_margin_pct"),
        "net_margin_pct": metrics.get("net_margin_pct"),
        "what_they_do": biz.get("what_they_do", "")[:120],
    }


def main():
    results = []

    for code, name in STOCKS:
        print(f"\n{'='*60}")
        print(f"正在打分: {code} {name}")
        print(f"{'='*60}")

        data = load_fundamentals(code)
        info = extract_key_info(data)

        # LLM 打分
        llm_score = None
        llm_brief = ""
        stock_json = _build_stock_json(code)
        if stock_json:
            llm_result = call_llm_score(code, stock_json)
            if llm_result:
                llm_score = llm_result.get("score")
                llm_brief = llm_result.get("brief", "")

        # 规则引擎打分
        rule_score = _rule_based_score(code)

        # 最终得分（优先 LLM，回退规则）
        final_score = llm_score if llm_score is not None else rule_score

        print(f"  LLM 打分:  {llm_score if llm_score is not None else 'N/A (调用失败，回退规则引擎)'}")
        if llm_brief:
            print(f"  LLM 理由: {llm_brief}")
        print(f"  规则打分:  {rule_score}")
        print(f"  最终得分:  {final_score} / 50")
        print(f"  行业:      {info['industry']}")
        print(f"  护城河:    {info['moat']}  |  财务: {info['health']}")
        print(f"  营收: {info['revenue_yi']}亿  |  净利: {info['net_profit_yi']}亿")
        print(f"  ROE: {info['roe_pct']}%  |  毛利率: {info['gross_margin_pct']}%  |  净利率: {info['net_margin_pct']}%")

        results.append({
            "code": code,
            "name": info["name"],
            "industry": info["industry"],
            "moat": info["moat"],
            "health": info["health"],
            "revenue_yi": info["revenue_yi"],
            "net_profit_yi": info["net_profit_yi"],
            "roe_pct": info["roe_pct"],
            "gross_margin_pct": info["gross_margin_pct"],
            "net_margin_pct": info["net_margin_pct"],
            "llm_score": llm_score,
            "llm_brief": llm_brief,
            "rule_score": rule_score,
            "final_score": final_score,
        })

    # ============ 汇总表 ============
    print(f"\n\n{'='*94}")
    print("                           📊 10 支股票基本面打分汇总")
    print(f"{'='*94}")
    header = f"{'排名':<4} {'代码':<8} {'名称':<8} {'行业':<16} {'护城河':<6} {'ROE%':<8} {'净利率%':<8} {'LLM':<5} {'规则':<5} {'最终':<5}"
    print(header)
    print(f"{'-'*94}")

    sorted_results = sorted(results, key=lambda x: x["final_score"] or 0, reverse=True)
    for i, r in enumerate(sorted_results, 1):
        llm = f"{r['llm_score']}" if r['llm_score'] is not None else "N/A"
        rule = f"{r['rule_score']}" if r['rule_score'] is not None else "N/A"
        final = f"{r['final_score']}" if r['final_score'] is not None else "N/A"
        roe = f"{r['roe_pct']:.1f}" if r['roe_pct'] else "N/A"
        nm = f"{r['net_margin_pct']:.1f}" if r['net_margin_pct'] else "N/A"
        print(f"{i:<4} {r['code']:<8} {r['name']:<8} {r['industry']:<16} {r['moat']:<6} {roe:<8} {nm:<8} {llm:<5} {rule:<5} {final:<5}")

    print(f"{'='*94}")

    # LLM vs 规则引擎差异
    print(f"\n📊 LLM vs 规则引擎差异分析:")
    print(f"{'代码':<8} {'名称':<8} {'LLM':<5} {'规则':<5} {'差值':<6} {'说明'}")
    print(f"{'-'*60}")
    for r in sorted_results:
        llm = r['llm_score'] if r['llm_score'] is not None else None
        rule = r['rule_score'] if r['rule_score'] is not None else None
        if llm is not None and rule is not None:
            diff = llm - rule
            if diff != 0:
                direction = "LLM更高 ↑" if diff > 0 else "LLM更低 ↓"
                print(f"{r['code']:<8} {r['name']:<8} {llm:<5} {rule:<5} {diff:+<6} {direction}")
        elif llm is not None:
            print(f"{r['code']:<8} {r['name']:<8} {llm:<5} {'N/A':<5} {'--':<6} 仅 LLM")
        elif rule is not None:
            print(f"{r['code']:<8} {r['name']:<8} {'N/A':<5} {rule:<5} {'--':<6} 仅规则")

    # LLM 打分简要理由汇总
    llm_results = [r for r in sorted_results if r.get("llm_brief")]
    if llm_results:
        print(f"\n📝 LLM 打分简要理由:")
        for r in llm_results:
            print(f"  [{r['llm_score']}分] {r['code']} {r['name']}: {r['llm_brief']}")

if __name__ == "__main__":
    main()