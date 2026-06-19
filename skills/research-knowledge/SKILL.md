---
name: research-knowledge
version: 1.0.0
description: >-
  研报知识系统 — 从财经博主圈子采集研报信息，通过LLM提取结构化知识，
  构建双层知识库（行业+通用），为选股系统提供增量信息支持。
  支持增量采集、知识检索、历史快照回测。
  Research Knowledge System — collect, extract, and serve structured market research knowledge.
tags:
  - research
  - 研报
  - knowledge-base
  - 知识库
  - market-analysis
  - 市场分析
  - sector-knowledge
  - 行业知识
  - incremental-update
  - 增量更新
  - backtest
  - 回测
  - A-share
  - A股
  - stock-picking
  - 选股
  - claude-code
---

# 研报知识系统 Research Knowledge System

从财经博主圈子采集盘前/盘中/盘后复盘及行业研报信息，通过 LLM 提取结构化知识，构建双层知识库，为选股系统辩论阶段提供增量信息支持。

## 数据规模（2026.04-2026.06）

| 指标 | 数量 |
|:---|:---|
| 原始帖子 | 213 条 |
| 行业知识库 | 682 条 |
| 通用知识库 | 209 条 |
| 每日复盘索引 | 209 条 |
| 覆盖时间范围 | 2026-04-01 ~ 2026-06-15 |

## 五层架构

```
L1. Collector  ─ 数据采集层 (小鹅通圈子API + cursor分页 + 增量更新)
L2. Cleaner    ─ 数据清洗与标准化层 (去噪/分段/信息类型分类/行业标签初筛)
L3. Extractor  ─ 知识提取层 (LLM结构化提取: 行业观点/个股提及/逻辑链条/关键数据)
L4. Store      ─ 知识存储层 (SQLite + 双层知识库 + 快照 + 回测)
L5. Service    ─ 知识服务层 (API + 检索 + 回测接口)
```

## 双层知识库

### 行业知识库 (Layer 1)

按行业/板块维度组织，每条记录包含：
- `sector` — 行业/板块名称
- `viewpoint` — 核心观点
- `logic_chain` — 逻辑链条 (JSON数组)
- `sentiment` — 情绪 (bullish/bearish/neutral)
- `key_data` — 关键数据 (JSON数组)

### 通用知识库 (Layer 2)

按帖子维度组织，每条记录包含：
- `info_type` — 信息类型 (morning_review/noon_review/close_review/research/analysis)
- `summary` — 摘要
- `market_overview` — 市场概览
- `key_insights` — 关键洞察 (JSON数组)
- `risk_warnings` — 风险提示 (JSON数组)
- `stock_mentions` — 个股提及 (JSON数组, 含name/code/sentiment/reason)

### 每日复盘索引 (Organization A)

按交易日维度组织，快速定位某日的全部复盘信息。

## 快速上手

**直接对我说：**
- "采集最新的圈子数据"
- "查询半导体行业的知识"
- "6月15日有什么研报信息"
- "提取未处理帖子的结构化知识"
- "输出今天的复盘"

## 核心脚本

```bash
cd /path/to/J-TradingAgents

# ★ 日常增量更新 (推荐, 采集+提取+注入三合一)
uv run python3 picker/pipeline/run_daily_update.py              # 全流程
uv run python3 picker/pipeline/run_daily_update.py --step 1     # 仅采集
uv run python3 picker/pipeline/run_daily_update.py --step 2     # 仅提取
uv run python3 picker/pipeline/run_daily_update.py --step 3     # 仅注入fundamentals

# 一次性全量历史回填 (首次部署)
uv run python3 picker/pipeline/run_research_pipeline.py
```

> 详细的 --step 用法见 research-daily-update skill。

## 知识检索 (CLI)

```bash
# 统计概览
uv run python3 skills/research-knowledge/scripts/query.py stats

# 按行业检索
uv run python3 skills/research-knowledge/scripts/query.py sector 半导体

# 按个股检索
uv run python3 skills/research-knowledge/scripts/query.py stock 立昂微

# 按日期检索
uv run python3 skills/research-knowledge/scripts/query.py date 2026-06-15
```

## 增量更新机制

1. **基于时间戳** — Collector 记录上次采集时间，仅拉取新帖子
2. **is_processed 标记** — 跟踪每条帖子的处理状态
3. **更新日志** — `update_log` 表记录每次更新的内容、时间和影响范围

