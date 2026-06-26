# 实证发现索引

按主题组织,每条附:结论 / 数据支撑 / 代码位置 / 状态。
状态标记:✅ 成立 / ❌ 已否定 / ⚠ 有条件 / 🔬 待验证

---

> ⚠️ **2026-06-26 更新**: 本文档的 `delivery`(业绩兑现) 分析(全池 ρ=+0.10、新晋股内 −0.33、最优锚 `chain+capital×2-delivery×0.5` 等)均为**历史记录**。delivery 已替换为 **surge 爆发分**(30天超额收益概率=成长性加速拐点×催化近度), 锚公式改为 `chain+capital×2+surge×SURGE_WEIGHT`(+1.0, 待回测)。experiment 证实 delivery 全池 ρ=+0.082 正向, 原 −0.5 负权重是基于新晋股子池 −0.33 的错误外推(新晋股反向指标特性不适用全池)。下方 delivery 结论保留作历史参考。

## 一、量化锚公式

### ✅ 最优排序锚: `chain + capital×2 - delivery×0.5`

- **数据**: 21期 × 530只 × 30日窗口,Spearman = +0.555(后用更长窗口复核为 +0.541~+0.552),20/20 期正相关,最低 +0.34。
- **对比**: 远超 LLM 从头排序(−0.14)、V3总分排序(+0.47)、chain only(+0.50)。
- **代码**: `tradingagents/agents/picker/debaters.py:389` `_anchor_score()`。
- **教训**: LLM 排序会破坏量化信号。LLM 的正确定位是候选筛选 + 投资逻辑生成,不是精确排序。

### ✅ delivery 在全池弱(+0.10),在锚里做轻微惩罚(−0.5)

- 全池 delivery Spearman 仅 +0.10,拖后腿,所以在锚里给负权重。
- **但注意**: delivery 在【新晋股群体内部】是强反向指标,见下文。

---

## 二、新晋股 (Rising Stars)

### 定义
新晋股 = `v3<15 & r20>15%` 的量价异动股。生产里还要求 LLM 归因为板块供需型。
- 发现: `picker/discovery/scan_mispriced.py`
- 加载: `tradingagents/agents/picker/data_io.py:171` `_load_rising_stars()`
- 候选 dict 带 `_rising_star=True` 标记,一路传到 `_anchor_score`(未被读取)。

### ✅ 新晋股链路有价值,但价值在"进入候选池"不在"加分"

- **三轮 boost 实验(2026-06)全部失败**:
  - `scripts/analyze_rising_star_boost.py`: 无差别加 chain 分 → ❌ TOP10 涨幅反降。
  - `scripts/analyze_star_rerank.py`: star 子集用专属锚重排 → ❌ star 锚分太低推不动 TOP10。
  - `scripts/analyze_star_chainmul.py`: chain×系数(1.24~1.57) → 正常期仅 coef=1.30(3只)勉强过线,靠末期集中爆发,不稳。
- **根因**: 新晋股虽有超额收益(+13.8% 正常期 vs 同档非新晋),但已被 capital(update_capital 含 r5/r20)充分捕捉。锚排序系数微调只会扰动全池,不会稳定改善 TOP10。

### ✅ 新晋股在 TOP10 的出现率极低(0.5%),被锚系统性压出

- 正常期 12 期,纯锚 TOP10 里新晋股平均仅 0.05 只/期(200 席位中 1 只)。
- 候选池 star 占比 8.4%,TOP10 star 占比 0.5% → 压制强度 ~16x。
- 每期【最强】新晋股 20/20 期碾压 TOP10 末位(平均 +127pp),但这些强势股的锚分仍排 30 名开外。

### ✅ 新晋股内部排序信号: `chain+capital-delivery` 最强(+0.49)

- 新晋股池内部(n=877),各指标 vs 后续30日涨幅 Spearman:
  - `chain+capital-delivery`: **+0.481**(20/20 正)
  - `chain-delivery`: +0.469
  - `chain`: +0.370
  - delivery: **−0.236**(反向!与全池相反)
