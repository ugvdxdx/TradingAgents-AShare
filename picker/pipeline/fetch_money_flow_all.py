#!/usr/bin/env python3
"""
增量拉取资金流数据到磁盘缓存 (单一持久文件)。

缓存策略:
  - 单一文件 .mf_cache/mf.pkl, 不再按日期分文件。
  - 增量更新: 只拉每只股缺失的交易日, 已有日期不重拉。
  - 分层深度:
      * 热股 (有 fundamentals/{code}.json) → 保留 14 个月 (~300 交易日)
      * 其余白名单股 → 保留 60 天
  - 行业资金流历史: 个股 mf.pkl 更新后, 按热股 fundamentals 行业映射逐日汇总,
    存到 .mf_cache/board_flow_history.pkl (纯本地计算, 不联网)。

数据源: Tushare moneyflow (付费稳定, 限速 200/min)。
首次运行会自动迁移旧 mf_YYYY-MM-DD.pkl → 单一 mf.pkl。

用法:
    uv run python3 fetch_money_flow_all.py              # 增量更新(默认)
    uv run python3 fetch_money_flow_all.py --purge-old  # 增量并删除旧日期文件
    uv run python3 fetch_money_flow_all.py --no-board   # 跳过实时板块资金流刷新
"""
import json
import sys
import argparse
import os
import glob
import pickle
import time

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
from picker import paths

load_dotenv(os.path.join(paths.PROJECT_ROOT, '.env'))

from picker.data import money_flow

# ══════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════

# 缓存分层深度 (交易日)。14 个月 ≈ 292 交易日, 取 290 为稳定上限
# (HOT_LOOKBACK_DAYS=440 自然日可覆盖 ~292 交易日, 留余量避免无限重拉)。
HOT_DAYS = 290      # 热股: 14 个月
NORMAL_DAYS = 60    # 其余: 60 天

# 首次全量拉取的起始日期回溯 (自然日, 留余量覆盖非交易日)
HOT_LOOKBACK_DAYS = 440     # 热股往前推 ~14 个自然月
NORMAL_LOOKBACK_DAYS = 90   # 其余往前推 90 自然日

SINGLE_FILE = os.path.join(paths.MF_CACHE_DIR, "mf.pkl")

# Tushare moneyflow 接口限速 200次/分钟 → 0.33s/请求 (留余量)
RATE_INTERVAL = 0.35
# 触发限速时的退避等待 (秒)
RATE_BACKOFF = 3.0
RATE_MAX_RETRY = 3


# ══════════════════════════════════════════════════════════
# 热股判定
# ══════════════════════════════════════════════════════════

def _is_hot(code: str) -> bool:
    """有 fundamentals/{code}.json 即为热股。"""
    return os.path.exists(os.path.join(paths.FUNDAMENTALS_DIR, f"{code}.json"))


def _scan_fundamentals() -> list:
    """扫描 fundamentals/ 目录, 返回全部热股 code (热股池唯一真相源)。

    以 fundamentals/ 为准而非 白名单∩fundamentals: 白名单外的新股 (次新/小代码
    未进 stock_whitelist) 也要覆盖, 否则资金流会漏掉它们。
    """
    fdir = paths.FUNDAMENTALS_DIR
    if not os.path.isdir(fdir):
        return []
    return [f[:-5] for f in os.listdir(fdir) if f.endswith(".json")]


# ══════════════════════════════════════════════════════════
# 单文件读写
# ══════════════════════════════════════════════════════════

def _load_single() -> dict:
    """读取单一缓存文件 → {code: [daily_rows]}。不存在返回 {}。"""
    if not os.path.exists(SINGLE_FILE):
        return {}
    try:
        with open(SINGLE_FILE, "rb") as f:
            raw = pickle.load(f)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(v, list)}
    except Exception:
        pass
    return {}


def _save_single(data: dict) -> str:
    """写入单一缓存文件 (纯 code key), 返回路径。"""
    os.makedirs(paths.MF_CACHE_DIR, exist_ok=True)
    with open(SINGLE_FILE, "wb") as f:
        pickle.dump(data, f)
    return SINGLE_FILE


