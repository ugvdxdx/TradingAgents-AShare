"""Tests for cli.local_store — JSON-backed local storage for watchlist/scheduled."""

import json
import os
import tempfile
from pathlib import Path

from cli.local_store import (
    list_watchlist,
    add_watchlist_item,
    add_watchlist_items,
    delete_watchlist_item,
    list_scheduled,
    create_scheduled,
    delete_scheduled,
    _validate_trigger_time,
    _validate_horizon,
    MAX_WATCHLIST_ITEMS,
    MAX_SCHEDULED_ITEMS,
)


class TestValidateTriggerTime:
    def test_valid_evening(self):
        assert _validate_trigger_time("20:00") == "20:00"

    def test_valid_early_morning(self):
        assert _validate_trigger_time("07:30") == "07:30"

    def test_valid_midnight(self):
        assert _validate_trigger_time("00:00") == "00:00"

    def test_valid_late_night(self):
        assert _validate_trigger_time("23:59") == "23:59"

    def test_invalid_daytime(self):
        import pytest
        with pytest.raises(ValueError, match="定时时间仅允许"):
            _validate_trigger_time("10:00")

    def test_invalid_boundary_08_01(self):
        import pytest
        with pytest.raises(ValueError):
            _validate_trigger_time("08:01")

    def test_invalid_boundary_19_59(self):
        import pytest
        with pytest.raises(ValueError):
            _validate_trigger_time("19:59")

    def test_invalid_format(self):
        import pytest
        with pytest.raises(ValueError, match="时间格式错误"):
            _validate_trigger_time("25:00")


class TestValidateHorizon:
    def test_short(self):
        assert _validate_horizon("short") == "short"

    def test_medium(self):
        assert _validate_horizon("medium") == "medium"

    def test_invalid(self):
        import pytest
        with pytest.raises(ValueError, match="horizon"):
            _validate_horizon("long")


class TestWatchlist:
    def test_add_and_list(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        result = add_watchlist_item("600519.SH")
        assert result["symbol"] == "600519.SH"
        assert "id" in result

        items = list_watchlist()
        assert len(items) == 1
        assert items[0]["symbol"] == "600519.SH"

    def test_add_duplicate_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        add_watchlist_item("600519.SH")
        import pytest
        with pytest.raises(ValueError, match="已在自选列表中"):
            add_watchlist_item("600519.SH")

    def test_add_multiple(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        results = add_watchlist_items(["600519.SH", "000001.SZ"])
        added = [r for r in results if r["status"] == "added"]
        assert len(added) == 2

    def test_delete(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        result = add_watchlist_item("600519.SH")
        assert delete_watchlist_item(result["id"]) is True
        assert list_watchlist() == []

    def test_delete_nonexistent(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        assert delete_watchlist_item("fake-id") is False

    def test_max_limit(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        import pytest
        for i in range(MAX_WATCHLIST_ITEMS):
            add_watchlist_item(f"{i:06d}.SH")
        with pytest.raises(ValueError, match="已达上限"):
            add_watchlist_item("999999.SH")

    def test_atomic_write_no_corruption(self, monkeypatch, tmp_path):
        """Verify the JSON file is valid after write."""
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        add_watchlist_item("600519.SH")
        store_file = tmp_path / "cli_store.json"
        with open(store_file) as f:
            data = json.load(f)
        assert "watchlist" in data
        assert len(data["watchlist"]) == 1


class TestScheduled:
    def test_create_and_list(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        result = create_scheduled("600519.SH", "short", "20:00")
        assert result["symbol"] == "600519.SH"
        assert result["trigger_time"] == "20:00"

        items = list_scheduled()
        assert len(items) == 1
        assert items[0]["symbol"] == "600519.SH"

    def test_create_duplicate_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        create_scheduled("600519.SH", "short", "20:00")
        import pytest
        with pytest.raises(ValueError, match="已有定时分析任务"):
            create_scheduled("600519.SH", "medium", "21:00")

    def test_delete(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        result = create_scheduled("600519.SH", "short", "20:00")
        assert delete_scheduled(result["id"]) is True
        assert list_scheduled() == []

    def test_delete_nonexistent(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        assert delete_scheduled("fake-id") is False

    def test_max_limit(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        import pytest
        for i in range(MAX_SCHEDULED_ITEMS):
            create_scheduled(f"{i:06d}.SH", "short", "20:00")
        with pytest.raises(ValueError, match="已达上限"):
            create_scheduled("999999.SH", "short", "20:00")

    def test_has_scheduled_flag_in_watchlist(self, monkeypatch, tmp_path):
        monkeypatch.setattr("cli.local_store._CLI_STORE_DIR", str(tmp_path))
        add_watchlist_item("600519.SH")
        create_scheduled("600519.SH", "short", "20:00")
        items = list_watchlist()
        assert items[0]["has_scheduled"] is True