- **⚠ 但这个信号用于"选1只进TOP10"效果不稳**(见下,去重后 chain 反而更优)。

### ✅ delivery 在新晋股内是【反向指标】(−0.33)

- 全池 delivery 弱正向(+0.10),新晋股内强负向(−0.236~−0.33)。
- 投资逻辑: 新晋股=业绩未兑现的补涨标的,业绩兑现度高反而利好出尽。
- **可用作风险标签**(报告标注"业绩已兑现、补涨空间有限"),不适合做排序项。

---

## 三、新晋股选星指标优化(TOP9+1席方案)

### ✅ TOP9+1席方案: 锚排序 TOP9 + 1席最强新晋股

- **唯一通过门槛的方案**(对比 boost/rerank/chain×系数)。
- 正常期(剔3月战争期)12 期:TOP10 涨幅 +39.34% → **+44.81%**(Δ+5.47pp),↑11↓0(有 star 的期全胜)。
- **接入点**: `debaters.py make_ranking_debate` 取 TOP9 后,从 `_rising_star` 候选按某指标降序取 1 只填第10席。
- **⚠ 尚未接入生产**(截至 2026-06-20)。

### ⚠ 选星指标: chain 均值最高,但需注意"重复持有"假象

- **未去重**(同一股跨期重复选中): `chain/5+r20/50` 评分最高(夏普1.86,min+20.9)。
- **去重后**(同一股只计首次入选): **chain 重新领先**(均值+57.3,min−2.6);`chain/5+r20/50` min 掉到 −13.9。
- **教训**: 评估选星指标必须去重(chain 是季度慢变量,会反复选同一只强势股,造成虚高)。
- 去重后权衡:
  - 要均值最大 → **chain**(+57.3, min −2.6)
  - 要从不亏 → **chain×2+capital**(+51.1, min +3.1, 100%正收益)
- **🔬 待定**: 选 chain 还是 chain×2+capital,取决于风险偏好,需更长回测窗口验证。

---

## 四、回测的前视偏差问题(重要!)

### ⚠ 当前回测(4个月窗口)有两个前视偏差源

1. **chain/delivery 用当前快照**: V3 cache 是单文件当前快照,无历史版本。
   `fundamentals/{code}.json` 会被 `run_daily_update.py` step3 用当前 LLM 重写。
   → 用当前打分排序历史涨幅,等于偷看未来,Spearman 可能被高估。
2. **capital 用当前快照**: 但 capital 可以历史重建(见下)。

### ✅ capital 可以干净历史重建(唯一能重建的子维度)

- `capital = base_capital(板块动量) × price_factor(个股r5/r20)`
- `price_factor`: 纯 K 线计算(`v3_full_score.py:305`),按 cutoff 截断即可。
- `base_capital`: `get_sector_momentum(cutoff_date=)` **已支持**(`consumer.py:221`)。
- 重建 vs cache 差异:abs≥0.5 占 62%,abs≥1.0 占 39%(前视影响显著)。

### ✅ 圈子研报可回溯到 2022-06(实测)

- API 链式翻页(用 db 最早 feed_id 作 cursor 往回翻)能到 2022-06-15。
- 回填脚本 `picker/pipeline/backfill_research.py`(只采集+提取,**护栏禁止改 fundamentals**)。
- ⚠ 日常更新 `run_daily_update.py` 会改 fundamentals(step3),**回测回填绝不能用它**。

---

## 五、回测扩展(进行中 2026-06-20)

### 🔄 数据回溯进行中

- **研报**: 已采集到 2025-03-27(1089 帖),正在并行 LLM 提取。
- **K 线**: 补采到 300 根(≈14个月),`picker/pipeline/backfill_klines.py`。
- **K线缓存过期已作废**(`data_cache.py:53`):防止补采的长 K 线被判过期重拉覆盖。

### 回测工具索引

