# J-TradingAgents — 量化多 Agent 分析框架

## 项目简介

本项目是一个多 Agent 量化分析系统，使用 LangGraph 编排 14 个专业 Agent（市场、新闻、基本面、宏观、资金流、量价、牛熊辩论、风险讨论等）对 A 股/美股/港股进行综合分析。

**核心交互方式**: 通过 Claude Code CLI 直接对话，无需前端。

**两阶段选股流水线**：
1. 阶段一（每周+研报触发）：V3 基本面打分（`picker/scoring/v3_full_score.py`）— 每周全量评分 chain/delivery/essence；研报涉及个股时增量更新；capital 每日动态重算
2. 阶段二（每日）：量化锚排序选股（`picker/pipeline/debate_picker_v5.py`）— LangGraph 4 节点纯量化基线：collect_data(全池采集) → quantum_rank(锚分排序取TOP5) → risk_review(可信度) → report_render(报告+🎯策略信号)。零 LLM 调用（回测证明 LLM 从头排序为负相关 -0.14，破坏量化信号）。三分析师+增量信息节点暂未接入，待优化。

**核心机制**：
- **chain 赛道热度×竞争力**（`picker/scoring/chain_tiers.py`）：chain=赛道热度(theme级)×个股竞争力，6档可重叠热度带(热主线→高档/退潮→低档)；档位映射外部化为 `chain_tier_map.json`，每日用最新研报动态更新(manual/auto)，注入评分 prompt
- **新晋股发现**（`picker/discovery/scan_mispriced.py`）：量价扫描 + 网络搜索归因 + 板块扩散 + 冷股激活 + 冷门清理(热→冷)
- **板块缺口发现**（`picker/discovery/discover_sector_gap.py`）：研报热但池未覆盖的主题 → 智谱web search找股 → 生成基本面+V3评分入池
- **池子边界管理**（三操作闭环）：Step 2.5 缺口补充(加热) / Step 6 冷股激活(冷→热) / Step 6.5 冷门清理(热→冷)
- **capital 动态更新**（`picker/scoring/v3_full_score.py:update_capital`）：每次选股前用研报板块动量 + 个股量价(双窗口)重算 capital，纯量化 0 次 LLM
- **过热股检测**（`picker/scoring/v3_full_score.py:detect_overheated`）：高分滞涨股搜索验证 + 风险标记，不自动惩罚
- **冷股池**（`cold_fundamentals/`）：无催化股票冬眠，新晋股逻辑可激活

**归档**：旧版脚本（`run_debate_picker.py`, `run_stock_picker.py` 等）已移入 `archive/`。

## 关键命令

```bash
# 单只股票分析
tradingagents analyze 600519.SH --date 2026-05-26
tradingagents analyze 贵州茅台

# 自选股管理
tradingagents watchlist list
tradingagents watchlist add 600519.SH

# 定时任务
tradingagents scheduled list
tradingagents scheduled add 600519.SH --time 20:00

# API 服务（供外部调用）
tradingagents-api

# 定时调度器（独立进程）
tradingagents-scheduler
```

## 项目结构

