# J-TradingAgents — 量化多 Agent 分析框架

## 项目简介

本项目是一个多 Agent 量化分析系统，使用 LangGraph 编排 14 个专业 Agent（市场、新闻、基本面、宏观、资金流、量价、牛熊辩论、风险讨论等）对 A 股/美股/港股进行综合分析。

**核心交互方式**: 通过 Claude Code CLI 直接对话，无需前端。

**两阶段选股流水线**：
1. 阶段一（半月）：V3 基本面打分（`_v3_full_score.py`）— 544 只全量评分 + essence 精华，ρ=0.527
2. 阶段二（每日）：30 天涨幅竞争辩论（`debate_picker_v5.py`）— LangGraph 7 阶段：三分析师并行 → 分组海选 Top50→20 → claim 驱动多空交叉辩论 20→10 → 终极 PK → 风控复核 → 终端富文本报告

**归档**：旧版脚本（`run_debate_picker.py`, `run_stock_picker.py` 等）已移入 `archive/`。

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
skills/
  tradingagents-analysis/  # 15-Agent 深度分析
  tradingagents-sector/    # 板块分析
  fundamentals-scorer/     # ★ V3 基本面评分 & 赛道Alpha（回测ρ=0.527）

── 两阶段选股工具 (根目录) ──
_v3_full_score.py         # V3 全量基本面评分 + essence（544只，8并发，~35min）
_v3_full_backtest.py      # V3 vs 半年涨幅 Spearman 回测
debate_picker_v5.py       # 30天涨幅竞争辩论 — LangGraph 7阶段 (Top50→20→10, claim驱动辩论)
_v5_rolling_backtest.py   # v5 滚动回测 — V3基线 vs v5辩论 收益对比
tradingagents/agents/picker/  # v5 辩论选股包 (graph/state/analysts/judges/debaters/reporter)
fundamental_scorer.py     # V3 评分引擎（三子维度 + essence）
money_flow.py             # 资金流分析（东方财富 + Tushare fallback）
fetch_money_flow_all.py   # 全市场资金流预拉取 → .mf_cache/
_gen_top500_fundamentals.py  # 个股基本面 JSON 生成器
archive/                  # 历史脚本归档
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