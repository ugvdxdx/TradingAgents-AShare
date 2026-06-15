#!/usr/bin/env python3
"""
V3 子维度连续打分验证脚本（C 方案）

目标：验证「让 LLM 分别输出三个赛道子维度并保留一位小数，再加权求和」
能否提升头部（接近满分）股票的区分度。

- 只跑指定的 30 只接近满分的票
- 独立缓存 .fundamental_v3_scores.json，断点续跑，不污染主缓存
- 跑完对比 V2 整数 sector vs V3 连续 sector 的头部区分度
"""
import os, sys, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fundamental_scorer as fs

V3_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fundamental_v3_scores.json')

# V3 Prompt：子维度强制保留一位小数，sector 由子维度加权求和 → 连续分
SCORING_PROMPT_V3 = """你是A股量化研究员，需对一只股票的「赛道动量」做精细评分。

## 回测背景
赛道动量分与未来半年涨幅的Spearman秩相关达0.56，是唯一有效的Alpha信号。
头部股票（接近满分）大量并列，难以排名。本次任务要求你为三个子维度给出**带一位小数**的精细分，
拉开头部区分度。不要取整、不要凑整数，差距体现在小数位上。

## 三个子维度（均为连续分，保留1位小数）

### 1. 产业链位置 (0.0 - 10.0)
从 what_they_do / strengths 判断真实业务的产业链卡位，不要只看 industry 标签。
- AI算力最核心环节（800G/1.6T光模块、HBM、CoWoS先进封装、AI主芯片、高端存储）→ 8.5-10.0
- AI算力次核心（PCB、铜连接、液冷、AI电源、光芯片）→ 6.5-8.4
- 半导体设备/材料/EDA、国产算力配套 → 5.0-6.4
- 消费电子/汽车电子/间接配套 → 3.0-4.9
- 产业链外但独立成长逻辑清晰（创新药/高端制造）→ 1.0-2.9
- 旧赛道退潮（锂电/白酒/地产/传统矿业）→ 0.0
用小数区分同档位内的卡位差异：1.6T光模块龙头 9.5+，400G跟随者 8.6；CoWoS核心 9.0，普通封测 6.8。

### 2. 业绩兑现度 (0.0 - 10.0)
- 明确顶级大客户（英伟达/谷歌/华为/苹果/特斯拉）+ 产能扩张 + 业绩高增已兑现 → 8.0-10.0
- 有大客户、产能在建、业绩开始放量 → 5.5-7.9
- 有客户但未放量 / 业绩增速一般 → 3.0-5.4
- 只有概念无订单（"国产替代/政策红利"空话）→ 0.0-2.9
用小数区分兑现的确定性与斜率。

### 3. 资金关注度 (0.0 - 5.0)
- 当前最热主线（AI算力/光模块/先进封装/HBM）→ 4.0-5.0
- 二线热点（国产算力/半导体设备/机器人）→ 2.5-3.9
- 消费电子复苏/汽车电子 → 1.5-2.4
- 冷门/资金流出 → 0.0-1.4

## 输出要求
严格JSON，三个子维度均保留一位小数，sector_score = 三者之和（也保留一位小数）：
{"chain":数字, "delivery":数字, "capital":数字, "sector_score":数字, "brief":"一句话理由(40字内)"}

股票数据：
"""


def _call_v3(data_json: str):
    content = fs._call_llm_api(SCORING_PROMPT_V3 + data_json[:8000])
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    # 容错：截取首个 JSON 对象
    import re
    m = re.search(r'\{.*\}', text, re.S)
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
    sect = r.get("sector_score")
    try:
        sect = round(float(sect), 1)
    except (TypeError, ValueError):
        sect = None
    # 子维度求和作为权威 sector（防止模型加错）
    summed = round(chain + delivery + capital, 1)
    return {
        "chain": chain, "delivery": delivery, "capital": capital,
        "sector_score_model": sect,
        "sector_score": summed,
        "brief": str(r.get("brief", ""))[:60],
    }


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass

    codes = ['002463', '002600', '002851', '002916', '003031', '300308', '300394',
             '300502', '300548', '300757', '301666', '601138', '688041', '688300',
             '688630', '688809', '688820', '002138', '300395', '300476', '301308',
             '600601', '688256', '000021', '000938', '002156', '002281', '002371',
             '002384', '002837']

    cache = {}
    if os.path.exists(V3_CACHE):
        try:
            cache = json.load(open(V3_CACHE))
        except Exception:
            cache = {}

    v2 = json.load(open(fs.LLM_CACHE_FILE))
    v2_by_code = {k.split('_')[0]: v for k, v in v2.items()}

    todo = [c for c in codes if c not in cache]
    print(f"共 {len(codes)} 只，已缓存 {len(codes)-len(todo)}，待跑 {len(todo)}")

    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    MAX_WORKERS = int(os.environ.get("V3_WORKERS", "8"))
    lock = threading.Lock()
    done = [0]

    def _work(code):
        sj = fs._build_stock_json(code)
        if not sj:
            return code, {"error": "no_fundamentals"}, 0.0
        t0 = time.time()
        r = _call_v3(sj)
        return code, r, time.time() - t0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_work, c): c for c in todo}
        for fut in as_completed(futures):
            code, r, dt = fut.result()
            with lock:
                done[0] += 1
                n = done[0]
                if not r:
                    print(f"  [{n}/{len(todo)}] {code} LLM失败/解析失败 ({dt:.1f}s)")
                    continue
                cache[code] = r
                # 立即落盘（加锁，断点续跑）
                json.dump(cache, open(V3_CACHE, 'w'), ensure_ascii=False, indent=1)
                if "sector_score" not in r:
                    print(f"  [{n}/{len(todo)}] {code} 无fundamentals，跳过")
                    continue
                v2s = v2_by_code.get(code, {}).get('sector_score', '?')
                print(f"  [{n}/{len(todo)}] {code} V2={v2s} → V3={r['sector_score']} "
                      f"(链{r['chain']}+绩{r['delivery']}+资{r['capital']}) {dt:.1f}s | {r['brief']}")

    # ---- 对比报告 ----
    print("\n" + "=" * 70)
    print("头部区分度对比（V2 整数 vs V3 连续）")
    print("=" * 70)
    rows = []
    for code in codes:
        c = cache.get(code, {})
        if "sector_score" not in c:
            continue
        rows.append((code, v2_by_code.get(code, {}).get('sector_score'), c['sector_score'], c.get('brief', '')))

    import collections
    v2vals = [r[1] for r in rows if r[1] is not None]
    v3vals = [r[2] for r in rows]
    print(f"\nV2 唯一分值数: {len(set(v2vals))} / {len(v2vals)}  → {sorted(collections.Counter(v2vals).items(), reverse=True)}")
    print(f"V3 唯一分值数: {len(set(v3vals))} / {len(v3vals)}")

    print("\nV3 排名（降序，括号内为×4映射到百分制）：")
    rows.sort(key=lambda r: -(r[2] or 0))
    for rank, (code, v2s, v3s, brief) in enumerate(rows, 1):
        v3_100 = round(v3s * 4, 1)
        v2_100 = v2s * 4 if v2s else 0
        print(f"  {rank:>2}. {code}  V2={v2s:>2}({v2_100:>3}) → V3={v3s:>4.1f}({v3_100:>5.1f})   {brief}")


if __name__ == "__main__":
    main()
