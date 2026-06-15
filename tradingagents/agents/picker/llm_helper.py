"""debate_picker v5 — LLM 辅助层 (M2)。

提供 deep/quick 双模型、重试退避、tagged-JSON 解析。
借鉴 sector_graph._stream_llm 与 debate_utils.extract_tagged_json。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage, SystemMessage

# 确保独立运行(回测/CLI)时也能读到 .env
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass


class LLMHelper:
    """封装 deep/quick 两个 LLM, 带重试与解析。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        # 注意: 走 OpenAI 兼容直连。.env 中 TA_BASE_URL 为 OpenAI 兼容端点,
        # 模型走 deepseek, 故 provider 固定 openai (TA_LLM_PROVIDER=anthropic 是给主框架用的)。
        self._provider = config.get("llm_provider") or "openai"
        self._backend = config.get("backend_url") or os.getenv("TA_BASE_URL")
        self._api_key = config.get("api_key") or os.getenv("TA_API_KEY", "")
        self._deep_model = config.get("deep_think_llm") or os.getenv("TA_LLM_DEEP") or "gpt-4o"
        self._quick_model = config.get("quick_think_llm") or os.getenv("TA_LLM_QUICK") or "gpt-4o-mini"
        self._deep = None
        self._quick = None

    def _make(self, model: str, temperature: float):
        from tradingagents.llm_clients import create_llm_client
        client = create_llm_client(
            provider=self._provider, model=model,
            base_url=self._backend, api_key=self._api_key, temperature=temperature,
        )
        return client.get_llm()

    @property
    def deep(self):
        if self._deep is None:
            self._deep = self._make(self._deep_model, 0.7)
        return self._deep

    @property
    def quick(self):
        if self._quick is None:
            self._quick = self._make(self._quick_model, 0.5)
        return self._quick

    def call(self, system_msg: str, human_msg: str, *, deep: bool = True,
             max_retries: int = 3, max_chars: Optional[int] = None) -> str:
        """调用 LLM (流式累积), 失败指数退避重试。返回完整文本。"""
        llm = self.deep if deep else self.quick
        messages = [SystemMessage(content=system_msg), HumanMessage(content=human_msg)]
        for attempt in range(max_retries):
            try:
                content = ""
                for chunk in llm.stream(messages):
                    piece = chunk.content if hasattr(chunk, "content") else str(chunk)
                    content += piece
                    if max_chars and len(content) >= max_chars:
                        return content[:max_chars]
                if content.strip():
                    return content
            except Exception as e:
                wait = 2 ** attempt * 5
                print(f"  [LLM] 调用失败(尝试 {attempt+1}/{max_retries}): {type(e).__name__}: {e}")
                if attempt < max_retries - 1:
                    print(f"  [LLM] {wait}s 后重试...")
                    time.sleep(wait)
        return ""


# ── 解析工具 ──

def extract_json_array(text: str) -> list:
    """从 LLM 输出提取 JSON 数组 (容忍 ```json 包裹)。"""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(m.group()))
        except Exception:
            return []


def extract_tagged_json(text: str, tag: str) -> Dict[str, Any]:
    """提取 <!-- TAG: {...} --> 机读块。"""
    pattern = rf"<!--\s*{re.escape(tag)}:\s*(\{{.*?\}})\s*-->"
    m = re.search(pattern, text or "", flags=re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(m.group(1)))
        except Exception:
            return {}
