"""LLM 客户端公共工具。

下沉自:
  - v3_full_score._llm     : 评分链路调用, 带 429 限流长退避 + 抖动, 最多 5 次重试
  - scan_mispriced._llm_quick : 归因等轻量任务, 进程级 client 单例, 失败返回空串

配置均从环境变量读取 (TA_API_KEY / TA_BASE_URL / TA_LLM_QUICK / TA_LLM_DEEP)。
env 采用延迟读取 (调用时而非 import 时), 避免 .env 未 load 时拿到 None。
"""
import os
import threading
import time

# _llm 的 per-thread client (评分链路多 worker 并发, 各线程独立 client)
_CLIENT_LOCAL = threading.local()


def _resolve_model() -> str:
    """LLM 模型名: 优先 TA_LLM_QUICK, 回退 TA_LLM_DEEP, 再回退 deepseek-v4-pro。"""
    return (os.environ.get("TA_LLM_QUICK")
            or os.environ.get("TA_LLM_DEEP")
            or "deepseek-v4-pro")


def _client():
    """per-thread OpenAI client (lazy 初始化, 延迟读 env)。"""
    if not hasattr(_CLIENT_LOCAL, "c"):
        from openai import OpenAI
        _CLIENT_LOCAL.c = OpenAI(
            api_key=os.environ.get("TA_API_KEY"),
            base_url=os.environ.get("TA_BASE_URL"),
        )
    return _CLIENT_LOCAL.c


def _llm(prompt, max_tokens=2048):
    """调用 LLM, 带自动重试。429 限流用长退避 + 抖动, 其他瞬时错误短退避。

    GLM/BigModel 账户有速率限制, 多 worker 并发时易触发 429
    ("您的账户已达到速率限制")。原 3 次×1.5s 短退避不足以等限流窗口恢复。
    现策略: 429 单独走 10-30s 长退避 + 随机抖动 (防多 worker 同步重试再撞限流),
    最多 5 次; 其他错误仍 3 次短退避。

    Args:
        max_tokens: 输出上限。GLM-5.2 是推理模型, 大输出(如 tier_map JSON)需调高
                    (默认 2048; chain_tiers 等大结构用 4096)。
    """
    import random as _rnd
    last_err = None
    for attempt in range(5):
        try:
            resp = _client().chat.completions.create(
                model=_resolve_model(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=max_tokens, timeout=120,
            )
            msg = resp.choices[0].message
            content = (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")
            if content:
                return content
            last_err = "empty content"
            time.sleep(2)
            continue
        except Exception as e:
            err_str = str(e)
            err_type = type(e).__name__
            last_err = f"{err_type}: {err_str[:120]}"
            if attempt >= 4:
                break
            # 429 限流: 长退避 (限流窗口需较长冷却) + 抖动 (防多 worker 同步)
            is_rate_limit = ("429" in err_str or "RateLimit" in err_type
                             or "速率限制" in err_str or "1302" in err_str)
            if is_rate_limit:
                wait = 10 * (attempt + 1) + _rnd.uniform(0, 8)  # ~10-58s 带抖动
                time.sleep(wait)
            else:
                # 其他瞬时错误: 短退避
                time.sleep(1.5 * (attempt + 1))
    # 全部重试失败, 记录原因便于排查 (不再静默吞掉)
    if last_err:
        print(f"    [LLM] 放弃: {last_err}", flush=True)
    return None


def _llm_quick(prompt: str) -> str:
    """快速 LLM 调用 (归因等轻量任务)。

    进程级 client 单例 (函数属性持有); 配置同 _llm, 延迟读 env。
    失败返回空串 (归因任务宁可降级为"未知"也不要阻断主流程)。
    """
    if not hasattr(_llm_quick, "_client"):
        from openai import OpenAI
        _llm_quick._client = OpenAI(
            api_key=os.environ.get("TA_API_KEY", ""),
            base_url=os.environ.get("TA_BASE_URL", ""),
        )
    try:
        resp = _llm_quick._client.chat.completions.create(
            model=_resolve_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=300, timeout=60,
        )
        return resp.choices[0].message.content or ""
    except Exception:
        return ""
