# J-TradingAgents

量化多 Agent 分析框架 — 使用 LangGraph 编排 14 个专业 Agent 对 A 股/美股/港股进行综合投研分析。

## 架构概览

```
┌─────────────────────────────────────────────────────┐
│                   LangGraph 编排                     │
├──────────┬──────────┬──────────┬──────────┬─────────┤
│ 7 位分析师 │ 牛熊辩论   │ 3 位风险   │  Trader  │ PM     │
│ ──────── │ ──────── │ ──────── │ ──────── │ ────── │
│ Market   │ Bull     │ Aggressive│          │        │
│ Social   │ Bear     │ Neutral  │  交易建议  │ 组合   │
│ News     │ Manager  │ Conservative│         │ 管理   │
│ Fundamentals│       │          │          │        │
│ Macro    │          │          │          │        │
│ SmartMoney│          │          │          │        │
│ VolumePrice│          │          │          │        │
├──────────┴──────────┴──────────┴──────────┴─────────┤
│              数据供应商路由 (fallback 链)              │
│  cn_astock → cn_akshare → cn_baostock → yfinance    │
└─────────────────────────────────────────────────────┘
```

## 快速开始

### 安装

```bash
# 克隆项目
git clone https://github.com/KylinMountain/TradingAgents-AShare.git
cd TradingAgents-AShare

# 安装依赖 (推荐 uv)
uv sync

# 或 pip
pip install -e .
```

### 配置

```bash
# 复制配置模板
cp .env.example .env

# 编辑 .env，填入 LLM API Key
# TA_API_KEY=sk-your-api-key
# TA_BASE_URL=https://api.openai.com/v1
# TA_LLM_PROVIDER=openai
```

支持的环境变量见 [.env.example](.env.example)。

### 使用

```bash
# CLI — 单只股票分析
tradingagents analyze 600519.SH --date 2026-05-26
tradingagents analyze 贵州茅台
tradingagents analyze 600519 --horizon short --quick

# CLI — 自选股管理
tradingagents watchlist list
tradingagents watchlist add 600519.SH
tradingagents watchlist remove <item_id>

# CLI — 定时任务
tradingagents scheduled list
tradingagents scheduled add 600519.SH --time 20:00
tradingagents scheduled remove <item_id>

# API 服务 (供外部程序调用)
tradingagents-api

# 定时调度器 (独立进程)
tradingagents-scheduler
```

也可以通过 Python API 直接调用：

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

