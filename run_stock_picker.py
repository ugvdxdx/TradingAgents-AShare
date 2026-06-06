#!/usr/bin/env python3
"""
J-TradingAgents 选股系统 v4.2

数据源策略（混合）：
  1. 知识库优先 —— ai_knowledge_base（世界知识识别行业归属）
  2. Eastmoney push2 API（行业板块成分股精确匹配）
  3. 腾讯行情：PE/PB/市值（稳定）
  4. 技术分析：TickFlow K线缓存 + tech_analysis

评分体系 v7：世界知识40% + 技术分析30% + PE估值20% + 市场溢价10%

白名单管理：第一次全量筛选后只操作白名单
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
from tradingagents.dataflows.providers.astock_provider import AstockProvider

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_knowledge_base import lookup_knowledge
from fundamental_scorer import compute_fundamental_knowledge
from data_cache import KlineCache
from tech_analysis import compute_tech_score, TechScore

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

DEFAULT_WHITELIST_FILE = "stock_whitelist.json"
CACHE_DIR = "kline_cache"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PUSH2_URL = "https://push2.eastmoney.com/api/qt/clist/get"


def load_whitelist(filepath: str) -> Optional[List[Dict]]:
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.info(f"白名单加载成功：{len(data)} 只股票")
            return data
        except Exception as e:
            logger.warning(f"白名单加载失败：{e}")
    return None


def save_whitelist(filepath: str, stocks: List[Dict]) -> None:
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)
    logger.info(f"白名单已保存：{len(stocks)} 只股票")


def get_market_type(code: str) -> str:
    code = code.strip().upper()
    if code.startswith('688'):
        return 'star'
    elif code.startswith('30'):
        return 'gem'
    else:
        return 'mainboard'


# ──────────────────────────────────────────────
# 名称关键词行业匹配（降级方案，始终可用）
# ──────────────────────────────────────────────

HOT_KEYWORDS = [
    ('半导体', 9.0, ['半导体', '芯片', '集成电路', '光刻', '封测', '晶圆', 'IC', '硅片', '微电子']),
    ('光通信', 8.5, ['光通信', '光模块', 'CPO', '光纤', '光器件', '光电子']),
    ('AI', 8.0, ['AI', '人工智能', '算力', 'GPU', '多模态', '大模型', '深度学习']),
    ('机器人', 7.5, ['机器人', '数控', '自动化设备', '智能机器', '工业自动化']),
    ('低空经济', 7.0, ['低空经济', '飞行汽车', '无人机', 'eVTOL']),
    ('新能源车', 7.0, ['新能源车', '比亚迪', '特斯拉', '充电桩', '整车', '电动']),
    ('锂电池', 6.5, ['锂电池', '锂电', '固态电池', '正极', '负极', '电解液', '隔膜']),
    ('光伏', 6.5, ['光伏', 'TOPCon', 'HJT', '逆变器', '太阳能', '硅料', '硅片']),
    ('储能', 6.0, ['储能', '钠电池', '蓄能', '虚拟电厂']),
    ('军工', 6.0, ['军工', '航天装备', '雷达', '北斗', '军品', '兵器', '航发']),
    ('信创', 5.5, ['信创', '国产替代', '操作系统', '数据库', 'CPU', '国产软件']),
    ('生物医药', 5.5, ['生物医药', '创新药', 'CXO', '疫苗', '生物科技']),
    ('数据要素', 5.0, ['数据', '数字', '云计算', '大数据', '数据中心', 'IDC']),
    ('消费电子', 5.0, ['消费电子', 'MR', 'VR', 'AR', '可穿戴', '传感器']),
    ('氢能源', 5.0, ['氢能', '氢能源', '燃料电池', '电解槽']),
    ('新材料', 4.5, ['新材料', '稀土', '永磁', '碳纤维', '高温合金']),
    ('金融科技', 4.5, ['金融科技', 'FinTech', '数字货币', '区块链']),
    ('环保', 4.0, ['环保', '水务', '污水处理', '固废', '环卫']),
    ('电力', 4.0, ['电力', '发电', '电网', '特高压', '核电']),
    ('消费', 3.5, ['消费', '食品', '饮料', '白酒', '乳业', '家电']),
]


def match_by_name(name: str, code: str) -> tuple:
    """基于名称的关键词匹配，返回 (行业列表, 行业热度分数)"""
    name_upper = name.upper()
    matched = []
    max_score = 0
    for category, score, keywords in HOT_KEYWORDS:
        for kw in keywords:
            if kw in name_upper:
                if category not in matched:
                    matched.append(category)
                    max_score = max(max_score, score)
                break
    
    if code.startswith('688'):
        max_score = max(max_score, 3.0)
    elif code.startswith('30'):
        max_score = max(max_score, 2.0)
    
    return matched[:3], max_score


# ──────────────────────────────────────────────
# Eastmoney API 行业匹配（精确方案，可能不可用）
# ──────────────────────────────────────────────

def api_fetch_board_list(board_type: int = 2) -> Optional[Dict[str, str]]:
    """尝试获取板块列表，失败返回 None"""
    result = {}
    for page in range(1, 5):
        params = {
            "pn": str(page), "pz": "200", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"m:90+t:{board_type}",
            "fields": "f12,f14",
        }
        try:
            r = requests.get(PUSH2_URL, params=params,
                           headers={"User-Agent": UA}, timeout=8)
            items = r.json().get("data", {}).get("diff", [])
            if not items:
                break
            for item in items:
                name, code = item.get('f14', ''), item.get('f12', '')
                if name and code:
                    result[name] = code
            if len(items) < 200:
                break
        except:
            return None
    return result


def api_fetch_board_stocks(board_code: str) -> Optional[set]:
    """尝试获取板块成分股，失败返回 None"""
    stocks = set()
    for page in range(1, 5):
        params = {
            "pn": str(page), "pz": "200", "po": "0", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"b:{board_code}+f:!50",
            "fields": "f12",
        }
        try:
            r = requests.get(PUSH2_URL, params=params,
                           headers={"User-Agent": UA}, timeout=8)
            items = r.json().get("data", {}).get("diff", [])
            if not items:
                break
            for item in items:
                code = item.get('f12', '')
                if code:
                    stocks.add(code)
            if len(items) < 200:
                break
        except:
            return None
    return stocks


def api_fetch_hot_boards() -> Optional[Dict[str, float]]:
    """尝试获取板块涨跌幅，失败返回 None"""
    hot = {}
    for bt in [2, 3]:
        params = {
            "pn": "1", "pz": "500", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"m:90+t:{bt}",
            "fields": "f12,f14,f3",
        }
        try:
            r = requests.get(PUSH2_URL, params=params,
                           headers={"User-Agent": UA}, timeout=8)
            items = r.json().get("data", {}).get("diff", [])
            for item in items:
                name, change = item.get('f14', ''), item.get('f3', 0)
                if name and change is not None:
                    hot[name] = round(change, 2)
        except:
            return None
    return hot


def build_industry_index() -> Dict:
    """
    建立行业索引（混合策略）
    返回: {stock_industries: {code: {industries, max_industry_score}}, hot_boards: {}}
    """
    stock_industries = {}
    hot_boards = {}
    api_success = False
    
    # 尝试 API 精确匹配
    logger.info("  尝试 Eastmoney API 行业匹配...")
    all_boards = {}
    for bt in [2, 3]:
        boards = api_fetch_board_list(bt)
        if boards is not None:
            all_boards.update(boards)
    
    if all_boards:
        api_success = True
        logger.info(f"  API成功：获取到 {len(all_boards)} 个板块")
        
        hot_boards = api_fetch_hot_boards() or {}
        
        target_keywords = {
            '半导体': ['半导体', '芯片', '集成电路', '光刻', '封测'],
            '光通信': ['光通信', '光模块', 'CPO', '光纤'],
            'AI': ['AI', '人工智能', '算力', '多模态'],
            '机器人': ['机器人', '人形机器人'],
            '低空经济': ['低空经济', '飞行汽车'],
            '新能源车': ['新能源汽车', '新能源车'],
            '锂电池': ['锂电池', '锂电', '固态电池'],
            '光伏': ['光伏', '光伏设备', 'TOPCon', 'HJT'],
            '储能': ['储能', '钠电池'],
            '军工': ['军工', '航天航空', '北斗导航'],
            '信创': ['信创', '国产软件'],
        }
        
        target_boards = {}
        for board_name, board_code in all_boards.items():
            for category, keywords in target_keywords.items():
                if any(kw in board_name for kw in keywords):
                    target_boards.setdefault(category, []).append((board_name, board_code))
                    break
        
        for category, boards in target_boards.items():
            for board_name, board_code in boards:
                stocks = api_fetch_board_stocks(board_code)
                if stocks is None:
                    continue
                board_change = hot_boards.get(board_name, 0)
                for code in stocks:
                    stock_industries.setdefault(code, {'industries': [], 'max_industry_score': 0})
                    if category not in stock_industries[code]['industries']:
                        stock_industries[code]['industries'].append(category)
                    stock_industries[code]['max_industry_score'] = max(
                        stock_industries[code]['max_industry_score'], abs(board_change))
        
        matched = len(stock_industries)
        logger.info(f"  API匹配完成：{matched} 只股票获得行业分类")
    
    return {
        'stock_industries': stock_industries,
        'hot_boards': hot_boards,
        'api_success': api_success,
    }


# ──────────────────────────────────────────────
# 选股流程
# ──────────────────────────────────────────────

def build_stock_pool(provider: AstockProvider) -> List[Dict]:
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        stock_list = []
        for _, row in df.iterrows():
            code = str(row.get('code', '')).strip()
            name = str(row.get('name', '')).strip()
            if not code or not name:
                continue
            if not any(code.startswith(p) for p in ['60', '000', '001', '002', '30', '688']):
                continue
            stock_list.append({'code': code, 'name': name, 'market': get_market_type(code)})
        logger.info(f"股票池构建完成：共 {len(stock_list)} 只")
        return stock_list
    except Exception as e:
        logger.error(f"股票池构建失败：{e}")
        return []


def filter_basic(stock_list: List[Dict]) -> List[Dict]:
    filtered = [s for s in stock_list if 'ST' not in s['name'] and '*ST' not in s['name'] and '退' not in s['name']]
    logger.info(f"基础过滤：{len(filtered)} / {len(stock_list)} 只通过")
    return filtered


def filter_market_cap(provider: AstockProvider, stock_list: List[Dict]) -> List[Dict]:
    market_min_cap = {'mainboard': 50, 'gem': 30, 'star': 40}
    filtered = []
    batch_size = 50
    total = len(stock_list)
    
    for start in range(0, total, batch_size):
        batch = stock_list[start:start + batch_size]
        codes = [s['code'] for s in batch]
        
        try:
            quotes = json.loads(provider.get_realtime_quotes(codes))
            for stock in batch:
                data = quotes.get(stock['code'])
                if not data:
                    continue
                mcap = data.get('mcap_yi')
                if mcap is None or mcap < market_min_cap[stock['market']]:
                    continue
                stock['pe_ttm'] = data.get('pe_ttm')
                stock['pb'] = data.get('pb')
                stock['mcap_yi'] = mcap
                filtered.append(stock)
        except Exception as e:
            logger.warning(f"  批量 {start+1}-{start+batch_size} 失败: {e}")
            continue
        
        if (start // batch_size + 1) % 5 == 0:
            logger.info(f"  进度：{start+batch_size}/{total}")
    
    logger.info(f"市值筛选：{len(filtered)} / {total} 只通过")
    return filtered


def match_industry(stock_list: List[Dict], index_data: Dict) -> List[Dict]:
    """行业匹配（知识库 > API > 名称关键词）"""
    from ai_knowledge_base import lookup_knowledge
    
    stock_industries = index_data['stock_industries']
    use_api = index_data.get('api_success', False)
    hot_boards = index_data.get('hot_boards', {})
    
    matched_by_kb = 0
    matched_by_api = 0
    matched_by_name = 0
    
    for stock in stock_list:
        code = stock['code']
        name = stock.get('name', '')
        
        # 优先级1：知识库（世界知识）
        kb_industries, kb_score = lookup_knowledge(code, name)
        if kb_industries:
            stock['industries'] = kb_industries
            stock['industry_score'] = kb_score
            matched_by_kb += 1
        # 优先级2：API精确匹配
        elif use_api and code in stock_industries:
            info = stock_industries[code]
            stock['industries'] = list(set(info['industries']))[:3]
            stock['industry_score'] = info['max_industry_score']
            matched_by_api += 1
        # 优先级3：名称关键词降级
        else:
            industries, score = match_by_name(name, code)
            stock['industries'] = industries
            stock['industry_score'] = score
            if score > 0:
                matched_by_name += 1
    
    logger.info(f"行业匹配：知识库={matched_by_kb}, API={matched_by_api}, 名称={matched_by_name}, 无匹配={len(stock_list)-matched_by_kb-matched_by_api-matched_by_name}")
    return stock_list


def add_tech_and_score(stock_list: List[Dict]) -> List[Dict]:
    """技术分析 + v7评分"""
    cache = KlineCache(CACHE_DIR)

    # 构建 symbol 列表并批量预取 K 线
    symbols = []
    for stock in stock_list:
        code = stock['code']
        suffix = '.SH' if code.startswith('6') else '.SZ'
        symbols.append(f"{code}{suffix}")

    logger.info("  预取K线数据...")
    kline_data = cache.batch_fetch(symbols, count=60)
    logger.info(f"  K线获取：{len(kline_data)}/{len(symbols)} 只成功")

    for stock in stock_list:
        code = stock['code']
        suffix = '.SH' if code.startswith('6') else '.SZ'
        symbol = f"{code}{suffix}"

        # 技术分析
        df = kline_data.get(symbol)
        if df is not None and len(df) >= 20:
            stock['tech'] = compute_tech_score(df)
        else:
            stock['tech'] = TechScore()

        # v7评分
        stock['total_score'] = score_v7(stock, stock['tech'])

    return sorted(stock_list, key=lambda x: x.get('total_score', 0), reverse=True)


def score_v7(stock: Dict, tech: TechScore) -> float:
    """
    评分 v7：基本面知识驱动 + 技术辅助

    40分 基本面知识（fundamentals JSON 优先，无文件退回行业分）
    30分 技术分析 | 20分 PE估值 | 10分 市场溢价
    """
    pe = stock.get('pe_ttm')
    market = stock.get('market', 'mainboard')
    code = stock.get('code', '')
    name = stock.get('name', '')

    # 1. 基本面知识 (40分)
    # 优先读 fundamentals/{code}.json 用该股票的具体竞争/财务/成长/地缘数据
    fund_score = compute_fundamental_knowledge(code, name)
    if fund_score is not None:
        know_score = fund_score
        stock['_know_source'] = 'fundamentals'
    else:
        # 无基本面 JSON → 退回行业映射分，以 Top500 P10 = 16 为天花板
        # 原则：无数据 ≈ Top500 后10%水平，不应奖励未验证的公司
        industry_score = stock.get('industry_score', 0)
        if industry_score >= 9.5: know_score = 16
        elif industry_score >= 9.0: know_score = 15
        elif industry_score >= 8.5: know_score = 14
        elif industry_score >= 8.0: know_score = 13
        elif industry_score >= 7.5: know_score = 12
        elif industry_score >= 7.0: know_score = 11
        elif industry_score >= 6.5: know_score = 10
        elif industry_score >= 6.0: know_score = 9
        elif industry_score >= 5.5: know_score = 8
        elif industry_score >= 5.0: know_score = 7
        elif industry_score >= 4.5: know_score = 6
        elif industry_score >= 4.0: know_score = 5
        else: know_score = 4
        stock['_know_source'] = 'industry'

    stock['_know_score'] = know_score

    # 2. 技术分析 (30分)
    tech_score = tech.total * 0.30

    # 3. PE估值 (20分) - 合理区间最高分
    if pe and 15 <= pe <= 80: pe_score = 18
    elif pe and 0 < pe < 15: pe_score = 10
    elif pe and 80 < pe <= 200: pe_score = 14
    elif pe and pe > 200: pe_score = 8
    elif pe is not None and pe < 0: pe_score = 5
    else: pe_score = 10

    # 4. 市场溢价 (10分)
    market_bonus = 10 if market == 'star' else (6 if market == 'gem' else 0)

    return know_score + tech_score + pe_score + market_bonus


def generate_report(stocks: List[Dict], hot_boards: Dict[str, float] = None, output_file: str = None) -> str:
    now = datetime.now()
    match_type = "板块成分股API" if any(s.get('industries') and len(s['industries']) > 0 for s in stocks) else "名称关键词"
    
    report = f"""╔════════════════════════════════════════════════════════════════════════╗
