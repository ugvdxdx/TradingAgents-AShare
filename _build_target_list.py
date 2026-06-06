"""构建待生成股票集合并保存到 .target_stocks.txt"""
import json, os

# 行业热度：取前N只龙头
# AI主线 > 新能源进半导体 > 消费医药 > 传统
INDUSTRY_LEADER_COUNT = {
    "电子": 12,
    "计算机": 10,
    "通信": 10,
    "电力设备": 10,
    "汽车": 8,
    "国防军工": 7,
    "医药生物": 7,
    "有色金属": 7,
    "机械设备": 6,
    "基础化工": 5,
    "食品饮料": 5,
    "家用电器": 5,
    "银行": 8,
    "非银金融": 5,
    "公用事业": 5,
    "交通运输": 4,
    "建筑装饰": 3,
    "传媒": 3,
    "农林牧渔": 3,
    "钢铁": 3,
    "轻工制造": 3,
    "商贸零售": 3,
    "环保": 3,
    "石油石化": 4,
    "煤炭": 5,
    "房地产": 2,
    "纺织服饰": 2,
    "社会服务": 3,
    "建筑材料": 3,
    "美容护理": 1,
    "综合": 1,
}

# 1. 加载行业缓存
with open('.cache/stockapi_industry.json') as f:
    industry_map = json.load(f)

# 2. 加载白名单
with open('stock_whitelist.json') as f:
    wl = json.load(f)
wl.sort(key=lambda x: x.get('mcap_yi', 0), reverse=True)

# 3. 加载已有
existing = set()
for f in os.listdir('fundamentals'):
    if f.endswith('.json'): existing.add(f.replace('.json',''))

# 4. 为所有股票补充行业
for s in wl:
    code = s['code']
    raw = industry_map.get(code, '其他')
    # 取一级行业
    s['_industry1'] = raw.split('-')[0] if '-' in raw else raw
    s['_industry_full'] = raw

# 5. 构建候选
target_codes = set()

# 5a. 市值前500
for s in wl[:500]:
    target_codes.add(s['code'])
print(f"市值前500: {len(target_codes)}只")

# 5b. 各行业龙头
for ind, count in sorted(INDUSTRY_LEADER_COUNT.items(), key=lambda x: -x[1]):
    ind_stocks = [s for s in wl if s['_industry1'] == ind]
    if not ind_stocks:
        print(f"  {ind}: 无股票")
        continue
    ind_stocks.sort(key=lambda x: x.get('mcap_yi', 0), reverse=True)
    for s in ind_stocks[:count]:
        target_codes.add(s['code'])
    added = sum(1 for s in ind_stocks[:count] if s['code'] in target_codes)
    print(f"  {ind}: {len(ind_stocks)}只, 取前{count} -> 候选{added}只")

print(f"\n合并去重后: {len(target_codes)}只")

# 6. 过滤已有
need = [c for c in target_codes if c not in existing]
print(f"已生成: {len(target_codes) - len(need)}只")
print(f"待生成: {len(need)}只")

# 7. 构建 code->info 映射
code_to_stock = {s['code']: s for s in wl}

need_sorted = sorted(need, key=lambda c: code_to_stock[c].get('mcap_yi', 0), reverse=True)

# 按行业统计
dist = {}
for c in need_sorted:
    ind = code_to_stock[c]['_industry1']
    dist[ind] = dist.get(ind, 0) + 1
print("\n待生成行业分布:")
for ind, cnt in sorted(dist.items(), key=lambda x: -x[1]):
    print(f"  {ind}: {cnt}只")

# 8. 保存待生成列表
target_files = []
for c in need_sorted:
    s = code_to_stock[c]
    target_files.append({
        "code": c,
        "name": s.get('name', ''),
        "industry": s['_industry1'],
        "industry_full": s.get('_industry_full', ''),
        "mcap_yi": s.get('mcap_yi', 0),
    })

with open('.need_generate.json', 'w') as f:
    json.dump(target_files, f, ensure_ascii=False, indent=2)
print(f"\n待生成列表已保存到 .need_generate.json ({len(target_files)}只)\n")

# 9. 显示前30只
print("前30只:")
for s in target_files[:30]:
    print(f"  {s['code']} {s['name']:10s} {s['industry']:10s} {s['mcap_yi']:>8.0f}亿")
