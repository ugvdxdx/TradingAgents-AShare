"""快速个股分析脚本 — 实时采集+LLM研判"""
import os, sys, json, time
from datetime import date as _date, datetime
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

from tradingagents.dataflows.providers.astock_provider import AstockProvider
from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
from tradingagents.llm_clients import create_llm_client
from langchain_core.messages import HumanMessage, SystemMessage

symbol = "000592"
name = "平潭发展"
today = _date.today().strftime("%Y-%m-%d")

print(f"{'='*60}")
print(f"  {name}({symbol}) 实时分析")
print(f"  日期: {today} | 时间: {datetime.now().strftime('%H:%M:%S')}")
print(f"{'='*60}")

# ── Init Providers ──
print(f"\n📡 数据采集...")
astock = AstockProvider()
akshare = CnAkshareProvider()

data_parts = []

# 1. Real-time quote
print(f"  ▶ 实时行情...")
try:
    qt = astock.get_realtime_quotes([symbol])
    j = json.loads(qt)
    if symbol in j:
        q = j[symbol]
        print(f"  当前价: {q.get('price','N/A')}  涨幅: {q.get('change_pct','N/A')}%")
        data_parts.append(f"【实时行情】\n{json.dumps(q, ensure_ascii=False)}")
    else:
        # try 000592
        qt2 = astock.get_realtime_quotes(["000592"])
        j2 = json.loads(qt2)
        if "000592" in j2:
            q = j2["000592"]
            print(f"  当前价: {q.get('price','N/A')}  涨幅: {q.get('change_pct','N/A')}%")
            data_parts.append(f"【实时行情】\n{json.dumps(q, ensure_ascii=False)}")
        else:
            print(f"  ⚠ 实时行情获取结果: {qt[:300]}")
            data_parts.append(f"【实时行情】\n{qt[:1000]}")
except Exception as e:
    print(f"  ⚠ 实时行情失败: {e}")

# 2. Stock history (近30日)
print(f"  ▶ 历史K线...")
try:
    hist = astock.get_stock_data(symbol, _date.fromordinal(_date.today().toordinal()-60).strftime("%Y-%m-%d"), today)
    # try with full code
    if "No data" in hist or "不可用" in hist:
        hist = astock.get_stock_data("000592", _date.fromordinal(_date.today().toordinal()-60).strftime("%Y-%m-%d"), today)
    print(f"  ✓ 历史数据获取完成 ({len(hist)} 字符)")
    data_parts.append(f"【历史K线(近60日)】\n{hist[:3000]}")
except Exception as e:
    print(f"  ⚠ 历史K线失败: {e}")

# 3. Individual fund flow
print(f"  ▶ 个股资金流...")
try:
    flow = astock.get_individual_fund_flow(symbol)
    if "暂不可用" in flow or flow.strip() == "":
        flow = akshare.get_individual_fund_flow(symbol)
    print(f"  ✓ 资金流获取完成")
    data_parts.append(f"【个股资金流】\n{flow[:2000]}")
except Exception as e:
    print(f"  ⚠ 资金流失败: {e}")

# 4. Concept belonging
print(f"  ▶ 所属概念...")
try:
    concept = astock.get_stock_concept_belonging(symbol)
    if "暂不可用" in concept or concept.strip() == "":
        concept = akshare.get_stock_concept_belonging(symbol)
    print(f"  ✓ 概念归属获取完成")
    data_parts.append(f"【所属概念】\n{concept[:2000]}")
except Exception as e:
    print(f"  ⚠ 概念归属失败: {e}")

# 5. News
print(f"  ▶ 相关新闻...")
try:
    news = astock.get_news(f"{name}", "", today)
    if "暂不可用" in news or news.strip() == "":
        news = akshare.get_news(f"{name}", "", today)
    print(f"  ✓ 新闻获取完成")
    data_parts.append(f"【相关新闻】\n{news[:2000]}")
except Exception as e:
    print(f"  ⚠ 新闻失败: {e}")

combined_data = "\n\n".join(data_parts)
print(f"\n  ✓ 数据采集完成，共 {len(data_parts)} 个数据源")

# ── LLM Analysis ──
print(f"\n{'='*60}")
print(f"🧠 LLM 分析中...")
print(f"{'='*60}")

# 加载历史复盘上下文
from tradingagents.agents.utils.history_reviewer import HistoryReviewer
hist_rev = HistoryReviewer()
history = hist_rev.find_quick_analysis_history(name, symbol)
review_prompt = ""
if history:
    review_prompt = hist_rev.generate_review_prompt(history)
    print(f"📜 已加载 {len(history)} 条历史分析记录")
else:
    history = hist_rev.find_stock_history(f"{symbol}.SZ")
    if not history:
        history = hist_rev.find_stock_history(f"{symbol}.SH")
    if history:
        review_prompt = hist_rev.generate_review_prompt(history)
        print(f"📜 已加载 {len(history)} 条完整历史分析记录")

# Init LLM
deep_client = create_llm_client(
    provider=os.getenv("TA_LLM_PROVIDER", "openai"),
    model=os.getenv("TA_LLM_DEEP", "gpt-4o"),
    base_url=os.getenv("TA_BASE_URL", "https://api.openai.com/v1"),
    api_key=os.getenv("TA_API_KEY", ""),
    temperature=0.7,
)
llm = deep_client.get_llm()

system_prompt = """你是一位经验丰富的A股短线交易分析师。你的任务是基于实时数据，预测个股下午的走势。

## 分析框架

1. **上午盘面回顾**：基于开盘价、最高价、最低价、当前价，判断上午走势特征
2. **资金动向**：主力资金流向、大单小单情况
3. **技术信号**：均线位置、支撑位、压力位、成交量变化
4. **概念情绪**：所属概念板块今日表现、联动效应
5. **下午走势预测**：明确看多/看空/震荡的判断，并给出具体逻辑

## 输出要求

- 输出格式为结构化分析报告
- 所有判断必须有数据支撑
- 给出下午走势的明确判断（看多/看空/震荡）及置信度
- 如果给出价格区间预测更好（如：预计下午在X.XX-X.XX区间运行）"""

human_prompt = f"""请分析 {name}({symbol}) 今天的走势，特别是下午的走向。

分析日期：{today}

采集到的数据：
{combined_data}

{review_prompt}

请基于以上数据（含历史复盘），给出下午走势的明确判断。
注意：分析报告中必须包含【历史复盘】小结，回顾过去预测的准确性并对比当前情况。"""

messages = [
    SystemMessage(content=system_prompt),
    HumanMessage(content=human_prompt),
]

print(f"  ▶ LLM推理中...")
result = ""
for chunk in llm.stream(messages):
    content = chunk.content if hasattr(chunk, "content") else str(chunk)
    result += content
    print(content, end="", flush=True)
print(f"\n\n  ✓ 分析完成")

# ── Save ──
archive_dir = f"results/个股/{name}_{symbol}/{today}"
os.makedirs(archive_dir, exist_ok=True)
with open(f"{archive_dir}/data.txt", "w", encoding="utf-8") as f:
    f.write(combined_data)
with open(f"{archive_dir}/analysis.txt", "w", encoding="utf-8") as f:
    f.write(result)
print(f"\n📁 分析已保存至: {archive_dir}/")