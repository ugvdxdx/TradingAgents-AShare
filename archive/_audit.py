"""导出所有fundamentals的股票列表和当前数据摘要"""
import json, os

fund_dir = 'fundamentals'
files = sorted([f for f in os.listdir(fund_dir) if f.endswith('.json')])

stocks = []
for f in files:
    path = os.path.join(fund_dir, f)
    with open(path, 'r', encoding='utf-8') as fp:
        d = json.load(fp)
    code = f.replace('.json', '')
    name = d.get('name', '')
    industry = d.get('business_overview', {}).get('industry', '')
    wtd = d.get('business_overview', {}).get('what_they_do', '')
    strengths = d.get('competitive_analysis', {}).get('strengths', [])
    weaknesses = d.get('competitive_analysis', {}).get('weaknesses', [])
    growth = d.get('growth_assessment', {}).get('growth_drivers', [])
    headwinds = d.get('growth_assessment', {}).get('headwinds', [])
    metrics = d.get('financial_health', {}).get('key_metrics', {})

    stocks.append({
        'code': code,
        'name': name,
        'industry': industry,
        'wtd_len': len(wtd),
        'wtd_preview': wtd[:80],
        'strengths': strengths,
        'weaknesses': weaknesses,
        'growth': growth,
        'headwinds': headwinds,
        'revenue_yi': metrics.get('revenue_yi', 0),
        'net_profit_yi': metrics.get('net_profit_yi', 0),
        'roe_pct': round(metrics.get('roe_pct', 0) or 0, 1),
        'gross_margin_pct': round(metrics.get('gross_margin_pct', 0) or 0, 1),
        'debt_ratio_pct': round(metrics.get('debt_ratio_pct', 0) or 0, 1),
    })

# 保存完整列表
with open('_stocks_audit.json', 'w', encoding='utf-8') as f:
    json.dump(stocks, f, ensure_ascii=False, indent=2)

# 统计
print(f'总数: {len(stocks)}')

# 按行业统计
from collections import Counter
ind_count = Counter(s['industry'] for s in stocks)
print(f'\n行业分布 (top20):')
for ind, cnt in ind_count.most_common(20):
    print(f'  {ind}: {cnt}')

# 需要重写的（模板strengths或空growth）
template_kw = ['规模优势', '产业链布局完整', '技术研发投入大', '细分市场竞争力强',
               '制造业规模优势', '产能规模大', '成本优势明显', '品牌合作资源丰富',
               '零售渠道覆盖广', '内容制作能力强', '渠道覆盖广', '技术壁垒高',
               '下游需求旺盛', '技术研发投入大']

need_rewrite = []
for s in stocks:
    is_template = False
    if not s['strengths']:
        is_template = True
    elif any(any(kw in st for kw in template_kw) for st in s['strengths']):
        is_template = True
    if not s['growth'] or not s['headwinds']:
        is_template = True
    if is_template:
        need_rewrite.append(s)

print(f'\n需要重写: {len(need_rewrite)}')

# 保存需要重写的列表
with open('_need_rewrite.json', 'w', encoding='utf-8') as f:
    json.dump(need_rewrite, f, ensure_ascii=False, indent=2)

# 按行业分组
ind_groups = {}
for s in need_rewrite:
    ind = s['industry']
    if ind not in ind_groups:
        ind_groups[ind] = []
    ind_groups[ind].append(s['code'])

print(f'\n需重写行业分布:')
for ind in sorted(ind_groups.keys()):
    print(f'  {ind}: {len(ind_groups[ind])} 只')
