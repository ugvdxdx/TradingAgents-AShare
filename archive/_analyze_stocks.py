"""分析待生成股票集合：市值前500 + 各行业龙头"""
import json, os

# 已知行业热度：AI主线 > 新能源/半导体/军工 > 消费/医药 > 传统
HOT_INDUSTRIES = {
    "通信设备": 10, "半导体": 10, "半导体设备": 8, "IT设备": 10,
    "元器件": 8, "消费电子": 8,
    "锂电池": 7, "汽车整车": 7, "汽车配件": 7, "小金属": 7,
    "新型电力": 5, "水力发电": 3, "火力发电": 3, "煤炭": 5, "石油开采": 5,
    "化学制药": 6, "生物制药": 6, "医疗保健": 5, "中成药": 5,
    "白酒": 5, "食品": 5, "家电": 5,
    "银行": 8, "证券": 5, "保险": 3,
    "军工": 6, "工程机械": 5,
    "软件服务": 5, "互联网": 5,
    "仓储物流": 3, "商贸代理": 3, "广告包装": 3,
    "化工原料": 5, "钢铁": 3, "建材": 3, "房地产": 2,
    "石油加工": 3, "农业综合": 3,
    "安防": 3, "教育": 2, "其他": 3,
}

# 1. 加载 SK
with open('_regen_fundamentals.py') as f:
    source = f.read()
start = source.find("STOCK_KNOWLEDGE = {")
brace_start = source.index("{", start)
depth, i = 0, brace_start
while i < len(source):
    if source[i] == "{": depth += 1
    elif source[i] == "}":
        depth -= 1
        if depth == 0: break
    i += 1
sk = eval(source[brace_start:i+1])

# 2. 加载白名单
with open('stock_whitelist.json') as f:
    wl = json.load(f)
wl.sort(key=lambda x: x.get('mcap_yi', 0), reverse=True)

# 3. 已有
existing = set()
for f in os.listdir('fundamentals'):
    if f.endswith('.json'): existing.add(f.replace('.json',''))

# 4. 为所有白名单股票补充 industry（从 SK）
for s in wl:
    code = s['code']
    if code in sk:
        s['industry'] = sk[code].get('industry', '其他')
    else:
        s['industry'] = '其他'

# 5. 构建候选集合
target_codes = set()

# 5a. 市值前500
top500 = wl[:500]
for s in top500:
    target_codes.add(s['code'])
print(f"市值前500: {len(top500)}只 -> 候选 {len(target_codes)}只")

# 5b. 各行业龙头
for ind, count in sorted(HOT_INDUSTRIES.items(), key=lambda x: -x[1]):
    ind_stocks = [s for s in wl if s['industry'] == ind]
    if not ind_stocks:
        continue
    ind_stocks.sort(key=lambda x: x.get('mcap_yi', 0), reverse=True)
    leaders = ind_stocks[:count]
    added = sum(1 for s in leaders if s['code'] not in target_codes)
    before = len(target_codes)
    for s in leaders:
        target_codes.add(s['code'])
    after = len(target_codes)
    print(f"  {ind}: 取前{count}只, 新增{after-before}只")

print(f"\n合并去重后: {len(target_codes)}只")

# 6. 过滤已有
need = [c for c in target_codes if c not in existing]
print(f"已生成: {len(target_codes) - len(need)}只")
print(f"待生成: {len(need)}只")

# 7. 输出待生成列表（按市值排序）
code_to_stock = {s['code']: s for s in wl}
need_sorted = sorted(need, key=lambda c: code_to_stock.get(c, {}).get('mcap_yi', 0), reverse=True)

print("\n=== 待生成列表 (前50只) ===")
for c in need_sorted[:50]:
    s = code_to_stock[c]
    ind = s.get('industry', '?')
    mcap = s.get('mcap_yi', 0)
    print(f"  {c} {s['name']:10s} {ind:8s} {mcap:>8.0f}亿")
print(f"  ... 共 {len(need)} 只")

# 按行业分布统计待生成
need_ind = {}
for c in need_sorted:
    ind = code_to_stock[c].get('industry', '?')
    need_ind.setdefault(ind, []).append(c)

print("\n=== 待生成行业分布 ===")
for ind in sorted(need_ind, key=lambda x: -len(need_ind[x])):
    print(f"  {ind}: {len(need_ind[ind])}只")

# 保存待生成列表为文件
with open('.need_generate.json', 'w') as f:
    json.dump(need_sorted, f, ensure_ascii=False)
print(f"\n待生成列表已保存到 .need_generate.json")
