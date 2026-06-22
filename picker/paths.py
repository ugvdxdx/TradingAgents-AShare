"""统一路径解析层 —— picker 包所有文件路径的唯一真相源。

设计目标:
  - 无论从哪个模块 import, 都解析到同一个项目根目录;
  - 彻底替代散落在各脚本里的 ``ROOT = os.path.dirname(os.path.abspath(__file__))``
    和 tradingagents/agents/picker 里脆弱的 4 层 dirname 回溯;
  - 历史缓存从根目录点前缀文件 (``.fundamental_v3_scores.json``) 迁移到
    ``data/caches/`` (去点前缀), 旧脚本经此模块统一引用新路径。

约定: 本模块只做"路径", 不做 IO, 不依赖项目内其它模块, 保证可在任意上下文
被安全 import。
"""
from __future__ import annotations

import os

# ══════════════════════════════════════════════════════════
# 项目根目录
# ══════════════════════════════════════════════════════════
# picker/paths.py 位于 <PROJECT_ROOT>/picker/paths.py, 上溯一层即根。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════
# 数据目录
# ══════════════════════════════════════════════════════════
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CACHES_DIR = os.path.join(DATA_DIR, "caches")          # 原 .xxx.json 点前缀缓存
WHITELIST_DIR = os.path.join(DATA_DIR, "whitelist")
REFERENCE_DIR = os.path.join(DATA_DIR, "reference")

# 运行时大缓存目录 (原位保留在项目根, 仅路径收口到此)
FUNDAMENTALS_DIR = os.path.join(PROJECT_ROOT, "fundamentals")
COLD_FUNDAMENTALS_DIR = os.path.join(PROJECT_ROOT, "cold_fundamentals")  # 冷股池

# 兼容别名：历史代码中引用的 FUNDAMENTALS_COLD_DIR（原指向不存在的 fundamentals_cold/）
# 统一为 COLD_FUNDAMENTALS_DIR（实际冷股目录为 cold_fundamentals/）
FUNDAMENTALS_COLD_DIR = COLD_FUNDAMENTALS_DIR
KLINE_CACHE_DIR = os.path.join(PROJECT_ROOT, "kline_cache")
MF_CACHE_DIR = os.path.join(PROJECT_ROOT, ".mf_cache")
# 行业资金流历史 (由 mf.pkl 个股资金流按 fundamentals 行业汇总, 逐日时间序列)
BOARD_FLOW_HISTORY = os.path.join(MF_CACHE_DIR, "board_flow_history.pkl")

# 原位 data/ 下的运行时缓存 (news 由 picker 包 4 层 dirname 引用,
# 同时也供 tradingagents/agents/picker 使用, 保持 data/ 下不动)
# 行业资金流历史见上方 BOARD_FLOW_HISTORY (.mf_cache/board_flow_history.pkl)。
NEWS_CACHE = os.path.join(DATA_DIR, "news_cache.json")


# ══════════════════════════════════════════════════════════
# 数据库
# ══════════════════════════════════════════════════════════
RESEARCH_DB = os.path.join(PROJECT_ROOT, "research.db")


# ══════════════════════════════════════════════════════════
# 点前缀缓存 → data/caches/ (去点前缀)
# ══════════════════════════════════════════════════════════
# 历史: 根目录下 .fundamental_v3_scores.json 等, 现统一至 data/caches/。
# 迁移工具会把旧文件搬过来; 首次访问若新路径缺失但旧路径存在, 自动回落读取旧文件
# (兼容未及时迁移的开发环境)。

def _cache(name: str) -> str:
    """返回 data/caches/<name> 路径, 必要时回落到根目录点前缀旧路径。"""
    new = os.path.join(CACHES_DIR, name)
    if os.path.exists(new):
        return new
    # 兼容回落: 旧路径形如 .<name>
    old = os.path.join(PROJECT_ROOT, "." + name)
    if os.path.exists(old):
        return old
    return new  # 写入时用新路径


V3_CACHE = _cache("fundamental_v3_scores.json")
ATTR_CACHE = _cache("mispriced_attribution_cache.json")
OVERHEATED_CACHE = _cache("overheated_risk_cache.json")
COLD_STOCKS_PATH = _cache("cold_stocks.json")
DEBATE_RESULT_PATH = _cache("debate_result.json")
DEBATE_LOG_PATH = _cache("debate_log.json")
FUNDAMENTAL_LLM_SCORES_PATH = _cache("fundamental_llm_scores.json")
NEED_GENERATE_PATH = _cache("need_generate.json")

# 每日选股快照目录: 每天一份, 含全池分数 + TOP5/10推荐结果 + 理由
# 回测按 cutoff 取最近快照, 消除 chain/delivery 前视偏差
V3_SNAPSHOT_DIR = os.path.join(CACHES_DIR, "v3_snapshots")
V3_FULL_BACKTEST_PATH = _cache("v3_full_backtest.json")
BACKTEST_CORRELATION_PATH = _cache("backtest_correlation.json")
SUB_SECTOR_OVERRIDE_PATH = _cache("sub_sector_override.json")

# 旧名兼容: 部分模块用 LLM_CACHE_FILE 变量名
LLM_CACHE_FILE = FUNDAMENTAL_LLM_SCORES_PATH


# ══════════════════════════════════════════════════════════
# 白名单 / 参考文件
# ══════════════════════════════════════════════════════════
def _ref(dir_: str, name: str) -> str:
    """返回参考目录下路径, 缺失时回落到项目根同名文件 (兼容未迁移环境)。"""
    new = os.path.join(dir_, name)
    if os.path.exists(new):
        return new
    old = os.path.join(PROJECT_ROOT, name)
    if os.path.exists(old):
        return old
    return new


# 白名单 (原位 fallback: 很多代码用 cwd 相对 "stock_whitelist.json")
STOCK_WHITELIST = _ref(WHITELIST_DIR, "stock_whitelist.json")
TOP500_WHITELIST = _ref(WHITELIST_DIR, "top500_whitelist.json")

# 参考文件 (原 _top500_and_leaders.txt / _world_knowledge_2026_06.md 等, 去下划线)
TOP500_AND_LEADERS = _ref(REFERENCE_DIR, "top500_and_leaders.txt")
FUNDAMENTALS_STATUS = _ref(REFERENCE_DIR, "fundamentals_status.txt")
TOP100_SCORES = _ref(REFERENCE_DIR, "top100_scores.txt")
WORLD_KNOWLEDGE_MD = _ref(REFERENCE_DIR, "world_knowledge_2026_06.md")
STOCKS_AUDIT = _ref(REFERENCE_DIR, "stocks_audit.json")
XIAOE_FEED_DATA = _ref(REFERENCE_DIR, "xiaoe_feed_data.json")


def ensure_caches_dir() -> None:
    """确保 caches 目录存在 (写入缓存前调用)。"""
    os.makedirs(CACHES_DIR, exist_ok=True)
