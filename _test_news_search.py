"""Test Eastmoney news search with proper parsing."""
import requests, re, json

def search_eastmoney_news(keyword, max_results=8):
    """Eastmoney search API - search by company name."""
    url = 'https://search-api-web.eastmoney.com/search/jsonp'
    param = json.dumps({
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": 10,
                "preTag": "",
                "postTag": ""
            }
        }
    }, ensure_ascii=False)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
        'Referer': 'https://so.eastmoney.com/',
        'Accept': '*/*'
    }
    resp = requests.get(url, params={'cb': 'cb', 'param': param}, headers=headers, timeout=10)
    text = resp.text
    # Strip JSONP wrapper
    json_str = text[text.index("(") + 1:text.rindex(")")]
    data = json.loads(json_str)
    result = data.get("result", {})
    # result["cmsArticleWebOld"] might be a list directly
    raw = result.get("cmsArticleWebOld", [])
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("list", [])
    else:
        items = []
    print(f"Found {len(items)} results for '{keyword}'")
    for item in items[:max_results]:
        title = item.get("title", "").replace("<em>", "").replace("</em>", "")
        date = item.get("date", "")[:10]
        content = (item.get("content", "") or "").replace("<em>", "").replace("</em>", "")[:150]
        source = item.get("mediaName", "")
        print(f"- [{date}] {title} ({source})")
        if content:
            print(f"  {content}")
    return items

print("=== 中际旭创 ===")
search_eastmoney_news("中际旭创")

print("\n=== 新易盛 ===")
search_eastmoney_news("新易盛")

print("\n=== 盛合晶微 ===")
search_eastmoney_news("盛合晶微")
