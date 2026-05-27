"""API smoke tests using FastAPI TestClient (no external server needed).

Covers:
1. AnalyzeRequest schema — query field exists, symbol optional
2. /v1/analyze dry_run — short-circuits before LLM
3. /v1/chat/completions — unrecognizable stock returns 400
4. /v1/jobs/{id}/result — completed job returns result
"""
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.database import ImportedPortfolioPositionDB, DEFAULT_USER_ID, get_db_ctx


# ---------------------------------------------------------------------------
# Schema-only test (no server needed)
# ---------------------------------------------------------------------------

class TestAnalyzeRequestSchema:
    def test_query_field_exists_and_optional(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest(symbol="600519.SH")
        assert req.query is None

    def test_query_field_accepts_string(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest(symbol="600519.SH", query="分析贵州茅台短线机会")
        assert req.query == "分析贵州茅台短线机会"

    def test_symbol_is_optional(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest()
        assert req.symbol == ""

    def test_dry_run_defaults_false(self):
        from api.main import AnalyzeRequest
        req = AnalyzeRequest(symbol="600519.SH")
        assert req.dry_run is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client():
    """Create a TestClient for the FastAPI app."""
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _wait_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    """Poll until job is no longer running, return result dict."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/v1/jobs/{job_id}")
        status = r.json().get("status")
        if status in ("completed", "failed"):
            break
        time.sleep(0.2)
    r2 = client.get(f"/v1/jobs/{job_id}/result")
    return r2.json()


# ---------------------------------------------------------------------------
# API integration tests (single-user mode, no auth required)
# ---------------------------------------------------------------------------

class TestAnalyzeEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()

    def test_dry_run_completes(self):
        """Legacy path: symbol + dry_run → completed immediately."""
        r = self.client.post("/v1/analyze", json={
            "symbol": "600519.SH",
            "trade_date": "2024-01-15",
            "dry_run": True,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, job_id)
        assert result["status"] == "completed"
        assert result["decision"] == "DRY_RUN"
        assert result["result"]["symbol"] == "600519.SH"

    def test_query_field_accepted_with_dry_run(self):
        """query field is accepted by schema; dry_run still short-circuits."""
        r = self.client.post("/v1/analyze", json={
            "symbol": "600519.SH",
            "trade_date": "2024-01-15",
            "query": "分析贵州茅台短线机会",
            "dry_run": True,
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, job_id)
        assert result["status"] == "completed"
        assert result["decision"] == "DRY_RUN"

    def test_missing_symbol_accepted_by_schema(self):
        r = self.client.post("/v1/analyze", json={
            "trade_date": "2024-01-15",
            "dry_run": True,
        })
        assert r.status_code == 200
        assert "job_id" in r.json()

    def test_selected_analysts_field(self):
        r = self.client.post("/v1/analyze", json={
            "symbol": "600519.SH",
            "selected_analysts": ["market", "news"],
            "dry_run": True,
        })
        job_id = r.json()["job_id"]
        result = _wait_job(self.client, job_id)
        assert result["result"]["selected_analysts"] == ["market", "news"]


class TestChatCompletionsEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()

    def test_unrecognizable_stock_returns_error(self):
        with patch("api.main._ai_extract_symbol_and_date", return_value=(None, None, ["short"], [], [], {})):
            r = self.client.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "今天天气真好"}],
                "stream": False,
                "dry_run": True,
            })
        assert r.status_code == 400

    def test_valid_stock_dry_run_creates_job(self):
        with patch("api.main._ai_extract_symbol_and_date", return_value=("600519.SH", "2024-01-15", ["short"], [], [], {})):
            r = self.client.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "分析600519短线机会"}],
                "stream": False,
                "dry_run": True,
            })
        assert r.status_code == 200
        body = r.json()
        assert "choices" in body


class TestOpenAPISchema:
    def test_analyze_request_has_query_field(self):
        client = _get_client()
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()["components"]["schemas"]["AnalyzeRequest"]
        assert "query" in schema["properties"]

    def test_analyze_request_symbol_not_required(self):
        client = _get_client()
        r = client.get("/openapi.json")
        schema = r.json()["components"]["schemas"]["AnalyzeRequest"]
        assert "symbol" not in schema.get("required", [])

    def test_healthz(self):
        client = _get_client()
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestConfigEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()

    def test_get_config(self):
        r = self.client.get("/v1/config")
        assert r.status_code == 200
        body = r.json()
        assert "llm_provider" in body
        assert "deep_think_llm" in body

    def test_patch_config(self):
        r = self.client.patch("/v1/config", json={
            "max_debate_rounds": 3,
        })
        assert r.status_code == 200
        body = r.json()
        assert "applied" in body

    def test_manual_warmup_returns_model_reply(self):
        with patch("api.main._invoke_runtime_warmup", return_value=[{
            "model": "gpt-test-quick",
            "targets": ["常规模型"],
            "content": "你好，我已准备就绪。",
            "error": None,
        }]) as invoke:
            r = self.client.post("/v1/config/warmup", json={
                "quick_think_llm": "gpt-test-quick",
                "prompt": "你好",
            })

        assert r.status_code == 200
        body = r.json()
        assert body["prompt"] == "你好"
        assert body["results"][0]["content"] == "你好，我已准备就绪。"


class TestWatchlistAddEndpoint:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = _get_client()

    def test_batch_add_supports_codes_and_full_names(self):
        name_to_code = {
            "贵州茅台": "600519.SH",
            "宁德时代": "300750.SZ",
        }
        code_to_name = {value: key for key, value in name_to_code.items()}
        with patch("api.main._load_cn_stock_map", return_value=name_to_code), \
             patch("api.main._get_reverse_stock_map", return_value=code_to_name):
            r = self.client.post("/v1/watchlist", json={
                "text": "600519 宁德时代, 未知标的",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["summary"] == {"total": 3, "added": 2, "duplicate": 0, "failed": 1}
        assert [item["status"] for item in body["results"]] == ["added", "added", "invalid"]