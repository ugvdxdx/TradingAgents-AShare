# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed
- **V3 第三子维度 delivery→surge 爆发分 (2026-06-26)**: delivery(业绩兑现) 与 fundamentals 的 growth_score 高度共线, 预测力稀释至 +0.10; experiment 证实其全池 Spearman=+0.082 正向, 原 −0.5 负权重是基于新晋股子池 −0.33 的错误外推。替换为 **surge 爆发分**(30天超额收益概率=成长性加速拐点×催化近度, 正交于 chain/capital 的二阶导 alpha, 4档: 加速主升8-10/温和加速5.5-7.9/平稳钝化3-5.4/失速0-2.9)。锚公式 `chain+capital×2-delivery×0.5` → `chain+capital×2+surge×SURGE_WEIGHT`(SURGE_WEIGHT=+1.0, 待实盘积累后用 experiment_surge_weight.py 回测固化)。字段 delivery→surge 全量重命名(字段/变量/TTL key `surge_scored_date`/cache 574只/snapshots/21个scripts/文档); judges.py 三处复制公式统一调用 `data_io.anchor_score` 消除技术债; delivery 7条交叉验证规则逐条裁决(继承客户实证/财报窗口/需求性质现金流, 改造利润率红线→杠杆方向/模板句→催化空话, 丢弃增速匹配/ROE校验)。cache `surge_scored_date` 已清空, 下次维护全量重评爆发分。详见 CLAUDE.md「surge 爆发分重设计」。

### Fixed
- **选股结果非确定性 (PYTHONHASHSEED)**：`normalize.py:get_sector_keyword_index()` 原用 `set()` 合并板块名导致 key 顺序随进程随机，industry 在多板块命中数相同时 classify 结果不稳 → capital 随进程波动、排名翻转。修复：`sorted(set(...))` 固定顺序 + classify 平局取命中关键词最长的板块。同一数据两次跑选股结果不一致问题根治。
- **新晋股/研报股算分逻辑统一为普通股**：`_load_rising_stars` / `_load_research_hot_stocks` 原各自构造完整 stock dict（含 V3=0 时的归因模板假数据 chain_position/biggest_bull 等），算分逻辑与普通股分叉。重构为只返回 code + 归因摘要（`_attribution_brief`），入池后由 `load_top_n` 统一调 `_build_stock(code, v3_entry)` 构造，chain/delivery/capital/essence 全部来自 V3 cache，与普通股完全一致。归因信息仅追加到 brief 供展示。同时移除行业动量调整对新晋股/研报股的特殊跳过逻辑（v3=0 自然不受影响）。无 V3 评分的保送股（chain/capital/delivery=0）锚分=0 沉底，不再靠假数据参与排序。

### Changed
- **行业归类细化 (蹭热门板块修复)**：原 `半导体设备/材料` 含 `半导体` 泛词，导致封测/代工/LED/显示/设计类股全被吸进来蹭 hot#0 的 base=5.0。拆出 4 个独立板块：`半导体封测/代工` / `半导体设计` / `显示面板/LED` / `光伏`（从锂电拆出）。回测(125期)：Spearman +0.213→+0.224 (+0.011)，TOP10 +23.33%→+25.02% (+1.69pp)。典型：三安光电(LED) capital 8.0→6.2，排名 #5→#94。
- **取消 capital 封顶 (G模式)**：`min(8.0, base+d2×2+pf×2)` → 无封顶(仅 `max(0,·)` 防负)。原封顶砍平 21% 热门主升浪股(如中际旭创/新易盛)，与温和上涨股拿相同 capital→区分度丧失。回测(G模式,125期)：无封顶 TOP10 +2.06pp，最差期改善，Spearman 仅 -0.003。取消后中际旭创/新易盛等光模块龙头回归 TOP5。
- **召回阶段改为全池 (无预筛)**：`collect_data` / `PickerGraph` / `debate_picker_v5.py` 去掉 `top_n` 参数，生产固定全池。回测验证(125期, G模式)：召回预筛 top50/100/150/200 的 TOP10 涨幅与全池无差异 (+0.00~+0.26pp)，且预筛会漏掉保送机制加挂的新晋股/研报股 → 全池的真正价值是让保送股进入最终排序。`load_top_n` 的 `n` 参数保留(默认 None=全池)，仅供测试脚本做召回实验。
- **回测脚本 classify 同步修复**：`build_capital_history.py` / `build_price_factor_history.py` 内联 classify 加平局裁决，与生产 `_classify_sector` 一致。

