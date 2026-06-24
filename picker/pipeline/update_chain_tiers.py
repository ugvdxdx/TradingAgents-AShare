#!/usr/bin/env python3
"""chain 分档映射 (赛道→档位) 动态更新器 — candidate 预览 + manual 人工应用。

把"赛道→档位"映射从 PROMPT_V3E 硬编码解放出来, 按【当前市场主线】重新组织。
旧分档定于较早时点, 可能滞后于市场; 本工具用 LLM 按研报信号重排候选, 人工审核后应用。

用法:
  python3 picker/pipeline/update_chain_tiers.py --mode candidate      # 生成候选+diff, 不写 (预览)
  python3 picker/pipeline/update_chain_tiers.py --mode manual         # 生成+diff+确认写入
  python3 picker/pipeline/update_chain_tiers.py --mode manual --yes   # 跳过确认直接写 (慎用)

分层: LLM 逻辑在本文件 (pipeline 层); 纯函数 (load/render/diff/save) 在
      picker.scoring.chain_tiers (scoring 层), 评分热路径零 LLM 依赖。
"""
import os
import sys
import json
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from picker.scoring import chain_tiers as ct


# ══════════════════════════════════════════════════════════
# LLM 直连 (复用 update_world_knowledge / v3_full_score 同款)
# ══════════════════════════════════════════════════════════
_CLIENT_LOCAL = threading.local()
_API_KEY = os.environ.get("TA_API_KEY") or ""
_BASE_URL = os.environ.get("TA_BASE_URL") or ""
_MODEL = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "glm-5.2"


def _client():
    if not hasattr(_CLIENT_LOCAL, "c"):
        from openai import OpenAI
        _CLIENT_LOCAL.c = OpenAI(api_key=_API_KEY, base_url=_BASE_URL)
    return _CLIENT_LOCAL.c


