"""分析知识库覆盖缺口，并批量扩编"""
import json
import os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from picker import paths
from picker.knowledge.ai_knowledge_base import CODE_TO_INDUSTRY

with open(paths.STOCK_WHITELIST) as f:
    whitelist = json.load(f)

# 分析未覆盖的股票
uncovered = []
for s in whitelist:
    code = s['code']
    if code not in CODE_TO_INDUSTRY:
        uncovered.append(s)

print(f"总股票: {len(whitelist)}, 已覆盖: {len(whitelist)-len(uncovered)}, 未覆盖: {len(uncovered)}")

# 按名称中的关键词聚类未覆盖股票
name_words = Counter()
for s in uncovered:
    name = s['name']
    # 找名称中的行业关键词
    for kw in ['银行', '证券', '保险', '信托', '地产', '房产', '钢铁', '煤炭',
               '化工', '医药', '制药', '医疗', '生物', '基因',
               '航空', '飞机', '航发', '航天', '船舶', '重工',
               '电力', '发电', '电网', '水电', '核电',
               '食品', '饮料', '白酒', '啤酒', '乳业', '农业', '种业',
               '建筑', '建材', '水泥', '玻璃', '工程', '路桥', '港', '高速',
               '汽车', '新能源', '充电', '电池', '锂', '钴', '镍',
               '通信', '运营商', '5G', '软件', '信息', '科技', '电子',
               '传媒', '影视', '出版', '教育', '旅游', '物流',
               '石油', '石化', '天然气', '燃气',
               '纺织', '服装', '造纸', '包装', '环保',
               '商贸', '百货', '零售',
               '黄金', '有色', '铜', '铝', '稀土', '矿产',
               '铁路', '交通', '运输',
               '检测', '仪器', '仪表', '机械', '制造', '精密',
               '材料', '新材',
               '物业', '园区',
               '贸易', '投资',
               '光', '电', '通信', '数据', '智能', '自动化',
               '服饰', '珠宝', '家具', '照明',
               ]:
        if kw in name:
            name_words[kw] += 1

# 按市场分类
market_dist = Counter(s['market'] for s in uncovered)
print(f"\n未覆盖股票市场分布: {dict(market_dist)}")

# 名称关键词统计
print(f"\n未覆盖股票名称关键词 TOP30:")
for kw, cnt in name_words.most_common(30):
    print(f"  {kw}: {cnt}只")

# 列出未覆盖股票的前50名（按市值）
uncovered_sorted = sorted(uncovered, key=lambda x: x.get('mcap_yi', 0) or 0, reverse=True)
print(f"\n未覆盖股票 TOP50（按市值）:")
for i, s in enumerate(uncovered_sorted[:50]):
    print(f"  {i+1}. {s['code']} {s['name']} 市值{s.get('mcap_yi','?')}亿 市场{s.get('market','?')}")