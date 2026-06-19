# J-TradingAgents

量化多 Agent 分析框架 — 使用 LangGraph 编排 14 个专业 Agent 对 A 股/美股/港股进行综合投研分析。内置「研报采集 → 基本面生成 → V3 评分 → 辩论选股」四步闭环选股流水线。

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

# 编辑 .env，填入以下必要凭据：
# ── LLM ──
# TA_API_KEY=sk-your-api-key            # LLM API Key
# TA_BASE_URL=https://api.openai.com/v1 # OpenAI 兼容端点
# TA_LLM_PROVIDER=openai                # openai/anthropic/google
# TA_LLM_DEEP=gpt-4o                    # 深度思考模型
# TA_LLM_QUICK=gpt-4o-mini              # 快速模型
# ── 财务/资金流数据源 (选股流水线必需) ──
# TUSHARE_TOKEN=your-tushare-token      # Tushare (财报+资金流, picker/data/fundamentals_data.py / picker/data/money_flow.py)
# STOCKAPI_TOKEN=your-stockapi-token    # StockAPI (行业数据)
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

项目内置七组可被 Claude Code / Cursor 等 AI IDE 直接调用的技能包：

| Skill | 路径 | 说明 |
|-------|------|------|
| **个股深度分析** | `skills/tradingagents-analysis/SKILL.md` | 15 个 AI 分析师五阶段协作：市场→博弈→多空辩论→交易→风控，输出买卖建议+风险评估 |
| **板块分析** | `skills/tradingagents-sector/SKILL.md` | 6 名 AI 分析师协作完成板块搜索、排名、资金流向、成分股分析，输出结构化研报 |
| **★ 基本面V3评分 & 赛道Alpha** | `skills/fundamentals-scorer/SKILL.md` | 回测验证（Spearman ρ=0.527），V3三子维度+essence精华，544只A股全覆盖 |
| **★ 基本面生成** | `skills/fundamentals-generator/SKILL.md` | Tushare真实财报 + 板块研报注入 + 防污染规则，生成 fundamentals JSON |
| **★ 辩论选股 v5** | `skills/debate-picker/SKILL.md` | LangGraph 7阶段选股辩论：增量信息→海选→claim辩论→TOP10 |
| **★ 每日研报更新** | `skills/research-daily-update/SKILL.md` | 博主研报增量采集→LLM提取→注入fundamentals（--step 流水线） |
| **研报知识系统** | `skills/research-knowledge/SKILL.md` | 财经博主圈子数据采集→LLM结构化知识提取→双层知识库，为选股辩论提供增量信息 |

所有技能均可通过自然语言直接触发（例："分析贵州茅台"、"分析商业航天板块"、"重生成全量基本面"、"评分完接着选股"、"更新研报"）。

## 基本面评分 & 赛道Alpha选股（V3 正式版）

基于 LLM 的三子维度评分系统，对 544 只 A 股进行基本面评估 + 精华信息提取。**回测验证（2025.12-2026.06）**：

| 发现 | 数据 |
|:---|:---|
| V3 赛道动量 vs 半年涨幅 | Spearman **ρ = 0.527** (p<0.001) |
| V3 产业链位置 vs 涨幅 | ρ = 0.495 |
| V3 资金关注度 vs 涨幅 | ρ = 0.489 |
| V3 业绩兑现度 vs 涨幅 | ρ = 0.378 |
| 五等分 Q1→Q5 涨幅 | **+2.4% → +6.1% → +39.4% → +78.2% → +142.8%** |
| 多空收益差（Q5 - Q1） | **+154.1%** |

**V3 vs V2 对比**：V3 使用小数分（0.0-25.0，251 个梯度），区分度远超 V2 整数的 51 个梯度；同时产出 essence 精华信息（卡位/催化/多空论据/质量红线/催化时效），零边际成本服务下游辩论。

```bash
# 全量 V3 打分（544只，8并发，~35分钟）
uv run python3 picker/scoring/v3_full_score.py

# 全量回测验证
uv run python3 picker/backtest/run_backtest.py
```

