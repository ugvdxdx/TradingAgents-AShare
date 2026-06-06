#!/usr/bin/env python3
"""个股复盘脚本 — 读取历史分析存档 + 采集最新行情 + LLM 生成复盘报告

用法:
    python run_review.py 002371
    python run_review.py 北方华创
    python run_review.py 002371.SZ
"""
import os, sys, json, re
from datetime import date as _date, datetime
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

sys.path.insert(0, '/Users/bilibili/Desktop/J-TradingAgents')

from tradingagents.dataflows.providers.astock_provider import AstockProvider
from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
from tradingagents.llm_clients import create_llm_client
from langchain_core.messages import HumanMessage, SystemMessage
from tradingagents.agents.utils.history_reviewer import HistoryReviewer
from tradingagents.stock_utils import normalize_symbol, search_cn_stock_by_name, get_reverse_stock_map

# ═══════════════════════════════════════════════
# 1. 解析输入
# ═══════════════════════════════════════════════
raw_input = sys.argv[1] if len(sys.argv) > 1 else "002371"
today = _date.today().strftime("%Y-%m-%d")
print(f"{'='*60}")
print(f"  个股复盘 — {raw_input}")
print(f"  日期: {today} | 时间: {datetime.now().strftime('%H:%M:%S')}")
print(f"{'='*60}")

# 解析股票代码/名称
ticker = normalize_symbol(raw_input) if re.search(r"\d", raw_input) else ""
name = raw_input

if ticker:
    # 纯代码输入
    name_map = get_reverse_stock_map() or {}
    name = name_map.get(ticker.replace(".SH","").replace(".SZ",""), raw_input)
else:
    # 中文名称输入 → 查代码
    result = search_cn_stock_by_name(raw_input)
    if result:
        ticker = result
print(f"  解析结果: {name} ({ticker})\n")

# ═══════════════════════════════════════════════
# 2. 读取历史分析记录
# ═══════════════════════════════════════════════
print(f"{'─'*60}")
print(f"📜 加载历史分析记录...")
print(f"{'─'*60}")

rev = HistoryReviewer()
all_history = []

# 完整分析存档 (results/{ticker}/)
code_short = ticker.replace(".SH","").replace(".SZ","")
hist_full = rev.find_stock_history(ticker)
if hist_full:
    print(f"  ✓ 完整分析存档: {len(hist_full)} 条记录")
    all_history.extend(hist_full)

# 快速分析存档 (results/个股/{name}_{code}/)
hist_quick = rev.find_quick_analysis_history(name, code_short)
if hist_quick:
    print(f"  ✓ 快速分析存档: {len(hist_quick)} 条记录")
    all_history.extend(hist_quick)

if not all_history:
    print(f"  ⚠ 未找到历史分析记录！")
    print(f"  请先通过 run_analysis.py 或 run_stock_quick.py 进行分析。")
    sys.exit(1)

# 去重（按日期）
seen_dates = set()
unique_history = []
for h in all_history:
    d = h.get("date", "")
    if d not in seen_dates:
        seen_dates.add(d)
        unique_history.append(h)
unique_history.sort(key=lambda x: x.get("date", ""), reverse=True)

print(f"  ℹ 去重后共 {len(unique_history)} 条记录")
for h in unique_history:
    print(f"    [{h['date']}] 方向={h.get('direction','?')}  核心={h.get('reason','')[:60]}")
print()

# ═══════════════════════════════════════════════
# 3. 采集最新行情
# ═══════════════════════════════════════════════
print(f"{'─'*60}")
print(f"📡 采集最新行情...")
print(f"{'─'*60}")

astock = AstockProvider()
akshare = CnAkshareProvider()
data_parts = []

# 3a. 实时行情
print(f"  ▶ 实时行情...")
current_price = None
try:
    qt = astock.get_realtime_quotes([code_short])
    quotes = json.loads(qt)
    if code_short in quotes:
        q = quotes[code_short]
        current_price = float(q.get("price", 0))
        print(f"    当前价: {q.get('price','N/A')}  涨幅: {q.get('change_pct','N/A')}%")
        data_parts.append(f"【实时行情】\n{json.dumps(q, ensure_ascii=False, indent=2)}")
    else:
        data_parts.append(f"【实时行情】\n{qt[:1000]}")
except Exception as e:
    print(f"    ⚠ {e}")

# 3b. 近期K线
print(f"  ▶ 近期K线(近60日)...")
try:
    hist = astock.get_stock_data(code_short,
        _date.fromordinal(_date.today().toordinal()-90).strftime("%Y-%m-%d"), today)
    data_parts.append(f"【历史K线】\n{hist[:3000]}")
    print(f"    ✓ 获取完成")
except Exception as e:
    print(f"    ⚠ {e}")

# 3c. 资金流
print(f"  ▶ 资金流...")
try:
    flow = astock.get_individual_fund_flow(code_short)
    data_parts.append(f"【资金流】\n{flow[:2000]}")
    print(f"    ✓ 获取完成")