```
tradingagents/          # 核心包
  agents/               # 14 个 Agent
    picker/             # v5 辩论选股包 (graph/state/analysts/judges/debaters/reporter/incremental/rotation)
  graph/                # LangGraph 编排
  dataflows/            # 数据流 + 数据源路由
    providers/          # 数据供应商
      astock_provider.py  # ★ 直接 HTTP/TCP 数据源（首选）
      cn_akshare_provider.py  # fallback
      cn_baostock_provider.py  # fallback
      yfinance_provider.py     # 美股/港股
  llm_clients/          # LLM 适配层
  prompts/              # 提示词模板
  research/             # 研报知识系统 (collector/cleaner/extractor/store/consumer/normalize)
  default_config.py     # 默认配置

api/                    # FastAPI 服务端
  main.py               # REST API
  database.py           # SQLite/PostgreSQL
  services/             # 业务逻辑
  job_store.py          # 任务队列

scheduler/              # 定时调度器（独立进程）
cli/                    # typer CLI 入口
a-stock-data/           # SKILL.md — 数据端点参考文档
skills/
  tradingagents-analysis/  # 15-Agent 深度分析
  tradingagents-sector/    # 板块分析
  fundamentals-scorer/     # ★ V3 基本面评分 & 赛道Alpha（回测ρ=0.527）

── 两阶段选股工具 (picker 包, 原根目录脚本) ──
picker/                 # ★ 选股工具包 (路径统一经 picker.paths 解析)
  paths.py              # 统一路径解析层 (缓存/DB/whitelist 唯一真相源)
  knowledge/            # world_knowledge / ai_knowledge_base / fundamental_agent
  data/                 # data_cache / money_flow / fundamentals_data
  scoring/              # tech_analysis / fundamental_scorer / v3_full_score / chain_tiers (赛道→热度档动态映射)
  discovery/            # scan_mispriced (新晋股) / discover_sector_gap (板块缺口发现)
  pipeline/             # debate_picker_v5 / gen_fundamentals / refresh_fundamentals / run_daily_maintenance / update_* / fetch_money_flow_all
  backtest/             # run_backtest

── 运行时数据 ──
data/                   # 运行时数据集中
  caches/               # 原 .xxx.json 点前缀缓存 (fundamental_v3_scores / overheated_risk / ...)
  whitelist/            # stock_whitelist / top500_whitelist
  reference/            # top500_and_leaders / world_knowledge_2026_06 / stocks_audit / chain_tier_map.json (赛道→热度档) / chain_tier_archive/ (历史归档)
  news_cache.json
fundamentals/           # 基本面 JSON (537只热股)
cold_fundamentals/      # 冷股池 (167只无催化, 冬眠)
kline_cache/  profiles/  .mf_cache/  .cache/   # 大缓存目录
                          # .mf_cache/: mf.pkl(个股资金流) + board_flow_history.pkl(行业资金流历史)
research.db             # 研报知识库

── 归档 (不进版本库) ──
archive/                # 备份/一次性脚本/批量产物 (.bak / batch* / 过期脚本)
docs/                   # 设计文档
```

> **入口脚本速查** (原根目录文件名 → 新路径):
> - `_v3_full_score.py` → `picker/scoring/v3_full_score.py`
> - `debate_picker_v5.py` → `picker/pipeline/debate_picker_v5.py`
> - `scan_mispriced.py` → `picker/discovery/scan_mispriced.py`
> - `_gen_top500_fundamentals.py` → `picker/pipeline/gen_fundamentals.py`
> - `refresh_fundamentals.py` → `picker/pipeline/refresh_fundamentals.py` **(新·研报触发彻底重写)**
> - `run_daily_maintenance.py` → `picker/pipeline/run_daily_maintenance.py` **(新·每日维护统一入口)**
> - `update_world_knowledge.py` → `picker/pipeline/update_world_knowledge.py`
> - `run_daily_update.py` → `picker/pipeline/run_daily_update.py`
> - `run_research_pipeline.py` → `picker/pipeline/run_research_pipeline.py`
> - `fetch_money_flow_all.py` → `picker/pipeline/fetch_money_flow_all.py`

## 数据源架构

`dataflows/interface.py` 中的 `route_to_vendor()` 实现了供应商路由和 fallback 机制：

- **首选**: `cn_astock` — 直接 HTTP/TCP 调用（mootdx 7709、腾讯 GBK 88 字段、东方财富 datacenter、同花顺、百度、新浪、财联社）
- **Fallback**: `cn_akshare` → `cn_baostock` → `yfinance`
- 配置在 `default_config.py` 的 `data_vendors` 中定义优先级链

## 环境配置

主要环境变量（见 `.env.example`）：
- `TA_API_KEY` — LLM API Key
- `TA_BASE_URL` — LLM API Base URL
- `TA_LLM_PROVIDER` — LLM 供应商 (openai/anthropic/google)
- `TA_LLM_DEEP` / `TA_LLM_QUICK` — 模型名
- `TA_LANGUAGE` — 提示词语言 (zh/en/auto)
- `TA_WECOM_WEBHOOK_URL` — 企业微信通知

