#!/usr/bin/env python3
"""
全量回测 v7.0
────────────────────────────────────────────────────────
评分体系：
  世界知识(40%) + 技术分析(30%) + 估值PE(20%) + 市场溢价(10%)
────────────────────────────────────────────────────────
数据源：
  K线 → TickFlow + 本地缓存 (data_cache.KlineCache)
  估值 → 腾讯行情 (白名单已有PE/市值)
  行业 → AI知识库 (ai_knowledge_base)
  技术 → 趋势/动量/量能/形态 (tech_analysis)
────────────────────────────────────────────────────────
"""

import json, sys, os, time
from datetime import datetime
from typing import List, Dict

# 项目根加进 sys.path (兼容从子目录直接运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from picker import paths
from picker.knowledge.ai_knowledge_base import lookup_knowledge
from picker.scoring.fundamental_scorer import compute_fundamental_knowledge
from picker.data.data_cache import KlineCache
from picker.scoring.tech_analysis import compute_tech_score, TechScore

CACHE_DIR = paths.KLINE_CACHE_DIR
WHITELIST_FILE = paths.STOCK_WHITELIST


def score_v7(stock: Dict, tech: TechScore) -> float:
    """
    评分 v7：基本面知识驱动 + 技术辅助
    
    40分 基本面知识（fundamentals JSON 优先，无文件退回行业分）
    30分 技术分析 —— 趋势/动量/量能/形态，辅助择时
    20分 估值水平 —— PE合理区间
    10分 市场溢价 —— 科创/创业板成长溢价
    """
    pe = stock.get('pe_ttm')
    mcap = stock.get('mcap_yi', 0)
    market = stock.get('market', 'mainboard')
    code = stock.get('code', '')
    name = stock.get('name', '')

    # ═══ 1. 基本面知识 (40分) ═══
    fund_score = compute_fundamental_knowledge(code, name)
    if fund_score is not None:
        know_score = fund_score
    else:
        industry_score = stock.get('industry_score', 0)
        if industry_score >= 9.5:
            know_score = 16  # AI芯片 → Top500 P10 天花板
        elif industry_score >= 9.0:
            know_score = 15  # 光通信/半导体材料
        elif industry_score >= 8.5:
            know_score = 14  # 算力
        elif industry_score >= 8.0:
            know_score = 13  # 数据中心
        elif industry_score >= 7.5:
            know_score = 12  # 机器人
        elif industry_score >= 7.0:
            know_score = 11  # 锂电池/低空经济
        elif industry_score >= 6.5:
            know_score = 10  # 光伏/AI应用
        elif industry_score >= 6.0:
            know_score = 9  # 军工/信创
        elif industry_score >= 5.5:
            know_score = 8  # 消费电子
        else:
            know_score = 6  # 其他

    # ═══ 2. 技术分析 (30分) ═══
    tech_score = tech.total * 0.30

    # ═══ 3. 估值水平 (20分) ═══
    # PE在合理范围给满分，极端值扣分
    if pe and 15 <= pe <= 80:
        pe_score = 18
    elif pe and 5 <= pe < 15:
        pe_score = 14  # 低PE但可能缺乏成长
    elif pe and 80 < pe <= 200:
        pe_score = 14  # 高成长PE
    elif pe and pe > 200:
        pe_score = 10  # 极高PE
    elif pe and pe < 0:
        pe_score = 8   # 亏损
    else:
        pe_score = 14  # 无数据默认

    # 市值加分（合理市值区间+2分）
    if market == 'star' and 30 <= mcap <= 150:
        pe_score += 2
    elif market == 'gem' and 30 <= mcap <= 200:
        pe_score += 2
    elif market == 'mainboard' and 50 <= mcap <= 300:
        pe_score += 2

    pe_score = min(20, pe_score)

    # ═══ 4. 市场溢价 (10分) ═══
    if market == 'star':
        market_bonus = 10
    elif market == 'gem':
        market_bonus = 6
    else:
        market_bonus = 0

    total = know_score + tech_score + pe_score + market_bonus
    stock['_know_score'] = know_score
    stock['_tech_score'] = tech_score
    stock['_pe_score'] = pe_score
    stock['_market_score'] = market_bonus
    return round(total, 1)


