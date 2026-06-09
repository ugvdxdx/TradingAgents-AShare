---
name: fundamentals-scorer
version: 2.0.0
description: >-
  A股基本面评分与赛道Alpha选股工具。
  回测验证(539只A股, 2025.12-2026.06): 赛道动量分Spearman ρ=0.56 (p<0.001),
  五等分Q1→Q5涨幅单调递增(-1.6%→+134.8%), 多空收益差112%。
  提供 V1(纯AI主线) 和 V2(赛道Alpha+基本面风控) 两套Prompt,
  自动适配 Anthropic/OpenAI API 协议, LLM不可用时回退规则引擎。
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
metadata:
  openclaw:
    requires:
      env: [TA_API_KEY, TA_BASE_URL]
      bins: [python3]
    emoji: "📊"
---

# 基本面评分 & 赛道Alpha选股

基于回测验证的双维度评分：**赛道动量是Alpha信号，基本面质量是风控底线**。

## 🎯 回测结论（539只A股，2025.12-2026.06）

| 发现 | 数据 |
|:---|:---|
| 赛道动量分 vs 半年涨幅 | Spearman **ρ = 0.556** (p<0.001) |
| 基本面分 vs 半年涨幅 | ρ = 0.039 (不显著) |
| 五等分 Q1→Q5 涨幅 | **-1.6% → +1.1% → +13.8% → +61.8% → +134.8%** |
| 赛道Top20 均涨幅 | **+109.78%** |
| 赛道Bottom20 均涨幅 | -2.31% |
| 多空收益差 | **+112.09%** |
| 基本面过滤(≥10)提升信号 | ρ 0.556 → **0.580** |

核心洞察：赛道动量是唯一有效的选股信号，基本面分的作用是排除垃圾股（亏损/财务危机/纯概念），而非选牛股。

## 🎯 快速上手

**直接对我说：**
- "给 600519 打基本面分" 
- "挑 10 支赛道动量最强的股票，排除基本面不达标的"
- "给 fundamentals 文件夹里所有股票打分，按赛道排名"
- "回测一下这批股票的评分和半年涨幅相关性"

## 🐍 Python API（推荐用法）

```python
from fundamental_scorer import (
    compute_sector_alpha,              # ★ 推荐：赛道Alpha + 基本面风控
    compute_fundamental_knowledge_v2,  # V2 双轨制详情
    compute_fundamental_knowledge,     # V1 纯AI主线（保留兼容）
    compute_fundamental_knowledge_both, # V1+V2 对比
    FUNDAMENTAL_MIN_THRESHOLD,         # 风控阈值 (默认10)
)

# ★ 赛道Alpha选股（回测验证的推荐用法）
result = compute_sector_alpha("300502")
# → {
#     "sector_score": 24,           # Alpha信号 (0-25), 按此排名
#     "fundamental_score": 22,      # 风控分数 (0-25), <10 排除
#     "filter_pass": True,          # 是否通过风控
#     "recommendation": "BUY",      # BUY | WATCH | PASS
#     "brief": "AI算力核心，业绩高增，全球光模块龙头"
#   }

# 批量选股流程
# 1. 遍历股票池，调用 compute_sector_alpha
# 2. 过滤 filter_pass=False （基本面<10的垃圾股）
# 3. 按 sector_score 降序排名 （唯一有效的Alpha信号）
# 4. sector≥15 → BUY, 8-14 → WATCH, <8 → PASS
```

## 🔬 推荐逻辑（赛道Alpha + 基本面风控）

| 层级 | 维度 | 满分 | 作用 | 回测ρ |
|:---|:---|:---:|:---|:---:|
| **Alpha 信号** | 赛道动量 | 25 | 排名选股 | **0.556** ★★★ |
| **风控底线** | 产业链位置 + 业绩兑现 + 资金关注 | | | |
| **风控过滤** | 基本面质量 | 25 | 排除垃圾 | 0.039 |
| | 盈利能力 + 护城河 + 财务安全 | | 阈值 ≥ 10 | |

### 四象限分布

```
赛道动量 ↑
  25 ┃  ❌排除区        ★BUY区
     ┃ (基本面<10,     (高赛道+好基本面)
  15 ┃  赛道再高也不碰)  
     ┃
   8 ┃  ❌PASS         👀WATCH
     ┃ (双低)          (好基本面但赛道弱)
   0 ┃
     ┗━━━━━━━━━━━━━━━━━━━━━━
     0        10        25  基本面质量
```

### 关键规则
- 赛道为旧赛道退潮品种（锂电/白酒/地产/矿业）→ 诚实给 0 分
- 基本面 < 10 → `filter_pass=False`，不参与赛道排名
- 赛道 ≥ 15 且通过风控 → `BUY`
- 赛道 8-14 且通过风控 → `WATCH`

## 🤖 V1 评分模式（AI主线匹配，保留兼容）

| 维度 | 满分 | 评估内容 |
|:---|:---:|:---|
| 产业链位置 | 18 | 是否在AI算力/半导体/消费电子链的关键节点？ |
| 业绩兑现度 | 16 | 有无具体订单/产能/大客户验证？ |
| 行业动量 | 10 | 当前是否处于资金关注焦点？ |
| 旧赛道惩罚 | 6 | 锂电/白酒/地产/矿业/银行 → 此项0分+总分扣5分 |

V1 与半年涨幅的 Spearman ρ = 0.477，略低于赛道动量分的 0.556，保留作为对照。

## 📜 批量脚本

```bash
# 全量打分（545只 → .fundamental_scores_batch.json）
uv run python3 skills/fundamentals-scorer/scripts/batch_score.py

# 相关性回测（评分 vs 半年涨幅）
uv run python3 skills/fundamentals-scorer/scripts/backtest_correlation.py
```

## ⚙️ 前置条件

1. **fundamentals JSON** — `fundamentals/{code}.json`，通过 `fundamental_agent.analyze_one(code, name)` 生成
2. **LLM API** — `.env` 中 `TA_API_KEY` + `TA_BASE_URL`，自动适配 Anthropic/OpenAI 协议
3. **风控阈值** — `FUNDAMENTAL_MIN_THRESHOLD = 10`，可在调用时覆盖

## 🔄 退避链

```
LLM (V2 Prompt, Anthropic/OpenAI 协议自动检测)
  ↓ 失败
规则引擎 (_rule_based_score, 纯规则 0-50, ρ=0.137)
  ↓ fundamentals JSON 不存在
None → 调用方退回到行业分
```

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `fundamental_scorer.py` | 核心评分模块（Prompt + LLM + 规则引擎 + 赛道Alpha） |
| `fundamental_agent.py` | 基本面数据生成（财务 + 业务画像 → `fundamentals/`） |
| `skills/fundamentals-scorer/scripts/batch_score.py` | 全量批量打分 |
| `skills/fundamentals-scorer/scripts/backtest_correlation.py` | 评分 vs 涨幅回测 |
| `.fundamental_scores_batch.json` | 545只全量评分缓存 |
| `.backtest_correlation.json` | 回测结果缓存 |

## 🔧 环境变量

| 变量 | 说明 | 必须 |
|:---|:---|:---:|
| `TA_API_KEY` | LLM API Key | ✅ |
| `TA_BASE_URL` | API Base URL（Anthropic/OpenAI 自动适配） | ✅ |
| `TA_LLM_QUICK` | 模型名（默认 gpt-4o-mini） | — |