### Added
- **每日选股快照存档** (`picker/snapshot.py` + `PickerGraph._save_daily_snapshot`)：实盘选股后自动存档到 `data/caches/v3_snapshots/YYYY-MM-DD.json`，含全池 chain/delivery/capital + TOP5/10 推荐理由。同日覆盖(一天一份)。chain/delivery 每周全量重评+研报触发更新，用当前快照回测会前视偷看未来评分；快照让回测按 cutoff 取历史真实评分，消除前视偏差。历史 cutoff 无快照时回退当前 V3 cache(已知近似)。
- **回测 capital cutoff 化**：`compute_capital_updates` / `update_capital` / `_compute_price_factor` / `_compute_d2_factor` / `_build_d2_sector_median_cache` 加 `cutoff_date` 参数。回测模式 pf/d2 按 cutoff 截断 K线重算(量价无前视)，base_capital 仍用当前 momentum 快照(研报无可靠历史版)。回测模式不再整个跳过 capital 重算，三天选股结果各不相同。
- **debate_picker_v5 --date 回测修复**：原 `--date 非今日` 只传 trade_date 不传 cutoff_date → 走实盘模式不截断K线。修复为非今日日期自动进入回测模式(trade_date=cutoff_date=该日)。
- **封顶实验脚本** (`scripts/experiment_capital_cap.py`)：G模式完整 capital，扫描封顶值 min(6~15/无) 对 Spearman/TOP10/撞顶比例的影响。注意：D模式 `eval_price_factors` capital max=6.5 封顶几乎不触发，封顶相关实验必须用本脚本(G模式)。
- **召回消融实验脚本** (`scripts/experiment_recall.py`)：G模式无封顶，对比召回规模 top50/100/150/200/300/全池 对 TOP10 涨幅 + Spearman 的影响。
- **delivery 权重实验脚本** (`scripts/experiment_delivery_weight.py`)：扫描 delivery 权重 -0.5~+2.0 对 TOP5/10 涨幅 + 稳定性影响。结论：维持 -0.5。
- **买1卖2 策略回测脚本** (`scripts/experiment_strategy_backtest.py`)：持仓轮动模拟(买1卖2换手约束)，对比 delivery 权重的月化收益+分月。结论：-0.5 月化 +16.79% 优于 +1.0 的 +15.83%(换手率驱动)。
- **历史表现降权/黑名单实验** (`scripts/experiment_perf_penalty.py` / `experiment_blacklist.py`)：串行回测(无前视)测试历史表现降权/黑名单规则。结论：均不接入(软降权稳定性下降，黑名单错杀趋势反转股)。

### Changed
- **资金流缓存重构为单一持久文件**：从每日一份 `mf_YYYY-MM-DD.pkl` 改为单一 `.mf_cache/mf.pkl`，不再产生多文件。
- **分层缓存深度**：有基本面(`fundamentals/`)的热股保留 14 个月(~290 交易日)，其余白名单股保留 60 天。
- **增量拉取**：`fetch_money_flow_all.py` 只对热股联网拉取缺失交易日；已有数据不重拉；自动探测最近交易日避免节假日空请求；首跑自动迁移旧 `mf_*.pkl`。

### Added
- **行业资金流历史回溯**：个股 mf.pkl 更新后，按热股 fundamentals 行业映射逐日汇总，存到 `.mf_cache/board_flow_history.pkl`（纯本地计算，零额外 API）。`rotation.get_board_flow_ranking` 在实时 akshare 失败时优先回退到该历史文件最新日。

### Removed
- **废弃老的单日板块缓存** `data/board_flow_cache.json` 及 `rotation.py` 中的 `_save_cache`/`_load_cache`/`_CACHE_PATH`、`paths.BOARD_FLOW_CACHE` 常量。板块资金流持久数据统一由 `board_flow_history.pkl` 承载；`get_board_flow_ranking` 回退链精简为：实时 akshare → 历史文件最新日 → Tushare 实时 → 候选池推算。

### Changed
- **磁盘 key 纯净化**：资金流缓存 key 从 `{code}_{days}` 改为纯 `{code}`，消除分层深度歧义；`data_io.load_mf_cache`/`rotation._infer_from_candidates` 同步简化。