graph = TradingAgentsGraph(DEFAULT_CONFIG)
result = graph.run("600519.SH", "2026-05-26")
print(result["final_trade_decision"])
```

## Skills (AI 能力包)

项目内置两组可被 Claude Code / Cursor 等 AI IDE 直接调用的技能包：

| Skill | 路径 | 说明 |
|-------|------|------|
| **个股深度分析** | `skills/tradingagents-analysis/SKILL.md` | 15 个 AI 分析师五阶段协作：市场→博弈→多空辩论→交易→风控，输出买卖建议+风险评估 |
| **板块分析** | `skills/tradingagents-sector/SKILL.md` | 6 名 AI 分析师协作完成板块搜索、排名、资金流向、成分股分析，输出结构化研报 |

两种技能均可通过自然语言直接触发（例："分析贵州茅台"或"分析商业航天板块"）。

## 高级工具：选股 & 回测

| 工具 | 命令 | 说明 |
|------|------|------|
| **实时选股** | `python run_stock_picker.py` | 全 A 股 v7 评分排序，输出 TOP10 推荐 + 热门板块，每日自动生成报告 |
| **辩论选股** | `python run_debate_picker.py` | Top100 → 四轮辩论筛选至 10 只，交互式 Bull/Bear 辩论 + 投降机制 + 30日收益验证 |
| **单股深度分析** | `python analyze_stock.py <代码>` | 单只股票深度分析，复用选股流水线评分和辩论逻辑，叠加实时行情（冲高回落、量比等） |
| **滚动回测** | `python backtest_rolling.py` | 9 个窗口滚动回测，每窗口用截止日之前数据选股，计算未来 30 交易日真实收益 |
| **全量回测** | `python run_backtest.py` | 3363 只股票 3 个月涨幅回测，输出评分分段/区分度分析/知识溢价/TOP15 推荐 |
| **月度回测** | `python run_monthly_backtest.py` | 月初 v7 评分选股 → 月末结算收益，2026年2月起逐月滚动验证策略有效性 |

### 滚动回测结论（9 窗口，2025.12 ~ 2026.04）

| 口径 | 平均 T+30 收益 | 胜率 | 最佳窗口 | 最差窗口 |
|------|:------:|:------:|:------:|:------:|
| Top5 | **+15.63%** | 66.7% | +35.1%（窗口7） | -4.6%（窗口3） |
| Top10 | **+16.73%** | 66.7% | +45.8%（窗口8） | -1.1%（窗口3） |
| Top20 | +15.78% | 62.2% | +39.2%（窗口8） | -0.7%（窗口4） |

**高频入选核心标的**：

| 代码 | 名称 | 出现次数 | T+30 平均收益 |
|------|------|:---:|:-----:|
| 300308 | 中际旭创 | 9/9 | **+24.6%** |
| 001309 | 德明利 | 7/9 | **+37.6%** |
| 300857 | 协创数据 | 7/9 | **+28.5%** |
| 688002 | 睿创微纳 | 6/9 | +11.3% |
| 688183 | 生益电子 | 5/9 | +12.0% |

中际旭创 9 个窗口全勤入选，德明利和协创数据平均收益超 28%，是策略最稳定的三大核心标的。

### 评分体系 v7（总分 100 分）

| 维度 | 权重 | 数据来源 | 说明 |
|------|:---:|------|------|
| **基本面知识** | 40分 | `fundamentals/{code}.json` + `world_knowledge.py` | 优先个股基本面 JSON，分析竞争优势(12)+财务质量(12)+成长性(10)+地缘政治(6)；无 JSON 退回行业热力分 |
| **技术分析** | 30分 | K 线缓存 (`kline_cache/`) | 趋势(35) + 动量(30) + 量能(20) + 形态(15)，含 MA/MACD/RSI/布林带 |
| **PE 估值** | 20分 | 白名单实时行情 | PE 15~80 最优(18分)，市值匹配+2分，PE异常扣分 |
| **市场溢价** | 10分 | — | 科创板+10，创业板+6 |

### 月度回测结论（2026.02~05，共4个月）

| 评分区间 | 月均收益 | 区分度 |
|:-----:|:------:|:-----:|
| ≥70 分 | **+12.31%** | 显著跑赢全市场中位数(-2.5%) |
| TOP10 组合 | 超额 **+9.98%** | 4个月均为正超额 |

评分越高收益越好：<40分(-0.29%) < 40-50(+1.07%) < 50-60(+1.75%) < 60-70(+3.18%) < ≥70(**+12.31%**)

### 辩论选股系统 v3.1

在 v7 评分召回 Top100 的基础上，通过四轮交互式辩论逐步筛选至最终 10 只推荐：

```
Top100 → 辩论1(50) → 辩论2(30) → 辩论3(20) → 辩论4(10)
  行业分散+基本面    竞争壁垒+成长   技术面+估值     综合博弈
```

**辩论机制**：
- **交互式辩论**：每只股票 Bull 陈述 → Bear 反驳 → Bear 陈述 → Bull 反驳
- **论据来源**：基本面 JSON + 世界知识 + 技术分析 + 估值，区分量化数据(📊)与定性判断(💬)
- **反驳机制**：语义对立检测(30+对) + 同领域数据优势反驳 + 权重衰减(非直接归零)
- **投降机制**（4条路径）：核心论据压制 / 连续被反驳(>60%) / 信息严重缺失 / 权重碾压(3倍+)
- **世界知识深度引用**：区分真实业务数据(权重7)与模板填充(权重1)
- **30日验证**：选股后基于真实行情计算T+30收益（无数据穿越）

**Judge 评分 v3.1 改进**：
- **趋势调节**：近20日横盘/下跌折分（×0.35~0.85），防止"基本面好但不涨"的票反复入选，健康上涨不额外加分
- **RSI 渐进惩罚**：75-80 扣3分 / 80-85 扣6分 / >85 扣10分（之前>75 仅扣2分）
- **MA20 乖离惩罚**：乖离>30% 扣6分 / >20% 扣3分 / >12% 扣1分
- **过热区加速反转**：RSI>80 时加速上涨变为扣分项，而非加分

## 增强型知识系统

### 三层知识架构

```
选股评分 (40分)
  │
  ▼