### V3 三子维度（小数分 0.0-25.0）

| 维度 | 范围 | 说明 |
|:---|:---:|:---|
| chain 产业链位置 | 0.0-10.0 | AI 算力核心(8.5-10) → 次核心(6.5-8.4) → 旧赛道退潮(0) |
| delivery 业绩兑现度 | 0.0-10.0 | 顶级大客户+产能扩张(8-10) → 有客户未放量(3-5.4) → 纯概念(0-2.9) |
| capital 资金关注度 | 0.0-5.0 | AI 算力主线(4-5) → 二线国产算力(2.5-3.9) → 冷门(0-1.4) |

**sector_score = chain + delivery + capital**，每次 LLM 调用同时产出 essence 精华信息（6 字段），为下游 30 天辩论提供弹药。

## 选股流水线：四步闭环

```
① 更新研报          ② 生成基本面            ③ V3 评分             ④ 辩论选股
run_daily_update → _gen_top500_fundamentals → _v3_full_score → debate_picker_v5
   │                  │  ├ Tushare 真实财报      │                   │
   │                  │  ├ 板块研报注入          │                   │
   │                  │  └ 防污染规则            │                   │
   ▼                  ▼                         ▼                   ▼
research.db    fundamentals/*.json     .fundamental_v3_       results/picker_v5/
(博主研报库)   (544只个股基本面)        scores.json           (Top10 排名+全过程落盘)
                   ↑                        ↑ 赖 ②                 ↑ 赖 ③
                   └────────────────────────┴──────────────────────┘
                          (严格顺序依赖: ① → ② → ③ → ④)
```

**关键依赖**：每一步都依赖上一步的产物。乱序会用旧数据。完整刷新顺序 = **① → ② → ③ → ④**。

### 每日操作命令对照表

| 目标 | 命令 | 说明 |
|------|------|------|
| **① 更新研报** | `uv run python3 picker/pipeline/run_daily_update.py` | 全流程（采集→提取→注入 fundamentals）。约 10-20 分钟 |
| ① 仅采集 | `uv run python3 picker/pipeline/run_daily_update.py --step 1` | 只拉最新博主编入 research.db |
| ① 仅提取 | `uv run python3 picker/pipeline/run_daily_update.py --step 2` | 只把帖子 LLM 提取为结构化知识 |
| ① 仅注入 | `uv run python3 picker/pipeline/run_daily_update.py --step 3` | 只把研报提炼写回 fundamentals JSON |
| ① 研报全量回填 | `uv run python3 picker/pipeline/run_research_pipeline.py` | 一次性历史回填（区别于日常增量） |
| **② 重生成基本面** | `uv run python3 picker/pipeline/gen_fundamentals.py --force` | 全量 544 只（Tushare财报+研报+防污染）。约 544×3 分钟 |
| ② 仅指定股票 | `uv run python3 picker/pipeline/gen_fundamentals.py --codes 603308,300394 --force` | 只重生指定代码 |
| ② 仅补缺失 | `uv run python3 picker/pipeline/gen_fundamentals.py` | 不加 `--force` 则跳过已存在的 |
| ② 试跑前 N 只 | `uv run python3 picker/pipeline/gen_fundamentals.py --count 10` | 先验证再全量 |
| **③ 全量评分** | `uv run python3 picker/scoring/v3_full_score.py` | 544 只 V3 评分（8 并发，~35 分钟）。复用缓存，已评的跳过 |
| **④ 选股 Top50→10** | `uv run python3 picker/pipeline/debate_picker_v5.py --top-n 50` | LangGraph 7 阶段辩论。约 18 分钟 |
| ④ 指定日期 | `uv run python3 picker/pipeline/debate_picker_v5.py --date 2026-06-16` | 回测/缓存数据 |
| ④ 验证管道 | `uv run python3 picker/pipeline/debate_picker_v5.py --dry-run` | 跳过 LLM，仅验证数据流 |
| **资金流预拉取** | `uv run python3 picker/pipeline/fetch_money_flow_all.py` | 全市场资金流缓存（`.mf_cache/`），辩论阶段秒读 |

