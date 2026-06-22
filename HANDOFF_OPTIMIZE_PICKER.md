# 任务交接：优化新晋股逻辑与增量信息接入

## 项目背景

J-TradingAgents 是一个 A股量化选股系统。选股流程刚完成了一次重大重构：从"LLM多轮辩论排序"改为"纯量化锚排序"。你现在接手的任务是**在这个干净基线上，优化新晋股逻辑并尝试把增量信息用起来**。

项目根目录：`/Users/bilibili/Desktop/J-TradingAgents`

## 当前架构（纯量化基线）

```
collect_data → quantum_rank → risk_review → report_render
```

- **collect_data**: 读V3缓存+K线+资金流，构建候选池（~100只），计算技术指标和量价信号(r5/r20/距高点)
- **quantum_rank**: 按**量化锚**排序取TOP10，零LLM调用，1.7秒完成
- 排序锚公式：`anchor_score = chain + capital×2 - delivery×0.5`

入口脚本：`picker/pipeline/debate_picker_v5.py`
图编排：`tradingagents/agents/picker/picker_graph.py`

## 核心发现（回测验证，极其重要）

这些是通过 **21个时间点 × 530只股 × 30日窗口** 严格验证的结论，是你的工作基础：

### 1. 量化锚的预测力

| 因子 | Spearman vs 30日涨幅 | 评估 |
|------|---------------------|------|
| **chain+capital×2-delivery×0.5** | **+0.555** | 最优，20/20期正相关，最低+0.34 |
| chain+capital(等权) | +0.540 | |
| chain only | +0.499 | |
| capital only | +0.496 | |
| V3总分(chain+delivery+capital) | +0.467 | delivery拖后腿 |
| delivery only | +0.100 | **几乎无预测力** |

验证脚本：`scripts/validate_anchor.py`（可复跑）

### 2. LLM排序是负优化（重要教训）

| 方法 | Spearman | 耗时 |
|------|----------|------|
| LLM多轮辩论从头排序 | **-0.14** | 40分钟 |
| LLM+量价动量prompt | -0.06 | 40分钟 |
| V3总分排序 | +0.47 | 秒级 |
| **量化锚排序** | **+0.555** | **1.7秒** |

**LLM从头排序会破坏量化信号**。即使给LLM量价数据并强调"动量延续"，它的排序仍不稳定。LLM在选股中的正确定位是**候选筛选+投资逻辑生成**，不是精确排序。

### 3. V3三子维度的预测力差异

| 维度 | 含义 | 更新频率 | 预测力 |
|------|------|---------|--------|
| chain | 产业链卡位(季度LLM打分) | 季度 | +0.55 (强) |
| capital | 资金热度(每日量化重算) | 每日 | +0.50 (强) |
| delivery | 业绩兑现(季度LLM打分) | 季度 | +0.10 (弱) |

capital每日动态更新逻辑：`picker/scoring/v3_full_score.py` 的 `update_capital()`，用研报板块动量(14天) × 个股量价(双窗口r5+r20)重算。

### 4. 特征交叉无效

已测试过以下交叉特征，**均不如线性公式 chain+capital×2-delivery×0.5**：
- chain×capital乘积：-0.01（引入噪声）
- chain²/capital²平方：-0.02~-0.04
- growth_assessment.growth_score叠加：-0.03（信号已被chain包含，双重计算）
- r20混入锚：在30只小样本上是负优化（全池有信号但抽样噪声大）

## 你要优化的两块

### 任务1：新晋股逻辑

**当前状态**：
- 新晋股 = 量价异动(V3<15 & r20>15%) + 板块供需型归因的股票
- 发现阶段：`picker/discovery/scan_mispriced.py`
- 加载阶段：`tradingagents/agents/picker/data_io.py` 的 `_load_rising_stars()`
- 现在全部进候选池（不限上限），保留真实v3参与量化排序竞争

**回测发现的问题**：
- 新晋股v3普遍偏低（均值~10），在量化锚排序里自然排末位
- 但回测显示：正常市场期(4月后)新晋股实际涨幅均值 vs 同v3档非新晋 = +6%超额
- 战争期(3月)新晋股反而跑输28%（金属/军工战后回调）
- **结论：新晋股有微弱超额收益，但不稳定，不应无条件加成**

**优化方向（待探索）**：
- 新晋股的归因质量参差不齐（回测模式研报为空时归因常为"未知"），能否提升归因准确性？
- 新晋股是否应该在锚分上做微调？（注意：任何加成都要回测验证Spearman不降）
- 板块扩散逻辑（同板块低分股跟随）是否有效？

### 任务2：增量信息接入

