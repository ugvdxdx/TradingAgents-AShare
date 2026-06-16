"""L5 — 知识服务层: API + 检索 + 回测。

职责:
  - 统一入口: 串联采集→清洗→提取→存储的全流水线
  - 知识检索: 为选股系统辩论阶段提供行业/个股知识
  - 回测接口: 支持按时间点回溯知识状态
  - 与选股系统无缝集成

使用:
  from tradingagents.research.service import ResearchService

  # 全流水线运行
  svc = ResearchService(db_path='research.db')
  svc.run_pipeline(cookie='...')

  # 选股系统集成
  knowledge = svc.query_for_debate(sector='光通信', stock_name='中际旭创')
  daily = svc.get_daily_review('2026-06-15')

  # 回测
  snap = svc.get_knowledge_at('2026-06-01')
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .collector import ResearchCollector
from .cleaner import ResearchCleaner, CleanedFeed
from .extractor import KnowledgeExtractor, StructuredKnowledge
from .store import KnowledgeStore


class ResearchService:
    """研报知识系统统一服务层。"""

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
            流水线执行结果统计
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
        extract_result = {'new': 0, 'updated': 0}
        if extract:
            unprocessed = self.collector.get_unprocessed(limit=100)
            if unprocessed:
                # 清洗
                cleaned = self.cleaner.clean_batch(unprocessed)
                # 提取
                knowledges = self.extractor.extract_batch(cleaned)
                # 设置 created_at
                for k, c in zip(knowledges, cleaned):
                    k.created_at = c.created_at
                # 存储
                extract_result = self.store.save_batch(knowledges)
                # 标记已处理
                self.collector.mark_processed([c.feed_id for c in cleaned])

        return {
            'collect': collect_result,
            'extract': extract_result,
        }

    # ── 知识检索 (选股系统集成) ──────────────────────────

    def query_for_debate(
        self,
        sector: str = '',
        stock_name: str = '',
        days: int = 30,
    ) -> Dict:
        """为选股系统辩论阶段提供知识检索。

        这是选股系统的主要集成接口。
        返回精简的结构化知识，直接注入辩论 prompt。

        Args:
            sector: 行业名称 (如 '光通信')
            stock_name: 个股名称 (如 '中际旭创')
            days: 回溯天数

        Returns:
            {
                'sector_knowledge': [...],   # 行业观点
                'stock_knowledge': [...],    # 个股相关
                'recent_insights': [...],    # 近期核心洞察
                'risk_warnings': [...],      # 风险提示
            }
        """
        result = {
            'sector_knowledge': [],
            'stock_knowledge': [],
            'recent_insights': [],
            'risk_warnings': [],
        }

        # 行业知识
        if sector:
            sector_rows = self.store.query_by_sector(sector, days=days)
            result['sector_knowledge'] = [
                {
                    'viewpoint': r['viewpoint'],
                    'sentiment': r['sentiment'],
                    'logic_chain': json.loads(r['logic_chain']) if r.get('logic_chain') else [],
                    'key_data': json.loads(r['key_data']) if r.get('key_data') else [],
                    'date': r.get('created_at', '')[:10],
                }
                for r in sector_rows
            ]

        # 个股知识
        if stock_name:
            stock_rows = self.store.query_by_stock(stock_name, days=days)
            for r in stock_rows:
                mentions = json.loads(r.get('stock_mentions', '[]'))
                matching = [m for m in mentions if m.get('name') == stock_name]
                if matching:
                    result['stock_knowledge'].append({
                        'sentiment': matching[0].get('sentiment', 'neutral'),
                        'reason': matching[0].get('reason', ''),
                        'date': r.get('created_at', '')[:10],
                        'summary': r.get('summary', ''),
                    })

        # 近期核心洞察 (跨行业)
        all_recent = self.store.query_by_type('post_market', days=days) + \
                     self.store.query_by_type('research', days=days)
        seen_insights = set()
        for r in all_recent:
            insights = json.loads(r.get('key_insights', '[]'))
            for ins in insights:
                if ins not in seen_insights:
                    result['recent_insights'].append(ins)
                    seen_insights.add(ins)
            risks = json.loads(r.get('risk_warnings', '[]'))
            for risk in risks:
                if risk not in result['risk_warnings']:
                    result['risk_warnings'].append(risk)

        return result

    def format_knowledge_for_prompt(self, knowledge: Dict) -> str:
        """将知识格式化为可注入辩论 prompt 的文本。

        Args:
            knowledge: query_for_debate() 的返回值

        Returns:
            格式化的文本，可直接拼入 prompt
        """
        parts = []

        if knowledge.get('sector_knowledge'):
            parts.append('【行业观点】')
            for sk in knowledge['sector_knowledge']:
                sentiment_map = {'bullish': '偏多', 'bearish': '偏空', 'neutral': '中性'}
                parts.append(
                    f"  - {sk['viewpoint']} ({sentiment_map.get(sk['sentiment'], sk['sentiment'])}, {sk['date']})"
                )
                for logic in sk.get('logic_chain', [])[:2]:
                    parts.append(f"    逻辑: {logic}")

        if knowledge.get('stock_knowledge'):
            parts.append('【个股观点】')
            for sk in knowledge['stock_knowledge']:
                sentiment_map = {'bullish': '偏多', 'bearish': '偏空', 'neutral': '中性'}
                parts.append(
                    f"  - {sk.get('reason', sk.get('summary', ''))} "
                    f"({sentiment_map.get(sk['sentiment'], sk['sentiment'])}, {sk['date']})"
                )

        if knowledge.get('recent_insights'):
            parts.append('【近期核心洞察】')
            for ins in knowledge['recent_insights'][:5]:
                parts.append(f"  - {ins}")

        if knowledge.get('risk_warnings'):
            parts.append('【风险提示】')
            for risk in knowledge['risk_warnings'][:3]:
                parts.append(f"  - {risk}")

        return '\n'.join(parts) if parts else '(暂无研报知识)'

    # ── 每日复盘 ─────────────────────────────────────────

    def get_daily_review(self, trade_date: str) -> Dict:
        """获取指定日期的复盘知识。

        Args:
            trade_date: 交易日期 (YYYY-MM-DD)

        Returns:
            {
                'trade_date': '2026-06-15',
                'market_overview': '...',
                'sector_views': [...],
                'stock_mentions': [...],
                'key_insights': [...],
                'risk_warnings': [...],
            }
        """
        rows = self.store.query_by_date(trade_date)
        if not rows:
            return {'trade_date': trade_date, 'message': '无该日数据'}

        result = {
            'trade_date': trade_date,
            'market_overview': '',
            'sector_views': [],
            'stock_mentions': [],
            'key_insights': [],
            'risk_warnings': [],
        }

        for r in rows:
            if r.get('market_overview'):
                result['market_overview'] = r['market_overview']
            if r.get('sectors'):
                result['sector_views'].extend(json.loads(r['sectors']))
            if r.get('stock_mentions'):
                result['stock_mentions'].extend(json.loads(r['stock_mentions']))
            if r.get('key_insights'):
                result['key_insights'].extend(json.loads(r['key_insights']))
            if r.get('risk_warnings'):
                result['risk_warnings'].extend(json.loads(r['risk_warnings']))

        # 去重
        result['sector_views'] = list(dict.fromkeys(result['sector_views']))
        result['key_insights'] = list(dict.fromkeys(result['key_insights']))
        result['risk_warnings'] = list(dict.fromkeys(result['risk_warnings']))

        return result

    # ── 回测支持 ─────────────────────────────────────────

    def get_knowledge_at(self, date: str) -> Dict:
        """获取指定时间点的知识状态 (用于回测)。

        先检查是否有快照，没有则实时查询。

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

        # 回退到实时查询
        sector_rows = self.store.query_by_sector.__wrapped__ if hasattr(self.store.query_by_sector, '__wrapped__') else []
        # 直接查 DB
        db = self.store._get_db()
        general_rows = db.execute("""
            SELECT * FROM general_knowledge
            WHERE created_at <= ? || ' 23:59:59'
            ORDER BY created_at DESC
        """, (date,)).fetchall()

        sector_rows = db.execute("""
            SELECT * FROM sector_knowledge
            WHERE created_at <= ? || ' 23:59:59'
            ORDER BY created_at DESC
        """, (date,)).fetchall()

        return {
            'date': date,
            'source': 'live_query',
            'sector_knowledge': [dict(r) for r in sector_rows],
            'general_knowledge': [dict(r) for r in general_rows],
            'feed_count': len(general_rows),
        }

    def create_daily_snapshot(self, date: str = '') -> int:
        """创建每日知识快照。"""
        date = date or datetime.now().strftime('%Y-%m-%d')
        return self.store.create_snapshot(date, snap_type='daily')

    def backtest_compare(
        self,
        date: str,
        actual_results: List[Dict],
    ) -> Dict:
        """回测对比: 将历史知识状态与实际结果对比。

        Args:
            date: 回测日期
            actual_results: 实际结果列表
                [{'code': '300308', 'name': '中际旭创', 'return_pct': 5.2}, ...]

        Returns:
            对比分析结果
        """
        knowledge = self.get_knowledge_at(date)
        general = knowledge.get('general_knowledge', [])

        # 分析知识覆盖度
        covered_stocks = set()
        for g in general:
            mentions = json.loads(g.get('stock_mentions', '[]'))
            for m in mentions:
                if m.get('code'):
                    covered_stocks.add(m['code'])

        # 分析行业覆盖
        covered_sectors = set()
        for g in general:
            if isinstance(g, dict):
                sectors = g.get('sectors', '')
                if sectors:
                    try:
                        covered_sectors.update(json.loads(sectors))
                    except (json.JSONDecodeError, TypeError):
                        pass

        # 对比
        hit = 0
        miss = 0
        for r in actual_results:
            if r.get('code') in covered_stocks:
                hit += 1
            else:
                miss += 1

        return {
            'date': date,
            'knowledge_feed_count': knowledge.get('feed_count', 0),
            'covered_stocks': len(covered_stocks),
            'covered_sectors': list(covered_sectors),
            'actual_stocks': len(actual_results),
            'hit_rate': round(hit / max(len(actual_results), 1) * 100, 1),
            'hit': hit,
            'miss': miss,
        }

    # ── 统计 ─────────────────────────────────────────────

    def stats(self) -> Dict:
        """获取系统统计信息。"""
        return self.store.stats()

    # ── 生命周期 ─────────────────────────────────────────

    def close(self):
        self.collector.close()
        self.store.close()
