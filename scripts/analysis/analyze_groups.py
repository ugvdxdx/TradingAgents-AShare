import json
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from picker import paths
from picker.knowledge.world_knowledge import BUSINESS_WORLD_KNOWLEDGE

with open(paths.STOCK_WHITELIST) as f:
    wl = json.load(f)

sorted_wl = sorted(wl, key=lambda x: x.get('mcap_yi', 0) or 0, reverse=True)
top500 = sorted_wl[:500]

name_industry = {
    '银行': '金融银行', '证券': '金融券商', '保险': '金融保险',
    '石油': '石油石化', '石化': '石油石化', '燃气': '燃气',
    '白酒': '食品饮料', '啤酒': '食品饮料', '食品': '食品饮料', '饮料': '食品饮料',
    '乳业': '食品饮料', '种业': '食品饮料', '农业': '食品饮料',
    '芯片': 'AI芯片', '半导体': 'AI芯片', '集成电路': 'AI芯片', '微电子': 'AI芯片', '晶圆': 'AI芯片',
    '通信': 'AI芯片', '光电': 'AI芯片', '光电子': 'AI芯片',
    '医药': '医药生物', '制药': '医药生物', '医疗': '医药生物', '生物': '医药生物', '药': '医药生物',
    '煤炭': '煤炭',
    '电力': '公用事业', '发电': '公用事业', '水电': '公用事业', '核电': '公用事业', '电网': '公用事业',
    '汽车': '汽车整车', '新能源': '新能源', '锂电': '锂电池', '电池': '锂电池',
    '钢铁': '钢铁', '有色': '矿业', '黄金': '矿业', '铜业': '矿业', '铝业': '矿业',
    '航空': '航空', '机场': '航空',
    '建筑': '建筑', '建材': '建筑', '水泥': '建筑', '工程': '建筑', '铁建': '建筑',
    '化工': '化工', '化学': '化工', '材料': '材料',
    '地产': '房地产', '房产': '房地产',
    '高速': '交通运输', '铁路': '铁路运输', '港口': '交通运输', '运输': '交通运输',
    '电子': '消费电子', '精密': '消费电子', '科技': '消费电子',
    '家电': '家电', '电器': '家电',
    '软件': '软件', '信息': '软件', '数据': '软件', '智能': 'AI芯片',
    '传媒': '传媒', '出版': '传媒', '影视': '传媒',
    '环保': '环保',
    '造纸': '轻工', '包装': '轻工', '家具': '轻工', '纺织': '轻工', '服装': '轻工',
    '船舶': '船舶制造', '重工': '船舶制造', '制造': '机械制造', '机械': '机械制造',
    '商贸': '商贸', '百货': '商贸', '零售': '商贸',
    '稀土': '矿业', '矿产': '矿业', '矿业': '矿业',
}

def classify(name, code):
    for kw, industry in name_industry.items():
        if kw in name:
            return industry
    if code.startswith('688'):
        return 'AI芯片'
    if code.startswith('30'):
        return '创业板'
    return '其他'

groups = defaultdict(list)
for s in top500:
    if s['code'] not in BUSINESS_WORLD_KNOWLEDGE:
        ind = classify(s['name'], s['code'])
        groups[ind].append(s)

print("待补充446只按行业分组：")
for ind, stocks in sorted(groups.items(), key=lambda x: -len(x[1])):
    print(f"\n  [{ind}] {len(stocks)}只")
    for s in stocks[:5]:
        print(f"    {s['code']} {s['name']} mcap={s.get('mcap_yi')}亿")
    if len(stocks) > 5:
        print(f"    ... 共{len(stocks)}只")