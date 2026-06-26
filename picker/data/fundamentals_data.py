"""Tushare 真实财报拉取模块 — 为 fundamentals 生成提供准确的财务数据。

解决的问题: 原 _gen_top500_fundamentals.py 的财务数据全靠 LLM 回忆, GLM-5.2 会取错
财报期或记错数字 (实测营收从真实153亿被改成107亿)。本模块直接调 Tushare 的
fina_indicator / income / cashflow 接口取最近年报, 返回 financial_health.key_metrics
格式的 dict, 由生成流程直接填入 JSON, 保证财务数字 100% 准确。

依赖: tushare (已装), TUSHARE_TOKEN (.env 已配置)
失败处理: Tushare 限频/无数据时返回 None, 生成流程回退让 LLM 填 (退化到现状)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from picker import paths

logger = logging.getLogger(__name__)

# Tushare 单例 (避免重复 set_token)
_PRO = None


def _get_pro_api():
    """懒加载 Tushare pro_api, 从 .env 读 token。"""
    global _PRO
    if _PRO is not None:
        return _PRO
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        # 尝试从 .env 读
        env_path = os.path.join(paths.PROJECT_ROOT, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "TUSHARE_TOKEN":
                            token = v.strip()
                            os.environ["TUSHARE_TOKEN"] = token
                            break
    if not token:
        logger.warning("无 TUSHARE_TOKEN, 无法拉取真实财报")
        return None
    try:
        import tushare as ts
        ts.set_token(token)
        _PRO = ts.pro_api()
        return _PRO
    except Exception as e:
        logger.warning(f"Tushare 初始化失败: {type(e).__name__}: {e}")
        return None


def _code_to_ts_code(code: str) -> str:
    """6位代码 → Tushare ts_code。

    60xxxx→.SH(沪) / 00xxxx+30xxxx→.SZ(深) / 8xxxxx+4xxxxx→.BJ(北交所)。
    修复: 原 8/4 开头默认 .SZ 导致北交所股 (如 839725 惠丰钻石) Tushare 查不到 → 全信源缺失。
    """
    code = str(code).strip().zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _safe_float(val, default=None) -> Optional[float]:
    """安全转 float, None/NaN → default。"""
    if val is None:
        return default
    try:
        f = float(val)
        import math
        if math.isnan(f) or math.isinf(f):
            return default
        return round(f, 4)
    except (TypeError, ValueError):
        return default


def fetch_real_financials(code: str, max_retries: int = 2) -> Optional[dict]:
    """调 Tushare 取最近年报财报, 返回 financial_health.key_metrics 格式 dict。

    字段映射:
      revenue_yi      ← income.total_revenue / 1e8  (亿元)
      net_profit_yi   ← income.n_income_attr_p / 1e8
      gross_margin_pct← fina_indicator.grossprofit_margin
      net_margin_pct  ← fina_indicator.netprofit_margin
      roe_pct         ← fina_indicator.roe
      debt_ratio_pct  ← fina_indicator.debt_to_assets
      operating_cf_yi ← cashflow.n_cashflow_act / 1e8
      eps             ← fina_indicator.eps

    返回 None 表示拉取失败, 调用方应回退让 LLM 填。
    """
    pro = _get_pro_api()
    if pro is None:
        return None

    ts_code = _code_to_ts_code(code)

    # Tushare pro_api 的 HTTP 层本身有 30s timeout (requests.post(timeout=30)),
    # 但历史上有 TCP 假死导致的更长卡顿。主线程额外用 SIGALRM 加 20s 硬超时兜底。
    # 关键: signal 只能在主线程用。原代码用 getsignal() 探测不可靠 (getsignal 在子线程
    # 不抛异常), 导致子线程里仍调用 signal() → ValueError: signal only works in main thread,
    # 使并行刷新 (--workers>1) 的 Tushare 财报全部失败。改用 threading.main_thread() 精确判断:
    # 主线程→SIGALRM 硬超时; 子线程→不设超时, 依赖 requests 自身的 30s timeout (足够安全)。
    import signal as _signal
    import threading as _threading
    class _TushareTimeout(Exception): pass
    _can_signal = (hasattr(_signal, "SIGALRM")
                   and _threading.current_thread() is _threading.main_thread())

    def _query_with_retry(fn, *args, **kwargs):
        # Tushare pro_api 的方法是 functools.partial 包装, 无 __name__, 兜底取底层函数名
        fn_name = getattr(fn, "__name__", None) or getattr(getattr(fn, "func", None), "__name__", "tushare_api")
        for attempt in range(max_retries + 1):
            try:
                if _can_signal:
                    old_handler = _signal.signal(_signal.SIGALRM,
                                                 lambda *_: (_ for _ in ()).throw(_TushareTimeout()))
                    _signal.alarm(20)  # 单次查询 20s 硬超时
                try:
                    df = fn(*args, **kwargs)
                finally:
                    if _can_signal:
                        _signal.alarm(0)
                        _signal.signal(_signal.SIGALRM, old_handler)
                return df
            except _TushareTimeout:
                logger.warning(f"Tushare {fn_name}({ts_code}) 超时(20s), attempt {attempt+1}/{max_retries+1}")
                if attempt < max_retries:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    return None
            except Exception as e:
                if _can_signal:
                    _signal.alarm(0)
                if attempt < max_retries:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    logger.warning(f"Tushare {fn_name}({ts_code}) 失败: {type(e).__name__}: {e}")
                    return None

    try:
        # 1. 财务指标 (毛利率/净利率/ROE/负债率/EPS)
        fina = _query_with_retry(
            pro.fina_indicator, ts_code=ts_code, limit=4
        )
        # 2. 利润表 (营收/净利) — 取最近年报
        income = _query_with_retry(
            pro.income, ts_code=ts_code, period="", limit=8
        )
        # 3. 现金流 (经营现金流)
        cashflow = _query_with_retry(
            pro.cashflow, ts_code=ts_code, period="", limit=8
        )

        if fina is None or len(fina) == 0:
            logger.warning(f"Tushare fina_indicator 无数据: {ts_code}")
            return None

        # 找最近年报 (end_date 以 1231 结尾)
        fina_year = fina[fina["end_date"].astype(str).str.endswith("1231")]
        fina_row = fina_year.iloc[0] if len(fina_year) > 0 else fina.iloc[0]
        ann_period = str(fina_row.get("end_date", ""))

        result = {
            "revenue_yi": None,
            "net_profit_yi": None,
            "gross_margin_pct": _safe_float(fina_row.get("grossprofit_margin")),
            "net_margin_pct": _safe_float(fina_row.get("netprofit_margin")),
            "roe_pct": _safe_float(fina_row.get("roe")),
            "debt_ratio_pct": _safe_float(fina_row.get("debt_to_assets")),
            "operating_cf_yi": None,
            "eps": _safe_float(fina_row.get("eps")),
            "rd_expense_yi": None,
            "rd_ratio_pct": None,
            "cf_to_profit": None,
        }

        # 营收/净利 (用同 period 的 income)
        if income is not None and len(income) > 0:
            inc_match = income[income["end_date"].astype(str) == ann_period]
            if len(inc_match) == 0:
                inc_match = income[income["end_date"].astype(str).str.endswith("1231")]
            if len(inc_match) > 0:
                inc_row = inc_match.iloc[0]
                rev = _safe_float(inc_row.get("total_revenue"))
                npft = _safe_float(inc_row.get("n_income_attr_p"))
                result["revenue_yi"] = round(rev / 1e8, 2) if rev is not None else None
                result["net_profit_yi"] = round(npft / 1e8, 2) if npft is not None else None
                # 研发费用 (income.rd_exp, 单位元) → 研发费用率 rd_ratio_pct
                rd_exp = _safe_float(inc_row.get("rd_exp"))
                if rd_exp is not None:
                    result["rd_expense_yi"] = round(rd_exp / 1e8, 2)
                    if rev is not None and rev != 0:
                        result["rd_ratio_pct"] = round(rd_exp / rev * 100, 2)

        # 经营现金流
        if cashflow is not None and len(cashflow) > 0:
            cf_match = cashflow[cashflow["end_date"].astype(str) == ann_period]
            if len(cf_match) == 0:
                cf_match = cashflow[cashflow["end_date"].astype(str).str.endswith("1231")]
            if len(cf_match) > 0:
                cf_row = cf_match.iloc[0]
                cf_val = _safe_float(cf_row.get("n_cashflow_act"))
                if cf_val is not None:
                    result["operating_cf_yi"] = round(cf_val / 1e8, 2)
                # cf_to_profit = 经营CF / 净利
                if result["operating_cf_yi"] is not None and result["net_profit_yi"]:
                    result["cf_to_profit"] = round(result["operating_cf_yi"] / result["net_profit_yi"], 2)

        result["_ann_period"] = ann_period  # 标注财报期, 供 prompt 展示
        logger.info(f"Tushare {ts_code} 财报({ann_period}): 营收{result['revenue_yi']}亿 净利{result['net_profit_yi']}亿")
        return result

    except Exception as e:
        logger.warning(f"fetch_real_financials({code}) 异常: {type(e).__name__}: {e}")
        return None


if __name__ == "__main__":
    # 快速自测
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "603228"
    r = fetch_real_financials(code)
    if r:
        print(f"\n{code} 真实财报 ({r.get('_ann_period')}):")
        for k, v in r.items():
            if not k.startswith("_"):
                print(f"  {k}: {v}")
    else:
        print(f"{code} 拉取失败")
