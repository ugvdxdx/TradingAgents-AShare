from __future__ import annotations
"""A-share market data provider using direct HTTP/TCP API calls.

Zero third-party data wrapper dependencies (no akshare/baostock).
All data sourced from: mootdx (TCP), Tencent (HTTP), Eastmoney (HTTP),
THS (HTTP), Baidu (HTTP), Sina (HTTP), CLS (HTTP), cninfo (HTTP).

Source: a-stock-data/SKILL.md (28 endpoints, V3.1 verified 2026-05-19)
"""

import json
import logging
import re
import urllib.request
import uuid
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from stockstats import wrap

from .base import BaseMarketDataProvider
from ..trade_calendar import cn_market_phase, cn_no_data_reason, cn_today_str, is_cn_trading_day

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# ── helpers ──

def _get_prefix(code: str) -> str:
    """6-digit code → market prefix (sh/sz/bj)."""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


def _market_code(code: str) -> int:
    """6-digit code → eastmoney market number (1=SH, 0=SZ)."""
    return 1 if code.startswith("6") else 0


def _normalize_symbol(symbol: str) -> str:
    """Extract pure 6-digit code from any format."""
    s = symbol.strip().lower()
    m = re.search(r"(\d{6})", s)
    if not m:
        raise NotImplementedError(
            f"cn_astock only supports A-share 6-digit symbols, got: {symbol}"
        )
    return m.group(1)


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if not pd.isna(f) else None
    except (ValueError, TypeError):
        return None


def eastmoney_datacenter(report_name: str, columns: str = "ALL",
                          filter_str: str = "", page_size: int = 50,
                          sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """Eastmoney datacenter unified query (shared by 6 endpoints)."""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ── mootdx client ──

_mootdx_client = None
_mootdx_lock = None  # threading.Lock, lazy init


def _get_mootdx_client():
    """Lazy-init mootdx TCP client (singleton, thread-safe)."""
    global _mootdx_client, _mootdx_lock
    if _mootdx_lock is None:
        import threading
        _mootdx_lock = threading.Lock()
    with _mootdx_lock:
        if _mootdx_client is None:
            try:
                from mootdx.quotes import Quotes
                _mootdx_client = Quotes.factory(market='std')
            except ImportError as exc:
                raise NotImplementedError(
                    "cn_astock requires 'mootdx'. Install: pip install mootdx"
                ) from exc
        return _mootdx_client


# ── tencent quote ──

def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """Batch fetch Tencent Finance real-time quotes (GBK, ~88 fields)."""
    prefixed = []
    for c in codes:
        if c.startswith(("6", "9")):
            prefixed.append(f"sh{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name":         vals[1],
            "price":        float(vals[3]) if vals[3] else 0,
            "last_close":   float(vals[4]) if vals[4] else 0,
            "open":         float(vals[5]) if vals[5] else 0,
            "change_amt":   float(vals[31]) if vals[31] else 0,
            "change_pct":   float(vals[32]) if vals[32] else 0,
            "high":         float(vals[33]) if vals[33] else 0,
            "low":          float(vals[34]) if vals[34] else 0,
            "amount_wan":   float(vals[37]) if vals[37] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm":       float(vals[39]) if vals[39] else 0,
            "amplitude_pct":float(vals[43]) if vals[43] else 0,
            "mcap_yi":      float(vals[44]) if vals[44] else 0,
            "float_mcap_yi":float(vals[45]) if vals[45] else 0,
            "pb":           float(vals[46]) if vals[46] else 0,
            "limit_up":     float(vals[47]) if vals[47] else 0,
            "limit_down":   float(vals[48]) if vals[48] else 0,
            "vol_ratio":    float(vals[49]) if vals[49] else 0,
            "pe_static":    float(vals[52]) if vals[52] else 0,
        }
    return result


# ── Provider class ──

INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 日均线（SMA）：中期趋势指标。用途：识别趋势方向，并作为动态支撑/阻力参考。",
    "close_200_sma": "200 日均线（SMA）：长期趋势基准。用途：确认大级别趋势，并辅助识别金叉/死叉结构。",
    "close_10_ema": "10 日指数均线（EMA）：短期响应更快。用途：捕捉短线动量变化与潜在入场时机。",
    "macd": "MACD：趋势与动量综合指标。",
    "macds": "MACD 信号线（Signal）。",
    "macdh": "MACD 柱状图（Histogram）。",
    "rsi": "RSI：衡量超买/超卖的动量指标。",
    "boll": "布林中轨（20 日均线）。",
    "boll_ub": "布林上轨。",
    "boll_lb": "布林下轨。",
    "atr": "ATR：真实波动幅度均值，用于波动与风控。",
    "vwma": "VWMA：成交量加权均线。",
    "mfi": "MFI：资金流量指标。",
}


