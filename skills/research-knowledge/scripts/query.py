#!/usr/bin/env python3
"""研报知识系统 — 命令行检索工具"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[3] / 'research.db'


def query_sector(sector: str, limit: int = 20):
    """按行业/板块检索知识"""
    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute("""
        SELECT feed_id, sector, viewpoint, logic_chain, sentiment, key_data, created_at
        FROM sector_knowledge
        WHERE sector LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (f'%{sector}%', limit)).fetchall()
    db.close()
    if not rows:
        print(f'未找到行业 "{sector}" 的知识')
        return
    for r in rows:
        print(f'[{r[6]}] {r[1]} | {r[3]} | sentiment={r[4]}')
        print(f'  观点: {r[2]}')
        if r[5]:
            data = json.loads(r[5])
            print(f'  关键数据: {", ".join(data[:5])}')
        print()


def query_stock(name: str, limit: int = 20):
    """按个股名称检索知识"""
    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute("""
        SELECT feed_id, info_type, summary, stock_mentions, created_at
        FROM general_knowledge
        WHERE stock_mentions LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (f'%{name}%', limit)).fetchall()
    db.close()
    if not rows:
        print(f'未找到个股 "{name}" 的提及')
        return
    for r in rows:
        print(f'[{r[4]}] {r[1]}')
        print(f'  摘要: {r[2][:100]}...' if len(r[2]) > 100 else f'  摘要: {r[2]}')
        if r[3]:
            mentions = json.loads(r[3])
            for m in mentions:
                if name in m.get('name', ''):
                    print(f'  个股: {m["name"]}({m.get("code","")}) sentiment={m.get("sentiment","")} reason={m.get("reason","")}')
        print()


def query_date(date: str):
    """按日期检索每日复盘"""
    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute("""
        SELECT g.feed_id, g.info_type, g.summary, g.key_insights, g.risk_warnings, g.created_at
        FROM general_knowledge g
        WHERE g.created_at LIKE ?
        ORDER BY g.created_at ASC
    """, (f'{date}%',)).fetchall()
    db.close()
    if not rows:
        print(f'未找到日期 {date} 的知识')
        return
    print(f'=== {date} 研报知识 ({len(rows)} 条) ===\n')
    for r in rows:
        print(f'[{r[5]}] {r[1]}')
        print(f'  摘要: {r[2][:120]}...' if len(r[2]) > 120 else f'  摘要: {r[2]}')
        if r[3]:
            insights = json.loads(r[3])
            for i in insights[:3]:
                print(f'  洞察: {i}')
        if r[4]:
            risks = json.loads(r[4])
            for ri in risks[:2]:
                print(f'  风险: {ri}')
        print()


def stats():
    """输出知识库统计"""
    db = sqlite3.connect(str(DB_PATH))
    total = db.execute('SELECT COUNT(*) FROM raw_feeds').fetchone()[0]
    processed = db.execute('SELECT COUNT(*) FROM raw_feeds WHERE is_processed = 1').fetchone()[0]
    sector_count = db.execute('SELECT COUNT(*) FROM sector_knowledge').fetchone()[0]
    general_count = db.execute('SELECT COUNT(*) FROM general_knowledge').fetchone()[0]
    daily_count = db.execute('SELECT COUNT(*) FROM daily_review').fetchone()[0]
    date_range = db.execute('SELECT MIN(created_at), MAX(created_at) FROM raw_feeds').fetchone()
    db.close()
    print('=== 研报知识系统统计 ===')
    print(f'原始帖子: {total} (已处理: {processed}, 处理率: {processed/total*100:.1f}%)')
    print(f'行业知识库: {sector_count} 条')
    print(f'通用知识库: {general_count} 条')
    print(f'每日复盘: {daily_count} 条')
    print(f'时间范围: {date_range[0]} ~ {date_range[1]}')


def main():
    parser = argparse.ArgumentParser(description='研报知识系统检索工具')
    sub = parser.add_subparsers(dest='command')

    s = sub.add_parser('sector', help='按行业检索')
    s.add_argument('name', help='行业/板块关键词')
    s.add_argument('-n', '--limit', type=int, default=20, help='返回条数')

    s = sub.add_parser('stock', help='按个股检索')
    s.add_argument('name', help='个股名称')
    s.add_argument('-n', '--limit', type=int, default=20, help='返回条数')

    s = sub.add_parser('date', help='按日期检索')
    s.add_argument('date', help='日期 (YYYY-MM-DD)')

    sub.add_parser('stats', help='知识库统计')

    args = parser.parse_args()
    if args.command == 'sector':
        query_sector(args.name, args.limit)
    elif args.command == 'stock':
        query_stock(args.name, args.limit)
    elif args.command == 'date':
        query_date(args.date)
    elif args.command == 'stats':
        stats()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
