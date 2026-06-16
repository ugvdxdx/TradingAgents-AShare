"""L4 — 知识存储层: SQLite + 双层知识库。

双层架构:
  1. 行业知识库 (sector_knowledge): 按行业/板块维度组织
  2. 通用知识库 (general_knowledge): 按时间/类型维度组织

三种知识组织形式:
  A. 每日复盘知识库 (daily_review): 按日期索引
  B. 行业专题知识库 (sector_topic): 按行业索引
  C. 分类知识库 (category): 按信息类型索引

存储特点:
  - 精简存储: 只存结构化知识，原文通过 feed_id 关联
  - 快照支持: knowledge_snapshots 表支持回测
  - 增量更新: 基于 raw_text_hash 检测变更

使用:
  from tradingagents.research.store import KnowledgeStore
  store = KnowledgeStore(db_path='research.db')
  store.save(knowledge)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from .extractor import StructuredKnowledge, SectorView, StockMention


class KnowledgeStore:
    """知识存储层，管理双层知识库。"""

    def __init__(self, db_path: str = 'research.db'):
        self.db_path = db_path
        self._db = None

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(self.db_path)
            self._db.row_factory = sqlite3.Row
            self._db.execute('PRAGMA journal_mode=WAL')
            self._init_tables()
        return self._db

    def _init_tables(self):
        db = self._db
        db.executescript("""
            -- ═══ 行业知识库 (Layer 1) ═══
            CREATE TABLE IF NOT EXISTS sector_knowledge (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id     TEXT NOT NULL,
                sector      TEXT NOT NULL,
                viewpoint   TEXT,
                logic_chain TEXT,        -- JSON array
                sentiment   TEXT DEFAULT 'neutral',
                key_data    TEXT,        -- JSON array
                created_at  TEXT,
                inserted_at TEXT NOT NULL DEFAULT (datetime('now')),
                raw_hash    TEXT,
                UNIQUE(feed_id, sector)
            );

            -- ═══ 通用知识库 (Layer 2) ═══
            CREATE TABLE IF NOT EXISTS general_knowledge (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id     TEXT NOT NULL UNIQUE,
                info_type   TEXT NOT NULL,   -- pre_market/intraday/post_market/research
                summary     TEXT,
                market_overview TEXT,
                key_insights TEXT,           -- JSON array
                risk_warnings TEXT,          -- JSON array
                stock_mentions TEXT,         -- JSON array of {name,code,sentiment,reason}
                created_at  TEXT,
                inserted_at TEXT NOT NULL DEFAULT (datetime('now')),
                raw_hash    TEXT
            );

            -- ═══ 每日复盘索引 (Organization A) ═══
            CREATE TABLE IF NOT EXISTS daily_review (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date  TEXT NOT NULL,   -- YYYY-MM-DD
                feed_id     TEXT NOT NULL,
                info_type   TEXT,
                summary     TEXT,
                sectors     TEXT,            -- JSON array of sector names
                inserted_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(trade_date, feed_id)
            );

            -- ═══ 知识快照 (回测支持) ═══
            CREATE TABLE IF NOT EXISTS knowledge_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                snap_date   TEXT NOT NULL,   -- 快照日期
                snap_type   TEXT NOT NULL DEFAULT 'daily',  -- daily/manual
                sector_json TEXT,            -- 行业知识库快照 (精简)
                general_json TEXT,           -- 通用知识库快照 (精简)
                feed_count  INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(snap_date, snap_type)
            );

            -- ═══ 索引 ═══
            CREATE INDEX IF NOT EXISTS idx_sk_sector ON sector_knowledge(sector);
            CREATE INDEX IF NOT EXISTS idx_sk_created ON sector_knowledge(created_at);
            CREATE INDEX IF NOT EXISTS idx_gk_type ON general_knowledge(info_type);
            CREATE INDEX IF NOT EXISTS idx_gk_created ON general_knowledge(created_at);
            CREATE INDEX IF NOT EXISTS idx_dr_date ON daily_review(trade_date);
        """)
        db.commit()

    # ── 写入 ─────────────────────────────────────────────

    def save(self, knowledge: StructuredKnowledge) -> bool:
        """保存结构化知识到双层知识库。

        Returns:
            True=新增, False=更新(内容未变)
        """
        db = self._get_db()

        # 检查是否已存在且内容未变
        existing = db.execute(
            'SELECT raw_hash FROM general_knowledge WHERE feed_id = ?',
            (knowledge.feed_id,)
        ).fetchone()

        if existing and existing['raw_hash'] == knowledge.raw_text_hash:
            return False  # 内容未变，跳过

        # 1. 写入通用知识库
        stock_mentions = [
            {'name': sm.name, 'code': sm.code, 'sentiment': sm.sentiment, 'reason': sm.reason}
            for sm in knowledge.stock_mentions
        ]
        if existing:
            db.execute("""
                UPDATE general_knowledge SET
                    info_type=?, summary=?, market_overview=?,
                    key_insights=?, risk_warnings=?, stock_mentions=?,
                    raw_hash=?, inserted_at=datetime('now')
                WHERE feed_id=?
            """, (
                knowledge.info_type, knowledge.summary, knowledge.market_overview,
                json.dumps(knowledge.key_insights, ensure_ascii=False),
                json.dumps(knowledge.risk_warnings, ensure_ascii=False),
                json.dumps(stock_mentions, ensure_ascii=False),
                knowledge.raw_text_hash,
                knowledge.feed_id,
            ))
        else:
            db.execute("""
                INSERT INTO general_knowledge
                    (feed_id, info_type, summary, market_overview,
                     key_insights, risk_warnings, stock_mentions,
                     created_at, raw_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                knowledge.feed_id, knowledge.info_type, knowledge.summary,
                knowledge.market_overview,
                json.dumps(knowledge.key_insights, ensure_ascii=False),
                json.dumps(knowledge.risk_warnings, ensure_ascii=False),
                json.dumps(stock_mentions, ensure_ascii=False),
                knowledge.created_at or datetime.now().isoformat(),
                knowledge.raw_text_hash,
            ))

        # 2. 写入行业知识库
        for sv in knowledge.sector_views:
            db.execute("""
                INSERT OR REPLACE INTO sector_knowledge
                    (feed_id, sector, viewpoint, logic_chain, sentiment, key_data, created_at, raw_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                knowledge.feed_id, sv.sector, sv.viewpoint,
                json.dumps(sv.logic_chain, ensure_ascii=False),
                sv.sentiment,
                json.dumps(sv.key_data, ensure_ascii=False),
                knowledge.created_at or datetime.now().isoformat(),
                knowledge.raw_text_hash,
            ))

        # 3. 写入每日复盘索引
        trade_date = self._extract_trade_date(knowledge.created_at, knowledge.info_type)
        if trade_date:
            sectors = [sv.sector for sv in knowledge.sector_views]
            db.execute("""
                INSERT OR REPLACE INTO daily_review
                    (trade_date, feed_id, info_type, summary, sectors)
                VALUES (?, ?, ?, ?, ?)
            """, (
                trade_date, knowledge.feed_id, knowledge.info_type,
                knowledge.summary,
                json.dumps(sectors, ensure_ascii=False),
            ))

        db.commit()
        return True

    def save_batch(self, knowledges: List[StructuredKnowledge]) -> Dict[str, int]:
        """批量保存。"""
        new = 0
        updated = 0
        for k in knowledges:
            if self.save(k):
                new += 1
            else:
                updated += 1
        return {'new': new, 'updated': updated}

    # ── 快照 (回测支持) ──────────────────────────────────

    def create_snapshot(self, snap_date: str, snap_type: str = 'daily') -> int:
        """创建知识库快照。

        Args:
            snap_date: 快照日期 (YYYY-MM-DD)
            snap_type: daily / manual

        Returns:
            snapshot id
        """
        db = self._get_db()

        # 精简快照: 只取 snap_date 及之前的知识
        sector_rows = db.execute("""
            SELECT sector, viewpoint, sentiment, key_data, created_at
            FROM sector_knowledge
            WHERE created_at <= ? || ' 23:59:59'
            ORDER BY created_at DESC
        """, (snap_date,)).fetchall()

        general_rows = db.execute("""
            SELECT feed_id, info_type, summary, key_insights, risk_warnings, created_at
            FROM general_knowledge
            WHERE created_at <= ? || ' 23:59:59'
            ORDER BY created_at DESC
        """, (snap_date,)).fetchall()

        sector_json = json.dumps(
            [dict(r) for r in sector_rows], ensure_ascii=False
        )
        general_json = json.dumps(
            [dict(r) for r in general_rows], ensure_ascii=False
        )
        feed_count = len(general_rows)

        db.execute("""
            INSERT OR REPLACE INTO knowledge_snapshots
                (snap_date, snap_type, sector_json, general_json, feed_count)
            VALUES (?, ?, ?, ?, ?)
        """, (snap_date, snap_type, sector_json, general_json, feed_count))
        db.commit()

        row = db.execute(
            'SELECT id FROM knowledge_snapshots WHERE snap_date=? AND snap_type=?',
            (snap_date, snap_type)
        ).fetchone()
        return row['id']

    def get_snapshot(self, snap_date: str, snap_type: str = 'daily') -> Optional[Dict]:
        """获取快照。"""
        db = self._get_db()
        row = db.execute(
            'SELECT * FROM knowledge_snapshots WHERE snap_date=? AND snap_type=?',
            (snap_date, snap_type)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result['sector_json'] = json.loads(result['sector_json']) if result['sector_json'] else []
        result['general_json'] = json.loads(result['general_json']) if result['general_json'] else []
        return result

    # ── 查询 ─────────────────────────────────────────────

    def query_by_sector(self, sector: str, days: int = 30) -> List[Dict]:
        """按行业查询知识 (行业知识库)。"""
        db = self._get_db()
        rows = db.execute("""
            SELECT sk.*, gk.summary, gk.info_type
            FROM sector_knowledge sk
            LEFT JOIN general_knowledge gk ON sk.feed_id = gk.feed_id
            WHERE sk.sector = ?
              AND sk.created_at >= datetime('now', ?)
            ORDER BY sk.created_at DESC
        """, (sector, f'-{days} days')).fetchall()
        return [dict(r) for r in rows]

    def query_by_date(self, trade_date: str) -> List[Dict]:
        """按日期查询每日复盘 (Organization A)。"""
        db = self._get_db()
        rows = db.execute("""
            SELECT dr.*, gk.market_overview, gk.key_insights, gk.risk_warnings, gk.stock_mentions
            FROM daily_review dr
            LEFT JOIN general_knowledge gk ON dr.feed_id = gk.feed_id
            WHERE dr.trade_date = ?
            ORDER BY dr.info_type
        """, (trade_date,)).fetchall()
        return [dict(r) for r in rows]

    def query_by_type(self, info_type: str, days: int = 30) -> List[Dict]:
        """按信息类型查询 (Organization C)。"""
        db = self._get_db()
        rows = db.execute("""
            SELECT * FROM general_knowledge
            WHERE info_type = ?
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
        """, (info_type, f'-{days} days')).fetchall()
        return [dict(r) for r in rows]

    def query_by_stock(self, stock_name: str, days: int = 30) -> List[Dict]:
        """按个股名称查询相关知识。"""
        db = self._get_db()
        rows = db.execute("""
            SELECT * FROM general_knowledge
            WHERE stock_mentions LIKE ?
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
        """, (f'%{stock_name}%', f'-{days} days')).fetchall()
        return [dict(r) for r in rows]

    def get_all_sectors(self) -> List[str]:
        """获取所有有知识的行业。"""
        db = self._get_db()
        rows = db.execute(
            'SELECT DISTINCT sector FROM sector_knowledge ORDER BY sector'
        ).fetchall()
        return [r['sector'] for r in rows]

    def get_date_range(self) -> Dict[str, Optional[str]]:
        """获取知识库的日期范围。"""
        db = self._get_db()
        row = db.execute(
            'SELECT MIN(created_at) as min_date, MAX(created_at) as max_date FROM general_knowledge'
        ).fetchone()
        return {'min_date': row['min_date'], 'max_date': row['max_date']}

    def stats(self) -> Dict:
        """获取知识库统计信息。"""
        db = self._get_db()
        gk_count = db.execute('SELECT COUNT(*) as c FROM general_knowledge').fetchone()['c']
        sk_count = db.execute('SELECT COUNT(*) as c FROM sector_knowledge').fetchone()['c']
        dr_count = db.execute('SELECT COUNT(*) as c FROM daily_review').fetchone()['c']
        snap_count = db.execute('SELECT COUNT(*) as c FROM knowledge_snapshots').fetchone()['c']
        sectors = self.get_all_sectors()
        date_range = self.get_date_range()
        return {
            'general_count': gk_count,
            'sector_count': sk_count,
            'daily_review_count': dr_count,
            'snapshot_count': snap_count,
            'sectors': sectors,
            'date_range': date_range,
        }

    # ── 辅助 ─────────────────────────────────────────────

    @staticmethod
    def _extract_trade_date(created_at: str, info_type: str) -> Optional[str]:
        """从 created_at 提取交易日期。

        盘前/盘中信息归属当日，盘后/研报归属当日。
        """
        if not created_at:
            return None
        # 尝试解析 ISO 格式
        try:
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            return dt.strftime('%Y-%m-%d')
        except (ValueError, AttributeError):
            pass
        # 尝试常见格式
        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d']:
            try:
                dt = datetime.strptime(created_at[:19], fmt)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
        return None

    def close(self):
        if self._db:
            self._db.close()
            self._db = None
