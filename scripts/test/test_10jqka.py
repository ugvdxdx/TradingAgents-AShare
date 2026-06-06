"""同花顺深度测试 - 找行业/概念接口"""
import requests, json, re

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"

print("="*70)
print("同花顺行业/概念数据深度测试")
print("="*70)

# ─── 1. 同花顺行情中心 - 行业板块 ───
print("\n=== 1. 同花顺行业板块列表 ===")
try:
    url = "https://q.10jqka.com.cn/thshy/"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    html = r.text
    print(f"行业板块首页: {len(html)}字符")
    # 找板块列表
    boards = re.findall(r'<a[^>]*href="/thshy/[^"]*"[^>]*>([^<]+)</a>', html)
    print(f"找到 {len(boards)} 个板块链接: {boards[:10]}")
except Exception as e:
    print(f"❌: {e}")

# ─── 2. 同花顺概念板块 ───
print("\n=== 2. 同花顺概念板块 ===")
try:
    url = "https://q.10jqka.com.cn/gn/"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    html = r.text
    print(f"概念板块首页: {len(html)}字符")
    concepts = re.findall(r'<a[^>]*href="/gn/[^"]*"[^>]*>([^<]+)</a>', html)
    print(f"找到 {len(concepts)} 个概念链接: {concepts[:10]}")
except Exception as e:
    print(f"❌: {e}")

# ─── 3. 同花顺个股页 - 找行业信息 ───
print("\n=== 3. 同花顺个股行业信息 ===")
for code, name in [("600519","贵州茅台"), ("002281","光迅科技"), ("688697","纽威数控"), ("600396","华电辽能")]:
    try:
        url = f"https://q.10jqka.com.cn/stock/{code}/"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        html = r.text
        # 找行业
        industry = re.search(r'所属行业[^<]*<[^>]*>([^<]+)', html)
        concept = re.search(r'所属概念[^<]*<[^>]*>([^<]+)', html)
        print(f"  {code} {name}:")
        print(f"    行业: {industry.group(1).strip() if industry else '未找到'}")
        print(f"    概念: {concept.group(1).strip()[:50] if concept else '未找到'}")
    except Exception as e:
        print(f"  {code}: ❌ {e}")

# ─── 4. 同花顺财务数据API ───
print("\n=== 4. 同花顺财务数据 ===")
try:
    url = "https://basic.10jqka.com.cn/api/stock/phase/600519/industry/"
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://q.10jqka.com.cn"}, timeout=10)
    print(f"行业API: {r.status_code} {r.text[:200]}")
except Exception as e:
    print(f"❌: {e}")

try:
    url = "https://stockpage.10jqka.com.cn/600519/industry/"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    print(f"行业页: {len(r.text)}字符, {r.text[:200]}")
except Exception as e:
    print(f"❌: {e}")

# ─── 5. 同花顺Ajax接口 ───
print("\n=== 5. 同花顺Ajax数据 ===")
try:
    url = "https://q.10jqka.com.cn/index/index/board/all/field/zdf/order/desc/page/1/ajax/1/"
    r = requests.get(url, headers={
        "User-Agent": UA, 
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://q.10jqka.com.cn",
    }, timeout=10)
    html = r.text
    print(f"Ajax涨停版: {len(html)}字符")
    # 找股票列表
    stocks = re.findall(r'<tr[^>]*id="tr_\d+"[^>]*>.*?<td[^>]*class="[^"]*code[^"]*"[^>]*>(\d+)</td>.*?<td[^>]*class="[^"]*name[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>', html, re.DOTALL)
    print(f"找到 {len(stocks)} 只股票")
    for code2, n in stocks[:3]:
        print(f"  {code2} {n}")
except Exception as e:
    print(f"❌: {e}")

# ─── 6. 尝试从10jqka获取行业分类数据 ───
print("\n=== 6. 10jqka个股行业分类 ===")
try:
    # 尝试从10jqka的stock搜索API获取
    url = "https://stockpage.10jqka.com.cn/spService/600519/operate/getIndustry/"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    print(f"行业接口: {r.text[:200]}")
except Exception as e:
    print(f"❌: {e}")

# 试试其他接口格式
for api_path in [
    "http://basic.10jqka.com.cn/api/stock/phase/600519/industry/",
    "http://basic.10jqka.com.cn/api/stock/phase/600519/",
    "https://stockpage.10jqka.com.cn/600519/operate/",
]:
    try:
        r = requests.get(api_path, headers={"User-Agent": UA}, timeout=10)
        print(f"  {api_path}: {r.status_code} {len(r.text)}字符")
        if r.status_code == 200 and len(r.text) > 50:
            # 找keywords
            keywords = re.findall(r'(?:行业|概念|板块)[：:]\s*([^<,，]+)', r.text)
            if keywords:
                print(f"    行业/概念关键词: {keywords}")
    except Exception as e:
        print(f"  {api_path}: ❌ {e}")