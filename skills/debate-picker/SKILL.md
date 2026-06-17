---
name: debate-picker
version: 1.0.0
description: >-
  A股30天涨幅竞争辩论选股系统 v5(LangGraph编排)。从V3 Top50出发, 经7阶段流水线
  (增量信息采集→三分析师→hybrid海选→claim驱动辩论→终极PK→风控)筛选至TOP10。
  核心创新: 增量信息层(实时财务+新闻+K线+资金流)、hybrid海选(V3保送6+海选4,
  回测黑马100%胜率)、claim驱动辩论(强制证据引用)、条件性排名调整(±3位需硬证据)。
tags:
  - stock-picking
  - 选股
  - debate
  - 辩论
  - langgraph
  - claim-driven
  - hybrid-screening
  - A股
  - claude-code
metadata:
  openclaw:
    requires:
      env: [TA_API_KEY, TA_BASE_URL]
      bins: [python3]
    emoji: "🏆"
---

# 辩论选股 v5（LangGraph 7阶段）

从 V3 Top50 出发，通过多智能体辩论筛选至最终 TOP10 排名。核心命题：**未来30天谁涨更多**（相对排名，非涨跌判断）。

## 🎯 快速上手

**直接对我说：**
- "跑一下选股"
- "用 06-16 的数据选股"
- "评分完接着选股"
- "验证一下选股管道"（dry-run）

## 🐍 核心命令

```bash
# 实盘选股(今日, Top50→Top10, 约18分钟)
uv run python3 debate_picker_v5.py --top-n 50

# 指定日期(回测/缓存数据)
uv run python3 debate_picker_v5.py --date 2026-06-16 --top-n 50

# 调整辩论轮次(默认3轮)
uv run python3 debate_picker_v5.py --rounds 2

# 验证管道(跳过LLM, 仅验证数据流)
uv run python3 debate_picker_v5.py --dry-run
```

## 📋 参数说明

| 参数 | 默认 | 说明 |
|:---|:---|:---|
| `--date` | 今日 | 交易日(实盘=当日, 回测=截止日) |
| `--top-n` | 50 | 候选股规模(从V3评分取Top-N) |
| `--rounds` | 3 | claim驱动辩论轮次上限 |
| `--dry-run` | — | 跳过LLM, 仅验证数据管道 |

## 🔄 七阶段流水线

```
① 数据采集 (collect_data)
   └ V3 Top50 + 技术面(K线) + 资金流(5日主力)
② 增量信息采集 (incremental_info)
   └ 实时财务 + 新闻 + K线10日走势 + 资金流明细 + 研报信号 + LLM事件摘要
③ 三分析师并行 (technical/fund/fundamental)
   └ 注入增量信息, 产出三份报告
④ 海选 hybrid (screen_round1)
   └ V3 Top-6保送 + 4个LLM海选名额 → 决赛10只
⑤ claim驱动辩论 (debate_round, 最多3轮)
   └ 多头建claim(强制证据) → 空头反驳(5种精准打击) → 收敛
⑥ 终极PK (final_judge)
   └ 条件性排名调整(±3位, 需硬证据), 输出TOP10
⑦ 风控+报告 (risk_review + report_render)
   └ 可信度评估 + 风险标签 + 终端报告 + 全过程落盘
```

## 🎯 核心创新

| 机制 | 说明 |
|:---|:---|
| **增量信息层** | 实时财务(akshare) + 新闻(按名称搜索+WebSearch缓存) + K线明细 + 资金流明细 |
| **hybrid海选** | V3 Top-6保送守住龙头 + 4个海选名额让黑马进入 |
| **claim驱动辩论** | 多头强制证据引用(日期/数值), 空头5种精准打击(催化过时/资金背离/量价背离/高位透支/增速证伪) |
| **条件性排名调整** | 仅当有硬证据时才调整(±3位), 避免空头误杀动量龙头 |

## 📊 回测验证（2窗口）

| 模式 | T5平均收益 | T10平均收益 | 黑马胜率 |
|------|-----------|-----------|---------|
| promote (V3保送) | +30.88% | +18.60% | — |
| llm (全海选) | +15.07% | +15.16% | 0% |
| **hybrid (保送6+海选4)** | **+30.42%** | **+22.14%** | **100%** |

## ⚙️ 前置条件

1. **V3 评分** — `.fundamental_v3_scores.json`(见 fundamentals-scorer skill), 从中取 Top-N
2. **fundamentals JSON** — `fundamentals/{code}.json`(见 fundamentals-generator skill)
3. **K线缓存** — `kline_cache/*.pkl`(技术面)
4. **资金流缓存** — `.mf_cache/*.pkl`(`fetch_money_flow_all.py` 预拉取)
5. **research.db** — 存在则注入研报信号/黑马/风险, 不存在则跳过
6. **LLM API** — `.env` 中 `TA_API_KEY` + `TA_BASE_URL`

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `debate_picker_v5.py` | ★ CLI 入口 |
| `tradingagents/agents/picker/picker_graph.py` | LangGraph 图编排(7节点+条件边) |
| `tradingagents/agents/picker/picker_state.py` | 状态 schema(候选/claim账本/排名) |
| `tradingagents/agents/picker/incremental.py` | 增量信息采集(财务+新闻+量化信号+研报) |
| `tradingagents/agents/picker/analysts.py` | 三分析师节点 + 数据采集 |
| `tradingagents/agents/picker/judges.py` | 海选(hybrid) + 终极PK(条件性调整) |
| `tradingagents/agents/picker/debaters.py` | claim驱动辩论节点 |
| `tradingagents/agents/picker/rotation.py` | 行业轮动感知 |
| `tradingagents/agents/picker/prompts.py` | 提示词(含AI主线认知先验) |
| `tradingagents/agents/picker/reporter.py` | 风控复核 + 报告渲染 |

## 📂 产出落盘

每次运行产出到 `results/picker_v5/{trade_date}/`：

| 文件 | 内容 |
|:---|:---|
| `01_candidates.json` | 候选股档案(V3+技术+资金流) |
| `01b_incremental.json` | 增量信息简报(财务+新闻+量化信号) |
| `02_analyst_*.md` | 三分析师报告 |
| `03_round1_screen.json` | 海选结果(hybrid模式日志) |
| `04_debate_roundN.json` | 每轮辩论claim快照 |
| `05_final_ranking.json` | ★ 最终TOP10排名 |
| `06_risk_review.json` | 可信度+风险标签 |
| `report.md` / `result.json` | 终端报告 |

## 🔧 环境变量

| 变量 | 说明 | 必须 |
|:---|:---|:---:|
| `TA_API_KEY` | LLM API Key | ✅ |
| `TA_BASE_URL` | LLM API Base URL | ✅ |
| `TA_LLM_DEEP` | 深度模型(基本面分析师/辩论/终极PK) | ✅ |
| `TA_LLM_QUICK` | 快速模型(技术/资金分析师/海选) | ✅ |
