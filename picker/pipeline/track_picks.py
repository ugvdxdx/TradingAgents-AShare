#!/usr/bin/env python3
"""选股效果跟踪 —— 每交易日闭市后, 整理之前 30 个交易日各日选股 TOP20 到目前为止的涨跌幅。

用途:
  评估选股模型的实盘效果。每天闭市后, 回看最近 N(默认30) 个交易日, 对每一个有选股快照的
  交易日 (pick_date):
    - 从该日选股快照 (data/caches/v3_snapshots/YYYY-MM-DD.json) 全池按选股口径取 TOP20;
    - "选股口径" = 选股流水线 debate_picker 的量化排序锚 anchor_score
      = chain + capital×2 + surge×SURGE_WEIGHT (真相源: agents.picker.data_io.anchor_score);
    - 入场基准 = 选股次日 (pick_date 的下一个交易日) 的【开盘价】;
    - 计算到"当前收盘 (as_of)"为止的涨跌幅, 汇总平均/中位涨幅与胜率;
    - 每个选股日单独产出一个 Markdown 表格文件: data/caches/pick_tracking/<pick_date>.md
      (每次跑刷新其到最新 as_of 的收益)。

数据来源:
  - 打分/排名: v3_snapshots/YYYY-MM-DD.json 的 scores 字段 (全池 chain/surge/capital)。
  - 价格: kline_cache/{code}_{SH|SZ}.pkl (trade_date + open + close), 需先跑 K线增量更新。

用法:
  uv run python3 picker/pipeline/track_picks.py                 # 默认: as_of=最新交易日, 回看30日, TOP20
  uv run python3 picker/pipeline/track_picks.py --as-of 2026-07-03
  uv run python3 picker/pipeline/track_picks.py --lookback 20 --top 10
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from picker import paths

LOOKBACK_DAYS = 30   # 回看交易日数
TOP_N = 20           # 每日跟踪的打分 TOP 只数
REFERENCE_CODE = "000001"  # 交易日历参考股 (平安银行, K线缓存常驻)


def _kline_path(code: str) -> str:
    """K线落盘命名: 600xxx_SH.pkl / 000xxx_SZ.pkl (与 update_klines_daily 一致)。"""
    suffix = "_SH.pkl" if code.startswith("6") else "_SZ.pkl"
    return os.path.join(paths.KLINE_CACHE_DIR, f"{code}{suffix}")


# 模块级 K线缓存: {code: {trade_date: {"open": .., "close": ..}}}
_KLINE_BAR_CACHE: Dict[str, Optional[Dict[str, Dict[str, float]]]] = {}


def _load_bars(code: str) -> Optional[Dict[str, Dict[str, float]]]:
    """读某只股票的 {trade_date: {open, close}} 映射, 无/损坏返回 None。"""
    if code in _KLINE_BAR_CACHE:
        return _KLINE_BAR_CACHE[code]
    p = _kline_path(code)
    bars: Optional[Dict[str, Dict[str, float]]] = None
    if os.path.exists(p):
        try:
            df = pickle.load(open(p, "rb"))
            if (df is not None and len(df) and "trade_date" in df.columns
                    and "open" in df.columns and "close" in df.columns):
                bars = {str(td): {"open": float(o), "close": float(c)}
                        for td, o, c in zip(df["trade_date"], df["open"], df["close"])}
        except Exception:
            bars = None
    _KLINE_BAR_CACHE[code] = bars
    return bars


def _trading_days() -> List[str]:
    """从参考股 K线取升序交易日列表 (YYYY-MM-DD)。失败返回空。"""
    bars = _load_bars(REFERENCE_CODE)
    if not bars:
        return []
    return sorted(bars.keys())


def _list_snapshot_dates() -> List[str]:
    """已有选股快照的日期列表 (升序)。"""
    if not os.path.isdir(paths.V3_SNAPSHOT_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(paths.V3_SNAPSHOT_DIR) if f.endswith(".json"))


def _load_snapshot(date: str) -> Optional[dict]:
    path = os.path.join(paths.V3_SNAPSHOT_DIR, f"{date}.json")
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


# 模块级 code→name 映射 (先用快照 ranking, 再回落 fundamentals JSON)
_NAME_CACHE: Dict[str, str] = {}


def _lookup_name(code: str) -> str:
    """取股票名: 优先已知缓存, 否则读 fundamentals/{code}.json 的 name。"""
    if code in _NAME_CACHE:
        return _NAME_CACHE[code]
    name = ""
    for fdir in (paths.FUNDAMENTALS_DIR, paths.COLD_FUNDAMENTALS_DIR):
        fp = os.path.join(fdir, f"{code}.json")
        if os.path.exists(fp):
            try:
                name = json.load(open(fp, encoding="utf-8")).get("name", "") or ""
            except Exception:
                name = ""
            if name:
                break
    _NAME_CACHE[code] = name
    return name


def _top_scored(snapshot: dict, top_n: int) -> List[dict]:
    """从快照全池 scores 取选股口径 TOP N (量化排序锚 anchor_score 降序)。

    选股口径 = 选股流水线 debate_picker 的量化排序锚:
      anchor_score = chain + capital×2 + surge×SURGE_WEIGHT
    与 picker 实盘选股排名的唯一真相源 (tradingagents.agents.picker.data_io.anchor_score)
    保持一致, 不用 v3 的 chain+surge+capital 简单求和。

    返回 [{rank, code, name, chain, surge, capital, score}, ...]。
    name 优先取快照 ranking 里的名字, 否则回落 fundamentals。
    """
    from tradingagents.agents.picker.data_io import anchor_score

    scores = snapshot.get("scores", {}) or {}
    # 预置 ranking 里已有的名字 (省去读 JSON)
    for r in snapshot.get("ranking", []) or []:
        c, nm = r.get("code", ""), r.get("name", "")
        if c and nm and c not in _NAME_CACHE:
            _NAME_CACHE[c] = nm

    rows = []
    for code, s in scores.items():
        if not isinstance(s, dict):
            continue
        try:
            chain = float(s.get("chain", 0))
            surge = float(s.get("surge", 0))
            capital = float(s.get("capital", 0))
        except (TypeError, ValueError):
            continue
        rows.append({
            "code": code,
            "chain": round(chain, 1),
            "surge": round(surge, 1),
            "capital": round(capital, 1),
            "score": round(anchor_score(s), 1),  # 选股口径量化锚
        })
    rows.sort(key=lambda x: x["score"], reverse=True)
    top = rows[:top_n]
    for i, r in enumerate(top, 1):
        r["rank"] = i
        r["name"] = _lookup_name(r["code"])
    return top


def _return_from_open(code: str, entry_date: str, as_of_date: str) -> Optional[dict]:
    """计算 code 从 entry_date 的【开盘价】到 as_of_date 收盘价的涨跌幅。

    entry_date = 选股次日 (pick_date 的下一个交易日); 入场用其开盘价。

    Returns:
        {entry_open, current_close, return_pct} 或 None (缺价/入场价<=0)。
    """
    bars = _load_bars(code)
    if not bars:
        return None
    entry_bar = bars.get(entry_date)
    cur_bar = bars.get(as_of_date)
    if not entry_bar or not cur_bar:
        return None
    entry = entry_bar.get("open")
    current = cur_bar.get("close")
    if entry is None or current is None or entry <= 0:
        return None
    return {
        "entry_open": round(entry, 2),
        "current_close": round(current, 2),
        "return_pct": round((current - entry) / entry * 100, 2),
    }


def build_pick_day(pick_date: str, as_of: str, trading_days: List[str],
                   top_n: int = TOP_N) -> Optional[dict]:
    """构建单个选股日 (pick_date) 的 TOP20 跟踪结果 (收益到 as_of 收盘)。

    入场基准 = pick_date 的下一个交易日开盘价; 若无下一交易日 (pick_date == as_of 当日,
    次日尚未产生) 则入场价不可得, 各票 return_pct=None (held=0)。

    Returns:
        跟踪 dict, 无快照/无 TOP 时返回 None。
    """
    snap = _load_snapshot(pick_date)
    if not snap:
        return None
    top = _top_scored(snap, top_n)
    if not top:
        return None

    # 入场日 = pick_date 的下一个交易日 (次日开盘入场)
    later = [d for d in trading_days if d > pick_date and d <= as_of]
    entry_date = later[0] if later else None

    picks = []
    returns = []
    for r in top:
        entry = {
            "rank": r["rank"], "code": r["code"], "name": r["name"],
            "score": r["score"],
            "chain": r["chain"], "surge": r["surge"], "capital": r["capital"],
        }
        ret = _return_from_open(r["code"], entry_date, as_of) if entry_date else None
        if ret is None:
            entry["return_pct"] = None  # 无次日/缺价 (停牌/新股/无K线)
        else:
            entry.update(ret)
            returns.append(ret["return_pct"])
        picks.append(entry)

    # 持有交易日数 = (entry_date, as_of] 的交易日数 + 1 (含入场日当日)
    held = len([d for d in trading_days if entry_date and entry_date <= d <= as_of])

    return {
        "pick_date": pick_date,
        "entry_date": entry_date,        # 次日开盘入场日
        "as_of_date": as_of,
        "entry_basis": "next_day_open",  # 入场基准: 选股次日开盘价
        "trading_days_held": held,
        "top_n": len(top),
        "covered": len(returns),         # 有价格可算涨幅的只数
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "median_return_pct": round(median(returns), 2) if returns else None,
        "win_rate": round(sum(1 for x in returns if x > 0) / len(returns), 3) if returns else None,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "picks": picks,
    }


def _fmt_pct(x: Optional[float]) -> str:
    """涨跌幅格式化: None→'—', 否则带符号百分比 (如 +3.21% / -5.40%)。"""
    if x is None:
        return "—"
    return f"{x:+.2f}%"


def render_markdown(day: dict) -> str:
    """把单个选股日跟踪 dict 渲染成 Markdown 表格文本。"""
    wr = day["win_rate"]
    wr_str = f"{wr:.0%}" if wr is not None else "—"
    lines = [
        f"# 选股效果跟踪 · {day['pick_date']}",
        "",
        f"- 选股日: **{day['pick_date']}**　入场日(次日开盘): **{day['entry_date'] or '—'}**"
        f"　截至: **{day['as_of_date']}**",
        f"- 入场基准: 次日开盘价　持有: **{day['trading_days_held']}** 个交易日"
        f"　跟踪只数: TOP{day['top_n']} (有效 {day['covered']})",
        f"- 平均涨跌: **{_fmt_pct(day['avg_return_pct'])}**"
        f"　中位涨跌: **{_fmt_pct(day['median_return_pct'])}**"
        f"　胜率: **{wr_str}**",
        f"- 生成时间: {day['generated_at']}",
        "",
        "| 排名 | 代码 | 名称 | 选股锚分 | chain | surge | capital | 入场开盘 | 现价(收盘) | 涨跌幅 |",
        "| ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for p in day["picks"]:
        lines.append(
            f"| {p['rank']} | {p['code']} | {p.get('name', '') or '—'} | "
            f"{p['score']} | {p['chain']} | {p['surge']} | {p['capital']} | "
            f"{p.get('entry_open', '—')} | {p.get('current_close', '—')} | "
            f"{_fmt_pct(p.get('return_pct'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_tracking(as_of: Optional[str] = None,
                 lookback: int = LOOKBACK_DAYS,
                 top_n: int = TOP_N) -> List[dict]:
    """跟踪最近 lookback 个交易日各选股日 TOP20 到 as_of 的收益, 每日各存一个 Markdown 表格文件。

    Returns:
        已生成的每日跟踪 dict 列表 (按 pick_date 升序); 无交易日/快照时返回 []。
    """
    trading_days = _trading_days()
    if not trading_days:
        print(f"  ✗ 无法从参考股 {REFERENCE_CODE} K线获取交易日历 (先跑 K线更新)")
        return []

    if as_of:
        if as_of not in trading_days:
            # as_of 非交易日或无 K线: 取 <= as_of 的最近交易日
            earlier = [d for d in trading_days if d <= as_of]
            if not earlier:
                print(f"  ✗ as_of={as_of} 早于所有已知交易日")
                return []
            as_of = earlier[-1]
    else:
        as_of = trading_days[-1]

    # 回看窗口: <= as_of 的最近 lookback 个交易日
    window = [d for d in trading_days if d <= as_of][-lookback:]
    snapshot_dates = set(_list_snapshot_dates())

    os.makedirs(paths.PICK_TRACKING_DIR, exist_ok=True)
    results = []
    for pick_date in window:
        if pick_date not in snapshot_dates:
            continue  # 该交易日无选股快照 (系统上线前/未运行)
        day = build_pick_day(pick_date, as_of, trading_days, top_n)
        if day is None:
            continue
        path = os.path.join(paths.PICK_TRACKING_DIR, f"{pick_date}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_markdown(day))
        results.append(day)
    return results


def main():
    parser = argparse.ArgumentParser(description="选股效果跟踪 (近30交易日选股TOP20涨跌幅, 每选股日一个文件)")
    parser.add_argument("--as-of", dest="as_of", default="",
                        help="当前收盘日 YYYY-MM-DD (默认最新交易日)")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                        help=f"回看交易日数 (默认 {LOOKBACK_DAYS})")
    parser.add_argument("--top", type=int, default=TOP_N,
                        help=f"每日跟踪的选股 TOP 只数 (默认 {TOP_N})")
    args = parser.parse_args()

    print("═" * 60)
    print("  选股效果跟踪 (近 %d 交易日选股 TOP%d 涨跌幅, 次日开盘入场)" % (args.lookback, args.top))
    print("═" * 60)

    results = run_tracking(as_of=args.as_of or None, lookback=args.lookback, top_n=args.top)
    if not results:
        print("  ✗ 生成失败 (无交易日历或快照)")
        sys.exit(1)

    as_of = results[-1]["as_of_date"]
    print(f"  as_of={as_of} | 窗口 {results[0]['pick_date']}~{results[-1]['pick_date']} "
          f"| 生成选股日文件: {len(results)}")
    for d in results:
        avg = d["avg_return_pct"]
        wr = d["win_rate"]
        print(f"    {d['pick_date']}→入场{d['entry_date'] or 'NA'} (持有{d['trading_days_held']:>2}日) "
              f"TOP{d['top_n']} 覆盖{d['covered']:>2} "
              f"均值 {avg if avg is not None else 'NA':>6}% "
              f"胜率 {f'{wr:.0%}' if wr is not None else 'NA'}")
    print(f"  ✓ 已保存至: {paths.PICK_TRACKING_DIR}")


if __name__ == "__main__":
    main()
