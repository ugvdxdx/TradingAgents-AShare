---
name: research-daily-update
version: 2.0.0
description: >-
  研报更新流水线。从财经博主圈子采集最新研报→LLM结构化提取→赛道热度档/异动归因/缺口发现→
  fundamentals彻底重写→资金分重算→池子边界管理→每日快照。支持「一键全量更新」和「子任务单独跑」。
  主入口 picker/pipeline/run_daily_maintenance.py (--step 1-9 控制单步, 0=全流程)。
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
      env: [TA_API_KEY, TA_BASE_URL, XIAOE_COOKIE]
      bins: [python3, uv]
    emoji: "📰"
---

# 研报更新

每日从财经博主圈子采集最新研报，LLM 提取结构化知识，并传播到下游所有评分/选股依赖项。
是选股流水线的**最上游**（① 更新研报 → ② 生成基本面 → ③ 评分 → ④ 选股）。

## 🎯 快速上手

**直接对我说：**
- "更新研报" → 跑完整流程（采集到快照）
- "只采集最新研报" → `--step 1`
- "只跑资金分" → `--step 4`
- "跳过 web search 直接刷 fundamentals" → `--skip-movement --skip-discovery`
- "收盘后一起跑" → 完整流程放后台

## 🐍 核心命令（主入口）

```bash
# ★ 一键全流程: 采集→提取→异动→缺口→tier→fundamentals→capital→池子边界→K线→快照
uv run python3 picker/pipeline/run_daily_maintenance.py
```

默认行为：近 3 天增量采集 + 全部 9 步执行，K线/资金流子进程与研报采集**并行**。

## 📋 九步流程详解

| Step | 名称 | 说明 | LLM? | 依赖 web search? |
|:---:|:---|:---|:---:|:---:|
| **1** | 研报采集 | 小鹅通圈子API → `raw_feeds` 表 | ✗ | ✗ |
| **2** | 知识提取 | LLM 把帖子→结构化知识(行业观点/个股/逻辑链) → `research.db` | ✓ | ✗ |
| **2.7** | 异动分析 | 扫描全池异动股(r20≥25%或≤-18%)，web search 涨跌原因→缓存 | ✗ | ✓ |
| **2.5** | 板块缺口发现 | 研报热但池未覆盖的主题，web search 找股入池 | ✓ | ✓ |
| **2.6** | chain tier更新 | 融合研报+异动+缺口三信号调赛道热度档 | ✓ | ✗ |
| **3** | fundamentals刷新 | 研报新提及的个股 → 彻底重写基本面JSON | ✓ | ✗ |
| **4** | capital更新 | 纯量化重算资金分(板块动量+量价)，0 LLM | ✗ | ✗ |
| **5** | 过热股检测 | 高分滞涨股搜索验证 | ✗ | ✓ |
| **6** | 冷股激活 | r5>15% 的冷池股移回 hot 池 | ✗ | ✗ |
| **6.5** | 冷门清理 | V3<7+chain<4+cap<3+r20<5+无研报 → 移入冷池 | ✗ | ✗ |
| **7** | K线增量更新 | 与研报采集并行(非独立step) | ✗ | ✗ |
| **8** | 世界知识更新 | 更新世界知识库 | ✗ | ✗ |
| **9** | 每日快照 | 创建当日评分快照 | ✗ | ✗ |

> **执行顺序**：采集并行启动 → 2.7 异动 → 2.5 缺口 → 2.6 tier → 3 fundamentals → 4 capital → 5 过热 → 6 冷股激活 → 6.5 清理 → 8 世界知识 → 9 快照。
> 2.7/2.5/2.6 有依赖关系：异动和缺口为 tier 更新提供信号，故先跑。
> **Step 3 刷新列表** = 研报提及 ∪ **上涨**异动（下跌异动股不入刷新——它们用于感知行业动向，喂给 Step 2.6 chain_tiers 的 `price_confirmed_cold` 发现板块风险）。若某下跌股同时被研报提及，仍会刷新且其下跌结论作为 headwinds 注入。

## 🔧 子任务单独跑

### 单步执行（`--step N`）

```bash
# Step 1: 只采集最新研报 (默认近3天)
uv run python3 picker/pipeline/run_daily_maintenance.py --step 1

# Step 1: 指定日期范围采集 (补采)
uv run python3 picker/pipeline/run_daily_maintenance.py --step 1 --from 2026-06-18 --to 2026-06-24

# Step 2: 只提取 (把未处理帖子转为结构化知识)
uv run python3 picker/pipeline/run_daily_maintenance.py --step 2

# Step 3: 只刷 fundamentals (研报新提及个股的彻底重写)
uv run python3 picker/pipeline/run_daily_maintenance.py --step 3

# Step 4: 只重算资金分 (纯量化, 秒级)
uv run python3 picker/pipeline/run_daily_maintenance.py --step 4

# Step 9: 只建每日快照
uv run python3 picker/pipeline/run_daily_maintenance.py --step 9
```