> 💡 **怎么说给 AI 听**：与其记命令，不如直接说"重生成全量基本面"、"评分完接着选股"、"从研报到选股全跑一遍"，AI 会按上表执行。

### 核心工具一览

| 工具 | 命令 | 说明 |
|------|------|------|
| **V3 全量打分** | `uv run python3 picker/scoring/v3_full_score.py` | 544 只 A 股基本面评分 + essence 精华（8 并发，~35 分钟） |
| **30 天辩论选股** | `uv run python3 picker/pipeline/debate_picker_v5.py` | LangGraph 7 阶段：增量信息→三分析师→海选→claim 辩论→TOP10 |
| **全量回测** | `uv run python3 picker/backtest/run_backtest.py` | V3 评分 vs 涨幅相关性回测 |
| **资金流预拉取** | `uv run python3 picker/pipeline/fetch_money_flow_all.py` | 全市场资金流缓存（`.mf_cache/`），辩论阶段秒读 |
| **个股基本面生成** | `uv run python3 picker/pipeline/gen_fundamentals.py` | Tushare 财报 + 板块研报 + 防污染规则驱动的 JSON 生成 |

### Tushare 财报集成（`picker/data/fundamentals_data.py`）

基本面 JSON 的财务数字（营收/净利/毛利率/ROE 等）直接来自 Tushare 真实财报，不再依赖 LLM 回忆——后者会取错财报期（实测曾把真实营收 153 亿错记成 107 亿）。`picker/pipeline/gen_fundamentals.py` 生成时自动调用，需配置 `TUSHARE_TOKEN`。失败时回退到 LLM 填写（退化到旧行为，不会崩）。

### 板块研报注入 & 防污染规则

**研报注入**：基本面生成时，通过 `tradingagents/research/consumer.py` 的 `get_industry_research_brief()` 按个股所属行业从 `research.db` 取板块研报（如 PCB 股取 PCB 板块研报），注入 prompt 并标注「板块级·信源中」。冷门股（未被研报直接点名的）也能获得板块视角。

**防污染规则**（`picker/pipeline/gen_fundamentals.py` 的 SYSTEM_PROMPT）：对「一供/份额 XX%/锁定/独家」等强断言强制信源分级（高=公告/券商、中=行业媒体、低=雪球自媒体），**信源低且与他源矛盾的强断言直接删除**。避免自媒体乐观叙事污染基本面数据。

### 辩论系统 v5 — 增量信息驱动的 claim 竞争辩论

在 V3 Top50 基础上，通过七阶段流水线筛选至最终 TOP10：

```
Top50 (V3基本面排序 + 强制纳入001309/600522)
  → 增量信息采集: 实时财务 + 新闻 + K线10日走势 + 资金流5日明细
  → 三分析师报告: 技术面/资金面/基本面 (注入增量信息)
  → 海选(hybrid): V3 Top-6保送 + 4个LLM海选名额
  → 三轮claim辩论: 建claim→反驳证据→定排序
  → 终极PK: 条件性排名调整(±3位, 需硬证据)
  → 最终 TOP10 排名
```

**核心创新**：
- **增量信息层**：实时财务(akshare) + 新闻(按公司名称搜索+WebSearch缓存) + K线明细 + 资金流明细
- **hybrid海选**：V3 Top-6保送守住龙头 + 4个海选名额让黑马进入（回测验证：黑马100%胜率，换入股平均强11.82%）
- **claim驱动辩论**：多头强制证据引用(日期/数值)，空头5种精准打击(催化过时/资金背离/量价背离/高位透支/增速证伪)
- **条件性排名调整**：仅当有硬证据时才调整(±3位)，避免空头误杀动量龙头

**回测验证（2窗口）**：

| 模式 | T5平均收益 | T10平均收益 | 黑马胜率 |
|------|-----------|-----------|---------|
| promote (V3保送) | +30.88% | +18.60% | — |
| llm (全海选) | +15.07% | +15.16% | 0% |
| **hybrid (保送6+海选4)** | **+30.42%** | **+22.14%** | **100%** |

