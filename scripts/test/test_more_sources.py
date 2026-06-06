"""测试更多数据源（雪球、新浪备用接口等）"""
import requests, json

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"

print("="*70)
print("更多数据源测试")
print("="*70)

# ─── 1. 雪球 (xueqiu) ───
print("\n=== 雪球API ===")
try:
    url = "https://stock.xueqiu.com/v5/stock/batch/quote.json"
    params = {"symbol": "SH600519,SZ000001,SZ300750,SH688981", "extend": "detail"}
    r = requests.get(url, params=params, headers={"User-Agent": UA, "Cookie": "xq_a_token=test"}, timeout=10)
    data = r.json()
    print(f"雪球行情: {r.status_code}, {str(data)[:200]}")
except Exception as e:
    print(f"❌ 雪球: {e}")

# 雪球K线
try:
    url = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
    params = {"symbol": "SH600519", "begin": "20260301", "end": "20260529", "period": "day"}
    r = requests.get(url, params=params, headers={"User-Agent": UA, "Cookie": "xq_a_token=test"}, timeout=10)
    data = r.json()
    print(f"雪球K线: {r.status_code}, {str(data)[:200]}")
except Exception as e:
    print(f"❌ 雪球K线: {e}")

# ─── 2. 新浪备用接口 ───
print("\n=== 新浪备用API ===")
try:
    url = "https://vip.stock.finance.sina.com.cn/corp/go.php/vMS_MarketHistory/stockid/600519.phtml"
    params = {"Year": "2026", "jri": "5"}
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
    text = r.text
    if len(text) > 100:
        print(f"✅ 新浪历史数据: {len(text)}字符, 含table: {'<table' in text}")
        # 尝试查找数据行
        import re
        rows = re.findall(r'<tr[^>]*>.*?</tr>', text, re.DOTALL)
        print(f"  找到 {len(rows)} 个行标签")
except Exception as e:
    print(f"❌: {e}")

# ─── 3. 新浪财经个股页 ───
print("\n=== 新浪个股页 ===")
try:
    url = "https://finance.sina.com.cn/realstock/company/sh600519/nc.shtml"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    print(f"个股页: {len(r.text)}字符")
except Exception as e:
    print(f"❌: {e}")

# ─── 4. 腾讯个股页 ───
print("\n=== 腾讯个股页 ===")
try:
    url = "https://gu.qq.com/sh600519"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    print(f"腾讯个股页: {len(r.text)}字符")
except Exception as e:
    print(f"❌: {e}")

# ─── 5. 新浪多股票实时接口 ───
print("\n=== 新浪多股票实时行情 ===")
try:
    codes = "sh600519,sz000001,sz300750,sh688981"
    url = f"https://hq.sinajs.cn/list={codes}"
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}, timeout=10)
    print(f"新浪实时行情: {r.text[:300]}")
except Exception as e:
    print(f"❌: {e}")

# ─── 6. 腾讯批量行情 ───
print("\n=== 腾讯批量行情 ===")
try:
    codes = "sh600519,sz000001,sz300750,sh688981"
    url = f"https://qt.gtimg.cn/q={codes}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    print(f"腾讯批量行情: {r.text[:300]}")
except Exception as e:
    print(f"❌: {e}")

print("\n=== 数据源总结 ===")
print("✅ 新浪K线(scale=240): 日K线60条，最可靠")
print("✅ 新浪实时行情(hq.sinajs.cn): 批量实时数据")
print("✅ 腾讯批量行情(qt.gtimg.cn): PE/PB/市值")
print("❌ 同花顺K线(d.10jqka.com.cn): 返回空")
print("❌ 雪球API: 可能需要cookie认证")
print("❌ 东财push2: 部分接口被阻断")