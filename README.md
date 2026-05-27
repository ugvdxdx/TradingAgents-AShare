# J-TradingAgents

量化多 Agent 分析框架 — 使用 LangGraph 编排 14 个专业 Agent 对 A 股/美股/港股进行综合投研分析。

## 架构概览

```
┌─────────────────────────────────────────────────────┐
│                   LangGraph 编排                     │
├──────────┬──────────┬──────────┬──────────┬─────────┤
│ 7 位分析师 │ 牛熊辩论   │ 3 位风险   │  Trader  │ PM     │
│ ──────── │ ──────── │ ──────── │ ──────── │ ────── │
│ Market   │ Bull     │ Aggressive│          │        │
│ Social   │ Bear     │ Neutral  │  交易建议  │ 组合   │
│ News     │ Manager  │ Conservative│         │ 管理   │
│ Fundamentals│       │          │          │        │
│ Macro    │          │          │          │        │
│ SmartMoney│          │          │          │        │
│ VolumePrice│          │          │          │        │
├──────────┴──────────┴──────────┴──────────┴─────────┤
│              数据供应商路由 (fallback 链)              │
│  cn_astock → cn_akshare → cn_baostock → yfinance    │
└─────────────────────────────────────────────────────┘
```

## 快速开始

### 安装

```bash
# 克隆项目
git clone https://github.com/KylinMountain/TradingAgents-AShare.git
cd TradingAgents-AShare

# 安装依赖 (推荐 uv)
uv sync

# 或 pip
pip install -e .
```

### 配置

```bash
# 复制配置模板
cp .env.example .env

# 编辑 .env，填入 LLM API Key
# TA_API_KEY=sk-your-api-key
# TA_BASE_URL=https://api.openai.com/v1
# TA_LLM_PROVIDER=openai
```

支持的环境变量见 [.env.example](.env.example)。

### 使用

```bash
# CLI — 单只股票分析
tradingagents analyze 600519.SH --date 2026-05-26
tradingagents analyze 贵州茅台
tradingagents analyze 600519 --horizon short --quick

# CLI — 自选股管理
tradingagents watchlist list
tradingagents watchlist add 600519.SH
tradingagents watchlist remove <item_id>

# CLI — 定时任务
tradingagents scheduled list
tradingagents scheduled add 600519.SH --time 20:00
tradingagents scheduled remove <item_id>

# API 服务 (供外部程序调用)
tradingagents-api

# 定时调度器 (独立进程)
tradingagents-scheduler
```

也可以通过 Python API 直接调用：

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

graph = TradingAgentsGraph(DEFAULT_CONFIG)
result = graph.run("600519.SH", "2026-05-26")
print(result["final_trade_decision"])
```

## 14 个 Agent

| 角色 | Agent | 说明 |
|------|-------|------|
| 分析师 | Market Analyst | 技术面分析（均线/MACD/KDJ/布林带） |
| 分析师 | Social Media Analyst | 社交媒体情绪分析 |
| 分析师 | News Analyst | 新闻舆情分析 |
| 分析师 | Fundamentals Analyst | 基本面分析（财务报表/估值） |
| 分析师 | Macro Analyst | 宏观经济分析 |
| 分析师 | Smart Money Analyst | 资金流向分析（主力/北向/龙虎榜） |
| 分析师 | Volume Price Analyst | 量价关系分析 |
| 研究员 | Bull Researcher | 看多研究员（辩论正方） |
| 研究员 | Bear Researcher | 看空研究员（辩论反方） |
| 研究员 | Research Manager | 辩论裁判，综合牛熊观点 |
| 风险 | Aggressive Debator | 激进风险视角 |
| 风险 | Neutral Debator | 中性风险视角 |
| 风险 | Conservative Debator | 保守风险视角 |
| 交易 | Trader | 最终交易决策与仓位建议 |

## 数据源

采用供应商路由 + fallback 机制，优先使用直接 HTTP/TCP 数据源：

| 优先级 | 供应商 | 数据通道 | 说明 |
|--------|--------|----------|------|
| 1 | cn_astock | mootdx TCP 7709 / 腾讯 HTTP / 东方财富 / 同花顺 / 百度 / 新浪 / 财联社 | 直接接口，最鲁棒 |
| 2 | cn_akshare | akshare Python 库 | A 股 fallback |
| 3 | cn_baostock | baostock Python 库 | 历史 K 线 fallback |
| 4 | yfinance | yfinance Python 库 | 美股/港股 |

配置优先级链在 [default_config.py](tradingagents/default_config.py) 的 `data_vendors` 中定义。

## 项目结构

```
tradingagents/              # 核心包
  agents/                   # 14 个 Agent 实现
    analysts/               # 7 位分析师
    researchers/            # 牛熊研究员 + Research Manager
    risk_mgmt/              # 3 位风险 Debator
    trader/                 # Trader
    managers/               # Risk Manager
    utils/                  # 共享工具
  graph/                    # LangGraph 编排 (TradingAgentsGraph)
    trading_graph.py        # 主图定义
    data_collector.py       # 数据收集器
    intent_parser.py        # 自然语言意图解析
  dataflows/                # 数据流 + 供应商路由
    interface.py            # route_to_vendor() 路由 + fallback
    config.py               # set_config() 全局配置
    providers/              # 数据供应商实现
      astock_provider.py    # ★ 直接 HTTP/TCP 数据源 (首选)
      cn_akshare_provider.py
      cn_baostock_provider.py
      yfinance_provider.py
      alpha_vantage_provider.py
  llm_clients/              # LLM 适配层 (OpenAI/Anthropic/Google)
  prompts/                  # 提示词模板 (支持 zh/en/auto)
  default_config.py         # 默认配置 + 环境变量映射

api/                        # FastAPI 服务端
  main.py                   # REST API (analyze/jobs/reports/watchlist/scheduled/config)
  database.py               # SQLite/PostgreSQL 模型 (单用户模式)
  services/                 # 业务逻辑层
  job_store.py              # 内存/Redis 任务队列

scheduler/                  # 定时调度器 (独立进程)
  main.py                   # asyncio 循环 + concurrency 控制

cli/                        # typer CLI 入口
  main.py                   # analyze/watchlist/scheduled 命令

a-stock-data/               # SKILL.md — 数据端点参考文档
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/analyze` | POST | 提交分析任务 |
| `/v1/jobs/{id}` | GET | 查询任务状态 |
| `/v1/jobs/{id}/result` | GET | 获取分析结果 |
| `/v1/jobs/{id}/events` | GET (SSE) | 实时分析流 |
| `/v1/chat/completions` | POST | OpenAI 兼容接口 |
| `/v1/reports` | GET/POST | 报告列表/创建 |
| `/v1/reports/{id}` | GET/DELETE | 单条报告 |
| `/v1/watchlist` | GET/POST | 自选股 |
| `/v1/scheduled` | GET/POST | 定时任务 |
| `/v1/config` | GET/PATCH | 运行时配置 |
| `/v1/portfolio/imports` | POST | 持仓导入 |
| `/v1/market/*` | GET | 市场数据/搜索 |
| `/v1/backtest` | POST | 回测 |
| `/healthz` | GET | 健康检查 |

所有端点无需认证（单用户模式）。

## 通知

定时分析完成后可自动推送：

- **企业微信**: 设置 `TA_WECOM_WEBHOOK_URL`
- **邮件**: 设置 `TA_EMAIL_REPORT_TO` + SMTP 环境变量（暂搁置）

## 开发

```bash
# 运行测试
pytest tests/ -v

# 本地 API 开发
uvicorn api.main:app --reload --port 8000
```

## License

MIT