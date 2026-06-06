---
name: "fundamentals-generator"
description: "Generates high-quality stock fundamental analysis JSON files with precise financial data, named competitors, and individualized risk factors. Invoke when user asks to generate/regenerate fundamental files for stocks."
---

# Fundamentals Generator

Generate high-quality stock fundamental analysis JSON files for the J-TradingAgents project.

## Core Principle: Quality Over Speed

**ALWAYS generate ONE file at a time.** Never batch-generate multiple files. Each file requires:
1. Real data search (WebSearch) before writing
2. Precise financial figures (not rounded estimates)
3. Named competitors with market share data
4. Individualized risk factors (not generic templates)

## File Location & Format

- Output path: `/Users/bilibili/Desktop/J-TradingAgents/fundamentals/<stock_code>.json`
- Reference template: Read an existing high-quality file (e.g., `002281.json` or `605117.json`) before generating
- World knowledge: `/Users/bilibili/Desktop/J-TradingAgents/_world_knowledge_2026_06.md`

## JSON Structure

```json
{
  "code": "6位股票代码",
  "name": "股票名称",
  "fetch_date": "YYYY-MM-DDT00:00:00",
  "market": "沪市/深市",
  "business_overview": {
    "what_they_do": "详细业务描述，含具体财务数据",
    "industry": "行业分类（细分行业）",
    "industry_position": "市场地位，含具体市占率和排名"
  },
  "competitive_analysis": {
    "strengths": ["5条具体优势，含数据"],
    "weaknesses": ["5条具体劣势，含数据"],
    "moat_level": "低/中/中高/高"
  },
  "financial_health": {
    "key_metrics": {
      "revenue_yi": 0.0,
      "net_profit_yi": 0.0,
      "gross_margin_pct": 0.0,
      "net_margin_pct": 0.0,
      "roe_pct": 0.0,
      "debt_ratio_pct": 0.0,
      "rd_ratio_pct": 0.0,
      "rd_expense_yi": 0.0,
      "operating_cf_yi": 0.0,
      "cf_to_profit": 0.0
    },
    "health_rating": "健康/一般/较差",
    "benchmark_ref": "行业基准",
    "highlights": ["4条财务亮点"],
    "risks": ["4条财务风险"]
  },
  "growth_assessment": {
    "growth_score": 0.0,
    "growth_drivers": ["5条增长驱动"],
    "headwinds": ["5条增长阻力"]
  },
  "geopolitical_assessment": {
    "risks": ["4条地缘风险，必须引用世界知识"],
    "opportunities": ["4条地缘机会，必须引用世界知识"],
    "industry_momentum": ["3条行业趋势"]
  },
  "summary": "200-300字总结，含核心优势+主要风险+关键数据"
}
```

## Generation Workflow (STRICT ORDER)

### Step 1: Read Reference Files
- Read `_world_knowledge_2026_06.md` for latest macro/industry data
- Read 1-2 existing high-quality fundamental files as format reference

### Step 2: Search Real Data (MANDATORY)
Use WebSearch to find the following for the target stock. Run 3-5 parallel searches:

1. **Financial data search**: `"<stock_name> <stock_code> 2025年报 营收 净利润 毛利率 资产负债率 财务数据"`
2. **Quarterly data search**: `"<stock_name> <stock_code> 2026年一季报 业绩 营收 净利润"`
3. **Competition search**: `"<stock_name> <stock_code> 市场份额 竞争对手 行业排名 2025"`
4. **Business detail search**: `"<stock_name> <stock_code> 业务结构 主营业务 2025年报"`

### Step 3: Cross-Validate Data
- Compare data from multiple sources (东方财富/雪球/新浪/公司公告)
- If data conflicts, prefer: 官方年报 > 交易所公告 > 权威财经媒体 > 分析师研报
- Always use the most recent available data (2025年报 + 2026Q1 if available)

### Step 4: Write the JSON File
Follow these quality standards strictly:

## Quality Standards (NON-NEGOTIABLE)

