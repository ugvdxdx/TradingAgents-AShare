"""debate_picker v5 — 行业轮动感知层 (快层)。

解决系统过度依赖 V3 行业动量、热门行业切换时反应滞后的问题。

三个能力:
  1. 板块资金流排名 (结构化): 每日主力净流入 TOP/BOTTOM 板块, 注入辩论上下文,
     让 agent 感知"当前资金正在往哪个方向集中/撤离"。
  2. 轮动信号检测: 对比候选池所属板块 vs 全市场热门板块, 输出
     "未被候选池覆盖的资金净流入板块" → 提示主线可能切换。
  3. 待重评名单: 热门但未进 Top50 的板块龙头 → 输出给慢层触发 V3 重评。

实盘模式才采集 (依赖实时接口); 回测模式跳过 (无未来函数)。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from picker import paths

# 板块资金流离线缓存 (实时取不到时回退)
_CACHE_PATH = paths.BOARD_FLOW_CACHE


# ══════════════════════════════════════════════════════════
# 1. 板块资金流 (多源回退: akshare → Tushare个股汇总 → 离线缓存 → 候选池推算)
# ══════════════════════════════════════════════════════════

def _fetch_board_df(retries: int = 3):
    """直连 akshare 拉取行业板块资金流 DataFrame (带重试)。失败返回 None。

    用 stock_sector_fund_flow_rank (akshare 1.18+); 列含:
      名称 / 今日涨跌幅 / 今日主力净流入-净额 ...
    """
    import time
    try:
        import akshare as ak
    except ImportError:
        return None
    for attempt in range(retries):
        try:
            df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
            if df is not None and not df.empty:
                return df
        except Exception:
            time.sleep(1.5)
    return None


def _fetch_board_from_tushare() -> List[Dict[str, Any]]:
    """用 Tushare 全市场个股资金流 + 行业分组汇总出板块资金流。

    回退策略: akshare 取不到时调用, 不需要 5000 积分 (moneyflow 接口 1000 积分档)。
    返回 [{name, change_pct, main_net_yi}]; 失败返回 []。
    """
    import os
    from dotenv import load_dotenv
    load_dotenv(os.path.join(paths.PROJECT_ROOT, ".env"), override=True)
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        return []
    try:
        import tushare as ts
        pro = ts.pro_api(token)
        from datetime import date, timedelta
        # 回退试最近7天 (覆盖长周末/节假日)
        for delta in range(7):
            trade_date = (date.today() - timedelta(days=delta)).strftime("%Y%m%d")
            df = pro.moneyflow(trade_date=trade_date)
            if df is not None and not df.empty:
                break
        else:
            return []
    except Exception:
        return []

    # 行业映射: 个股 → 行业 (从 fundamentals JSON 读取)
    fdir = paths.FUNDAMENTALS_DIR
    code_ind: Dict[str, str] = {}
    try:
        for fn in os.listdir(fdir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(fdir, fn)) as f:
                    d = json.load(f)
                code = fn.replace(".json", "")
                ind = (d.get("business_overview", {}) or {}).get("industry", "")
                if ind:
                    code_ind[code] = ind
            except Exception:
                continue
    except Exception:
        pass

    # 按行业分组汇总主力净流入
    sector_net: Dict[str, float] = {}
    for _, r in df.iterrows():
        ts_code = str(r.get("ts_code", ""))
        code = ts_code.split(".")[0]
        ind = code_ind.get(code)
        if not ind:
            continue
        net = float(r.get("net_mf_amount", 0) or 0)  # 万元
        sector_net[ind] = sector_net.get(ind, 0) + net

    rows = []
    for name, net_wan in sorted(sector_net.items(), key=lambda x: -x[1]):
        rows.append({
            "name": name,
            "change_pct": 0.0,  # Tushare 个股资金流无行业涨跌幅
            "main_net_yi": round(net_wan / 10000, 2),  # 万元 → 亿元
        })
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _infer_from_candidates() -> List[Dict[str, Any]]:
    """从 .mf_cache/ 个股资金流 + fundamentals 行业分组汇总出板块资金流。

    纯离线, 不依赖任何网络接口, 作为最后一级回退。
    """
    import pickle
    import glob as _glob
    mf_dir = paths.MF_CACHE_DIR
    if not os.path.exists(mf_dir):
        return []
    # 找最新 mf_*.pkl
    files = sorted(_glob.glob(os.path.join(mf_dir, "mf_*.pkl")), reverse=True)
    if not files:
        return []
    try:
        with open(files[0], "rb") as f:
            raw = pickle.load(f)
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []

    # 行业映射
    fdir = paths.FUNDAMENTALS_DIR
    code_ind: Dict[str, str] = {}
    for fn in os.listdir(fdir):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(fdir, fn)) as f:
                d = json.load(f)
            code = fn.replace(".json", "")
            ind = (d.get("business_overview", {}) or {}).get("industry", "")
            if ind:
                code_ind[code] = ind
        except Exception:
            continue

    # 汇总: 取每只股最近1日主力净流入
    sector_net: Dict[str, float] = {}
    for k, v in raw.items():
        if not isinstance(v, list) or not v:
            continue
        code = k.rsplit("_", 1)[0]
        ind = code_ind.get(code)
        if not ind:
            continue
        last = v[-1]
        net = float(last.get("main_net", 0) or 0) / 1e8  # 元 → 亿元
        sector_net[ind] = sector_net.get(ind, 0) + net

    rows = []
    for name, net_yi in sorted(sector_net.items(), key=lambda x: -x[1]):
        rows.append({"name": name, "change_pct": 0.0, "main_net_yi": round(net_yi, 2)})
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _save_cache(rows: List[Dict[str, Any]]) -> None:
    """实时获取成功时, 落盘结构化板块资金流 (带日期), 供离线回退。"""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d"), "rows": rows},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _load_cache() -> Tuple[str, List[Dict[str, Any]]]:
    """读最近一次离线缓存。返回 (日期, rows); 无缓存返回 ("", [])。"""
    try:
        with open(_CACHE_PATH) as f:
            d = json.load(f)
        return d.get("date", ""), d.get("rows", []) or []
    except Exception:
        return "", []


def get_board_flow_ranking(top_n: int = 10) -> Tuple[str, List[Dict[str, Any]]]:
    """返回 (说明文本, 结构化行)。结构化行按主力净额降序。

    实时获取成功 → 落盘缓存并返回; 失败 → 回退最近一次离线缓存; 都没有返回 ("", [])。
    main_net_yi 单位: 亿元。
    """
    df = _fetch_board_df()
    if df is None:
        # 第二级回退: Tushare 全市场个股资金流 → 行业汇总
        ts_rows = _fetch_board_from_tushare()
        if ts_rows:
            _save_cache(ts_rows)
            return f"行业板块资金流 (Tushare个股汇总)", ts_rows
        # 第三级回退: 离线缓存
        date, rows = _load_cache()
        if rows:
            return f"行业板块资金流 (离线缓存 {date})", rows
        # 第四级回退: 候选池推算
        rows = _infer_from_candidates()
        if rows:
            _save_cache(rows)
            return f"行业板块资金流 (由候选池个股资金流推算)", rows
        return "(板块资金流获取失败, 无离线缓存)", []

    # 兼容列名 (不同 akshare 版本列名一致, 但做防御)
    name_col = next((c for c in df.columns if c == "名称"), None)
    chg_col = next((c for c in df.columns if "涨跌幅" in c), None)
    net_col = next((c for c in df.columns if "主力净流入-净额" in c), None)
    if not (name_col and net_col):
        ts_rows = _fetch_board_from_tushare()
        if ts_rows:
            _save_cache(ts_rows)
            return f"行业板块资金流 (Tushare个股汇总, 实时列名异常)", ts_rows
        date, rows = _load_cache()
        if rows:
            return f"行业板块资金流 (离线缓存 {date}, 实时列名异常)", rows
        rows = _infer_from_candidates()
        if rows:
            _save_cache(rows)
            return f"行业板块资金流 (由候选池个股资金流推算, 实时列名异常)", rows
        return "(板块资金流列名异常)", []

    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        try:
            net = float(r[net_col])
        except (TypeError, ValueError):
            continue
        chg = 0.0
        if chg_col is not None:
            try:
                chg = float(r[chg_col])
            except (TypeError, ValueError):
                chg = 0.0
        rows.append({
            "name": str(r[name_col]).strip(),
            "change_pct": chg,
            "main_net_yi": round(net / 1e8, 2),  # 净额单位元 → 亿元
        })
    rows.sort(key=lambda x: -x["main_net_yi"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    _save_cache(rows)
    return f"行业板块资金流 (共{len(rows)}个)", rows


# ══════════════════════════════════════════════════════════
# 2. 候选池板块归属 (个股 → 行业)
# ══════════════════════════════════════════════════════════

def _load_candidate_industries(candidates: List[Dict[str, Any]]) -> Dict[str, str]:
    """从 fundamentals JSON 读取候选股所属行业 (business_overview.industry)。

    返回 {code: industry}; 读不到的跳过。
    """
    import json
    import os

    fdir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))),
        "fundamentals",
    )
    out: Dict[str, str] = {}
    for c in candidates:
        code = c.get("code", "")
        try:
            with open(os.path.join(fdir, f"{code}.json")) as f:
                d = json.load(f)
            ind = (d.get("business_overview", {}) or {}).get("industry", "")
            if ind:
                out[code] = ind
        except Exception:
            continue
    return out


def _industry_keywords(industry: str) -> List[str]:
    """把行业名拆成关键词, 用于和板块名模糊匹配 (如 '半导体设备' → ['半导体','设备'])。"""
    # 去掉括号内容
    base = re.sub(r"[（(].*?[)）]", "", industry or "")
    # 常见拆分: 直接按 2-4 字子串匹配, 这里取整体 + 去后缀
    kws = [base]
    for suf in ("设备", "材料", "制造", "服务", "Ⅱ", "Ⅲ", "III", "II"):
        if base.endswith(suf) and len(base) > len(suf):
            kws.append(base[: -len(suf)])
    return [k for k in kws if k]


# ══════════════════════════════════════════════════════════
# 3. 轮动信号检测
# ══════════════════════════════════════════════════════════

def detect_rotation(
    candidates: List[Dict[str, Any]],
    board_rows: List[Dict[str, Any]],
    top_k: int = 10,
) -> Dict[str, Any]:
    """检测主线轮动信号。

    对比 "资金净流入 TOP-K 板块" 与 "候选池覆盖的行业":
      - covered: 候选池已覆盖的热门板块
      - uncovered: 资金净流入但候选池没覆盖的板块 (主线切换预警)

    返回 dict, 含 covered/uncovered/summary。
    """
    if not board_rows:
        return {"covered": [], "uncovered": [], "summary": ""}

    inds = _load_candidate_industries(candidates)
    # 候选池所有行业关键词集合
    cand_kws: List[str] = []
    for ind in set(inds.values()):
        cand_kws.extend(_industry_keywords(ind))

    hot = [r for r in board_rows[:top_k] if r["main_net_yi"] > 0]
    covered, uncovered = [], []
    for r in hot:
        bname = r["name"]
        # 板块名是否与候选池任一行业关键词互相包含
        matched = any(kw and (kw in bname or bname in kw) for kw in cand_kws)
        (covered if matched else uncovered).append(r)

    parts = []
    if covered:
        parts.append("候选池已覆盖热门板块: " + ", ".join(
            f"{r['name']}(主力+{r['main_net_yi']:.1f}亿)" for r in covered[:5]))
    if uncovered:
        parts.append("⚠️ 资金净流入但候选池未覆盖(主线切换预警): " + ", ".join(
            f"{r['name']}(主力+{r['main_net_yi']:.1f}亿,涨{r['change_pct']:.1f}%)"
            for r in uncovered[:5]))
    return {
        "covered": covered,
        "uncovered": uncovered,
        "summary": "\n".join(parts),
    }


# ══════════════════════════════════════════════════════════
# 4. 上下文文本 (注入辩论/分析师)
# ══════════════════════════════════════════════════════════

def build_rotation_context(
    board_rows: List[Dict[str, Any]],
    rotation: Dict[str, Any],
    top_n: int = 10,
) -> str:
    """组装注入 agent 的板块轮动上下文文本。"""
    if not board_rows:
        return ""
    lines = ["【今日板块资金流向 (主力净流入TOP/流出BOTTOM)】"]
    top = board_rows[:top_n]
    for r in top:
        lines.append(f"  +{r['rank']:>2}. {r['name']} 主力净额{r['main_net_yi']:+.1f}亿 "
                     f"涨跌{r['change_pct']:+.1f}%")
    bottom = [r for r in board_rows if r["main_net_yi"] < 0]
    if bottom:
        bottom = sorted(bottom, key=lambda x: x["main_net_yi"])[:5]
        lines.append("  ── 资金流出板块 ──")
        for r in bottom:
            lines.append(f"  {r['name']} 主力净额{r['main_net_yi']:+.1f}亿 "
                         f"涨跌{r['change_pct']:+.1f}%")
    if rotation.get("summary"):
        lines.append("\n【主线轮动信号】\n" + rotation["summary"])

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 5. 行业成分股 (触发式重评用)
# ══════════════════════════════════════════════════════════

def get_industry_constituents(industry_name: str, top_n: int = 10) -> List[Dict[str, Any]]:
    """获取某行业板块的成分股 (按涨跌幅排序), 用于轮动触发式 V3 重评。

    返回 [{code, name, change_pct}], 失败返回 []。
    """
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=industry_name)
        if df is None or df.empty:
            return []
        rows = []
        for _, r in df.head(top_n).iterrows():
            code = str(r.get("代码", "")).strip()
            if code:
                rows.append({
                    "code": code,
                    "name": str(r.get("名称", "")).strip(),
                    "change_pct": float(r.get("涨跌幅", 0) or 0),
                })
        return rows
    except Exception:
        return []
