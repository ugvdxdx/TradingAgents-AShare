import json, re
from collections import defaultdict

with open('world_knowledge.py') as f:
    covered = set(re.findall(r'    "(\d{6})":', f.read()))
with open('uncovered_stocks.json') as f:
    uncovered = json.load(f)
remaining = [(c,n,m) for c,n,m in uncovered if c not in covered]
print(f"Remaining: {len(remaining)}")

groups = defaultdict(list)
for code, name, mcap in remaining:
    if code.startswith('688'): groups['AI芯片/科创板'].append((code,name,mcap))
    elif code.startswith('30'): groups['创业板'].append((code,name,mcap))
    elif '银行' in name: groups['银行'].append((code,name,mcap))
    elif '证券' in name: groups['券商'].append((code,name,mcap))
    elif any(k in name for k in ['药','医','生物']): groups['医药'].append((code,name,mcap))
    elif any(k in name for k in ['芯','微','半导','存储','集成']): groups['半导体'].append((code,name,mcap))
    elif any(k in name for k in ['光电','电子','电路','通信','光纤']): groups['电子通信'].append((code,name,mcap))
    elif any(k in name for k in ['电力','能源','电','风电','光伏','发电','水电','核电']): groups['电力能源'].append((code,name,mcap))
    elif any(k in name for k in ['汽车','车','客车']): groups['汽车'].append((code,name,mcap))
    elif any(k in name for k in ['钢','铁']): groups['钢铁'].append((code,name,mcap))
    elif any(k in name for k in ['有色','黄金','铜','铝','稀土','矿','钴','镍']): groups['矿业'].append((code,name,mcap))
    elif any(k in name for k in ['化工','化学','化纤','材料','玻纤','石化','石油']): groups['化工材料'].append((code,name,mcap))
    elif any(k in name for k in ['建筑','建材','水泥','工程','铁建','路桥']): groups['建筑'].append((code,name,mcap))
    elif any(k in name for k in ['食品','酒','饮料','乳','榨菜','醋','调味']): groups['食品饮料'].append((code,name,mcap))
    elif any(k in name for k in ['航空','机场']): groups['航空'].append((code,name,mcap))
    elif any(k in name for k in ['高速','港口','铁路','物流','运输','快递']): groups['交通运输'].append((code,name,mcap))
    elif any(k in name for k in ['软件','信息','数据','网络']): groups['软件IT'].append((code,name,mcap))
    elif any(k in name for k in ['地产','房产','物业','园区']): groups['房地产'].append((code,name,mcap))
    elif any(k in name for k in ['环保','水务']): groups['环保'].append((code,name,mcap))
    elif any(k in name for k in ['家电','电器']): groups['家电'].append((code,name,mcap))
    elif any(k in name for k in ['传媒','出版','影视','广告']): groups['传媒'].append((code,name,mcap))
    elif any(k in name for k in ['机械','制造','装备','重工','数控','机床']): groups['机械装备'].append((code,name,mcap))
    elif any(k in name for k in ['农业','种业','牧','猪','鸡','渔']): groups['农业'].append((code,name,mcap))
    elif any(k in name for k in ['纸','包装','家具','纺织','服装']): groups['轻工'].append((code,name,mcap))
    elif any(k in name for k in ['商贸','百货','零售','超市','贸易']): groups['商贸'].append((code,name,mcap))
    else: groups['其他'].append((code,name,mcap))

for g, stocks in sorted(groups.items(), key=lambda x: -len(x[1])):
    print(f"\n[{g}] {len(stocks)}只: {', '.join(f'{c} {n}' for c,n,_ in stocks[:5])}")
    if len(stocks) > 3:
        # also show all for large groups
        for code, name, mcap in stocks[5:]:
            print(f"  {code} {name} {mcap}亿")