详细设计见 [DEBATE_SYSTEM_DESIGN.md](DEBATE_SYSTEM_DESIGN.md)。

## 研报知识系统

从财经博主圈子采集盘前/盘中/盘后复盘及行业研报，通过 LLM 提取结构化知识，构建双层知识库，为选股辩论提供增量信息。

### 数据规模（2026.04-2026.06）

| 指标 | 数量 |
|:---|:---|
| 原始帖子 | 211 条 |
| 已提取结构化知识 | 198 条 |
| 行业知识库 | 676 条 |
| 通用知识库 | 207 条 |
| 每日复盘索引 | 207 条 |

### 六层架构

```
L1. Collector  ─ 数据采集层 (小鹅通圈子API + cursor分页 + 增量更新)
L2. Cleaner    ─ 数据清洗与标准化层 (去噪/分段/信息类型分类/行业标签初筛)
L3. Extractor  ─ 知识提取层 (LLM结构化提取: 行业观点/个股提及/逻辑链条/关键数据)
L4. Store      ─ 知识存储层 (SQLite + 双层知识库 + 快照 + 回测)
L5. Service    ─ 知识服务层 (API + 检索 + 回测接口)
L6. Consumer   ─ 消费层 (研报→选股/基本面注入桥接, consumer.py)
```

### 双层知识库

| 层 | 表 | 组织维度 | 内容 |
|:---|:---|:---|:---|
| 行业知识库 | `sector_knowledge` | 行业/板块 | 观点 + 逻辑链条 + 情绪 + 关键数据 |
| 通用知识库 | `general_knowledge` | 帖子 | 摘要 + 市场概览 + 洞察 + 风险 + 个股提及(JSON) |
| 每日复盘索引 | `daily_review` | 交易日 | 快速定位某日全部复盘信息 |
| 原始帖子 | `raw_feeds` | 帖子 | 博主原文（含 title/content/text） |

### 与选股/基本面系统集成（consumer.py）

```
research.db
  │  get_stock_research_signal()  → 个股研报信号 (辩论+增量)
  │  get_industry_research_brief()→ 板块研报摘要 (基本面生成注入)
  │  get_sector_momentum()        → 行业研报动量 (分析师+轮动)
  │  get_dark_horse_stocks()      → 研报黑马 (海选保送)
  │  get_research_risk_signals()  → 研报风险 (海选排雷)
  ▼
基本面生成 (picker/pipeline/gen_fundamentals.py)  ← 注入板块研报 (信源:中)
辩论选股 (picker/pipeline/debate_picker_v5.py)             ← 注入个股研报信号
```

### 核心脚本

```bash
# 日常增量更新 (推荐, 含采集+提取+注入 fundamentals)
uv run python3 picker/pipeline/run_daily_update.py              # 全流程
uv run python3 picker/pipeline/run_daily_update.py --step 1     # 仅采集最新研报
uv run python3 picker/pipeline/run_daily_update.py --step 2     # 仅 LLM 提取
uv run python3 picker/pipeline/run_daily_update.py --step 3     # 仅注入 fundamentals

# 一次性历史回填 (首次部署或补历史数据)
uv run python3 picker/pipeline/run_research_pipeline.py

# 研报→fundamentals 增量注入 (单独跑, 可指定个股)
uv run python3 picker/pipeline/update_fundamentals_from_research.py
uv run python3 picker/pipeline/update_fundamentals_from_research.py --stock 300308
uv run python3 picker/pipeline/update_fundamentals_from_research.py --dry-run

# 知识检索
uv run python3 skills/research-knowledge/scripts/query.py stats
uv run python3 skills/research-knowledge/scripts/query.py sector 半导体
uv run python3 skills/research-knowledge/scripts/query.py stock 立昂微
uv run python3 skills/research-knowledge/scripts/query.py date 2026-06-15
```

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
    sector/                 # 板块分析 Agent (6 智能体)
    utils/                  # 共享工具
    picker/                 # ★ v5 辩论选股包 (LangGraph 7阶段)
      picker_graph.py       #   图编排
      picker_state.py       #   状态 schema
      incremental.py        #   增量信息采集 (实时财务+新闻+量化信号)
      analysts.py           #   三分析师节点
      judges.py             #   海选 + 终极PK
      debaters.py           #   claim 驱动辩论
      rotation.py           #   行业轮动感知
      data_io.py            #   数据采集层
      prompts.py            #   提示词 (含防污染先验)
      reporter.py           #   报告渲染
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
  llm_clients/              # LLM 适配层 (OpenAI/Anthropic/Google)
  prompts/                  # 提示词模板 (支持 zh/en/auto)
  research/                 # ★ 研报知识系统 (六层架构)
    collector.py            # L1 数据采集 (小鹅通API + cursor分页)
    cleaner.py              # L2 数据清洗 (去噪/分段/分类)
    extractor.py            # L3 知识提取 (LLM结构化提取)
    store.py                # L4 知识存储 (SQLite + 双层知识库)
    service.py              # L5 知识服务 (API + 检索 + 回测)
    consumer.py             # L6 消费层 (研报→选股/基本面注入桥接)
  default_config.py         # 默认配置 + 环境变量映射

