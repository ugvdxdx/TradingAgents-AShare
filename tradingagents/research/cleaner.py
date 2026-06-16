"""L2 — 数据清洗与标准化层。

职责:
  - 原始文本去噪 (去除 HTML 标签、特殊字符、广告水印)
  - 文本分段 (按主题/段落拆分长文)
  - 信息类型分类 (盘前/盘中/盘后复盘/研报/公告)
  - 行业/板块标签初筛 (基于关键词匹配)
  - 标准化输出 (CleanedFeed 结构)

使用:
  from tradingagents.research.cleaner import ResearchCleaner
  cleaner = ResearchCleaner()
  cleaned = cleaner.clean(raw_feed_dict)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class InfoType(Enum):
    """信息类型分类。"""
    PRE_MARKET = 'pre_market'       # 盘前 (盘前策略/早盘提示)
    INTRADAY = 'intraday'           # 盘中 (盘中异动/实时点评)
    POST_MARKET = 'post_market'     # 盘后复盘 (收盘信息/日复盘)
    RESEARCH = 'research'           # 研报 (行业研报/深度分析)
    ANNOUNCEMENT = 'announcement'   # 公告 (重要公告/政策解读)
    GENERAL = 'general'             # 通用 (无法明确分类)


# ── 关键词映射 ────────────────────────────────────────────

INFO_TYPE_KEYWORDS = {
    InfoType.PRE_MARKET: [
        '盘前', '早盘', '隔夜', '开盘', '早参', '晨报', '盘前策略',
        '开盘提示', '竞价', '夜盘',
    ],
    InfoType.INTRADAY: [
        '盘中', '异动', '拉升', '跳水', '涨停', '跌停', '快速',
        '盘中点评', '实时', '突发',
    ],
    InfoType.POST_MARKET: [
        '收盘', '复盘', '日评', '日复盘', '盘后', '收盘信息',
        '今日市场', '两市', '成交额',
    ],
    InfoType.RESEARCH: [
        '研报', '深度', '专题', '行业', '产业链', '赛道', '技术路线',
        '投资逻辑', '核心观点', '行业分析', '产业趋势',
    ],
    InfoType.ANNOUNCEMENT: [
        '公告', '政策', '监管', '证监会', '发改委', '国务院',
        '新规', '征求意见',
    ],
}

# 行业/板块关键词 (与 ai_knowledge_base.py 对齐)
SECTOR_KEYWORDS = {
    'AI芯片': ['AI芯片', 'GPU', '算力芯片', 'GPU服务器', '英伟达', 'H100', 'B200'],
    '光通信': ['光模块', 'CPO', 'NPO', '光通信', 'MPO', '光纤', '硅光', '光互联'],
    '半导体设备': ['光刻', '刻蚀', '薄膜沉积', '半导体设备', '前道设备', '量测'],
    '半导体材料': ['光刻胶', '靶材', '电子特气', 'CMP', '湿电子化学品'],
    '封装': ['封装', 'CoPoS', 'FOPLP', '先进封装', '面板级封装', 'TGV'],
    'AI应用': ['AI应用', '大模型', 'AIGC', 'Agent', 'AI办公', 'AI编程'],
    '存储': ['存储芯片', 'HBM', 'DDR5', 'NAND', 'DRAM', 'SSD'],
    'PCB': ['PCB', '覆铜板', 'CCL', '载板', '线路板'],
    '新能源': ['光伏', '风电', '储能', '锂电', '充电桩', '氢能'],
    '汽车': ['智能驾驶', '自动驾驶', '车规', '激光雷达', '线控'],
    '医药': ['创新药', 'CXO', '医疗器械', 'GLP', '减肥药'],
    '消费': ['消费复苏', '白酒', '免税', '旅游', '医美'],
}


@dataclass
class CleanedFeed:
    """清洗后的标准化帖子。"""
    feed_id: str
    text: str                                   # 清洗后纯文本
    segments: List[str] = field(default_factory=list)  # 按段落拆分
    info_type: InfoType = InfoType.GENERAL      # 信息类型
    sectors: List[str] = field(default_factory=list)   # 涉及行业
    created_at: str = ''
    author_name: str = ''
    title: str = ''
    word_count: int = 0


class ResearchCleaner:
    """数据清洗与标准化。"""

    def clean(self, raw: Dict) -> CleanedFeed:
        """清洗单条原始帖子。"""
        text = raw.get('text', '') or ''
        feed_id = raw.get('feed_id', '')
        created_at = raw.get('created_at', '') or ''
        author_name = raw.get('author_name', '') or ''
        title = raw.get('title', '') or ''

        # 1. 文本去噪
        text = self._denoise(text)

        # 2. 分段
        segments = self._segment(text)

        # 3. 信息类型分类
        info_type = self._classify_info_type(text, title)

        # 4. 行业标签
        sectors = self._detect_sectors(text)

        return CleanedFeed(
            feed_id=feed_id,
            text=text,
            segments=segments,
            info_type=info_type,
            sectors=sectors,
            created_at=created_at,
            author_name=author_name,
            title=title,
            word_count=len(text),
        )

    def clean_batch(self, raws: List[Dict]) -> List[CleanedFeed]:
        """批量清洗。"""
        return [self.clean(r) for r in raws]

    # ── 内部方法 ─────────────────────────────────────────

    @staticmethod
    def _denoise(text: str) -> str:
        """文本去噪。"""
        # 去除 HTML 标签
        text = re.sub(r'<[^>]+>', '', text)
        # 去除 &nbsp; 等 HTML 实体
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
        # 统一空白
        text = re.sub(r'[ \t]+', ' ', text)
        # 去除行首行尾空白
        lines = [line.strip() for line in text.split('\n')]
        lines = [l for l in lines if l]
        text = '\n'.join(lines)
        # 去除常见广告水印
        text = re.sub(r'更多.*?请关注.*', '', text)
        text = re.sub(r'扫码.*?入群', '', text)
        text = re.sub(r'点击.*?阅读原文', '', text)
        return text.strip()

    @staticmethod
    def _segment(text: str) -> List[str]:
        """按段落拆分，过滤空段。"""
        # 先按换行分段
        raw_segments = text.split('\n')
        # 合并过短的段落 (单行列表项合并到上一段)
        segments = []
        current = ''
        for seg in raw_segments:
            seg = seg.strip()
            if not seg:
                continue
            # 列表项 (以数字. 或 - 开头) 归入上一段
            if re.match(r'^[\d]+[\.、）)]', seg) or seg.startswith('-'):
                current = current + '\n' + seg if current else seg
            else:
                if current:
                    segments.append(current)
                current = seg
        if current:
            segments.append(current)
        return segments

    @staticmethod
    def _classify_info_type(text: str, title: str = '') -> InfoType:
        """基于关键词分类信息类型。"""
        combined = f'{title} {text[:200]}'
        best_type = InfoType.GENERAL
        best_score = 0
        for info_type, keywords in INFO_TYPE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in combined)
            if score > best_score:
                best_score = score
                best_type = info_type
        return best_type

    @staticmethod
    def _detect_sectors(text: str) -> List[str]:
        """基于关键词检测涉及行业。"""
        sectors = []
        for sector, keywords in SECTOR_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                sectors.append(sector)
        return sectors
