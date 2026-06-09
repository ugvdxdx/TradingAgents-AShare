#!/usr/bin/env python3
"""全量基本面打分 —— fundamentals/ 下所有股票，V1+V2 双 Prompt，结果缓存到 JSON"""
import json, os, sys, time, urllib.request

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)

from fundamental_scorer import (
    _rule_based_score, _build_stock_json, _parse_llm_response, SCORING_PROMPT, SCORING_PROMPT_V2,
    _load_llm_cache, _save_llm_cache
)

FUNDAMENTALS_DIR = os.path.join(_PROJECT_ROOT, "fundamentals")
OUTPUT_FILE = os.path.join(_PROJECT_ROOT, ".fundamental_scores_batch.json")


def discover_stocks():
    """从 fundamentals/ 目录自动发现所有股票"""
    stocks = []
    for fname in sorted(os.listdir(FUNDAMENTALS_DIR)):
        if fname.endswith(".json"):
            code = fname.replace(".json", "")
            try:
                with open(os.path.join(FUNDAMENTALS_DIR, fname), "r") as f:
                    data = json.load(f)
                name = data.get("name", code)
            except:
                name = code
            stocks.append((code, name))
    return stocks


def _call_anthropic(api_key, base_url, model, prompt, v2_mode=False):
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
                return _parse_response(content, v2_mode)
        except Exception as e:
            err = str(e)[:80]
            if "404" not in err and "405" not in err and "timeout" not in err.lower():
                print(f"    [API] {err}")
            continue
    return None


def _parse_response(content, v2_mode):
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
    except json.JSONDecodeError:
        return None

    if "fundamental_score" in result or "sector_score" in result:
        return {
            "fundamental_score": result.get("fundamental_score"),
            "sector_score": result.get("sector_score"),
            "total": result.get("total", 0),
            "brief": result.get("brief", ""),
        }
    else:
        score = int(result.get("score", 0))
        return {"score": max(0, min(50, score)), "brief": result.get("brief", "")}


def load_existing_results():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r") as f:
            data = json.load(f)
        return data.get("results", {}), data.get("meta", {})
    return {}, {}


def save_results(results, meta):
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump({"meta": meta, "results": results}, f, ensure_ascii=False, indent=1)


def main():
    stocks = discover_stocks()
    total = len(stocks)
    print(f"发现 {total} 只股票")

    api_key = os.environ.get("TA_API_KEY")
    base_url = (os.environ.get("TA_BASE_URL") or "").rstrip("/")
    model = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"

    # 加载已有结果
    results, meta = load_existing_results()
    skipped = sum(1 for r in results.values() if r.get("v1") and r.get("v2"))
    print(f"已有 {len(results)} 条缓存，{skipped} 条完整（V1+V2 都有）")

    start_time = time.time()
    done = 0
    errors = 0

    for i, (code, name) in enumerate(stocks):
        # 跳过已完整缓存的
        if code in results and results[code].get("v1") is not None and results[code].get("v2") is not None:
            done += 1
            continue

        stock_json = _build_stock_json(code)
        if not stock_json:
            errors += 1
            continue

        # 初始化条目
        if code not in results:
            results[code] = {}

        # V1
        v1_result = None
        if results[code].get("v1") is None:
            v1_result = _call_anthropic(api_key, base_url, model, SCORING_PROMPT + stock_json[:8000], v2_mode=False)
            if v1_result is not None:
                results[code]["v1"] = v1_result

        # V2
        v2_result = None
        if results[code].get("v2") is None:
            v2_result = _call_anthropic(api_key, base_url, model, SCORING_PROMPT_V2 + stock_json[:8000], v2_mode=True)
            if v2_result is not None:
                results[code]["v2"] = v2_result

        # 规则引擎
        if results[code].get("rule") is None:
            results[code]["rule"] = _rule_based_score(code)

        # 补充元信息
        results[code]["name"] = name

        v1_s = results[code].get("v1", {}).get("score", "?") if results[code].get("v1") else "?"
        v2_t = results[code].get("v2", {}).get("total", "?") if results[code].get("v2") else "?"
        rule = results[code].get("rule", "?")
        done += 1

        elapsed = time.time() - start_time
        rate = done / max(elapsed, 1)
        eta = (total - done) / max(rate, 0.01)
        print(f"[{done}/{total}] {code} {name:<8} V1={v1_s} V2={v2_t} Rule={rule}  "
              f"| {elapsed:.0f}s elapsed, ~{eta:.0f}s remaining")

        # 每 20 条保存一次
        if done % 20 == 0:
            meta = {"total": total, "done": done, "errors": errors,
                    "elapsed_s": round(elapsed), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_results(results, meta)

    # 最终保存
    meta = {"total": total, "done": done, "errors": errors,
            "elapsed_s": round(time.time() - start_time), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_results(results, meta)

    # 统计摘要
    v2_scores = []
    for r in results.values():
        v2 = r.get("v2", {})
        if v2 and v2.get("total"):
            v2_scores.append((r.get("name", ""), v2["total"],
                              v2.get("fundamental_score", 0), v2.get("sector_score", 0), v2.get("brief", "")))

    v2_scores.sort(key=lambda x: x[1], reverse=True)

    print(f"\n{'='*90}")
    print(f"  全量打分完成 — {len(v2_scores)} 只有效 V2 评分")
    print(f"{'='*90}")
    print(f"\n🏆 Top 20 (按 V2 总分):")
    for i, (name, total, fund, sect, brief) in enumerate(v2_scores[:20], 1):
        print(f"  {i:>2}. {name:<10} {total:>2} (基本{fund}+赛道{sect})  {brief}")

    print(f"\n📉 Bottom 10:")
    for name, total, fund, sect, brief in v2_scores[-10:]:
        print(f"     {name:<10} {total:>2} (基本{fund}+赛道{sect})  {brief}")

    # 分布统计
    buckets = {"40-50": 0, "30-39": 0, "20-29": 0, "10-19": 0, "0-9": 0}
    for _, total, _, _, _ in v2_scores:
        if total >= 40: buckets["40-50"] += 1
        elif total >= 30: buckets["30-39"] += 1
        elif total >= 20: buckets["20-29"] += 1
        elif total >= 10: buckets["10-19"] += 1
        else: buckets["0-9"] += 1

    print(f"\n📊 V2 分数分布:")
    for k, v in buckets.items():
        bar = "█" * (v // max(1, sum(buckets.values()) // 20) + 1)
        print(f"  {k}: {v:>4} 只 {bar}")

    print(f"\n💾 结果已保存到: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()