## 注意事项

- 单用户模式，`DEFAULT_USER_ID = "default"`，无认证系统
- 定时分析通知通过环境变量配置，不依赖 DB 加密存储
- `astock_provider.py` 使用 mootdx TCP 连接（端口 7709），懒加载单例
- 所有 Agent 输出格式统一为 markdown/CSV 字符串

## 基本面文件更新体系 (2026-06 重构)

### 设计原则

> **"研报提及 → 彻底重新生成"**（非增量追加），从 Web Search 开始，到 V3 打分结束。

旧体系的 `update_fundamentals_from_research.py` 只做增量追加（列表追新条目，旧信息永不淘汰），
已被 `refresh_fundamentals.py` 的完全重写替代。新体系综合 **Web Search + Tushare + 研报 + 世界知识**。

### 四级更新层级

| 层级 | 触发方式 | 模块 | 操作 | 成本 |
|---|---|---|---|---|
| **L0 每日量化** | 每日自动 | `v3_full_score.py:update_capital()` | 纯量价+板块动量重算 capital | 秒级，0 LLM |
| **L1 研报触发** | 研报有新提及 | `refresh_fundamentals.py:refresh_one()` | Web+Tushare+研报 → LLM 完整重写 JSON + V3 重评 | ~30s/只 |
| **L2 每周全量** | 每周一次 | `v3_full_score.py:main()` | 全部 537 只重评 chain/delivery/essence | ~10min |
| **L3 冷启动** | 手动/新入池 | `gen_fundamentals.py` | 从 top500 列表批量生成新 JSON | 按需 |

### 每日维护统一入口

```bash
# 推荐：统一编排器（按序执行所有步骤；研报链路与 K线/资金流采集并行）
uv run python3 picker/pipeline/run_daily_maintenance.py

# 只采集 K线+资金流 (带新鲜度预检, 不跑研报/评分)
uv run python3 picker/pipeline/run_daily_maintenance.py --data-only

# 只刷新 fundamentals（研报触发）
uv run python3 picker/pipeline/refresh_fundamentals.py

# 刷新单只股票
uv run python3 picker/pipeline/refresh_fundamentals.py --stock 300308

# chain 档位映射更新 (manual=只出diff / auto=写入归档)
uv run python3 picker/pipeline/run_daily_maintenance.py --chain-tiers-mode auto
```

### 更新链路

```
run_daily_maintenance.py (统一编排器)
  ├─ 主进程: 研报链路 ────────────┐   ┌─ 子进程: 数据采集 (并行)
  │   Step 1: 研报采集            │   │   K线更新 (update_klines_daily)
  │   Step 2: 知识提取            │   │   资金流 (fetch_money_flow_all)
  │   Step 2.5: 板块缺口发现 (加热)│   │   (两者带新鲜度预检, 已最新则跳过)
  │   Step 2.6: chain 档位更新     │   └────────────────────────────────
  │   Step 3: 彻底刷新 (研报触发)  │
  │   Step 4: capital (纯量化)     │
  │   Step 5: 过热检测             │
  │   Step 6: 冷股激活 (冷→热)     │
  │   Step 6.5: 冷门清理 (热→冷)   │
  │   Step 8: 世界知识             │
  └─ Step 9: 每日快照 (snapshot)
```

### 已废弃

- `picker/pipeline/update_fundamentals_from_research.py` — 增量追加，被 `refresh_fundamentals.py` 替代
- `picker/knowledge/fundamental_agent.py:analyze_one()` — 旧规则型生成（新浪/百科），写入能力已移除
- `picker/paths.py:FUNDAMENTALS_COLD_DIR` — 原指向不存在的 `fundamentals_cold/`，统一为 `COLD_FUNDAMENTALS_DIR`

## 选股系统架构 (2026-06 量化锚重构)

### 核心发现 (回测验证)

