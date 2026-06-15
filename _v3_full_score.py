#!/usr/bin/env python3
"""
全量 V3 打分 + 基本面精华信息（阶段一：基本面排序）

一次 LLM 调用同时产出：
  1. 赛道动量三子维度小数分（chain/delivery/capital → sector_score 求和）
  2. 基本面精华信息 essence（服务下游30天涨幅竞争辩论）

工程保障：8线程并发、逐只落盘加锁、断点续跑、失败不落盘自动重试。
缓存：.fundamental_v3_scores.json（复用，已缓存但缺 essence 的会重跑补齐）

⚠️ 前视偏差：fundamentals 快照含最新已兑现叙事。本榜单用于【当前选股】是合理的
   （就是要用最新认知选未来），但不可再用历史涨幅自证。
"""
import os, sys, json, re, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv
load_dotenv(override=True)
import fundamental_scorer as fs

V3_CACHE = os.path.join(ROOT, ".fundamental_v3_scores.json")
FUNDAMENTALS_DIR = os.path.join(ROOT, "fundamentals")

# 直连 OpenAI 客户端，120s 超时（reasoning 模型单次生成可能 >30s，避免被误杀）
_API_KEY = os.environ.get("TA_API_KEY")
_BASE_URL = os.environ.get("TA_BASE_URL")
_MODEL = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"
_CLIENT_LOCAL = threading.local()


def _client():
    if not hasattr(_CLIENT_LOCAL, "c"):
        from openai import OpenAI
        _CLIENT_LOCAL.c = OpenAI(api_key=_API_KEY, base_url=_BASE_URL)
    return _CLIENT_LOCAL.c


