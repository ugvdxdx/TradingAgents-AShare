"""新浪日K - 不同参数测试"""
import json, requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

print("=== 各种参数组合测试 ===\n")

params_list = [
    {"symbol": "sh600519", "datalen": 60},
    {"symbol": "sh600519", "datalen": 60, "ma": "no"},
    {"symbol": "sh600519", "datalen": 60, "scale": 240},
    {"symbol": "sh600519", "datalen": 60, "scale": 1440},
]

for params in params_list:
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            print(f"✅ {params}: {len(data)}条, 最新={data[-1].get('day','')}")
        elif isinstance(data, dict):
            print(f"❌ {params}: {data}")
        else:
            print(f"❌ {params}: {str(data)[:100]}")
    except Exception as e:
        print(f"❌ {params}: {e}")

print("\n=== 其他新浪日K接口 ===\n")

# 接口1: quotes
try:
    url2 = "https://quotes.sina.com.cn/api/quotes?format=json&plate=1&symbol=sh600519&page=1&num=60"
    r = requests.get(url2, headers={"User-Agent": UA}, timeout=10)
    print(f"quotes接口: {r.text[:200]}")
except Exception as e:
    print(f"quotes接口FAIL: {e}")

# 接口2: 历史数据
try:
    url3 = "https://vip.stock.finance.sina.com.cn/corp/go.php/vMS_MarketHistory/stockid/600519.phtml?Year=2026&jri=5"
    r = requests.get(url3, headers={"User-Agent": UA}, timeout=10)
    print(f"历史数据接口: {len(r.text)}字符, 前100: {r.text[:100]}")
except Exception as e:
    print(f"历史数据接口FAIL: {e}")

# 接口3: 新浪股票数据V2
try:
    url4 = "https://quotes.sina.com.cn/api/jsonp/var%20data=2026-05-29&code=sh600519"
    r = requests.get(url4, headers={"User-Agent": UA}, timeout=10)
    print(f"jsonp接口: {r.text[:200]}")
except Exception as e:
    print(f"jsonp接口FAIL: {e}")

print("\n=== 用新浪60分钟线代替日K线 ===\n")

# 60分钟线有60条数据 = 60个交易日 ≈ 3个月
for code, prefix, name in [("600519","sh","贵州茅台"), ("000001","sz","平安银行"), ("300750","sz","宁德时代"), ("688981","sh","中芯国际")]:
    try:
        params = {"symbol": f"{prefix}{code}", "datalen": 60, "scale": 60, "ma": 5}
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            last = data[-1]
            print(f"✅ {name}({code}): {len(data)}条60分钟线, 最新={last.get('day','')} 收{last.get('close','')}")
            closes = [float(d.get("close",0)) for d in data if d.get("close")]
            # 可用数据量: 60条60分钟 ≈ 15个交易日
            ma5 = sum(closes[-5:])/5 if len(closes)>=5 else 0
            ma10 = sum(closes[-10:])/10 if len(closes)>=10 else 0
            print(f"   MA5(60min)={ma5:.2f}  MA10(60min)={ma10:.2f}")
        else:
            print(f"❌ {name}({code}): {str(data)[:100]}")
    except Exception as e:
        print(f"❌ {name}({code}): {e}")