def _llm(prompt: str, max_tokens: int = 8192) -> str:
    """调用 LLM, 带自动重试 (3 次)。"""
    last_err = None
    for attempt in range(3):
        try:
            resp = _client().chat.completions.create(
                model=_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max_tokens,
                timeout=180,
            )
            msg = resp.choices[0].message
            content = (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")
            if content:
                return content
            last_err = "empty content"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    print(f"    [LLM] 放弃: {last_err}")
    return ""


def _extract_last_json(raw: str):
    """从 LLM 输出提取最后一个合法 JSON 对象 (平衡花括号扫描, 支持嵌套/字符串)。

    应对 GLM 推理模型可能输出的: 前导分析文字 / 复述 prompt 示例 / 代码块包裹。
    扫描所有顶层 {...}, 返回最后一个能 json.loads 成功的。
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    objects = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(raw):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objects.append(raw[start:i + 1])
                    start = -1
    for obj in reversed(objects):
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            continue
    return None


# ══════════════════════════════════════════════════════════
# 信号采集 (研报 momentum + 市场情绪 + 世界知识)
# ══════════════════════════════════════════════════════════
def _collect_signals(days: int = 14) -> dict:
    """从 research.db + 世界知识采集当前市场信号, 供 LLM 重排分档。"""
    from tradingagents.research.consumer import get_sector_momentum, get_market_sentiment
    from picker.scoring.v3_full_score import _load_world_knowledge_slim

    mom = get_sector_momentum(days=days)
    sent_obj = get_market_sentiment(days=7)
    hot = "; ".join(f"{s['sector']}({s.get('bullish_count', 0)})" for s in mom.get("hot_sectors", [])[:10])
    cold = "; ".join(f"{s['sector']}({s.get('bearish_count', 0)})" for s in mom.get("cold_sectors", [])[:8])
    emerging = "; ".join(f"{s['sector']}({s.get('bullish_count', 0)})" for s in mom.get("emerging_sectors", [])[:5])
    sentiment = sent_obj.get("sentiment", "N/A") if isinstance(sent_obj, dict) else "N/A"
    wk = _load_world_knowledge_slim() or ""
    return {
        "sentiment": sentiment,
        "hot": hot or "(无)",
        "cold": cold or "(无)",
        "emerging": emerging or "(无)",
        "wk": (wk[:1500] + ("..." if len(wk) > 1500 else "")) or "(无世界知识)",
    }


# ══════════════════════════════════════════════════════════
# 候选生成: LLM 大胆按当前主线重排 8 档
# ══════════════════════════════════════════════════════════
CANDIDATE_PROMPT = """你是A股量化研究员，负责维护"产业链位置(chain)"评分的分档映射表。

chain 分档 = 稳定的8档刻度(0-10) + 动态的"赛道→档位"映射。刻度不变，变的是每档放哪些赛道——它必须反映【当前市场主线】，而非历史定式。

【8档刻度骨架(严格固定，不得改动range)】
- 9.0-10.0: 当前市场最核心环节 (主线最大、最确定受益者)
- 8.5-8.9: 核心但非第一梯队
- 7.0-8.4: 主线上游关键材料/元件
- 6.0-6.9: 次核心配套
- 5.0-5.9: 受益扩产但非主线专用
- 3.0-4.9: 边缘/传统业务转型
- 1.0-2.9: 产业链外独立成长 (非当前主线)
- 0.0-0.9: 退潮/旧赛道

【当前市场研报信号 (近14天, 来自research.db)】
市场情绪: {sentiment}
热门赛道(bullish计数): {hot}
冷门/退潮赛道(bearish): {cold}
新兴赛道(近7天新起): {emerging}

【当前世界知识摘要】
{wk}

【当前分档映射 (可能已滞后于市场，仅供参照)】
{current_map}

【任务 (大胆按当前主线重排)】
1. 判断当前A股市场主线是什么(一句话)，写入 theme。允许与旧 theme 不同(主线会切换/轮动)。
2. 把每个赛道按"在【当前主线】下的产业链卡位深度"重新分配到8档之一:
   - 主线最核心环节 → 9.0-10.0
   - 主线上游关键材料 → 7.0-8.4
   - 主线配套 → 6.0-6.9
   - 冷门/退潮(bearish密集) → 0.0-2.9
3. 大胆校正滞后: 若旧映射把当前热门新材料放在低档，或把已退潮赛道留在高档，必须调整。不要为"接近旧值"而保留过时判断。
4. 每档 sectors 填具体赛道/产品关键词(3-5个, 精简优先, 供后续评分时匹配个股业务)。criteria 填该档准入标准(可选, 短句)。
5. 8档每档都要输出; range 严格用上面的值，不得改动。

严格输出JSON (不要解释，不要markdown代码块):
{{"theme":"当前主线一句话","theme_strength":"绝对主线/主线之一/主线轮动","tiers":[{{"range":"9.0-10.0","label":"...","sectors":["..."],"criteria":"..."}},{{"range":"8.5-8.9","label":"...","sectors":["..."]}},{{"range":"7.0-8.4","label":"...","sectors":["...","..."]}},{{"range":"6.0-6.9","label":"...","sectors":["..."]}},{{"range":"5.0-5.9","label":"...","sectors":["..."],"criteria":"..."}},{{"range":"3.0-4.9","label":"...","sectors":["..."]}},{{"range":"1.0-2.9","label":"...","sectors":["..."],"criteria":"等..."}},{{"range":"0.0-0.9","label":"...","sectors":["..."],"criteria":"..."}}]}}"""


# 8 档 range 标准集 (校验候选必须严格匹配)
_EXPECTED_RANGES = ["9.0-10.0", "8.5-8.9", "7.0-8.4", "6.0-6.9", "5.0-5.9", "3.0-4.9", "1.0-2.9", "0.0-0.9"]


def build_candidate_tier_map(days: int = 14):
    """LLM 基于当前研报信号 + 世界知识生成候选 tier_map。

    Returns:
        (candidate_dict, None) 成功 | (None, error_msg) 失败。
    """
    from datetime import datetime
    sig = _collect_signals(days=days)
    cur = ct.load_chain_tier_map()
    cur_text = json.dumps(cur, ensure_ascii=False, indent=2) if cur else "(无当前tier_map, 从零生成)"

    prompt = CANDIDATE_PROMPT.format(
        sentiment=sig["sentiment"], hot=sig["hot"], cold=sig["cold"],
        emerging=sig["emerging"], wk=sig["wk"], current_map=cur_text,
    )
    print("  调用 LLM 生成候选 tier_map (大胆按当前主线重排)...")
    raw = _llm(prompt, max_tokens=8192)
    if not raw:
        return None, "LLM 返回空"
    cand = _extract_last_json(raw)
    if not cand or "tiers" not in cand:
        return None, f"JSON 解析失败 | 原始: {raw[:200]}"

    cand_ranges = [t.get("range") for t in cand.get("tiers", [])]
    if sorted(cand_ranges) != sorted(_EXPECTED_RANGES):
        return None, f"档位range不匹配标准集: {cand_ranges}"

    today = datetime.now().strftime("%Y-%m-%d")
    cand["version"] = f"{today}-candidate"
    cand["theme"] = cand.get("theme", "未标注")
    cand["theme_strength"] = cand.get("theme_strength", "未标注")
    return cand, None


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════
def main():
    import argparse
    from datetime import datetime
    ap = argparse.ArgumentParser(description="chain 分档映射动态更新器 (candidate 预览 / manual 应用)")
    ap.add_argument("--mode", choices=["manual", "candidate"], default="candidate")
    ap.add_argument("--days", type=int, default=14, help="研报信号回看天数")
    ap.add_argument("--yes", action="store_true", help="manual 模式跳过确认直接写入 (慎用)")
    args = ap.parse_args()

    print("═" * 60)
    print(f"chain tier_map 更新  (mode={args.mode}, days={args.days})")
    print("═" * 60)
    cand, err = build_candidate_tier_map(days=args.days)
    if err:
        print(f"✗ 候选生成失败: {err}")
        return
    cur = ct.load_chain_tier_map()
    print(f"\n当前版本: {ct.get_tier_version()} | theme: {(cur or {}).get('theme','?')}")
    print(f"候选版本: {cand['version']} | theme: {cand.get('theme')} | 强度: {cand.get('theme_strength')}")
    print("\n【diff (候选 vs 当前)】")
    print(ct.diff_tier_maps(cur, cand))

    if args.mode == "candidate":
        print("\n[candidate] 仅预览, 未写入。用 --mode manual 应用。")
        return

    if args.mode == "manual":
        confirm = args.yes or (input("\n应用此候选到 chain_tier_map.json? [y/N]: ").strip().lower() == "y")
        if not confirm:
            print("已取消, 未写入。"); return
        # 写入时定版 (candidate → manual-yyyymmdd)
        today = datetime.now().strftime("%Y-%m-%d")
        cand["version"] = f"{today}-manual"
        path = ct.save_chain_tier_map(cand, generated_by="manual", archive_reason="manual-replace")
        print(f"\n✓ 已写入 {path}")
        print(f"✓ 当前生效: {cand['version']} | theme: {cand.get('theme')}")
        print("  注: 受影响股票的 chain 将在 7天TTL 过期后自然重评;")
        print("      如需立即生效, 手动跑 v3_full_score 全量重评。")
        return


if __name__ == "__main__":
    main()
