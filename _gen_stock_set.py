"""获取市值Top500 + 各行业龙头股，去重得到最终股票集合"""
import tushare as ts, os, json, collections
from dotenv import load_dotenv
load_dotenv()
ts.set_token(os.getenv('TUSHARE_TOKEN'))
pro = ts.pro_api()

# 1. 获取市值排名
df = pro.daily_basic(trade_date='20260605', fields='ts_code,total_mv')
df = df.dropna(subset=['total_mv'])
df = df.sort_values('total_mv', ascending=False)
df['code'] = df['ts_code'].str[:6]
top500 = df.head(500).copy()
print(f'Top500市值范围: {top500["total_mv"].iloc[-1]:.0f}万 ~ {top500["total_mv"].iloc[0]:.0f}万')

# 2. 获取行业分类
ind = pro.stock_basic(fields='ts_code,industry,name')
ind['code'] = ind['ts_code'].str[:6]
merged = top500.merge(ind, on='code', how='left')

# 行业分布
ind_counts = collections.Counter(merged['industry'].dropna())
print(f'\nTop500行业分布:')
for k, v in ind_counts.most_common():
    print(f'  {v:3d}  {k}')

# 3. 各行业选龙头（不在Top500中的）
# 获取所有股票的行业和市值
all_df = pro.daily_basic(trade_date='20260605', fields='ts_code,total_mv')
all_df = all_df.dropna(subset=['total_mv'])
all_df['code'] = all_df['ts_code'].str[:6]
all_ind = pro.stock_basic(fields='ts_code,industry,name')
all_ind['code'] = all_ind['ts_code'].str[:6]
all_merged = all_df.merge(all_ind, on='code', how='left')

top500_codes = set(top500['code'].tolist())

# 按行业分组，选市值最大的龙头
industry_leaders = {}
for industry_name, group in all_merged.groupby('industry'):
    if not industry_name or industry_name == 'None':
        continue
    group = group.sort_values('total_mv', ascending=False)
    # 热门行业多选
    hot_industries = ['半导体', '通信设备', '消费电子', '光模块', '计算机应用', '军工', '新能源', '电力设备', '医药生物', '人工智能']
    n = 10 if industry_name in hot_industries else 5
    leaders = group.head(n)
    leaders_not_in_top500 = leaders[~leaders['code'].isin(top500_codes)]
    if len(leaders_not_in_top500) > 0:
        industry_leaders[industry_name] = leaders_not_in_top500[['code','name','industry','total_mv']].to_dict('records')

# 合并
final_set = {}
# 先加Top500
for _, row in merged.iterrows():
    final_set[row['code']] = {'code': row['code'], 'name': row['name'], 'industry': row['industry'], 'total_mv': row['total_mv'], 'source': 'top500'}
# 再加行业龙头
for ind_name, leaders in industry_leaders.items():
    for l in leaders:
        if l['code'] not in final_set:
            final_set[l['code']] = {**l, 'source': 'industry_leader'}

print(f'\n最终股票集合: {len(final_set)} 只')
print(f'  Top500: {len([v for v in final_set.values() if v["source"]=="top500"])}')
print(f'  行业龙头补充: {len([v for v in final_set.values() if v["source"]=="industry_leader"])}')

# 保存
with open('/tmp/final_stock_set.json', 'w') as f:
    json.dump(list(final_set.values()), f, ensure_ascii=False, indent=2)
print(f'\n已保存到 /tmp/final_stock_set.json')
