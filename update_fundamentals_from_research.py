"""将研报知识系统中提及的个股信息更新到 fundamentals JSON 文件中。

从 research.db 的 general_knowledge 和 sector_knowledge 表中提取个股提及信息，
匹配 fundamentals 目录下的 JSON 文件，将研报观点作为增量信息写入。
"""
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime


def load_fundamentals(fundamentals_dir: str) -> dict:
    """加载所有 fundamentals JSON 文件，返回 {code: data} 映射。"""
    result = {}
    for f in os.listdir(fundamentals_dir):
        if not f.endswith('.json'):
            continue
        code = f.replace('.json', '')
        filepath = os.path.join(fundamentals_dir, f)
        try:
            with open(filepath, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
                result[code] = data
        except Exception as e:
            print(f'  [WARN] 读取 {filepath} 失败: {e}')
    return result


def build_name_to_code_map(fundamentals: dict) -> dict:
    """构建 {name: code} 反向映射。"""
    return {data['name']: code for code, data in fundamentals.items() if 'name' in data}


def extract_stock_knowledge(db_path: str) -> dict:
    """从 research.db 提取所有个股相关研报知识。

    Returns:
        {stock_name: {
            'code': str,
            'mentions': [{'sentiment', 'reason', 'date', 'info_type'}],
            'sector_views': [{'sector', 'viewpoint', 'sentiment', 'logic_chain', 'date'}],
        }}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    stock_knowledge = defaultdict(lambda: {
        'code': '',
        'mentions': [],
        'sector_views': [],
    })

    # 1. 从 general_knowledge 提取 stock_mentions
    c.execute("""
        SELECT gk.feed_id, gk.info_type, gk.stock_mentions, gk.created_at,
               gk.summary, gk.key_insights, gk.risk_warnings
        FROM general_knowledge gk
        WHERE gk.stock_mentions IS NOT NULL AND gk.stock_mentions != '[]'
    """)
    for row in c.fetchall():
        mentions = json.loads(row['stock_mentions'])
        date_str = (row['created_at'] or '')[:10]
        for m in mentions:
            name = m.get('name', '')
            if not name:
                continue
            code = m.get('code', '')
            if code:
                stock_knowledge[name]['code'] = code
            stock_knowledge[name]['mentions'].append({
                'sentiment': m.get('sentiment', 'neutral'),
                'reason': m.get('reason', ''),
                'date': date_str,
                'info_type': row['info_type'],
                'summary': row['summary'] or '',
            })

    # 2. 从 sector_knowledge 提取行业观点（用于关联个股的行业背景）
    c.execute("""
        SELECT sk.sector, sk.viewpoint, sk.sentiment, sk.logic_chain,
               sk.key_data, sk.created_at, sk.feed_id
        FROM sector_knowledge sk
    """)
    sector_by_feed = defaultdict(list)
    for row in c.fetchall():
        sector_by_feed[row['feed_id']].append({
            'sector': row['sector'],
            'viewpoint': row['viewpoint'],
            'sentiment': row['sentiment'],
            'logic_chain': json.loads(row['logic_chain']) if row['logic_chain'] else [],
            'key_data': json.loads(row['key_data']) if row['key_data'] else [],
            'date': (row['created_at'] or '')[:10],
        })

    # 3. 关联：找到每个个股对应的行业观点
    # 通过 feed_id 将 general_knowledge 和 sector_knowledge 关联
    c.execute("""
        SELECT gk.feed_id, gk.stock_mentions
        FROM general_knowledge gk
        WHERE gk.stock_mentions IS NOT NULL AND gk.stock_mentions != '[]'
    """)
    for row in c.fetchall():
        mentions = json.loads(row['stock_mentions'])
        feed_id = row['feed_id']
        sectors = sector_by_feed.get(feed_id, [])
        for m in mentions:
            name = m.get('name', '')
            if name and sectors:
                stock_knowledge[name]['sector_views'].extend(sectors)

    conn.close()

    # 去重 sector_views
    for name in stock_knowledge:
        seen = set()
        unique = []
        for sv in stock_knowledge[name]['sector_views']:
            key = (sv['sector'], sv['viewpoint'], sv['date'])
            if key not in seen:
                seen.add(key)
                unique.append(sv)
        stock_knowledge[name]['sector_views'] = unique

    return dict(stock_knowledge)


def update_fundamental_json(filepath: str, data: dict, stock_name: str, knowledge: dict) -> bool:
    """将研报知识更新到 fundamentals JSON 文件中。

    在 JSON 中添加/更新 'research_insights' 字段：
    - latest_mentions: 最近研报提及（去重，最多保留20条）
    - sector_views: 相关行业观点（去重，最多保留10条）
    - overall_sentiment: 综合情绪统计
    - last_updated: 更新时间

    Returns:
        True 如果有更新，False 如果无变化
    """
    mentions = knowledge.get('mentions', [])
    sector_views = knowledge.get('sector_views', [])

    if not mentions and not sector_views:
        return False

    # 统计综合情绪
    sentiment_counts = defaultdict(int)
    for m in mentions:
        sentiment_counts[m['sentiment']] += 1
    total = sum(sentiment_counts.values())
    overall_sentiment = {
        'bullish_count': sentiment_counts.get('bullish', 0),
        'bearish_count': sentiment_counts.get('bearish', 0),
        'neutral_count': sentiment_counts.get('neutral', 0),
        'total_mentions': total,
    }
    if total > 0:
        if sentiment_counts.get('bullish', 0) > sentiment_counts.get('bearish', 0) * 2:
            overall_sentiment['overall'] = 'bullish'
        elif sentiment_counts.get('bearish', 0) > sentiment_counts.get('bullish', 0) * 2:
            overall_sentiment['overall'] = 'bearish'
        else:
            overall_sentiment['overall'] = 'neutral'
    else:
        overall_sentiment['overall'] = 'neutral'

    # 精简 mentions（去重，按日期倒序，最多20条）
    seen_reasons = set()
    unique_mentions = []
    for m in sorted(mentions, key=lambda x: x.get('date', ''), reverse=True):
        reason_key = m.get('reason', '')[:50]
        if reason_key not in seen_reasons:
            seen_reasons.add(reason_key)
            unique_mentions.append({
                'sentiment': m['sentiment'],
                'reason': m['reason'],
                'date': m['date'],
                'info_type': m['info_type'],
            })
    unique_mentions = unique_mentions[:20]

    # 精简 sector_views（最多10条）
    unique_sectors = sector_views[:10]

    # 构建 research_insights
    research_insights = {
        'latest_mentions': unique_mentions,
        'sector_views': [
            {
                'sector': sv['sector'],
                'viewpoint': sv['viewpoint'],
                'sentiment': sv['sentiment'],
                'logic_chain': sv['logic_chain'][:3],  # 最多3条逻辑链
                'date': sv['date'],
            }
            for sv in unique_sectors
        ],
        'overall_sentiment': overall_sentiment,
        'last_updated': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
    }

    # 检查是否有变化
    existing = data.get('research_insights', {})
    if existing.get('last_updated') == research_insights['last_updated']:
        return False

    # 更新 JSON
    data['research_insights'] = research_insights

    # 写入文件
    with open(filepath, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

    return True


def main():
    project_dir = os.path.dirname(os.path.abspath(__file__))
    fundamentals_dir = os.path.join(project_dir, 'fundamentals')
    db_path = os.path.join(project_dir, 'research.db')

    print('=== 研报知识 → Fundamentals JSON 更新 ===')
    print()

    # 1. 加载 fundamentals
    print('[1/4] 加载 fundamentals JSON 文件...')
    fundamentals = load_fundamentals(fundamentals_dir)
    print(f'  共 {len(fundamentals)} 个 fundamentals 文件')

    # 2. 构建名称映射
    name_to_code = build_name_to_code_map(fundamentals)
    print(f'  名称映射: {len(name_to_code)} 个')

    # 3. 提取研报知识
    print('[2/4] 从 research.db 提取个股研报知识...')
    stock_knowledge = extract_stock_knowledge(db_path)
    print(f'  研报提及个股: {len(stock_knowledge)} 个')

    # 4. 匹配并更新
    print('[3/4] 匹配并更新 fundamentals JSON...')
    matched_count = 0
    updated_count = 0
    unmatched_names = []

    for name, knowledge in stock_knowledge.items():
        code = knowledge.get('code', '')
        # 优先用研报中的 code 匹配
        if code and code in fundamentals:
            matched_code = code
        elif name in name_to_code:
            matched_code = name_to_code[name]
        else:
            # 尝试模糊匹配
            matched_code = None
            for fname, fcode in name_to_code.items():
                if name in fname or fname in name:
                    matched_code = fcode
                    break

        if matched_code:
            matched_count += 1
            filepath = os.path.join(fundamentals_dir, f'{matched_code}.json')
            data = fundamentals[matched_code]
            if update_fundamental_json(filepath, data, name, knowledge):
                updated_count += 1
                sentiment = knowledge.get('mentions', [])
                bullish = sum(1 for m in sentiment if m.get('sentiment') == 'bullish')
                bearish = sum(1 for m in sentiment if m.get('sentiment') == 'bearish')
                print(f'  ✓ {name} ({matched_code}): {len(knowledge["mentions"])} mentions, bullish={bullish}/bearish={bearish}')
        else:
            unmatched_names.append(name)

    # 5. 统计
    print()
    print('[4/4] 更新完成')
    print(f'  研报提及个股总数: {len(stock_knowledge)}')
    print(f'  匹配到 fundamentals: {matched_count}')
    print(f'  实际更新文件数: {updated_count}')
    print(f'  未匹配个股: {len(unmatched_names)}')

    if unmatched_names:
        print()
        print('  未匹配个股列表 (前30个):')
        for name in sorted(unmatched_names)[:30]:
            print(f'    - {name}')


if __name__ == '__main__':
    main()