> ⚠️ `--step N` 只对整数 step(1,2,3,4,5,6,9)有效。2.5/2.6/2.7 是子步骤，**只在全流程(`--step 0`)中执行**，需用 `--skip-*` 组合控制。

### 全流程 + 选择性跳过

最灵活的方式：跑全流程但跳过某些子步骤。

```bash
# 跳过采集+提取(今天已采过)，直接跑下游
uv run python3 picker/pipeline/run_daily_maintenance.py --skip-collect

# 跳过 web search 相关(异动+缺口发现), 让 fundamentals/capital 核心链路先跑
# —— web search 资源包耗尽时用这个
uv run python3 picker/pipeline/run_daily_maintenance.py --skip-movement --skip-discovery

# 跳过整个研报链路(step1-3)，只跑资金分+池子管理+快照
uv run python3 picker/pipeline/run_daily_maintenance.py --skip-research

# chain tier 用 auto 模式(有变化即写入, 归档可回滚; 默认 manual 只输出 diff)
uv run python3 picker/pipeline/run_daily_maintenance.py --chain-tiers-mode auto

# 只采集数据(K线+资金流), 不跑研报/评分
uv run python3 picker/pipeline/run_daily_maintenance.py --data-only

# 干跑(只输出不写文件) — 用于预览 fundamentals 会改哪些
uv run python3 picker/pipeline/run_daily_maintenance.py --step 3 --dry-run
```

### 数据采集（独立模式）

K线/资金流有专用入口，也可在主流程内并行采集：

```bash
# 只更新 K线 + 资金流 (带新鲜度预检, 已最新则跳过)
uv run python3 picker/pipeline/run_daily_maintenance.py --data-only

# 只采 K线, 不采资金流
uv run python3 picker/pipeline/run_daily_maintenance.py --data-only --skip-moneyflow

# 强制采集(忽略新鲜度预检)
uv run python3 picker/pipeline/run_daily_maintenance.py --data-only --no-fresh-check
```

## 📋 全部参数

| 参数 | 说明 |
|:---|:---|
| `--step N` | 只执行指定步骤 (1-9 整数; 0=全流程, 默认) |
| `--from / --to` | 采集日期范围 (YYYY-MM-DD, 默认近3天到今天) |
| `--skip-research` | 跳过整个研报链路 (step1-3 含缺口发现) |
| `--skip-collect` | 只跳采集+提取 (step1-2), 保留缺口发现+刷新 |
| `--skip-discovery` | 跳过板块缺口发现 (step2.5) |
| `--skip-movement` | 跳过异动分析 (step2.7) |
| `--skip-chain-tiers` | 跳过 chain tier_map 更新 (step2.6) |
| `--chain-tiers-mode` | `manual`(默认,只输出diff) / `auto`(diff有变化即写入) |
| `--discover-threshold` | 缺口发现入池 V3 阈值 (默认 8.0) |
| `--skip-cleanup` | 跳过冷门清理 (step6.5) |
| `--cleanup-threshold` | 冷门清理 V3 阈值 (默认 7.0) |
| `--capital-mode` | capital 模式 `G`(默认)/`D`/`A` |
| `--skip-data` | 跳过 K线+资金流采集 |
| `--skip-klines` | 跳过 K线采集 (保留资金流) |
| `--skip-moneyflow` | 跳过资金流采集 (保留K线) |
| `--no-fresh-check` | 跳过新鲜度预检 (强制采集) |
| `--data-only` | 只采集 K线+资金流, 不跑研报/评分 |
| `--dry-run` | 只输出不写文件 |

## 🔄 流水线全景

```
并行采集层 (主进程 + 子进程):
  ├─ Step 1: 小鹅通圈子API → raw_feeds (博主原文)
  └─ Step 7: K线/资金流 子进程并行采集

研报链路 (主进程, 串行):
  ├─ Step 2: LLM提取 → research.db (sector/general/daily_review 三张表)
  ├─ Step 2.7: 异动分析 → movement_driver_cache.json (涨跌原因归因)
  ├─ Step 2.5: 缺口发现 → 新股入池 (V3≥阈值)
  ├─ Step 2.6: chain tier_map 更新 (赛道热度6档)
  └─ Step 3: fundamentals/{code}.json 彻底重写

评分链路 (主进程):
  ├─ Step 4: capital 重算 (纯量化)
  ├─ Step 5: 过热股检测
  ├─ Step 6: 冷股激活 (冷→热)
  ├─ Step 6.5: 冷门清理 (热→冷)
  └─ Step 9: 每日评分快照
```

## ⚠️ 运维要点（踩过的坑）

