"""L1 — 数据采集层: 小鹅通圈子爬虫 + 增量更新。

职责:
  - 从小鹅通圈子 API 拉取帖子列表
  - 基于时间戳的增量更新 (只拉取 last_fetch_time 之后的新帖)
  - 原始数据落盘 (raw_feeds 表)
  - 更新日志记录 (update_log 表)

使用:
  from tradingagents.research.collector import ResearchCollector
  collector = ResearchCollector(db_path='research.db')
  new_count = collector.collect(cookie='...')
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

# 默认配置
DEFAULT_API_URL = (
    'https://quanzi.xiaoe-tech.com/xe.community.community_service/'
    'small_community/xe.community/get_feeds_list/1.1.0'
)
DEFAULT_APP_ID = 'appv5zuapfz7716'
DEFAULT_COMMUNITY_ID = 'c_62a95f0db904a_yYyOAuyh3445'


class ResearchCollector:
    """小鹅通圈子数据采集器，支持增量更新。"""

    def __init__(
        self,
        db_path: str = 'research.db',
        api_url: str = DEFAULT_API_URL,
        app_id: str = DEFAULT_APP_ID,
        community_id: str = DEFAULT_COMMUNITY_ID,
    ):
        self.db_path = db_path
        self.api_url = api_url
        self.app_id = app_id
        self.community_id = community_id
        self._db = None

    # ── DB 操作 ──────────────────────────────────────────

    def _get_db(self):
        """懒加载 SQLite 连接。"""
        if self._db is None:
            import sqlite3
            self._db = sqlite3.connect(self.db_path)
            self._db.row_factory = sqlite3.Row
            self._init_tables()
        return self._db

    def _init_tables(self):
        """初始化数据库表。"""
        db = self._db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS raw_feeds (
                feed_id     TEXT PRIMARY KEY,
                community_id TEXT NOT NULL,
                author_id   TEXT,
                author_name TEXT,
                title       TEXT,
                content     TEXT,        -- 原始 content JSON
                text        TEXT,        -- 提取的纯文本
                created_at  TEXT,        -- 帖子发布时间
                fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                is_processed INTEGER DEFAULT 0,
                version     INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS update_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at      TEXT NOT NULL DEFAULT (datetime('now')),
                new_count   INTEGER DEFAULT 0,
                update_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                last_feed_id TEXT,
                last_created_at TEXT,
                detail      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_raw_feeds_created
                ON raw_feeds(created_at);
            CREATE INDEX IF NOT EXISTS idx_raw_feeds_processed
                ON raw_feeds(is_processed);
        """)
        db.commit()

    # ── API 请求 ─────────────────────────────────────────

    def _fetch_page(
        self,
        cookie: str,
        cursor: str = '',
        page_size: int = 10,
    ) -> Dict:
        """请求单页数据 (cursor 分页)。

        Args:
            cookie: 浏览器 Cookie
            cursor: 上一页返回的 cursor, 为空则请求第一页
            page_size: 每页条数
        """
        headers = {
            'accept': 'application/json, text/plain, */*',
            'agent-type': 'pc',
            'app_id': self.app_id,
            'login_app_id': self.app_id,
            'referer': f'https://quanzi.xiaoe-tech.com/{self.community_id}/feed_list?app_id={self.app_id}',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
            'x-b3-flags': '1',
            'x-b3-sampled': '1',
            'Cookie': cookie,
        }
        params = {
            'app_id': self.app_id,
            'community_id': self.community_id,
            'feeds_list_type': '-1',
            'order_filed': 'created_at',
            'hide_exercise': '1',
            'page_size': str(page_size),
        }
        if cursor:
            params['cursor'] = cursor
        resp = requests.get(self.api_url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _get_last_fetch_time(self) -> Optional[str]:
        """获取上次成功采集的最新帖子时间。"""
        db = self._get_db()
        row = db.execute(
            'SELECT last_created_at FROM update_log '
            'WHERE error_count = 0 ORDER BY run_at DESC LIMIT 1'
        ).fetchone()
        return row['last_created_at'] if row else None

    # ── 核心采集逻辑 ─────────────────────────────────────

    def collect(
        self,
        cookie: str,
        max_pages: int = 500,
        incremental: bool = True,
        date_from: str = '',
        date_to: str = '',
    ) -> Dict:
        """采集帖子数据，支持增量更新和日期范围过滤。

        Args:
            cookie: 浏览器登录 Cookie
            max_pages: 最大翻页数
            incremental: True=只拉取新帖, False=全量拉取
            date_from: 起始日期 (YYYY-MM-DD), 为空则不限制
            date_to: 结束日期 (YYYY-MM-DD), 为空则不限制

        Returns:
            {'new': int, 'updated': int, 'errors': int}
        """
        db = self._get_db()
        last_time = self._get_last_fetch_time() if incremental else None
        new_count = 0
        update_count = 0
        error_count = 0
        last_feed_id = None
        last_created_at = None
        empty_streak = 0
        date_from_dt = date_from + ' 00:00:00' if date_from else ''
        date_to_dt = date_to + ' 23:59:59' if date_to else ''
        cursor = ''  # cursor 分页

        for page_num in range(1, max_pages + 1):
            try:
                data = self._fetch_page(cookie, cursor=cursor, page_size=10)
            except Exception as e:
                error_count += 1
                print(f'  [Collector] 请求失败 page={page_num}: {e}')
                if error_count >= 5:
                    break
                time.sleep(1)
                continue

            code = data.get('code', -1)
            if code != 0:
                error_count += 1
                msg = data.get('msg', '')
                print(f'  [Collector] API错误 page={page_num}: code={code} msg={msg}')
                if code == 23:
                    print('  [Collector] Cookie已过期，请重新获取')
                    break
                time.sleep(1)
                continue

            feeds = data.get('data', {}).get('list', [])
            next_cursor = data.get('data', {}).get('cursor', '')

            if not feeds and not next_cursor:
                # 没有 feeds 也没有 cursor，说明已到末尾
                break

            stop = False
            for feed in feeds:
                feed_id = feed.get('id', '')
                created_at = feed.get('created_at', '')

                # 日期范围过滤: 早于 date_from 则停止翻页 (列表按时间倒序)
                if date_from_dt and created_at and created_at < date_from_dt:
                    stop = True
                    break

                # 日期范围过滤: 晚于 date_to 则跳过
                if date_to_dt and created_at and created_at > date_to_dt:
                    continue

                # 增量: 遇到已采集的帖子就停止
                if last_time and created_at and created_at <= last_time:
                    stop = True
                    break

                # 检查是否已存在
                existing = db.execute(
                    'SELECT feed_id, version FROM raw_feeds WHERE feed_id = ?',
                    (feed_id,)
                ).fetchone()

                # 提取文本
                content = feed.get('content', {})
                text = ''
                if isinstance(content, dict):
                    text = (content.get('text', '') or '')
                elif isinstance(content, list) and content:
                    text = str(content[0])

                if existing:
                    # 更新已有记录
                    db.execute("""
                        UPDATE raw_feeds SET
                            content = ?, text = ?, updated_at = datetime('now'),
                            version = version + 1
                        WHERE feed_id = ?
                    """, (json.dumps(content, ensure_ascii=False), text, feed_id))
                    update_count += 1
                else:
                    # 插入新记录
                    db.execute("""
                        INSERT INTO raw_feeds
                            (feed_id, community_id, author_id, author_name,
                             title, content, text, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        feed_id,
                        self.community_id,
                        feed.get('author', {}).get('id', '') if isinstance(feed.get('author'), dict) else '',
                        feed.get('author', {}).get('nickname', '') if isinstance(feed.get('author'), dict) else str(feed.get('author', '')),
                        feed.get('title', ''),
                        json.dumps(content, ensure_ascii=False),
                        text,
                        created_at,
                    ))
                    new_count += 1

                if not last_created_at or (created_at and created_at > last_created_at):
                    last_created_at = created_at
                    last_feed_id = feed_id

            db.commit()

            if stop:
                break

            # 使用 cursor 继续翻页
            if next_cursor:
                cursor = next_cursor
            else:
                break  # 没有 cursor 说明已到最后一页

            # 进度输出
            if page_num % 5 == 0:
                print(f'  [Collector] page={page_num}, new={new_count}, updated={update_count}, latest={last_created_at}')

            time.sleep(0.2)

        # 记录更新日志
        db.execute("""
            INSERT INTO update_log (new_count, update_count, error_count, last_feed_id, last_created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (new_count, update_count, error_count, last_feed_id, last_created_at))
        db.commit()

        return {
            'new': new_count,
            'updated': update_count,
            'errors': error_count,
            'last_created_at': last_created_at,
        }

    def get_unprocessed(self, limit: int = 100) -> List[Dict]:
        """获取未处理的帖子。"""
        db = self._get_db()
        rows = db.execute("""
            SELECT feed_id, community_id, author_name, title, text, created_at
            FROM raw_feeds WHERE is_processed = 0
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, feed_ids: List[str]):
        """标记帖子为已处理。"""
        db = self._get_db()
        db.executemany(
            'UPDATE raw_feeds SET is_processed = 1 WHERE feed_id = ?',
            [(fid,) for fid in feed_ids]
        )
        db.commit()

    def close(self):
        if self._db:
            self._db.close()
            self._db = None
