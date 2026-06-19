#!/usr/bin/env python3
"""
资金流分析模块 —— 东方财富 + Tushare 双数据源

数据源:
  1. 东方财富 push2his.eastmoney.com (实时，免费但不稳定)
  2. Tushare moneyflow 接口 (付费，稳定，需 TUSHARE_TOKEN)

优先级: 东方财富优先 → Tushare fallback
东方财富不可用时自动切换到 Tushare。

字段:
  - 主力净流入   = 超大单 + 大单  (单笔>20万)
  - 超大单净流入  单笔≥100万 (机构)
  - 大单净流入    单笔20~100万 (游资/大户)
  - 中单净流入    单笔4~20万
  - 小单净流入    单笔<4万 (散户)
  - 主力净占比%   主力净额 / 成交额

信号提取:
  1. 主力连续流入天数
  2. 近5日主力累计净流入/市值比
  3. 主力 vs 散户背离度
  4. 超大单占比 (机构参与度)
  5. 近5日主力净占比均值 & 趋势
"""

import json
import logging
import os
import pickle
import random
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# API 配置
# ────────────────────────────────────────────────

# 数据源: 东方财富 (实时)
_EM_BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

# 数据源: Tushare (付费，稳定)
_TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
_TUSHARE_PRO = None  # 懒加载 pro_api

TIMEOUT = 15

# 全局开关：启动时探测，要么全部参与，要么全部不参与
_ENABLED: Optional[bool] = None  # None=未检测, True=可用, False=不可用
_ACTIVE_SOURCE: Optional[str] = None  # "eastmoney" / "tushare"
_CACHE: Dict[str, dict] = {}

# 磁盘缓存目录
from picker import paths as _paths
_DISK_CACHE_DIR = _paths.MF_CACHE_DIR


def _disk_cache_path() -> str:
    """当天缓存文件路径，隔天自动失效"""
    os.makedirs(_DISK_CACHE_DIR, exist_ok=True)
    return os.path.join(_DISK_CACHE_DIR, f"mf_{date.today().isoformat()}.pkl")


def _load_disk_cache() -> Dict[str, dict]:
    """加载当天磁盘缓存"""
    p = _disk_cache_path()
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    return {}


def _save_disk_cache():
    """将内存缓存持久化到磁盘"""
    p = _disk_cache_path()
    try:
        with open(p, "wb") as f:
            pickle.dump(_CACHE, f)
    except Exception:
        pass

def _get_proxies() -> Optional[dict]:
    """从环境变量读取代理配置，支持 Clash/V2Ray/Surge 等常见工具"""
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "")
        if val:
            return {"https": val, "http": val}
    return None


# 随机 User-Agent 池，降低被识别为爬虫的概率
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# 进程级复用 session，减少 TCP 握手
_SHARED_SESSION: Optional[requests.Session] = None
_LAST_REQUEST_TIME: float = 0
_MIN_INTERVAL: float = 0.35  # 最小请求间隔（秒）