**当前状态**：
- 增量信息采集代码已存在但**未接入流程**：`tradingagents/agents/picker/incremental.py`
- 三分析师代码已存在但**未接入流程**：`tradingagents/agents/picker/analysts.py`（technical/fund/fundamental）
- 基线流程里这两个阶段被跳过了

**增量信息包含**：
- 实时财务（akshare最新财报，比fundamentals JSON更新）
- 近期新闻（东方财富搜索，按公司名）
- 量化信号（K线r5/r20/距高点/动量加速/量能放大/新高标记）
- 研报信号（research.db个股提及）

**回测发现**：
- r20在全池530只上有正相关(+0.2~+0.5)，但在抽样30只上噪声大
- 纯r20混入锚是负优化，但r20作为**独立的风险/机会信号**可能有效
- 实时财务/新闻的预测力**尚未测试**

**优化方向（待探索）**：
- 增量信息不应用于"排序"（LLM排序已证明有害），而应用于**风险调整**或**机会识别**
- 例如：近5日资金持续流出 → 锚分下调；近期有重大订单新闻 → 标记为机会
- 关键约束：任何调整都要用 `scripts/validate_anchor.py` 的方法回测验证Spearman不降

## 重要约束

1. **所有改动必须回测验证**：用 `scripts/validate_anchor.py` 跑21期Spearman，确保不低于 +0.50
2. **回测方法**：`--date YYYY-MM-DD` 传入历史截止日，K线/资金流会自动截断；scan_mispriced 支持 `cutoff_date` 跳过网络搜索（防前视偏差）
3. **V3分是慢变量**：chain/delivery季度更新，不适合作为"每日变化"的信号源；capital每日更新是唯一快变量
4. **delivery无用但有历史包袱**：V3缓存里存量数据包含delivery，不要删除字段，只在锚公式里降权
5. **LLM调用要克制**：基线1.7秒是因为零LLM；接入增量信息时如果调LLM，注意耗时和成本

## 关键文件索引

| 文件 | 作用 |
|------|------|
| `tradingagents/agents/picker/picker_graph.py` | 图编排（4节点基线） |
| `tradingagents/agents/picker/debaters.py` | 量化锚 `_anchor_score` + `make_ranking_debate` |
| `tradingagents/agents/picker/data_io.py` | 候选池加载 `load_top_n` + 新晋股 `_load_rising_stars` + 研报股 `_load_research_hot_stocks` |
| `tradingagents/agents/picker/analysts.py` | `collect_data`（数据采集）+ 三分析师（未接入） |
| `tradingagents/agents/picker/incremental.py` | 增量信息采集（未接入） |
| `tradingagents/agents/picker/judges.py` | `format_stock_brief` / `format_comparison_matrix`（展示用） |
| `picker/discovery/scan_mispriced.py` | 新晋股发现+归因（支持回测cutoff_date） |
| `picker/scoring/v3_full_score.py` | V3评分 + capital每日动态更新 |
| `scripts/validate_anchor.py` | 大规模锚分验证工具（21期×530只） |
| `scripts/test_deep_rank.py` | 深辩排序对比测试（v5/v6/v7，含Spearman） |
| `picker/data/data_cache.py` | K线缓存（90根） |

## 第一步建议

1. 先读 `CLAUDE.md` 的"选股系统架构"章节，理解完整数据流
2. **先读 `cognition/findings.md`** —— 本次及历次会话的实证发现索引(避免重复验证已否定的方案)
3. 跑一次 `uv run python3 scripts/validate_anchor.py` 确认基线Spearman
4. 跑一次 `uv run python3 picker/pipeline/debate_picker_v5.py --dry-run` 看基线输出
5. 选择一个方向（新晋股 or 增量信息）开始，每次改动后回测验证

---

# 2026-06-20 会话成果（新晋股优化 + 回测基础设施）

本节是 2026-06-20 会话的完整产出。**详细实证数据见 `cognition/findings.md`**，本节只给结论和接入指引。

## 核心结论：新晋股优化找到了可行方案（TOP9+1席）

经过 5 轮实验，唯一通过回测门槛的新晋股优化方案是：

> **锚排序取 TOP9 + 第10席给"最强新晋股"**（非 boost、非重排，而是结构性的席位预留）

### 五轮实验结论一览

| 轮次 | 方案 | 正常期结果 | 判定 |
|------|------|-----------|------|
| 1. 无差别 boost | 新晋股 chain 加固定分 B | TOP10 涨幅反降 | ❌ |
| 2. 内部信号挖掘 | 发现新晋股内 `chain+capital-delivery` ρ=+0.49 | (认知，非方案) | 📊 |
| 3. 子集重排 | star 用专属锚重算 | 19/20 期 TOP10 不变 | ❌ |
| 4. 价值检验 | star 跑赢率 18.7% vs 控制组 6.5% | 链路成立 | ✅ |
| **5. 席位预留** | **TOP9 + 1席最强 star** | **+5.47pp, ↑11↓0** | **✅** |

