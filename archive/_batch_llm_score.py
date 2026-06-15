#!/usr/bin/env python3
"""
批量调用 .env 中的 LLM API 对所有股票进行 V2 双轨制评分
然后与模型直读评分对比
"""
import json
import os
import sys
import time
from dotenv import load_dotenv

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# 导入评分模块
from fundamental_scorer import (
    compute_fundamental_knowledge_v2,
    _load_llm_cache,
    _save_llm_cache,
    _build_stock_json,
    _call_llm_score_v2,
    FUNDAMENTALS_DIR,
    LLM_CACHE_FILE,
)

MODEL_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fundamental_model_scores.json')
COMPARE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.fundamental_score_compare.json')


def batch_llm_score(max_stocks=None, skip_cached=True):
    """批量调用LLM评分"""
    files = sorted(f for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith('.json'))
    if max_stocks:
        files = files[:max_stocks]
    
    total = len(files)
    cache = _load_llm_cache()
    scored = 0
    skipped = 0
    errors = 0
    
    print(f"开始LLM评分，共 {total} 只股票")
    print(f"API: {os.environ.get('TA_BASE_URL', 'N/A')}")
    print(f"Model: {os.environ.get('TA_LLM_QUICK', 'N/A')}")
    print()
    
    for i, fname in enumerate(files):
        code = fname.replace('.json', '')
        fpath = os.path.join(FUNDAMENTALS_DIR, fname)
        mtime = os.path.getmtime(fpath)
        cache_key = f"{code}_{mtime:.0f}_v2"
        
        if skip_cached and cache_key in cache:
            cached = cache[cache_key]
            if isinstance(cached, dict) and 'total' in cached:
                skipped += 1
                continue
        
        # 调用LLM
        stock_json = _build_stock_json(code)
        if not stock_json:
            errors += 1
            continue
        
        try:
            result = _call_llm_score_v2(stock_json)
            if result and result.get('total', 0) > 0:
                cache[cache_key] = result
                scored += 1
                if scored % 10 == 0:
                    print(f"  进度: {i+1}/{total} (新评{scored} 跳过{skipped} 错误{errors})")
                    _save_llm_cache(cache)
            else:
                errors += 1
        except Exception as e:
            errors += 1
            print(f"  错误 {code}: {str(e)[:60]}")
        
        # 限速：每秒不超过2次
        time.sleep(0.5)
    
    _save_llm_cache(cache)
    print(f"\nLLM评分完成: 新评{scored} 跳过{skipped} 错误{errors}")
    return cache


