"""Lightweight JSON-backed local store for CLI watchlist & scheduled tasks.

Zero SQLAlchemy / FastAPI dependencies. Stores data at
~/.tradingagents/cli_store.json (override with TA_CLI_STORE_DIR).

Uses atomic writes (os.replace) to prevent corruption on crash.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

# ── Storage path ────────────────────────────────────────────────────────

_CLI_STORE_DIR = os.getenv("TA_CLI_STORE_DIR", "")
_DEFAULT_DIR = str(Path.home() / ".tradingagents")

_STORE_LOCK = threading.Lock()


def _store_path() -> str:
    dir_ = _CLI_STORE_DIR or _DEFAULT_DIR
    return os.path.join(dir_, "cli_store.json")


def _ensure_dir() -> None:
    dir_ = _CLI_STORE_DIR or _DEFAULT_DIR
    os.makedirs(dir_, exist_ok=True)


def _load_store() -> Dict:
    """Load the JSON store from disk. Returns {} if missing/corrupt."""
    path = _store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_store(data: Dict) -> None:
    """Atomic write: write to temp file then os.replace."""
    _ensure_dir()
    path = _store_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── Limits ──────────────────────────────────────────────────────────────

MAX_WATCHLIST_ITEMS = 50
MAX_SCHEDULED_ITEMS = 10
VALID_HORIZONS = {"short", "medium"}

# Allowed trigger times: 20:00~23:59 or 00:00~08:00
# (avoid interfering with daytime usage)


def _validate_trigger_time(t: str) -> str:
    parts = t.strip().split(":")
    if len(parts) != 2:
        raise ValueError("时间格式错误，请使用 HH:MM")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("时间格式错误，请使用 HH:MM")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("时间格式错误，请使用 HH:MM")
    time_val = hh * 60 + mm
    if 8 * 60 < time_val < 20 * 60:
        raise ValueError("定时时间仅允许 20:00~次日 08:00（避免影响白天使用）")
    return f"{hh:02d}:{mm:02d}"


def _validate_horizon(horizon: str) -> str:
    if horizon not in VALID_HORIZONS:
        raise ValueError("horizon 必须为 short 或 medium")
    return horizon


# ── Watchlist ───────────────────────────────────────────────────────────


def list_watchlist() -> List[dict]:
    """List all watchlist items."""
    with _STORE_LOCK:
        data = _load_store()
    items = data.get("watchlist", [])
    # Attach scheduled status
    scheduled_symbols = {
        s["symbol"] for s in data.get("scheduled", []) if s.get("is_active", True)
    }
    return [
        {
            "id": item["id"],
            "symbol": item["symbol"],
            "created_at": item.get("created_at"),
            "has_scheduled": item["symbol"] in scheduled_symbols,
        }
        for item in items
    ]


def add_watchlist_item(symbol: str) -> dict:
    """Add a stock to watchlist."""
    with _STORE_LOCK:
        data = _load_store()
        items = data.setdefault("watchlist", [])

        if len(items) >= MAX_WATCHLIST_ITEMS:
            raise ValueError(f"自选股数量已达上限 ({MAX_WATCHLIST_ITEMS})")

        if any(it["symbol"] == symbol for it in items):
            raise ValueError(f"{symbol} 已在自选列表中")

        new_item = {
            "id": uuid4().hex,
            "symbol": symbol,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        items.append(new_item)
        _save_store(data)

    return {
        "id": new_item["id"],
        "symbol": new_item["symbol"],
        "created_at": new_item["created_at"],
    }


def add_watchlist_items(symbols: List[str]) -> List[dict]:
    """Add multiple stocks to watchlist. Returns per-item results."""
    results: List[dict] = []
    for symbol in symbols:
        try:
            item = add_watchlist_item(symbol)
            results.append({
                "symbol": symbol,
                "status": "added",
                "item": item,
                "message": "已添加到自选列表",
            })
        except ValueError as exc:
            message = str(exc)
            status = "duplicate" if "已在自选列表" in message else "failed"
            results.append({
                "symbol": symbol,
                "status": status,
                "message": message,
            })
    return results


def delete_watchlist_item(item_id: str) -> bool:
    """Delete a watchlist item. Returns True if found."""
    with _STORE_LOCK:
        data = _load_store()
        items = data.get("watchlist", [])
        new_items = [it for it in items if it["id"] != item_id]
        if len(new_items) == len(items):
            return False
        data["watchlist"] = new_items
        _save_store(data)
    return True


# ── Scheduled ───────────────────────────────────────────────────────────


def list_scheduled() -> List[dict]:
    """List all scheduled analysis tasks."""
    with _STORE_LOCK:
        data = _load_store()
    items = data.get("scheduled", [])
    return [
        {
            "id": item["id"],
            "symbol": item["symbol"],
            "horizon": item.get("horizon", "short"),
            "trigger_time": item.get("trigger_time", "20:00"),
            "is_active": item.get("is_active", True),
            "last_run_date": item.get("last_run_date"),
            "last_run_status": item.get("last_run_status"),
            "last_report_id": item.get("last_report_id"),
            "consecutive_failures": item.get("consecutive_failures", 0),
            "created_at": item.get("created_at"),
        }
        for item in items
    ]


def create_scheduled(
    symbol: str,
    horizon: str = "short",
    trigger_time: str = "20:00",
) -> dict:
    """Create a scheduled analysis task."""
    horizon = _validate_horizon(horizon)
    trigger_time = _validate_trigger_time(trigger_time)

    with _STORE_LOCK:
        data = _load_store()
        items = data.setdefault("scheduled", [])

        if len(items) >= MAX_SCHEDULED_ITEMS:
            raise ValueError(f"定时分析数量已达上限 ({MAX_SCHEDULED_ITEMS})")

        if any(it["symbol"] == symbol for it in items):
            raise ValueError(f"{symbol} 已有定时分析任务")

        new_item = {
            "id": uuid4().hex,
            "symbol": symbol,
            "horizon": horizon,
            "trigger_time": trigger_time,
            "is_active": True,
            "last_run_date": None,
            "last_run_status": None,
            "last_report_id": None,
            "consecutive_failures": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        items.append(new_item)
        _save_store(data)

    return {
        "id": new_item["id"],
        "symbol": new_item["symbol"],
        "horizon": new_item["horizon"],
        "trigger_time": new_item["trigger_time"],
        "is_active": True,
        "created_at": new_item["created_at"],
    }


def delete_scheduled(item_id: str) -> bool:
    """Delete a scheduled task. Returns True if found."""
    with _STORE_LOCK:
        data = _load_store()
        items = data.get("scheduled", [])
        new_items = [it for it in items if it["id"] != item_id]
        if len(new_items) == len(items):
            return False
        data["scheduled"] = new_items
        _save_store(data)
    return True