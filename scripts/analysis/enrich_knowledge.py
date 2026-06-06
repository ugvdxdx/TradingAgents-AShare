#!/usr/bin/env python3
"""
知识库自动扩充 —— 批量搜索未覆盖股票的公司背景
结果持久化到 JSON，后续可增量更新
"""
import json, os, time, re
import requests

KNOWLEDGE_STORE = "stock_knowledge.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def load_knowledge_store():
    if os.path.exists(KNOWLEDGE_STORE):
        with open(KNOWLEDGE_STORE, 'r') as f:
            return json.load(f)
    return {}

def save_knowledge_store(data):
    with open(KNOWLEDGE_STORE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def search_stock_business(code, name):
    """用同花顺 basic 页面获取主营业务"""
    try:
        url = f"https://basic.10jqka.com.cn/{code}/operate.html"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=8)
        if r.status_code != 200:
            return None
        # 提取主营业务
        m = re.search(r'主营业务[：:]\s*<[^>]*>([^<]+)', r.text)
        if not m:
            m = re.search(r'主营业务[：:]\s*([^<]+)', r.text)
        if m:
            biz = m.group(1).strip()
            if len(biz) < 3:
                return None
            return biz
        return None
    except:
        return None

def classify_business(biz_text, name):
    """根据业务描述分类到知识库赛道"""
    biz = biz_text.lower()
    
    rules = [
        (['半导体', '芯片', '集成电路', 'ic', '晶圆', '光刻', '封测', '微电子'],
         'AI芯片', 9.5),
        (['光模块', '光纤', '光器件', '光电子', 'cpo'],
         '光通信', 9.0),
        (['光伏', '太阳能电池', '逆变器', '多晶硅', '单晶硅', 'hjt', 'topcon', '薄膜太阳能'],
         '光伏储能', 6.5),
        (['锂电池', '锂离子', '电解液', '正极材料', '负极材料', '隔膜', '固态电池', '锂矿',
          '动力电池', '储能电池', '六氟磷酸锂'],
         '锂电池', 7.0),
        (['机器人', '数控机床', 'cnc', '自动化', 'plc', '伺服', '减速器', '步进电机',
          '工业机器人', '机器视觉'],
         '机器人', 7.5),
        (['服务器', '云计算', 'idc', '数据中心', '液冷', '算力', 'gpu', '人工智能',
          '大数据', '数据湖'],
         '算力', 8.5),
        (['医药', '制药', '原料药', '制剂', '生物药', '疫苗', '创新药', 'cro', 'cmo',
          '医疗器械', '诊断试剂', '中药', '化药', '抗生素'],
         '生物医药', 5.5),
        (['军工', '导弹', '弹药', '雷达', '航电', '坦克', '舰艇', '鱼雷', '战斗机',
          '隐形', '红外', '北斗', '卫星导航', '航天器'],
         '军工', 6.0),
        (['风电', '发电机', '火电', '热电', '核电', '水力发电', '电力', '供电',
          '输配电', '电网', '充电桩', '电改', '售电'],
         '能源电力', 4.5),
        (['银行', '存贷款', '商业银行', '政策性银行', '城商行'],
         '金融银行', 4.5),
        (['证券', '保险', '期货', '信托', '基金', '券商', '投行', '资管', '资管'],
         '券商保险', 5.0),
        (['汽车', '整车', '新能源车', '电动汽车', '客车', '商用车', '乘用车', '轿车'],
         '汽车整车', 5.0),
        (['化工', '化学', '石化', '化肥', '农药', '涂料', '颜料', '聚氨酯',
          '钛白粉', '染料', '纯碱', '烧碱', 'pvc', '聚乙烯', '可降解'],
         '化工材料', 4.0),
        # 通用电子/通信
        (['pcb', '印制电路', '电路板', '连接器', '线缆', '光纤'],
         '消费电子', 5.5),
        (['软件', '互联网', '计算机', '信息', '人工智能', '数字孪生', '工业软件',
          '操作系统', '数据库', '中间件', 'oa', 'erp', 'crm', '金融科技'],
         'AI应用', 6.5),
        (['通信', '5g', '基站', '网络', '交换机', '路由器', '宽带', '移动互联网',
          '物联网', '卫星通信'],
         '光通信', 5.5),  # 光通信分低
    ]
    
    for keywords, industry, score in rules:
        for kw in keywords:
            if kw in biz:
                return industry, score
    return None, 0

def batch_enrich(limit=50):
    """批量扩充知识库"""
    from ai_knowledge_base import CODE_TO_INDUSTRY, match_by_name_traditional
    
    with open("stock_whitelist.json") as f:
        whitelist = json.load(f)
    
    store = load_knowledge_store()
    
    # 找未覆盖的股票（按市值排序，先搜大市值的）
    uncovered = []
    for s in whitelist:
        code = s['code']
        if code not in CODE_TO_INDUSTRY:
            inds, sc = match_by_name_traditional(s['name'])
            if not inds and code not in store:
                uncovered.append(s)
    
    uncovered.sort(key=lambda x: x.get('mcap_yi', 0) or 0, reverse=True)
    
    print(f"待搜索: {len(uncovered)} 只 (限制 {limit} 只)")
    
    enriched = 0
    for i, stock in enumerate(uncovered[:limit]):
        code = stock['code']
        name = stock['name']
        
        biz = search_stock_business(code, name)
        if biz:
            industry, score = classify_business(biz, name)
            if industry:
                store[code] = {
                    'name': name,
                    'business': biz[:200],
                    'industry': industry,
                    'score': score,
                }
                enriched += 1
                print(f"  [{i+1}/{limit}] ✅ {code} {name}: {industry}({score}) - {biz[:60]}")
            else:
                store[code] = {
                    'name': name,
                    'business': biz[:200],
                    'industry': '未分类',
                    'score': 0,
                }
        else:
            store[code] = {
                'name': name,
                'business': '',
                'industry': '获取失败',
                'score': 0,
            }
        
        if (i + 1) % 10 == 0:
            save_knowledge_store(store)
            print(f"    已保存 ({i+1}/{limit})")
        
        time.sleep(0.3)  # 控制频率
    
    save_knowledge_store(store)
    
    # 统计
    matched = sum(1 for v in store.values() if v['score'] > 0)
    print(f"\n扩充完成: 总计 {len(store)} 只, 成功分类 {matched} 只")
    return store

if __name__ == "__main__":
    batch_enrich(limit=50)