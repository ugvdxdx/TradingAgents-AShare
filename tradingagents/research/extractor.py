"""L3 — 知识提取层: LLM 结构化提取。

职责:
  - 从清洗后的帖子文本中提取结构化知识
  - 提取维度: 行业观点/个股提及/逻辑链条/情绪倾向/关键数据
  - 输出 StructuredKnowledge 结构

使用:
  from tradingagents.research.extractor import KnowledgeExtractor
  ext = KnowledgeExtractor()
  knowledge = ext.extract(cleaned_feed)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 确保独立运行时也能读到 .env
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass


@dataclass
class StockMention:
    """个股提及。"""
    name: str                    # 公司名称
    code: str = ''               # 股票代码 (如果能推断)
    context: str = ''            # 提及上下文
    sentiment: str = 'neutral'   # bullish / bearish / neutral
    reason: str = ''             # 看多/看空理由


@dataclass
class SectorView:
    """行业观点。"""
    sector: str                  # 行业名称
    viewpoint: str               # 核心观点
    logic_chain: List[str] = field(default_factory=list)  # 逻辑链条
    sentiment: str = 'neutral'   # bullish / bearish / neutral
    key_data: List[str] = field(default_factory=list)     # 关键数据点


@dataclass
class StructuredKnowledge:
    """结构化知识 — 知识提取层的输出。"""
    feed_id: str
    info_type: str               # pre_market / intraday / post_market / research
    summary: str                 # 一句话摘要
    market_overview: str = ''    # 市场概况 (大盘/成交/情绪)
    sector_views: List[SectorView] = field(default_factory=list)
    stock_mentions: List[StockMention] = field(default_factory=list)
    key_insights: List[str] = field(default_factory=list)   # 核心洞察
    risk_warnings: List[str] = field(default_factory=list)  # 风险提示
    raw_text_hash: str = ''      # 原文哈希 (用于变更检测)


EXTRACT_PROMPT = """你是一个金融研报知识提取专家。请从以下帖子文本中提取结构化知识。

## 输入文本
标题: {title}
类型: {info_type}
正文:
{text}

## 提取要求
请严格按以下 JSON 格式输出，不要添加任何其他内容:

```json
{{
  "summary": "一句话摘要(30字以内)",
  "market_overview": "市场概况(大盘走势/成交额/情绪, 无则留空)",
  "sector_views": [
    {{
      "sector": "行业名称",
      "viewpoint": "核心观点",
      "logic_chain": ["逻辑1", "逻辑2"],
      "sentiment": "bullish/bearish/neutral",
      "key_data": ["关键数据1"]
    }}
  ],
  "stock_mentions": [
    {{
      "name": "公司名称",
      "code": "股票代码(6位数字,不确定则留空)",
      "context": "提及上下文(30字)",
      "sentiment": "bullish/bearish/neutral",
      "reason": "看多/看空理由"
    }}
  ],
  "key_insights": ["核心洞察1", "核心洞察2"],
  "risk_warnings": ["风险提示1"]
}}
```