| 方法 | Spearman (预测排名 vs 实际涨幅) | 耗时 |
|---|---|---|
| LLM 多轮辩论从头排序 | **-0.14** (负相关!) | 40分钟 |
| V3 总分排序 | +0.47 | 秒级 |
| **chain+capital×2-delivery×0.5 量化锚** | **+0.555** | **1.7秒** |

**结论**: LLM 从头排序会破坏量化信号; 纯量化锚 (chain+capital×2-delivery×0.5) 是最优排序方法。
- 21个时间点 × 530只股 × 30日窗口验证, **20/20期正相关**, 最低 +0.34
- capital 权重×2 最优 (比等权 +0.06, 比纯chain +0.05)
- delivery 子维度预测力极弱 (+0.10), 做轻微负权重惩罚 (-0.5) 提升 +0.014

### 数据流 (纯量化基线)

```
每日选股 (picker/pipeline/debate_picker_v5.py)
  ├─ collect_data
  │    ├─ update_capital(persist=False, mode=G) ← capital动态更新(G模式), 0次LLM, 秒级
  │    │    G模式: base_capital + D2(行业相对强度)×2 + price_factor×2 (无封顶)
  │    ├─ detect_overheated()               ← 过热股检测, 搜索验证+风险标记, 不改分
  │    ├─ load_top_n(v3_cache=内存cache)    ← 全池530只 (不预筛, 全部参与排序)
  │    └─ K线 + 资金流 → candidates (~530只)
  ├─ quantum_rank                           ← 量化锚排序, 0次LLM, 秒级
  │    锚 = chain + capital×2 - delivery×0.5 → 取 TOP5
  └─ risk_review + report_render            ← 报告 + 🎯买1卖2策略信号 + 全量复盘落盘

capital 模式 (CAPITAL_MODE 环境变量):
  G(默认): base + D2×2 + pf×2 (无封顶, 仅 max(0,·) 防负) — 换手率2.2/天, 策略月均+31%
    回测验证(125期): 无封顶 TOP10涨+2.06pp vs min(8.0), Spearman仅-0.003
    (封顶会砍平21%热门主升浪股, 如中际旭创/新易盛, 与温和上涨股同分→区分度丧失)
  D(旧):   base × pf (min5.0)    — 换手率3.0/天, 回测用
  A:       base                  — ρ最优但换手0.1(策略不可用)
```

### V3 评分三子维度

| 维度 | 更新频率 | 方式 | 排序预测力 | 说明 |
|---|---|---|---|---|
| chain (赛道热度×竞争力) | 每周+研报触发 | LLM | **+0.55** (强) | **6档可重叠热度带**(热主线8.5-10/热门7-9/温热新兴5.5-7.5/中性3.5-5.5/偏冷2-4/退潮0-2.5); 档=赛道热度, 档内分由竞争力定; 档位映射=`chain_tier_map.json`每日动态更新 |
| capital (资金热度) | **每日** | **量化** | **+0.50** (强) | 板块动量+个股量价, 模式G(默认): base+D2×2+pf×2, 无封顶 |
| delivery (业绩兑现) | 每周+研报触发 | LLM | +0.10 (弱) | 顶级客户+产能+高增兑现→8-10 |

> delivery 预测力弱, 在锚公式里做 -0.5 负权重惩罚; chain+capital 是主信号。
> capital 模式G的换手率(2.2/天)是策略可执行的必要条件; price_factor的真正价值是提供换手率而非排序质量。

### PROMPT_V3E 升级 + 模型切换 (2026-06-22)

**背景**: 原 PROMPT_V3E 过简(每子维度一行), chain/delivery 缺边界规则与交叉验证, 世界知识未注入评分, 且原模型 deepseek-v4-pro 在新 prompt 下 20% 解析失败。

