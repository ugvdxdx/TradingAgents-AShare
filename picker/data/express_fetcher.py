"""业绩快报拉取 (akshare 东财 stock_yjkb_em) → 磁盘缓存。

供 v3 评分 surge 催化判断: 业绩快报披露窗口在预告之后、正式财报之前, 数据比预告更准
(快报为初步核算值, 预告为区间预测)。与业绩预告(yjyg)并列, 消费端 _compute_forecast_signals
按 "快报优先覆盖预告" 合并 — 同一股同时有两者时用快报。

每日维护 step 2.9 调 precompute_pool_express 刷新; v3._call 经 _compute_forecast_signals
读取注入 surge_block。

设计参照: forecast_fetcher (同样磁盘缓存 + _updated + 3 次重试 + _recent_periods 推算报告期)。

用法:
    uv run python3 picker/data/express_fetcher.py        # 拉取写缓存
"""
import json
import os
import time as _time
from datetime import datetime, timedelta

from picker.data.forecast_fetcher import _recent_periods  # 复用季末推算 (不重复实现)
from picker.paths import EXPRESS_CACHE


def _infer_type(change_pct, net_profit):
    """快报无'预告类型'列, 由净利润同比 + 净利润符号推断。"""
    if net_profit is not None and net_profit < 0:
        # 亏损: 同比恶化(同比更负)算续亏/增亏, 同比改善算减亏
        return "快报续亏" if (change_pct is not None and change_pct < 0) else "快报减亏"
    if change_pct is None:
        return "快报"
    if change_pct >= 50:
        return "快报大增"
    if change_pct > 0:
        return "快报预增"
    return "快报预减"


def _fmt_netprofit(v):
    """净利润原始值(元) → 'X.XX亿'/'X.XX万' 可读串。"""
    if v is None:
        return "?"
    yi = v / 1e8
    if abs(yi) >= 0.01:
        return f"{yi:.2f}亿"
    return f"{v/1e4:.2f}万"


def fetch_earnings_express(periods=None, recent_days=60):
    """akshare 拉取最近报告期业绩快报, 按公告日期过滤近 recent_days 天。

    Returns: {code: [{report_period, notice_date, type, change_pct, net_profit, summary}]}
    同股多报告期 → 保留公告日期最近的一条。
    内置 3 次重试 (东财间歇 ConnectionError)。
    """
    if periods is None:
        periods = _recent_periods(n=4)
    cutoff_date = (datetime.now() - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    out = {}
    for period in periods:
        df = None
        for attempt in range(3):
            try:
                import akshare as ak
                df = ak.stock_yjkb_em(date=period)
                break
            except Exception:
                if attempt < 2:
                    _time.sleep(1.5 * (attempt + 1))
                    continue
                df = None
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            code = str(row.get("股票代码", "")).zfill(6)
            if not code or code == "000000":
                continue
            # 公告日期: akshare 返回 datetime.date → 转 YYYY-MM-DD 字符串
            nd = row.get("公告日期", None)
            if hasattr(nd, "strftime"):
                notice = nd.strftime("%Y-%m-%d")
            else:
                notice = str(nd)[:10]
            if not notice or len(notice) != 10 or notice < cutoff_date:
                continue  # 过滤近期
            change = row.get("净利润-同比增长", None)
            try:
                change_pct = float(change) if change is not None and str(change) != "nan" else None
            except (ValueError, TypeError):
                change_pct = None
            np_raw = row.get("净利润-净利润", None)
            try:
                net_profit = float(np_raw) if np_raw is not None and str(np_raw) != "nan" else None
            except (ValueError, TypeError):
                net_profit = None
            ftype = _infer_type(change_pct, net_profit)
            chg_str = f"{change_pct:+.0f}%" if change_pct is not None else "未披露幅度"
            summary = f"快报净利润 {_fmt_netprofit(net_profit)}, 同比 {chg_str}"
            rec = {
                "report_period": period,
                "notice_date": notice,
                "type": ftype,
                "change_pct": change_pct,
                "net_profit": net_profit,
                "summary": summary,
            }
            # 一股去重: 保留公告日期最新条 (快报无"预测指标"多行问题, 单股单报告期一行)
            if code in out:
                if notice > out[code][0].get("notice_date", ""):
                    out[code] = [rec]
            else:
                out[code] = [rec]
    return out


def precompute_pool_express():
    """每日维护调用: 拉取近期业绩快报 → 写 EXPRESS_CACHE。返回统计 dict。"""
    periods = _recent_periods(n=4)
    expresses = fetch_earnings_express(periods=periods, recent_days=60)
    cache = {
        "_updated": datetime.now().strftime("%Y-%m-%d"),
        "periods": periods,
        "expresses": expresses,
    }
    os.makedirs(os.path.dirname(EXPRESS_CACHE), exist_ok=True)
    with open(EXPRESS_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    n_records = sum(len(v) for v in expresses.values())
    print(f"  [express] 拉 {len(periods)} 个报告期 | 覆盖 {len(expresses)} 只股 / {n_records} 条快报"
          f" | 写 {os.path.basename(EXPRESS_CACHE)}")
    return {"periods": periods, "n_codes": len(expresses), "n_records": n_records}


if __name__ == "__main__":
    precompute_pool_express()