### 1. Financial Data Must Be PRECISE
- **GOOD**: `"2025年营收119.3亿（+44.2%）"`, `"毛利率51.1%"`, `"研发费用244.75亿元，占营收18.28%"`
- **BAD**: `"营收超百亿"`, `"毛利率约50%"`, `"研发投入较高"`
- Always include: absolute value + YoY change + percentage where applicable

### 2. Competitive Analysis Must Be NAMED
- **GOOD**: `"与中际旭创（全球第一28%）、新易盛（全球第二15-18%）同列中国光模块第一阵营"`
- **BAD**: `"行业竞争激烈，公司面临国内外竞争对手挑战"`
- Every competitor must have: company name + market share/ranking + differentiation

### 3. Risk Factors Must Be INDIVIDUALIZED
- **GOOD**: `"存货74.8亿激增（+30%），跌价准备3.1亿，若需求放缓风险巨大"`
- **BAD**: `"行业竞争加剧可能影响公司盈利能力"`
- Every risk must have: specific data point + mechanism + potential impact

### 4. Geopolitical Assessment Must Reference World Knowledge
- Must incorporate data from `_world_knowledge_2026_06.md`:
  - 伊朗战争: 布伦特原油$93.09/桶, 霍尔木兹海峡通行受限
  - 中美贸易战: 加权平均实际税率约21.6%, 普通工业品税率约37.5%
  - AI革命: 全球数据中心投资7880亿美元（+56%）
  - 新能源: 中国新能源渗透率62.5%
  - 稀土出口管制: 对日断供, 日本库存预计2026年8-10月见底
- Each risk/opportunity must connect world knowledge to the specific stock

### 5. Business Overview Must Be DATA-RICH
- Include: revenue breakdown by segment with absolute values + YoY growth + margins
- Include: latest quarterly data (2026Q1 if available)
- Include: specific product/technology differentiators with data

### 6. Summary Must Be CONCISE But COMPLETE
- 200-300 characters
- Must include: core competitive advantage + main risk + key financial data + growth outlook
- Format: `"<公司>是<定位>。<核心财务数据>。核心优势：①...②...③...。主要风险：①...②...③...。<增长展望>。"`

## Common Mistakes to Avoid

1. **Never use vague language**: "较大影响" → "影响约X亿元/降低毛利率X个百分点"
2. **Never copy template text**: Each file must be individually crafted based on real data
3. **Never skip the data search**: Even if you think you know the company, search for latest data
4. **Never mix up tariff data**: 美国对华累计关税145%, 中方反制关税125%, 加权平均实际税率约21.6%
5. **Never use outdated data**: Always verify data is from 2025年报 or later
6. **Never generate multiple files at once**: One file per generation cycle, with full data search

## Geopolitical Data Quick Reference

When writing `geopolitical_assessment`, always incorporate these from world knowledge:

| Topic | Key Data Points |
|-------|----------------|
| 伊朗战争 | 布伦特$93.09/桶, 霍尔木兹海峡受限, 6月5-6日美伊再次交火 |
| 中美贸易战 | 加权平均实际税率21.6%, 普通工业品37.5%, 中美基准关税10%至11月 |
| AI革命 | 数据中心投资7880亿美元(+56%), HBM/CoWoS全线短缺, 端侧AI设备12亿台(+55%) |
| 新能源 | 中国渗透率62.5%, 储能需求爆发, 锂价16.4-18.2万/吨(+67%) |
| 稀土管制 | 对日断供镝/铽, 日本库存8-10月见底, 潜在损失2.6万亿日元 |
| 波音 | 125%关税暂停交付, 引进200架波音飞机协议 |
| 人民币 | USD/CNY从7.35回落至7.15-7.20 |

## File Validation Checklist

Before writing the file, verify:
- [ ] All financial data has absolute values (not just percentages)
- [ ] All competitors are named with market share/ranking
- [ ] All risk factors have specific data points
- [ ] Geopolitical section references world knowledge data
- [ ] Summary is 200-300 characters with key data
- [ ] No generic/template language used
- [ ] Latest quarterly data (2026Q1) included if available
- [ ] Business overview includes segment breakdown with margins