def compare_scores():
    """对比模型评分 vs LLM评分"""
    # 加载模型评分
    with open(MODEL_CACHE_FILE, 'r') as f:
        model_cache = json.load(f)
    
    # 加载LLM评分
    llm_cache = _load_llm_cache()
    
    # 按code整理
    model_by_code = {}
    for k, v in model_cache.items():
        if isinstance(v, dict) and 'code' in v:
            model_by_code[v['code']] = v
    
    llm_by_code = {}
    for k, v in llm_cache.items():
        if isinstance(v, dict) and 'total' in v and 'fundamental_score' in v:
            # 从cache key提取code
            code = k.split('_')[0]
            if code not in llm_by_code or v.get('total', 0) > llm_by_code[code].get('total', 0):
                llm_by_code[code] = v
    
    # 对比
    comparisons = []
    for code in model_by_code:
        if code in llm_by_code:
            m = model_by_code[code]
            l = llm_by_code[code]
            comparisons.append({
                'code': code,
                'name': m.get('name', ''),
                'industry': m.get('industry', ''),
                'model_total': m.get('total', 0),
                'model_fundamental': m.get('fundamental_score', 0),
                'model_sector': m.get('sector_score', 0),
                'llm_total': l.get('total', 0),
                'llm_fundamental': l.get('fundamental_score', 0),
                'llm_sector': l.get('sector_score', 0),
                'diff_total': round(m.get('total', 0) - l.get('total', 0), 1),
                'model_brief': m.get('brief', ''),
                'llm_brief': l.get('brief', ''),
            })
    
    comparisons.sort(key=lambda x: abs(x['diff_total']), reverse=True)
    
    # 保存对比结果
    with open(COMPARE_FILE, 'w', encoding='utf-8') as f:
        json.dump(comparisons, f, ensure_ascii=False, indent=1)
    
    # 统计
    if not comparisons:
        print("无对比数据")
        return
    
    diffs = [c['diff_total'] for c in comparisons]
    abs_diffs = [abs(d) for d in diffs]
    avg_diff = sum(abs_diffs) / len(abs_diffs)
    max_diff = max(abs_diffs)
    
    model_higher = sum(1 for d in diffs if d > 2)
    llm_higher = sum(1 for d in diffs if d < -2)
    similar = sum(1 for d in abs_diffs if d <= 2)
    
    # 相关性
    model_totals = [c['model_total'] for c in comparisons]
    llm_totals = [c['llm_total'] for c in comparisons]
    n = len(model_totals)
    if n > 1:
        mean_m = sum(model_totals) / n
        mean_l = sum(llm_totals) / n
        cov = sum((model_totals[i] - mean_m) * (llm_totals[i] - mean_l) for i in range(n)) / n
        std_m = (sum((x - mean_m) ** 2 for x in model_totals) / n) ** 0.5
        std_l = (sum((x - mean_l) ** 2 for x in llm_totals) / n) ** 0.5
        corr = cov / (std_m * std_l) if std_m * std_l > 0 else 0
    else:
        corr = 0
    
    print(f"\n{'='*110}")
    print(f"模型评分 vs LLM评分 对比报告")
    print(f"{'='*110}")
    print(f"对比股票数: {len(comparisons)}")
    print(f"平均绝对偏差: {avg_diff:.1f} 分")
    print(f"最大绝对偏差: {max_diff:.1f} 分")
    print(f"皮尔逊相关系数: {corr:.3f}")
    print(f"模型评分更高(差>2): {model_higher} 只")
    print(f"LLM评分更高(差>2): {llm_higher} 只")
    print(f"评分接近(差≤2): {similar} 只")
    
    # 基本面 vs 赛道 分别对比
    fund_diffs = [abs(c.get('model_fundamental', 0) - c.get('llm_fundamental', 0) or 0) for c in comparisons if c.get('llm_fundamental') is not None]
    sect_diffs = [abs(c.get('model_sector', 0) - c.get('llm_sector', 0) or 0) for c in comparisons if c.get('llm_sector') is not None]
    if fund_diffs:
        print(f"\n基本面维度平均偏差: {sum(fund_diffs)/len(fund_diffs):.1f}")
    if sect_diffs:
        print(f"赛道维度平均偏差: {sum(sect_diffs)/len(sect_diffs):.1f}")
    
    # 分数段对比
    print(f"\n--- 分数段对比 ---")
    for lo, hi in [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)]:
        bucket = [c for c in comparisons if lo <= c['model_total'] < hi]
        if bucket:
            avg_m = sum(c['model_total'] for c in bucket) / len(bucket)
            avg_l = sum(c['llm_total'] for c in bucket) / len(bucket)
            print(f"  模型分[{lo}-{hi}): {len(bucket)}只, 模型均分{avg_m:.1f}, LLM均分{avg_l:.1f}, 差{avg_m-avg_l:+.1f}")
    
    # 偏差最大的Top 20
    print(f"\n--- 偏差最大 Top 20 ---")
    print(f"{'代码':<8} {'名称':<10} {'行业':<18} {'模型':>5} {'LLM':>5} {'差值':>6}  {'模型理由':<16} {'LLM理由'}")
    print(f"{'-'*110}")
    for c in comparisons[:20]:
        print(f"{c['code']:<8} {c['name']:<10} {c['industry'][:16]:<18} "
              f"{c['model_total']:>5} {c['llm_total']:>5} {c['diff_total']:>+6.1f}  "
              f"{c['model_brief'][:14]:<16} {c['llm_brief'][:30]}")
    
    # 高分一致性
    print(f"\n--- 高分股(Top30)一致性 ---")
    top_model = sorted(comparisons, key=lambda x: x['model_total'], reverse=True)[:30]
    top_llm = sorted(comparisons, key=lambda x: x['llm_total'], reverse=True)[:30]
    model_top_codes = set(c['code'] for c in top_model)
    llm_top_codes = set(c['code'] for c in top_llm)
    overlap = model_top_codes & llm_top_codes
    print(f"模型Top30 vs LLM Top30 重叠: {len(overlap)}/30")
    for c in top_model:
        marker = '*' if c['code'] in overlap else ' '
        print(f" {marker} {c['code']:<8} {c['name']:<10} 模型{c['model_total']:>5} LLM{c['llm_total']:>5} {c['diff_total']:>+6.1f}")
    
    return comparisons


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--max', type=int, default=None, help='最多评分几只')
    parser.add_argument('--no-skip', action='store_true', help='不跳过已缓存的')
    parser.add_argument('--compare-only', action='store_true', help='只做对比不评分')
    args = parser.parse_args()
    
    if not args.compare_only:
        batch_llm_score(max_stocks=args.max, skip_cached=not args.no_skip)
    
    compare_scores()