api/                        # FastAPI 服务端
  main.py                   # REST API (analyze/jobs/reports/watchlist/scheduled/config)
  database.py               # SQLite/PostgreSQL 模型 (单用户模式)
  services/                 # 业务逻辑层
  job_store.py              # 内存/Redis 任务队列

scheduler/                  # 定时调度器 (独立进程)
cli/                        # typer CLI 入口

skills/                     # AI IDE 技能包 (Claude Code / Cursor)
  tradingagents-analysis/   # 个股深度分析 — 15 Agent 协作
  tradingagents-sector/     # 板块分析 — 6 Agent 协作
  fundamentals-scorer/      # ★ 基本面 V3 评分 & 赛道 Alpha
  research-knowledge/       # ★ 研报知识系统 — 采集+提取+双层知识库
  fundamentals-generator/   # ★ 基本面生成 (Tushare财报+研报+防污染)
  debate-picker/            # ★ 辩论选股 v5 (LangGraph 7阶段)
  research-daily-update/    # ★ 每日研报增量更新

a-stock-data/               # 数据端点参考文档

── 选股流水线工具 (picker 包, 路径统一经 picker/paths.py) ──
picker/pipeline/debate_picker_v5.py         # ④ 30天涨幅辩论选股 — LangGraph 7阶段
picker/scoring/v3_full_score.py           # ③ V3 全量基本面评分 + essence 精华 (544只，8并发)
picker/pipeline/gen_fundamentals.py # ② 个股基本面 JSON 生成 (Tushare财报+研报+防污染)
picker/data/fundamentals_data.py        #   Tushare 真实财报拉取模块 (②的依赖)
picker/scoring/fundamental_scorer.py       #   V3 评分引擎 (三子维度 + essence, ③的依赖)
picker/pipeline/run_daily_update.py         # ① 每日研报增量更新 (采集→提取→注入)
picker/pipeline/run_research_pipeline.py    # ① 研报全量历史回填 (首次部署用)
picker/pipeline/update_fundamentals_from_research.py  # 研报→fundamentals 注入 (①的step3独立版)
picker/pipeline/rotation_rescore.py        # 轮动重评
picker/pipeline/fetch_money_flow_all.py     # 全市场资金流预拉取 → .mf_cache/
picker/data/money_flow.py               # 资金流分析 (双源: 东方财富 + Tushare fallback)
picker/scoring/tech_analysis.py            # 技术指标 (趋势/动量/量能/形态)
picker/data/data_cache.py               # K 线缓存
picker/backtest/run_backtest.py             # 全量回测分析
picker/pipeline/run_analysis.py             # 单股深度分析 (流式输出+历史复盘)
data/news_cache.json        # WebSearch 新闻缓存 (按股票代码, 按时间线排序)
research.db                 # 研报知识库 (SQLite)
fundamentals/               # 个股基本面 JSON (544只)
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
