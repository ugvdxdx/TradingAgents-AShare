#!/usr/bin/env python3
"""资金流 vs 合成capital 回测 — 干净(point-in-time)对比, 回答"V3+真实资金流是否优于价格代理"。

设计原则 (修正旧 validate_anchor 的前视偏差):
  1. 所有信号精确截断到 cutoff (资金流 main_pct 用 cutoff 前 N 日累计; 价格因子用 cutoff 前 r20)
  2. 资金流用 main_pct (净流入占成交比, 跨股可比), 非原始 main_net (大股市值偏置)
  3. chain/surge 无历史快照 → 用当前分=前视, 单列"含基本面(前视上界)"不当主结论
  4. 多指标: Spearman + TOP10收益 + 价差 + 胜率; + TOP5可持续性体检

核心问题: 真实主力资金流 (main_pct) 预测30日收益, 是否优于价格涨幅代理 (r20)?
"""
import json, os, sys, pickle
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picker.paths as paths

HOLD = 30  # 前视收益窗口
STEP = 5  # cutoff 间隔 (交易日)


def _load_mf():
    """{code: {date_int: main_pct}} 资金流时间序列"""
    p = '.mf_cache/mf.pkl'
    if not os.path.exists(p):
        p = os.path.join(paths.DATA_DIR, '.mf_cache', 'mf.pkl')
    raw = pickle.load(open(p, 'rb'))
    out = {}
    for code, lst in raw.items():
        d = {}
        for r in lst:
            try:
                d[int(r['date'])] = float(r.get('main_pct', 0) or 0)
            except Exception:
                pass
        if d:
            out[code.zfill(6)] = d
    return out


def _load_klines():
    """{code: df} K线"""
    out = {}
    for f in os.listdir(paths.KLINE_CACHE_DIR):
        if not f.endswith('.pkl'):
            continue
        code = f.replace('_SH.pkl', '').replace('_SZ.pkl', '')
        try:
            df = pickle.load(open(os.path.join(paths.KLINE_CACHE_DIR, f), 'rb'))
            df = df.sort_values('trade_date').reset_index(drop=True)
            out[code] = df
        except Exception:
            pass
    return out


def _date_int(s):
    return int(str(s).replace('-', ''))


def _cutoffs(klines, step=STEP):
    """从一只全周期股取 cutoff 日期 (留 HOLD 日算前视收益)"""
    for code, df in klines.items():
        if len(df) > 200:
            dates = df['trade_date'].tolist()
            return [_date_int(d) for i, d in enumerate(dates) if 60 <= i < len(dates) - HOLD - 1 and i % step == 0]
    return []


def spearman(a, b):
    a, b = np.array(a, float), np.array(b, float)
    def rk(x):
        o = np.argsort(x); r = np.empty(len(x));
        for i, idx in enumerate(o): r[idx] = i
        return r
    ra, rb = rk(a), rk(b)
    n = len(a)
    return float(np.corrcoef(ra, rb)[0, 1]) if n > 3 else 0.0