║                    J-TradingAgents 选股报告 v4.2                    ║
╠════════════════════════════════════════════════════════════════════════╣
║ 报告编号: JTA-STOCK-PICKER-{now.strftime('%Y%m%d')}-001                         ║
║ 生成日期: {now.strftime('%Y-%m-%d %H:%M:%S')}                                   ║
║ 覆盖市场: A股主板 / 创业板 / 科创板                                   ║
║ 知识库: fundamentals 个股分析 500 只 + world_knowledge 1011 条    ║
║ 最终推荐: {len(stocks)} 只股票                                                ║
╠════════════════════════════════════════════════════════════════════════╣
║ 排名 │ 代码       │ 名称         │ 市场   │ 评分  │ 知识  │ 技术  │ 行业归属         ║
╠═══════╪════════════╪══════════════╪════════╪═══════╪═══════╪═══════╪════════════════════╣
"""
    
    for i, stock in enumerate(stocks, 1):
        code = stock['code']
        name = stock['name'][:8]
        market = {'mainboard': 'A股', 'gem': '创业板', 'star': '科创板'}.get(stock['market'], 'A股')
        score = stock.get('total_score', 0)
        know_s = stock.get('_know_score', 0)
        tech_t = stock.get('tech', TechScore()).total
        industries = ', '.join(stock.get('industries', [])[:3])[:18]
        
        report += f"║ {i:4d} │ {code:10} │ {name:10} │ {market:6} │ {score:5.1f} │ {know_s:5.0f} │ {tech_t:5.1f} │ {industries:20} ║\n"
    
    report += """╚═══════╩════════════╩══════════════╩════════╩═══════╩═══════╩═══════╩════════════════════╝

