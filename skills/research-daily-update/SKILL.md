---
name: research-daily-update
version: 1.0.0
description: >-
  每日研报增量更新流水线。从财经博主圈子采集最新研报→LLM结构化提取→注入fundamentals JSON。
  区别于 run_research_pipeline.py(全量历史回填), 本流程聚焦日常增量, 用 --step 控制
  采集/提取/注入三阶段。产出 research.db 供选股辩论和基本面生成消费。
tags:
  - research
  - 研报
  - daily-update
  - 增量更新
  - knowledge-base
  - 知识库
  - claude-code
metadata:
  openclaw:
    requires:
      env: [TA_API_KEY, TA_BASE_URL]
      bins: [python3]
    emoji: "📰"
---

# 每日研报增量更新

每日从财经博主圈子采集最新研报，LLM 提取结构化知识，注入 fundamentals JSON。是选股流水线的**最上游**（① 更新研报 → ② 生成基本面 → ③ 评分 → ④ 选股）。

## 🎯 快速上手

**直接对我说：**
- "更新研报"
- "只采集最新研报，不要提取"
- "把研报注入到基本面"

## 🐍 核心命令

```bash
# 全流程: 采集 → LLM提取 → 注入fundamentals (约10-20分钟)
uv run python3 run_daily_update.py

# 仅采集最新研报到 research.db
uv run python3 run_daily_update.py --step 1

# 仅LLM提取(把未处理帖子转为结构化知识)
uv run python3 run_daily_update.py --step 2

# 仅注入fundamentals(把研报提炼写回JSON)
uv run python3 run_daily_update.py --step 3
```

## 📋 参数说明

| 参数 | 说明 |
|:---|:---|
| `--step 0` | 全流程(默认, 采集+提取+注入) |
| `--step 1` | 仅采集(小鹅通圈子API拉最新帖子) |
| `--step 2` | 仅提取(LLM把帖子转为行业观点/个股提及/逻辑链条) |
| `--step 3` | 仅注入(把研报知识写回 fundamentals/{code}.json) |

## 🔄 三阶段流水线

```
① 采集 (Collector)
   └ 小鹅通圈子API + cursor分页 → raw_feeds 表(博主原文)
② 提取 (Cleaner + Extractor)
   └ 去噪/分段/分类 → LLM结构化提取
   └ → sector_knowledge (行业观点+逻辑链条+情绪+关键数据)
   └ → general_knowledge (摘要+市场概览+洞察+风险+个股提及)
   └ → daily_review (每日复盘索引)
③ 注入 (update_fundamentals_from_research)
   └ 把研报知识提炼后增量追加到 fundamentals/{code}.json
```

## 🆚 与全量回填的区别

| 场景 | 命令 | 用途 |
|:---|:---|:---|
| **日常增量** | `run_daily_update.py` | 每天跑，只处理新帖子 |
| **首次部署/补历史** | `run_research_pipeline.py` | 一次性回填指定日期范围的历史数据 |

## 🔧 单独注入研报（不走完整流水线）

```bash
# 把研报提炼注入全部提及过的个股
uv run python3 update_fundamentals_from_research.py

# 只注入指定个股
uv run python3 update_fundamentals_from_research.py --stock 300308

# 最少提及2次才处理(默认)
uv run python3 update_fundamentals_from_research.py --min-mentions 2

# 干跑(只输出不写文件)
uv run python3 update_fundamentals_from_research.py --dry-run
```

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `run_daily_update.py` | ★ 每日增量流水线入口(--step 控制) |
| `run_research_pipeline.py` | 全量历史回填(首次部署) |
| `update_fundamentals_from_research.py` | 研报→fundamentals 注入(可单独跑) |
| `tradingagents/research/collector.py` | L1 数据采集(小鹅通API) |
| `tradingagents/research/cleaner.py` | L2 数据清洗 |
| `tradingagents/research/extractor.py` | L3 知识提取(LLM) |
| `tradingagents/research/store.py` | L4 知识存储(SQLite) |
| `tradingagents/research/service.py` | L5 知识服务 |
| `tradingagents/research/consumer.py` | L6 消费层(研报→选股/基本面注入桥接) |
| `research.db` | ★ 研报知识库(SQLite) |

## ⚠️ 注意事项

- **Cookie** — `run_daily_update.py` 中的小鹅通 Cookie 是硬编码的，可能过期失效。失效时采集步骤会报错，需更新 Cookie。
- **LLM 成本** — 提取步骤(--step 2)对每个新帖子调用一次 LLM，量大时有成本。
- **注入范围** — `update_fundamentals_from_research.py` 只注入被研报**直接点名**的个股(`stock_mentions`)，冷门股靠板块匹配(见 fundamentals-generator skill 的 `get_industry_research_brief`)。

## 🔗 上下游

- **上游** — 财经博主圈子(小鹅通API)
- **下游** — ② 基本面生成(`_gen_top500_fundamentals.py` 注入板块研报) + ④ 选股辩论(`debate_picker_v5.py` 注入个股研报信号)
