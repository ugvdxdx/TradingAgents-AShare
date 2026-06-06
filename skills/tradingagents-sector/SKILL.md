---
name: tradingagents-sector
version: 0.2.0
description: >-
  A股板块多智能体分析工具 — 6名AI分析师协作完成板块数据采集、
  多维度分析、多空辩论、风控裁决，输出含短/中/长期趋势预测的结构化研报。
  支持"商业航天"、"半导体设备"、"人工智能"等板块关键词。
  China A-share sector multi-agent analysis tool.
tags:
  - sector-analysis
  - 板块分析
  - 概念板块
  - 行业板块
  - 板块轮动
  - A-share
  - A股
  - 股票
  - investment
  - trading
  - finance
  - China
  - multi-agent
  - 多智能体
  - 趋势预测
  - claude-code
---

# 板块分析 Sector Analysis

6名AI分析师协作，对A股概念/行业板块进行深度分析、多空辩论与趋势预测，研报自动归档。

## 🎯 快速上手

**直接对我说：**
- "分析一下商业航天板块"
- "今天哪些概念板块涨幅最大"
- "半导体设备板块成分股有哪些"
- "板块轮动情况怎么样"
- "002371属于哪些概念板块"

## 🤖 核心功能

| 功能 | 命令 | 说明 |
|------|------|------|
| 板块搜索 | `sector_analysis.py search <关键词>` | 按关键词匹配概念/行业板块 |
| 板块排名 | `sector_analysis.py rank [top_n]` | 当日概念板块涨跌幅排名 |
| 成分股分析 | `sector_analysis.py stocks <板块代码>` | 板块内个股涨跌/资金排名 |
| 个股归属 | `sector_analysis.py belong <股票代码>` | 反查个股所属概念/行业板块 |
| 板块资金流 | `sector_analysis.py fund_flow` | 行业板块资金流向排名 |
| 数据分析 | `sector_analysis.py analysis <关键词>` | 一站式板块数据综合分析 |
| **深度分析** | `sector_analysis.py deep <关键词>` | **6智能体协作：分析+辩论+裁决+研报** |

## ⚙️ 配置

无需额外配置，直接使用项目虚拟环境：

```bash
cd /path/to/J-TradingAgents
.venv/bin/python skills/tradingagents-sector/scripts/sector_analysis.py <command> [args]
```

## 🚀 使用方式

### 方式一：直接运行 Python 脚本

```bash
# 搜索板块
.venv/bin/python skills/tradingagents-sector/scripts/sector_analysis.py search 航天

# 概念板块排名（前20）
.venv/bin/python skills/tradingagents-sector/scripts/sector_analysis.py rank 20

# 查看板块成分股
.venv/bin/python skills/tradingagents-sector/scripts/sector_analysis.py stocks BK0903

# 个股所属板块
.venv/bin/python skills/tradingagents-sector/scripts/sector_analysis.py belong 002371

# 一站式综合分析
.venv/bin/python skills/tradingagents-sector/scripts/sector_analysis.py analysis 商业航天
```

### 方式二：通过 Shell 脚本（支持 API 模式）

```bash
# 本地分析（默认）
bash skills/tradingagents-sector/scripts/sector_analysis.sh 商业航天

# API 模式（需配置 TRADINGAGENTS_TOKEN）
TRADINGAGENTS_TOKEN=ta-sk-xxx \
bash skills/tradingagents-sector/scripts/sector_analysis.sh 商业航天 --api
```

## 📋 分析框架

板块分析报告包含以下维度：

1. **板块定位**：板块代码、名称、所属分类（概念/行业/地域）
2. **当日表现**：涨跌幅、上涨/下跌家数、成交额、领涨股
3. **资金流向**：主力净流入/流出、板块间资金迁移
4. **成分股分析**：涨幅TOP10、跌幅TOP5、成交额TOP10
5. **龙头识别**：板块内市值最大/涨幅最大/成交最活跃的个股
6. **板块轮动**：近5日强势板块对比、资金偏好变化

## 🔒 数据源

- **东财 push2**：概念板块/行业板块列表、成分股、资金流向
- **百度股市通**：个股概念归属（行业/概念/地域三维）
- **东方财富 datacenter**：板块资金流向明细

## 📊 示例输出

```
=== 板块综合分析：商业航天 ===

【板块定位】
名称: 商业航天    代码: BK0903    类型: 概念板块

【今日表现】
涨跌幅: +1.25%    上涨家数: 62    下跌家数: 271
总成交额: 3224.88亿元    领涨股: 光启技术(+4.42%)

【成分股涨幅TOP5】
1. 铖昌科技 001270  +7.45%  成交28.04亿
2. 顺络电子 002138  +7.64%  成交15.32亿
3. 风华高科 000636  +6.04%  成交8.45亿
4. 光启技术 002625  +4.42%  成交18.92亿
5. 长盈通 301091  +3.59%  成交6.21亿

【资金流向】
主力净流入: -9713.71万元  游资净流入: +1.38亿元
散户净流入: +2754.5万元

【龙头识别】
- 市值龙头: 中国卫星 600118  市值969.64亿
- 涨幅龙头: 铖昌科技 001270  +7.45%
- 活跃龙头: 铖昌科技 001270  换手率8.89%
```

## ⚠️ 注意事项

- 板块数据依赖东财/百度实时API，非交易时间数据可能为空
- 概念板块代码（如 BK0903）可通过 `search` 命令获取
- 成分股列表可能随市场调整而变化
