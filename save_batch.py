#!/usr/bin/env python3
"""批量写入提取结果到数据库。"""
import json, os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.research.store import KnowledgeStore
from tradingagents.research.extractor import StructuredKnowledge, SectorView, StockMention

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'research.db')
BATCH_FILE = '/tmp/extract_batch.json'

def save_batch():
    with open(BATCH_FILE, 'r') as f:
        items = json.load(f)

    store = KnowledgeStore(db_path=DB_PATH)
    db = sqlite3.connect(DB_PATH)
    success = 0
    fail = 0

    for item in items:
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
            db.execute('UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?', (item['feed_id'],))
            db.commit()
            success += 1
        except Exception as e:
            fail += 1
            print(f'  FAIL: {item.get("feed_id", "?")} - {e}')

    db.close()
    store.close()
    print(f'Batch saved: success={success}, fail={fail}')

if __name__ == '__main__':
    save_batch()
