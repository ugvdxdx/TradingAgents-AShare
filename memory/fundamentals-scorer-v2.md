# 基本面评分 & 赛道Alpha选股

基于回测验证的双维度评分：**赛道动量是Alpha信号，基本面质量是风控底线**。

## 回测结论（539只A股，2025.12-2026.06）

| 发现 | 数据 |
|:---|:---|
| 赛道动量分 vs 半年涨幅 | Spearman **ρ = 0.556** (p<0.001) |
| 基本面分 vs 半年涨幅 | ρ = 0.039 (不显著) |
| 五等分 Q1→Q5 涨幅 | **-1.6% → +1.1% → +13.8% → +61.8% → +134.8%** |
| 赛道Top20 均涨幅 | **+109.78%** |
| 赛道Bottom20 均涨幅 | -2.31% |
| 多空收益差 | **+112.09%** |
| 基本面过滤(≥10)提升信号 | ρ 0.556 → **0.580** |

## 推荐用法

```python
from fundamental_scorer import compute_sector_alpha

result = compute_sector_alpha("300502")
# → {"sector_score": 24, "fundamental_score": 22, 
#    "filter_pass": True, "recommendation": "BUY"}
```

1. 遍历股票池，调用 `compute_sector_alpha`
2. 过滤 `filter_pass=False`（基本面<10）
3. 按 `sector_score` 降序排名
4. `sector≥15` → BUY，`8-14` → WATCH，`<8` → PASS

## 评分维度

| 维度 | 满分 | 作用 | 回测ρ |
|:---|:---:|:---|:---:|
| 赛道动量 | 25 | Alpha 信号 | 0.556 ★★★ |
| 基本面质量 | 25 | 风控底线（阈值≥10） | 0.039 |

## 相关文件

| 文件 | 用途 |
|:---|:---|
| `picker/scoring/fundamental_scorer.py` | 核心评分模块 |
| `picker/knowledge/fundamental_agent.py` | 基本面数据生成 |
| `skills/fundamentals-scorer/scripts/batch_score.py` | 批量打分 |
| `skills/fundamentals-scorer/scripts/backtest_correlation.py` | 相关性回测 |