## 回测支持

- **历史时间过滤** — `consumer.py` 的各查询接口支持 `cutoff_date` 参数，可按历史时间点过滤知识（回测时只取该日前的研报，避免未来函数）
- **回测接口** — 选股系统 `picker/pipeline/debate_picker_v5.py` 可基于 `--date` 指定历史日期，配合 `cutoff_date` 走回测模式（仅用本地缓存数据）
- **生产隔离** — 回测只读 research.db，不影响生产环境数据
- ⚠️ `knowledge_snapshots` 表当前为空（快照机制未启用），历史回测靠 `cutoff_date` 时间过滤实现

## 与选股系统集成

研报知识系统为选股辩论阶段提供增量信息：

```
V3 基本面评分 + essence精华
  │
  ▼
研报知识系统 (consumer.py)
  │  get_stock_research_signal()  → 个股研报信号 (辩论+增量)
  │  get_industry_research_brief()→ 板块研报摘要 (基本面生成注入)
  │  get_sector_momentum()        → 行业研报动量 (分析师+轮动)
  │  get_dark_horse_stocks()      → 研报黑马 (海选保送)
  │  get_research_risk_signals()  → 研报风险 (海选排雷)
  ▼
辩论选股系统 (picker/pipeline/debate_picker_v5.py)
  │  三分析师报告注入研报知识
  │  claim辩论引用行业逻辑链条
  ▼
最终 TOP10 排名
```

## 数据库设计

```sql
-- 原始帖子
raw_feeds (feed_id, community_id, author_id, author_name, title, content, text, created_at, fetched_at, updated_at, is_processed, version)

-- 行业知识库
sector_knowledge (id, feed_id, sector, viewpoint, logic_chain, sentiment, key_data, created_at, inserted_at, raw_hash)

-- 通用知识库
general_knowledge (id, feed_id, info_type, summary, market_overview, key_insights, risk_warnings, stock_mentions, created_at, inserted_at, raw_hash)

-- 每日复盘索引
daily_review (id, trade_date, feed_id, info_type, summary, sectors, inserted_at)

-- 知识快照 (回测, 当前未启用)
knowledge_snapshots (id, snap_date, snap_type, sector_json, general_json, feed_count, created_at)

-- 更新日志
update_log (id, run_at, new_count, update_count, error_count, last_feed_id, last_created_at, detail)
```

## 信息类型分类

| 类型 | 说明 | 典型内容 |
|:---|:---|:---|
| morning_review | 盘前/早盘 | 隔夜外盘、政策消息、开盘预判 |
| noon_review | 午盘 | 半日行情、板块轮动、资金动向 |
| close_review | 收盘 | 全日复盘、涨跌统计、明日展望 |
| research | 研报资料 | 行业深度分析、技术路线、产业链梳理 |
| analysis | 分析评论 | 市场逻辑、投资策略、风险提示 |

## 文件结构

```
tradingagents/research/       # 核心模块
  __init__.py                 # 架构定义与集成点
  collector.py                # L1 数据采集 (小鹅通API + cursor分页)
  cleaner.py                  # L2 数据清洗 (去噪/分段/分类)
  extractor.py                # L3 知识提取 (LLM结构化提取)
  store.py                    # L4 知识存储 (SQLite + 双层知识库)
  service.py                  # L5 知识服务 (API + 检索 + 回测)

picker/pipeline/run_research_pipeline.py      # 全流程运行脚本
save_batch.py                 # 批量知识导入脚本
research.db                   # SQLite 数据库

skills/research-knowledge/    # Skill 定义
  SKILL.md                    # 本文件
  scripts/
    query.py                  # 知识检索脚本
```

## 环境变量

| 变量 | 说明 | 必须 |
|:---|:---|:---:|
| `XIAOE_COOKIE` | 小鹅通登录Cookie | 采集时需要 |
| `OPENAI_API_KEY` | LLM API Key | 提取时需要 |
| `OPENAI_BASE_URL` | LLM API Base URL | 提取时需要 |
| `OPENAI_MODEL` | LLM 模型名 | 提取时需要 |

## 注意事项

- 小鹅通Cookie有效期为数小时，过期需重新获取
- LLM提取质量取决于模型能力，建议使用高质量模型
- 数据库路径默认为项目根目录的 `research.db`
- 增量采集建议每日执行一次，避免频繁请求
