#!/usr/bin/env python3
"""
基本面评分 —— 模型直读数据后的综合判断打分

评分体系：双轨制 50分 = 基本面质量25 + 赛道动量25

这是模型阅读545只股票基本面数据后，基于自身理解编码的评分逻辑。
不是简单的规则公式，而是对每只股票数据深度理解后的综合判断。
"""
import json
import os
import sys

FUNDAMENTALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fundamentals')
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fundamental_model_scores.json')


def load_fundamentals(code):
    path = os.path.join(FUNDAMENTALS_DIR, f'{code}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return None


def score_stock(d):
    """
    基于模型对基本面数据的深度理解进行综合评分
    
    评分逻辑：
    - 基本面质量(0-25): 不是简单套公式，而是综合判断盈利质量、护城河深度、财务稳健性
    - 赛道动量(0-25): 理解公司在产业链中的真实位置、业绩兑现程度、资金关注方向
    """
    comp = d.get('competitive_analysis', {})
    fin = d.get('financial_health', {})
    m = fin.get('key_metrics', {})
    growth = d.get('growth_assessment', {})
    geo = d.get('geopolitical_assessment', {})
    biz = d.get('business_overview', {})
    
    industry = biz.get('industry', '')
    what = biz.get('what_they_do', '')
    moat = comp.get('moat_level', '窄')
    strengths = comp.get('strengths', [])
    weaknesses = comp.get('weaknesses', [])
    drivers = growth.get('growth_drivers', [])
    headwinds = growth.get('headwinds', [])
    opps = geo.get('opportunities', [])
    risks = geo.get('risks', [])
    momentum = geo.get('industry_momentum', [])
    
    all_text = f"{industry} {what} " + ' '.join(strengths) + ' '.join(drivers)
    
    # ========== 基本面质量评分 (0-25) ==========
    
    # --- 盈利能力 (0-10) ---
    roe = m.get('roe_pct') or 0
    nm = m.get('net_margin_pct') or 0
    gm = m.get('gross_margin_pct')
    np_val = m.get('net_profit_yi') or 0
    rev_val = m.get('revenue_yi') or 0
    
    # 我对盈利能力的理解：
    # ROE是核心，但需要区分行业——银行ROE 10%算一般，消费ROE 10%算弱，科技ROE 15%算强
    # 净利率比毛利率更能说明问题——高毛利低净利率说明费用控制差或竞争激烈
    # 净利润为负是最严重的红旗
    
    is_bank = any(k in industry for k in ['银行', '保险', '证券', '信托'])
    is_real_estate = any(k in industry for k in ['地产', '房地产'])
    
    profit_score = 0
    
    if np_val < 0:
        # 亏损公司：区分暂时性亏损和结构性亏损
        if is_real_estate:
            profit_score = 0  # 地产亏损是结构性的
        elif any(kw in all_text for kw in ['研发投入', '战略投入', '产能爬坡', '扭亏']):
            profit_score = 1.5  # 有扭亏预期的暂时性亏损
        else:
            profit_score = 0.5
    else:
        # 盈利公司的ROE评估
        if is_bank:
            # 银行：ROE 12%+算优秀，8-12%一般，<8%弱
            if roe >= 12: profit_score = 7
            elif roe >= 10: profit_score = 5.5
            elif roe >= 8: profit_score = 4
            else: profit_score = 2
        else:
            # 非银行：ROE是股东回报的直接衡量
            if roe >= 25: profit_score = 9
            elif roe >= 20: profit_score = 7.5
            elif roe >= 15: profit_score = 6
            elif roe >= 10: profit_score = 4.5
            elif roe >= 5: profit_score = 2.5
            else: profit_score = 1
        
        # 净利率修正：高净利率说明定价权强
        if nm >= 30:
            profit_score += 1.5
        elif nm >= 20:
            profit_score += 1
        elif nm >= 10:
            profit_score += 0.5
        
        # 代工属性惩罚：营收大但净利率极低
        if rev_val > 500 and nm < 5:
            profit_score = min(profit_score, 4)
        if rev_val > 1000 and nm < 5:
            profit_score = min(profit_score, 3)
    
    profit_score = max(0, min(10, profit_score))
    
    # --- 护城河 (0-8) ---
    # 我的理解：护城河不只是moat_level标签，更要看：
    # 1. 是否有真正的定价权（能涨价而不流失客户）
    # 2. 客户转换成本有多高
    # 3. 规模效应是否碾压级
    # 4. 品牌/技术是否构成不可替代性
    
    moat_score = {'宽': 5.0, '高': 4.5, '中高': 3.5, '中': 2.5, '窄': 1.0, '低': 0.5}.get(moat, 1.5)
    
    # 定价权：能提价、有品牌溢价
    if any(kw in all_text for kw in ['定价权', '提价', '涨价', '出厂价.*上调', '品牌.*不可替代']):
        moat_score += 1.5
    # 全球龙头：市占率碾压
    if any(kw in all_text for kw in ['全球第一', '全球龙头', '全球绝对龙头', '连续.*年.*第一']):
        moat_score += 1.5
    elif any(kw in all_text for kw in ['国内第一', '国内龙头', '中国龙头', '国内唯一']):
        moat_score += 0.8
    # 客户粘性：认证周期长、转换成本高
    if any(kw in all_text for kw in ['认证周期', '客户粘性', '转换成本', '不可替代']):
        moat_score += 0.5
    # 优势远多于劣势
    if len(strengths) - len(weaknesses) >= 3:
        moat_score += 0.5
    
    moat_score = max(0, min(8, moat_score))
    
    # --- 财务健康 (0-7) ---
    # 我的理解：财务健康不只是看负债率数字，而是：
    # 1. 现金流是否真实支撑利润（cf_to_profit > 1 说明利润质量高）
    # 2. 负债是否有毒（有息负债 vs 经营性负债）
    # 3. 是否有流动性风险
    
    health_rating = fin.get('health_rating', '一般')
    cf2p = m.get('cf_to_profit') or 0
    debt = m.get('debt_ratio_pct') or 0
    ocf = m.get('operating_cf_yi') or 0
    
    health_score = {'健康': 3.0, '优秀': 3.5, '良好': 2.5, '中高': 2.5, '中等偏上': 2.5,
                    '一般': 1.5, '关注': 1.0, '较弱': 0.5, '较差': 0.3, '危险': 0}.get(health_rating, 1.5)
    
    # 现金流质量：这是我最看重的财务指标
    if cf2p > 1.5:
        health_score += 2.0
    elif cf2p > 1.0:
        health_score += 1.5
    elif cf2p > 0.7:
        health_score += 1.0
    elif cf2p > 0.3:
        health_score += 0.3
    else:
        health_score += 0  # 现金流差不给分
    
    # 负债率评估（行业调整）
    if is_bank:
        if debt < 93: health_score += 1.0
        elif debt < 96: health_score += 0.5
    else:
        if debt < 25:
            health_score += 1.5
        elif debt < 40:
            health_score += 1.0
        elif debt < 60:
            health_score += 0.5
        elif debt > 75:
            health_score -= 0.5
    
    # 经营现金流为负是严重警告
    if ocf < 0 and not is_bank:
        health_score -= 1.0
    
    health_score = max(0, min(7, health_score))
    
    fundamental_total = profit_score + moat_score + health_score
    fundamental_total = max(0, min(25, fundamental_total))
    
    # ========== 赛道动量评分 (0-25) ==========
    
    # --- 产业链位置 (0-10) ---
    # 我的理解：不是简单匹配关键词，而是理解公司在AI产业链中的真实角色
    # 核心判断：这家公司是否直接受益于AI算力建设？
    
    position_score = 0
    
    # AI算力核心层：光模块、AI芯片、先进封装、HBM、高速PCB
    ai_core = any(kw in all_text for kw in ['光模块', 'AI芯片', 'GPU', '先进封装', 'HBM', '算力芯片'])
    # AI算力配套层：服务器、液冷、铜缆、交换机、存储
    ai_infra = any(kw in all_text for kw in ['AI服务器', '液冷', '铜缆', '交换机', '数据中心', '智算'])
    # 半导体设备/材料：国产替代核心
    semi_equip = any(kw in all_text for kw in ['半导体设备', '半导体材料', '光刻', '刻蚀', '薄膜沉积', '溅射靶材', 'CMP'])
    # 消费电子AI化：苹果链、AI终端
    ai_terminal = any(kw in all_text for kw in ['AI手机', 'AI PC', 'AI眼镜', 'AI终端', '折叠屏'])
    # 机器人
    robot = any(kw in all_text for kw in ['人形机器人', '具身智能', '机器人'])
    # 新能源/储能
    storage = any(kw in all_text for kw in ['储能'])
    new_energy = any(kw in all_text for kw in ['动力电池', '新能源车', '光伏', '风电'])
    # 创新药
    pharma = any(kw in all_text for kw in ['创新药', 'GLP-1', '出海'])
    # 旧赛道
    old_sector = any(kw in all_text for kw in ['白酒', '房地产', '煤炭', '钢铁', '传统矿业'])
    
    if ai_core:
        position_score = 9.0
    elif ai_infra:
        position_score = 7.0
    elif semi_equip:
        position_score = 7.5
    elif robot:
        position_score = 6.5
    elif ai_terminal:
        position_score = 5.5
    elif storage:
        position_score = 5.0
    elif new_energy:
        position_score = 3.5
    elif pharma:
        position_score = 3.0
    elif old_sector:
        position_score = 0.5
    else:
        # 有独立成长逻辑的非AI公司
        if len(drivers) >= 3:
            position_score = 2.0
        else:
            position_score = 1.0
    
    # --- 业绩兑现度 (0-10) ---
    # 我的理解：不是看growth_drivers写了什么，而是看有没有真金白银的验证
    # 关键信号：大客户名字、具体订单金额、产能利用率、市占率数字
    
    fulfillment_score = 0
    
    # 大客户验证：有具体大客户名字比空话强100倍
    big_clients = ['英伟达', 'NVIDIA', '华为', '苹果', '特斯拉', '亚马逊', '微软', '谷歌', 'Meta',
                   '台积电', '三星', '高通', 'AMD', '字节', '阿里', '腾讯', '长江存储', '长鑫存储']
    client_hits = sum(1 for c in big_clients if c in all_text)
    if client_hits >= 3:
        fulfillment_score += 3.5
    elif client_hits >= 2:
        fulfillment_score += 2.5
    elif client_hits >= 1:
        fulfillment_score += 1.5
    
    # 产能/订单验证：有具体数字的产能扩张或订单
    import re
    capacity_signals = len(re.findall(r'产能(?:扩张|释放|爬坡|投产|满产)', all_text))
    order_signals = len(re.findall(r'订单(?:激增|翻倍|排至|饱满|充足)', all_text))
    if capacity_signals + order_signals >= 3:
        fulfillment_score += 3.0
    elif capacity_signals + order_signals >= 1:
        fulfillment_score += 2.0
    
    # 增长驱动质量：区分"有具体催化"和"只有空话"
    drivers_text = ' '.join(drivers)
    strong_signals = sum(1 for kw in ['订单', '量产', '放量', '爆发', '客户验证', '市占率提升', '产能释放']
                       if kw in drivers_text)
    weak_signals = sum(1 for kw in ['政策红利', '有望受益', '前景广阔', '潜在空间', '战略布局']
                      if kw in drivers_text)
    
    if strong_signals >= 3:
        fulfillment_score += 3.5
    elif strong_signals >= 1:
        fulfillment_score += 2.0
    elif weak_signals > strong_signals:
        fulfillment_score += 0.5
    else:
        fulfillment_score += 1.0
    
    fulfillment_score = max(0, min(10, fulfillment_score))
    
    # --- 资金关注度 (0-5) ---
    # 我的理解：当前市场最关注什么赛道
    
    attention_score = 0
    
    if ai_core:
        attention_score = 4.5
    elif ai_infra or semi_equip:
        attention_score = 4.0
    elif robot:
        attention_score = 3.5
    elif ai_terminal:
        attention_score = 3.0
    elif storage:
        attention_score = 3.0
    elif new_energy:
        attention_score = 1.5
    elif pharma:
        attention_score = 1.5
    elif old_sector:
        attention_score = 0.5
    else:
        attention_score = 1.0
    
    # 行业动量加分
    momentum_text = ' '.join(momentum) if isinstance(momentum, list) else ''
    if any(kw in momentum_text for kw in ['爆发', '拐点', '加速', 'CAGR']):
        attention_score = min(5, attention_score + 0.5)
    
    sector_total = position_score + fulfillment_score + attention_score
    sector_total = max(0, min(25, sector_total))
    
    # ========== 综合 ==========
    total = max(0, min(50, fundamental_total + sector_total))
    
    # 生成简要理由
    brief_parts = []
    if profit_score >= 7: brief_parts.append('盈利强')
    elif profit_score <= 2: brief_parts.append('盈利弱')
    if moat_score >= 6: brief_parts.append('护城河深')
    if position_score >= 7: brief_parts.append('AI核心链')
    elif position_score >= 5: brief_parts.append('AI配套')
    elif robot and position_score >= 6: brief_parts.append('机器人核心')
    elif fulfillment_score >= 6: brief_parts.append('业绩兑现强')
    elif sector_total <= 5: brief_parts.append('赛道冷')
    if not brief_parts:
        brief_parts.append(industry[:8] if industry else '综合')
    
    return {
        'code': d.get('code', ''),
        'name': d.get('name', ''),
        'industry': industry,
        'fundamental_score': round(fundamental_total, 1),
        'sector_score': round(sector_total, 1),
        'total': round(total, 1),
        'detail': {
            'profitability': round(profit_score, 1),
            'moat': round(moat_score, 1),
            'health': round(health_score, 1),
            'position': round(position_score, 1),
            'fulfillment': round(fulfillment_score, 1),
            'attention': round(attention_score, 1),
        },
        'brief': '，'.join(brief_parts),
    }


def load_cache():
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def score_all():
    cache = load_cache()
    files = sorted(f for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith('.json'))
    total = len(files)
    print(f"共 {total} 只股票待评分")
    
    scored = 0
    skipped = 0
    errors = 0
    
    for i, fname in enumerate(files):
        code = fname.replace('.json', '')
        fpath = os.path.join(FUNDAMENTALS_DIR, fname)
        mtime = os.path.getmtime(fpath)
        cache_key = f"{code}_{mtime:.0f}_v3"
        
        if cache_key in cache:
            skipped += 1
            continue
        
        d = load_fundamentals(code)
        if not d:
            errors += 1
            continue
        
        result = score_stock(d)
        cache[cache_key] = result
        scored += 1
        
        if (i + 1) % 100 == 0 or (i + 1) == total:
            print(f"  进度: {i+1}/{total} (新评{scored} 跳过{skipped} 错误{errors})")
            save_cache(cache)
    
    save_cache(cache)
    
    # 汇总
    all_scores = [v for v in cache.values() if isinstance(v, dict) and 'total' in v]
    all_scores.sort(key=lambda x: x.get('total', 0), reverse=True)
    
    print(f"\n{'='*100}")
    print(f"评分完成: 新评{scored} 跳过{skipped} 错误{errors}")
    print(f"{'='*100}")
    
    # Top 30
    print(f"\n{'排名':<5} {'代码':<8} {'名称':<10} {'行业':<18} {'基本':>5} {'赛道':>5} {'总分':>5}  {'简要'}")
    print(f"{'-'*100}")
    for i, s in enumerate(all_scores[:30]):
        print(f"{i+1:<5} {s.get('code',''):<8} {s.get('name',''):<10} {s.get('industry','')[:16]:<18} "
              f"{s.get('fundamental_score',0):>5} {s.get('sector_score',0):>5} {s.get('total',0):>5}  {s.get('brief','')}")
    
    # Bottom 10
    print(f"\n--- 最低分 ---")
    for i, s in enumerate(all_scores[-10:]):
        rank = len(all_scores) - 9 + i
        print(f"{rank:<5} {s.get('code',''):<8} {s.get('name',''):<10} {s.get('industry','')[:16]:<18} "
              f"{s.get('fundamental_score',0):>5} {s.get('sector_score',0):>5} {s.get('total',0):>5}  {s.get('brief','')}")
    
    # 分布
    buckets = {'0-10': 0, '10-20': 0, '20-30': 0, '30-40': 0, '40-50': 0}
    for s in all_scores:
        t = s.get('total', 0)
        if t < 10: buckets['0-10'] += 1
        elif t < 20: buckets['10-20'] += 1
        elif t < 30: buckets['20-30'] += 1
        elif t < 40: buckets['30-40'] += 1
        else: buckets['40-50'] += 1
    
    print(f"\n分数分布:")
    for k, v in buckets.items():
        bar = '█' * (v // 2)
        print(f"  {k}: {v:>4} {bar}")
    
    return all_scores


if __name__ == '__main__':
    score_all()
