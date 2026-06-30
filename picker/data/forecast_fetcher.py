"""业绩预告拉取 (akshare 东财 stock_yjyg_em) → 磁盘缓存。

供 v3 评分 surge 催化判断: 业绩预告是 A 股最强 30 天催化, 带公告日期 + 预增幅度,
直接满足 surge "催化日期硬要求" (PROMPT_V3E 要求高分档催化含具体日期, LLM 原本拿不到真日历)。

每日维护 step 2.8 调 precompute_pool_forecast 刷新; v3._call 经 _compute_forecast_signals
读取注入 surge_block。

设计参照: mispriced_attribution_cache (磁盘缓存 + _updated 字段) +
_fetch_sector_fund_flow (akshare 东财接口 3 次重试应对 ConnectionError)。

用法:
    uv run python3 picker/data/forecast_fetcher.py        # 拉取写缓存
"""
import json
import os
import time as _time
from datetime import datetime, timedelta

from picker.paths import FORECAST_CACHE

# 季末日 (报告期月-日): 一季报0331 / 中报0630 / 三季报0930 / 年报1231
_QEND_DAY = {3: "31", 6: "30", 9: "30", 12: "31"}


def _recent_periods(n=4, today=None):
    """推算最近 n 个报告期 (季末, YYYYMMDD), 从当前季末(含)倒推。

    今天 2026-06-27 (6月在Q2) → 当前季末 0630(中报,披露中) + 倒推 0331/上年1231/上年0930 →
    ['20260630', '20260331', '20251231', '20250930']。多拉一个报告期保险, 靠公告日期近60天过滤。
    """
    today = today or datetime.now()
    y, m = today.year, today.month
    qend_m = ((m - 1) // 3 + 1) * 3  # 当前季末月 3/6/9/12
    periods = []
    cy, cm = y, qend_m
    for _ in range(n):
        periods.append(f"{cy}{cm:02d}{_QEND_DAY[cm]}")
        cm -= 3
        if cm <= 0:
            cm += 12
            cy -= 1
    return periods


def fetch_earnings_forecast(periods=None, recent_days=60):
    """akshare 拉取最近报告期业绩预告, 按公告日期过滤近 recent_days 天。

    Returns: {code: [{report_period, notice_date, type, change_pct, summary}]}
    同股多报告期/多指标 → 保留公告日期最近的一条 (surge 只需最近催化)。
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
                df = ak.stock_yjyg_em(date=period)
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
            notice = str(row.get("公告日期", ""))[:10]
            if not notice or len(notice) != 10 or notice < cutoff_date:
                continue  # 过滤近期
            ftype = str(row.get("预告类型", "")).strip()
            change = row.get("业绩变动幅度", None)
            try:
                change_pct = float(change) if change is not None and str(change) != "nan" else None
            except (ValueError, TypeError):
                change_pct = None
            summary = str(row.get("业绩变动", ""))[:120]
            rec = {
                "report_period": period,
                "notice_date": notice,
                "type": ftype,
                "change_pct": change_pct,
                "summary": summary,
            }
            # 一股去重: 优先"归属于上市公司股东的净利润"(surge 主信号), 同类保留公告日期最新
            indicator = str(row.get("预测指标", ""))
            is_parent = ("归属于" in indicator) or ("净利润" in indicator and "扣除非经常性" not in indicator and "扣非" not in indicator and "营收" not in indicator)
            rec["_is_parent"] = is_parent
            if code in out:
                cur = out[code][0]
                cur_is_parent = cur.get("_is_parent", False)
                if (is_parent and not cur_is_parent) or (is_parent == cur_is_parent and notice > cur.get("notice_date", "")):
                    out[code] = [rec]
            else:
                out[code] = [rec]
    return out


def precompute_pool_forecast():
    """每日维护调用: 拉取近期业绩预告 → 写 FORECAST_CACHE。返回统计 dict。"""
    periods = _recent_periods(n=4)
    forecasts = fetch_earnings_forecast(periods=periods, recent_days=60)
    cache = {
        "_updated": datetime.now().strftime("%Y-%m-%d"),
        "periods": periods,
        "forecasts": forecasts,
    }
    os.makedirs(os.path.dirname(FORECAST_CACHE), exist_ok=True)
    with open(FORECAST_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    n_records = sum(len(v) for v in forecasts.values())
    print(f"  [forecast] 拉 {len(periods)} 个报告期 | 覆盖 {len(forecasts)} 只股 / {n_records} 条预告"
          f" | 写 {os.path.basename(FORECAST_CACHE)}")
    return {"periods": periods, "n_codes": len(forecasts), "n_records": n_records}


if __name__ == "__main__":
    precompute_pool_forecast()