# ══════════════════════════════════════════════════════════
# 一次性迁移: 旧 mf_YYYY-MM-DD.pkl → mf.pkl
# ══════════════════════════════════════════════════════════

def _migrate_from_old_files(existing: dict) -> tuple:
    """从旧 mf_YYYY-MM-DD.pkl 合并数据到 existing (纯 code key, 取最长列表)。

    返回 (merged_dict, migrated_count)。
    """
    cache_dir = paths.MF_CACHE_DIR
    if not os.path.exists(cache_dir):
        return existing, 0
    old_files = sorted(
        glob.glob(os.path.join(cache_dir, "mf_*.pkl")),
        # 排除 mf.pkl 本身
    )
    old_files = [f for f in old_files if os.path.basename(f) != "mf.pkl"]
    if not old_files:
        return existing, 0

    merged = dict(existing)
    n_before = len(merged)
    for fp in old_files:
        try:
            with open(fp, "rb") as f:
                raw = pickle.load(f)
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        for k, v in raw.items():
            if not isinstance(v, list) or len(v) == 0:
                continue
            # 旧 key 形如 '{code}_{days}', 剥后缀
            code = k.rsplit("_", 1)[0] if "_" in k else k
            if code not in merged or len(v) > len(merged[code]):
                merged[code] = v
    migrated = len(merged) - n_before
    if merged != existing:
        print(f"📦 迁移: 从 {len(old_files)} 个旧文件合并数据 (新增 {migrated} 只 / 更新若干)")
    return merged, migrated


# ══════════════════════════════════════════════════════════
# 增量拉取
# ══════════════════════════════════════════════════════════

def _fetch_tushare(pro, ts_code: str, start: str, end: str) -> list:
    """从 Tushare 拉取资金流, 返回归一化后的 daily_rows 列表。

    触发限速 (200/min) 时退避重试。
    """
    df = None
    for attempt in range(RATE_MAX_RETRY):
        try:
            df = pro.moneyflow(ts_code=ts_code, start_date=start, end_date=end)
            break
        except Exception as e:
            msg = str(e)
            if "频率超限" in msg or "频次" in msg:
                time.sleep(RATE_BACKOFF)
                continue
            raise
    if df is None or df.empty:
        return []
    new_data = []
    for _, row in df.iterrows():
        super_large_wan = (row.get("buy_elg_amount", 0) or 0) - (row.get("sell_elg_amount", 0) or 0)
        large_wan = (row.get("buy_lg_amount", 0) or 0) - (row.get("sell_lg_amount", 0) or 0)
        medium_wan = (row.get("buy_md_amount", 0) or 0) - (row.get("sell_md_amount", 0) or 0)
        small_wan = (row.get("buy_sm_amount", 0) or 0) - (row.get("sell_sm_amount", 0) or 0)
        main_force_wan = super_large_wan + large_wan
        buy_total_wan = (row.get("buy_elg_amount", 0) or 0) + (row.get("buy_lg_amount", 0) or 0) + (row.get("buy_md_amount", 0) or 0) + (row.get("buy_sm_amount", 0) or 0)
        sell_total_wan = (row.get("sell_elg_amount", 0) or 0) + (row.get("sell_lg_amount", 0) or 0) + (row.get("sell_md_amount", 0) or 0) + (row.get("sell_sm_amount", 0) or 0)
        turnover_wan = buy_total_wan + sell_total_wan
        main_pct = (main_force_wan / turnover_wan * 100) if turnover_wan > 0 else 0.0
        new_data.append({
            "date": str(row["trade_date"]),
            "main_net": float(main_force_wan * 1e4),
            "super_large": float(super_large_wan * 1e4),
            "large": float(large_wan * 1e4),
            "medium": float(medium_wan * 1e4),
            "small": float(small_wan * 1e4),
            "main_pct": float(main_pct),
        })
    new_data.sort(key=lambda x: x["date"])
    return new_data


def _merge_dedup(old: list, new_rows: list, target_days: int) -> list:
    """合并 old+new 并按日期去重, 只保留尾部 target_days 条。"""
    merged = (old or []) + new_rows
    seen = set()
    deduped = []
    for r in reversed(merged):
        dd = r["date"].replace("-", "")
        if dd not in seen:
            seen.add(dd)
            deduped.append(r)
    deduped.reverse()
    if len(deduped) > target_days:
        deduped = deduped[-target_days:]
    return deduped


