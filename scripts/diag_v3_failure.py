#!/usr/bin/env python3
"""诊断 V3 评分失败根因 — 对失败股直接调 LLM, 打印原始返回。

用法: python3 scripts/diag_v3_failure.py
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(override=True)

from picker.scoring import v3_full_score as v3
from picker.scoring import fundamental_scorer as fs

# 从上次失败名单抽样
FAILED_CODES = ["000333", "000408", "000625", "000776", "002747", "688183", "000960"]

print("=" * 80)
print("V3 评分失败根因诊断")
print("=" * 80)

for code in FAILED_CODES:
    sj = fs._build_stock_json(code)
    if not sj:
        print(f"\n【{code}】无 fundamentals 文件")
        continue

    attr_hint = v3._load_attr_hint(code)
    wk_slim = v3._load_world_knowledge_slim()
    wk_section = ""
    if wk_slim:
        wk_section = f"\n\n【当前市场宏观背景 (来自世界知识)】\n{wk_slim}\n请将以上宏观背景纳入 chain 和 delivery 判断。"
    full_prompt = v3.PROMPT_V3E + wk_section + sj[:8000] + attr_hint

    print(f"\n{'='*80}")
    print(f"【{code}】 prompt 总长 = {len(full_prompt)} 字符")
    print(f"{'='*80}")

    # 直接调 _llm (已有 3 次重试), 打印原始返回
    t0 = time.time()
    raw = v3._llm(full_prompt)
    dt = time.time() - t0

    if raw is None:
        print(f"  ❌ _llm 返回 None (3次重试全失败, 耗时{dt:.0f}s) — 连接/超时类错误")
        continue

    print(f"  ✅ _llm 返回内容, 长度={len(raw)} 字符, 耗时{dt:.0f}s")
    print(f"  --- 原始返回 (前 1500 字符) ---")
    print(raw[:1500])
    print(f"  --- 原始返回 (后 500 字符) ---")
    print(raw[-500:] if len(raw) > 500 else "")

    # 尝试解析
    parsed = v3._parse(raw)
    if parsed:
        print(f"  ✅ 解析成功: chain={parsed['chain']} delivery={parsed['delivery']} capital={parsed['capital']}")
    else:
        print(f"  ❌ 解析失败 — 见上方原始返回找原因 (截断? markdown包裹? 无JSON?)")