def _llm(prompt):
    try:
        resp = _client().chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=2048, timeout=120,
        )
        msg = resp.choices[0].message
        return (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")
    except Exception:
        return None

PROMPT_V3E = """你是A股量化研究员，对股票赛道动量评分并提炼30天涨幅辩论精华。

## 赛道动量评分（三子维度，各保留1位小数）
- chain 产业链位置(0.0-10.0): AI算力最核心(1.6T光模块/HBM/CoWoS/AI主芯片)→8.5-10.0；次核心(PCB/铜连接/液冷/光芯片)→6.5-8.4；半导体设备材料→5.0-6.4；消费电子/汽车电子→3.0-4.9；产业链外独立成长→1.0-2.9；旧赛道退潮(锂电/白酒/地产/传统矿业)→0.0
- delivery 业绩兑现度(0.0-10.0): 顶级大客户(英伟达/谷歌/华为/苹果)+产能扩张+业绩高增兑现→8.0-10.0；有客户业绩放量→5.5-7.9；有客户未放量→3.0-5.4；只有概念无订单→0.0-2.9
- capital 资金关注度(0.0-5.0): 最热主线(AI算力/光模块/HBM)→4.0-5.0；二线(国产算力/半导体设备/机器人)→2.5-3.9；消费电子/汽车电子→1.5-2.4；冷门→0.0-1.4

**sector_score 必须严格等于 chain + delivery + capital 之和（范围0.0-25.0，保留1位小数）。不要另算、不要归一化。** 用小数拉开同档位区分度。旧赛道退潮品种诚实给低分。

## 精华信息（服务30天涨幅竞争辩论，每项≤25字，字段不可重复）
- chain_position: 产业链卡位一句话
- core_catalyst: 30天内最强上涨催化（仅一条）
- biggest_bull: 多头最强论据
- biggest_bear: 空头最强攻击点
- quality_redline: 财务质量底线(ROE/净利率/健康度)
- catalyst_horizon: near(30天内有催化)/mid(1季内)/far(更远或无)

严格输出JSON（essence每个key只出现一次，不要解释）:
{"chain":数,"delivery":数,"capital":数,"sector_score":数,"brief":"40字内理由","essence":{"chain_position":"","core_catalyst":"","biggest_bull":"","biggest_bear":"","quality_redline":"","catalyst_horizon":"near"}}

【推理要求】直接判断，推理控制在80字内：仅说明产业链档位归属和兑现度档位，不要复述评分规则原文，不要逐字数essence字数。

股票数据：
"""

ESSENCE_KEYS = ["chain_position", "core_catalyst", "biggest_bull",
                "biggest_bear", "quality_redline", "catalyst_horizon"]


def _parse(content):
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    m = re.search(r'\{.*\}', text.strip(), re.S)
    if not m:
        return None
    try:
        r = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    try:
        chain = round(float(r.get("chain", 0)), 1)
        delivery = round(float(r.get("delivery", 0)), 1)
        capital = round(float(r.get("capital", 0)), 1)
    except (TypeError, ValueError):
        return None
    # sector_score 一律用子维度求和（权威），防模型加错/归一化
    summed = round(chain + delivery + capital, 1)
    ess = r.get("essence", {}) or {}
    essence = {k: str(ess.get(k, ""))[:40] for k in ESSENCE_KEYS}
    if essence["catalyst_horizon"] not in ("near", "mid", "far"):
        essence["catalyst_horizon"] = "mid"
    return {
        "chain": chain, "delivery": delivery, "capital": capital,
        "sector_score": summed,
        "sector_score_model": (round(float(r["sector_score"]), 1)
                               if isinstance(r.get("sector_score"), (int, float)) else None),
        "brief": str(r.get("brief", ""))[:60],
        "essence": essence,
    }


def _call(code):
    sj = fs._build_stock_json(code)
    if not sj:
        return code, {"error": "no_fundamentals"}, 0.0
    t0 = time.time()
    content = _llm(PROMPT_V3E + sj[:8000])
    return code, _parse(content), time.time() - t0


def needs_run(entry):
    """没分 或 有分但缺完整essence → 需要跑"""
    if not entry or "sector_score" not in entry:
        return True
    ess = entry.get("essence")
    if not isinstance(ess, dict):
        return True
    return any(not ess.get(k) for k in ESSENCE_KEYS)


def main():
    codes = sorted(f[:-5] for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith(".json"))
    cache = {}
    if os.path.exists(V3_CACHE):
        try:
            cache = json.load(open(V3_CACHE))
        except Exception:
            cache = {}

    todo = [c for c in codes if needs_run(cache.get(c))]
    print(f"全量 {len(codes)} 只 | 已完整(含essence) {len(codes)-len(todo)} | 待跑 {len(todo)}", flush=True)

    MAX_WORKERS = int(os.environ.get("V3_WORKERS", "8"))
    lock = threading.Lock()
    done = [0]
    fail = [0]
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_call, c): c for c in todo}
        for fut in as_completed(futures):
            code, r, dt = fut.result()
            with lock:
                done[0] += 1
                n = done[0]
                if not r:
                    fail[0] += 1
                    print(f"[{n}/{len(todo)}] {code} 失败/解析失败 ({dt:.0f}s)", flush=True)
                    continue
                cache[code] = r
                json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)
                if "sector_score" not in r:
                    print(f"[{n}/{len(todo)}] {code} 无fundamentals", flush=True)
                    continue
                el = time.time() - t_start
                eta = (len(todo) - n) / max(n / el, 0.001)
                ess = r["essence"]
                print(f"[{n}/{len(todo)}] {code} V3={r['sector_score']:>4.1f} "
                      f"[{r['chain']}+{r['delivery']}+{r['capital']}] {ess['catalyst_horizon']} "
                      f"{dt:.0f}s ETA{eta/60:.0f}m | {ess['core_catalyst']}", flush=True)

    el = time.time() - t_start
    print(f"\n完成: 成功 {done[0]-fail[0]}, 失败 {fail[0]}, 耗时 {el/60:.1f}m", flush=True)

    # Top50 榜单
    scored = [(c, v) for c, v in cache.items() if "sector_score" in v]
    scored.sort(key=lambda x: -x[1]["sector_score"])
    print(f"\n{'='*70}\n  阶段一产出：基本面排序 Top50\n{'='*70}", flush=True)
    for i, (code, v) in enumerate(scored[:50], 1):
        name = ""
        try:
            name = json.load(open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json"))).get("name", "")
        except Exception:
            pass
        print(f"  {i:>2}. {code} {name:<8} {v['sector_score']:>4.1f} "
              f"{v['essence']['catalyst_horizon']:<4} | {v['essence']['core_catalyst']}", flush=True)


if __name__ == "__main__":
    main()