注意:
1. sector 名称从以下选取: AI芯片, 光通信, 半导体设备, 半导体材料, 封装, AI应用, 存储, PCB, 新能源, 汽车, 医药, 消费, 其他
2. stock_mentions 只提取明确提及的公司，不要推测
3. sentiment 严格三选一: bullish/bearish/neutral
4. key_insights 提取最有价值的投资逻辑，不超过5条
5. 如果文本不含某类信息，对应字段留空数组或空字符串"""


class KnowledgeExtractor:
    """LLM 驱动的知识提取器。"""

    def __init__(self, llm_helper=None, openai_api_key: str = '', openai_base_url: str = '', openai_model: str = ''):
        """
        Args:
            llm_helper: 可选的 LLMHelper 实例。为 None 则自动创建。
            openai_api_key: OpenAI 兼容 API Key (如 DeepSeek), 优先于 llm_helper
            openai_base_url: API Base URL
            openai_model: 模型名称
        """
        self._llm = llm_helper
        self._openai_api_key = openai_api_key or os.getenv('OPENAI_API_KEY', '')
        self._openai_base_url = openai_base_url or os.getenv('OPENAI_BASE_URL', '')
        self._openai_model = openai_model or os.getenv('OPENAI_MODEL', 'deepseek-chat')
        self._openai_client = None

    def _get_openai_client(self):
        """懒加载 OpenAI 兼容客户端。"""
        if self._openai_client is None and self._openai_api_key:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI(
                    api_key=self._openai_api_key,
                    base_url=self._openai_base_url or None,
                )
            except ImportError:
                pass
        return self._openai_client

    def _get_llm(self):
        """懒加载 LLM。"""
        if self._llm is None:
            from tradingagents.agents.picker.llm_helper import LLMHelper
            self._llm = LLMHelper()
        return self._llm

    def extract(self, cleaned_feed) -> StructuredKnowledge:
        """从清洗后的帖子提取结构化知识。

        Args:
            cleaned_feed: CleanedFeed 实例 (来自 cleaner 层)
        """
        text = cleaned_feed.text
        title = cleaned_feed.title
        info_type = cleaned_feed.info_type.value if hasattr(cleaned_feed.info_type, 'value') else str(cleaned_feed.info_type)

        # 短文本直接用规则提取，不调 LLM
        if len(text) < 50:
            return self._rule_based_extract(cleaned_feed)

        # 调用 LLM 提取
        prompt = EXTRACT_PROMPT.format(
            title=title or '(无标题)',
            info_type=info_type,
            text=text[:4000],  # 限制长度避免超 token
        )

        try:
            llm = self._get_llm()
            response = llm.call(
                system_msg='你是金融研报知识提取专家。请严格按要求的JSON格式输出，不要添加任何其他内容。',
                human_msg=prompt,
                deep=False,
            )
            parsed = self._parse_llm_response(response)
            if not parsed:
                import logging
                logging.getLogger(__name__).warning(f'LLM 返回解析为空, raw response[:500]: {response[:500]}')
                parsed = self._rule_based_parse(text, title, info_type)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'LLM 调用失败: {e}, 回退到规则提取')
            parsed = self._rule_based_parse(text, title, info_type)

        # 计算原文哈希
        import hashlib
        raw_hash = hashlib.md5(text.encode()).hexdigest()[:12]

        return StructuredKnowledge(
            feed_id=cleaned_feed.feed_id,
            info_type=info_type,
            summary=parsed.get('summary', ''),
            market_overview=parsed.get('market_overview', ''),
            sector_views=[SectorView(**sv) for sv in parsed.get('sector_views', [])],
            stock_mentions=[StockMention(**sm) for sm in parsed.get('stock_mentions', [])],
            key_insights=parsed.get('key_insights', []),
            risk_warnings=parsed.get('risk_warnings', []),
            raw_text_hash=raw_hash,
        )

    def extract_batch(self, cleaned_feeds) -> List[StructuredKnowledge]:
        """批量提取。"""
        return [self.extract(cf) for cf in cleaned_feeds]

    # ── 解析辅助 ─────────────────────────────────────────

    @staticmethod
    def _parse_llm_response(response: str) -> Dict:
        """解析 LLM 返回的 JSON。"""
        # 尝试提取 ```json ... ``` 块
        m = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            # 尝试直接解析
            text = response

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试修复常见 JSON 问题
            text = re.sub(r',\s*}', '}', text)
            text = re.sub(r',\s*]', ']', text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}

    @staticmethod
    def _rule_based_extract(cleaned_feed) -> StructuredKnowledge:
        """规则提取 (LLM 不可用时的回退方案)。"""
        text = cleaned_feed.text
        title = cleaned_feed.title
        info_type = cleaned_feed.info_type.value if hasattr(cleaned_feed.info_type, 'value') else str(cleaned_feed.info_type)

        parsed = KnowledgeExtractor._rule_based_parse(text, title, info_type)

        import hashlib
        raw_hash = hashlib.md5(text.encode()).hexdigest()[:12]

        return StructuredKnowledge(
            feed_id=cleaned_feed.feed_id,
            info_type=info_type,
            summary=parsed.get('summary', title or text[:30]),
            market_overview=parsed.get('market_overview', ''),
            sector_views=[SectorView(**sv) for sv in parsed.get('sector_views', [])],
            stock_mentions=[StockMention(**sm) for sm in parsed.get('stock_mentions', [])],
            key_insights=parsed.get('key_insights', []),
            risk_warnings=parsed.get('risk_warnings', []),
            raw_text_hash=raw_hash,
        )

    @staticmethod
    def _rule_based_parse(text: str, title: str, info_type: str) -> Dict:
        """基于规则的知识提取。"""
        from tradingagents.research.cleaner import SECTOR_KEYWORDS

        # 摘要
        summary = title or text[:30].replace('\n', ' ')

        # 行业观点
        sector_views = []
        for sector, keywords in SECTOR_KEYWORDS.items():
            matched_kws = [kw for kw in keywords if kw in text]
            if matched_kws:
                # 提取匹配关键词周围的句子作为 viewpoint
                viewpoint = ''
                for kw in matched_kws[:2]:
                    idx = text.find(kw)
                    start = max(0, idx - 20)
                    end = min(len(text), idx + len(kw) + 40)
                    viewpoint += text[start:end].strip() + '；'
                sector_views.append({
                    'sector': sector,
                    'viewpoint': viewpoint.strip('；')[:100],
                    'logic_chain': [],
                    'sentiment': 'neutral',
                    'key_data': matched_kws[:3],
                })

        return {
            'summary': summary,
            'market_overview': '',
            'sector_views': sector_views,
            'stock_mentions': [],
            'key_insights': [],
            'risk_warnings': [],
        }
