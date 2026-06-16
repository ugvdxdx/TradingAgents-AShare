#!/usr/bin/env python3
"""批量提取结构化知识并写入数据库。

由 AI 在对话中逐批生成 extract_results.json，然后本脚本读取并入库。
"""
import json, os, sys, sqlite3, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.research.store import KnowledgeStore
from tradingagents.research.extractor import StructuredKnowledge, SectorView, StockMention

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'research.db')
RESULTS_PATH = '/tmp/extract_results.json'

def load_and_save():
    """读取提取结果并写入数据库。"""
    if not os.path.exists(RESULTS_PATH):
        print(f'错误: {RESULTS_PATH} 不存在')
        return

    with open(RESULTS_PATH, 'r') as f:
        results = json.load(f)

    store = KnowledgeStore(db_path=DB_PATH)
    db = sqlite3.connect(DB_PATH)
    success = 0
    fail = 0

    for item in results:
        try:
            knowledge = StructuredKnowledge(
                feed_id=item['feed_id'],
                info_type=item.get('info_type', 'research'),
                summary=item.get('summary', ''),
                market_overview=item.get('market_overview', ''),
                sector_views=[SectorView(**sv) for sv in item.get('sector_views', [])],
                stock_mentions=[StockMention(**sm) for sm in item.get('stock_mentions', [])],
                key_insights=item.get('key_insights', []),
                risk_warnings=item.get('risk_warnings', []),
                raw_text_hash=item.get('raw_text_hash', ''),
            )
            knowledge.created_at = item.get('created_at', '')
            store.save(knowledge)

            # 标记已处理
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (item['feed_id'],))
            db.commit()
            success += 1
        except Exception as e:
            fail += 1
            print(f'  失败: {item.get("feed_id", "?")} - {e}')

    db.close()
    store.close()
    print(f'入库完成: success={success}, fail={fail}')

if __name__ == '__main__':
    load_and_save()
