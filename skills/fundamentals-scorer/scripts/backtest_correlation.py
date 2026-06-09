#!/usr/bin/env python3
"""基本面评分 vs 半年涨幅 相关性回测"""
import json, os, sys, time, math

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)

SCORES_FILE = os.path.join(_PROJECT_ROOT, ".fundamental_scores_batch.json")
OUTPUT_FILE = os.path.join(_PROJECT_ROOT, ".backtest_correlation.json")

# 半年区间
END_DATE = "2026-06-09"
START_DATE = "2025-12-09"


def get_price_akshare(code):
    """用 akshare 获取起止日期价格，返回 (start_price, end_price) 或 None"""
    try:
        import akshare as ak
    except ImportError:
        return None

    # 转换代码格式
    if code.startswith("6") or code.startswith("68"):
        symbol = f"sh{code}"
    else:
        symbol = f"sz{code}"

    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=START_DATE.replace("-", ""),
                                end_date=END_DATE.replace("-", ""),
                                adjust="qfq")
        if df is None or len(df) < 2:
            return None
        start_row = df.iloc[0]
        end_row = df.iloc[-1]
        return float(start_row["收盘"]), float(end_row["收盘"])
    except Exception as e:
        return None


def get_price_mootdx(code):
    """用 mootdx 获取价格"""
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std', timeout=10)
    except ImportError:
        return None

    # mootdx 可能不支持这么长区间，尝试获取 K 线
    try:
        # mootdx 用通达信格式: 市场#代码
        market = 1 if code.startswith("6") else 0  # 1=上海, 0=深圳
        df_start = client.bars(symbol=code, frequency=9, start=0, offset=120)  # 日线约半年
        if df_start is None or len(df_start) < 2:
            return None
        # 简化：取最早和最新
        return float(df_start.iloc[0]["close"]), float(df_start.iloc[-1]["close"])
    except:
        return None


def get_price_tencent(code):
    """用腾讯行情 API 获取半年线"""
    import urllib.request
    market = "sh" if code.startswith("6") else "sz"
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,130,qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        klines = data.get("data", {}).get(f"{market}{code}", {}).get("day", []) or \
                 data.get("data", {}).get(f"{market}{code}", {}).get("qfqday", [])
        if not klines or len(klines) < 2:
            return None
        # 每条: [日期, 开, 收, 高, 低, 量]
        start_close = float(klines[0][2])
        end_close = float(klines[-1][2])
        return start_close, end_close
    except:
        return None


