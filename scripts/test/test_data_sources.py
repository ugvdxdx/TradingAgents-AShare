"""补测：翻页概念板块 + 腾讯K线正确格式"""
import json, requests, time

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
url = "https://push2.eastmoney.com/api/qt/clist/get"

print("=" * 70)
print("17️⃣  概念板块 - 翻页获取全部")
print("=" * 70)
try:
    all_concepts = []
    for page in range(1, 10):
        params = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": "m:90+t:3",
            "fields": "f12,f14,f3",
        }
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        items = r.json().get("data", {}).get("diff", [])
        if not items:
            break
        all_concepts.extend(items)
        if len(items) < 100:
            break
    
    print(f"  总共获取到 {len(all_concepts)} 个概念板块")
    
    # 按热度排序
    concepts_sorted = sorted(all_concepts, key=lambda x: abs(x.get('f3', 0)), reverse=True)
    
    # 查找热门赛道概念
    keywords = ['芯片', '半导体', 'AI', '人工智能', '光通信', 'CPO', '算力', 
                '机器人', '光子', '集成电路', '信创', '数据要素', '低空']
    print("\n  热门赛道概念板块:")
    for item in concepts_sorted:
        name = item.get('f14', '')
        code = item.get('f12', '')
        change = item.get('f3', 0)
        if any(kw in name for kw in keywords):
            print(f"    {name} ({code}): {change:+.2f}%")
    
    # 保存全量概念映射到文件（用于后续本地查询）
    concept_map = {}
    for item in all_concepts:
        name = item.get('f14', '')
        code = item.get('f12', '')
        if name and code:
            concept_map[name] = code
    
    # 输出热门概念板块TOP20
    print("\n  热门概念板块TOP20 (按涨跌幅绝对值):")
    for item in concepts_sorted[:20]:
        name = item.get('f14', '')
        code = item.get('f12', '')
        change = item.get('f3', 0)
        print(f"    {name} ({code}): {change:+.2f}%")
    
except Exception as e:
    print(f"  FAIL: {e}")

print()
print("=" * 70)
print("18️⃣  个股所属板块查询 (push2)")
print("=" * 70)
try:
    # 通过行业板块成分股反向建索引 - 选前5个热门板块
    hot_boards = ['半导体', '芯片', '光通信', '机器人', '人工智能']
    
    # 先查行业板块和概念板块
    industry_map = {}
    for t in [2, 3]:
        params = {
            "pn": "1", "pz": "500", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"m:90+t:{t}",
            "fields": "f12,f14",
        }
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        items = r.json().get("data", {}).get("diff", [])
        for item in items:
            name = item.get('f14', '')
            code = item.get('f12', '')
            if name and code:
                industry_map[name] = code
                industry_map[code] = name
    
    print(f"  总板块数: {len(industry_map)//2}")
    
    # 查热门板块成分股
    print("\n  热门板块成分股:")
    for board_name in hot_boards:
        board_code = industry_map.get(board_name)
        if not board_code:
            continue
        
        params2 = {
            "pn": "1", "pz": "10", "po": "0", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"b:{board_code}+f:!50",
            "fields": "f12,f14,f3",
        }
        r2 = requests.get(url, params=params2, headers={"User-Agent": UA}, timeout=10)
        stocks = r2.json().get("data", {}).get("diff", [])
        total = r2.json().get("data", {}).get("total", 0)
        stock_names = [f"{s.get('f14')}({s.get('f12')})" for s in stocks[:5]]
        print(f"    {board_name}: {total}只股票, 例: {', '.join(stock_names)}")

except Exception as e:
    print(f"  FAIL: {e}")

print()
print("=" * 70)
print("19️⃣  腾讯K线 - 其他格式")
print("=" * 70)
# 其他格式 - 新浪K线
try:
    # 新浪日K
    url = "https://quotes.sina.com.cn/api/quotes?format=json&plate=1&symbol=sz000001&page=1&num=60"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    data = r.json()
    print(f"  新浪日K: {str(data)[:200]}")
except Exception as e:
    print(f"  新浪日K FAIL: {e}")

# 腾讯日K另一种格式
try:
    url = "http://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sz000001,mday,2026-04-01,2026-05-29,60"
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "http://finance.qq.com/"}, timeout=10)
    data = r.json()
    print(f"\n  腾讯日K (带日期): {str(data)[:200]}")
except Exception as e:
    print(f"  腾讯日K (带日期) FAIL: {e}")

# 腾讯日K 格式3
try:
    url = "http://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sz000001,mday,2026-04-01,,60"
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "http://finance.qq.com/"}, timeout=10)
    data = r.json()
    print(f"\n  腾讯日K (开始日期): {str(data)[:200]}")
except Exception as e:
    print(f"  腾讯日K (开始日期) FAIL: {e}")