**改动** (全部在 `picker/scoring/v3_full_score.py`):
1. **PROMPT_V3E 结构化**: chain 加 5 条边界规则(主业占比/财务交叉/旧赛道新兴业务/研报催化/产业链传导); delivery 加 6 条交叉验证(净利率红线<5%→≤6分/增速匹配/客户名不可信/模板句检测/ROE校验/中报窗口加权); essence 加质量禁则(禁空话/禁套话对仗/禁同义重复/必须含数据)
2. **世界知识注入**: `_load_world_knowledge_slim()` 提取市场格局+AI算力主线+退潮赛道+业绩窗口(~1500字, 进程级缓存)注入评分 prompt
3. **chain/delivery TTL 7天**: `_parse` 写入 `chain_scored_date`/`delivery_scored_date`, `needs_run()` 超期自动重评; `detect_overheated` 检出 chain高估→强制重评
4. **模型切 GLM-5.2**: deepseek-v4-pro 返回缺字段/畸形 JSON → 20% 失败; GLM-5.2 结构化输出稳定(诊断 100% 成功)

**失败处理 4 层防御** (实测 80%→100% 成功率):
- `_parse`: 要求 JSON 必须含 chain/delivery 字段, 否则判失败(防缺字段静默成 0 分污染排名)
- `_llm`: 429 限流(BigModel 速率限制)走长退避(10-58s)+随机抖动, 重试 5 次; 其他瞬时错误短退避
- `_call`: 解析失败重试 LLM 最多 3 次(并发下偶发畸形响应, 串行重跑可成功)

**验证结果** (546/546 全量重评):
- essence bull/bear 含数据率: 26%/33% → **86%/80%**
- 低净利(<5%)股 delivery 平均降 -1.26(利润率红线生效, 工业富联/浪潮信息等代工股被正确降分)
- PCB/CCL 板块内 chain 区分度 σ: 0.24 → 1.05
- **锚分回测**(同方法相对对比, 125期): 生产锚 Spearman +0.216→**+0.245**(+0.029), 正相关期 92→96/125, 新 prompt 安全上线
  - 注: 绝对值低于历史 +0.555 因本对比用当前分数对齐全部 cutoff(前视近似), 非 cutoff 化 capital+快照 chain/delivery; 相对结论(新>旧)可靠

### chain 语义重设计 + 档位动态化 (2026-06-23)

**语义变更** (用户纠正: 目标是收益预测, 不是产业研究):
- 旧: chain=产业链**位置核心度** (8档非重叠, 月级稳定结构性判断)
- 新: chain=赛道**热度**(theme级) × 个股**核心竞争力** — 6档**可重叠**热度带
  - 档=赛道当前热度(热主线→高档, 退潮→低档, 周-月尺度); 档内具体分由竞争力定(龙头/份额/壁垒→档内高分, 跟风→档内低分)
  - 档间有意重叠: 强竞争力温热股(7.5)可追平弱竞争力热门股(7) — 竞争力能跨档
  - 热门新主题(金刚石散热/PCIe Retimer)按热度进温热带, 不再被"位置支撑"压低; 锂电储能回暖则上移

**档位外部化 + 动态更新** (`picker/scoring/chain_tiers.py`):
- 档位映射从 PROMPT_V3E 硬编码 → `data/reference/chain_tier_map.json` (theme/theme_strength/tiers[6])
- 评分时 `get_chain_prompt()` 把 tier_map 渲染注入 PROMPT_V3E 档位段 (锚点 splice); 缺失/锚点失效则回退硬编码(并告警, 非静默)
- `build_candidate_tier_map()` 读最新研报(板块动量+代表性观点)+世界知识, LLM 生成候选; **骨架校验**: 候选 6档 ranges 必须严格等于 `_TIER_SKELETON_RANGES`, 否则拒绝(防 LLM 擅改刻度, 只接受赛道重映射)
- `update_chain_tiers(mode)`: manual 只出 diff 供审核; auto diff有变化即写入(归档旧版可回滚)。固化为每日维护 Step 2.6

> ⚠ splice 锚点曾因 PROMPT_V3E 重写 (档位规则行带括注) 失配, 导致 `get_chain_prompt()` 静默回退硬编码, 动态 tier_map 永不注入(已修复: 起始锚点只取 `**档位规则` 前缀, 终止锚点改 `**竞争力档内分化`, 失配时告警)。改 PROMPT_V3E 档位段结构时须同步 `_BLOCK_START/END_MARKER`。