def main():
    print("=" * 70)
    print("J-TradingAgents 全量回测 v7.0")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    # 加载白名单
    with open(WHITELIST_FILE, 'r') as f:
        whitelist = json.load(f)
    print(f"\n白名单: {len(whitelist)} 只")

    # 初始化缓存
    cache = KlineCache(CACHE_DIR)
    stats = cache.stats()
    print(f"K线缓存: 总计{stats['total']}只, 有效{stats['fresh']}只")

    # ═══ 阶段1：获取全部K线并计算涨幅 ═══
    print(f"\n{'─'*70}")
    print("阶段1: 获取K线 & 计算3个月涨幅")
    print(f"{'─'*70}")

    all_symbols = [
        f"{s['code']}.SH" if s['code'].startswith('6') else f"{s['code']}.SZ"
        for s in whitelist
    ]

    stocks_with_data = []
    total = len(whitelist)
    processed = 0
    batch_start = time.time()

    for i in range(0, total, 20):
        batch_symbols = all_symbols[i:i + 20]
        batch_stocks = whitelist[i:i + 20]

        klines = cache.batch_fetch(batch_symbols, count=60)

        for j, sym in enumerate(batch_symbols):
            stock = batch_stocks[j]
            df = klines.get(sym)

            if df is not None and len(df) >= 20:
                closes = df['close'].values
                ret = (closes[-1] - closes[0]) / closes[0] * 100
                stock['return_pct'] = round(ret, 2)
                stock['_df'] = df  # 暂存DataFrame，用于技术分析
                stocks_with_data.append(stock)
            else:
                stock['return_pct'] = None
                stock['_df'] = None

        processed += len(batch_stocks)
        if processed % 500 == 0:
            elapsed = time.time() - batch_start
            print(f"  进度: {processed}/{total} ({len(stocks_with_data)}只有K线) | 耗时{elapsed:.0f}s")

    print(f"\n  完成: {len(stocks_with_data)}/{total} 只股票有完整K线数据")

    # ═══ 阶段2：行业知识 + 技术分析 ═══
    print(f"\n{'─'*70}")
    print("阶段2: 世界知识 + 技术分析")
    print(f"{'─'*70}")

    kb_matched = 0
    for stock in stocks_with_data:
        code = stock['code']
        df = stock.pop('_df', None)

        # 知识库匹配
        industries, score = lookup_knowledge(code, stock['name'])
        if industries:
            stock['industries'] = industries
            stock['industry_score'] = score
            kb_matched += 1
        else:
            stock['industries'] = []
            stock['industry_score'] = 0

        # 技术分析
        if df is not None:
            stock['tech'] = compute_tech_score(df)
        else:
            stock['tech'] = TechScore()

    print(f"  知识库匹配: {kb_matched}/{len(stocks_with_data)} ({kb_matched/len(stocks_with_data)*100:.1f}%)")

    # ═══ 阶段3：v7评分 ═══
    print(f"\n{'─'*70}")
    print("阶段3: v7综合评分")
    print(f"{'─'*70}")

    for stock in stocks_with_data:
        stock['total_score'] = score_v7(stock, stock['tech'])

    scored = sorted(stocks_with_data, key=lambda x: x.get('total_score', 0), reverse=True)
    by_return = sorted(stocks_with_data, key=lambda x: x.get('return_pct', 0) or -999, reverse=True)

    score_rank = {s['code']: i + 1 for i, s in enumerate(scored)}
    return_rank = {s['code']: i + 1 for i, s in enumerate(by_return)}

    # ═══ 阶段4：分析报告 ═══
    print(f"\n{'='*130}")
    print("涨幅 TOP30 vs v7评分排名对照")
    print(f"{'='*130}")
    hdr = f"{'#':>3} {'代码':>10} {'名称':>10} {'涨幅%':>8} {'v7评分':>7} {'知识':>5} {'技术':>5} {'PE':>8} {'市值亿':>8} {'市场':>6} {'行业':>14} {'评分#':>6} {'涨幅#':>6}"
    print(hdr)
    print('-' * 130)

    for i, s in enumerate(by_return[:30]):
        code = s['code']
        rtn = s.get('return_pct') or 0
        total = s.get('total_score', 0)
        know_s = s.get('_know_score', 0)
        tech_t = s['tech'].total
        pe = s.get('pe_ttm', '-')
        mcap = s.get('mcap_yi', '-')
        market = {'mainboard': 'A股', 'gem': '创业板', 'star': '科创板'}.get(s.get('market', ''), 'A股')
        inds = ','.join(s.get('industries', []))[:12]
        sr = score_rank.get(code, '-')
        rr = return_rank.get(code, '-')

        print(f"{i+1:>3} {code:>10} {s['name'][:8]:>10} {rtn:>8.1f} {total:>7.1f} {know_s:>5.0f} "
              f"{tech_t:>5.1f} {str(pe):>8} {str(mcap):>8} {market:>6} {inds:>14} {sr:>6} {rr:>6}")

    # 重叠率
    print(f"\n{'─'*70}")
    print("重叠率统计")
    print(f"{'─'*70}")
    for n in [10, 20, 30, 50, 100]:
        ret_set = {s['code'] for s in by_return[:n]}
        scr_set = {s['code'] for s in scored[:n]}
        overlap = ret_set & scr_set
        print(f"  TOP{n}: {len(overlap)}只重叠 ({len(overlap)/n*100:.0f}%)")

    # 评分分段涨幅
    print(f"\n{'─'*70}")
    print("评分分段 vs 实际涨幅")
    print(f"{'─'*70}")
    total_n = len(scored)

    for lo, hi, label in [(0, 40, "评分<40"), (40, 50, "评分40-50"),
                           (50, 60, "评分50-60"), (60, 70, "评分60-70"),
                           (70, 80, "评分70-80"), (80, 100, "评分≥80")]:
        group = [s for s in scored if lo <= s.get('total_score', 0) < hi]
        if group:
            avg_ret = sum(s.get('return_pct', 0) or 0 for s in group) / len(group)
            pos_pct = sum(1 for s in group if (s.get('return_pct') or 0) > 0) / len(group) * 100
            kb_pct = sum(1 for s in group if s.get('industries')) / len(group) * 100
            print(f"  {label:12} {len(group):>5}只 | 均涨幅{avg_ret:>+7.2f}% | 正收益率{pos_pct:>5.0f}% | 知识覆盖{kb_pct:>5.0f}%")

    # TOP 20% vs Bottom 20%
    top20pct = total_n // 5
    top20 = scored[:top20pct]
    bot20 = scored[-top20pct:]
    avg_top = sum(s.get('return_pct', 0) or 0 for s in top20) / len(top20)
    avg_bot = sum(s.get('return_pct', 0) or 0 for s in bot20) / len(bot20)

    print(f"\n  ═══ 前20% vs 后20% ═══")
    print(f"  评分前20% ({len(top20)}只): 均涨幅 {avg_top:.2f}%")
    print(f"  评分后20% ({len(bot20)}只): 均涨幅 {avg_bot:.2f}%")
    print(f"  区分度: {avg_top - avg_bot:.2f}%")

    # 知识覆盖 vs 无知识覆盖
    with_kb = [s for s in scored if s.get('industries')]
    without_kb = [s for s in scored if not s.get('industries')]
    if with_kb and without_kb:
        avg_wk = sum(s.get('return_pct', 0) or 0 for s in with_kb) / len(with_kb)
        avg_nk = sum(s.get('return_pct', 0) or 0 for s in without_kb) / len(without_kb)
        print(f"\n  ═══ 知识覆盖 vs 无覆盖 ═══")
        print(f"  有知识库覆盖 ({len(with_kb)}只): 均涨幅 {avg_wk:.2f}%")
        print(f"  无知识库覆盖 ({len(without_kb)}只): 均涨幅 {avg_nk:.2f}%")
        print(f"  知识溢价: {avg_wk - avg_nk:.2f}%")

    # 最终推荐 TOP 15
    print(f"\n{'='*120}")
    print("v7 评分 TOP15 推荐")
    print(f"{'='*120}")
    print(f"{'#':>3} {'代码':>10} {'名称':>10} {'评分':>7} {'知识':>5} {'技术':>5} {'PE':>8} {'市值亿':>8} {'市场':>6} {'行业':>16} {'3月涨':>8}")
    print('-' * 120)
    for i, s in enumerate(scored[:15]):
        code = s['code']
        total = s.get('total_score', 0)
        tech_t = s['tech'].total
        know_s = s.get('_know_score', 0)
        pe = s.get('pe_ttm', '-')
        mcap = s.get('mcap_yi', '-')
        market = {'mainboard': 'A股', 'gem': '创业板', 'star': '科创板'}.get(s.get('market', ''), 'A股')
        inds = ','.join(s.get('industries', []))[:15]
        rtn = s.get('return_pct') or 0

        print(f"{i+1:>3} {code:>10} {s['name'][:8]:>10} {total:>7.1f} {know_s:>5.0f} {tech_t:>5.1f} "
              f"{str(pe):>8} {str(mcap):>8} {market:>6} {inds:>16} {rtn:>+7.1f}%")

    # 总结
    print(f"\n{'='*70}")
    print("回测结论")
    print(f"{'='*70}")
    print(f"  样本: {len(stocks_with_data)} 只")
    print(f"  评分体系: 世界知识40% + 技术分析30% + 估值PE20% + 市场10%")
    print(f"  知识库覆盖: {kb_matched}只 ({kb_matched/len(stocks_with_data)*100:.1f}%)")
    print(f"  前20% vs 后20%区分度: {avg_top - avg_bot:.2f}%")
    if with_kb and without_kb:
        print(f"  知识溢价: {avg_wk - avg_nk:.2f}%")
    print(f"\n  【风险提示】回测仅供参考，不构成投资建议")


if __name__ == "__main__":
    main()