| 脚本 | 用途 |
|------|------|
| `scripts/validate_anchor.py` | 锚公式大规模验证(21期×530只) |
| `scripts/analyze_rising_star_boost.py` | 新晋股 boost 可行性(已否定) |
| `scripts/analyze_star_rerank.py` | 新晋股重排可行性(已否定) |
| `scripts/analyze_star_chainmul.py` | chain×系数机制(正常期仅3只勉强过线) |
| `scripts/analyze_star_seat.py` | TOP9+1席机制(✅ 通过) |
| `scripts/analyze_star_top1_metric.py` | 第10席选星指标优化(去重后 chain 领先) |
| `scripts/probe_xiaoe_history.py` | 圈子 API 历史深度探测 |

### 关键脚本产出缓存

- `data/caches/rising_star_boost_analysis.json`
- `data/caches/star_rerank_analysis.json`
- `data/caches/star_chainmul_analysis.json`
- `data/caches/star_seat_analysis.json`
- `data/caches/star_top1_metric_normal.json`
- `data/caches/price_factor_history.json` (12变体+base_capital, 50 cutoff)
- `data/caches/price_factor_eval.json`
- `data/caches/capital_history.json`

---

## 六、capital 优化:price_factor → base_capital (2026-06-21 完成)

### ✅ price_factor 12 变体全部不显著优于 r5/r20 基线

- **实验**: 设计 12 个 price_factor 变体(量价 A1-A4 / 个股资金流 B1-B3 / 层背离 C1-C2 / 行业共振 D1-D2),50 cutoff × 530只 回测。
- **结果**: 所有变体 Δρ < ±0.002(噪声),无一显著。
- **根因**: price_factor 是 base_capital 的乘子(0.6~1.3),而 base_capital(0.5~5.0)的方差远大于 pf,锚 `chain+capital×2` 里 chain(0~10)进一步稀释 pf 的影响。
- **代码**: `scripts/build_price_factor_history.py` + `scripts/eval_price_factors.py`

### ✅ capital 最优: A 方案(纯 base_capital,去掉 price_factor)

- **A(base单独)** vs **C(生产 base×pf)** vs **G(base+D2×2+pf×2)**,50 cutoff:
  | 方案 | TOP10均 | ρ | 说明 |
  |------|---------|---|------|
  | **A** | +24.15 | **+0.1917** | 纯板块热度,最简 |
  | C生产 | +22.88 | +0.1901 | base×pf(当前) |
  | G | +24.53 | +0.1836 | 三项叠加,TOP10均最高但方差大 |
- **A 的 ρ 最优,TOP10 也高**,且逻辑最简(去掉 pf)。

### ✅ A 和 G 在 TOP10 选股上等价(分位点分析)

- 500 只 TOP10 个股的分位点:除 95% 分位外(G +6.7),全部差异 < ±0.5。
- G 的"优势"集中在 top 5% 极端暴涨股(偶尔选到更爆的头部),非系统性。
- min/max/Q1/中位/Q3/内部分散度/亏损股数,A 和 G 几乎相同。
- **结论: A 和 G 不构成真正的选择差异,G 的均值高 +0.38pp 是噪声。**

### ⚠ base_capital 的单一研报源风险(已验证)

- **圈子只有 1 个团队**(100% 研报来自同一源),虽团队有质量但视角单一:
  - **覆盖偏差**: 仅覆盖 28 个标准板块,传统行业(地产/银行/纺织)系统性排除。
  - **过度看多**: bullish/bearish = 6.5x,capital 区分度被压缩。
  - **情绪固化**: 3 个板块(AI电源/固态电池/先进封装)长期 90%+ 单方向。
  - **研报滞后**: 50 期里 10+ 次出现"资金流>5亿但 base_capital<2.5"。
- **但**: G 的 D2+pf 不能修复这些风险(G 的 base 仍来自同一研报源)。
- **真正解法**: 补充信息源(第二个研报 / 资金流热度替代 base),不是调 price_factor。

### ✅ 动态切换(A/G)有效但收益有限