### 行业归类与确定性 (2026-06 修复)

### 行业归类与确定性 (2026-06 修复)

**capital 的 base_capital 来自个股 industry 字段经板块归类后, 查研报 hot_sectors 排名**。
归类质量直接影响 capital 准确性, 两类问题已修复:

**① 确定性 (PYTHONHASHSEED 修复)**:
- `normalize.py:get_sector_keyword_index()` 原用 `set()` 合并板块名 → key 顺序随进程哈希种子随机
- 当 industry 在多个板块命中数相同(平局)时, classify 取到不同板块 → capital 随进程波动
- 修复: `sorted(set(...))` 固定顺序 + classify 平局取命中关键词最长的板块(更精确)

**② 归类细化 (蹭板块修复)**:
- 原 `半导体设备/材料` 板块含 `半导体` 泛词 → 封测/代工/LED/显示/设计全被吸进来蹭 hot#0 的 base=5.0
- 拆出 4 个独立板块: `半导体封测/代工` / `半导体设计` / `显示面板/LED` / `光伏`
- 回测(125期): Spearman **+0.213→+0.224 (+0.011)**, TOP10 **+23.33%→+25.02% (+1.69pp)**
- 典型: 三安光电(LED) capital 8.0→6.2, 排名 #5→#94

**classify 平局裁决规则** (`_classify_sector`):
命中数相同时, 取命中关键词中"最长"的板块(长词更精确, 如"算力芯片">"半导体"), 仍平则按板块名排序(跨进程确定)。

### 候选池 (全池采集, 无召回预筛)

```
load_top_n(n=None):  # 生产固定全池
  全池V3 530只 (按chain+capital入池排序) + 强制纳入 + 新晋股 + 研报热门股 → 去重后~530只
  全部进入collect_data算tech/fund/r5/r20, 然后量化锚排序取TOP5

回测验证(125期, G模式无封顶) — 召回消融实验:
  top50/100/150/200 的 TOP10涨幅与全池无差异 (+0.00~+0.26pp)
  → 召回预筛对选最强股无帮助, 反而会漏掉保送机制加挂的新晋股/研报股
  → 全池的真正价值: 让 chain+capital 排序靠后的保送股能进入最终锚排序
load_top_n 的 n 参数仅供测试脚本(scripts/test_deep_rank.py)做召回实验, 生产不传。
```

### 新晋股发现 + 池子边界管理 (picker/discovery/scan_mispriced.py)

```
量价扫描 (近5日>15% & V3<15) → 归因(14天缓存) → 板块扩散(强度过滤)
  ├─ 板块供需型 (保送进候选池, 保留真实v3)
  ├─ 个股事件型 (不扩散)
  └─ 冷股激活 _reactivate_cold_stocks (冷→热: r5>15% 自动移回 fundamentals/)
池子边界三操作闭环 (每日维护):
  ├─ 加热     discover_sector_gap (Step 2.5): 研报热但池未覆盖 → web search找股入池
  ├─ 冷→热    _reactivate_cold_stocks (Step 6): 量价异动激活冷股
  └─ 热→冷    cleanup_to_cold_stocks (Step 6.5): V3<7+chain<4+cap<3+r20<5+无研报 → 移入冷池
       (chain<4 保护优质AI股即使回调也不误杀; 三步同步: 移JSON+删V3 cache+加cold_list)
归因支持回测模式: cutoff_date 非空时跳过网络搜索(前视偏差), 用研报+LLM现场判断
```

### 关键文件说明

> 所有缓存路径经 `picker/paths.py` 统一解析；下表为相对项目根的实际位置。