## [v0.6.0] - 2026-06-11

### Added
- **增量信息采集层** (`incremental.py`)：实时财务(akshare) + 新闻(东方财富API按公司名称搜索) + K线10日走势 + 资金流5日明细 + LLM事件摘要，解决辩论信息增量不足问题
- **新闻缓存机制** (`data/news_cache.json`)：WebSearch高质量新闻预填充，按股票代码缓存，按时间线降序排列
- **hybrid海选模式**：V3 Top-6保送 + 4个LLM海选名额，回测验证黑马100%胜率，T10收益提升3.54%
- **海选模式A/B对照回测** (`_screen_mode_ab_backtest.py`)：promote/llm/hybrid三模式对照，量化黑马优势与名单重合度
- **辩论策略优化**：多头强制证据引用(日期/数值) + 催化新鲜度维度；空头5种精准打击(催化过时/资金背离/量价背离/高位透支/增速证伪)
- **条件性排名调整**：仅当有硬证据时才调整(±3位)，对称调整多头/空头，硬风险标签直接降权
- **行业认知注入**：AI行情中"大客户集中""估值高"为正向信号，禁止空头无效攻击
- **强制纳入候选股**：001309(德明利)、600522(中天科技)无论V3排名均参与辩论

### Changed
- **screen_mode默认值**：从 `promote` 改为 `hybrid`（force_k=6）
- **辩论三轮目标重构**：第1轮建claim(全量信息) → 第2轮反驳证据(精简) → 第3轮定排序(时间窗口+资金趋势)
- **排名调整机制**：从线性混合(α·v3 + (1-α)·llm)改为条件性调整，避免空头误杀动量龙头
- **增长信息输出**：取消60字截断，完整输出增长驱动/逆风/业务概览/行业地位/财务核心指标
- **K线/资金流输出**：从一行标签扩展为10日走势明细+5日资金流明细+关键价位
- **DEBATE_SYSTEM_DESIGN.md**：全面重写为v5实现版，含7阶段流水线、增量信息架构、hybrid模式回测数据

### Fixed
- **黑马截断bug**：`round1_promoted`按V3降序导致6只黑马(V3排名21-50)被静默丢弃，现显式保序输出
- **fundamentals JSON数据过时**：用akshare `get_fundamentals`拉取实时财务数据替代
- **新闻搜索质量**：从按股票代码搜索改为按公司名称搜索，过滤成交额/换手率等泛资讯

## [v0.5.0] - 2026-03-22

### Added
- **Token 级流式输出**：全部 15 个 Agent 支持 astream Token 推送，对话框实时展示 LLM 输出过程。
- **自选股管理**：数据库持久化的自选列表（上限 50），支持股票代码/名称模糊搜索。
- **定时分析**：每个交易日在用户设定时间（20:00~08:00）自动触发分析，连续失败 3 次自动停用。
- **后台调度器**：FastAPI lifespan 内嵌 asyncio 调度协程，交易日判断、防重复触发、串行预采集。
- **多阶段状态指示器**：用户提交后即时反馈：连接中 → 识别标的 → 采集数据 → 多智能体分析。
- **股票搜索 API**：`/v1/market/stock-search` 支持代码前缀和名称模糊匹配，7 天 TTL 缓存。
- **Dependabot**：自动依赖更新（pip、npm、GitHub Actions、Docker）。

### Changed
- **对话框重构**：Agent 消息改为紧凑卡片（图标+标签+实时预览），点击展开完整内容，完成后自动转为报告卡片。
- **图标体系统一**：对话框与协作面板使用一致的 Lucide 图标与配色，覆盖全部 15 个 Agent。
- **结构化提取增强**：使用 json-repair 替代正则剥离；Pydantic 模型容忍 LLM 输出变体（数组→首元素、数字→字符串、缺失字段默认值）。
- **后端异步并行**：分析师节点全部转为 async，数据采集并行执行，意图解析不再阻塞 SSE 流返回。
- **Portfolio 页面**：从"热榜选股"重构为"自选 & 定时分析"，去掉外部热榜数据依赖。
- **日志体系**：uvicorn 日志配置文件统一时间戳格式。
- **Docker 优化**：CMD 使用 tradingagents-api 入口点；git tag 注入 VERSION build-arg。
- **登录页**：12-Agent → 15-Agent，补齐宏观分析、主力资金、博弈裁判。
- **SQLite WAL 模式**：启用 WAL 支持并发读写。

