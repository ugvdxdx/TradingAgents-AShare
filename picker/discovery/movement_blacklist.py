"""异动黑名单 + 冷却期机制。

把"概念炒作 / 错误归因"的股票从异动体系踢出, 冷却期内阻止其再次进入异动扫描、
归因缓存、以及 fundamentals 生成时的异动注入。

三处主防线生效点 (冷却期内拦截):
  - scan_mispriced.scan_price_momentum:  不进 gems (不会被归因 / 板块扩散保送)
  - attribution.precompute_pool_attribution:  每日维护不绕过 scan 直接写回顾因缓存
  - refresh_fundamentals.refresh_one:  fundamentals 生成时不注入 surge_section
    (满足"fundamentals 全量更新不受错误归因影响")

设计:
  - 单一缓存 movement_blacklist.json, entry 带 expires_at, 到期自动解除
  - 进程级缓存 (按 mtime 失效) 避免循环内反复读盘
  - 人工拉黑 (区别于 scripts/experiment_blacklist.py 的自动规则——后者回测证明错杀反转股, 不接入)

为避免循环依赖: 对 attribution 的引用一律用函数内局部 import
(attribution.precompute 也会反向局部 import 本模块)。
"""
from __future__ import annotations

import os
import json
from datetime import datetime, timedelta

from picker import paths

BLACKLIST_PATH = paths.MOVEMENT_BLACKLIST_PATH
DEFAULT_DAYS = 30

# 进程级缓存: (mtime, codes) —— 按文件 mtime 失效, 避免循环内反复读盘
_CACHE: tuple = (0.0, set())


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_raw() -> dict:
    """读原始黑名单 dict (含过期项, 未做剔除)。"""
    if not os.path.exists(BLACKLIST_PATH):
        return {}
    try:
        with open(BLACKLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(BLACKLIST_PATH), exist_ok=True)
    with open(BLACKLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _prune_expired(data: dict) -> tuple:
    """剔除过期项, 返回 (新data, 被剔除的code列表)。"""
    today = _today()
    expired = [c for c, e in data.items()
               if (e.get("expires_at") or "2000-01-01") < today]
    for c in expired:
        data.pop(c, None)
    return data, expired


def load_blacklist() -> set:
    """返回有效(未过期)黑名单 code 集合。带进程级缓存 (按 mtime 失效)。
    首次 load 自动剔除过期项并回写。"""
    global _CACHE
    try:
        mtime = os.path.getmtime(BLACKLIST_PATH) if os.path.exists(BLACKLIST_PATH) else 0.0
    except OSError:
        mtime = 0.0
    if mtime == _CACHE[0]:
        return _CACHE[1]
    data = _read_raw()
    data, expired = _prune_expired(data)
    if expired:
        _write_raw(data)
        print(f"[blacklist] {len(expired)} 只冷却到期已解除: {expired}")
    codes = set(data.keys())
    _CACHE = (mtime, codes)
    return codes


def load_blacklist_detail() -> dict:
    """完整 entry dict (展示前先剔除过期项), 供 CLI 列表展示。"""
    data, _ = _prune_expired(_read_raw())
    return data


def is_blacklisted(code: str) -> bool:
    """单点判定, 走进程缓存, O(1)。scan/refresh 循环内安全调用。"""
    return code in load_blacklist()


def _invalidate_cache() -> None:
    """写盘后调用, 强制下次 load 重读。"""
    global _CACHE
    _CACHE = (0.0, set())


def _get_name(code: str) -> str:
    """复用 attribution._get_stock_name 拿名称 (局部 import 避免循环依赖)。"""
    try:
        from picker.discovery.attribution import _get_stock_name
        return _get_stock_name(code) or code
    except Exception:
        return code


def add_to_blacklist(code: str, reason: str = "", days: int = DEFAULT_DAYS,
                     reason_type: str = "", sector_tag: str = "") -> dict:
    """加/更新条目 (已存在则覆盖 = 续期)。自动算 expires_at = today + days。"""
    data = _read_raw()
    entry = {
        "name": _get_name(code),
        "reason": reason,
        "reason_type": reason_type,
        "sector_tag": sector_tag,
        "blacklisted_date": _today(),
        "expires_at": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"),
        "days": days,
    }
    data[code] = entry
    _write_raw(data)
    _invalidate_cache()
    return entry


def add_many_to_blacklist(items, days: int = DEFAULT_DAYS,
                          reason_type: str = "", sector_tag_map: dict = None) -> list:
    """批量加。items=[(code, reason), ...]; sector_tag_map={code: tag} 可选。返回写入 code 列表。"""
    sector_tag_map = sector_tag_map or {}
    codes = []
    for code, reason in items:
        add_to_blacklist(code, reason=reason, days=days,
                         reason_type=reason_type,
                         sector_tag=sector_tag_map.get(code, ""))
        codes.append(code)
    return codes


def blacklist_by_reason_type(reason_type: str, reason: str, days: int = DEFAULT_DAYS) -> list:
    """扫归因缓存, 把所有 reason_type==给定值 的 code 批量拉黑 (拉"全部概念炒作"的入口)。
    返回拉黑的 code 列表。"""
    from picker.discovery.attribution import _load_attr_cache
    cache = _load_attr_cache()
    targets = [(c, e) for c, e in cache.items() if e.get("reason_type") == reason_type]
    items = [(c, reason) for c, _ in targets]
    tag_map = {c: e.get("sector_tag", "") for c, e in targets}
    return add_many_to_blacklist(items, days=days, reason_type=reason_type, sector_tag_map=tag_map)


def purge_from_attr_cache(codes: list) -> int:
    """从 mispriced_attribution_cache.json 删除这些 code 的归因条目。
    复用 attribution._load_attr_cache/_save_attr_cache (写法参考 scan_mispriced.py 删 V3 cache)。"""
    if not codes:
        return 0
    from picker.discovery.attribution import _load_attr_cache, _save_attr_cache
    cache = _load_attr_cache()
    n = 0
    for c in codes:
        if cache.pop(c, None) is not None:
            n += 1
    if n:
        _save_attr_cache(cache)
    return n
