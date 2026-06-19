"""测试TickFlow免费版数据源"""
import json, time
from tickflow import TickFlow

print("="*70)
print("TickFlow 免费版测试")
print("="*70)

tf = TickFlow.free()
print("✅ TickFlow 初始化成功\n")

# ─── 1. 查询标的信息 ───
print("=== 1. 标的信息 ===")
try:
    instruments = tf.instruments.batch(symbols=["600519.SH", "000001.SZ", "300750.SZ", "688981.SH"])
    print(f"获取到 {len(instruments)} 只标的信息")
    for inst in instruments:
        print(f"  {inst.get('symbol')}: {inst.get('name')} - {inst.get('industry', 'N/A')}")
except Exception as e:
    print(f"❌: {e}")

# ─── 2. 日K线数据 ───
print("\n=== 2. 日K线数据 ===")
for symbol, name in [("600519.SH", "贵州茅台"), ("000001.SZ", "平安银行"), ("300750.SZ", "宁德时代"), ("688981.SH", "中芯国际"), ("000636.SZ", "风华高科")]:
    try:
        df = tf.klines.get(symbol, period="1d", count=60, as_dataframe=True)
        if df is not None and len(df) > 0:
            closes = df['close'].values
            first, last = closes[0], closes[-1]
            ret = (last - first) / first * 100
            print(f"✅ {name}({symbol}): {len(df)}条日K, 涨幅={ret:.1f}%")
            print(f"   最新: {df.index[-1]} 收{last:.2f}")
        else:
            print(f"❌ {symbol}: 返回空")
    except Exception as e:
        print(f"❌ {symbol}: {e}")

# ─── 3. 批量K线 ───
print("\n=== 3. 批量K线测试 ===")
try:
    symbols = ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "000636.SZ"]
    dfs = tf.klines.batch(symbols, period="1d", count=60, as_dataframe=True, show_progress=True)
    print(f"批量获取: {len(dfs)} 只")
    for sym, df in dfs.items():
        if df is not None and len(df) > 0:
            ret = (df['close'].iloc[-1] - df['close'].iloc[0]) / df['close'].iloc[0] * 100
            print(f"  {sym}: {len(df)}条, 涨幅={ret:.1f}%")
except Exception as e:
    print(f"❌: {e}")

# ─── 4. 板块/标的市场查询 ───
print("\n=== 4. 沪深A股全量查询 ===")
try:
    # 先只测试能否获取标的池
    all_a = tf.instruments.query(market="CN_Equity_A")
    if all_a is not None:
        print(f"全A股标的池: {len(all_a)} 只")
        if len(all_a) > 10:
            print(f"  前5只: {[s.get('symbol') for s in all_a[:5]]}")
    else:
        print("❌: 返回None")
except Exception as e:
    print(f"❌: {e}")

# ─── 5. 性能测试 - 批量20只股票 ───
print("\n=== 5. 性能测试（20只批量）===")
test_symbols = [f"{code}.SH" if code.startswith('6') else f"{code}.SZ" 
                for code in ["600519","600036","601166","600900","600276",
                             "600887","600030","600585","600028","600104",
                             "000001","000002","000333","000651","000858",
                             "002415","002475","002714","300750","300059"]]

start = time.time()
dfs = tf.klines.batch(test_symbols, period="1d", count=60, as_dataframe=True, show_progress=True)
elapsed = time.time() - start

success = sum(1 for s in test_symbols if dfs.get(s) is not None and len(dfs[s]) > 0)
print(f"批量20只: {success}成功, 用时{elapsed:.1f}秒 (平均{elapsed/20:.2f}秒/只)")

# 推算3000只需要多久
print(f"推算3363只预估: {elapsed/20*3363/60:.1f}分钟")

# ─── 6. 验证风华高科（涨幅最大）───
print("\n=== 6. 风华高科涨幅验证 ===")
try:
    df = tf.klines.get("000636.SZ", period="1d", count=100, as_dataframe=True)
    if df is not None and len(df) > 20:
        closes = df['close'].values
        ret_3m = (closes[-1] - closes[-60]) / closes[-60] * 100 if len(closes) >= 60 else 0
        ret_1m = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
        print(f"  近3个月涨幅: {ret_3m:.1f}%")
        print(f"  近1个月涨幅: {ret_1m:.1f}%")
        print(f"  最新收盘: {closes[-1]:.2f}")
except Exception as e:
    print(f"❌: {e}")