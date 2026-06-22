"""买1卖2 策略信号生成。

策略规则:
  - 买入: 某股进入 TOP5 当天买入 (买1, 无需确认)
  - 卖出: 某股连续 2 天不在 TOP5 才卖出 (卖2, 容忍1天掉出)

每天跑 debate_picker_v5 后, 记录当天 TOP5 到历史文件, 并与历史对比生成信号:
  - 🟢 买入: 新进 TOP5 (昨天不在)
  - 🔴 卖出: 连续2天不在 TOP5 (前天在、昨天不在、今天也不在)
  - ⏸ 持有: 在 TOP5 里且昨天也在
  - ⚠ 观察: 掉出1天 (昨天在、今天不在, 还没触发卖出)

历史存储: data/caches/top5_history.json
  {date: [code1, code2, ...]}  # 按日期记录每天 TOP5
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                            "data", "caches", "top5_history.json")


def _load_history() -> Dict[str, List[str]]:
    """加载 TOP5 历史记录 {date: [codes]}。"""
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        return json.load(open(HISTORY_PATH, encoding="utf-8"))
    except Exception:
        return {}


def _save_history(history: Dict[str, List[str]]):
    """保存历史记录。"""
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    json.dump(history, open(HISTORY_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _prev_dates(history: Dict, trade_date: str, n: int = 2) -> List[str]:
    """获取 trade_date 之前最近的 n 个有记录的日期 (降序)。"""
    dates = sorted((d for d in history.keys() if d < trade_date), reverse=True)
    return dates[:n]


def update_and_signal(trade_date: str, top5_codes: List[str]) -> Tuple[Dict, Dict]:
    """记录当天 TOP5, 并生成买卖信号。

    Args:
        trade_date: 交易日 (YYYY-MM-DD)
        top5_codes: 当天 TOP5 股票代码列表 (已排序)

    Returns:
        (signals, history)
        signals = {
            "buy": [{"code", "name", "reason"}],      # 今日买入
            "sell": [{"code", "name", "reason"}],      # 今日卖出
            "hold": [{"code", "name", "reason"}],      # 继续持有
            "watch": [{"code", "name", "reason"}],     # 观察(掉出1天)
        }
    """
    history = _load_history()
    today_set = set(top5_codes)

    # 获取前2个交易日的历史
    prev_dates = _prev_dates(history, trade_date, 2)
    prev1_set = set(history[prev_dates[0]]) if len(prev_dates) >= 1 else set()
    prev2_set = set(history[prev_dates[1]]) if len(prev_dates) >= 2 else set()

    # 所有相关股票 (今天TOP5 + 近2天TOP5)
    all_codes = today_set | prev1_set | prev2_set

    # 记录持仓状态: 某股在 prev2 或 prev1 里 = 之前持有
    was_holding = prev2_set | prev1_set

    signals = {"buy": [], "sell": [], "hold": [], "watch": []}

    for code in all_codes:
        in_today = code in today_set
        in_prev1 = code in prev1_set
        in_prev2 = code in prev2_set

        if in_today and not (in_prev1 or in_prev2):
            # 新进 TOP5 → 买入
            signals["buy"].append({"code": code, "reason": "新进TOP5"})
        elif in_today and (in_prev1 or in_prev2):
            # 持续在 TOP5 → 持有
            signals["hold"].append({"code": code, "reason": "连续在TOP5"})
        elif not in_today and (in_prev1 or in_prev2):
            # 掉出 TOP5
            if in_prev1 and not in_prev2:
                # 昨天在、今天不在 → 掉出1天 (观察)
                signals["watch"].append({"code": code, "reason": "掉出TOP5第1天"})
            elif not in_prev1 and in_prev2:
                # 前天在、昨天不在、今天也不在 → 连续2天不在 → 卖出
                signals["sell"].append({"code": code, "reason": "连续2天不在TOP5"})
            elif in_prev1 and in_prev2:
                # 前天和昨天都在、今天不在 → 掉出1天 (观察)
                signals["watch"].append({"code": code, "reason": "掉出TOP5第1天"})

    # 记录当天 TOP5
    history[trade_date] = top5_codes
    # 只保留最近 30 天 (避免文件膨胀)
    cutoff = (datetime.strptime(trade_date, "%Y-%m-%d").replace(day=1)).strftime("%Y-%m-%d")
    # 简单保留: 按日期排序取最近60条
    sorted_dates = sorted(history.keys(), reverse=True)
    if len(sorted_dates) > 60:
        history = {d: history[d] for d in sorted_dates[:60]}
    _save_history(history)

    return signals, history


def format_signals(signals: Dict, code_to_name: Dict[str, str] = None) -> str:
    """格式化信号为报告段落。"""
    lines = []
    code_to_name = code_to_name or {}

    def name(code):
        return code_to_name.get(code, code)

    buys = signals.get("buy", [])
    sells = signals.get("sell", [])
    holds = signals.get("hold", [])
    watches = signals.get("watch", [])

    if not any([buys, sells, watches]):
        lines.append("  (无变动, 维持当前持仓)")
        return "\n".join(lines)

    if buys:
        lines.append("  🟢 买入 (新进TOP5):")
        for s in buys:
            lines.append(f"     {name(s['code'])} ({s['code']}) — {s['reason']}")
    if sells:
        lines.append("  🔴 卖出 (连续2天不在TOP5):")
        for s in sells:
            lines.append(f"     {name(s['code'])} ({s['code']}) — {s['reason']}")
    if watches:
        lines.append("  ⚠ 观察 (掉出1天, 明天再跌则卖):")
        for s in watches:
            lines.append(f"     {name(s['code'])} ({s['code']}) — {s['reason']}")
    if holds:
        lines.append(f"  ⏸ 持有 ({len(holds)}只, 连续在TOP5)")
    return "\n".join(lines)
