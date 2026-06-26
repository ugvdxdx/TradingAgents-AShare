"""debate_picker v5 — 候选股格式化与评判辅助函数。

本模块只保留被 debaters.py (生产: make_ranking_debate 量化锚排序) 和
reporter.py 复用的格式化/辅助函数 (format_stock_brief / format_comparison_matrix /
_confidence_level 等), 不再有独立节点。
旧的 make_screen_round1 / make_final_judge / 海选逻辑已废弃 (详见下方说明)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from .data_io import anchor_score  # 排序锚唯一真相源: chain+capital×2+surge×SURGE_WEIGHT


def _trace(node: str, note: str) -> dict:
    return {"node": node, "note": note, "ts": datetime.now().isoformat()}


def _dump(run_dir: str, name: str, content: Any, as_json: bool = False):
    path = os.path.join(run_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        if as_json:
            json.dump(content, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(content))


def _confidence_level(conf: float) -> str:
    """置信度分级 (高/中/低)。"""
    if conf >= 0.7:
        return "高"
    if conf >= 0.4:
        return "中"
    return "低"


def format_stock_brief(c: Dict[str, Any]) -> str:
    """候选股精简档案 (喂给评委/辩论的统一格式)。

    v6: 新增量价动量信号 r20/r5/距高点 (回测验证的最强涨幅先行指标)。
    """
    e = c.get("essence", {})
    star_tag = " ★新晋股(量价归因)" if c.get("_rising_star") else ""
    research_tag = " ☆研报热门" if c.get("_research_hot") else ""
    # 量价动量信号 (回测: r20>15% 或距高点<5% 的股后续涨幅显著更高)
    r20 = c.get("r20")
    r5 = c.get("r5")
    dist_high = c.get("dist_high")
    mom_tag = ""
    if r20 is not None:
        fire = "🔥" if r20 >= 15 else ("📈" if r5 and r5 >= 3 else "")
        break_tag = "⚡即将突破" if dist_high is not None and dist_high >= -3 else ""
        mom_parts = [f"r20={r20:+.0f}%"]
        if r5 is not None:
            mom_parts.append(f"r5={r5:+.0f}%")
        if dist_high is not None:
            mom_parts.append(f"距高点{dist_high:+.0f}%")
        mom_tag = f" {fire}{break_tag}({', '.join(mom_parts)})"
    # 量化锚 (排序依据, 真相源 data_io.anchor_score): chain + capital×2 + surge×SURGE_WEIGHT
    anchor = anchor_score(c)
    return (
        f"{c['code']} {c['name']} 锚={anchor:.1f}{star_tag}{research_tag}{mom_tag} "
        f"[链{c['chain']}+爆{c['surge']}+资{c['capital']}]\n"
        f"  卡位:{e.get('chain_position', '')} | 催化:{e.get('core_catalyst', '')}\n"
        f"  多头:{e.get('biggest_bull', '')} | 空头:{e.get('biggest_bear', '')}\n"
        f"  红线:{e.get('quality_redline', '')} | horizon:{e.get('catalyst_horizon', 'mid')}\n"
        f"  实时: tech={c['tech_total']:.0f}/100(趋势{c['tech_trend']:.0f}) "
        f"5日主力净{c['fund_5d']:+.1f}亿"
    )


def format_comparison_matrix(finalists: List[Dict[str, Any]]) -> str:
    """生成候选股横向对比矩阵 (按板块分组), 帮助 LLM 做相对排名判断。

    解决"逐只孤立展示无法横向比较"的核心矛盾:
    - 按板块分组, 同板块内可直接对比 V3/涨幅/资金/技术位置
    - 明确标注同板块替代关系 (如两只光模块龙头互相竞争)
    """
    if not finalists:
        return ""

    # 简单板块归类 (从 industry 提取关键词)
    SECTOR_KEYWORDS = {
        "光模块/光通信": ["光模块", "光通信", "CPO", "光器件", "光纤"],
        "PCB/CCL": ["PCB", "覆铜板", "电路板", "电子布"],
        "存储/HBM": ["存储", "HBM", "DRAM"],
        "AI芯片/算力": ["AI芯片", "GPU", "ASIC", "算力", "服务器"],
        "半导体材料": ["半导体材料", "电子特气", "CMP", "靶材", "光刻"],
        "半导体设备": ["半导体设备", "刻蚀", "薄膜"],
        "MLCC/被动元件": ["MLCC", "被动元件", "电感", "电容"],
        "AI电源/散热": ["电源", "散热", "液冷", "温控"],
        "铜/有色": ["铜", "钨", "钼", "稀土", "有色"],
    }

    def guess_sector(c):
        ind = c.get("essence", {}).get("chain_position", "") + " " + str(c.get("name", ""))
        for sector, kws in SECTOR_KEYWORDS.items():
            if any(kw in ind for kw in kws):
                return sector
        return "其他"

    # 分组
    from collections import defaultdict
    groups = defaultdict(list)
    for c in finalists:
        groups[guess_sector(c)].append(c)

    lines = ["【候选股横向对比矩阵】(按板块分组, 按锚分降序)"]
    lines.append("  锚=chain+capital×2+surge×SURGE_WEIGHT (anchor_score 真相源, 回测Spearman+0.555)")
    for sector, stocks in sorted(groups.items(), key=lambda x: -len(x[1])):
        lines.append(f"\n  ▸ {sector} ({len(stocks)}只):")
        # 按anchor降序排
        for c in sorted(stocks, key=lambda x: -(anchor_score(x))):
            star = "★" if c.get("_rising_star") else ("☆" if c.get("_research_hot") else " ")
            anchor = anchor_score(c)
            r20 = c.get("r20")
            r20s = f"r20={r20:+.0f}%" if r20 is not None else ""
            lines.append(
                f"    {star} {c['code']} {c['name']:8} 锚={anchor:4.1f} "
                f"[链{c['chain']}+资{c['capital']}+爆{c['surge']}] {r20s} 资金{c['fund_5d']:+.1f}亿"
            )
        if len(stocks) >= 2:
            lines.append(f"    ⚡ 同板块竞争: {', '.join(c['name'] for c in stocks)}")

    return "\n".join(lines)


def _apply_ranking(group: List[Dict[str, Any]], result: List[dict]) -> List[Dict[str, Any]]:
    """按 LLM 排序结果重排一组候选股, 遗漏的按 V3 补末尾。"""
    cmap = {c["code"]: c for c in group}
    ordered: List[Dict[str, Any]] = []
    for r in result:
        code = str(r.get("code", "")).strip()
        if code in cmap:
            c = cmap.pop(code)
            c["screen_reason"] = r.get("reason", "")
            ordered.append(c)
    for c in sorted(cmap.values(), key=lambda x: -x["v3"]):
        ordered.append(c)
    return ordered



# ══════════════════════════════════════════════════════════
# 废弃节点说明
# ══════════════════════════════════════════════════════════
# 旧的 make_screen_round1 (分组海选) / make_screen_debate / make_final_judge
# (终极PK) 已废弃 — LLM 辩论回测为负相关(-0.14), 重构为纯量化锚排序。
# 当前生产路径仅 debaters.make_ranking_debate (量化锚 chain+capital×2+surge×SURGE_WEIGHT)。
# 本文件仅保留被 debaters.py / reporter.py 复用的格式化/辅助函数 (上方已定义)。
