#!/usr/bin/env python3
"""压测智谱 API 并发上限 — 找 refresh_fundamentals 能稳定支撑的最大线程数。

瓶颈在智谱两个端点:
  - web-search-pro (POST /tools): refresh 第一步
  - GLM-5.2 chat (POST /chat/completions): fundamentals 重写 + V3 重评 (最重)

阶梯加压 (3→5→8→12→16→20→25), 每档统计 成功率 / P50 / P95 / 失败数,
找"成功率首次<100% 或 P95 飙升"的拐点。

用法:
  uv run python3 scripts/test_api_concurrency.py              # 测 web + llm, 到25并发
  uv run python3 scripts/test_api_concurrency.py --target web # 只测 web-search
  uv run python3 scripts/test_api_concurrency.py --max-w 30   # 加到30并发
"""
import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

# 真实股票名做查询池 (避免空结果, 测"能否拿到有效数据")
STOCKS = ["中际旭创", "新易盛", "寒武纪", "工业富联", "海光信息", "北方华创",
          "澜起科技", "韦尔股份", "兆易创新", "紫光国微", "圣邦股份", "士兰微",
          "卓胜微", "三安光电", "扬杰科技", "斯达半导"]


def _stats(lats, fails, W):
    sr = (W - fails) / W * 100
    p50 = statistics.median(lats) if lats else 0
    srt = sorted(lats)
    p95 = srt[max(0, int(len(srt) * 0.95) - 1)] if srt else 0
    return sr, p50, p95, fails


def test_websearch(W):
    """W 并发下 web-search-pro: 成功=返回>100字符的有效结果"""
    from picker.pipeline.refresh_fundamentals import _web_search
    queries = [f"{STOCKS[i % len(STOCKS)]} 光模块 芯片 最新订单 2026" for i in range(W)]
    lats, fails = [], 0

    def task(q):
        t0 = time.time()
        try:
            r = _web_search(q)
            return time.time() - t0, (len(r) > 100), ""
        except Exception as e:
            return time.time() - t0, False, f"{type(e).__name__}"

    with ThreadPoolExecutor(W) as ex:
        for lat, ok, err in ex.map(task, queries):
            if ok:
                lats.append(lat)
            else:
                fails += 1
    return _stats(lats, fails, W)


def test_llm(W):
    """W 并发下 GLM-5.2 chat (短prompt): 成功=返回>10字符"""
    from picker.pipeline.refresh_fundamentals import _call_llm
    prompts = [f"用一句话(30字内)介绍{STOCKS[i % len(STOCKS)]}的主营业务和行业地位" for i in range(W)]
    lats, fails = [], 0

    def task(p):
        t0 = time.time()
        try:
            r = _call_llm("你是A股研究员", p, max_tokens=200)
            return time.time() - t0, bool(r and len(r) > 10)
        except Exception as e:
            return time.time() - t0, False

    with ThreadPoolExecutor(W) as ex:
        for lat, ok in ex.map(task, prompts):
            if ok:
                lats.append(lat)
            else:
                fails += 1
    return _stats(lats, fails, W)


def run_table(name, fn, levels):
    print("=" * 64)
    print(f"{name} 并发压测")
    print("=" * 64)
    print(f"{'并发':>4} | {'成功率':>7} | {'P50':>6} | {'P95':>6} | {'失败':>4} | 拐点")
    print("-" * 64)
    prev_sr = 100
    for W in levels:
        sr, p50, p95, f = fn(W)
        flag = ""
        if sr < 100 and prev_sr >= 100:
            flag = " ◀ 首次出现失败"
        if p95 > 30 and flag == "":
            flag = " ◀ P95>30s"
        print(f"{W:>4} | {sr:>6.0f}% | {p50:>5.1f}s | {p95:>5.1f}s | {f:>4} |{flag}",
              flush=True)
        prev_sr = sr
        time.sleep(2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="智谱 API 并发压测")
    ap.add_argument("--target", choices=["web", "llm", "both"], default="both")
    ap.add_argument("--max-w", type=int, default=25, help="最大并发数 (默认25)")
    args = ap.parse_args()

    levels = [w for w in [3, 5, 8, 12, 16, 20, args.max_w] if w <= args.max_w]
    print(f"压测并发阶梯: {levels}\n")

    if args.target in ("web", "both"):
        run_table("web-search-pro", test_websearch, levels)
        print()
    if args.target in ("llm", "both"):
        run_table("GLM-5.2 chat", test_llm, levels)

    print("\n结论: 取两者中'成功率开始下降'的较低并发作为 refresh 安全线程数。")