- 切换指标: 14 天研报数(实盘可得),≥80 用 G 否则用 A。
- switch≥80: TOP10 均 +24.95(比永远 A 高 +0.81pp),但 min 仍是 −10.26(G 踩雷期切不掉)。
- **保留为扩展点,默认用 A。**

### ✅ 已落地: CAPITAL_MODE = G (默认)

- **最终决策**: 经过多轮回测, capital 模式从 A → D → 最终定为 **G**。
- A 模式(纯 base_capital)虽然排序 ρ 最优, 但 **TOP10 换手率 0.1 只/天** (半年不变),
  导致策略无法动态调仓。G 模式换手率 2.2 只/天, 策略正常运转。
- **price_factor 的真正价值不是排序质量 (那个确实不显著), 而是提供每日换手率。**
- `analysts.py:52`: `CAPITAL_MODE` 默认 "G"。
- 三种模式:
  - `A`: capital = base_capital (ρ最优但换手率0.1, 策略不可用)
  - `D`: capital = base × price_factor (旧生产, 换手率3.0)
  - `G`(默认): capital = base + D2×2 + price_factor×2 (换手2.2, TOP5月均+31%)

### ✅ 最终策略: G模式 + TOP5 + 买1卖2

- **回测结果** (2025-10~2026-06, 159天):
  | 策略 | 月均收益 | 月胜率 | 日均组合收益 | 均持仓 |
  |------|---------|--------|------------|--------|
  | **G TOP5 买1卖2** | **+31.4%** | **100%** | **+1.58%** | 6.2只 |
  | G TOP5 买2卖2 | +25.4% | 100% | +1.31% | 4.8只 |
  | G TOP10 买1卖2 | +23.1% | 100% | +1.20% | 12.1只 |
  | G TOP10 买2卖2 | +16.2% | 89% | +0.88% | 9.7只 |
  | G TOP5 30天持有 | +17.0% | 89% | +0.91% | 11.5只 |
- **关键发现**:
  - TOP5 > TOP10 (头部5只质量显著高于6-10名, +6~9pp/月)
  - 买1 > 买2 (确认再买反而错过启动段, G模式TOP5已足够精准)
  - 30天持有最差 (固定持有期不如动态调仓)
- **落地**: `debate_picker_v5.py` 默认 top-k=5。

### ✅ 策略信号集成到生产报告

- `strategy_signal.py`: 买1卖2 信号生成 (记录历史TOP5, 算连续出现/消失次数)。
- `reporter.py`: 报告头部输出"🎯 今日操作"段落 (买入/卖出/持有/观察)。
- 历史存储: `data/caches/top5_history.json` (保留最近60天)。
- **每天跑 `debate_picker_v5` 即可看到当天操作信号。**

### ✅ 全量采集 + 全量复盘记录

- **collect_data 改为全池采集**: top_n=9999 (不再预筛TOP50), 全池532只都算 tech/fund/r5/r20。
  - 性能: 全池 tech+fund 计算 1.3 秒, 可接受。
  - 落地: `picker_graph.py:36` top_n 默认 9999; `debate_picker_v5.py:33` --top-n 默认 9999。
- `review_log.py`: 每天记录全池(~532只)的完整得分到历史文件, 不截断。
- 存储: `data/caches/daily_top50_review.json` (保留180天)。
- 每只股 14 个字段: rank/code/name/anchor/chain/delivery/capital/v3/tech_total/fund_5d/r5/r20/dist_high/momentum_factor。
- **复盘时可看到全池任何一只股的历史得分变化**, 不只是入池候选。

### ❌ 已推翻的结论 (A模式不可用)

- 之前"A模式ρ最优"是对的, 但忽略了换手率维度。
- A模式 TOP10 换手率 0.1 → 策略变"买入持有半年" → 失去动态调仓意义。
- **教训**: 评估 capital 模式不能只看排序质量(Spearman/TOP10收益), 必须同时看换手率。
  排序最优 ≠ 策略最优。
