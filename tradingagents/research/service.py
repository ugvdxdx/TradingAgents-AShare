"""L5 — 知识服务层: 流水线编排 + 统计 + 回测入口。

职责:
  - 统一入口: 串联采集→清洗→提取→存储的全流水线
  - 回测接口: 支持按时间点回溯知识状态

注意:
  选股系统的知识检索走 consumer.py (函数式接口), 本层不重复提供检索能力。
  早先版本的 query_for_debate / get_daily_review / backtest_compare 等方法
  经全库确认零引用, 已移除以消除两套并存的检索逻辑。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .collector import ResearchCollector
from .cleaner import ResearchCleaner
from .extractor import KnowledgeExtractor
from .store import KnowledgeStore


class ResearchService:
    """研报知识系统统一服务层 (流水线编排)。"""

    def __init__(
        self,
        db_path: str = 'research.db',
        cookie: str = '',
        llm_helper=None,
    ):
        self.db_path = db_path
        self.cookie = cookie
        self.collector = ResearchCollector(db_path=db_path)
        self.cleaner = ResearchCleaner()
        self.extractor = KnowledgeExtractor(llm_helper=llm_helper)
        self.store = KnowledgeStore(db_path=db_path)

    # ── 全流水线 ─────────────────────────────────────────

    def run_pipeline(
        self,
        cookie: str = '',
        incremental: bool = True,
        extract: bool = True,
        date_from: str = '',
        date_to: str = '',
    ) -> Dict:
        """运行完整流水线: 采集 → 清洗 → 提取 → 存储。

        Args:
            cookie: 浏览器 Cookie (为空则使用初始化时的 cookie)
            incremental: True=增量采集, False=全量
            extract: True=对新数据执行 LLM 提取, False=仅采集
            date_from: 起始日期 (YYYY-MM-DD)
            date_to: 结束日期 (YYYY-MM-DD)

        Returns:
            流水线执行结果统计 (含 cookie_expired 标志)
        """
        cookie = cookie or self.cookie
        if not cookie:
            return {'error': 'Cookie 未提供'}

        # 1. 采集
        collect_result = self.collector.collect(
            cookie=cookie, incremental=incremental,
            date_from=date_from, date_to=date_to,
        )

        # 2. 清洗 + 提取 + 存储
        extract_result = {'new': 0, 'updated': 0, 'failed': 0}
        if extract:
            unprocessed = self.collector.get_unprocessed(limit=100)
            if unprocessed:
                failed_ids = []
                cleaned = self.cleaner.clean_batch(unprocessed)
                knowledges = self.extractor.extract_batch(cleaned)
                # 设置 created_at
                for k, c in zip(knowledges, cleaned):
                    k.created_at = c.created_at
                # 存储 (逐条, 隔离失败)
                saved_ids = []
                for k, c in zip(knowledges, cleaned):
                    try:
                        self.store.save(k)
                        saved_ids.append(c.feed_id)
                    except Exception:
                        failed_ids.append(c.feed_id)
                extract_result = {
                    'new': len(saved_ids),
                    'updated': 0,
                    'failed': len(failed_ids),
                }
                # 成功的标记为已处理, 失败的标记为失败态 (可重试)
                if saved_ids:
                    self.collector.mark_processed(saved_ids)
                if failed_ids:
                    self.collector.mark_failed(failed_ids)

        return {
            'collect': collect_result,
            'extract': extract_result,
        }

    # ── 回测支持 ─────────────────────────────────────────

    def get_knowledge_at(self, date: str) -> Dict:
        """获取指定时间点的知识状态 (用于回测)。

        先检查是否有快照, 没有则实时查询 (走 store 公开接口)。

        Args:
            date: 日期 (YYYY-MM-DD)

        Returns:
            该时间点的完整知识状态
        """
        # 优先使用快照
        snap = self.store.get_snapshot(date)
        if snap:
            return {
                'date': date,
                'source': 'snapshot',
                'sector_knowledge': snap['sector_json'],
                'general_knowledge': snap['general_json'],
                'feed_count': snap['feed_count'],
            }

        # 回退到实时查询 (公开接口, 不再访问 _get_db)
        result = self.store.query_knowledge_until(date)
        return {
            'date': date,
            'source': 'live_query',
            'sector_knowledge': result['sector_knowledge'],
            'general_knowledge': result['general_knowledge'],
            'feed_count': result['feed_count'],
        }

    # ── 统计 ─────────────────────────────────────────────

    def stats(self) -> Dict:
        """获取系统统计信息。"""
        return self.store.stats()

    # ── 生命周期 ─────────────────────────────────────────

    def close(self):
        self.collector.close()
        self.store.close()
