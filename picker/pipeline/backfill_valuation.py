"""轻量补 key_metrics 估值字段, 不触发 LLM 叙事重写。

用途: surge price-in 估值信号(板块相对分位 + PEG)需要 key_metrics 里有
pe_ttm/pb/ps_ttm/total_mv_yi/turnover_rate/dv_ratio/netprofit_yoy。完整 refresh_fundamentals
会花 ~530 次 LLM 重写叙事; 本脚本只补估值字段 (Tushare only, 0 LLM):
  - PE/PB/PS/总市值/换手/股息率: 1 次 daily_basic(全市场某日) 按 ts_code 查
  - 净利同比 netprofit_yoy: fina_indicator 每股 1 次

用法:
  uv run python3 picker/pipeline/backfill_valuation.py             # 全池补
  uv run python3 picker/pipeline/backfill_valuation.py --dry-run   # 只统计不改
  uv run python3 picker/pipeline/backfill_valuation.py --code 001309  # 单只
  uv run python3 picker/pipeline/backfill_valuation.py --limit 20   # 前 N 只(测试)
"""
import os
import sys
import json
import time
import glob
import argparse
from datetime import datetime, timedelta

from picker import paths
from picker.data.fundamentals_data import (
    _get_pro_api, _code_to_ts_code, _tushare_query, _safe_float,
)

DAILY_BASIC_FIELDS = "ts_code,pe_ttm,pb,ps_ttm,total_mv,turnover_rate,dv_ratio"
YOY_SLEEP = 0.3  # fina_indicator 每股调用间隔, 规避 Tushare 速率限制


def _fetch_market_daily_basic(pro):
    """1 次全市场 daily_basic → {ts_code: row}。从今天回退找最近有数据的交易日。"""
    for back in range(8):
        d = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
        df = _tushare_query(pro.daily_basic, "ALL", 1, trade_date=d, fields=DAILY_BASIC_FIELDS)
        if df is not None and len(df) > 0:
            return dict(zip(df["ts_code"], df.to_dict("records"))), d
    return None, None


def _fetch_netprofit_yoy(pro, code):
    """单股 netprofit_yoy: fina_indicator 取最近年报的净利同比。"""
    ts = _code_to_ts_code(code)
    fi = _tushare_query(pro.fina_indicator, ts, 1, ts_code=ts, limit=4)
    if fi is None or len(fi) == 0:
        return None
    fi_y = fi[fi["end_date"].astype(str).str.endswith("1231")]
    row = fi_y.iloc[0] if len(fi_y) > 0 else fi.iloc[0]
    return _safe_float(row.get("netprofit_yoy"))


def backfill_one(code, market_db, pro, dry_run=False):
    """给单只 fundamentals JSON 补 key_metrics 估值字段。返回 (状态, 详情)。"""
    path = os.path.join(paths.FUNDAMENTALS_DIR, f"{code}.json")
    if not os.path.exists(path):
        return "skip", "no_fundamentals"
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return "skip", "json_corrupt"
    km = d.setdefault("financial_health", {}).setdefault("key_metrics", {})
    ts = _code_to_ts_code(code)
    row = (market_db or {}).get(ts)

    # daily_basic 估值字段 (仅当取到值才覆盖, 避免空值冲掉旧值)
    if row is not None:
        for fld, src in [("pe_ttm", "pe_ttm"), ("pb", "pb"), ("ps_ttm", "ps_ttm"),
                         ("turnover_rate", "turnover_rate"), ("dv_ratio", "dv_ratio")]:
            v = _safe_float(row.get(src))
            if v is not None:
                km[fld] = v
        total_mv = _safe_float(row.get("total_mv"))
        if total_mv is not None:
            km["total_mv_yi"] = round(total_mv / 1e4, 1)

    # netprofit_yoy
    yoy = _fetch_netprofit_yoy(pro, code)
    if yoy is not None:
        km["netprofit_yoy"] = yoy

    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    return "ok", (f"PE={km.get('pe_ttm')} PB={km.get('pb')} yoy={km.get('netprofit_yoy')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不改文件")
    ap.add_argument("--code", help="只补单只")
    ap.add_argument("--limit", type=int, help="只处理前 N 只(测试)")
    args = ap.parse_args()

    pro = _get_pro_api()
    if pro is None:
        print("✗ 无 TUSHARE_TOKEN, 退出"); sys.exit(1)

    if args.code:
        codes = [args.code]
        market_db, dt = _fetch_market_daily_basic(pro)
    else:
        files = sorted(glob.glob(os.path.join(paths.FUNDAMENTALS_DIR, "*.json")))
        codes = [os.path.basename(f).replace(".json", "") for f in files]
        if args.limit:
            codes = codes[: args.limit]
        print(f"全市场 daily_basic 拉取中...")
        market_db, dt = _fetch_market_daily_basic(pro)
        if market_db is None:
            print("✗ daily_basic 拉取失败, 退出"); sys.exit(1)
        print(f"✓ 市场快照 {len(market_db)} 只 (交易日 {dt})")

    print(f"待补: {len(codes)} 只 | dry_run={args.dry_run}")
    n_ok = n_skip = 0
    for i, code in enumerate(codes, 1):
        st, detail = backfill_one(code, market_db, pro, dry_run=args.dry_run)
        if st == "ok":
            n_ok += 1
        else:
            n_skip += 1
        if i % 50 == 0 or i == len(codes) or args.code:
            print(f"  [{i}/{len(codes)}] {code}: {st} ({detail})", flush=True)
        if not args.code:
            time.sleep(YOY_SLEEP)  # fina_indicator 限速

    print(f"\n✓ 完成: 补值 {n_ok} 只, 跳过 {n_skip} 只")
    if not args.dry_run:
        print("下一步: 跑 `uv run python3 picker/scoring/v3_full_score.py` 重评 (新 surge_block 含估值分位+PEG)")


if __name__ == "__main__":
    main()
