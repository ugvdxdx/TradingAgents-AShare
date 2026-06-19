"""将研报知识系统中提及的个股信息，经 LLM 提炼后更新到 fundamentals JSON 文件中。

核心思路：
  1. 从 research.db 提取个股研报知识（mentions + sector_views）
  2. 过滤噪音：剔除纯盘中快讯，只保留有分析价值的提及
  3. 用 LLM 提炼为与 fundamentals JSON 现有字段对齐的增量信息
  4. 增量追加到现有字段，去重不覆盖

使用:
  cd /path/to/J-TradingAgents
  uv run python3 update_fundamentals_from_research.py
  uv run python3 update_fundamentals_from_research.py --stock 300308  # 只更新指定个股
  uv run python3 update_fundamentals_from_research.py --dry-run       # 只输出不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

# 确保独立运行时也能读到 .env
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass


# ═══════════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════════

def get_llm_helper():
    """懒加载 LLMHelper。"""
    from tradingagents.agents.picker.llm_helper import LLMHelper
    return LLMHelper()


# ═══════════════════════════════════════════════════════════
# 数据提取
# ═══════════════════════════════════════════════════════════

def load_fundamentals(fundamentals_dir: str) -> dict:
    """加载所有 fundamentals JSON 文件，返回 {code: data} 映射。"""
    result = {}
    for f in sorted(os.listdir(fundamentals_dir)):
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
            'mentions': [{'sentiment', 'reason', 'date', 'info_type', 'summary'}],
            'sector_views': [{'sector', 'viewpoint', 'sentiment', 'logic_chain', 'key_data', 'date'}],
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

    # 2. 从 sector_knowledge 提取行业观点
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

    # 3. 关联行业观点
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
            key = (sv['sector'], sv['viewpoint'][:50], sv['date'])
            if key not in seen:
                seen.add(key)
                unique.append(sv)
        stock_knowledge[name]['sector_views'] = unique

    return dict(stock_knowledge)


# ═══════════════════════════════════════════════════════════
# 研报知识过滤与精简
# ═══════════════════════════════════════════════════════════

NOISE_PATTERNS = re.compile(
    r'^(涨停|跌停|涨超|跌超|涨逾|跌逾|大涨|大跌|冲高|回落|拉升|跳水|封板|开板|炸板|'
    r'跟涨|跟跌|翻红|翻绿|小幅|微涨|微跌|横盘|震荡|高开|低开|平开|'
    r'\d+连板|\d+cm涨停|20%涨停|10cm涨停|涨\d+%|跌\d+%)'
)


def filter_noise_mentions(mentions: list) -> list:
    """过滤纯盘中快讯噪音，只保留有分析价值的提及。"""
    filtered = []
    for m in mentions:
        reason = m.get('reason', '').strip()
        # 纯涨跌快讯过滤
        if NOISE_PATTERNS.match(reason) and len(reason) <= 15:
            continue
        # 空理由过滤
        if not reason:
            continue
        filtered.append(m)
    return filtered


# ═══════════════════════════════════════════════════════════
# 高动量赛道催化提取 (research_catalysts)
# ═══════════════════════════════════════════════════════════

# 高动量赛道关键词表：命中则视为研报背书的高动量新增长极。
# key = 催化标签（写入 catalyst_tags），value = 触发关键词列表。
HIGH_MOMENTUM_KEYWORDS = {
    "AI算力/光模块": ["光模块", "cpo", "光通信", "硅光", "opo", "lpo", "800g", "1.6t", "光互联"],
    "PCB/CCL": ["pcb", "ccl", "覆铜板", "电子布", "钻针", "高频高速"],
    "先进封装": ["先进封装", "copos", "面板级封装", "玻璃基板", "chiplet", "2.5d", "3d封装"],
    "存储/HBM": ["hbm", "存储芯片", "ddr", "存储颗粒", "nand", "dram"],
    "AI芯片/算力": ["ai芯片", "gpu", "算力芯片", "昇腾", "国产算力", "asic", "推理芯片"],
    "AI电源/散热": ["ai电源", "液冷", "散热", "hvdc", "800v", "固态变压器", "sst", "电源管理"],
    "AI用铜/连接": ["数据中心用铜", "ai用铜", "铜连接", "高速铜缆", "铜缆"],
    "战略金属": ["战略金属", "稀土", "锗", "铟", "镓", "稀有金属", "稀散金属", "永磁"],
    "半导体设备/材料": ["半导体设备", "光刻", "刻蚀", "半导体材料", "靶材", "电子特气", "石英", "氢氟酸", "光刻胶"],
    "机器人/物理AI": ["人形机器人", "物理ai", "机器人核心零部件", "减速器", "丝杠", "灵巧手"],
    "固态电池": ["固态电池", "半固态", "硫化物电解质"],
    "商业航天": ["商业航天", "卫星", "spacex", "火箭", "星网"],
    "创新药": ["创新药", "adc", "双抗", "glp-1", "出海授权", "license out"],
}

# research_catalysts 只看近 N 天的 bullish 提及与行业观点，避免陈旧题材累积。
CATALYST_LOOKBACK_DAYS = 45


def _match_momentum_tags(text: str) -> List[str]:
    """返回 text 命中的高动量赛道标签列表。"""
    low = text.lower()
    tags = []
    for tag, kws in HIGH_MOMENTUM_KEYWORDS.items():
        if any(kw in low for kw in kws):
            tags.append(tag)
    return tags


def compute_research_catalysts(knowledge: dict, latest_db_date: str = '') -> dict:
    """从研报知识中提取高动量赛道催化，输出 research_catalysts 结构。

    逻辑（外部市场视角，与公司自述隔离）：
      1. 只用**个股专属**的 bullish 提及 reason（stock_mention.reason 是 LLM 按个股提取的，
         不会像 sector_views 那样被 feed 级别地散播到同帖所有个股 → 避免误报）
      2. 用 HIGH_MOMENTUM_KEYWORDS 匹配出命中的高动量赛道标签
      3. high_momentum_exposure (0-5) = f(命中标签数, 证据条数, 看多一致性)
      4. 记录证据明细供评分器与人工复核

    Returns: {} 表示无高动量催化（评分器据此完全按主业判断）。

    注意：刻意不使用 sector_views/summary —— 它们是 feed 级别的共享上下文，
    会把同帖其它个股的赛道观点错误地安到本股头上（如养猪股被打上"机器人"标签）。
    """
    # 基准日：优先用库中最新研报日期，否则用系统时间
    if latest_db_date:
        base = latest_db_date[:10]
    else:
        base = datetime.now().strftime('%Y-%m-%d')
    from datetime import timedelta
    try:
        base_dt = datetime.strptime(base, '%Y-%m-%d')
    except ValueError:
        base_dt = datetime.now()
    cutoff = (base_dt - timedelta(days=CATALYST_LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    evidence = []
    tag_set = set()

    # 个股专属提及（只看近期 bullish，过滤噪音；仅匹配 reason 这一股票专属字段）
    for m in filter_noise_mentions(knowledge.get('mentions', [])):
        date = m.get('date', '')
        if date < cutoff:
            continue
        if m.get('sentiment') != 'bullish':
            continue
        reason = m.get('reason', '')
        tags = _match_momentum_tags(reason)
        if tags:
            tag_set.update(tags)
            evidence.append({
                'source': 'stock_mention',
                'catalyst': reason[:60],
                'tags': tags,
                'sentiment': 'bullish',
                'date': date,
            })

    if not tag_set:
        return {}

    # 暴露度评分 0-5：标签丰富度 + 证据条数（研报反复背书）
    n_tags = len(tag_set)
    n_ev = len(evidence)
    exposure = min(n_tags, 3) + (1 if n_ev >= 2 else 0) + (1 if n_ev >= 4 else 0)
    exposure = max(0, min(5, exposure))

    # 证据按日期倒序，最多留 6 条
    evidence.sort(key=lambda x: x.get('date', ''), reverse=True)
    latest_date = evidence[0]['date'] if evidence else ''

    return {
        'high_momentum_exposure': exposure,
        'catalyst_tags': sorted(tag_set),
        'evidence': evidence[:6],
        'latest_date': latest_date,
        'lookback_days': CATALYST_LOOKBACK_DAYS,
        'updated_at': base,
    }


def prepare_research_context(stock_name: str, knowledge: dict) -> str:
    """将研报知识整理为 LLM 可读的上下文文本。"""
    parts = [f'## {stock_name} 研报知识汇总\n']

    # 个股提及
    mentions = filter_noise_mentions(knowledge.get('mentions', []))
    if mentions:
        parts.append('### 个股提及（按日期倒序）')
        for m in sorted(mentions, key=lambda x: x.get('date', ''), reverse=True)[:15]:
            sentiment_map = {'bullish': '看多', 'bearish': '看空', 'neutral': '中性'}
            parts.append(f"- {m['date']} [{sentiment_map.get(m['sentiment'], m['sentiment'])}] {m['reason']}")
        parts.append('')

    # 行业观点
    sector_views = knowledge.get('sector_views', [])
    if sector_views:
        parts.append('### 相关行业观点')
        for sv in sector_views[:8]:
            sentiment_map = {'bullish': '偏多', 'bearish': '偏空', 'neutral': '中性'}
            parts.append(f"- [{sv['sector']}] {sentiment_map.get(sv['sentiment'], sv['sentiment'])}: {sv['viewpoint']}")
            for lc in sv.get('logic_chain', [])[:2]:
                parts.append(f"  逻辑: {lc}")
        parts.append('')

    return '\n'.join(parts)


# ═══════════════════════════════════════════════════════════
# LLM 提炼 Prompt
# ═══════════════════════════════════════════════════════════

REFINE_PROMPT = """你是一位资深金融分析师。请根据研报知识，为个股 fundamentals 提炼增量信息。

