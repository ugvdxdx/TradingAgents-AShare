"""分析涨幅TOP50的股票特征，找出评分优化方向"""
import json
from tickflow import TickFlow
from run_stock_picker import match_by_name, score_and_rank

tf = TickFlow.free()

with open("stock_whitelist.json") as f:
    whitelist = json.load(f)

print("获取500只股票的3个月涨幅...")
top_stocks = []
processed = 0
total = min(500, len(whitelist))

for i in range(0, total, 20):
    batch = whitelist[i:i+20]
    symbols = [f"{s['code']}.SH" if s['code'].startswith('6') else f"{s['code']}.SZ" for s in batch]
    
    try:
        dfs = tf.klines.batch(symbols, period="1d", count=60, as_dataframe=True)
        for j, sym in enumerate(symbols):
            df = dfs.get(sym)
            if df is not None and len(df) >= 20:
                closes = df['close'].values
                ret = (closes[-1] - closes[0]) / closes[0] * 100
                s = batch[j]
                top_stocks.append({
                    'code': s['code'], 'name': s['name'],
                    'return_pct': round(ret, 2),
                    'pe_ttm': s.get('pe_ttm'), 'mcap_yi': s.get('mcap_yi'),
                    'market': s.get('market'),
                })
    except:
        pass
    processed += len(batch)
    print(f"  进度: {processed}/{total}")

top_stocks.sort(key=lambda x: x.get('return_pct', 0), reverse=True)

# 特征分析
top50 = top_stocks[:50]
bottom50 = top_stocks[-50:]

print(f"\n{'='*70}")
print("涨幅TOP50 vs 跌幅TOP50 特征对比")
print(f"{'='*70}")

for group, label in [(top50, "涨幅TOP50"), (bottom50, "跌幅TOP50")]:
    pes = [s.get('pe_ttm') for s in group if s.get('pe_ttm') and s['pe_ttm'] > 0]
    mcaps = [s.get('mcap_yi') for s in group if s.get('mcap_yi')]
    neg_pe = sum(1 for s in group if s.get('pe_ttm') and s['pe_ttm'] < 0)
    
    print(f"\n{label}:")
    print(f"  平均涨幅: {sum(s['return_pct'] for s in group)/len(group):.1f}%")
    if pes:
        pes_sorted = sorted(pes)
        print(f"  PE中位数: {pes_sorted[len(pes)//2]:.1f}")
        print(f"  PE<30占比: {sum(1 for p in pes if p < 30)/len(pes)*100:.0f}%")
        print(f"  PE>100占比: {sum(1 for p in pes if p > 100)/len(pes)*100:.0f}%")
    print(f"  亏损个数(PE<0): {neg_pe}/{len(group)}")
    if mcaps:
        print(f"  平均市值: {sum(mcaps)/len(mcaps):.0f}亿")
        print(f"  市值<100亿: {sum(1 for m in mcaps if m < 100)/len(mcaps)*100:.0f}%")
        print(f"  市值>500亿: {sum(1 for m in mcaps if m > 500)/len(mcaps)*100:.0f}%")
    
    matched = sum(1 for s in group if match_by_name(s['name'], s['code'])[1] > 0)
    print(f"  可匹配行业: {matched}/{len(group)}")

# 对TOP50评分
for s in top50:
    ind, sc = match_by_name(s['name'], s['code'])
    s['industries'] = ind
    s['industry_score'] = sc

scored = score_and_rank(top50)
print(f"\n{'='*70}")
print("当前v5评分对涨幅TOP50的排名（看前10名在涨幅中的位置）")
print(f"{'='*70}")
by_return = sorted(scored, key=lambda x: x.get('return_pct', 0), reverse=True)
return_rank = {s['code']: i+1 for i, s in enumerate(by_return)}

for i, s in enumerate(scored[:10]):
    rr = return_rank.get(s['code'], '?')
    print(f"  评分#{i+1}: {s['code']} {s['name']} 评分{s['total_score']:.1f} 涨幅{s['return_pct']:.1f}% 涨幅排名#{rr} PE={s.get('pe_ttm','-')}")

# 当前评分的有效区分度
print(f"\n{'='*70}")
print("评分前20% vs 后20% 在500只样本中的涨幅对比")
print(f"{'='*70}")
all_scored = score_and_rank(top_stocks)
n = len(all_scored)
top_pct = all_scored[:n//5]
bot_pct = all_scored[-n//5:]
avg_top = sum(s['return_pct'] for s in top_pct) / len(top_pct)
avg_bot = sum(s['return_pct'] for s in bot_pct) / len(bot_pct)
print(f"  评分前20% ({len(top_pct)}只) 平均涨幅: {avg_top:.2f}%")
print(f"  评分后20% ({len(bot_pct)}只) 平均涨幅: {avg_bot:.2f}%")
print(f"  区分度: {avg_top - avg_bot:.2f}%")

# 理想权重模拟 - 如果只使用PE+市值+市场，不用行业
print(f"\n{'='*70}")
print("如果去掉行业维度，只用PE+市值+市场打分效果如何？")
print(f"{'='*70}")
for s in all_scored:
    pe = s.get('pe_ttm')
    mcap = s.get('mcap_yi', 0)
    market = s.get('market', 'mainboard')
    
    pe_score = 15
    if pe and pe > 0:
        if pe < 10: pe_score = 28
        elif pe < 30: pe_score = 24
        elif pe < 80: pe_score = 20
        elif pe < 300: pe_score = 16
        else: pe_score = 15
    elif pe and pe < 0: pe_score = 8
    
    if market == 'star':
        mcap_score = 25 if 30 <= mcap <= 150 else 22 if mcap <= 300 else 18
    elif market == 'gem':
        mcap_score = 25 if 30 <= mcap <= 200 else 22 if mcap <= 500 else 18
    else:
        mcap_score = 25 if 50 <= mcap <= 300 else 22 if mcap <= 800 else 18
    
    market_bonus = 10 if market == 'star' else 5 if market == 'gem' else 0
    s['alt_score'] = pe_score + mcap_score + market_bonus

alt_sorted = sorted(all_scored, key=lambda x: x.get('alt_score', 0), reverse=True)
alt_top = alt_sorted[:n//5]
alt_bot = alt_sorted[-n//5:]
avg_alt_top = sum(s['return_pct'] for s in alt_top) / len(alt_top)
avg_alt_bot = sum(s['return_pct'] for s in alt_bot) / len(alt_bot)
print(f"  仅PE+市值+市场 前20% 平均涨幅: {avg_alt_top:.2f}%")
print(f"  仅PE+市值+市场 后20% 平均涨幅: {avg_alt_bot:.2f}%")
print(f"  区分度(去行业): {avg_alt_top - avg_alt_bot:.2f}%")

print(f"\n{'='*70}")
print("结论与建议")
print(f"{'='*70}")
print(f"当前v5区分度在前500只中: 前20%={avg_top:.2f}% 后20%={avg_bot:.2f}% 差距={avg_top-avg_bot:.2f}%")
print(f"去掉行业后的区分度: 前20%={avg_alt_top:.2f}% 后20%={avg_alt_bot:.2f}% 差距={avg_alt_top-avg_alt_bot:.2f}%")