### 为什么前四轮都失败，第五轮成功

- **boost/rerank 失败根因**：新晋股的动量信号已被 capital（update_capital 含 r5/r20）充分捕捉，锚排序系数微调只会扰动全池，不会稳定改善 TOP10。
- **席位预留成功根因**：不扰动锚排序的头部 9 只（稳的），只把第 10 席"强制"给新晋股——而每期最强新晋股 20/20 期碾压 TOP10 末位。

### 选星指标（第10席用什么选）—— 有权衡，待定

去重评估（同一股跨期只计首次入选）后：

| 指标 | 均值 | min | 正收益 | 适用场景 |
|------|------|-----|--------|---------|
| **chain** | +57.3 | −2.6 | 91% | 要均值最大 |
| **chain×2+capital** | +51.1 | +3.1 | 100% | 要从不亏 |

- ⚠ `chain/5+r20/50` 在未去重时评分最高，但**去重后 min 掉到 −13.9**，是重复持有强势股的假象。
- **选哪个待 14 个月回测验证后定**（见下文"下一步"）。

### 生产接入指引（尚未实施）

接入点：`tradingagents/agents/picker/debaters.py` `make_ranking_debate()`（约 364 行）。

```python
# 当前
ordered = sorted(finalists, key=lambda x: -_anchor_score(x))[:top_k]

# 改为（伪代码）
anchor_top9 = sorted(finalists, key=lambda x: -_anchor_score(x))[:9]
in9 = {c["code"] for c in anchor_top9}
stars = [c for c in finalists if c.get("_rising_star") and c["code"] not in in9]
stars.sort(key=lambda c: -c.get("chain", 0))  # 或 chain×2+capital
final10 = anchor_top9 + stars[:1]  # 无 star 则该席回退给锚第10名
```

⚠ `r20` 字段已由 `collect_data`（analysts.py:105）算好并传入，零额外开销。

---

## 回测基础设施：已扩展到 14 个月

### 做了什么

1. **圈子研报历史回溯**：链式翻页 API 能到 2022-06。已回填到 **2025-03-27**（1089 帖），LLM 提取进行中。
   - 回填脚本：`picker/pipeline/backfill_research.py`（**只采集+提取，护栏禁止改 fundamentals**）
   - ⚠ 日常更新 `run_daily_update.py` 会改 fundamentals（step3），**回测回填绝不能用它**。
2. **K 线历史补采**：509 只补到 **300 根**（覆盖 2025-02 ~ 2026-06）。
   - 脚本：`picker/pipeline/backfill_klines.py`（增量合并，只补有 fundamentals 的股）
3. **K 线缓存过期作废**：`data_cache.py:53` `get()` 不再判过期（防止补采的长 K 线被重拉覆盖）。

### capital 历史重建（关键，已验证可行）

`capital = base_capital(板块动量) × price_factor(个股r5/r20)`，两部分都能历史重建：
- `price_factor`：纯 K 线，按 cutoff 截断算（`v3_full_score.py:305`）。
- `base_capital`：`get_sector_momentum(cutoff_date=)` **已支持**（`consumer.py:221`）。
- 重建 vs cache 差异显著（abs≥0.5 占 62%）——说明用当前 cache 回测确实有前视偏差。

### 仍有的前视偏差（无法完全消除）

- **chain/delivery**：季度 LLM 打分，只有当前快照，无历史版本。fundamentals JSON 会被 step3 重写。
- 折中：回测时 chain/delivery 用当前快照（标注"偏乐观"），capital 用 cutoff 重建（干净）。

---

## 新增的认知沉淀文件夹

`cognition/` 文件夹记录实证认知，避免换 agent 后丢失：
- `cognition/README.md` — 使用说明
- `cognition/findings.md` — 实证发现索引（按主题，附数据/代码位置/状态）
- `cognition/daily/` — 每日认知更新（本次会话的发现已写入 findings.md）

**规则**：每次回测验证出新结论 → 更新 `findings.md`；推翻旧结论 → 标注"❌ 已推翻"不删除。

---

## 下一步建议（优先级排序）

1. **等研报提取完成**（后台进行中），然后用 14 个月窗口重跑 `validate_anchor.py` 和 TOP9+1席方案，确认结论在长窗口下成立。
2. **决定选星指标**：14 个月回测下对比 chain vs chain×2+capital，选更优的接入生产。
3. **接入 TOP9+1席** 到 `make_ranking_debate`，实盘 dry-run 验证。
4. （可选）探索增量信息用于**风险调整**（交接文档原"任务2"，尚未触碰）。