【评分体系 v7】（总分100分）
├─ 基本面知识 (40分): 优先读 fundamentals/{code}.json 分析竞争优势/财务/成长/地缘政治
├─ 技术分析 (30分): 趋势35% + 动量30% + 量能20% + 形态15%
├─ PE估值  (20分): PE 15-80最优(18分), 极高/极低中等分
└─ 市场溢价 (10分): 科创板+10，创业板+6
"""
    
    if hot_boards:
        sorted_boards = sorted(hot_boards.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
        report += "\n【热门板块】（今日涨跌幅TOP10）\n"
        for name, change in sorted_boards:
            report += f"  • {name}: {change:+.2f}%\n"
    
    report += "\n【风险提示】本报告仅供参考，不构成投资建议。\n"
    
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"报告已保存至：{output_file}")
    
    return report


def main():
    parser = argparse.ArgumentParser(description='J-TradingAgents 选股系统 v4.2')
    parser.add_argument('--top-n', type=int, default=10, help='推荐股票数量')
    parser.add_argument('--output', type=str, help='报告输出文件路径')
    parser.add_argument('--force-update', action='store_true', help='强制重新构建白名单')
    args = parser.parse_args()
    
    logger.info("=" * 70)
    logger.info("J-TradingAgents 选股系统 v4.2 (v7评分)")
    logger.info("=" * 70)
    
    provider = AstockProvider()
    
    whitelist = None
    if not args.force_update:
        whitelist = load_whitelist(DEFAULT_WHITELIST_FILE)
    
    if not whitelist:
        logger.info("\n【阶段1】构建股票池")
        pool = build_stock_pool(provider)
        if not pool: return
        
        logger.info("\n【阶段2】基础过滤")
        pool = filter_basic(pool)
        
        logger.info("\n【阶段3】市值筛选")
        pool = filter_market_cap(provider, pool)
        
        save_whitelist(DEFAULT_WHITELIST_FILE, pool)
        whitelist = pool
    else:
        logger.info(f"\n使用白名单：{len(whitelist)} 只股票")
    
    logger.info("\n【阶段4】更新实时行情")
    whitelist = filter_market_cap(provider, whitelist)
    if not whitelist: return
    
    logger.info("\n【阶段5】知识库行业匹配（优先）")
    index_data = build_industry_index()
    whitelist = match_industry(whitelist, index_data)
    
    logger.info("\n【阶段6】技术分析 + v7评分")
    ranked = add_tech_and_score(whitelist)
    
    logger.info("\n【阶段7】生成报告")
    top_n = min(args.top_n, len(ranked))
    report = generate_report(ranked[:top_n], index_data.get('hot_boards'), args.output)
    
    print("\n" + "=" * 70)
    print("选股结果")
    print("=" * 70)
    print(report)
    logger.info(f"\n选股完成！共推荐 {top_n} 只股票")


if __name__ == "__main__":
    main()