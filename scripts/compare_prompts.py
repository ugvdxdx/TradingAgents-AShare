#!/usr/bin/env python3
"""
新旧 V3 Prompt 对比工具 — A/B 验证 PROMPT_V3E 升级效果。

用法:
  1. 在升级前备份旧 V3 cache:
     cp data/caches/fundamental_v3_scores.json data/caches/fundamental_v3_scores.json.old

  2. 跑新 prompt:
     python3 picker/scoring/v3_full_score.py

  3. 对比:
     python3 scripts/compare_prompts.py

     python3 scripts/compare_prompts.py --sample 30         # 抽查30只详细对比
     python3 scripts/compare_prompts.py --essence-quality    # essence 质量专项检查
     python3 scripts/compare_prompts.py --surge-audit     # surge 交叉验证审计
     python3 scripts/compare_prompts.py --chain-calibration  # chain 区分度检查
     python3 scripts/compare_prompts.py --all                # 全部检查

产出:
  - 终端彩色对比表 (chain/surge/essence 变化明细)
  - data/caches/prompt_compare_report.json (结构化对比数据, 供后续分析)
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

# 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picker.paths as paths

OLD_CACHE_PATH = os.path.join(paths.DATA_DIR, "caches", "fundamental_v3_scores.json.old")
NEW_CACHE_PATH = paths.V3_CACHE
REPORT_PATH = os.path.join(paths.DATA_DIR, "caches", "prompt_compare_report.json")
FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR

# ANSI color
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

# ── quality check constants ──
BULL_EMPTY_PATTERNS = [
    "行业景气度", "政策支持", "国产替代大趋势", "行业高景气", "赛道高景气",
    "下游需求旺盛", "市场需求旺盛", "行业需求增长", "受益于", "景气度持续",
    "景气度上行", "需求增长", "国产化趋势", "行业发展", "产业趋势",
]
BEAR_EMPTY_PATTERNS = [
    "竞争加剧", "宏观不确定性", "估值偏高", "估值较高", "市场波动",
    "行业竞争", "下游需求波动", "需求不及预期", "经济下行", "市场情绪",
    "政策不确定性", "外部环境", "地缘政治风险",
]


def load_old() -> dict:
    if not os.path.exists(OLD_CACHE_PATH):
        print(f"{RED}旧缓存不存在: {OLD_CACHE_PATH}{RESET}")
        print(f"请先备份: cp {NEW_CACHE_PATH} {OLD_CACHE_PATH}")
        sys.exit(1)
    return json.load(open(OLD_CACHE_PATH))


def load_new() -> dict:
    if not os.path.exists(NEW_CACHE_PATH):
        print(f"{RED}新缓存不存在: {NEW_CACHE_PATH}{RESET}")
        sys.exit(1)
    return json.load(open(NEW_CACHE_PATH))


def load_name(code):
    try:
        with open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json")) as f:
            return json.load(f).get("name", "")
    except Exception:
        return ""


def load_financial_metrics(code):
    """读取 fundamentals JSON 中的财务关键指标"""
    try:
        with open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json")) as f:
            d = json.load(f)
        km = d.get("financial_health", {}).get("key_metrics", {})
        return {
            "revenue_yi": km.get("revenue_yi"),
            "net_margin_pct": km.get("net_margin_pct"),
            "roe_pct": km.get("roe_pct"),
            "gross_margin_pct": km.get("gross_margin_pct"),
        }
    except Exception:
        return {}


def color_delta(new_val, old_val, high_good=True):
    """给变化上色: 绿色=改善, 红色=恶化"""
    if old_val is None or new_val is None:
        return ""
    delta = new_val - old_val
    if abs(delta) < 0.2:
        return ""
    if high_good:
        return f"{GREEN}↑{delta:+.1f}{RESET}" if delta > 0 else f"{RED}↓{delta:+.1f}{RESET}"
    else:
        return f"{RED}↑{delta:+.1f}{RESET}" if delta > 0 else f"{GREEN}↓{delta:+.1f}{RESET}"


# ════════════════════════════════════════════════
# ① 核心对比: chain/surge/essence 变化
# ════════════════════════════════════════════════

def compare_core(old: dict, new: dict):
    """对比 chain/surge/ranking 变化。"""
    common = set(old.keys()) & set(new.keys())
    only_old = set(old.keys()) - set(new.keys())
    only_new = set(new.keys()) - set(old.keys())

    chain_deltas = []
    surge_deltas = []
    anchor_deltas = []
    rank_shifts = []  # (code, old_rank, new_rank, rank_delta)
    essence_changed = []

    # 计算排序
    old_ranked = sorted(
        [(c, v.get("sector_score", 0)) for c, v in old.items() if c in common],
        key=lambda x: -x[1],
    )
    new_ranked = sorted(
        [(c, v.get("sector_score", 0)) for c, v in new.items() if c in common],
        key=lambda x: -x[1],
    )
    old_rank_map = {c: i + 1 for i, (c, _) in enumerate(old_ranked)}
    new_rank_map = {c: i + 1 for i, (c, _) in enumerate(new_ranked)}

    for code in sorted(common):
        o = old[code]
        n = new[code]
        if not isinstance(o, dict) or not isinstance(n, dict):
            continue

        chain_old = o.get("chain")
        chain_new = n.get("chain")
        surge_old = o.get("surge")
        surge_new = n.get("surge")

        if chain_old is not None and chain_new is not None:
            d = round(chain_new - chain_old, 1)
            if d != 0:
                chain_deltas.append(d)

        if surge_old is not None and surge_new is not None:
            d = round(surge_new - surge_old, 1)
            if d != 0:
                surge_deltas.append(d)

        # 锚分变化
        anchor_old = (
            o.get("chain", 0) + o.get("capital", 0) * 2 - o.get("surge", 0) * 0.5
        )
        anchor_new = (
            n.get("chain", 0) + n.get("capital", 0) * 2 - n.get("surge", 0) * 0.5
        )
        anchor_deltas.append(round(anchor_new - anchor_old, 1))

        # 排名变化
        old_r = old_rank_map.get(code, 0)
        new_r = new_rank_map.get(code, 0)
        if old_r and new_r:
            rank_shift = old_r - new_r  # 正=排名上升
            if abs(rank_shift) >= 3:
                rank_shifts.append((code, old_r, new_r, rank_shift))

        # essence 变化
        old_ess = o.get("essence", {}) or {}
        new_ess = n.get("essence", {}) or {}
        if old_ess != new_ess:
            essence_changed.append(code)

    # ── 输出 ──
    print(f"{BOLD}{'='*80}{RESET}")
    print(f"{BOLD}  新旧 Prompt 对比报告{RESET}")
    print(f"{BOLD}{'='*80}{RESET}")
    print(f"  对比时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  旧缓存: {OLD_CACHE_PATH}")
    print(f"  新缓存: {NEW_CACHE_PATH}")
    print(f"  共有股: {len(common)} | 仅旧: {len(only_old)} | 仅新: {len(only_new)}")

    if only_old:
        print(f"\n  {YELLOW}仅存在于旧缓存: {', '.join(sorted(only_old)[:10])}{RESET}")
    if only_new:
        print(f"\n  {YELLOW}仅存在于新缓存: {', '.join(sorted(only_new)[:10])}{RESET}")

    print(f"\n{BOLD}── chain 变化 ──{RESET}")
    if chain_deltas:
        pos = [d for d in chain_deltas if d > 0]
        neg = [d for d in chain_deltas if d < 0]
        print(f"  变化的股数: {len(chain_deltas)}/{len(common)} "
              f"({GREEN}↑{len(pos)}{RESET} / {RED}↓{len(neg)}{RESET})")
        print(f"  平均变化: {sum(chain_deltas)/len(chain_deltas):+.2f}")
        print(f"  标准差: {(sum(d**2 for d in chain_deltas)/len(chain_deltas))**0.5:.2f}")
        # 分布
        hist = Counter()
        for d in chain_deltas:
            bucket = min(2, max(-2, int(d * 2) / 2))  # -2.0 ~ +2.0, 0.5步长
            hist[bucket] += 1
        print(f"  分布: ", end="")
        for k in sorted(hist.keys()):
            color = GREEN if k > 0 else (RED if k < 0 else "")
            print(f"{color}{k:+.1f}:{hist[k]}{RESET} ", end="")
        print()
    else:
        print(f"  chain 无变化 (可能新 prompt 尚未运行)")

    print(f"\n{BOLD}── surge 变化 ──{RESET}")
    if surge_deltas:
        pos = [d for d in surge_deltas if d > 0]
        neg = [d for d in surge_deltas if d < 0]
        print(f"  变化的股数: {len(surge_deltas)}/{len(common)} "
              f"({GREEN}↑{len(pos)}{RESET} / {RED}↓{len(neg)}{RESET})")
        print(f"  平均变化: {sum(surge_deltas)/len(surge_deltas):+.2f}")
        print(f"  标准差: {(sum(d**2 for d in surge_deltas)/len(surge_deltas))**0.5:.2f}")

    print(f"\n{BOLD}── 锚分 (chain+capital×2+surge×SURGE_WEIGHT) 变化 ──{RESET}")
    if anchor_deltas:
        print(f"  变化的股数: {sum(1 for d in anchor_deltas if d != 0)}/{len(common)}")
        print(f"  平均变化: {sum(anchor_deltas)/len(anchor_deltas):+.2f}")
        print(f"  最大变化: {min(anchor_deltas):+.1f} ~ {max(anchor_deltas):+.1f}")

    print(f"\n{BOLD}── 排名变化 (≥3位) ──{RESET}")
    if rank_shifts:
        rank_shifts.sort(key=lambda x: -abs(x[3]))
        print(f"  {len(rank_shifts)} 只股排名变动 ≥3 位:")
        for code, old_r, new_r, shift in rank_shifts[:15]:
            name = load_name(code)
            direction = f"{GREEN}↑{shift}{RESET}" if shift > 0 else f"{RED}↓{abs(shift)}{RESET}"
            print(f"    {code} {name:<8} #{old_r:>3} → #{new_r:>3} ({direction})")
        if len(rank_shifts) > 15:
            print(f"    ... 另有 {len(rank_shifts)-15} 只")
    else:
        print(f"  无排名显著变动")

    print(f"\n{BOLD}── essence 变化 ──{RESET}")
    print(f"  {len(essence_changed)}/{len(common)} 股的 essence 有变动")

    return {
        "chain_deltas": chain_deltas,
        "surge_deltas": surge_deltas,
        "anchor_deltas": anchor_deltas,
        "rank_shifts": [(c, r1, r2, s) for c, r1, r2, s in rank_shifts],
        "essence_changed_count": len(essence_changed),
        "common_count": len(common),
    }


# ════════════════════════════════════════════════
# ② essence 质量专项检查
# ════════════════════════════════════════════════

def check_essence_quality(new: dict):
    """检查新 prompt 的 essence 质量 vs 旧 prompt。"""
    print(f"\n{BOLD}{'='*80}{RESET}")
    print(f"{BOLD}  Essence 质量审计{RESET}")
    print(f"{BOLD}{'='*80}{RESET}")

    old_data = load_old() if os.path.exists(OLD_CACHE_PATH) else {}
    common = set(new.keys()) & set(old_data.keys()) if old_data else set()

    # 空话检测
    old_empty_bull = 0
    old_empty_bear = 0
    new_empty_bull = 0
    new_empty_bear = 0
    old_total = 0
    new_total = 0

    old_specific_bull = 0  # 含数字的
    new_specific_bull = 0
    old_specific_bear = 0
    new_specific_bear = 0

    # 矛盾检测 (bull=bear换个说法)
    improvements = []  # 旧prompt空话 → 新prompt具体的essence

    for code in sorted(common)[:200]:  # 抽查200只
        o = old_data.get(code, {})
        n = new.get(code, {})
        old_ess = o.get("essence", {}) or {}
        new_ess = n.get("essence", {}) or {}

        old_bull = old_ess.get("biggest_bull", "")
        old_bear = old_ess.get("biggest_bear", "")
        new_bull = new_ess.get("biggest_bull", "")
        new_bear = new_ess.get("biggest_bear", "")

        if old_bull:
            old_total += 1
        if new_bull:
            new_total += 1

        # 检测旧空话
        if any(p in old_bull for p in BULL_EMPTY_PATTERNS):
            old_empty_bull += 1
        if any(p in old_bear for p in BEAR_EMPTY_PATTERNS):
            old_empty_bear += 1
        if any(p in new_bull for p in BULL_EMPTY_PATTERNS):
            new_empty_bull += 1
        if any(p in new_bear for p in BEAR_EMPTY_PATTERNS):
            new_empty_bear += 1

        # 检测含数字 (具体)
        if any(c.isdigit() for c in old_bull):
            old_specific_bull += 1
        if any(c.isdigit() for c in new_bull):
            new_specific_bull += 1
        if any(c.isdigit() for c in old_bear):
            old_specific_bear += 1
        if any(c.isdigit() for c in new_bear):
            new_specific_bear += 1

        # 记录改善案例 (旧空话 → 新具体)
        old_is_empty = (
            any(p in old_bull for p in BULL_EMPTY_PATTERNS)
            or any(p in old_bear for p in BEAR_EMPTY_PATTERNS)
        )
        new_has_data = any(c.isdigit() for c in new_bull + new_bear)
        if old_is_empty and new_has_data:
            improvements.append({
                "code": code,
                "name": load_name(code),
                "old_bull": old_bull,
                "new_bull": new_bull,
                "old_bear": old_bear,
                "new_bear": new_bear,
            })

    print(f"\n{BOLD}── bull 空话率 ──{RESET}")
    if old_total:
        print(f"  旧 prompt: {old_empty_bull}/{old_total} ({old_empty_bull/max(old_total,1)*100:.1f}%)")
    if new_total:
        print(f"  新 prompt: {new_empty_bull}/{new_total} ({new_empty_bull/max(new_total,1)*100:.1f}%)")

    print(f"\n{BOLD}── bear 空话率 ──{RESET}")
    if old_total:
        print(f"  旧 prompt: {old_empty_bear}/{old_total} ({old_empty_bear/max(old_total,1)*100:.1f}%)")
    if new_total:
        print(f"  新 prompt: {new_empty_bear}/{new_total} ({new_empty_bear/max(new_total,1)*100:.1f}%)")

    print(f"\n{BOLD}── bull 含数据率 (含数字=更具体) ──{RESET}")
    if old_total:
        print(f"  旧 prompt: {old_specific_bull}/{old_total} ({old_specific_bull/max(old_total,1)*100:.1f}%)")
    if new_total:
        print(f"  新 prompt: {new_specific_bull}/{new_total} ({new_specific_bull/max(new_total,1)*100:.1f}%)")

    print(f"\n{BOLD}── bear 含数据率 ──{RESET}")
    if old_total:
        print(f"  旧 prompt: {old_specific_bear}/{old_total} ({old_specific_bear/max(old_total,1)*100:.1f}%)")
    if new_total:
        print(f"  新 prompt: {new_specific_bear}/{new_total} ({new_specific_bear/max(new_total,1)*100:.1f}%)")

    print(f"\n{BOLD}── 改善案例 (旧空话→新具体, 前10条) ──{RESET}")
    if improvements:
        for imp in improvements[:10]:
            print(f"  {CYAN}{imp['code']} {imp['name']}{RESET}")
            print(f"    旧bull: {RED}{imp['old_bull']}{RESET}")
            print(f"    新bull: {GREEN}{imp['new_bull']}{RESET}")
            print(f"    旧bear: {RED}{imp['old_bear']}{RESET}")
            print(f"    新bear: {GREEN}{imp['new_bear']}{RESET}")
    else:
        print(f"  无改善案例 (可能旧 prompt 已经较好, 或新 prompt 尚未跑)")

    return {
        "old_empty_bull_rate": old_empty_bull / max(old_total, 1),
        "new_empty_bull_rate": new_empty_bull / max(new_total, 1),
        "old_specific_bull_rate": old_specific_bull / max(old_total, 1),
        "new_specific_bull_rate": new_specific_bull / max(new_total, 1),
        "improvements": len(improvements),
    }


# ════════════════════════════════════════════════
# ③ surge 交叉验证审计
# ════════════════════════════════════════════════

def audit_surge(new: dict):
    """检查新 prompt 是否对低净利(<5%)的股票降低了 surge 分。

    这是 PROMPT_V3E 新增的交叉验证规则:
      "净利率<5% → 说明公司是代工/组装模式，即使有大客户也不超过6.0分"
    """
    print(f"\n{BOLD}{'='*80}{RESET}")
    print(f"{BOLD}  Delivery 交叉验证审计 (净利率红线规则){RESET}")
    print(f"{BOLD}{'='*80}{RESET}")

    old_data = load_old() if os.path.exists(OLD_CACHE_PATH) else {}
    common = set(new.keys()) & set(old_data.keys()) if old_data else set()

    low_margin_audit = []  # 低净利股 surge 变化
    high_margin_audit = []  # 高净利股 surge 变化 (对照组)

    for code in sorted(common):
        fin = load_financial_metrics(code)
        nm = fin.get("net_margin_pct")
        if nm is None:
            continue

        o = old_data.get(code, {})
        n = new.get(code, {})
        old_del = o.get("surge")
        new_del = n.get("surge")
        if old_del is None or new_del is None:
            continue

        delta = round(new_del - old_del, 1)
        name = load_name(code)

        if nm < 5:  # 低净利 — 新prompt应该降surge
            if delta != 0:
                low_margin_audit.append({
                    "code": code, "name": name,
                    "net_margin": nm,
                    "old_del": old_del, "new_del": new_del, "delta": delta,
                })
        elif nm > 20:  # 高净利 — 对照组, surge不应无故下降
            if delta < -0.5:
                high_margin_audit.append({
                    "code": code, "name": name,
                    "net_margin": nm,
                    "old_del": old_del, "new_del": new_del, "delta": delta,
                })

    print(f"\n{BOLD}── 低净利股 (<5%) surge 变化 ──{RESET}")
    if low_margin_audit:
        # 统计被降/被升的比例
        down = [a for a in low_margin_audit if a["delta"] < 0]
        up = [a for a in low_margin_audit if a["delta"] > 0]
        print(f"  变化的股数: {len(low_margin_audit)} "
              f"({GREEN}↓降{len(down)}{RESET} / {RED}↑升{len(up)}{RESET})")
        print(f"  平均变化: {sum(a['delta'] for a in low_margin_audit)/len(low_margin_audit):+.2f}")
        print(f"  期望: 低净利股 surge 应下降 (利润率红线规则)")
        if len(down) > len(up):
            print(f"  {GREEN}✓ 低净利股 surge 以降为主, 符合预期{RESET}")
        else:
            print(f"  {RED}⚠ 低净利股 surge 以升为主, 可能规则未生效{RESET}")

        # 展示 Top 变化
        for a in sorted(low_margin_audit, key=lambda x: x["delta"])[:10]:
            color = GREEN if a["delta"] < 0 else RED
            print(f"    {a['code']} {a['name']:<8} 净利{a['net_margin']:.1f}% "
                  f"surge {a['old_del']}→{color}{a['new_del']} ({a['delta']:+.1f}){RESET}")
    else:
        # 检查是否有低净利股但 surge 没变
        low_margin_total = 0
        low_margin_high_del = 0  # 低净利但 surge > 6
        for code in sorted(common):
            fin = load_financial_metrics(code)
            nm = fin.get("net_margin_pct")
            if nm is not None and nm < 5:
                low_margin_total += 1
                if new[code].get("surge", 0) > 6.0:
                    low_margin_high_del += 1
        print(f"  低净利股总数: {low_margin_total}")
        print(f"  其中 surge > 6.0 的: {low_margin_high_del}")
        if low_margin_high_del > 0:
            print(f"  {RED}⚠ 仍有 {low_margin_high_del} 只低净利股 surge > 6, 规则可能未生效{RESET}")
        else:
            print(f"  {GREEN}✓ 所有低净利股 surge ≤ 6{RESET}")

    print(f"\n{BOLD}── 高净利股 (>20%) surge 异常下降 ──{RESET}")
    if high_margin_audit:
        print(f"  {len(high_margin_audit)} 只高净利股 surge 下降 >0.5:")
        for a in sorted(high_margin_audit, key=lambda x: x["delta"])[:10]:
            print(f"    {a['code']} {a['name']:<8} 净利{a['net_margin']:.1f}% "
                  f"surge {a['old_del']}→{RED}{a['new_del']} ({a['delta']:+.1f}){RESET}")
        print(f"  建议人工复核: 这些股是否被误降 (正误降 vs 误伤)")
    else:
        print(f"  {GREEN}✓ 无高净利股被异常降分{RESET}")

    return {
        "low_margin_changed": len(low_margin_audit),
        "low_margin_down": len([a for a in low_margin_audit if a["delta"] < 0]),
        "high_margin_dropped": len(high_margin_audit),
    }


# ════════════════════════════════════════════════
# ④ chain 区分度检查
# ════════════════════════════════════════════════

def check_chain_calibration(new: dict):
    """检查同一板块内 chain 分数的区分度。

    旧 prompt 的问题: 同板块内 chain 几乎同分 (标准差 ≈ 0.2-0.5)
    新 prompt 期望: 同板块内有 0.5-1.5 的标准差
    """
    print(f"\n{BOLD}{'='*80}{RESET}")
    print(f"{BOLD}  Chain 区分度检查{RESET}")
    print(f"{BOLD}{'='*80}{RESET}")

    old_data = load_old() if os.path.exists(OLD_CACHE_PATH) else {}
    common = set(new.keys()) & set(old_data.keys()) if old_data else set()

    # 按板块分组 (从 industry 分类)
    try:
        from tradingagents.research.normalize import get_sector_keyword_index
        kw_index = get_sector_keyword_index()
    except Exception:
        kw_index = {}

    def classify(industry):
        if not industry:
            return "其他"
        best, best_hit, best_kw_len = "", 0, 0
        for sec, kws in kw_index.items():
            matched = [k for k in kws if k in industry]
            h = len(matched)
            if h <= 0:
                continue
            max_kw_len = max(len(k) for k in matched)
            if h > best_hit or (h == best_hit and max_kw_len > best_kw_len):
                best_hit, best_kw_len, best = h, max_kw_len, sec
        return best or "其他"

    def get_industry(code):
        try:
            with open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json")) as f:
                d = json.load(f)
            return d.get("industry", "") or d.get("business_overview", {}).get("industry", "")
        except Exception:
            return ""

    old_sector_chains = defaultdict(list)
    new_sector_chains = defaultdict(list)

    for code in sorted(common):
        industry = get_industry(code)
        sector = classify(industry)
        if not sector:
            continue

        o = old_data.get(code, {})
        n = new.get(code, {})
        old_c = o.get("chain")
        new_c = n.get("chain")
        if old_c is not None:
            old_sector_chains[sector].append(old_c)
        if new_c is not None:
            new_sector_chains[sector].append(new_c)

    print(f"\n{'板块':<20} {'股数':>5} {'旧σ':>7} {'新σ':>7} {'变化':>7}")
    print("-" * 50)

    total_old_sigma = 0
    total_new_sigma = 0
    count = 0

    for sector in sorted(old_sector_chains.keys() | new_sector_chains.keys()):
        old_vals = old_sector_chains.get(sector, [])
        new_vals = new_sector_chains.get(sector, [])
        if len(new_vals) < 3:
            continue
        n = len(new_vals)
        old_mean = sum(old_vals) / len(old_vals) if old_vals else 0
        new_mean = sum(new_vals) / n
        old_sigma = (sum((v - old_mean) ** 2 for v in old_vals) / len(old_vals)) ** 0.5 if old_vals else 0
        new_sigma = (sum((v - new_mean) ** 2 for v in new_vals) / n) ** 0.5

        delta = new_sigma - old_sigma
        color = GREEN if delta > 0 else (RED if delta < -0.1 else "")
        print(f"  {sector:<18} {n:>5} {old_sigma:>7.3f} {new_sigma:>7.3f} {color}{delta:>+7.3f}{RESET}")

        total_old_sigma += old_sigma
        total_new_sigma += new_sigma
        count += 1

    if count:
        avg_old = total_old_sigma / count
        avg_new = total_new_sigma / count
        print("-" * 50)
        print(f"  {'平均':<18} {'':>5} {avg_old:>7.3f} {avg_new:>7.3f} {avg_new-avg_old:>+7.3f}")
        if avg_new > avg_old:
            print(f"\n  {GREEN}✓ 新 prompt 板块内区分度提升 (标准差 ↑){RESET}")
        else:
            print(f"\n  {RED}⚠ 新 prompt 板块内区分度未提升{RESET}")

    return {
        "avg_old_sigma": total_old_sigma / count if count else 0,
        "avg_new_sigma": total_new_sigma / count if count else 0,
    }


# ════════════════════════════════════════════════
# ⑤ 抽样详细对比
# ════════════════════════════════════════════════

def sample_detail(old: dict, new: dict, n: int = 30):
    """随机抽样 N 只股, 逐只对比 chain/surge/essence 详情。"""
    import random

    print(f"\n{BOLD}{'='*80}{RESET}")
    print(f"{BOLD}  抽样详细对比 (随机 {n} 只){RESET}")
    print(f"{BOLD}{'='*80}{RESET}")

    common = sorted(set(old.keys()) & set(new.keys()))
    random.seed(42)
    sample = random.sample(common, min(n, len(common)))

    for code in sample:
        o = old[code]
        n = new[code]
        name = load_name(code)
        anchor_old = (
            o.get("chain", 0) + o.get("capital", 0) * 2 - o.get("surge", 0) * 0.5
        )
        anchor_new = (
            n.get("chain", 0) + n.get("capital", 0) * 2 - n.get("surge", 0) * 0.5
        )

        print(f"\n  {BOLD}{code} {name}{RESET}")
        print(f"  chain:  {o.get('chain', '?'):.1f} → {n.get('chain', '?'):.1f} "
              f"{color_delta(n.get('chain',0), o.get('chain',0))}", end="  ")
        print(f"surge: {o.get('surge', '?'):.1f} → {n.get('surge', '?'):.1f} "
              f"{color_delta(n.get('surge',0), o.get('surge',0))}", end="  ")
        print(f"锚: {anchor_old:.1f} → {anchor_new:.1f} "
              f"{color_delta(anchor_new, anchor_old)}")

        old_ess = o.get("essence", {}) or {}
        new_ess = n.get("essence", {}) or {}
        if old_ess.get("biggest_bull") != new_ess.get("biggest_bull"):
            print(f"  bull: {RED}{old_ess.get('biggest_bull','?')}{RESET} → "
                  f"{GREEN}{new_ess.get('biggest_bull','?')}{RESET}")
        if old_ess.get("biggest_bear") != new_ess.get("biggest_bear"):
            print(f"  bear: {RED}{old_ess.get('biggest_bear','?')}{RESET} → "
                  f"{GREEN}{new_ess.get('biggest_bear','?')}{RESET}")
        if old_ess.get("core_catalyst") != new_ess.get("core_catalyst"):
            print(f"  催化: {RED}{old_ess.get('core_catalyst','?')}{RESET} → "
                  f"{GREEN}{new_ess.get('core_catalyst','?')}{RESET}")


# ════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════

def main():
    args = set(sys.argv[1:])
    do_all = "--all" in args

    if not os.path.exists(OLD_CACHE_PATH):
        print(f"{RED}旧缓存不存在: {OLD_CACHE_PATH}{RESET}")
        print(f"\n请先备份当前 V3 缓存:\n"
              f"  cp {NEW_CACHE_PATH} {OLD_CACHE_PATH}")
        sys.exit(1)

    old = load_old()
    new = load_new()

    report = {}

    # ① 核心对比 (默认总跑)
    report["core"] = compare_core(old, new)

    # ② essence 质量
    if do_all or "--essence-quality" in args:
        report["essence"] = check_essence_quality(new)

    # ③ surge 审计
    if do_all or "--surge-audit" in args:
        report["surge"] = audit_surge(new)

    # ④ chain 区分度
    if do_all or "--chain-calibration" in args:
        report["chain"] = check_chain_calibration(new)

    # ⑤ 抽样
    if do_all or "--sample" in args:
        n = 30
        for i, a in enumerate(sys.argv):
            if a == "--sample" and i + 1 < len(sys.argv):
                try:
                    n = int(sys.argv[i + 1])
                except ValueError:
                    pass
        sample_detail(old, new, n)

    # 保存报告
    report["_meta"] = {
        "compared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "old_path": OLD_CACHE_PATH,
        "new_path": NEW_CACHE_PATH,
    }
    json.dump(report, open(REPORT_PATH, "w"), ensure_ascii=False, indent=1)
    print(f"\n{BOLD}结构化报告已保存: {REPORT_PATH}{RESET}")


if __name__ == "__main__":
    main()
