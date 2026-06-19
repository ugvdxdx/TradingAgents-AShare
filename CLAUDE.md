# J-TradingAgents — 量化多 Agent 分析框架

## 项目简介

本项目是一个多 Agent 量化分析系统，使用 LangGraph 编排 14 个专业 Agent（市场、新闻、基本面、宏观、资金流、量价、牛熊辩论、风险讨论等）对 A 股/美股/港股进行综合分析。

**核心交互方式**: 通过 Claude Code CLI 直接对话，无需前端。

**两阶段选股流水线**：
1. 阶段一（每周）：V3 基本面打分（`picker/scoring/v3_full_score.py`）— 全量评分 + essence + capital 动态更新
2. 阶段二（每日）：30 天涨幅竞争辩论（`picker/pipeline/debate_picker_v5.py`）— LangGraph 7 阶段：三分析师并行 → 分组海选 Top50→20 → claim 驱动三段式辩论(多→空→多反驳) → 终极 PK → 风控复核 → 终端富文本报告

**核心机制**：
- **新晋股发现**（`picker/discovery/scan_mispriced.py`）：量价扫描 + 网络搜索归因 + 板块扩散 + 冷股激活
- **capital 动态更新**（`picker/scoring/v3_full_score.py:update_capital`）：每次选股前用研报板块动量 + 个股量价(双窗口)重算 capital，纯量化 0 次 LLM
- **过热股检测**（`picker/scoring/v3_full_score.py:detect_overheated`）：高分滞涨股搜索验证 + 风险标记，不自动惩罚
- **冷股池**（`cold_fundamentals/`）：167 只无催化股票冬眠，新晋股逻辑可激活

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
    picker/             # v5 辩论选股包 (graph/state/analysts/judges/debaters/reporter/incremental/rotation)
  graph/                # LangGraph 编排
  dataflows/            # 数据流 + 数据源路由
    providers/          # 数据供应商
      astock_provider.py  # ★ 直接 HTTP/TCP 数据源（首选）
      cn_akshare_provider.py  # fallback
      cn_baostock_provider.py  # fallback
      yfinance_provider.py     # 美股/港股
  llm_clients/          # LLM 适配层
  prompts/              # 提示词模板
  research/             # 研报知识系统 (collector/cleaner/extractor/store/consumer/normalize)
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

── 两阶段选股工具 (picker 包, 原根目录脚本) ──
picker/                 # ★ 选股工具包 (路径统一经 picker.paths 解析)
  paths.py              # 统一路径解析层 (缓存/DB/whitelist 唯一真相源)
  knowledge/            # world_knowledge / ai_knowledge_base / fundamental_agent
  data/                 # data_cache / money_flow / fundamentals_data
  scoring/              # tech_analysis / fundamental_scorer / v3_full_score (原 _v3_full_score)
  discovery/            # scan_mispriced — 新晋股发现
  pipeline/             # debate_picker_v5 / gen_fundamentals / update_* / run_* / fetch_money_flow_all
  backtest/             # run_backtest

── 运行时数据 ──
data/                   # 运行时数据集中
  caches/               # 原 .xxx.json 点前缀缓存 (fundamental_v3_scores / overheated_risk / ...)
  whitelist/            # stock_whitelist / top500_whitelist
  reference/            # top500_and_leaders / world_knowledge_2026_06 / stocks_audit / ...
  board_flow_cache.json / news_cache.json
fundamentals/           # 基本面 JSON (537只热股)
cold_fundamentals/      # 冷股池 (167只无催化, 冬眠)
kline_cache/  profiles/  .mf_cache/  .cache/   # 大缓存目录
research.db             # 研报知识库

── 归档 (不进版本库) ──
archive/                # 备份/一次性脚本/批量产物 (.bak / batch* / 过期脚本)
docs/                   # 设计文档
```

> **入口脚本速查** (原根目录文件名 → 新路径):
> - `_v3_full_score.py` → `picker/scoring/v3_full_score.py`
> - `debate_picker_v5.py` → `picker/pipeline/debate_picker_v5.py`
> - `scan_mispriced.py` → `picker/discovery/scan_mispriced.py`
> - `_gen_top500_fundamentals.py` → `picker/pipeline/gen_fundamentals.py`
> - `update_world_knowledge.py` → `picker/pipeline/update_world_knowledge.py`
> - `run_daily_update.py` → `picker/pipeline/run_daily_update.py`
> - `run_research_pipeline.py` → `picker/pipeline/run_research_pipeline.py`
> - `fetch_money_flow_all.py` → `picker/pipeline/fetch_money_flow_all.py`

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

## 选股系统架构 (2026-06 重构)

### 数据流

```
每日选股 (picker/pipeline/debate_picker_v5.py)
  ├─ collect_data
  │    ├─ update_capital(persist=False)     ← capital动态更新, 0次LLM, 秒级
  │    │    研报板块动量(14天) + 个股量价(双窗口r5+r20) → 重算capital
  │    ├─ detect_overheated()               ← 过热股检测, 搜索验证+风险标记, 不改分
  │    ├─ load_top_n(v3_cache=内存cache)    ← 候选池: V3 Top50 + 新晋股保送 + 行业动量调整
  │    └─ K线 + 资金流 → candidates (66只)
  ├─ incremental_info (并行预拉取)           ← 实时财务+新闻+量化信号+研报+过热风险+新晋股归因
  ├─ 三分析师并行 (技术/资金/基本面)
  ├─ 海选 (hybrid: V3保送6 + LLM竞争4 + 新晋股独立加挂3)
  ├─ 辩论 (3轮×3段式: 多头→空头→多头反驳)
  │    └─ 横向对比矩阵(按板块分组) + 可持续性裁决 + ROUND_GOALS驱动
  └─ 终极PK (LLM排名为基准 + 硬风险下调OR逻辑)
```

### V3 评分三子维度

| 维度 | 更新频率 | 方式 | 说明 |
|---|---|---|---|
| chain (产业链位置) | 季度 | LLM | AI核心8.5+/AI上游材料7.0-8.4/次核心6.0-6.9/设备材料5.0-5.9 |
| delivery (业绩兑现) | 季度 | LLM | 顶级客户+产能+高增兑现→8-10 |
| capital (资金热度) | **每日** | **量化** | 板块动量×个股量价, 模式D(默认): 细分拆分+双窗口 |

### 新晋股发现机制 (picker/discovery/scan_mispriced.py)

```
量价扫描 (近5日>15% & V3<15) → 搜索归因(14天缓存) → 板块扩散(强度过滤)
  ├─ 板块供需型 (保送进候选池)
  ├─ 个股事件型 (不扩散)
  └─ 冷股激活 (r5>15% 自动移回 fundamentals/)
```

### 关键文件说明

> 所有缓存路径经 `picker/paths.py` 统一解析；下表为相对项目根的实际位置。

| 缓存文件 | 内容 | TTL |
|---|---|---|
| `data/caches/fundamental_v3_scores.json` | V3 评分 (chain/delivery/capital/essence) | 季度+每日capital |
| `data/caches/mispriced_attribution_cache.json` | 新晋股归因 (板块供需/个股事件) | 14天 |
| `data/caches/overheated_risk_cache.json` | 过热股风险验证 | 7天 |
| `data/caches/cold_stocks.json` | 冷股清单 | 手动 |
| `data/caches/sub_sector_override.json` | 细分赛道 capital 拆分表 | scan_mispriced 维护 |
| `data/reference/world_knowledge_2026_06.md` | 世界知识 (宏观+归因) | 每日更新 |