## 个股基本信息
名称: {name}
代码: {code}
行业: {industry}

## 现有 fundamentals 摘要
{existing_summary}

## 研报知识（来源：2026年4月-6月财经博主圈子研报）
{research_context}

## 任务
请从研报知识中提炼出对该公司基本面分析有价值的增量信息，严格按以下 JSON 格式输出：

```json
{{
  "competitive_analysis": {{
    "strengths": ["研报支撑的新竞争优势1", "研报支撑的新竞争优势2"],
    "weaknesses": ["研报指出的新风险1"]
  }},
  "growth_assessment": {{
    "growth_drivers": ["研报看好的新增长驱动1"],
    "headwinds": ["研报提示的新逆风1"]
  }},
  "geopolitical_assessment": {{
    "risks": ["研报提示的新政策/地缘风险1"],
    "opportunities": ["研报提示的新政策/行业机会1"]
  }},
  "research_summary": "100字以内的研报近期观点总结"
}}
```

## 要求
1. 只提炼有实质分析价值的信息，不要搬运纯涨跌快讯
2. 每个字段最多3条，宁缺毋滥，没有则留空数组
3. 与现有 fundamentals 摘要去重：不要重复已有内容
4. 研报观点需标注来源性质，如"研报认为..."、"圈子观点..."
5. research_summary 要综合多日研报观点，不要只写某一天
6. 如果研报知识没有有价值的信息，所有字段留空数组，research_summary 留空字符串"""


# ═══════════════════════════════════════════════════════════
# LLM 调用与解析
# ═══════════════════════════════════════════════════════════

def refine_with_llm(llm, stock_name: str, code: str, industry: str,
                    existing_summary: str, research_context: str) -> Optional[dict]:
    """调用 LLM 提炼研报知识为结构化增量信息。"""
    prompt = REFINE_PROMPT.format(
        name=stock_name,
        code=code,
        industry=industry,
        existing_summary=existing_summary[:800],
        research_context=research_context[:3000],
    )

    try:
        response = llm.call(
            system_msg='你是资深金融分析师，擅长从研报中提炼有价值的增量信息。请严格按JSON格式输出。',
            human_msg=prompt,
            deep=False,  # 用 quick 模型，节省成本
        )
        return parse_llm_json(response)
    except Exception as e:
        print(f'  [ERROR] LLM 调用失败 ({stock_name}): {e}')
        return None


def parse_llm_json(response: str) -> Optional[dict]:
    """解析 LLM 返回的 JSON。"""
    if not response:
        return None

    # 提取 ```json ... ``` 块
    m = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        # 尝试直接找 JSON 对象
        m = re.search(r'\{.*\}', response, re.DOTALL)
        text = m.group(0) if m else response

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 修复常见问题
        text = re.sub(r',\s*}', '}', text)
        text = re.sub(r',\s*]', ']', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


# ═══════════════════════════════════════════════════════════
# 增量合并
# ═══════════════════════════════════════════════════════════

def get_existing_summary(data: dict) -> str:
    """从 fundamentals JSON 中提取现有摘要信息，供 LLM 去重参考。"""
    parts = []

    ca = data.get('competitive_analysis', {})
    if ca.get('strengths'):
        parts.append('现有优势: ' + '; '.join(ca['strengths'][:3]))
    if ca.get('weaknesses'):
        parts.append('现有劣势: ' + '; '.join(ca['weaknesses'][:3]))

    ga = data.get('growth_assessment', {})
    if ga.get('growth_drivers'):
        parts.append('现有增长驱动: ' + '; '.join(ga['growth_drivers'][:3]))
    if ga.get('headwinds'):
        parts.append('现有逆风: ' + '; '.join(ga['headwinds'][:3]))

    geo = data.get('geopolitical_assessment', {})
    if geo.get('risks'):
        parts.append('现有风险: ' + '; '.join(geo['risks'][:2]))
    if geo.get('opportunities'):
        parts.append('现有机会: ' + '; '.join(geo['opportunities'][:2]))

    if data.get('summary'):
        parts.append('现有总结: ' + data['summary'][:200])

    return '\n'.join(parts)


def merge_incremental(data: dict, incremental: dict) -> bool:
    """将 LLM 提炼的增量信息合并到 fundamentals JSON 中。

    Returns:
        True 如果有实质性更新
    """
    updated = False

    # 合并 competitive_analysis
    ca_inc = incremental.get('competitive_analysis', {})
    for field in ('strengths', 'weaknesses'):
        new_items = ca_inc.get(field, [])
        if not new_items:
            continue
        existing = data.setdefault('competitive_analysis', {}).setdefault(field, [])
        for item in new_items:
            if item and item not in existing:
                existing.append(item)
                updated = True

    # 合并 growth_assessment
    ga_inc = incremental.get('growth_assessment', {})
    for field in ('growth_drivers', 'headwinds'):
        new_items = ga_inc.get(field, [])
        if not new_items:
            continue
        existing = data.setdefault('growth_assessment', {}).setdefault(field, [])
        for item in new_items:
            if item and item not in existing:
                existing.append(item)
                updated = True

    # 合并 geopolitical_assessment
    geo_inc = incremental.get('geopolitical_assessment', {})
    for field in ('risks', 'opportunities'):
        new_items = geo_inc.get(field, [])
        if not new_items:
            continue
        existing = data.setdefault('geopolitical_assessment', {}).setdefault(field, [])
        for item in new_items:
            if item and item not in existing:
                existing.append(item)
                updated = True

    # 追加 research_summary 到 summary 末尾
    research_summary = incremental.get('research_summary', '')
    if research_summary:
        existing_summary = data.get('summary', '')
        # 检查是否已有研报观点段落
        if '【研报近期观点】' not in existing_summary:
            data['summary'] = existing_summary.rstrip() + f'\n\n【研报近期观点】{research_summary}'
            updated = True
        else:
            # 替换已有研报观点
            parts = existing_summary.split('【研报近期观点】')
            data['summary'] = parts[0].rstrip() + f'\n\n【研报近期观点】{research_summary}'
            updated = True

    return updated


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def match_stock_to_fundamental(name: str, knowledge: dict,
                                fundamentals: dict, name_to_code: dict) -> Optional[str]:
    """将研报个股名匹配到 fundamentals 代码。"""
    code = knowledge.get('code', '')

    # 优先用研报中的 code 匹配
    if code and code in fundamentals:
        return code

    # 精确名称匹配
    if name in name_to_code:
        return name_to_code[name]

    # 模糊匹配
    for fname, fcode in name_to_code.items():
        if name in fname or fname in name:
            return fcode

    return None


def process_stock(llm, stock_name: str, matched_code: str,
                  fundamentals: dict, knowledge: dict,
                  fundamentals_dir: str, dry_run: bool = False,
                  latest_db_date: str = '') -> bool:
    """处理单个个股：提炼研报知识并更新 fundamentals JSON。"""
    data = fundamentals[matched_code]
    industry = data.get('business_overview', {}).get('industry', '')

    # 过滤噪音
    mentions = filter_noise_mentions(knowledge.get('mentions', []))
    if not mentions and not knowledge.get('sector_views'):
        return False

    updated = False

    # ── A. 提取 research_catalysts（规则提取，独立于 LLM）──
    catalysts = compute_research_catalysts(knowledge, latest_db_date=latest_db_date)
    old_catalysts = data.get('research_catalysts')
    if catalysts:
        if old_catalysts != catalysts:
            data['research_catalysts'] = catalysts
            updated = True
    elif old_catalysts:
        # 近期已无高动量催化 → 清除陈旧标记，避免误导评分器
        data.pop('research_catalysts', None)
        updated = True

    # ── B. LLM 提炼增量信息（strengths/headwinds/summary 等）──
    research_context = prepare_research_context(stock_name, knowledge)
    existing_summary = get_existing_summary(data)

    incremental = refine_with_llm(
        llm, stock_name, matched_code, industry,
        existing_summary, research_context,
    )

    has_content = bool(incremental) and (any(
        incremental.get(section, {}).get(field)
        for section in ('competitive_analysis', 'growth_assessment', 'geopolitical_assessment')
        for field in ('strengths', 'weaknesses', 'growth_drivers', 'headwinds', 'risks', 'opportunities')
    ) or incremental.get('research_summary'))

    if has_content and merge_incremental(data, incremental):
        updated = True

    if not updated:
        return False

    # 写入文件
    if not dry_run:
        filepath = os.path.join(fundamentals_dir, f'{matched_code}.json')
        with open(filepath, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

    return True


def main():
    parser = argparse.ArgumentParser(description='将研报知识提炼后更新到 fundamentals JSON')
    parser.add_argument('--stock', type=str, help='只更新指定代码的个股 (如 300308)')
    parser.add_argument('--dry-run', action='store_true', help='只输出不写文件')
    parser.add_argument('--min-mentions', type=int, default=2, help='最少提及次数才处理 (默认2)')
    args = parser.parse_args()

    from picker import paths
    project_dir = paths.PROJECT_ROOT
    fundamentals_dir = paths.FUNDAMENTALS_DIR
    db_path = paths.RESEARCH_DB

    print('=== 研报知识 → Fundamentals JSON 更新（LLM 提炼版）===')
    print()

    # 1. 加载 fundamentals
    print('[1/4] 加载 fundamentals JSON 文件...')
    fundamentals = load_fundamentals(fundamentals_dir)
    name_to_code = build_name_to_code_map(fundamentals)
    print(f'  共 {len(fundamentals)} 个 fundamentals 文件')

    # 2. 提取研报知识
    print('[2/4] 从 research.db 提取个股研报知识...')
    stock_knowledge = extract_stock_knowledge(db_path)
    print(f'  研报提及个股: {len(stock_knowledge)} 个')

    # 库中最新研报日期（作为 research_catalysts 的回看基准，离线/回测友好）
    latest_db_date = ''
    for kn in stock_knowledge.values():
        for m in kn.get('mentions', []):
            if m.get('date', '') > latest_db_date:
                latest_db_date = m['date']
    print(f'  最新研报日期: {latest_db_date or "N/A"}')

    # 3. 匹配
    print('[3/4] 匹配并 LLM 提炼...')
    llm = get_llm_helper()

    matched_stocks = []
    unmatched_names = []

    for name, knowledge in stock_knowledge.items():
        # 过滤低频提及
        mentions = filter_noise_mentions(knowledge.get('mentions', []))
        if len(mentions) < args.min_mentions and not knowledge.get('sector_views'):
            continue

        matched_code = match_stock_to_fundamental(name, knowledge, fundamentals, name_to_code)

        if matched_code:
            # 如果指定了 --stock，只处理该个股
            if args.stock and matched_code != args.stock:
                continue
            matched_stocks.append((name, matched_code, knowledge))
        else:
            unmatched_names.append(name)

    # 按提及数排序，多的先处理
    matched_stocks.sort(key=lambda x: len(x[2].get('mentions', [])), reverse=True)

    print(f'  待处理个股: {len(matched_stocks)} 个')
    print()

    # 4. 逐个处理
    updated_count = 0
    skipped_count = 0

    for i, (name, code, knowledge) in enumerate(matched_stocks, 1):
        mentions = filter_noise_mentions(knowledge.get('mentions', []))
        bullish = sum(1 for m in mentions if m.get('sentiment') == 'bullish')
        bearish = sum(1 for m in mentions if m.get('sentiment') == 'bearish')
        print(f'  [{i}/{len(matched_stocks)}] {name} ({code}): {len(mentions)} mentions, bullish={bullish}/bearish={bearish}')

        try:
            success = process_stock(
                llm, name, code, fundamentals, knowledge,
                fundamentals_dir, dry_run=args.dry_run,
                latest_db_date=latest_db_date,
            )
            if success:
                updated_count += 1
                mode = '[DRY-RUN] ' if args.dry_run else ''
                print(f'    {mode}✓ 已更新')
            else:
                skipped_count += 1
                print(f'    - 无增量信息，跳过')
        except Exception as e:
            print(f'    ✗ 处理失败: {e}')
            skipped_count += 1

        # 简单限流
        if i < len(matched_stocks):
            time.sleep(0.5)

    # 5. 统计
    print()
    print('[4/4] 完成')
    print(f'  待处理: {len(matched_stocks)}')
    print(f'  已更新: {updated_count}')
    print(f'  跳过: {skipped_count}')
    print(f'  未匹配: {len(unmatched_names)}')

    if unmatched_names and len(unmatched_names) <= 20:
        print()
        print('  未匹配个股:')
        for name in sorted(unmatched_names):
            print(f'    - {name}')


if __name__ == '__main__':
    main()
