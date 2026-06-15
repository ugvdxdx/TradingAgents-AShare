---
name: fundamentals-scorer
version: 3.1.0
description: >-
  A股基本面 V3 评分与赛道Alpha选股工具。
  回测验证(544只A股, 2025.12-2026.06): V3赛道动量Spearman ρ=0.527 (p<0.001),
  五等分Q1→Q5涨幅完全单调(+2.4%→+142.8%), 多空收益差154%。
  V3三子维度(产业链+兑现度+资金关注)+essence精华信息(卡位/催化/多空论据/质量红线/催化时效),
  自动适配OpenAI API协议, 失败时自动重试。
  配套v5辩论选股系统: 增量信息采集+hybrid海选+claim驱动辩论, 回测T5 +30.42%。
tags:
  - fundamental-analysis
  - 基本面分析
  - sector-alpha
  - 赛道动量
  - stock-scoring
  - 股票打分
  - A-share
  - A股
  - AI-stocks
  - claude-code
  - backtest-verified
  - 回测验证
  - v3
  - debate-picker
  - 辩论选股
metadata:
  openclaw:
    requires:
      env: [TA_API_KEY, TA_BASE_URL]
      bins: [python3]
    emoji: "📊"
---

# 基本面 V3 评分 & 赛道Alpha选股

基于 LLM 的三子维度评分 + 精华信息提取。**一次 LLM 调用同时产出评分和 essence，零边际成本**。

## 🎯 回测结论（544只A股，2025.12-2026.06）

| 发现 | 数据 |
|:---|:---|
| V3 赛道动量 vs 半年涨幅 | Spearman **ρ = 0.527** (p<0.001) |
| V3 产业链位置 vs 涨幅 | ρ = 0.495 |
| V3 资金关注度 vs 涨幅 | ρ = 0.489 |
| V3 业绩兑现度 vs 涨幅 | ρ = 0.378 |
| 五等分 Q1→Q5 涨幅 | **+2.4% → +6.1% → +39.4% → +78.2% → +142.8%** |
| 多空收益差 | **+154.1%** |

核心洞察：赛道动量是唯一有效的选股信号。V3 三子维度（chain + delivery + capital）在全量样本上都显著有效（ρ 均 > 0.37），不要砍维度。

## 🎯 快速上手

**直接对我说：**
- "跑一下全量 V3 打分"
- "挑 10 支赛道动量最强的股票"
- "回测一下 V3 评分和半年涨幅相关性"
- "更新 fundamentals 后重新打分"

## 🐍 核心脚本

```bash
# 全量 V3 打分（544只，8并发，~35分钟）
# 产出: .fundamental_v3_scores.json (评分+essence精华)
uv run python3 _v3_full_score.py

# 全量回测（V3 vs 半年涨幅 Spearman ρ）
uv run python3 _v3_full_backtest.py
```

## V3 三子维度（小数分 0.0-25.0）

| 维度 | 范围 | 评分锚点 |
|:---|:---:|:---|
| chain 产业链位置 | 0.0-10.0 | AI 算力核心(1.6T光模块/HBM/CoWoS/AI主芯片)→8.5-10；次核心(PCB/铜连接/液冷)→6.5-8.4；半导体设备材料→5.0-6.4；旧赛道退潮(锂电/白酒/地产)→0 |
| delivery 业绩兑现度 | 0.0-10.0 | 顶级大客户(英伟达/谷歌/华为/苹果)+产能扩张→8-10；有客户业绩放量→5.5-7.9；有客户未放量→3-5.4；只有概念无订单→0-2.9 |
| capital 资金关注度 | 0.0-5.0 | AI 算力主线→4-5；国产算力/半导体设备/机器人→2.5-3.9；消费电子/汽车电子→1.5-2.4；冷门→0-1.4 |

**sector_score = chain + delivery + capital**（程序自动计算，不信任 LLM 的加法）

## essence 精华信息（6 字段，服务下游30天辩论）

每次 V3 打分同步产出，为 `debate_picker_v5.py` 提供弹药：

| 字段 | 说明 | 30天辩论用途 |
|:---|:---|:---|
| chain_position | 产业链卡位 | 横向比谁更核心 → 弹性更大 |
| core_catalyst | 30天最强催化 | 竞争辩论核心比较项 |
| biggest_bull | 多头最强论据 | 辩论攻防弹药（预提炼） |
| biggest_bear | 空头最强攻击点 | 辩论攻防弹药（预提炼） |
| quality_redline | 财务质量底线 | 一票否决（暴雷不参与竞争） |
| catalyst_horizon | near/mid/far | 30天窗口过滤，far 降权 |

## ⚙️ 前置条件

1. **fundamentals JSON** — `fundamentals/{code}.json`，通过 `_gen_top500_fundamentals.py` 生成
2. **LLM API** — `.env` 中 `TA_API_KEY` + `TA_BASE_URL`
3. **STOCKAPI_TOKEN** — `.env` 中配置，用于行业数据（`_fetch_industry_cache.py`）

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `fundamental_scorer.py` | V3 评分引擎（Prompt + LLM 调用 + JSON 解析 + 规则引擎 fallback） |
| `_v3_full_score.py` | ★ 全量 V3 打分 + essence（8 并发+断点续跑） |
| `_v3_full_backtest.py` | ★ V3 全量 vs 半年涨幅 Spearman 回测 |
| `.fundamental_v3_scores.json` | ★ V3 评分缓存（544只，含 essence），唯一权威缓存 |
| `.fundamental_llm_scores.json` | V2 评分缓存（历史参考，不再使用） |
| `fundamental_agent.py` | 基本面数据生成 Agent |
| `_gen_top500_fundamentals.py` | 个股基本面 JSON 生成器 |

## 🔧 环境变量

| 变量 | 说明 | 必须 |
|:---|:---|:---:|
| `TA_API_KEY` | LLM API Key | ✅ |
| `TA_BASE_URL` | LLM API Base URL | ✅ |
| `TA_LLM_QUICK` | 模型名（默认 deepseek-v4-pro） | ✅ |
| `STOCKAPI_TOKEN` | StockAPI Token（行业数据） | — |
| `V3_WORKERS` | 并发数（默认 8） | — |

## 辩论选股系统 v5

V3 评分产出 Top50 + essence 后，由 v5 辩论系统进行 30 天涨幅竞争排序：

```bash
# 30天辩论选股（LangGraph 7阶段）
uv run python3 debate_picker_v5.py

# 海选模式A/B对照回测
uv run python3 _screen_mode_ab_backtest.py --windows 3 --rounds 2
```

### 七阶段流水线

1. **增量信息采集** — 实时财务 + 新闻 + K线10日走势 + 资金流5日明细
2. **三分析师报告** — 技术面/资金面/基本面（注入增量信息）
3. **海选(hybrid)** — V3 Top-6保送 + 4个LLM海选名额
4. **三轮claim辩论** — 建claim → 反驳证据 → 定排序
5. **终极PK** — 条件性排名调整(±3位，需硬证据)
6. **报告输出** — TOP10排名 + claim账本 + 调整理由

### 回测验证

| 模式 | T5平均收益 | T10平均收益 | 黑马胜率 |
|------|-----------|-----------|---------|
| promote | +30.88% | +18.60% | — |
| **hybrid** | **+30.42%** | **+22.14%** | **100%** |

详细设计见 `DEBATE_SYSTEM_DESIGN.md`。