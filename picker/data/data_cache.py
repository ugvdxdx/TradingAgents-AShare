#!/usr/bin/env python3
"""
K线数据缓存层 —— 持久化TickFlow日K线，避免重复API请求

使用方式：
    cache = KlineCache("kline_cache")
    df = cache.get("600519.SH")  # 优先读缓存，缓存未命中才调API
    cache.batch_get(symbols)      # 批量获取并缓存
"""

import json
import os
import time
import pickle
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from collections import OrderedDict

import pandas as pd

from picker import paths


class KlineCache:
    """本地K线数据缓存，按symbol存储，支持过期策略"""

    def __init__(self, cache_dir: str = None, expiry_hours: int = 24):
        if cache_dir is None:
            cache_dir = paths.KLINE_CACHE_DIR
        self.cache_dir = cache_dir
        self.expiry_hours = expiry_hours
        os.makedirs(cache_dir, exist_ok=True)
        self._meta_file = os.path.join(cache_dir, "_meta.json")
        self._meta = self._load_meta()

    def _load_meta(self) -> dict:
        if os.path.exists(self._meta_file):
            try:
                with open(self._meta_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save_meta(self):
        with open(self._meta_file, 'w') as f:
            json.dump(self._meta, f, indent=2)

    def _cache_path(self, symbol: str) -> str:
        safe_name = symbol.replace('.', '_')
        return os.path.join(self.cache_dir, f"{safe_name}.pkl")

    def get(self, symbol: str) -> Optional[pd.DataFrame]:
        """获取单只股票的缓存K线。

        过期策略已作废: 历史 K 线是只增不改的事实数据, 24h 过期会导致
        backfill 补采的长 K 线被判过期 → 重新拉 90 根覆盖 → 白补。
        现在只要文件存在就返回 (缓存优先), 需要强制刷新时直接删 pkl。
        """
        path = self._cache_path(symbol)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_pickle(path)
            return df
        except:
            return None

    def put(self, symbol: str, df: pd.DataFrame):
        """缓存单只股票的K线数据"""
        path = self._cache_path(symbol)
        df.to_pickle(path)
        self._meta[symbol] = {
            'cached_at': time.time(),
            'rows': len(df),
            'last_date': str(df.index[-1]) if len(df) > 0 else '',
        }
        self._save_meta()

    def batch_fetch(self, symbols: List[str], period: str = "1d", count: int = 90) -> Dict[str, pd.DataFrame]:
        """
        批量获取K线（缓存优先，API补缺）
        count默认90根 (~4个月), 为60日回测验证预留足够未来数据。

        返回: {symbol: DataFrame}
        """
        from tickflow import TickFlow
        tf = TickFlow.free()

        result = {}
        missed = []

        for sym in symbols:
            cached = self.get(sym)
            if cached is not None:
                result[sym] = cached
            else:
                missed.append(sym)

        if missed:
            print(f"  缓存命中 {len(result)}，请求 {len(missed)} 只...")
            for i in range(0, len(missed), 20):
                batch = missed[i:i + 20]
                try:
                    dfs = tf.klines.batch(batch, period=period, count=count, as_dataframe=True)
                    for sym in batch:
                        df = dfs.get(sym)
                        if df is not None and len(df) > 0:
                            self.put(sym, df)
                            result[sym] = df
                    time.sleep(1.2)  # Free tier 限速: 60请求/分钟
                except Exception as e:
                    print(f"    批量请求失败 [{i}-{i+20}]: {e}")
                    time.sleep(3)  # 出错后等久一点
                    continue

        return result

    def prefetch_all(self, whitelist_path: str = None,
                     batch_size: int = 20, count: int = 90) -> int:
        if whitelist_path is None:
            whitelist_path = paths.STOCK_WHITELIST
        """预取全部白名单的K线到缓存"""
        with open(whitelist_path, 'r') as f:
            whitelist = json.load(f)

        all_symbols = [
            f"{s['code']}.SH" if s['code'].startswith('6') else f"{s['code']}.SZ"
            for s in whitelist
        ]

        total = len(all_symbols)
        cached_count = 0

        for i in range(0, total, batch_size):
            batch = all_symbols[i:i + batch_size]
            result = self.batch_fetch(batch, count=count)
            cached_count += len(result)
            if (i // batch_size) % 50 == 0 and i > 0:
                print(f"  预取进度: {i}/{total} ({cached_count} 已缓存)")

        return cached_count

    def stats(self) -> dict:
        """缓存统计"""
        total = len(self._meta)
        fresh = sum(1 for v in self._meta.values()
                    if (time.time() - v.get('cached_at', 0)) / 3600 < self.expiry_hours)
        return {
            'total': total,
            'fresh': fresh,
            'expired': total - fresh,
            'cache_dir': self.cache_dir,
        }


def compute_returns_from_cache(cache: KlineCache, symbols: List[str],
                                lookback: int = 90) -> List[dict]:
    """从缓存计算涨幅，返回股票涨幅列表"""
    from tickflow import TickFlow
    tf = TickFlow.free()

    results = []
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i + 20]
        dfs = cache.batch_fetch(batch, count=lookback)
        for sym in batch:
            df = dfs.get(sym)
            if df is not None and len(df) >= max(lookback // 2, 10):
                closes = df['close'].values
                first, last = closes[0], closes[-1]
                if first > 0:
                    ret = (last - first) / first * 100
                    code = sym.split('.')[0]
                    results.append({
                        'code': code, 'symbol': sym,
                        'return_pct': round(ret, 2),
                        'close_start': round(float(first), 2),
                        'close_end': round(float(last), 2),
                        'kline_rows': len(df),
                    })
    return results


if __name__ == "__main__":
    cache = KlineCache()

    # 测试单只
    df = cache.get("000636.SZ")  # 风华高科
    if df is not None:
        print(f"缓存命中: 000636.SZ, {len(df)} 行")
        print(f"  最新价: {df['close'].iloc[-1]:.2f}")
    else:
        print("缓存未命中，拉取...")
        result = cache.batch_fetch(["000636.SZ"])
        if "000636.SZ" in result:
            print(f"  成功: {len(result['000636.SZ'])} 行")

    print(f"\n缓存统计: {cache.stats()}")