# J-TradingAgents — 量化多 Agent 分析框架

## 项目简介

本项目是一个多 Agent 量化分析系统，使用 LangGraph 编排 14 个专业 Agent（市场、新闻、基本面、宏观、资金流、量价、牛熊辩论、风险讨论等）对 A 股/美股/港股进行综合分析。

**核心交互方式**: 通过 Claude Code CLI 直接对话，无需前端。

## 关键命令

```bash
# 单只股票分析
tradingagents analyze 600519.SH --date 2026-05-26
tradingagents analyze 贵州茅台

# 自选股管理
tradingagents watchlist list
tradingagents watchlist add 600519.SH

# 定时任务
tradingagents scheduled list
tradingagents scheduled add 600519.SH --time 20:00

# API 服务（供外部调用）
tradingagents-api

# 定时调度器（独立进程）
tradingagents-scheduler
```

## 项目结构

```
tradingagents/          # 核心包
  agents/               # 14 个 Agent
  graph/                # LangGraph 编排
  dataflows/            # 数据流 + 数据源路由
    providers/          # 数据供应商
      astock_provider.py  # ★ 直接 HTTP/TCP 数据源（首选）
      cn_akshare_provider.py  # fallback
      cn_baostock_provider.py  # fallback
      yfinance_provider.py     # 美股/港股
  llm_clients/          # LLM 适配层
  prompts/              # 提示词模板
  default_config.py     # 默认配置

api/                    # FastAPI 服务端
  main.py               # REST API
  database.py           # SQLite/PostgreSQL
  services/             # 业务逻辑
  job_store.py          # 任务队列

scheduler/              # 定时调度器（独立进程）
cli/                    # typer CLI 入口
a-stock-data/           # SKILL.md — 数据端点参考文档
```

## 数据源架构

`dataflows/interface.py` 中的 `route_to_vendor()` 实现了供应商路由和 fallback 机制：

- **首选**: `cn_astock` — 直接 HTTP/TCP 调用（mootdx 7709、腾讯 GBK 88 字段、东方财富 datacenter、同花顺、百度、新浪、财联社）
- **Fallback**: `cn_akshare` → `cn_baostock` → `yfinance`
- 配置在 `default_config.py` 的 `data_vendors` 中定义优先级链

## 环境配置

主要环境变量（见 `.env.example`）：
- `TA_API_KEY` — LLM API Key
- `TA_BASE_URL` — LLM API Base URL
- `TA_LLM_PROVIDER` — LLM 供应商 (openai/anthropic/google)
- `TA_LLM_DEEP` / `TA_LLM_QUICK` — 模型名
- `TA_LANGUAGE` — 提示词语言 (zh/en/auto)
- `TA_WECOM_WEBHOOK_URL` — 企业微信通知

## 注意事项

- 单用户模式，`DEFAULT_USER_ID = "default"`，无认证系统
- 定时分析通知通过环境变量配置，不依赖 DB 加密存储
- `astock_provider.py` 使用 mootdx TCP 连接（端口 7709），懒加载单例
- 所有 Agent 输出格式统一为 markdown/CSV 字符串