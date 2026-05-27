"""Tests for tradingagents.stock_utils — pure stock-symbol utilities."""

from tradingagents.stock_utils import normalize_symbol, search_cn_stock_by_name, load_cn_stock_map


class TestNormalizeSymbol:
    def test_six_digit_sh_code(self):
        assert normalize_symbol("600519") == "600519.SH"

    def test_six_digit_sz_code(self):
        assert normalize_symbol("000001") == "000001.SZ"

    def test_six_digit_sz_code_3_prefix(self):
        assert normalize_symbol("300750") == "300750.SZ"

    def test_existing_sh_suffix(self):
        assert normalize_symbol("600519.SH") == "600519.SH"

    def test_existing_sz_suffix(self):
        assert normalize_symbol("000001.SZ") == "000001.SZ"

    def test_ss_suffix_converted_to_sh(self):
        assert normalize_symbol("600519.SS") == "600519.SH"

    def test_bj_suffix(self):
        assert normalize_symbol("430047.BJ") == "430047.BJ"

    def test_us_ticker(self):
        assert normalize_symbol("AAPL") == "AAPL"

    def test_us_ticker_with_exchange(self):
        assert normalize_symbol("BRK.B") == "BRK.B"

    def test_whitespace_trimmed(self):
        assert normalize_symbol("  600519  ") == "600519.SH"

    def test_mixed_case(self):
        assert normalize_symbol("aapl") == "AAPL"

    def test_5_prefix_sh(self):
        assert normalize_symbol("510050") == "510050.SH"

    def test_9_prefix_sh(self):
        assert normalize_symbol("900901") == "900901.SH"


class TestSearchCnStockByName:
    def test_exact_match(self, monkeypatch):
        """With a mock stock map, exact name match returns code."""
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map",
            {"贵州茅台": "600519.SH"},
        )
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map_loaded_at",
            9999999999.0,
        )
        result = search_cn_stock_by_name("贵州茅台")
        assert result == "600519.SH"

    def test_partial_match_single(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map",
            {"贵州茅台": "600519.SH"},
        )
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map_loaded_at",
            9999999999.0,
        )
        result = search_cn_stock_by_name("茅台")
        assert result == "600519.SH"

    def test_partial_match_multiple_shortest_name(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map",
            {"贵州茅台": "600519.SH", "茅台集团": "600519.SH"},
        )
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map_loaded_at",
            9999999999.0,
        )
        result = search_cn_stock_by_name("茅台")
        # "贵州茅台" (4 chars) is shorter than "茅台集团" (4 chars), same length → either
        assert result == "600519.SH"

    def test_empty_query(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map",
            {"贵州茅台": "600519.SH"},
        )
        result = search_cn_stock_by_name("")
        assert result is None

    def test_no_match(self, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map",
            {"贵州茅台": "600519.SH"},
        )
        monkeypatch.setattr(
            "tradingagents.stock_utils._cn_stock_map_loaded_at",
            9999999999.0,
        )
        result = search_cn_stock_by_name("不存在公司")
        assert result is None


class TestLoadCnStockMap:
    def test_cached_map_returned(self, monkeypatch):
        """When cache is warm, returns it without hitting akshare."""
        fake_map = {"贵州茅台": "600519.SH"}
        monkeypatch.setattr("tradingagents.stock_utils._cn_stock_map", fake_map)
        monkeypatch.setattr("tradingagents.stock_utils._cn_stock_map_loaded_at", 9999999999.0)
        result = load_cn_stock_map()
        assert result == fake_map