def incr_update(codes: list, existing: dict) -> dict:
    """增量更新: 只拉每只股缺失的交易日。"""
    from datetime import date, timedelta
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        print("✗ TUSHARE_TOKEN 未配置, 无法拉取。")
        return existing

    pro = ts.pro_api(token)

    # 确定最近交易日: 探测 000001 的最新数据日 (一次 API 调用, 精确含节假日)。
    # 周末/节假日时 today 无交易, 直接用 today 会触发大量空请求。
    today = date.today()
    while today.weekday() >= 5:  # 先回退到工作日
        today = today - timedelta(days=1)
    today_str = today.strftime("%Y%m%d")
    try:
        ref = _fetch_tushare(pro, money_flow._tushare_ts_code("000001"),
                             (today - timedelta(days=10)).strftime("%Y%m%d"), today_str)
        if ref:
            today_str = max(today_str, ref[-1]["date"].replace("-", ""))
            # 若参考股最新日 < 工作日, 说明该工作日是节假日, 取实际交易日
            today_str = ref[-1]["date"].replace("-", "")
    except Exception:
        pass
    print(f"最近交易日: {today_str}")

    updated = 0       # 拉取了新数据
    uptodate = 0      # 已是最新, 跳过
    failed = 0
    n_hot = sum(1 for c in codes if _is_hot(c))
    print(f"待更新 {len(codes)} 只 (热股 {n_hot} → {HOT_DAYS}日 / 其余 {len(codes)-n_hot} → {NORMAL_DAYS}日)")

    for i, code in enumerate(codes):
        target_days = HOT_DAYS if _is_hot(code) else NORMAL_DAYS
        lookback = HOT_LOOKBACK_DAYS if _is_hot(code) else NORMAL_LOOKBACK_DAYS
        target_start = (date.today() - timedelta(days=lookback)).strftime("%Y%m%d")
        old = existing.get(code)
        latest_cached = None
        if old and isinstance(old, list) and len(old) > 0:
            latest_cached = str(old[-1].get("date", "")).replace("-", "")

        # 已是最新交易日 + 深度达标 → 跳过 (无需联网)。
        # today_str 已回退到最近工作日; 节假日时 latest < today_str 属正常,
        # 会发一次空请求 (无害, ~0.4s), 但能保证下个交易日补上新数据。
        if latest_cached and latest_cached >= today_str and len(old or []) >= target_days:
            uptodate += 1
            continue

        # 决定起始日期: 取目标回溯起点 与 缓存最新日+1 的较小值。
        # - 深度不足(< target_days) → 从 target_start 起, 补齐历史
        # - 深度达标但缺近期 → 从 latest_cached+1 起, 只补近期
        if latest_cached and len(old or []) >= target_days:
            start = str(int(latest_cached) + 1)
        else:
            start = target_start

        try:
            ts_code = money_flow._tushare_ts_code(code)
            new_rows = _fetch_tushare(pro, ts_code, start, today_str)
            if not new_rows:
                uptodate += 1
            else:
                existing[code] = _merge_dedup(old, new_rows, target_days)
                updated += 1
        except Exception as e:
            failed += 1
            if failed <= 10:
                print(f"  ✗ {code}: {e}", flush=True)

        # Tushare moneyflow 限速 200/min → 0.35s/请求
        time.sleep(RATE_INTERVAL)

        if (i + 1) % 200 == 0:
            pct = (i + 1) / len(codes) * 100
            print(f"  [{pct:5.1f}%] {i+1}/{len(codes)} 新增={updated} 已最新={uptodate} 失败={failed}", flush=True)
            # 增量保存检查点, 防止中途被杀丢数据
            _save_single(existing)

    print(f"增量完成: 新增={updated} 已最新={uptodate} 失败={failed}", flush=True)
    return existing


# ══════════════════════════════════════════════════════════
# 行业资金流历史 (由 mf.pkl 个股资金流按 fundamentals 行业汇总)
# ══════════════════════════════════════════════════════════