def _get_session() -> requests.Session:
    """获取复用的 session，随机 UA"""
    global _SHARED_SESSION
    if _SHARED_SESSION is not None:
        return _SHARED_SESSION
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(_UA_POOL),
        "Referer": "https://data.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    s.verify = False
    proxies = _get_proxies()
    if proxies:
        s.proxies.update(proxies)
    adapter = HTTPAdapter(
        max_retries=Retry(total=2, backoff_factor=1.0, status_forcelist=[502, 503, 429]),
        pool_connections=2, pool_maxsize=2,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _SHARED_SESSION = s
    return s


def _rate_limit():
    """请求间限速，模拟人类浏览节奏"""
    global _LAST_REQUEST_TIME
    elapsed = time.time() - _LAST_REQUEST_TIME
    if elapsed < _MIN_INTERVAL:
        jitter = random.uniform(0.05, 0.25)  # 随机抖动
        time.sleep(_MIN_INTERVAL - elapsed + jitter)
    _LAST_REQUEST_TIME = time.time()


def _stock_secid(code: str) -> str:
    """东方财富 secid: 0.000001(SZ) / 1.600519(SH)"""
    if code.startswith(('6', '68')):
        return f"1.{code}"
    return f"0.{code}"


def fetch_fund_flow(code: str, days: int = 60) -> Optional[List[dict]]:
    """
    获取个股历史资金流向。自动选择可用数据源。
    返回: [{date, main_net, super_large, large, medium, small, main_pct}, ...]
    """
    global _ENABLED

    cache_key = f"{code}_{days}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # 尝试从磁盘缓存加载（即使 API 不可用也能用缓存）
    if not _CACHE:
        _CACHE.update(_load_disk_cache())
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # API 不可用且无缓存，返回 None
    if _ENABLED is False:
        return None

    # 根据活跃数据源拉取，优先用已探测成功的源
    result = None
    if _ACTIVE_SOURCE == "tushare":
        result = _fetch_from_tushare(code, days)
    elif _ACTIVE_SOURCE == "eastmoney":
        result = _fetch_from_eastmoney(code, days)
        # 东方财富失败时 fallback 到 Tushare
        if result is None and _TUSHARE_TOKEN:
            result = _fetch_from_tushare(code, days)
    else:
        # 未探测过时，东方财富优先 → Tushare fallback
        result = _fetch_from_eastmoney(code, days)
        if result is None and _TUSHARE_TOKEN:
            result = _fetch_from_tushare(code, days)

    _CACHE[cache_key] = result
    # 不缓存 None 值（拉取失败不写入缓存，下次可重试其他数据源）
    if result is not None:
        _CACHE[cache_key] = result
    return result


def _fetch_from_eastmoney(code: str, days: int = 60) -> Optional[List[dict]]:
    """从东方财富拉取资金流数据"""
    params = {
        "lmt": str(days),
        "klt": "101",
        "secid": _stock_secid(code),
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    try:
        _rate_limit()
        s = _get_session()
        resp = s.get(_EM_BASE_URL, params=params, timeout=TIMEOUT)
        data = resp.json()
        if data.get("rc") != 0 or not data.get("data"):
            return None
        klines = data["data"].get("klines", [])
        if not klines:
            return None

        result = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            result.append({
                "date": parts[0],
                "main_net": float(parts[1]),
                "super_large": float(parts[2]),
                "large": float(parts[3]),
                "medium": float(parts[4]),
                "small": float(parts[5]),
                "main_pct": float(parts[6]),
            })
        return result
    except Exception:
        return None


def _get_tushare_pro():
    """懒加载 Tushare pro_api，避免未安装 tushare 时启动报错"""
    global _TUSHARE_PRO
    if _TUSHARE_PRO is not None:
        return _TUSHARE_PRO
    if not _TUSHARE_TOKEN:
        return None
    try:
        import tushare as ts
        _TUSHARE_PRO = ts.pro_api(_TUSHARE_TOKEN)
        logger.info("Tushare pro_api 初始化成功")
        return _TUSHARE_PRO
    except ImportError:
        logger.warning("tushare 未安装，请运行: pip install tushare")
        return None
    except Exception as e:
        logger.warning(f"Tushare 初始化失败: {e}")
        return None


def _tushare_ts_code(code: str) -> str:
    """
    将纯数字代码转为 Tushare ts_code 格式。
    600519 → 600519.SH, 000001 → 000001.SZ, 300308 → 300308.SZ
    """
    if code.startswith(('6', '68')):
        return f"{code}.SH"
    elif code.startswith(('0', '3', '30')):
        return f"{code}.SZ"
    # 未知格式，直接加 .SZ
    return f"{code}.SZ"


def _fetch_from_tushare(code: str, days: int = 60) -> Optional[List[dict]]:
    """
    从 Tushare 拉取个股资金流数据。
    Tushare moneyflow 接口字段：
      - net_mf_amount: 净流入额(万元)
      - buy_elg_amount / sell_elg_amount: 特大单(万元)
      - buy_lg_amount / sell_lg_amount: 大单(万元)
      - buy_md_amount / sell_md_amount: 中单(万元)
      - buy_sm_amount / sell_sm_amount: 小单(万元)
    """
    pro = _get_tushare_pro()
    if pro is None:
        return None

    ts_code = _tushare_ts_code(code)
    end_date = date.today().strftime("%Y%m%d")
    # 往前推 days 天，多取一些确保够（含非交易日）
    from datetime import timedelta
    start_date = (date.today() - timedelta(days=days + 30)).strftime("%Y%m%d")

    try:
        _rate_limit()
        df = pro.moneyflow(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )
        if df is None or df.empty:
            return None

        result = []
        for _, row in df.iterrows():
            # Tushare 字段单位是万元，转成元（与东方财富格式一致）
            net_mf_amount_wan = (row.get("net_mf_amount", 0) or 0)  # 总净流入额(万元)
            super_large_wan = (row.get("buy_elg_amount", 0) or 0) - (row.get("sell_elg_amount", 0) or 0)  # 特大单净额
            large_wan = (row.get("buy_lg_amount", 0) or 0) - (row.get("sell_lg_amount", 0) or 0)          # 大单净额
            medium_wan = (row.get("buy_md_amount", 0) or 0) - (row.get("sell_md_amount", 0) or 0)         # 中单净额
            small_wan = (row.get("buy_sm_amount", 0) or 0) - (row.get("sell_sm_amount", 0) or 0)          # 小单净额

            # 主力 = 超大单 + 大单 (与东方财富定义一致)
            main_force_wan = super_large_wan + large_wan

            # 万元 → 元
            main_net = float(main_force_wan * 1e4)
            super_large = float(super_large_wan * 1e4)
            large = float(large_wan * 1e4)
            medium = float(medium_wan * 1e4)
            small = float(small_wan * 1e4)

            # main_pct = 主力净额 / 成交额 (估算成交额 = 所有买+卖金额之和)
            buy_total_wan = (row.get("buy_elg_amount", 0) or 0) + (row.get("buy_lg_amount", 0) or 0) + (row.get("buy_md_amount", 0) or 0) + (row.get("buy_sm_amount", 0) or 0)
            sell_total_wan = (row.get("sell_elg_amount", 0) or 0) + (row.get("sell_lg_amount", 0) or 0) + (row.get("sell_md_amount", 0) or 0) + (row.get("sell_sm_amount", 0) or 0)
            turnover_wan = buy_total_wan + sell_total_wan  # 成交额估算(万元)
            main_pct = (main_force_wan / turnover_wan * 100) if turnover_wan > 0 else 0.0

            result.append({
                "date": str(row["trade_date"]),
                "main_net": float(main_net),
                "super_large": float(super_large),
                "large": float(large),
                "medium": float(medium),
                "small": float(small),
                "main_pct": float(main_pct),
            })

        # 按日期升序排列（Tushare 默认降序）
        result.sort(key=lambda x: x["date"])
        # 只取最近 days 条
        if len(result) > days:
            result = result[-days:]

        return result if result else None
    except Exception as e:
        logger.warning(f"Tushare moneyflow {ts_code} 拉取失败: {e}")
        return None


# ────────────────────────────────────────────────
# 信号提取
# ────────────────────────────────────────────────

@dataclass
class MoneyFlowScore:
    """资金流中期信号 —— 仅做尾部风险过滤，不加分"""
    multiplier: float = 1.0            # 固定 1.0，不做乘性调节
    consecutive_out: int = 0           # 主力连续流出天数（用于硬过滤）


def compute_money_flow_score(
    code: str,
    market_cap_yi: float = 0,
    days: int = 60,
    close_prices: list = None,
    cutoff_date: str = None,
) -> MoneyFlowScore:
    """
    资金流尾部风险过滤。只返回 consecutive_out，不做评分。

    用途：回测/选股阶段硬过滤连续流出 ≥10 天的股票。
    """
    flows = fetch_fund_flow(code, days)
    if not flows or len(flows) < 10:
        return MoneyFlowScore()

    if cutoff_date:
        cutoff_clean = cutoff_date.replace("-", "")
        flows = [f for f in flows if f["date"].replace("-", "") <= cutoff_clean]
        if len(flows) < 10:
            return MoneyFlowScore()

    result = MoneyFlowScore()
    for f in reversed(flows):
        if f["main_net"] < 0:
            result.consecutive_out += 1
        else:
            break

    return result


def get_fund_flow_summary(code: str, market_cap_yi: float = 0) -> str:
    """获取资金流尾部风险摘要"""
    s = compute_money_flow_score(code, market_cap_yi)
    if s.consecutive_out == 0:
        return "无资金流数据"

    lines = [f"主力连续流出: {s.consecutive_out}日"]
    if s.consecutive_out >= 10:
        lines.append("[风险] 连续流出 ≥10日，硬过滤")
    elif s.consecutive_out >= 5:
        lines.append("[注意] 连续流出 ≥5日")
    return "  ".join(lines)


# ────────────────────────────────────────────────
# 全局探测 & 批量预拉（要么全参与，要么全不参与）
# ────────────────────────────────────────────────

_PROBE_STOCKS = [  # 不同市场的代表性股票 secid
    "0.300308",   # 中际旭创 (创业板)
    "1.688111",   # 金山办公 (科创板)
    "0.000001",   # 平安银行 (主板)
]

def probe_availability() -> bool:
    """
    启动时探测数据源可用性（东方财富优先，Tushare fallback）。
    成功则启用，失败则全局禁用。
    """
    global _ENABLED, _ACTIVE_SOURCE
    if _ENABLED is not None:
        return _ENABLED

    # 1. 先探测东方财富（免费但不稳定）
    em_success = 0
    for secid in _PROBE_STOCKS:
        try:
            _rate_limit()
            s = _get_session()
            params = {
                "lmt": "3", "klt": "101", "secid": secid,
                "fields1": "f1,f2,f3",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }
            resp = s.get(_EM_BASE_URL, params=params, timeout=TIMEOUT)
            data = resp.json()
            if data.get("rc") == 0 and data.get("data", {}).get("klines"):
                em_success += 1
        except Exception:
            pass
        time.sleep(0.3)

    if em_success >= 2:
        _ENABLED = True
        _ACTIVE_SOURCE = "eastmoney"
        logger.info(f"资金流 API 探测成功 - 东方财富 ({em_success}/3)，模块启用")
        return True

    # 2. 东方财富不可用，探测 Tushare（付费但稳定）
    if _TUSHARE_TOKEN:
        pro = _get_tushare_pro()
        if pro is not None:
            try:
                df = pro.moneyflow(ts_code="000001.SZ", start_date=date.today().strftime("%Y%m%d"), end_date=date.today().strftime("%Y%m%d"))
                if df is not None and not df.empty:
                    _ENABLED = True
                    _ACTIVE_SOURCE = "tushare"
                    logger.info("资金流 API 探测成功 - Tushare，模块启用")
                    return True
                # 可能今天还没数据，试前一天
                from datetime import timedelta
                yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
                df = pro.moneyflow(ts_code="000001.SZ", start_date=yesterday, end_date=yesterday)
                if df is not None and not df.empty:
                    _ENABLED = True
                    _ACTIVE_SOURCE = "tushare"
                    logger.info("资金流 API 探测成功 - Tushare，模块启用")
                    return True
            except Exception as e:
                logger.warning(f"Tushare 探测失败: {e}")

    _ENABLED = False
    _ACTIVE_SOURCE = None
    logger.warning("资金流 API 探测失败（东方财富+Tushare均不可用），已全局禁用")
    return False


def is_enabled() -> bool:
    """返回资金流模块是否已启用（供外部判断）"""
    return _ENABLED is True


def prefetch_batch(codes: List[str], days: int = 60):
    """
    预拉一批股票的资金流数据到缓存。
    探测失败则不拉取任何数据。
    内置限速：每请求间隔 0.35s+随机抖动，每 50 只额外休息 2s。
    """
    if not probe_availability():
        return

    success = 0
    fail = 0
    for i, code in enumerate(codes):
        if fail >= 3:  # 拉取过程中出现 3 次连续失败，全局禁用
            logger.warning(f"资金流批量拉取连续失败，全局禁用 (已成功{success})")
            global _ENABLED
            _ENABLED = False
            _CACHE.clear()
            return
        try:
            fetch_fund_flow(code, days)
            success += 1
            fail = 0
        except Exception:
            fail += 1
        # 每 50 只额外休息，给服务器喘息
        if i > 0 and i % 50 == 0:
            time.sleep(2.0)
            # 每 200 只增量保存一次，防止中途挂掉丢数据
            if i % 200 == 0:
                _save_disk_cache()
                logger.info(f"资金流预取进度: {i}/{len(codes)} (已保存)")

    logger.info(f"资金流预取完成: {success}成功/{len(codes)}总计")
    _save_disk_cache()