fundamental_scorer.py  ← 优先读取 fundamentals/{code}.json (500只 Top500)
  │   竞争优劣势 · 财务健康 · 成长驱动 · 地缘政治
  ▼
world_knowledge.py     ← 1011 条业务认知 (Top1000 全覆盖)
  │   公司专属 strengths / weaknesses / growth_drivers
  ▼
ai_knowledge_base.py   ← 行业热力分映射 (其余2363只)
      AI芯片 · 光通信 · 机器人 · 锂电池 · 光伏 · ...
```

| 组件 | 覆盖 | 说明 |
|------|:---:|------|
| `fundamentals/{code}.json` | 500 只 | Top500 市值，每只5维度手写分析：业务概况、竞争优势、财务健康、成长评估、地缘政治 |
| `fundamental_scorer.py` | 评分引擎 | 从 JSON 提取4维度 0~40 评分，无数据退回行业热力分 |
| `world_knowledge.py` | 1011 条 | Top1000 差异化业务认知，覆盖 1000/1000 |
| `ai_knowledge_base.py` | 全市场 | 行业热力分映射，43%+ 覆盖，含 AI芯片 光通信 算力 机器人等 |
| `stock_whitelist.json` | 3363 只 | 全市场白名单含 PE/市值/市场 实时估值数据 |

## 14 个 Agent

| 角色 | Agent | 说明 |
|------|-------|------|
| 分析师 | Market Analyst | 技术面分析（均线/MACD/KDJ/布林带） |
| 分析师 | Social Media Analyst | 社交媒体情绪分析 |
| 分析师 | News Analyst | 新闻舆情分析 |
| 分析师 | Fundamentals Analyst | 基本面分析（财务报表/估值） |
| 分析师 | Macro Analyst | 宏观经济分析 |
| 分析师 | Smart Money Analyst | 资金流向分析（主力/北向/龙虎榜） |
| 分析师 | Volume Price Analyst | 量价关系分析 |
| 研究员 | Bull Researcher | 看多研究员（辩论正方） |
| 研究员 | Bear Researcher | 看空研究员（辩论反方） |
| 研究员 | Research Manager | 辩论裁判，综合牛熊观点 |
| 风控 | Aggressive Debator | 激进风险视角 |
| 风控 | Neutral Debator | 中性风险视角 |
| 风控 | Conservative Debator | 保守风险视角 |
| 交易 | Trader | 最终交易决策与仓位建议 |

## 数据源

采用供应商路由 + fallback 机制，优先使用直接 HTTP/TCP 数据源：

| 优先级 | 供应商 | 数据通道 | 说明 |
|--------|--------|----------|---------|
| 1 | cn_astock | mootdx TCP 7709 / 腾讯 HTTP / 东方财富 / 同花顺 / 百度 / 新浪 / 财联社 | 直接接口，最鲁棒 |
| 2 | cn_akshare | akshare Python 库 | A 股 fallback |
| 3 | cn_baostock | baostock Python 库 | 历史 K 线 fallback |
| 4 | yfinance | yfinance Python 库 | 美股/港股 |

配置优先级链在 [default_config.py](tradingagents/default_config.py) 的 `data_vendors` 中定义。

## 项目结构

```
tradingagents/              # 核心包
  agents/                   # 14 个 Agent 实现
    analysts/               # 7 位分析师
    researchers/            # 牛熊研究员 + Research Manager
    risk_mgmt/              # 3 位风控 Debator
    trader/                 # Trader
    managers/               # Risk Manager
    utils/                  # 共享工具
  graph/                    # LangGraph 编排 (TradingAgentsGraph)
    trading_graph.py        # 主图定义
    data_collector.py       # 数据收集器
    intent_parser.py        # 自然语言意图解析
  dataflows/                # 数据流 + 供应商路由
    interface.py            # route_to_vendor() 路由 + fallback
    config.py               # set_config() 全局配置
    providers/              # 数据供应商实现
      astock_provider.py    # ★ 直接 HTTP/TCP 数据源 (首选)
      cn_akshare_provider.py
      cn_baostock_provider.py
      yfinance_provider.py
      alpha_vantage_provider.py
  llm_clients/              # LLM 适配层 (OpenAI/Anthropic/Google)
  prompts/                  # 提示词模板 (支持 zh/en/auto)
  default_config.py         # 默认配置 + 环境变量映射