def _load_industry_map() -> dict:
    """从 fundamentals/*.json 读 {code: industry} (business_overview.industry)。"""
    fdir = paths.FUNDAMENTALS_DIR
    code_ind: dict = {}
    if not os.path.isdir(fdir):
        return code_ind
    for fn in os.listdir(fdir):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(fdir, fn)) as f:
                d = json.load(f)
            ind = (d.get("business_overview", {}) or {}).get("industry", "")
            if ind:
                code_ind[fn.replace(".json", "")] = ind
        except Exception:
            continue
    return code_ind


def _aggregate_board_flow_history(stock_mf: dict, code_ind: dict) -> dict:
    """把个股资金流按行业汇总成逐日行业资金流时间序列。

    ⚠️ 口径: 只用同时有 fundamentals 行业映射的股票 (=热股, ~533只)。
    mf.pkl 里非热股虽有资金流数据, 但不参与行业汇总, 保持历史与近期口径一致。

    输入:
      stock_mf: {code: [daily_rows]}  (mf.pkl 全量; 仅 code_ind 中的 code 被采用)
      code_ind: {code: industry}      (热股的行业映射)
    输出:
      {date: [board_rows]}  按 date 升序; 每个 board_row = {industry, main_net_yi}
      (未排序、未编号 rank, 消费端按需排)
    """
    # date -> industry -> 净额累加(元)。仅遍历 code_ind 里的热股, 显式排除非热股。
    date_ind_net: dict = {}
    for code, ind in code_ind.items():
        rows = stock_mf.get(code)
        if not ind or not isinstance(rows, list):
            continue
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            day = date_ind_net.setdefault(d, {})
            day[ind] = day.get(ind, 0.0) + float(r.get("main_net", 0) or 0)

    # 转成 {date: [{industry, main_net_yi}]}
    history: dict = {}
    for d in sorted(date_ind_net):
        rows = [
            {"industry": ind, "main_net_yi": round(net / 1e8, 2)}
            for ind, net in date_ind_net[d].items()
        ]
        history[d] = rows
    return history


def _load_board_history() -> dict:
    """读行业资金流历史 → {date: [board_rows]}。无文件返回 {}。"""
    p = paths.BOARD_FLOW_HISTORY
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_board_history(history: dict) -> str:
    """写行业资金流历史, 返回路径。"""
    os.makedirs(os.path.dirname(paths.BOARD_FLOW_HISTORY), exist_ok=True)
    with open(paths.BOARD_FLOW_HISTORY, "wb") as f:
        pickle.dump(history, f)
    return paths.BOARD_FLOW_HISTORY


def rebuild_board_flow_history(stock_mf: dict = None) -> dict:
    """从 mf.pkl (个股资金流) 全量重建行业资金流历史。

    纯本地计算, 不联网。mf.pkl 更新后调用, 把新增的交易日补进历史。
    增量友好: 只重算, 但结果与旧历史取并集 (保留旧文件里 mf.pkl 已删的早期日)。

    Args:
      stock_mf: 个股资金流 {code: [rows]}; None 则从 mf.pkl 读。
    Returns:
      {date: [board_rows]}
    """
    if stock_mf is None:
        stock_mf = _load_single()
    code_ind = _load_industry_map()
    new_hist = _aggregate_board_flow_history(stock_mf, code_ind)

    # 与旧历史并集 (保留 mf.pkl 已裁剪掉的早期交易日)
    old_hist = _load_board_history()
    merged = dict(old_hist)
    merged.update(new_hist)
    # 按日期升序重排 (dict 保持插入序)
    merged = {d: merged[d] for d in sorted(merged)}
    return merged


# ══════════════════════════════════════════════════════════
# 板块资金流刷新
# ══════════════════════════════════════════════════════════

