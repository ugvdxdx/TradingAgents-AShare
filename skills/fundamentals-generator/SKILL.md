---
name: fundamentals-generator
version: 1.0.0
description: >-
  A股个股基本面 JSON 生成器(三源融合)。融合 Tushare 真实财报 + research.db 板块研报 +
  防污染规则，生成 fundamentals/{code}.json。解决财务数字靠LLM回忆不准(曾把营收153亿
  错记成107亿)、研报完全未注入、自媒体乐观叙事污染(虚假"一供份额40%+")三大问题。
  产出供 V3 评分(picker/scoring/v3_full_score.py)和辩论选股(picker/pipeline/debate_picker_v5.py)消费。
tags:
  - fundamental-analysis
  - 基本面生成
  - tushare
  - 研报注入
  - 防污染
  - A股
  - claude-code
metadata:
  openclaw:
    requires:
      env: [TA_API_KEY, TA_BASE_URL, TUSHARE_TOKEN]
      bins: [python3]
    emoji: "🔬"
---

# 基本面生成器（三源融合）

生成 `fundamentals/{code}.json`，作为 V3 评分和辩论选股的数据基础。核心是**三源融合 + 防污染**。

## 🎯 三源融合架构

```
Tushare 真实财报 (picker/data/fundamentals_data.py)
  └→ 直接填 financial_health.key_metrics (营收/净利/毛利率/ROE等绝对值, 100%准确)
research.db 板块研报 (consumer.get_industry_research_brief)
  └→ 注入 prompt, 标注 [信源:中·板块级] (冷门股也能获得板块视角)
防污染规则 (SYSTEM_PROMPT)
  └→ 强断言信源分级, 低可信且矛盾的直接删除 (杜绝"一供份额40%+"类污染)
       ↓
  fundamentals/{code}.json
```

## 🎯 快速上手

**直接对我说：**
- "重生成全量基本面"
- "重生成 603308 和 300394 的基本面"
- "只刷新 603308 的财务数据"（用 `--fh-only`）

## 🐍 核心命令

```bash
# 全量重生成 (544只, 约544×3分钟, 含Tushare财报+研报+防污染)
uv run python3 picker/pipeline/gen_fundamentals.py --force

# 只重生成指定股票
uv run python3 picker/pipeline/gen_fundamentals.py --codes 603308,300394 --force

# 只补缺失的(不覆盖已有)
uv run python3 picker/pipeline/gen_fundamentals.py

# 试跑前 N 只验证
uv run python3 picker/pipeline/gen_fundamentals.py --count 10

# 从指定代码开始(断点续跑)
uv run python3 picker/pipeline/gen_fundamentals.py --start-from 600019

# 只刷新财务数据(慢层增量, 比全量便宜)
uv run python3 picker/pipeline/gen_fundamentals.py --codes 603308 --fh-only

# 干跑(只打印不生成)
uv run python3 picker/pipeline/gen_fundamentals.py --dry-run
```

## 📋 参数说明

| 参数 | 说明 |
|:---|:---|
| `--codes 603308,300394` | 只生成指定代码(逗号分隔) |
| `--force` | 覆盖已有 fundamentals JSON(不加则跳过已存在) |
| `--count N` | 只处理前 N 只 |
| `--start-from 600019` | 从指定代码开始(跳过之前的, 用于断点续跑) |
| `--fh-only` | 只刷新 financial_health 部分(Tushare财报, 比全量便宜) |
| `--dry-run` | 只打印待生成列表, 不实际调用 LLM |

## 🛡️ 防污染规则（核心）

LLM 生成时对「一供/份额XX%/锁定/独家/唯一」等强断言强制信源分级：

| 信源 | 定义 | 处理 |
|:---|:---|:---|
| **高** | 公司公告/财报/券商深度研报/权威媒体 | 可作为硬事实 |
| **中** | 行业媒体/产业数据库/券商晨会 | 可信但需交叉验证 |
| **低** | 雪球/股吧/东方财富号/自媒体/推测 | **低可信且与他源矛盾则删除** |

**删除规则**：信源低且满足任一条件 → 整条删除（不降级保留）：
- 与更高信源事实矛盾
- 是核心多头逻辑的关键支点（失实会扭曲判断）
- 把"送样测试/规划"表述成已确定事实（如"送样中"写成"已锁定一供"）

## ⚙️ 前置条件

1. **股票清单** — `_top500_and_leaders.txt`（代码/名称/行业/市值）
2. **Tushare** — `.env` 中 `TUSHARE_TOKEN`（财报数据，失败时回退LLM填）
3. **research.db** — 存在则有板块研报注入，不存在则跳过（不崩）
4. **LLM API** — `.env` 中 `TA_API_KEY` + `TA_BASE_URL`
5. **世界知识** — `_world_knowledge_2026_06.md`（宏观地缘评估用）

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `picker/pipeline/gen_fundamentals.py` | ★ 主生成脚本(SYSTEM_PROMPT含防污染规则 + build_prompt注入两源) |
| `picker/data/fundamentals_data.py` | Tushare 真实财报拉取(`fetch_real_financials`) |
| `tradingagents/research/consumer.py` | `get_industry_research_brief()` 板块研报注入 |
| `_top500_and_leaders.txt` | 股票清单(代码/名称/行业/市值), 带进度标记 |
| `_world_knowledge_2026_06.md` | 宏观世界知识(地缘评估用) |
| `fundamentals/{code}.json` | ★ 产出(544只个股基本面JSON) |

## 🔧 环境变量

| 变量 | 说明 | 必须 |
|:---|:---|:---:|
| `TA_API_KEY` | LLM API Key | ✅ |
| `TA_BASE_URL` | LLM API Base URL | ✅ |
| `TA_LLM_DEEP` | 深度模型名 | ✅ |
| `TUSHARE_TOKEN` | Tushare Token(财报) | ✅(财务准确性保障) |

## 📐 产出 JSON 结构

```json
{
  "code": "603228", "name": "景旺电子",
  "business_overview": {"what_they_do": "...", "industry": "...", "industry_position": "..."},
  "competitive_analysis": {"strengths": ["[信源:高]..."], "weaknesses": [...], "moat_level": "高"},
  "financial_health": {"key_metrics": {营收/净利/毛利率/ROE等来自Tushare}, "highlights": [...], "risks": [...]},
  "growth_assessment": {"growth_score": 7.5, "growth_drivers": [...], "headwinds": [...]},
  "geopolitical_assessment": {"risks": [...], "opportunities": [...], "industry_momentum": [...]},
  "summary": "..."
}
```

## 🔗 下游消费

- **V3 评分** — `picker/scoring/v3_full_score.py` 读 JSON 全文打分(见 fundamentals-scorer skill)
- **辩论选股** — `picker/pipeline/debate_picker_v5.py` 读 essence + financial_health(见 debate-picker skill)