### Fixed
- 修复 mini_racer/V8 多线程 crash：启动时预加载交易日历，后续请求全走缓存。
- 修复结构化提取失败：LLM 返回 markdown 代码围栏或非标准 JSON 格式导致 Pydantic 解析错误。
- 修复定时任务重复触发：启动前标记 last_run_date，防止调度循环重复启动。
- 修复 `import re` 缺失导致 report_service 崩溃。
- 修复 `_load_cn_stock_map` 错误导入位置。
- 修复 Agent 卡片状态：job.completed 时标记所有 Agent 为已完成，不再显示"撰写中"。
- 修复意图解析 JSON 在对话框显示的问题。
- 移除协作面板流光动画。

### Removed
- 移除 12 个未使用的依赖（backtrader、chainlit、redis、alembic、rich、typer 等）。
- 移除热榜选股功能（外部数据源不稳定且有合规风险）。
- 移除对话框中冗余的系统消息（job.created、job.running、agent.tool_call）。

## [v0.4.4] - 2026-03-18

### Fixed
- Fixed critical **SQLAlchemy TimeoutError** by unifying database session lifecycle across API endpoints and background tasks.
- Fixed **Resource/Semaphore Leakage** on shutdown by adding executor shutdown to the FastAPI lifespan.
- Improved repository structure by moving `announcements.json` to the `api/` directory and updating search paths.
- Cleaned up redundant `uv.lock.cp313` and `CLAUDE.md` files.
- Resolved **Announcement Schema Validation** errors (500) by aligning `announcements.json` with Pydantic model requirements.
- Made `/v1/announcements/latest` a public endpoint to ensure visibility before login.

## [v0.4.3] - 2026-03-16

### Added
- Added **Task Lifecycle Persistence and Recovery** (#32): Analysis jobs can now survive server restarts.
- Added **Configurable Max Workers** (#33): Job executor concurrency is now tunable via `TA_MAX_WORKERS` env var.
- Added persistent report lifecycle fields, including `status`, `error`, and richer section-level report storage.
- Added structured analyst trace persistence to support future report-side insight displays.
- Added header announcement support backed by `announcements.json` and `/v1/announcements/latest`.

### Changed
- Changed the report flow to initialize records earlier and update report content incrementally during long-running analysis jobs.
- Changed the header announcement entry to load from backend data instead of hard-coded preview text.
- Improved error messaging for failed analysis steps in the UI.

### Fixed
- Fixed report serialization gaps so newly persisted lifecycle and extended section fields can be returned consistently.
- Fixed report finalization and failure recording so completed and failed jobs leave clearer artifacts for follow-up inspection.

## [v0.4.2] - 2026-03-16

### Added
- Added user-context grounding so analysis can incorporate objective, risk preference, investment horizon, and holding constraints.
- Added local Docker one-click deployment script for easier self-hosted setup.

### Changed
- Upgraded the debate workflow to a claim-driven flow for stronger argument organization and downstream judgment.
- Improved multi-horizon analysis wording and parameter handling.

### Fixed
- Fixed structured extraction prompts by explicitly restoring missing JSON keywords that caused 400 errors.
- Removed mistakenly committed runtime artifacts such as `deploy` and `.vite` from version control.

## [v0.4.1] - 2026-03-15

### Added
- Added intent-driven multi-horizon analysis with streaming progress updates.
- Added integrated frontend-backend Docker packaging and multi-architecture CI/CD automation.
- Added restored A-share analysis skills with a hardened CI environment.

### Changed
- Re-applied missing dependency updates including `marshmallow` and `python-socketio`.

### Fixed
- Fixed review issues raised during the v0.4.1 stabilization cycle.
- Improved SKILL metadata and SEO-related presentation.

## [v0.4.0] - 2026-03-13

### Added
- Added monorepo synchronization and the new game-theory agent integration.
- Added report `direction` field and UTC timestamp serialization.
- Added frontend commit message support.
- Added skills support for using TradingAgents through reusable skill workflows.

### Fixed
- Fixed default agent settings.
- Fixed stock symbol normalization at task startup and during K-line data retrieval.

## [v0.3.0] - 2026-03-12

### Changed
- Removed the redundant `frontend_backup/` tree from the main branch to simplify the repository layout.