### 1. 空帖积压（已修复）
圈子里的**图片帖/链接卡**（正文在图里抓不到）会进 `raw_feeds` 但 `text` 为空。采集器现在自动把这类帖子标 `is_processed=1` 跳过提取，**不会积压成"100+待处理"的误导数字**。
- 阈值常量：`tradingagents/research/collector.py` 的 `MIN_TEXT_LEN = 10`（与提取 SQL 共用）
- 若历史空帖积压，清理：`UPDATE raw_feeds SET is_processed=1 WHERE is_processed=0 AND (text IS NULL OR text='' OR length(text)<=10)`

### 2. Web search 计费坑
代码的 web search（异动/缺口发现用）走智谱 **MCP `web_search_prime`**（Remote MCP Server，`open.bigmodel.cn/api/mcp/web_search_prime/mcp`），额度**计入 GLM Coding Plan 套餐**（Pro 1000次/月, Max 4000次/月），**不再走**易限流的旧 `web-search-pro` 独立资源包。
- **协议**：MCP streamable HTTP，有状态会话（先 `initialize` 握手拿 `Mcp-Session-Id` → `notifications/initialized` → `tools/call`）。`refresh_fundamentals.py` 的 `_McpSession` 进程级复用 session，`_web_search` 自动重建失效会话。
- **识别**：套餐/资源包耗尽时报 `HTTP 429 + code:1113 余额不足`。代码 `_is_rate_limited` 已区分，1113 是余额不足(不可恢复)，1302 才是真限流(可重试)；余额不足时不死循环重试。
- **规避**：额度耗尽时用 `--skip-movement --skip-discovery` 跳过 web search，core 链路(fundamentals/capital/快照)不受影响。

### 3. LLM 限流退避（Step 2 提取）
GLM 限流时 Step 2 单条可能卡 5-30s 退避。积压大时全量提取耗时长（~30s-3min/条）。提取是**串行**的，无并发。

### 4. Cookie 过期
Step 1 采集依赖 `XIAOE_COOKIE` 环境变量。失效时采集报错或返回空，需更新 Cookie。

### 5. 并行采集
K线/资金流采集是**子进程并行**启动的（与研报采集同时跑），研报链路跑完后 join 收集结果。`--step N`(单步)时**不启动**并行采集（除非 `--step 0`），需数据时用 `--data-only` 单独跑。

## 🆚 与其他入口的区别

| 场景 | 命令 | 用途 |
|:---|:---|:---|
| **日常更新（首选）** | `picker/pipeline/run_daily_maintenance.py` | 9步全流程，含下游传播 |
| 仅研报采集+提取+注入 | `picker/pipeline/run_daily_update.py` | 旧3步入口(已被maintenance取代) |
| 首次部署/补历史 | `picker/pipeline/run_research_pipeline.py` | 全量回填指定日期范围 |
| 单独注入研报到fundamentals | `picker/pipeline/update_fundamentals_from_research.py` | 不走完整流水线 |

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `picker/pipeline/run_daily_maintenance.py` | ★ 主入口(9步编排, --step/--skip 控制) |
| `picker/pipeline/run_daily_update.py` | 旧3步入口(采集/提取/注入) |
| `picker/pipeline/refresh_fundamentals.py` | Step 3 实现(含 `_web_search` MCP web_search_prime 封装) |
| `picker/scoring/v3_full_score.py` | Step 4 capital / Step 2.7 异动 / Step 5 过热 |
| `picker/scoring/chain_tiers.py` | Step 2.6 赛道热度档更新 |
| `picker/discovery/discover_sector_gap.py` | Step 2.5 缺口发现 |
| `picker/discovery/scan_mispriced.py` | Step 6/6.5 池子边界管理 |
| `tradingagents/research/collector.py` | L1 数据采集(小鹅通API, 含 `MIN_TEXT_LEN`) |
| `tradingagents/research/extractor.py` | L3 知识提取(LLM) |
| `tradingagents/research/store.py` | L4 知识存储(SQLite) |
| `research.db` | ★ 研报知识库 |
| `data/caches/fundamental_v3_scores.json` | V3 评分缓存(capital 更新写这里) |
| `data/caches/surge_driver_cache.json` | 异动归因缓存(Step 2.7 写) |
| `data/reference/chain_tier_map.json` | 赛道热度档映射(Step 2.6 写) |

## 🔗 上下游

- **上游** — 财经博主圈子(小鹅通API) + Tushare(K线/资金流)
- **下游** — ② 基本面生成 + ③ V3评分(`fundamental_v3_scores.json`) + ④ 选股辩论

## ✅ 验证更新是否成功

```bash
# 看各环节时间戳是否为今天
uv run python3 -c "
from picker import paths
from tradingagents.research.store import KnowledgeStore
import os, datetime
# 研报库
s = KnowledgeStore(db_path=paths.RESEARCH_DB)
print('知识库:', s.stats()); s.close()
# 关键缓存 mtime
for f in ['data/caches/fundamental_v3_scores.json', 'data/reference/chain_tier_map.json']:
    mt = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
    print(f'{f}: {mt}')
"
```
