"""联网搜索公共工具 (智谱 MCP web_search_prime)。

下沉自 refresh_fundamentals。走 GLM Coding Plan 的 Remote MCP Server, 复用 TA_API_KEY,
额度计入 coding plan 套餐 (Pro 1000次/月, Max 4000次/月), 不走易限流的
web-search-pro 独立资源包。协议为 MCP streamable HTTP (有状态会话)。

带 429 限速退避 (最长等 120s) + 全局令牌桶 (事前限流平滑爆发)。
网络故障抛 RuntimeError (不静默降级, 不用降级数据偷偷生成); 只有"连接成功但无结果"才返回空。
"""
import os
import json
import re
import threading
import time
import urllib.request
import urllib.error
from typing import Optional

# 智谱 MCP web_search_prime 端点 (Remote MCP Server)
_MCP_SEARCH_URL = "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"


def _is_rate_limited(e: Exception) -> bool:
    """判断异常是否为 429 速率限制。

    覆盖: urllib HTTPError(code=429) / openai RateLimitError / 智谱 code 1302。
    """
    if getattr(e, "code", None) == 429:
        return True
    if type(e).__name__ == "RateLimitError":
        return True
    s = str(e)
    return "429" in s or "速率限制" in s or "1302" in s


def _rate_limit_wait(attempt: int) -> int:
    """429 退避秒数: 30→60→90→120 (封顶120)。"""
    return min(30 * (attempt + 1), 120)


class _TokenBucket:
    """全局令牌桶: 连续两个请求至少间隔 min_interval 秒, 串行化请求发起、平滑爆发。

    主动速率控制 (事前限流), 配合 _is_rate_limited 的 429 退避 (事后兜底)。
    持锁 sleep 确保严格间隔 —— 多线程同时请求时排队, 每隔 min_interval 放行一个。
    min_interval 由环境变量 RATE_LIMIT_INTERVAL 配置 (秒, 默认0.5≈2请求/秒)。
    """
    def __init__(self, min_interval: float = 0.5):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()


# 智谱 API 全局速率限制器 (跨 _web_search 共享, 一个 key 一个桶)
_ZHIPU_LIMITER = _TokenBucket(float(os.environ.get("RATE_LIMIT_INTERVAL", "0.5")))


def _mcp_parse_sse(raw: str) -> dict:
    """从 MCP streamable HTTP 的 SSE 响应里提取 JSON-RPC payload。

    响应形如:
        id:1
        event:message
        data:{"jsonrpc":"2.0",...}
    取最后一个 data: 行解析为 dict (流式可能多段, 末段为终态)。
    """
    matches = re.findall(r'data:(\{.*\})', raw, re.DOTALL)
    if not matches:
        return {}
    return json.loads(matches[-1])


# MCP 会话 (进程级复用, 避免每次搜索都握手)
# 持有 session_id; 失效/出错时置 None 触发下次重建。
class _McpSession:
    _id: Optional[str] = None
    _lock = threading.Lock()