class AstockProvider(BaseMarketDataProvider):
    """A-share provider using direct HTTP/TCP API calls from a-stock-data.

    Zero wrapper dependencies. All data sourced from:
    mootdx (TCP), Tencent (HTTP), Eastmoney (HTTP), THS (HTTP),
    Baidu (HTTP), Sina (HTTP), CLS (HTTP), cninfo (HTTP).
    """

    @property
    def name(self) -> str:
        return "cn_astock"

    # ── internal helpers ──

    def _normalize_symbol(self, symbol: str) -> str:
        return _normalize_symbol(symbol)

    def _sina_symbol(self, symbol: str) -> str:
        code = _normalize_symbol(symbol)
        prefix = _get_prefix(code)
        return f"{prefix}{code}"

    def _is_likely_etf(self, code: str) -> bool:
        return code.startswith(("5", "15", "16", "18"))

    def _normalize_hist_df(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        """Normalize historical OHLCV DataFrame to standard format."""
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()

        col_map = {
            "日期": "Date", "date": "Date", "Date": "Date",
            "开盘": "Open", "open": "Open", "Open": "Open",
            "最高": "High", "high": "High", "High": "High",
            "最低": "Low", "low": "Low", "Low": "Low",
            "收盘": "Close", "close": "Close", "Close": "Close",
            "成交量": "Volume", "volume": "Volume", "Volume": "Volume",
            "amount": "Volume", "Amount": "Volume",
        }
        df = raw_df.rename(columns=col_map).copy()
        required = ["Date", "Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"hist dataframe missing columns: {missing}")

        out = df[required].copy()
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
        out = out.dropna(subset=["Date"]).sort_values("Date")

        for c in ["Open", "High", "Low", "Close", "Volume"]:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        out["Volume"] = out["Volume"].astype(float)
        return out

    def _format_hist(self, df: pd.DataFrame, symbol: str, start: str, end: str) -> str:
        """Format historical data as CSV with header (same format as CnAkshareProvider)."""
        if df is None or df.empty:
            return f"No data found for symbol '{symbol}' between {start} and {end}"
        out = self._normalize_hist_df(df)
        out["Dividends"] = 0.0
        out["Stock Splits"] = 0.0
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")

        header = f"# Stock data for {symbol} from {start} to {end}\n"
        header += f"# Total records: {len(out)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + out.to_csv(index=False)

    def _shrink_table(self, df: pd.DataFrame, max_rows: int = 12, max_cols: int = 16) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        rows = min(max_rows, len(df))
        cols = min(max_cols, len(df.columns))
        return df.head(rows).iloc[:, :cols]

    @staticmethod
    def _slice_hist_df(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        start_dt = pd.to_datetime(start_date, errors="coerce")
        end_dt = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(start_dt) or pd.isna(end_dt):
            return df
        out = df.copy()
        out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
        out = out.dropna(subset=["Date"])
        out = out[(out["Date"] >= start_dt) & (out["Date"] <= end_dt)]
        return out.sort_values("Date").reset_index(drop=True)

    # ── BaseMarketDataProvider implementations ──

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        """Fetch OHLCV via mootdx bars + tencent realtime append."""
        code = _normalize_symbol(symbol)

        # Source 1: mootdx bars (TCP, most reliable)
        try:
            client = _get_mootdx_client()
            market = 1 if code.startswith("6") else 0  # 1=SH, 0=SZ
            start_offset = 0  # fetch from beginning
            df_mootdx = client.bars(symbol=code, category=4, offset=300, market=market)
            if df_mootdx is not None and not df_mootdx.empty:
                # mootdx returns: open, close, high, low, vol, amount, datetime
                df = df_mootdx.rename(columns={
                    "datetime": "Date",
                    "open": "Open",
                    "close": "Close",
                    "high": "High",
                    "low": "Low",
                    "vol": "Volume",
                }).copy()
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                df = self._slice_hist_df(df, start_date, end_date)
                if not df.empty:
                    df = self._maybe_append_realtime_row(code, df, end_date)
                    return self._format_hist(df, symbol, start_date, end_date)
        except (NotImplementedError, Exception) as exc:
            logger.debug("[astock] mootdx bars failed: %s", exc)

        # Source 2: Baidu K-line (HTTP, fallback)
        try:
            bd_data = self._baidu_kline_raw(code, start_date.replace("-", ""))
            if bd_data and bd_data.get("rows"):
                df = self._parse_baidu_kline(bd_data, start_date, end_date)
                if not df.empty:
                    df = self._maybe_append_realtime_row(code, df, end_date)
                    return self._format_hist(df, symbol, start_date, end_date)
        except Exception as exc:
            logger.debug("[astock] baidu kline failed: %s", exc)

        raise NotImplementedError(
            f"cn_astock temporarily unavailable for price history "
            f"(mootdx/baidu both failed for {symbol})"
        )

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        """Fetch hist bars via mootdx, compute with stockstats."""
        if indicator not in INDICATOR_DESCRIPTIONS:
            raise ValueError(
                f"Indicator {indicator} is not supported. "
                f"Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
            )

        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - timedelta(days=max(look_back_days, 260))
        try:
            hist_str = self.get_stock_data(symbol, start_dt.strftime("%Y-%m-%d"), curr_date)
        except NotImplementedError:
            return f"No data found for {symbol} for indicator {indicator}"

        # Parse CSV back to DataFrame
        lines = hist_str.strip().split("\n")
        csv_start = 0
        for i, line in enumerate(lines):
            if line.startswith("Date,"):
                csv_start = i
                break
        if csv_start == 0 and "No data found" in hist_str:
            return f"No data found for {symbol} for indicator {indicator}"

        from io import StringIO as _StringIO
        df = pd.read_csv(_StringIO("\n".join(lines[csv_start:])))
        if df.empty:
            return f"No data found for {symbol} for indicator {indicator}"

        ind_df = df.rename(columns={
            "Date": "date", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })[["date", "open", "high", "low", "close", "volume"]].copy()
        ind_df["date"] = pd.to_datetime(ind_df["date"], errors="coerce")
        ind_df = ind_df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

        ss = wrap(ind_df)
        indicator_series = ss[indicator]

        values_by_date = {}
        for idx, dt_val in enumerate(ind_df["date"]):
            date_str = pd.to_datetime(dt_val).strftime("%Y-%m-%d")
            val = indicator_series.iloc[idx]
            values_by_date[date_str] = "N/A" if pd.isna(val) else str(val)

        begin = curr_dt - timedelta(days=look_back_days)
        lines_out = []
        d = curr_dt
        while d >= begin:
            key = d.strftime("%Y-%m-%d")
            if key in values_by_date:
                value = values_by_date[key]
                if value == "N/A":
                    value = cn_no_data_reason(key)
            else:
                value = cn_no_data_reason(key)
            lines_out.append(f"{key}: {value}")
            d -= timedelta(days=1)

        return (
            f"## {indicator} 指标值（{begin.strftime('%Y-%m-%d')} 至 {curr_date}）：\n\n"
            + "\n".join(lines_out)
            + "\n\n"
            + INDICATOR_DESCRIPTIONS[indicator]
        )

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        """PE/PB/mcap from tencent + quarterly snapshot from mootdx finance."""
        code = _normalize_symbol(ticker)

        parts = [f"## Fundamentals for {ticker}"]
        errors = []

        # Source 1: Tencent (PE, PB, mcap, etc.)
        try:
            quotes = tencent_quote([code])
            if code in quotes:
                q = quotes[code]
                info_df = pd.DataFrame([
                    {"item": k, "value": str(v)} for k, v in q.items()
                ])
                parts.append("### Company Profile (Tencent Finance)")
                parts.append(self._shrink_table(info_df, max_rows=25, max_cols=2).to_markdown(index=False))
        except Exception as exc:
            errors.append(f"tencent_quote: {type(exc).__name__}")

        # Source 2: mootdx finance (quarterly snapshot)
        try:
            client = _get_mootdx_client()
            fin = client.finance(symbol=code)
            if fin is not None and not fin.empty:
                parts.append("### Financial Snapshot (mootdx quarterly)")
                parts.append(self._shrink_table(fin, max_rows=20, max_cols=10).to_markdown(index=False))
        except (NotImplementedError, Exception) as exc:
            errors.append(f"mootdx finance: {type(exc).__name__}")

        # Source 3: Eastmoney stock info (industry, shares, mcap, list date)
        try:
            info = self._eastmoney_stock_info_raw(code)
            if info:
                info_df = pd.DataFrame([
                    {"item": k, "value": str(v)} for k, v in info.items()
                ])
                parts.append("### Basic Info (Eastmoney push2)")
                parts.append(info_df.to_markdown(index=False))
        except Exception as exc:
            errors.append(f"eastmoney_stock_info: {type(exc).__name__}")

        if len(parts) > 1:
            return "\n\n".join(parts)

        raise NotImplementedError(
            f"cn_astock temporarily unavailable for fundamentals: {'; '.join(errors)}"
        )

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        """Sina financial report — balance sheet."""
        code = _normalize_symbol(ticker)
        try:
            rows = self._sina_financial_report(code, "fzb")
            if rows:
                df = pd.DataFrame(rows)
                return f"## Balance Sheet ({ticker})\n\n{self._shrink_table(df, max_rows=12, max_cols=18).to_markdown(index=False)}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for balance sheet: {type(exc).__name__}: {exc}"
            ) from exc
        raise NotImplementedError(f"cn_astock: no balance sheet data for {ticker}")

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        """Sina financial report — cash flow statement."""
        code = _normalize_symbol(ticker)
        try:
            rows = self._sina_financial_report(code, "llb")
            if rows:
                df = pd.DataFrame(rows)
                return f"## Cashflow ({ticker})\n\n{self._shrink_table(df, max_rows=12, max_cols=18).to_markdown(index=False)}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for cashflow: {type(exc).__name__}: {exc}"
            ) from exc
        raise NotImplementedError(f"cn_astock: no cashflow data for {ticker}")

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        """Sina financial report — income statement."""
        code = _normalize_symbol(ticker)
        try:
            rows = self._sina_financial_report(code, "lrb")
            if rows:
                df = pd.DataFrame(rows)
                return f"## Income Statement ({ticker})\n\n{self._shrink_table(df, max_rows=12, max_cols=18).to_markdown(index=False)}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for income statement: {type(exc).__name__}: {exc}"
            ) from exc
        raise NotImplementedError(f"cn_astock: no income statement data for {ticker}")

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        """Eastmoney stock news (JSONP) + tencent for recent headlines."""
        code = _normalize_symbol(ticker)
        try:
            rows = self._eastmoney_stock_news(code, page_size=30)
            if not rows:
                return f"No news found for {ticker} between {start_date} and {end_date}"

            # Filter by date range
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            filtered = []
            for r in rows:
                try:
                    news_dt = pd.to_datetime(r.get("time", ""), errors="coerce")
                    if not pd.isna(news_dt) and start_dt <= news_dt < end_dt:
                        filtered.append(r)
                except Exception:
                    filtered.append(r)  # keep if date parsing fails

            if not filtered:
                filtered = rows[:20]  # fallback: return latest regardless of date

            lines = []
            for r in filtered[:20]:
                title = r.get("title", "No title")
                src = r.get("source", "Unknown")
                summary = r.get("content", "")[:400]
                url = r.get("url", "")
                lines.append(f"### {title} (source: {src})")
                if summary:
                    lines.append(summary)
                if url:
                    lines.append(f"Link: {url}")
                lines.append("")

            return f"## {ticker} 新闻（{start_date} 至 {end_date}）：\n\n" + "\n".join(lines)
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for news: {type(exc).__name__}: {exc}"
            ) from exc

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        """CLS telegraph + Eastmoney global news."""
        rows = []
        errors = []

        # Source 1: CLS telegraph (real-time)
        try:
            cls_rows = self._cls_telegraph(page_size=limit)
            rows.extend(cls_rows)
        except Exception as exc:
            errors.append(f"cls_telegraph: {type(exc).__name__}")

        # Source 2: Eastmoney global news (7x24)
        try:
            em_rows = self._eastmoney_global_news(page_size=limit)
            rows.extend(em_rows)
        except Exception as exc:
            errors.append(f"eastmoney_global_news: {type(exc).__name__}")

        if not rows:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for global news: {'; '.join(errors)}"
            )

        # Deduplicate by title
        seen = set()
        unique = []
        for r in rows:
            title = r.get("title", "")
            if title not in seen:
                seen.add(title)
                unique.append(r)

        lines = []
        start = (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
        for r in unique[:limit]:
            title = r.get("title", "No title")
            summary = (r.get("content", "") or r.get("summary", ""))[:300]
            time = r.get("time", "")
            lines.append(f"### {title} ({time})")
            if summary:
                lines.append(summary)
            lines.append("")

        return f"## 全球市场新闻（{start} 至 {curr_date}）：\n\n" + "\n".join(lines)

    def get_insider_transactions(self, symbol: str) -> str:
        """Holder count change + mootdx finance top holders."""
        code = _normalize_symbol(symbol)
        parts = [f"## Insider Transactions for {symbol}"]
        errors = []

        # Source 1: Holder count change (quarterly, chip concentration)
        try:
            holders = self._holder_num_change_raw(code)
            if holders:
                df = pd.DataFrame(holders)
                parts.append("### 股东户数变化（筹码集中度）")
                parts.append(self._shrink_table(df, max_rows=10, max_cols=6).to_markdown(index=False))
        except Exception as exc:
            errors.append(f"holder_num_change: {type(exc).__name__}")

        # Source 2: mootdx F10 — shareholder study
        try:
            client = _get_mootdx_client()
            text = client.F10(symbol=code, name='股东研究')
            if text:
                # Keep only latest period (-70% tokens per SKILL.md advice)
                lines = text.split("\n")
                truncated = "\n".join(lines[:80])
                parts.append("### 股东研究 (mootdx F10)")
                parts.append(truncated)
        except (NotImplementedError, Exception) as exc:
            errors.append(f"mootdx F10: {type(exc).__name__}")

        # Source 3: Margin trading data (financing balance)
        try:
            margin = self._margin_trading_raw(code, page_size=5)
            if margin:
                df_m = pd.DataFrame(margin)
                parts.append("### 融资融券近期变化")
                parts.append(df_m.to_markdown(index=False))
        except Exception as exc:
            errors.append(f"margin_trading: {type(exc).__name__}")

        if len(parts) > 1:
            return "\n\n".join(parts)
        raise NotImplementedError(
            f"cn_astock temporarily unavailable for insider transactions: {'; '.join(errors)}"
        )

    def get_realtime_quotes(self, symbols: list[str]) -> str:
        """Tencent batch quote + mootdx depth."""
        code_to_original: dict[str, str] = {}
        for s in symbols:
            if not s or not s.strip():
                continue
            try:
                code = _normalize_symbol(s)
            except NotImplementedError:
                continue
            if code and code not in code_to_original:
                code_to_original[code] = s.strip().upper()

        if not code_to_original:
            return json.dumps({})

        # Source 1: Tencent (has PE/PB/mcap/limit prices)
        try:
            tq = tencent_quote(list(code_to_original.keys()))
            result = {}
            for code, original in code_to_original.items():
                if code in tq:
                    q = tq[code]
                    price = _safe_float(q.get("price"))
                    prev_close = _safe_float(q.get("last_close"))
                    change = round(price - prev_close, 4) if price is not None and prev_close else None
                    change_pct = round(change / prev_close * 100, 4) if change is not None and prev_close else None
                    result[original] = {
                        "price": price,
                        "open": _safe_float(q.get("open")),
                        "high": _safe_float(q.get("high")),
                        "low": _safe_float(q.get("low")),
                        "previous_close": prev_close,
                        "change": change,
                        "change_pct": change_pct,
                        "volume": _safe_float(q.get("amount_wan")),  # Tencent amount_wan is in 万
                        "amount": _safe_float(q.get("amount_wan")),
                        "pe_ttm": _safe_float(q.get("pe_ttm")),
                        "pb": _safe_float(q.get("pb")),
                        "mcap_yi": _safe_float(q.get("mcap_yi")),
                        "limit_up": _safe_float(q.get("limit_up")),
                        "limit_down": _safe_float(q.get("limit_down")),
                        "source": "tencent",
                    }
            if result:
                return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.debug("[astock] tencent_quote failed: %s", exc)

        # Source 2: Sina fallback
        try:
            result = self._fetch_quotes_sina(code_to_original)
            if result and result != "{}":
                return result
        except Exception as exc:
            logger.debug("[astock] sina fallback failed: %s", exc)

        return json.dumps({})

    # ── cn_market_data extended methods ──

    def get_board_fund_flow(self) -> str:
        """Eastmoney industry fund flow ranking."""
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1", "pz": "100", "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fs": "m:90+t:2",
                "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
            }
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            if not items:
                return "今日板块资金流向数据暂不可用。"

            rows = []
            for i, item in enumerate(items):
                rows.append({
                    "排名": i + 1,
                    "名称": item.get("f14", ""),
                    "涨跌幅%": item.get("f3", 0),
                    "代码": item.get("f12", ""),
                    "上涨家数": item.get("f104", 0),
                    "下跌家数": item.get("f105", 0),
                    "领涨股": item.get("f140", ""),
                })

            df = pd.DataFrame(rows).sort_values("涨跌幅%", ascending=False).reset_index(drop=True)
            df.insert(0, "排名", range(1, len(df) + 1))
            total = len(df)
            result = df.head(10).to_string(index=False)
            return f"板块资金流向排名（共{total}个板块，前10名）：\n{result}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for board fund flow: {type(exc).__name__}: {exc}"
            ) from exc

    def get_individual_fund_flow(self, symbol: str) -> str:
        """Eastmoney push2 individual fund flow (minute + 120d)."""
        code = _normalize_symbol(symbol)
        try:
            # Minute-level realtime fund flow
            realtime = self._eastmoney_fund_flow_minute_raw(code)
            # 120-day daily fund flow
            daily = self._stock_fund_flow_120d_raw(code)

            parts = [f"{symbol} 资金流向数据："]
            if daily:
                df_d = pd.DataFrame(daily[-5:])
                parts.append("### 近5日主力资金净流向（日级）")
                parts.append(df_d.to_string(index=False))
            if realtime:
                df_r = pd.DataFrame(realtime[-5:])
                parts.append("### 最新5分钟资金流（分钟级）")
                parts.append(df_r.to_string(index=False))

            return "\n\n".join(parts) if len(parts) > 1 else f"{symbol} 近期主力资金流向数据暂不可用。"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for individual fund flow: {type(exc).__name__}: {exc}"
            ) from exc

    def get_lhb_detail(self, symbol: str, date: str) -> str:
        """Dragon tiger board detail from Eastmoney datacenter."""
        code = _normalize_symbol(symbol)
        try:
            data = dragon_tiger_board(code, date)
            records = data.get("records", [])
            if not records:
                return f"{symbol} 在 {date} 无龙虎榜数据（非异动日属正常）。"

            parts = [f"{symbol} 龙虎榜明细（{date}）："]
            df_r = pd.DataFrame(records)
            parts.append(df_r.head(10).to_string(index=False))

            seats = data.get("seats", {})
            if seats.get("buy"):
                parts.append("\n买入席位 TOP5:")
                df_buy = pd.DataFrame(seats["buy"])
                parts.append(df_buy.to_string(index=False))
            if seats.get("sell"):
                parts.append("\n卖出席位 TOP5:")
                df_sell = pd.DataFrame(seats["sell"])
                parts.append(df_sell.to_string(index=False))

            inst = data.get("institution", {})
            if inst:
                parts.append(f"\n机构净买入: {inst.get('net_amt', 0)}万元")

            return "\n".join(parts)
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for lhb detail: {type(exc).__name__}: {exc}"
            ) from exc

    def get_zt_pool(self, date: str) -> str:
        """Limit-up pool from Eastmoney datacenter."""
        try:
            date_clean = date.replace("-", "")
            data = eastmoney_datacenter(
                "RPT_DAILYBILLBOARD_DETAILSNEW",
                filter_str=f"(TRADE_DATE>='{date}')(TRADE_DATE<='{date}')",
                page_size=500,
                sort_columns="BILLBOARD_NET_AMT", sort_types="-1",
            )
            if not data:
                # Try eastmoney zt pool specifically
                zt_data = eastmoney_datacenter(
                    "RPTA_WEB_ZTBOARD",
                    filter_str=f"(TRADE_DATE='{date}')",
                    page_size=100,
                    sort_columns="FIRST_LIMIT_TIME", sort_types="1",
                )
                if not zt_data:
                    return f"{date} 涨停板情绪池数据暂不可用。"

                df = pd.DataFrame(zt_data)
                count = len(df)
                result = f"{date} 涨停家数：{count}\n"
                if "CONCEPT" in df.columns:
                    concepts = df["CONCEPT"].value_counts().head(10)
                    result += f"涨停概念分布：\n{concepts.to_string()}"
                return result

            # Use billboard data as approximation
            stocks = []
            for row in data:
                change_rate = row.get("CHANGE_RATE") or 0
                if abs(float(change_rate)) >= 9.9:  # likely limit-up
                    stocks.append({
                        "代码": row.get("SECURITY_CODE", ""),
                        "名称": row.get("SECURITY_NAME_ABBR", ""),
                        "涨跌幅": round(float(change_rate), 2),
                    })

            if stocks:
                df = pd.DataFrame(stocks)
                count = len(df)
                return f"{date} 涨停/接近涨停家数：{count}\n{df.head(20).to_string(index=False)}"
            return f"{date} 涨停板数据暂不可用。"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for zt pool: {type(exc).__name__}: {exc}"
            ) from exc

    def get_hot_stocks_xq(self) -> str:
        """THS hot reason (richer than xueqiu, with editorial reason tags)."""
        try:
            df = self._ths_hot_reason_raw()
            if df is None or df.empty:
                return "当日强势股数据暂不可用。"
            return f"当日强势股前20（含题材归因）：\n{df.head(20).to_string(index=False)}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for hot stocks: {type(exc).__name__}: {exc}"
            ) from exc

    # ── Sector / Concept Board methods ──

    def get_concept_boards(self, top_n: int = 30) -> str:
        """Eastmoney concept board ranking with change% and fund flow."""
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1", "pz": str(top_n), "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fs": "m:90+t:3",
                "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f6,f128,f136,f140,f141",
            }
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            if not items:
                return "概念板块数据暂不可用。"

            rows = []
            for i, item in enumerate(items):
                rows.append({
                    "排名": i + 1,
                    "名称": item.get("f14", ""),
                    "涨跌幅%": item.get("f3", 0),
                    "代码": item.get("f12", ""),
                    "上涨家数": item.get("f104", 0),
                    "下跌家数": item.get("f105", 0),
                    "成交额": item.get("f6", 0),
                    "领涨股": item.get("f140", ""),
                })

            df = pd.DataFrame(rows)
            total = len(df)
            result = df.head(top_n).to_string(index=False)
            return f"概念板块涨跌幅排名（共{total}个，前{min(top_n, total)}名）：\n{result}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for concept boards: {type(exc).__name__}: {exc}"
            ) from exc

    def get_concept_board_stocks(self, board_code: str) -> str:
        """Eastmoney concept board constituent stocks."""
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1", "pz": "50", "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fs": f"b:{board_code}+f:!50",
                "fields": "f2,f3,f4,f12,f13,f14,f6,f15,f16,f17",
            }
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            if not items:
                return f"板块 {board_code} 成分股数据暂不可用。"

            rows = []
            for i, item in enumerate(items):
                rows.append({
                    "排名": i + 1,
                    "代码": item.get("f12", ""),
                    "名称": item.get("f14", ""),
                    "涨跌幅%": item.get("f3", 0),
                    "现价": item.get("f2", 0),
                    "成交额": item.get("f6", 0),
                    "最高": item.get("f15", 0),
                    "最低": item.get("f16", 0),
                })

            df = pd.DataFrame(rows)
            total = len(df)
            result = df.head(30).to_string(index=False)
            return f"板块 {board_code} 成分股（共{total}只，前30名）：\n{result}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for board stocks: {type(exc).__name__}: {exc}"
            ) from exc

    def get_stock_concept_belonging(self, symbol: str) -> str:
        """Baidu PAE concept/industry/region belonging for a stock."""
        code = _normalize_symbol(symbol)
        try:
            url = (
                f"https://finance.pae.baidu.com/api/getrelatedblock"
                f"?code={code}&market=ab"
                f"&typeCode=all&finClientType=pc"
            )
            headers = {
                "User-Agent": UA,
                "Accept": "application/vnd.finance-web.v1+json",
                "Origin": "https://gushitong.baidu.com",
                "Referer": "https://gushitong.baidu.com/",
            }
            r = requests.get(url, headers=headers, timeout=10)
            d = r.json()
            if str(d.get("ResultCode", -1)) != "0":
                return f"{symbol} 概念归属数据暂不可用。"

            parts = [f"{symbol} 所属板块："]
            for block in d.get("Result", []):
                block_type = block.get("type", "")
                items_list = block.get("list", [])
                if not items_list:
                    continue
                type_label = ""
                if "行业" in block_type:
                    type_label = "行业"
                elif "概念" in block_type:
                    type_label = "概念"
                elif "地域" in block_type:
                    type_label = "地域"
                else:
                    continue

                entries = []
                for item in items_list:
                    name = item.get("name", "")
                    change = item.get("increase", "")
                    if change != "":
                        entries.append(f"{name}({change}%)")
                    else:
                        entries.append(name)
                parts.append(f"\n{type_label}：{', '.join(entries)}")

            return "\n".join(parts)
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for stock concept belonging: {type(exc).__name__}: {exc}"
            ) from exc

    def search_concept_board(self, keyword: str) -> str:
        """Search concept boards by keyword from Eastmoney."""
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1", "pz": "200", "po": "1", "np": "1",
                "fltt": "2", "invt": "2",
                "fs": "m:90+t:3",
                "fields": "f2,f3,f12,f13,f14,f104,f105,f6,f140",
            }
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            if not items:
                return f"未找到与'{keyword}'相关的概念板块。"

            keyword_lower = keyword.lower()
            matched = []
            for item in items:
                name = item.get("f14", "")
                if keyword_lower in name.lower():
                    matched.append({
                        "名称": name,
                        "代码": item.get("f12", ""),
                        "涨跌幅%": item.get("f3", 0),
                        "上涨家数": item.get("f104", 0),
                        "下跌家数": item.get("f105", 0),
                        "成交额": item.get("f6", 0),
                        "领涨股": item.get("f140", ""),
                    })

            if not matched:
                return f"未找到与'{keyword}'匹配的概念板块。"

            df = pd.DataFrame(matched)
            return f"概念板块搜索'{keyword}'结果（共{len(matched)}个）：\n{df.to_string(index=False)}"
        except Exception as exc:
            raise NotImplementedError(
                f"cn_astock temporarily unavailable for concept search: {type(exc).__name__}: {exc}"
            ) from exc

    # ── Internal raw API methods (from SKILL.md) ──

    def _baidu_kline_raw(self, code: str, start_time: str = "") -> dict:
        """Baidu stock K-line with embedded MA5/10/20."""
        url = "https://finance.pae.baidu.com/selfselect/getstockquotation"
        params = {
            "all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
            "isFutures": "false", "isStock": "true", "newFormat": "1",
            "group": "quotation_kline_ab", "finClientType": "pc",
            "code": code, "start_time": start_time, "ktype": "1",
        }
        headers = {
            "User-Agent": UA,
            "Accept": "application/vnd.finance-web.v1+json",
            "Origin": "https://gushitong.baidu.com",
            "Referer": "https://gushitong.baidu.com/",
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        return r.json().get("Result", {})

    def _parse_baidu_kline(self, bd_data: dict, start_date: str, end_date: str) -> pd.DataFrame:
        """Parse Baidu K-line data into standard OHLCV DataFrame."""
        md = bd_data.get("newMarketData", {})
        keys = md.get("keys", [])
        rows = md.get("marketData", "").split(";")

        if not keys or not rows:
            return pd.DataFrame()

        # Find index for key columns
        key_map = {}
        for i, k in enumerate(keys):
            kl = k.lower()
            if kl == "time" or kl == "date":
                key_map["Date"] = i
            elif kl == "open":
                key_map["Open"] = i
            elif kl == "close":
                key_map["Close"] = i
            elif kl == "high":
                key_map["High"] = i
            elif kl == "low":
                key_map["Low"] = i
            elif kl == "volume" or kl == "vol":
                key_map["Volume"] = i

        required = ["Date", "Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in key_map]
        if missing:
            return pd.DataFrame()

        parsed = []
        for row in rows:
            parts = row.split(",")
            if len(parts) < len(keys):
                continue
            entry = {}
            for col, idx in key_map.items():
                entry[col] = parts[idx] if idx < len(parts) else None
            parsed.append(entry)

        df = pd.DataFrame(parsed)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["Date", "Open", "High", "Low", "Close"])
        return self._slice_hist_df(df, start_date, end_date)

    def _maybe_append_realtime_row(
        self, code: str, hist_df: pd.DataFrame, end_date: str
    ) -> pd.DataFrame:
        """Append today's realtime row if end_date is today and market is open."""
        if hist_df is None:
            hist_df = pd.DataFrame()
        try:
            end_dt = pd.to_datetime(end_date, errors="coerce")
            if pd.isna(end_dt):
                return hist_df
            today = pd.to_datetime(cn_today_str())
            if end_dt.normalize() < today:
                return hist_df
            if not is_cn_trading_day(today.strftime("%Y-%m-%d")):
                return hist_df

            has_today = False
            if not hist_df.empty:
                has_today = (pd.to_datetime(hist_df["Date"]).dt.normalize() == today).any()
            if has_today:
                return hist_df

            phase = cn_market_phase()
            if phase in ("pre_open", "closed"):
                return hist_df

            # Use tencent for realtime
            quotes = tencent_quote([code])
            if code in quotes:
                q = quotes[code]
                row = {
                    "Date": today.normalize(),
                    "Open": q.get("open", 0),
                    "High": q.get("high", 0),
                    "Low": q.get("low", 0),
                    "Close": q.get("price", 0),
                    "Volume": q.get("amount_wan", 0) * 10000,  # 万→元
                }
                rt = pd.DataFrame([row]).dropna(subset=["Open", "High", "Low", "Close"])
                if not rt.empty:
                    merged = pd.concat([hist_df, rt], ignore_index=True)
                    merged = merged.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
                    return merged.reset_index(drop=True)
        except Exception:
            pass
        return hist_df

    def _eastmoney_stock_info_raw(self, code: str) -> dict:
        """Eastmoney basic stock info (industry, shares, mcap)."""
        mc = _market_code(code)
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "fltt": "2", "invt": "2",
            "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
            "secid": f"{mc}.{code}",
        }
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        d = r.json().get("data", {})
        return {
            "code": d.get("f57", ""),
            "name": d.get("f58", ""),
            "industry": d.get("f127", ""),
            "total_shares": d.get("f84", 0),
            "float_shares": d.get("f85", 0),
            "mcap": d.get("f116", 0),
            "float_mcap": d.get("f117", 0),
            "list_date": str(d.get("f189", "")),
            "price": d.get("f43", 0),
        }

    def _sina_financial_report(self, code: str, report_type: str) -> list[dict]:
        """Sina financial report (balance sheet / income / cashflow)."""
        prefix = _get_prefix(code)
        paper_code = f"{prefix}{code}"
        url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
        params = {
            "paperCode": paper_code, "source": report_type,
            "type": "0", "page": "1", "num": "20",
        }
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        d = r.json()
        result = d.get("result", {}).get("data", {})
        items = result.get(report_type, [])
        return items if isinstance(items, list) else []

    def _eastmoney_stock_news(self, code: str, page_size: int = 20) -> list[dict]:
        """Eastmoney stock news (JSONP)."""
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = json.dumps({
            "uid": "", "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {
                "searchScope": "default", "sort": "default",
                "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": "",
            }},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": UA, "Referer": "https://so.eastmoney.com/"}
        r = requests.get(url, params=params, headers=headers, timeout=15)

        text = r.text
        json_str = text[text.index("(") + 1 : text.rindex(")")]
        d = json.loads(json_str)

        rows = []
        articles = d.get("result", {}).get("cmsArticleWebOld", {}).get("list", [])
        for a in articles:
            rows.append({
                "title": re.sub(r'<[^>]+>', '', a.get("title", "")),
                "content": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                "time": a.get("date", ""),
                "source": a.get("mediaName", ""),
                "url": a.get("url", ""),
            })
        return rows

    def _cls_telegraph(self, page_size: int = 50) -> list[dict]:
        """CLS telegraph (real-time market news)."""
        url = "https://www.cls.cn/nodeapi/telegraphList"
        params = {"rn": str(page_size), "page": "1"}
        headers = {"User-Agent": UA, "Referer": "https://www.cls.cn/"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        d = r.json()

        rows = []
        for item in d.get("data", {}).get("roll_data", []):
            rows.append({
                "title": item.get("title", "") or item.get("brief", ""),
                "content": item.get("content", "") or item.get("brief", ""),
                "time": item.get("ctime", ""),
            })
        return rows

    def _eastmoney_global_news(self, page_size: int = 50) -> list[dict]:
        """Eastmoney global financial news (7x24)."""
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724",
            "fastColumn": "102", "sortEnd": "",
            "pageSize": str(page_size),
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        d = r.json()

        rows = []
        for item in d.get("data", {}).get("fastNewsList", []):
            rows.append({
                "title": item.get("title", ""),
                "summary": item.get("summary", "")[:200],
                "time": item.get("showTime", ""),
            })
        return rows

    def _holder_num_change_raw(self, code: str, page_size: int = 10) -> list[dict]:
        """Holder count change (quarterly chip concentration)."""
        data = eastmoney_datacenter(
            "RPT_HOLDERNUMLATEST",
            filter_str=f'(SECURITY_CODE="{code}")',
            page_size=page_size,
            sort_columns="END_DATE", sort_types="-1",
        )
        rows = []
        for row in data:
            rows.append({
                "date": str(row.get("END_DATE", ""))[:10],
                "holder_num": row.get("HOLDER_NUM", 0),
                "change_num": row.get("HOLDER_NUM_CHANGE", 0),
                "change_ratio": row.get("HOLDER_NUM_RATIO", 0),
                "avg_shares": row.get("AVG_FREE_SHARES", 0),
            })
        return rows

    def _margin_trading_raw(self, code: str, page_size: int = 30) -> list[dict]:
        """Margin trading detail (daily)."""
        data = eastmoney_datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            filter_str=f'(SCODE="{code}")',
            page_size=page_size,
            sort_columns="DATE", sort_types="-1",
        )
        rows = []
        for row in data:
            rows.append({
                "date": str(row.get("DATE", ""))[:10],
                "rzye": row.get("RZYE", 0),
                "rzmre": row.get("RZMRE", 0),
                "rzche": row.get("RZCHE", 0),
                "rqye": row.get("RQYE", 0),
                "rzrqye": row.get("RZRQYE", 0),
            })
        return rows

    def _eastmoney_fund_flow_minute_raw(self, code: str) -> list[dict]:
        """Minute-level fund flow from Eastmoney push2."""
        secid = f"{_market_code(code)}.{code}"
        url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params = {
            "secid": secid, "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        headers = {
            "User-Agent": UA,
            "Referer": "https://quote.eastmoney.com/",
            "Origin": "https://quote.eastmoney.com",
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        d = r.json()
        rows = []
        for line in d.get("data", {}).get("klines", []):
            parts = line.split(",")
            if len(parts) >= 6:
                rows.append({
                    "time": parts[0],
                    "main_net": float(parts[1]),
                    "small_net": float(parts[2]),
                    "mid_net": float(parts[3]),
                    "large_net": float(parts[4]),
                    "super_net": float(parts[5]),
                })
        return rows

    def _stock_fund_flow_120d_raw(self, code: str) -> list[dict]:
        """120-day daily fund flow from Eastmoney push2his."""
        mc = _market_code(code)
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        params = {
            "secid": f"{mc}.{code}",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "lmt": "120",
        }
        headers = {
            "User-Agent": UA,
            "Referer": "https://quote.eastmoney.com/",
            "Origin": "https://quote.eastmoney.com",
        }
        r = requests.get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        klines = d.get("data", {}).get("klines", [])
        rows = []
        for line in klines:
            parts = line.split(",")
            if len(parts) >= 7:
                rows.append({
                    "date": parts[0],
                    "main_net": float(parts[1]) if parts[1] != "-" else 0,
                    "small_net": float(parts[2]) if parts[2] != "-" else 0,
                    "mid_net": float(parts[3]) if parts[3] != "-" else 0,
                    "large_net": float(parts[4]) if parts[4] != "-" else 0,
                    "super_net": float(parts[5]) if parts[5] != "-" else 0,
                })
        return rows

    def _ths_hot_reason_raw(self, date: str = None) -> pd.DataFrame:
        """THS hot reason — strong stocks with editorial tags."""
        if date is None:
            from datetime import date as _date
            date = _date.today().strftime("%Y-%m-%d")

        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {"User-Agent": UA}
        r = requests.get(url, headers=headers, timeout=10)
        d = r.json()
        if d.get("errocode", 0) != 0:
            return pd.DataFrame()

        rows = d.get("data") or []
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        rename_map = {
            "name": "名称", "code": "代码", "reason": "题材归因",
            "close": "收盘价", "zhangdie": "涨跌额", "zhangfu": "涨幅%",
            "huanshou": "换手率%", "chengjiaoe": "成交额",
            "chengjiaoliang": "成交量", "ddejingliang": "大单净量",
            "market": "市场",
        }
        return df.rename(columns=rename_map)

    def _fetch_quotes_sina(self, code_to_original: dict[str, str]) -> str:
        """Sina fallback for realtime quotes."""
        sina_codes = []
        sina_to_original: dict[str, str] = {}
        for code, original in code_to_original.items():
            prefix = _get_prefix(code)
            sina_code = f"{prefix}{code}"
            sina_codes.append(sina_code)
            sina_to_original[sina_code] = original

        if not sina_codes:
            return json.dumps({})

        try:
            resp = requests.get(
                "https://hq.sinajs.cn/list=" + ",".join(sina_codes),
                headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": UA},
                timeout=5,
            )
            resp.encoding = "gbk"
        except Exception:
            return json.dumps({})

        result: dict[str, dict] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or '="' not in line:
                continue
            try:
                var_part, data_part = line.split('="', 1)
                sina_code = var_part.split("_")[-1]
                fields = data_part.rstrip('";').split(",")
                if len(fields) < 10:
                    continue
                original = sina_to_original.get(sina_code)
                if not original:
                    continue
                price = _safe_float(fields[3])
                prev_close = _safe_float(fields[2])
                change = round(price - prev_close, 4) if price is not None and prev_close else None
                change_pct = round(change / prev_close * 100, 4) if change is not None and prev_close else None
                result[original] = {
                    "price": price,
                    "open": _safe_float(fields[1]),
                    "high": _safe_float(fields[4]),
                    "low": _safe_float(fields[5]),
                    "previous_close": prev_close,
                    "change": change,
                    "change_pct": change_pct,
                    "volume": _safe_float(fields[8]),
                    "amount": _safe_float(fields[9]),
                    "source": "sina",
                }
            except (ValueError, IndexError):
                continue
        return json.dumps(result, ensure_ascii=False)


def dragon_tiger_board(code: str, trade_date: str, look_back: int = 30) -> dict:
    """Dragon tiger board aggregation."""
    start = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)
    start_str = start.strftime("%Y-%m-%d")

    records = []
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{start_str}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
        page_size=50,
        sort_columns="TRADE_DATE", sort_types="-1",
    )
    for row in data:
        records.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "reason": row.get("EXPLANATION", ""),
            "net_buy": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
            "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
        })

    seats = {"buy": [], "sell": []}
    buy_data = []
    sell_data = []
    if records:
        latest_date = records[0]["date"]
        buy_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="BUY", sort_types="-1",
        )
        for row in buy_data[:5]:
            seats["buy"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                "net": round((row.get("NET") or 0) / 10000, 1),
            })
        sell_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="SELL", sort_types="-1",
        )
        for row in sell_data[:5]:
            seats["sell"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                "net": round((row.get("NET") or 0) / 10000, 1),
            })

    institution = {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}
    for detail_data, side in [(buy_data, "buy"), (sell_data, "sell")]:
        for row in detail_data:
            if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                amt = (row.get("BUY") or 0) if side == "buy" else (row.get("SELL") or 0)
                if side == "buy":
                    institution["buy_amt"] += amt
                else:
                    institution["sell_amt"] += amt
    institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
    institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
    institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)

    return {"records": records, "seats": seats, "institution": institution}