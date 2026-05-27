"""Pure stock-symbol utilities — no HTTP/DB dependencies.

Provides A-share stock name→code mapping, symbol normalization,
and name-based search. Only requires akshare for the name map.
"""

import logging
import os
import re
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Module-level cache ──────────────────────────────────────────────────

_cn_stock_map: Optional[Dict[str, str]] = None          # name → "XXXXXX.SH/SZ"
_cn_stock_reverse_map: Optional[Dict[str, str]] = None  # code → name
_cn_stock_map_lock = threading.Lock()
_cn_stock_map_loaded_at: float = 0                      # timestamp of last load
_STOCK_MAP_TTL = 7 * 86400                              # 7 days


def normalize_symbol(raw: str) -> str:
    """Normalize a stock code or name to standard format.

    Handles:
    - 6-digit CN codes → adds .SH/.SZ suffix
    - Codes with existing .SS suffix → converts to .SH
    - US tickers (1-6 letter codes) → returns unchanged
    - Chinese company names → resolves via stock map

    Examples:
        "600519" → "600519.SH"
        "000001.SZ" → "000001.SZ"
        "600519.SS" → "600519.SH"
        "AAPL" → "AAPL"
    """
    s = raw.strip().upper()
    # Priority: 6-digit CN stock code
    m = re.search(r"(\d{6})(?:\.(SH|SZ|SS|BJ))?", s)
    if m:
        code = m.group(1)
        suffix = m.group(2)
        if suffix:
            if suffix == "SS":
                return f"{code}.SH"
            return f"{code}.{suffix}"
        market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        return f"{code}.{market}"
    # Fallback: 1-6 letter ticker (US/HK)
    m2 = re.search(r"([A-Z]{1,6}(?:\.[A-Z]{1,3})?)", s)
    if m2:
        return m2.group(1)

    # Final Fallback: Chinese Name Map (e.g. "三花智控" → "002050.SZ")
    stock_map = load_cn_stock_map()
    if s in stock_map:
        return stock_map[s]

    return s


def load_cn_stock_map() -> Dict[str, str]:
    """Lazy-load and cache A-share stock + ETF/fund name→code mapping (7-day TTL).

    Uses akshare stock_info_a_code_name (static list, no anti-crawl) for A-shares,
    plus fund_name_em for ETFs/funds.
    """
    global _cn_stock_map, _cn_stock_reverse_map, _cn_stock_map_loaded_at
    now = time.time()
    if _cn_stock_map is not None and (now - _cn_stock_map_loaded_at) > _STOCK_MAP_TTL:
        _cn_stock_map = None      # expire cache
        _cn_stock_reverse_map = None
    if _cn_stock_map is not None:
        return _cn_stock_map
    with _cn_stock_map_lock:
        if _cn_stock_map is not None and (now - _cn_stock_map_loaded_at) <= _STOCK_MAP_TTL:
            return _cn_stock_map
        result: Dict[str, str] = {}
        try:
            import akshare as ak
            # A-share stocks (static list, no anti-crawl issue)
            df = ak.stock_info_a_code_name()
            for _, row in df.iterrows():
                name = str(row.get("name", "")).strip()
                code = str(row.get("code", "")).strip()
                if name and code:
                    result[name] = normalize_symbol(code)
            stock_count = len(result)
            # ETF / funds
            fund_count = 0
            try:
                fund_df = ak.fund_name_em()
                existing_codes = set(result.values())
                for _, row in fund_df.iterrows():
                    code = str(row.get("基金代码", "")).strip()
                    name = str(row.get("基金简称", "")).strip()
                    if name and code and len(code) == 6 and code.isdigit():
                        normalized = normalize_symbol(code)
                        if normalized not in existing_codes:
                            result[name] = normalized
                            existing_codes.add(normalized)
                fund_count = len(result) - stock_count
            except Exception as fe:
                logger.info("[StockMap] ETF/fund load skipped: %s", fe)
            _cn_stock_map = result
            _cn_stock_reverse_map = {code: name for name, code in result.items()}
            _cn_stock_map_loaded_at = now
            logger.info("[StockMap] Loaded %s stocks + %s ETFs/funds = %s total.",
                        stock_count, fund_count, len(result))
        except Exception as e:
            logger.info("[StockMap] Failed to load: %s", e)
            if _cn_stock_map is None:
                _cn_stock_map = {}
                _cn_stock_reverse_map = {}
    return _cn_stock_map


def get_reverse_stock_map() -> Dict[str, str]:
    """Return code→name mapping (loads the map if not cached)."""
    load_cn_stock_map()
    return dict(_cn_stock_reverse_map or {})


def get_reverse_stock_map_cached_only() -> Dict[str, str]:
    """Return code→name mapping only from already-warmed cache.

    When the cache is cold we simply return an empty mapping.
    """
    if _cn_stock_map is None or _cn_stock_reverse_map is None:
        return {}
    return dict(_cn_stock_reverse_map)


def search_cn_stock_by_name(query: str) -> Optional[str]:
    """Look up A-share stock code by company name (exact then partial match)."""
    query = query.strip()
    if not query:
        return None
    stock_map = load_cn_stock_map()
    # 1. Exact match
    if query in stock_map:
        return stock_map[query]
    # 2. Partial match: query is substring of a stock name or vice versa
    candidates = [(name, code) for name, code in stock_map.items()
                  if query in name or name in query]
    if len(candidates) == 1:
        return candidates[0][1]
    # 3. If multiple partial matches, pick the one with shortest name
    if candidates:
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0][1]
    return None