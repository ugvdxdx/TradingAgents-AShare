"""chain 分档动态化 (赛道→档位映射, 随市场主线更新)。

设计原理
========
chain 分档 = 6 档可重叠【热度】骨架 (赛道热度×竞争力; 档=热度带, 档间有意重叠)
+ 动态赛道映射 (哪档放哪些赛道)。市场主线热度变化时需要更新的是后者。
本模块把"赛道→档位映射"从 PROMPT_V3E 硬编码文本外部化为
data/reference/chain_tier_map.json, 使其可被 update_chain_tiers.py
(--mode manual/auto) 动态更新。

两种应用模式 (A+B 并存, 方便对比):
  - manual (方案A): LLM 生成候选 tier_map → 输出 diff → 人工确认 → 写入
  - auto   (方案B): 检测主线位移 → 生成候选 → 回测 gate 验证不劣化 → 自动应用/回滚/告警

阶段0 (本文件): load/render/get_chain_prompt —— 不改评分行为的地基。
  种子 v1 从 PROMPT_V3E:121-131 抽取, render_chain_tiers_block 渲染后与原硬编码等价。
阶段1+: build_candidate_tier_map / diff_tier_maps / 主线检测 / 回测 gate。
"""

import json
import os
from typing import Any, Dict, Optional, Tuple

from picker.paths import CHAIN_TIER_MAP_PATH, CHAIN_TIER_ARCHIVE_DIR

# ──────────────────────────────────────────────────────────────
# 进程级缓存 (mtime 感知: tier_map 文件被 update_chain_tiers.py 改写后,
# 同进程下次 load 自动刷新; 跨进程自然重读)
# ──────────────────────────────────────────────────────────────
_CHAIN_TIER_MAP_CACHE: Optional[Tuple[float, Dict[str, Any]]] = None