def _mcp_init() -> str:
    """MCP initialize 握手, 返回 session_id (带缓存)。线程安全。"""
    with _McpSession._lock:
        if _McpSession._id:
            return _McpSession._id
        api_key = os.environ.get("TA_API_KEY", "")
        if not api_key:
            raise RuntimeError("Web Search 需要 TA_API_KEY (智谱)")
        # 1) initialize
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "jt-research", "version": "1.0"}},
        }).encode("utf-8")
        req = urllib.request.Request(_MCP_SEARCH_URL, data=payload, headers={
            "Authorization": api_key, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            _McpSession._id = resp.headers.get("Mcp-Session-Id", "")
        if not _McpSession._id:
            raise RuntimeError("MCP initialize 未返回 Mcp-Session-Id")
        # 2) notifications/initialized (完成握手)
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8")
        req2 = urllib.request.Request(_MCP_SEARCH_URL, data=notif, headers={
            "Authorization": api_key, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": _McpSession._id,
        })
        try:
            urllib.request.urlopen(req2, timeout=15).read()
        except Exception:
            pass  # notification 无响应体, 忽略
        return _McpSession._id


def _mcp_call(name: str, arguments: dict) -> dict:
    """MCP tools/call, 自动初始化会话; 会话失效(401/-32603等)时重建重试一次。"""
    api_key = os.environ.get("TA_API_KEY", "")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode("utf-8")

    for _ in range(2):  # 首次 + 会话失效重建一次
        session_id = _mcp_init()
        req = urllib.request.Request(_MCP_SEARCH_URL, data=payload, headers={
            "Authorization": api_key, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": session_id,
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        j = _mcp_parse_sse(raw)
        result = j.get("result") if isinstance(j, dict) else None
        if isinstance(result, dict):
            # 会话级错误 (如 -401 Api key not found / -32603) → 会话失效, 重建重试
            if result.get("isError"):
                # 任何 isError 都先重建会话再试一次 (可能是 session 过期)
                with _McpSession._lock:
                    _McpSession._id = None
                continue
            return result
        # 非预期结构 → 当作失败
        break
    return {}


def _web_search(query: str, num_results: int = 5) -> str:
    """智谱 MCP web_search_prime 联网搜索，返回结果摘要文本。

    走 GLM Coding Plan 的 Remote MCP Server (web_search_prime), 复用 TA_API_KEY,
    额度计入 coding plan 套餐 (Pro 1000次/月, Max 4000次/月), 不走易限流的
    web-search-pro 独立资源包。429 限速自动退避重试 (最长等 120s)。
    网络故障必须报错暴露 (不静默跳过、不用降级数据偷偷生成)。
    只有"连接成功但无结果"才视为正常返回空。
    """
    api_key = os.environ.get("TA_API_KEY", "")
    if not api_key:
        raise RuntimeError("Web Search 需要 TA_API_KEY (智谱)")

    _ZHIPU_LIMITER.acquire()  # 主动速率控制 (事前限流, 平滑爆发)
    last_err = None
    for attempt in range(6):  # 429 退避最多 6 次
        try:
            result = _mcp_call("web_search_prime", {
                "search_query": query[:200],  # 官方建议 ≤70字符, 这里放宽防截断
                "location": "cn",
                "content_size": "high",      # 最大化上下文 (~2500字/条)
            })
            # result.content[0].text 是搜索结果, content_size=high 时为双重编码的
            # JSON 字符串数组: "[{\"title\":...,\"content\":...}]" (外层引号+内层转义),
            # 需 json.loads 两次才得到 list[dict]。
            results = []
            for c in result.get("content", []):
                if c.get("type") != "text":
                    continue
                items = c.get("text", "")
                # 解码至多为 list/dict (high 模式双重编码, medium 一般单层)
                for _ in range(2):
                    if isinstance(items, str):
                        try:
                            items = json.loads(items)
                        except (json.JSONDecodeError, TypeError):
                            break  # 不是 JSON → 当纯文本处理
                if isinstance(items, str):
                    # 非结构化纯文本: 当一条兜底
                    items = [{"title": "", "content": items}] if items.strip() else []
                if not isinstance(items, list):
                    items = [items] if isinstance(items, dict) else []
                for r in items:
                    if not isinstance(r, dict):
                        continue
                    t = (r.get("title", "") or "").strip()
                    content = (r.get("content", "") or "").strip()
                    if t or content:
                        results.append(f"[{t}] {content}")
                    if len(results) >= num_results:
                        break
                if len(results) >= num_results:
                    break
            if not results:
                return ""  # 连上了但无结果 → 正常
            return "\n".join(results)[:3000]
        except Exception as e:
            last_err = e
            if _is_rate_limited(e):
                wait = _rate_limit_wait(attempt)
                print(f"  [WebSearch] 429 限速, 等待{wait}s 后重试 ({attempt+1}/6)", flush=True)
                time.sleep(wait)
                # 会话也可能因限流失效, 清掉下次重建
                with _McpSession._lock:
                    _McpSession._id = None
                continue
            # 非限速瞬时错误: 短重试 1 次
            if attempt < 1:
                with _McpSession._lock:
                    _McpSession._id = None
                time.sleep(1)
                continue
            break
    # 重试仍失败 → 报错，不返回空
    raise RuntimeError(
        f"Web Search 失败 (智谱 MCP web_search_prime): {type(last_err).__name__}: {last_err}"
    ) from last_err