except Exception as e:
    print(f"    ⚠ {e}")

# 3d. 概念归属
print(f"  ▶ 所属概念...")
try:
    concept = astock.get_stock_concept_belonging(code_short)
    data_parts.append(f"【所属概念】\n{concept[:1500]}")
    print(f"    ✓ 获取完成")
except Exception as e:
    print(f"    ⚠ {e}")

# 3e. 近期新闻
print(f"  ▶ 相关新闻...")
try:
    news = astock.get_news(name, code_short, today)
    if "暂不可用" in news or news.strip() == "":
        news = akshare.get_news(name, code_short, today)
    data_parts.append(f"【相关新闻】\n{news[:2000]}")
    print(f"    ✓ 获取完成")
except Exception as e:
    print(f"    ⚠ {e}")

combined_data = "\n\n".join(data_parts)

# ═══════════════════════════════════════════════
# 4. 生成复盘上下文
# ═══════════════════════════════════════════════
sep = "─" * 60
print(f"\n{sep}")
print(f"🧠 生成复盘分析...")
print(f"{sep}")

review_context = rev.generate_review_prompt(unique_history, current_price=current_price)
print(f"  ✓ 历史复盘上下文生成完成")

# ═══════════════════════════════════════════════
# 5. LLM 复盘评价
# ═══════════════════════════════════════════════
deep_client = create_llm_client(
    provider=os.getenv("TA_LLM_PROVIDER", "openai"),
    model=os.getenv("TA_LLM_DEEP", "gpt-4o"),
    base_url=os.getenv("TA_BASE_URL", "https://api.openai.com/v1"),
    api_key=os.getenv("TA_API_KEY", ""),
    temperature=0.7,
)
llm = deep_client.get_llm()

system_prompt = f"""你是一位资深A股复盘分析师。你的任务是基于历史分析记录 + 最新行情数据，对一只股票的历史分析进行系统性复盘。

## 分析框架

### 1. 历史预测回顾
- 逐一回顾每次历史分析的方向判断、目标价、核心逻辑
- 用当前股价验证历史判断的准确性

### 2. 偏差分析
- 历史判断正确/错误？正确/错误在哪里？
- 如果历史方向是"看多"但股价下跌，分析是"逻辑错误"还是"逻辑对但时机未到"
- 如果历史方向是"看空"但股价上涨，分析错判的原因

### 3. 关键转折点识别
- 历史分析中是否有识别出重要的转折信号？
- 哪些信号被忽略或误判了？

### 4. 当前市场状态判断
- 基于最新数据，当前处于什么阶段？
- 与历史分析时的判断相比，市场逻辑是否发生了根本变化？

### 5. 可迁移经验总结
- 这次复盘能给未来分析带来什么教训？
- 哪些分析框架是有效的，哪些需要修正？

## 输出要求
- 约800-1000字
- 包含明确的【历史判断评估】和【当前市场判断】两部分
- 不要只说"对"或"错"，要分析逻辑链条是否正确
- A股语境：看多=建议买入/持有，看空=建议卖出/回避"""

human_prompt = f"""请对 {name}({ticker}) 进行系统性复盘。

复盘日期：{today}
当前股价：{current_price or "未知"}

{review_context}

采集到的最新市场数据：
{combined_data}

请生成完整的复盘报告。"""

print(f"  ▶ LLM 推理中...\n")
print(f"{'='*60}")
print(f"  📋 复盘报告开始")
print(f"{'='*60}\n")

result = ""
for chunk in llm.stream([SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]):
    content = chunk.content if hasattr(chunk, "content") else str(chunk)
    result += content
    print(content, end="", flush=True)

print(f"\n\n{'='*60}")
print(f"  ✓ 复盘完成")
print(f"{'='*60}")

# ═══════════════════════════════════════════════
# 6. 存档复盘报告
# ═══════════════════════════════════════════════
archive_dir = f"results/个股复盘/{name}_{code_short}/{today}"
os.makedirs(archive_dir, exist_ok=True)

with open(f"{archive_dir}/review_report.md", "w", encoding="utf-8") as f:
    f.write(f"# {name}({ticker}) 复盘报告\n\n")
    f.write(f"- 复盘日期: {today}\n")
    f.write(f"- 当前股价: {current_price or '未知'}\n")
    f.write(f"- 历史记录数: {len(unique_history)}\n\n")
    f.write(result)

with open(f"{archive_dir}/data.json", "w", encoding="utf-8") as f:
    json.dump({
        "name": name,
        "ticker": ticker,
        "review_date": today,
        "current_price": current_price,
        "history_count": len(unique_history),
        "history": [{"date": h["date"], "direction": h.get("direction","?"), "reason": h.get("reason","")[:100]} for h in unique_history],
    }, f, ensure_ascii=False, indent=2)

print(f"\n📁 复盘报告已保存至: {archive_dir}/")
print(f"   - review_report.md")
print(f"   - data.json")
print(f"{'='*60}")