def main():
    print("加载数据...", flush=True)
    mf = _load_mf()
    klines = _load_klines()
    cutoffs = _cutoffs(klines)
    print(f"  资金流: {len(mf)} 股 | K线: {len(klines)} 股 | cutoff: {len(cutoffs)} 个")
    print(f"  cutoff 范围: {cutoffs[0]} ~ {cutoffs[-1]} (每{STEP}交易日, 留{HOLD}日前视)")
    print()

    # 公式定义 (每个返回该股在cutoff的信号值; None=无数据)
    formulas = {
        'main_pct_5d (真实资金流5日)': lambda code, ci: _mf_sum(mf, code, ci, 5),
        'main_pct_20d (真实资金流20日)': lambda code, ci: _mf_sum(mf, code, ci, 20),
        'cap_r20 (价格涨幅代理, 当前capital基础)': lambda code, ci: _cap_r20(klines, code, ci),
        'main_pct_20d + cap_r20 (组合)': lambda code, ci: _combine(mf, klines, code, ci),
    }

    results = {k: {'spearman': [], 'top10': [], 'bot10': []} for k in formulas}

    for ci, cutoff in enumerate(cutoffs):
        # 计算前视收益 + 各信号
        rows = []
        for code, df in klines.items():
            dates = df['trade_date'].tolist()
            di = [_date_int(d) for d in dates]
            # 找 cutoff 位置
            try:
                idx = di.index(cutoff)
            except ValueError:
                continue
            if idx + HOLD >= len(df):
                continue
            fwd = (df['close'].iloc[idx + HOLD] / df['close'].iloc[idx] - 1) * 100
            row = {'code': code, 'fwd': fwd}
            ok = True
            for fname, fn in formulas.items():
                v = fn(code, cutoff)
                row[fname] = v
                if v is None and fname == list(formulas)[0]:
                    ok = False
            if ok:
                rows.append(row)
        if len(rows) < 50:
            continue
        fwds = [r['fwd'] for r in rows]
        pool_avg = np.mean(fwds)
        for fname in formulas:
            vals = [r[fname] for r in rows if r[fname] is not None]
            fwds_sub = [r['fwd'] for r in rows if r[fname] is not None]
            if len(vals) < 50:
                continue
            sp = spearman(vals, fwds_sub)
            results[fname]['spearman'].append(sp)
            # TOP10/BOT10 by signal
            order = sorted(range(len(rows)), key=lambda i: -(rows[i][fname] or -1e9))
            top10 = np.mean([rows[i]['fwd'] for i in order[:10]])
            bot10 = np.mean([rows[i]['fwd'] for i in order[-10:]])
            results[fname]['top10'].append(top10)
            results[fname]['bot10'].append(bot10)

    # 输出
    print(f"{'='*92}")
    print(f"  资金流 vs 合成capital 回测 ({len(cutoffs)}个cutoff × ~{len(klines)}股 × {HOLD}日前视, 全部point-in-time)")
    print(f"{'='*92}")
    print(f"{'公式':<42} {'Spearman':>9} {'胜率':>6} {'TOP10收益':>9} {'BOT10':>7} {'价差':>7}")
    print('-' * 92)
    ranked = sorted(results.items(), key=lambda x: -np.mean(x[1]['spearman']))
    for fname, r in ranked:
        sp = r['spearman']
        if not sp:
            continue
        avg_sp = np.mean(sp)
        win = sum(1 for s in sp if s > 0) / len(sp)
        top10 = np.mean(r['top10'])
        bot10 = np.mean(r['bot10'])
        print(f"  {fname:<40} {avg_sp:>+9.3f} {win*100:>5.0f}% {top10:>+8.1f}% {bot10:>+6.1f}% {top10-bot10:>+6.1f}")

    print(f"\n  池子平均前视收益: {np.mean([np.mean(r['top10']+r['bot10']) for r in results.values()]):+.1f}% (参考)")
    print(f"\n  ⚠ 本回测只测【时变信号】(资金流/价格), 不含chain/surge(无历史快照=前视)。")
    print(f"     结论: 若真实资金流(main_pct)Spearman > cap_r20, 说明用真实资金流替代合成capital更优。")


def _mf_sum(mf, code, cutoff_date, days):
    """该股 cutoff 前 N 日 main_pct 累计 (point-in-time)。"""
    d = mf.get(code)
    if not d:
        return None
    before = sorted(dt for dt in d if dt <= cutoff_date)
    if len(before) < days:
        return None
    return sum(d[dt] for dt in before[-days:])


def _cap_r20(klines, code, cutoff_date):
    """该股 cutoff 时 r20 (20日涨幅, 合成capital基础)。"""
    df = klines.get(code)
    if df is None:
        return None
    di = [_date_int(x) for x in df['trade_date'].tolist()]
    try:
        idx = di.index(cutoff_date)
    except ValueError:
        return None
    if idx < 20:
        return None
    return (df['close'].iloc[idx] / df['close'].iloc[idx - 20] - 1) * 100


def _combine(mf, klines, code, cutoff_date):
    a = _mf_sum(mf, code, cutoff_date, 20)
    b = _cap_r20(klines, code, cutoff_date)
    if a is None or b is None:
        return None
    return a + b


if __name__ == '__main__':
    main()