def _refresh_board_flow():
    """实时刷新板块资金流 (akshare 今日排名), 仅打印不入盘。

    持久历史由 rebuild_board_flow_history() 负责; 这里只做实时快照展示。
    """
    print("\n🔄 实时板块资金流 (akshare)...")
    try:
        from tradingagents.agents.picker import rotation as rot
        txt, rows = rot.get_board_flow_ranking(top_n=15)
        print(f"  {txt} | 共 {len(rows)} 个板块")
        for r in rows[:5]:
            print(f"    {r['rank']}. {r['name']:20s} 主力净流入: {r['main_net_yi']:+.2f}亿")
    except Exception as e:
        print(f"  板块资金流刷新失败: {e}")


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="增量拉取资金流数据到单一缓存文件")
    parser.add_argument("--purge-old", action="store_true",
                        help="成功后删除旧 mf_YYYY-MM-DD.pkl 文件")
    parser.add_argument("--no-board", action="store_true",
                        help="跳过板块资金流刷新")
    args = parser.parse_args()

    with open(paths.STOCK_WHITELIST) as f:
        wl = json.load(f)
    wl_codes = [s["code"] for s in wl]
    # 热股池以 fundamentals/ 为唯一真相源: 白名单外的新股 (次新/小代码) 也要覆盖,
    # 不再用 白名单∩fundamentals, 否则会漏掉 ~10 只没进 stock_whitelist 的热股。
    fund_set = set(_scan_fundamentals())
    hot_codes = sorted(fund_set)
    cold_codes = [c for c in wl_codes if c not in fund_set]
    off_wl = len(fund_set - set(wl_codes))
    print(f"白名单: {len(wl_codes)} 只 | 热股池 fundamentals/: {len(hot_codes)} 只"
          f" (含白名单外 {off_wl} 只新股) | 非热股 {len(cold_codes)} 只")

    # 1. 加载单一缓存
    existing = _load_single()
    print(f"基准缓存: mf.pkl ({len(existing)} 只)")

    # 2. 首次迁移旧文件 (纯本地, 不联网): 把旧 mf_YYYY-MM-DD.pkl 合并进来
    existing, migrated = _migrate_from_old_files(existing)
    if existing:
        print(f"  合并后: {len(existing)} 只")

    # 3. 非热股: 仅修剪到 60 天, 不联网 (已有数据直接保留)
    trimmed = 0
    for c in cold_codes:
        old = existing.get(c)
        if isinstance(old, list) and len(old) > NORMAL_DAYS:
            existing[c] = old[-NORMAL_DAYS:]
            trimmed += 1
    print(f"非热股 {len(cold_codes)} 只: 修剪到 {NORMAL_DAYS} 天 ({trimmed} 只超长被裁剪), 不联网")

    # 4. 热股增量更新: 补齐到 14 个月 + 每日增量
    print(f"模式: 增量更新 (仅热股 {len(hot_codes)} 只)")
    result = incr_update(hot_codes, existing)

    # 5. 保存单一文件
    path = _save_single(result)
    print(f"  已保存: {path} ({len(result)} 只)")

    # 6. 抽查深度
    for c in wl_codes[:3]:
        rows = result.get(c, [])
        if rows:
            print(f"    {c} ({'热股' if _is_hot(c) else '普通'}): "
                  f"{rows[0]['date']} ~ {rows[-1]['date']} ({len(rows)}日)")

    # 7. 行业资金流历史: 从 mf.pkl 按热股行业汇总 (纯本地, 不联网)
    print("\n📊 重建行业资金流历史 (mf.pkl → 热股行业汇总)...")
    board_hist = rebuild_board_flow_history(result)
    hp = _save_board_history(board_hist)
    dates = sorted(board_hist)
    print(f"  已保存: {hp} ({len(board_hist)} 个交易日, {dates[0]}~{dates[-1]})")
    if dates:
        latest = board_hist[dates[-1]]
        top = sorted(latest, key=lambda x: -x["main_net_yi"])[:3]
        for r in top:
            print(f"    {dates[-1]} TOP: {r['industry']} {r['main_net_yi']:+.2f}亿")

    # 8. 可选: 删除旧文件
    if args.purge_old:
        old_files = [f for f in glob.glob(os.path.join(paths.MF_CACHE_DIR, "mf_*.pkl"))
                     if os.path.basename(f) != "mf.pkl"]
        for f in old_files:
            os.remove(f)
        print(f"  已删除 {len(old_files)} 个旧日期文件")

    # 9. 刷新板块资金流 (实时接口, 独立于历史)
    if not args.no_board:
        _refresh_board_flow()

    print("\n全部完成。")


if __name__ == "__main__":
    main()
