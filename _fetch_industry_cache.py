#!/usr/bin/env python3
"""拉取 StockAPI 全量行业数据，缓存到 .cache/stockapi_industry.json

StockAPI /v1/base/all 每天只能请求2次，建议每天跑一次。
用法: uv run python3 _fetch_industry_cache.py
"""
import json
import os
import requests

def get_token():
    token = os.environ.get("STOCKAPI_TOKEN", "")
    if not token:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "STOCKAPI_TOKEN":
                            token = v.strip()
                            break
    return token

def main():
    token = get_token()
    if not token:
        print("ERROR: STOCKAPI_TOKEN not found")
        return

    print("Fetching StockAPI /v1/base/all ...")
    r = requests.get("https://www.stockapi.com.cn/v1/base/all",
                     params={"token": token}, timeout=30)
    d = r.json()

    if d.get("code") != 20000:
        print(f"ERROR: code={d.get('code')}, msg={d.get('msg', '')}")
        return

    data = d.get("data", [])
    print(f"Got {len(data)} stocks")

    industry_map = {}
    for item in data:
        code = str(item.get("api_code", ""))
        gl = item.get("gl", "")
        # gl 格式: "行业,子行业,..." 取前两个作为行业
        parts = [p for p in gl.split(",") if p]
        if len(parts) >= 2:
            industry = parts[0] + "-" + parts[1]  # 如 "汽车-汽车零部件"
        elif len(parts) == 1:
            industry = parts[0]
        else:
            continue
        if code:
            industry_map[code] = industry

    print(f"Built industry map: {len(industry_map)} stocks")

    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "stockapi_industry.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(industry_map, f, ensure_ascii=False)
    print(f"Saved to {cache_path}")

    # 验证
    for code in ["603211", "002222", "000581", "688192", "300400"]:
        print(f"  {code}: {industry_map.get(code, 'NOT FOUND')}")

if __name__ == "__main__":
    main()