api/                        # FastAPI 服务端
  main.py                   # REST API (analyze/jobs/reports/watchlist/scheduled/config)
  database.py               # SQLite/PostgreSQL 模型 (单用户模式)
  services/                 # 业务逻辑层
  job_store.py              # 内存/Redis 任务队列

scheduler/                  # 定时调度器 (独立进程)
  main.py                   # asyncio 循环 + concurrency 控制

cli/                        # typer CLI 入口
  main.py                   # analyze/watchlist/scheduled 命令

skills/                     # AI IDE 技能包 (Claude Code / Cursor)
  tradingagents-analysis/   # 个股深度分析 — 15 Agent 协作
  tradingagents-sector/     # 板块分析 — 6 Agent 协作

a-stock-data/               # 数据端点参考文档

── 高级工具 (根目录) ──
run_stock_picker.py         # 全A股 v7 实时选股 (+热门板块)
run_debate_picker.py        # 多轮辩论选股 (交互辩论+投降+30日验证)
analyze_stock.py            # 单股深度分析 (复用评分+辩论+实时行情叠加)
backtest_rolling.py         # 滚动回测 (9窗口×30日无数据穿越)
run_backtest.py             # 全量回测分析 (评分分段/区分度)
run_review.py               # 个股复盘 (历史存档+最新行情)
run_analysis.py             # 单股深度分析 (流式输出+历史复盘)
run_batch_sectors.py        # 批量板块深度分析
fundamental_scorer.py       # 个股基本面 4 维度评分引擎
world_knowledge.py          # Top1000 业务认知库 (1011条)
ai_knowledge_base.py        # 行业热力知识库 (43%+ 覆盖)
tech_analysis.py            # 技术指标 (趋势/动量/量能/形态)
data_cache.py               # K 线本地缓存 (3363只)
fundamental_agent.py        # 基本面数据采集 Agent
stock_whitelist.json        # 全市场白名单 (含PE/市值)
fundamentals/               # Top500 个股基本面 JSON (5维度手写)

scripts/                    # 一次性/辅助脚本 (不在主流程中)
  batch/                    # 知识库批量生成脚本 + 输出
  inject/                   # 知识库注入脚本
  fix/                      # 数据修复脚本
  gen/                      # 知识库生成脚本
  test/                     # 临时测试脚本
  analysis/                 # 一次性分析脚本
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/analyze` | POST | 提交分析任务 |
| `/v1/jobs/{id}` | GET | 查询任务状态 |
| `/v1/jobs/{id}/result` | GET | 获取分析结果 |
| `/v1/jobs/{id}/events` | GET (SSE) | 实时分析流 |
| `/v1/chat/completions` | POST | OpenAI 兼容接口 |
| `/v1/reports` | GET/POST | 报告列表/创建 |
| `/v1/reports/{id}` | GET/DELETE | 单条报告 |
| `/v1/watchlist` | GET/POST | 自选股 |
| `/v1/scheduled` | GET/POST | 定时任务 |
| `/v1/config` | GET/PATCH | 运行时配置 |
| `/v1/portfolio/imports` | POST | 持仓导入 |
| `/v1/market/*` | GET | 市场数据/搜索 |
| `/v1/backtest` | POST | 回测 |
| `/healthz` | GET | 健康检查 |

所有端点无需认证（单用户模式）。

## 通知

定时分析完成后可自动推送：

- **企业微信**: 设置 `TA_WECOM_WEBHOOK_URL`
- **邮件**: 设置 `TA_EMAIL_REPORT_TO` + SMTP 环境变量（暂搁置）

## 开发

```bash
# 运行测试
pytest tests/ -v

# 本地 API 开发
uvicorn api.main:app --reload --port 8000
```

## License

MIT