| 缓存文件 | 内容 | TTL |
|---|---|---|
| `data/caches/fundamental_v3_scores.json` | V3 评分 (chain/delivery/capital/essence) | 每周全量+研报触发+每日capital |
| `data/caches/v3_snapshots/YYYY-MM-DD.json` | **每日选股快照** (全池分数+TOP5/10推荐+理由) | 每日(同日覆盖) |
| `data/caches/mispriced_attribution_cache.json` | 新晋股归因 (板块供需/个股事件) | 14天 |
| `data/caches/overheated_risk_cache.json` | 过热股风险验证 | 7天 |
| `data/caches/cold_stocks.json` | 冷股清单 | 手动 |
| `data/caches/sub_sector_override.json` | 细分赛道 capital 拆分表 | scan_mispriced 维护 |
| `data/reference/chain_tier_map.json` | chain 赛道→6档热度带映射 (theme/tiers) | 每日维护 Step2.6 (manual/auto) |
| `data/reference/chain_tier_archive/` | chain tier_map 历史归档 (带时间戳+原因, 可回滚) | 每次变更归档 |
| `data/reference/world_knowledge_2026_06.md` | 世界知识 (宏观+归因) | 每日更新 |

> **每日快照** (`picker/snapshot.py`): 实盘选股后自动存档, 含全池 chain/delivery/capital + TOP推荐理由。
> 回测按 cutoff 取 ≤ 该日 的最近快照, **消除 chain/delivery 前视偏差** (chain/delivery 每周更新, 用当前快照回测会偷看未来)。
> 历史 cutoff 无快照时回退到当前 V3 cache (已知近似, 越早期越失真)。

### 回测/调试工具

| 脚本 | 用途 |
|---|---|
| `scripts/validate_anchor.py` | 大规模验证锚分预测力 (21期×530只, 秒级; 注意: 用V3当前快照capital, 非cutoff重建) |
| `scripts/compare_prompts.py` | 新旧 V3 prompt 对比 (chain/delivery变化+essence质量+delivery交叉验证+chain区分度) |
| `scripts/backtest_compare_prompts.py` | 新旧 prompt 锚分回测对比 (同方法125期, 相对Spearman) |
| `scripts/diag_v3_failure.py` | V3 评分失败根因诊断 (直调LLM打印原始返回) |
| `scripts/eval_price_factors.py` | price_factor 变体回测 (D模式 base×pf, 125期, 含Spearman+TOP10) |
| `scripts/build_price_factor_history.py` | 构建 pf 历史 (cutoff化, 无前视, 12变体+基线) |
| `scripts/build_capital_history.py` | 构建 capital 历史 (cutoff化, 无前视, base_capital剥离) |
| `scripts/experiment_capital_cap.py` | 封顶实验 (G模式, 扫描min(6~15/无)对Spearman/TOP10影响) |
| `scripts/experiment_recall.py` | 召回消融实验 (全池 vs top50/100/200, 对比TOP10/Spearman) |
| `scripts/experiment_delivery_weight.py` | delivery 权重实验 (扫描-0.5~+2.0, 结论:维持-0.5) |
| `scripts/experiment_strategy_backtest.py` | 买1卖2 持仓轮动回测 (月化+分月, delivery权重对比) |
| `scripts/experiment_perf_penalty.py` | 历史表现软降权实验 (串行无前视, 结论:不接入) |
| `scripts/experiment_blacklist.py` | 黑名单规则实验 (串行无前视, 结论:不接入,错杀反转股) |
| `picker/snapshot.py` | 每日快照读取 (回测按cutoff取历史chain/delivery, 消除前视) |
| `scripts/test_deep_rank.py` | 深辩排序对比测试 (v5纯LLM/v6动量/v7量化锚, 含Spearman计算) |
| `picker/data/data_cache.py` | K线缓存 (count=90根, 支持回测) |

> **回测模式差异**: 生产用 G 模式(base+d2×2+pf×2, 无封顶), `eval_price_factors` 用 D 模式(base×pf, min5.0)。
> D 模式 capital max=6.5 封顶几乎不触发; 封顶相关实验须用 `experiment_capital_cap.py`(G模式)。
> **前视偏差**: capital(ppf/d2) 已 cutoff 化; chain/delivery 需用 `picker/snapshot.py` 取历史快照消除前视, 无快照时回退当前 V3 cache(近似)。