def main():
    # 加载评分
    with open(SCORES_FILE, "r") as f:
        data = json.load(f)
    results = data["results"]

    stocks = []
    for code, r in results.items():
        v1 = r.get("v1", {})
        v2 = r.get("v2", {})
        v1_score = v1.get("score") if v1 else None
        v2_total = v2.get("total") if v2 else None
        rule = r.get("rule")
        if v2_total is not None:
            stocks.append({
                "code": code,
                "name": r.get("name", ""),
                "v1": v1_score,
                "v2_total": v2_total,
                "v2_fund": v2.get("fundamental_score"),
                "v2_sect": v2.get("sector_score"),
                "rule": rule,
            })

    print(f"加载 {len(stocks)} 只股票评分")

    # 逐个获取价格
    prices = {}
    success = 0
    fail = 0
    providers_tried = [get_price_tencent, get_price_akshare, get_price_mootdx]

    for i, s in enumerate(stocks):
        code = s["code"]
        price = None
        for provider in providers_tried:
            price = provider(code)
            if price:
                break

        if price:
            start_p, end_p = price
            if start_p > 0 and end_p > 0:
                ret = (end_p - start_p) / start_p
                prices[code] = {"start": start_p, "end": end_p, "return": ret}
                success += 1
            else:
                fail += 1
        else:
            fail += 1

        if (i + 1) % 50 == 0:
            print(f"  价格获取: {i+1}/{len(stocks)} (成功 {success}, 失败 {fail})")

    print(f"\n价格获取完成: 成功 {success}, 失败 {fail}")

    # 合并评分和涨幅
    merged = []
    for s in stocks:
        code = s["code"]
        if code in prices:
            p = prices[code]
            merged.append({
                **s,
                "start_price": p["start"],
                "end_price": p["end"],
                "return_6m": p["return"],
            })

    print(f"合并后 {len(merged)} 只有效样本")

    # ===== 相关性计算 =====
    def spearman(xs, ys):
        """Spearman 秩相关系数"""
        n = len(xs)
        if n < 3:
            return 0, 0

        def rank(arr):
            sorted_idx = sorted(range(n), key=lambda i: arr[i])
            ranks = [0] * n
            i = 0
            while i < n:
                j = i
                while j < n and arr[sorted_idx[j]] == arr[sorted_idx[i]]:
                    j += 1
                avg_rank = (i + j - 1) / 2.0 + 1
                for k in range(i, j):
                    ranks[sorted_idx[k]] = avg_rank
                i = j
            return ranks

        x_ranks = rank(xs)
        y_ranks = rank(ys)
        mean_xr = sum(x_ranks) / n
        mean_yr = sum(y_ranks) / n

        cov = sum((x_ranks[i] - mean_xr) * (y_ranks[i] - mean_yr) for i in range(n))
        sx = math.sqrt(sum((r - mean_xr) ** 2 for r in x_ranks))
        sy = math.sqrt(sum((r - mean_yr) ** 2 for r in y_ranks))

        rho = cov / (sx * sy) if sx * sy > 0 else 0
        # t-statistic
        t_stat = rho * math.sqrt((n - 2) / (1 - rho * rho)) if abs(rho) < 1 else float('inf')
        return rho, t_stat

    def pearson(xs, ys):
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        r = cov / (sx * sy) if sx * sy > 0 else 0
        return r

    returns = [m["return_6m"] for m in merged]

    # 各维度分别算相关性
    metrics = {
        "V2 总分": [m["v2_total"] for m in merged],
        "V2 基本面": [m["v2_fund"] or 0 for m in merged],
        "V2 赛道": [m["v2_sect"] or 0 for m in merged],
        "V1 得分": [m["v1"] if m["v1"] is not None else 0 for m in merged],
        "规则引擎": [m["rule"] if m["rule"] is not None else 0 for m in merged],
    }

    print(f"\n{'='*70}")
    print(f"  📈 半年涨幅相关性分析 (2025-12-09 → 2026-06-09, n={len(merged)})")
    print(f"{'='*70}")
    print(f"{'维度':<14} {'Spearman ρ':>10} {'t-stat':>10} {'Pearson r':>10}")
    print(f"{'-'*44}")

    corr_results = {}
    for label, xs in metrics.items():
        rho, t_stat = spearman(xs, returns)
        r = pearson(xs, returns)
        corr_results[label] = {"spearman": round(rho, 4), "t_stat": round(t_stat, 2), "pearson": round(r, 4)}
        sig = "***" if abs(t_stat) > 3.3 else ("**" if abs(t_stat) > 2.6 else ("*" if abs(t_stat) > 1.96 else ""))
        print(f"{label:<14} {rho:>10.4f} {t_stat:>10.2f}{sig} {r:>10.4f}")

    print(f"\n  * p<0.05  ** p<0.01  *** p<0.001")

    # ===== 分组回测 =====
    print(f"\n{'='*70}")
    print(f"  📊 分组回测：按 V2 总分分5组，看各组平均涨幅")
    print(f"{'='*70}")

    sorted_merged = sorted(merged, key=lambda x: x["v2_total"])
    n = len(sorted_merged)
    quintile_size = n // 5

    for q in range(5):
        start_idx = q * quintile_size
        end_idx = start_idx + quintile_size if q < 4 else n
        group = sorted_merged[start_idx:end_idx]
        avg_ret = sum(m["return_6m"] for m in group) / len(group)
        avg_v2 = sum(m["v2_total"] for m in group) / len(group)
        min_score = group[0]["v2_total"]
        max_score = group[-1]["v2_total"]
        positive = sum(1 for m in group if m["return_6m"] > 0)
        print(f"  Q{q+1} (V2 {min_score}-{max_score}, avg {avg_v2:.1f}): "
              f"平均涨幅 {avg_ret*100:+.2f}%, 上涨比例 {positive}/{len(group)}")

    # Top/Bottom 表现
    top20 = sorted_merged[-20:]
    bot20 = sorted_merged[:20]
    avg_top = sum(m["return_6m"] for m in top20) / 20
    avg_bot = sum(m["return_6m"] for m in bot20) / 20
    print(f"\n  Top 20 (V2高分) 平均涨幅: {avg_top*100:+.2f}%")
    print(f"  Bottom 20 (V2低分) 平均涨幅: {avg_bot*100:+.2f}%")
    print(f"  多空收益差: {(avg_top - avg_bot)*100:+.2f}%")

    # 保存结果
    output = {
        "meta": {
            "start_date": START_DATE,
            "end_date": END_DATE,
            "sample_size": len(merged),
            "price_fetch_failed": fail,
        },
        "correlation": corr_results,
        "top20_avg_return": round(avg_top, 6),
        "bottom20_avg_return": round(avg_bot, 6),
        "long_short_spread": round(avg_top - avg_bot, 6),
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n💾 回测结果已保存: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()