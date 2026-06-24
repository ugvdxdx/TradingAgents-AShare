"""公共工具层：LLM 客户端与联网搜索。

从评分/刷新链路下沉而来，作为归因模块 (picker/discovery/attribution) 与
v3_full_score / scan_mispriced / refresh_fundamentals 等上层共享的底层能力，
消除"归因模块 import 上层工具 → 上层 import 归因模块"的循环依赖。

- picker.common.llm_client : _llm (带 429 退避) / _llm_quick (轻量)
- picker.common.web_search  : _web_search (智谱 MCP web_search_prime)
"""