def load_chain_tier_map() -> Optional[Dict[str, Any]]:
    """读取 chain_tier_map.json。

    Returns:
        tier_map dict, 或 None (文件缺失/损坏时, 调用方回退到 PROMPT_V3E 硬编码)。
    """
    global _CHAIN_TIER_MAP_CACHE
    path = CHAIN_TIER_MAP_PATH
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _CHAIN_TIER_MAP_CACHE[1] if _CHAIN_TIER_MAP_CACHE else None
    if _CHAIN_TIER_MAP_CACHE and _CHAIN_TIER_MAP_CACHE[0] == mtime:
        return _CHAIN_TIER_MAP_CACHE[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            tm = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _CHAIN_TIER_MAP_CACHE[1] if _CHAIN_TIER_MAP_CACHE else None
    if not isinstance(tm, dict) or not tm.get("tiers"):
        return _CHAIN_TIER_MAP_CACHE[1] if _CHAIN_TIER_MAP_CACHE else None
    _CHAIN_TIER_MAP_CACHE = (mtime, tm)
    return tm


def reload_chain_tier_map() -> Optional[Dict[str, Any]]:
    """强制清缓存重读 (同进程内更新 tier_map 后立即生效)。"""
    global _CHAIN_TIER_MAP_CACHE
    _CHAIN_TIER_MAP_CACHE = None
    return load_chain_tier_map()


# ──────────────────────────────────────────────────────────────
# 渲染: tier_map → prompt 档位规则文本
# ──────────────────────────────────────────────────────────────
def render_chain_tiers_block(tier_map: Dict[str, Any]) -> str:
    """渲染 tier_map 为 PROMPT_V3E 风格的档位规则文本块。

    输出格式与 PROMPT_V3E:123-131 原文一致 (种子 v1 渲染后逐行等价):
        **档位规则**：
        - 9.0-10.0: AI算力最核心环节 (1.6T光模块/HBM/CoWoS/AI主芯片)，全球份额前3...
        - ...
    """
    lines = ["**档位规则**："]
    for t in tier_map.get("tiers", []):
        rng = t.get("range", "")
        label = t.get("label", "")
        sectors = t.get("sectors") or []
        criteria = t.get("criteria") or ""
        inner = "/".join(s for s in sectors if s)
        if inner and criteria:
            # criteria 以"等"开头 (如"等非AI链") 表示 sectors 为举例, 合并进括号 (中文表达习惯)
            if criteria.startswith("等"):
                line = f"- {rng}: {label} ({inner}{criteria})"
            else:
                line = f"- {rng}: {label} ({inner})，{criteria}"
        elif inner:
            line = f"- {rng}: {label} ({inner})"
        elif criteria:
            line = f"- {rng}: {label} ({criteria})"
        else:
            line = f"- {rng}: {label}"
        lines.append(line)
    return "\n".join(lines)


# PROMPT_V3E 中档位规则段的起止锚点 (用于等价替换)。
# ⚠ 起始锚点只取 "**档位规则" (不含 **：) — PROMPT_V3E 改版后该行可能带括注
#   (如 "**档位规则 (6档热度带...)...："), 死写 "**档位规则**：" 会导致 find 失败 →
#   动态 tier_map 永远不注入, update_chain_tiers 变成空操作 (曾发生的静默 bug)。
#   只取前缀即兼容 "**档位规则**：" 与 "**档位规则 (...)...**：" 两种写法。
# 终止锚点取档位规则段之后的下一个块 (当前为 "**竞争力档内分化"); 注意不可取
#   "**边界判断规则" — 它在竞争力块之后, 会把竞争力分化段一并吞掉。
_BLOCK_START_MARKER = "**档位规则"
_BLOCK_END_MARKER = "\n\n**竞争力档内分化"

# 进程内"锚点失效已告警"标记 (避免每只股都刷一遍告警)
_SPLICE_FALLBACK_WARNED = False


def _splice_chain_block(prompt: str, rendered_block: str) -> Optional[str]:
    """把 prompt 中的档位规则段替换为 rendered_block。

    锚点命中 → 返回替换后的 prompt; 锚点缺失 (PROMPT_V3E 结构漂移) → 返回 None,
    交由调用方决定回退策略 (见 get_chain_prompt)。返回 None 而非原 prompt, 是为
    了让"动态注入失效"可被上层感知并告警, 而不是静默降级。
    """
    si = prompt.find(_BLOCK_START_MARKER)
    ei = prompt.find(_BLOCK_END_MARKER)
    if si == -1 or ei == -1 or ei <= si:
        return None  # 锚点失效 → 调用方回退并告警 (不静默)
    return prompt[:si] + rendered_block + prompt[ei:]


def get_chain_prompt() -> str:
    """返回完整 V3E prompt, 档位块来自 chain_tier_map.json (动态)。

    tier_map 缺失/损坏时回退到 PROMPT_V3E 原文 (硬编码档位, 向后兼容,
    保证旧环境无 chain_tier_map.json 也能正常评分)。
    tier_map 存在但 PROMPT_V3E 锚点对不上 (结构漂移) 时也回退, 但会告警 —
    否则 update_chain_tiers 写了文件, 评分却永远用硬编码, 动态化形同虚设。
    """
    global _SPLICE_FALLBACK_WARNED
    # 延迟 import 避免与 v3_full_score 循环依赖
    from picker.scoring.v3_full_score import PROMPT_V3E

    tm = load_chain_tier_map()
    if not tm:
        return PROMPT_V3E  # 回退: 无 tier_map 文件 (首次部署前属正常)
    rendered = render_chain_tiers_block(tm)
    spliced = _splice_chain_block(PROMPT_V3E, rendered)
    if spliced is None:
        if not _SPLICE_FALLBACK_WARNED:
            _SPLICE_FALLBACK_WARNED = True
            print("⚠ chain_tiers: PROMPT_V3E 档位块锚点失效, 动态 tier_map 未注入 → 回退硬编码。"
                  "请检查 _BLOCK_START/END_MARKER 是否与 PROMPT_V3E 当前结构一致。", flush=True)
        return PROMPT_V3E
    return spliced


def get_tier_version() -> str:
    """当前生效的 chain tier 版本标识 (写入快照, 支撑回测分段与 A/B 对比)。"""
    tm = load_chain_tier_map()
    if not tm:
        return "PROMPT_V3E-hardcoded"  # 回退路径的版本标记
    return tm.get("version", "unknown")


# ──────────────────────────────────────────────────────────────
# diff / 持久化 (纯函数, candidate/manual 共用)
# ──────────────────────────────────────────────────────────────
def diff_tier_maps(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> str:
    """生成人类可读的新旧 tier_map 差异 (供 manual 审核)。"""
    if not old:
        return f"【全新 tier_map】 theme={new.get('theme','?')} | {len(new.get('tiers',[]))} 档"
    lines = []
    if old.get("theme") != new.get("theme"):
        lines.append(f"【主线变化】 {old.get('theme','?')}  →  {new.get('theme','?')}")
    if old.get("theme_strength") != new.get("theme_strength"):
        lines.append(f"【主线强度】 {old.get('theme_strength','?')}  →  {new.get('theme_strength','?')}")
    old_by_range = {t.get("range"): t for t in old.get("tiers", [])}
    new_by_range = {t.get("range"): t for t in new.get("tiers", [])}
    all_ranges = list(old_by_range.keys()) + [r for r in new_by_range if r not in old_by_range]
    for r in all_ranges:
        ot = old_by_range.get(r)
        nt = new_by_range.get(r)
        o_sec = set(ot.get("sectors") or []) if ot else set()
        n_sec = set(nt.get("sectors") or []) if nt else set()
        added = sorted(n_sec - o_sec)
        removed = sorted(o_sec - n_sec)
        label_changed = bool(ot and nt and ot.get("label") != nt.get("label"))
        if not ot:
            lines.append(f"  [{r}] ✚新增档 {nt.get('label','')}  {added}")
        elif not nt:
            lines.append(f"  [{r}] ✖删除档")
        elif added or removed or label_changed:
            tag = f"[{r}] {nt.get('label','')}"
            if label_changed:
                tag += f" (label: {ot.get('label','')}→{nt.get('label','')})"
            if added:
                tag += f"  +{added}"
            if removed:
                tag += f"  -{removed}"
            lines.append("  " + tag)
    if not lines:
        return "(无变化)"
    return "\n".join(lines)


def archive_current_tier_map(reason: str = "") -> Optional[str]:
    """归档当前 tier_map 到 CHAIN_TIER_ARCHIVE_DIR (带时间戳+原因)。返回归档路径或 None。"""
    from datetime import datetime
    cur = load_chain_tier_map()
    if not cur:
        return None
    os.makedirs(CHAIN_TIER_ARCHIVE_DIR, exist_ok=True)
    ver = cur.get("version", "unknown")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reason = (reason or "archive").replace(" ", "_").replace("/", "-")
    name = f"{ver}__{ts}__{safe_reason}.json".replace("/", "-")
    path = os.path.join(CHAIN_TIER_ARCHIVE_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return path


def save_chain_tier_map(tier_map: Dict[str, Any], generated_by: str = "", archive_reason: str = "") -> str:
    """归档当前版本后写入新 tier map, 刷新进程缓存。返回写入路径。"""
    from datetime import datetime
    archive_current_tier_map(archive_reason or "before-save")  # 先归档旧版
    tier_map["generated_at"] = datetime.now().strftime("%Y-%m-%d")
    if generated_by:
        tier_map["generated_by"] = generated_by
    with open(CHAIN_TIER_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(tier_map, f, ensure_ascii=False, indent=2)
    reload_chain_tier_map()
    return CHAIN_TIER_MAP_PATH


# ──────────────────────────────────────────────────────────────
# 阶段1: 候选生成 (从最新研报构建新 tier_map)
# ──────────────────────────────────────────────────────────────

# 6档【热度】骨架 (档位间有意重叠 — 热度x竞争力是连续的, 重叠让强竞争力股能跨档)
# chain 语义: 赛道热度(theme级) × 个股核心竞争力。档=热度带, 档内分数由竞争力定。
# 重叠区: 强竞争力的温热股可追平弱竞争力的热门股 (竞争力跨档)。
_TIER_SKELETON_RANGES = ["8.5-10.0", "7.0-9.0", "5.5-7.5", "3.5-5.5", "2.0-4.0", "0.0-2.5"]


def _gather_research_signals(days=14):
    """从 research.db + 异动分析 + 世界知识 汇总最新主线信号, 供 LLM 调整 tier_map。

    三信号融合:
      1. 研报信号: hot/cold/emerging sectors (LLM 从研报提取)
      2. 异动信号: price-confirmed 热门 (实际有股在涨 + web search 驱动) ← 比研报更硬
      3. 世界知识: 宏观主线

    Returns:
        {hot_sectors, cold_sectors, emerging_sectors, top_viewpoints,
         world_knowledge_theme, price_confirmed_hot, price_confirmed_cold}
    """
    import sqlite3
    from datetime import datetime, timedelta
    from picker.paths import RESEARCH_DB, WORLD_KNOWLEDGE_MD, DATA_DIR
    out = {"hot_sectors": [], "cold_sectors": [], "emerging_sectors": [],
           "top_viewpoints": [], "world_knowledge_theme": "",
           "price_confirmed_hot": [], "price_confirmed_cold": [],
           "gap_themes": []}

    # 研报板块动量 (复用 consumer 的聚合)
    try:
        from tradingagents.research.consumer import get_sector_momentum
        m = get_sector_momentum(days=days)
        out["hot_sectors"] = [{"sector": s["sector"], "bullish": s.get("bullish_count", 0)}
                              for s in m.get("hot_sectors", [])[:10]]
        out["cold_sectors"] = [{"sector": s["sector"], "bearish": s.get("bearish_count", 0)}
                               for s in m.get("cold_sectors", [])[:5]]
        out["emerging_sectors"] = [s["sector"] for s in m.get("emerging_sectors", [])[:5]]
    except Exception:
        pass

    # 近期代表性看多观点 (细粒度, 反映具体主题)
    try:
        conn = sqlite3.connect(RESEARCH_DB)
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT sector, viewpoint FROM sector_knowledge "
            "WHERE sentiment='bullish' AND created_at >= ? AND viewpoint != '' "
            "ORDER BY created_at DESC LIMIT 25", (cutoff,)
        ).fetchall()
        conn.close()
        out["top_viewpoints"] = [f"[{r[0]}] {r[1]}" for r in rows]
    except Exception:
        pass

    # 异动分析信号 (price-confirmed: 实际有股在涨/跌 + web search 归因)
    # 比研报bullish count更硬 — 是价格验证的真实热度
    # 读统一归因缓存 (替代已废弃的 surge_driver_cache.json)
    try:
        from picker.discovery.attribution import ATTR_TTL_DAYS
        from picker.paths import UNIFIED_ATTR_CACHE
        if os.path.exists(UNIFIED_ATTR_CACHE):
            sc = json.load(open(UNIFIED_ATTR_CACHE))
            today = datetime.now()
            for code, entry in sc.items():
                try:
                    cd = entry.get("cached_date") or entry.get("date", "2000-01-01")
                    age = (today - datetime.strptime(str(cd)[:10], "%Y-%m-%d")).days
                    if age > ATTR_TTL_DAYS:
                        continue  # 过期
                    summary = entry.get("summary") or entry.get("driver", "")
                    r20 = entry.get("r20", 0)
                    direction = entry.get("direction", "")
                    sector_tag = entry.get("sector_tag", "")
                    if not summary:
                        continue
                    tag = f" [{sector_tag}]" if sector_tag else ""
                    item = f"{code} r20={r20}%{tag} {summary}"
                    if direction == "上涨":
                        out["price_confirmed_hot"].append(item)
                    elif direction == "下跌":
                        out["price_confirmed_cold"].append(item)
                except Exception:
                    pass
            out["price_confirmed_hot"] = out["price_confirmed_hot"][:20]
            out["price_confirmed_cold"] = out["price_confirmed_cold"][:10]
    except Exception:
        pass

    # 缺口发现信号 (热门但池未覆盖的主题 → 应加入tier_map的新兴赛道)
    try:
        gap_cache_path = os.path.join(DATA_DIR, "caches", "gap_themes_cache.json")
        if os.path.exists(gap_cache_path):
            gc = json.load(open(gap_cache_path))
            age = (datetime.now() - datetime.strptime(gc.get("date", "2000-01-01"), "%Y-%m-%d")).days
            if age <= 7:
                out["gap_themes"] = [t.get("theme", "") for t in gc.get("themes", []) if t.get("theme")]
    except Exception:
        pass

    # 世界知识主线 (读首段/标题行)
    try:
        if os.path.exists(WORLD_KNOWLEDGE_MD):
            with open(WORLD_KNOWLEDGE_MD, "r", encoding="utf-8") as f:
                wk = f.read()
            # 提取标题 + 一级章节, 概括主线
            lines = [ln.strip() for ln in wk.split("\n") if ln.strip().startswith("#")][:15]
            out["world_knowledge_theme"] = "\n".join(lines)
    except Exception:
        pass

    return out


def build_candidate_tier_map(days=14):
    """LLM 根据最新研报, 生成候选 tier_map (6档热度骨架, 档间可重叠)。

    chain 语义 = 赛道热度(theme级) × 个股核心竞争力。档=热度带(可重叠),
    赛道按【当前热度】归档(热主线→高档, 退潮→低档), 档内分数由竞争力定(评分时)。
    失败返回 None。
    """
    from picker.scoring.v3_full_score import _llm  # 带 429 退避

    current = load_chain_tier_map()
    if not current:
        return None  # 无基准, 不构建 (阶段0要求种子存在)

    signals = _gather_research_signals(days=days)
    ranges = ", ".join(_TIER_SKELETON_RANGES)

    prompt = f"""你是A股量化研究员, 为"预测股票收益"维护 chain 分档(赛道→热度档映射)。

## 核心语义: chain = 赛道热度 × 个股竞争力 (目标是收益预测, 不是产业研究)
- 档位反映赛道【当前热度】(theme级, 周-月尺度): 热主线赛道→高档(资金涌入推升股价), 退潮赛道→低档。
- 档内具体分数由个股【核心竞争力】定(评分时另算): 龙头/高份额/技术壁垒 → 档内高分; 跟风 → 档内低分。
- 档间有意重叠: 强竞争力的温热股可追平弱竞争力的热门股。

## 6档热度带 (固定, ranges/labels 不变, 档间重叠)
- 8.5-10.0 最热主线·绝对龙头带: AI算力绝对主线, 资金极致集中, 业绩+逻辑双驱 (龙头拿9+, 二线8.5-9)
- 7.0-9.0 热门主线·高景气带: AI链高景气环节, 业绩兑现中 (龙头8-9, 跟风7-8)
- 5.5-7.5 温热·新兴升温带: 升温中的新主题, 逻辑先行业绩待验证 (龙头7-7.5, 概念5.5-6.5)
- 3.5-5.5 中性·稳定需求带: 有需求但非当前热点 (龙头4.5-5.5, 一般3.5-4.5)
- 2.0-4.0 偏冷·景气下行带: 资金流出/景气下行 (龙头3-4, 弱势2-3)
- 0.0-2.5 冷门·退潮出清带: 旧赛道出清, 诚实给低分

## 当前 tier_map (基准 6档热度, 已含合理热度归档)
{json.dumps(current, ensure_ascii=False, indent=1)}

## 最新研报信号 (用于判断赛道【当前热度】, 调整归档)
近{days}天活跃赛道(bullish密集 → 偏热): {json.dumps(signals['hot_sectors'], ensure_ascii=False)}
走弱赛道(bearish密集 → 偏冷): {json.dumps(signals['cold_sectors'], ensure_ascii=False)}
新升温赛道(近7天新出现 → 温热/新兴带): {signals['emerging_sectors']}
代表性观点 (判断各赛道当前热度):
{chr(10).join('- ' + v for v in signals['top_viewpoints'][:15])}

## ⚡ 异动分析信号 (price-confirmed — 实际有股在涨/跌, 比研报bullish count更硬的验证)
实际上涨股 + web search驱动 (这些主题的价格已验证热度, 应在高档):
{chr(10).join('- ' + v for v in signals['price_confirmed_hot']) if signals['price_confirmed_hot'] else '(暂无异动数据)'}
实际下跌股 + 原因 (这些主题价格走弱, 可能降温):
{chr(10).join('- ' + v for v in signals['price_confirmed_cold']) if signals['price_confirmed_cold'] else '(暂无)'}

## 🔍 缺口发现信号 (热门但池未覆盖的主题 — 新兴赛道, 应加入tier_map对应热度档)
{', '.join(signals['gap_themes']) if signals['gap_themes'] else '(暂无缺口数据)'}

## 世界知识主线
{signals['world_knowledge_theme'][:600]}

## 调整规则 (按【热度】归档, 非产业链位置)
1. 6档 ranges/labels 不变 (热度带, 档间重叠是有意的)
2. 赛道按【当前热度】归入对应带: 热主线→8.5-10/7-9带; 新升温主题(如金刚石散热/PCIe Retimer)→5.5-7.5温热带(够热就别压低); 中性→3.5-5.5; 退潮→0-2.5
3. 热度变化才移动: 赛道升温(冷→温/温→热)或降温才调档, 给出热度依据
4. theme 一句话当前主线; theme_strength = 绝对主线(单一主线碾压)/主线(主导有副线)/多线均衡(无单一主线)
5. sectors 用具体细分名 (如"1.6T光模块"非"光通信"); criteria 简述热度+龙头定位依据

**第一行就以 `{{` 开始直接输出 JSON, 不要推理/前言** (GLM 推理会占满token致JSON截断)。
输出完整 tier_map JSON: 最外层 theme/theme_strength/tiers, tiers 6个元素含 range/label/sectors/criteria。range 严格按顺序: {ranges}。"""
    # tier_map JSON 较大 + GLM 推理开销, 用 4096 防截断
    raw = _llm(prompt, max_tokens=4096)
    if not raw:
        return None

    # GLM 可能仍带推理前缀, 找最后一个完整 JSON 对象 (从首个 { 到末个 })
    import re
    # 去除可能的 ```json 代码块标记
    raw2 = raw.strip()
    if raw2.startswith("```"):
        raw2 = raw2.split("```")[1] if "```" in raw2[3:] else raw2
        if raw2.startswith("json"):
            raw2 = raw2[4:]
    m = re.search(r'\{.*\}', raw2, re.S)
    if not m:
        return None
    try:
        candidate = json.loads(m.group())
    except json.JSONDecodeError:
        return None

    # 校验: 骨架一致 (6档可重叠热度带 + ranges 完全匹配)
    cand_ranges = [t.get("range") for t in candidate.get("tiers", [])]
    if cand_ranges != _TIER_SKELETON_RANGES:
        # 骨架被破坏 (LLM 改了 ranges), 拒绝 — 只接受赛道重映射
        return None
    if not candidate.get("theme") or not candidate.get("tiers"):
        return None
    return candidate


def update_chain_tiers(mode="manual", days=14, force=False):
    """构建候选 tier_map → diff → 按 mode 应用。

    Args:
        mode: 'manual' 只输出 diff 供审核(不写入); 'auto' diff有变化即写入(归档旧版, 可回滚)
        force: True 时即使无变化也重新写入 (刷新 generated_at)
    Returns:
        (candidate, diff_text, applied: bool)
    """
    current = load_chain_tier_map()
    candidate = build_candidate_tier_map(days=days)
    if not candidate:
        print("  [chain_tiers] 候选生成失败 (LLM/骨架校验), 跳过")
        return None, "", False

    diff = diff_tier_maps(current, candidate)
    print(f"  [chain_tiers] 候选 theme: {candidate.get('theme','?')}")
    print(f"  [chain_tiers] diff vs 当前:\n{diff if diff != '(无变化)' else '  (无变化)'}")

    if diff == "(无变化)" and not force:
        print("  [chain_tiers] 无变化, 不写入")
        return candidate, diff, False

    if mode == "auto":
        from datetime import datetime
        candidate["version"] = datetime.now().strftime("%Y-%m-%d") + "-auto"
        path = save_chain_tier_map(candidate, generated_by=f"update_chain_tiers(auto,{mode})",
                                   archive_reason=f"auto-update-{mode}")
        print(f"  [chain_tiers] ✓ 已写入 (旧版已归档可回滚): {path}")
        return candidate, diff, True
    else:
        print("  [chain_tiers] manual 模式: 未写入 (审核 diff 后用 --apply-chain-tiers 写入)")
        return candidate, diff, False
