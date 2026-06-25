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
- "全量更新基本面"（= 刷新 fundamentals/ 池内所有股票）
- "重生成 300308 的基本面"
- "刷新近 3 天有研报的基本面"

## 🐍 核心命令

```bash
# 全量重写 fundamentals/ 池内所有股票 (Web+Tushare财报+研报+防污染, 并行)
uv run python3 picker/pipeline/refresh_fundamentals.py --all --workers 5

# 只刷新指定股票
uv run python3 picker/pipeline/refresh_fundamentals.py --stock 300308

# 断点续跑(跳过近 6h 已刷新的, 避免重复)
uv run python3 picker/pipeline/refresh_fundamentals.py --all --skip-recent-hours 6

# 研报触发批量刷新(近3天有研报提及的个股)
uv run python3 picker/pipeline/refresh_fundamentals.py

# 跳过网络搜索(省 web search 额度, 仅用 Tushare+研报+LLM)
uv run python3 picker/pipeline/refresh_fundamentals.py --all --no-web
```

## 📋 参数说明

| 参数 | 说明 |
|:---|:---|
| `--all` | 全量重写 fundamentals/ 池内所有股票 |
| `--stock 300308` | 只刷新指定代码 |
| `--skip-recent-hours 6` | 跳过最近 N 小时已刷新的(断点续跑) |
| `--days 3` | 研报触发模式下, 只处理近 N 天有研报提及的(默认3) |
| `--max N` | 最多刷新 N 只(0=全部) |
| `--no-web` | 跳过网络搜索(省 web search 额度) |
| `--no-v3` | 刷新后不触发 V3 重评 |
| `--workers N` | 并发线程数(默认1, LLM为IO密集建议5) |
| `--dry-run` | 只看不写 |

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

1. **股票池** — `fundamentals/` 目录下已有的 JSON（全量刷新 = 刷新这些文件；新发现股票由 discovery 模块经 `refresh_one(name_hint=...)` 生成）
2. **Tushare** — `.env` 中 `TUSHARE_TOKEN`（财报数据，失败时回退LLM填）
3. **research.db** — 存在则有板块研报注入，不存在则跳过（不崩）
4. **LLM API** — `.env` 中 `TA_API_KEY` + `TA_BASE_URL`
5. **世界知识** — `_world_knowledge_2026_06.md`（宏观地缘评估用）

## 📁 相关文件

| 文件 | 用途 |
|:---|:---|
| `picker/pipeline/refresh_fundamentals.py` | ★ 主重写脚本(REFRESH_SYSTEM_PROMPT含防污染规则 + Web+Tushare+研报注入) |
| `picker/data/fundamentals_data.py` | Tushare 真实财报拉取(`fetch_real_financials`) |
| `tradingagents/research/consumer.py` | `get_industry_research_brief()` 板块研报注入 |
| `_world_knowledge_2026_06.md` | 宏观世界知识(地缘评估用) |
| `fundamentals/{code}.json` | ★ 产出(个股基本面JSON) |

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