## 本次会话新增的脚本/文件索引

| 文件 | 作用 |
|------|------|
| `cognition/findings.md` | 实证发现索引（最重要，先读这个） |
| `picker/pipeline/backfill_research.py` | 研报历史回填（只采集+提取，护栏禁止改fundamentals） |
| `picker/pipeline/backfill_klines.py` | K线补采（增量合并，只补有fundamentals的股） |
| `scripts/probe_xiaoe_history.py` | 圈子API历史深度探测 |
| `scripts/analyze_rising_star_boost.py` | boost可行性（已否定） |
| `scripts/analyze_star_rerank.py` | 重排可行性（已否定） |
| `scripts/analyze_star_chainmul.py` | chain×系数（正常期仅3只勉强过线） |
| `scripts/analyze_star_seat.py` | TOP9+1席（✅通过） |
| `scripts/analyze_star_top1_metric.py` | 第10席选星指标（去重后chain领先） |
| `scripts/build_price_factor_history.py` | price_factor 12变体历史快照生成 |
| `scripts/build_capital_history.py` | capital 历史快照（base_capital无前视） |
| `scripts/eval_price_factors.py` | price_factor 变体批量回测评估 |

---

# 2026-06-21 capital + 策略优化结论（最终落地版）

## 核心结论：G 模式 + TOP5 + 买1卖2 策略

经过多轮探索（price_factor 12变体 → A/D/G 对比 → 策略回测），最终落地方案：

**G 模式 capital + TOP5 选股 + 买1卖2 动态调仓策略。**

### 探索路径（重要教训）

```
price_factor 12变体 → 排序上都不显著 → A(去pf)ρ最优 → 但A换手率0.1(TOP10锁死)
→ 策略不可用 → D(换手3.0) → G(换手2.2) → TOP5+买1卖2 → 月均+31%, 100%月胜率
```

**关键教训：排序最优 ≠ 策略最优。price_factor 的真正价值是提供每日换手率（让TOP5动起来），不是提升排序质量。**

### 三种 capital 模式

| 模式 | 公式 | 换手率 | 用途 |
|------|------|--------|------|
| A | `capital = base_capital` | 0.1 ❌ | ρ最优但策略不可用 |
| D | `capital = base × price_factor` | 3.0 | 旧生产 |
| **G（默认）** | `capital = base + D2×2 + pf×2` | **2.2** | **策略最优** |

### 策略回测结果（2025-10~2026-06, 159天）

| 策略 | 月均 | 月胜率 | 日均收益 |
|------|------|--------|---------|
| **G TOP5 买1卖2** | **+31.4%** | **100%** | **+1.58%** |
| G TOP5 买2卖2 | +25.4% | 100% | +1.31% |
| G TOP10 买1卖2 | +23.1% | 100% | +1.20% |
| G TOP10 买2卖2 | +16.2% | 89% | +0.88% |

### 每日产出

`uv run python3 picker/pipeline/debate_picker_v5.py` 会产出：
1. **终端报告**：TOP5 + 🎯今日操作信号(买1卖2) + 逐股解读
2. **`data/caches/top5_history.json`**：TOP5历史（策略信号用，60天）
3. **`data/caches/daily_top50_review.json`**：**全池532只**得分快照（复盘用，180天，14字段/股）
4. **`results/picker_v5/`**：归档目录（完整中间数据）

### 改动文件

| 文件 | 改动 |
|------|------|
| `analysts.py:52` | CAPITAL_MODE 默认 G |
| `debate_picker_v5.py:33` | top-n 默认 9999（全池）; top-k 默认 5 |
| `picker_graph.py:36` | top_n 默认 9999（全池采集，不再预筛TOP50） |
| `data_io.py:455` | 入池排序 chain+cap（去delivery） |
| `v3_full_score.py:480` | G 模式 + D2 因子(`_compute_d2_factor`) |
| `strategy_signal.py` | 买1卖2 信号（新增） |
| `review_log.py` | 全池532只得分复盘记录（新增） |
| `reporter.py` | 集成策略信号 + 复盘记录 |

### base_capital 的单一研报源风险（已验证，未解决）

圈子只有 1 个团队（100% 研报来自同一源）：
- 覆盖偏差（仅 28 板块，传统行业排除）
- 过度看多（bullish/bearish 6.5x）
- 研报滞后（10+/50 期资金流热但研报冷）
- **真正解法**：补充信息源（第二个研报/资金流热度替代 base），非 price_factor 调整

详见 `cognition/findings.md` 第六章。
