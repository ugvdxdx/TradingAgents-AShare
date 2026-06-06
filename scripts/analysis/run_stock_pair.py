"""北方华创(002371.SZ) vs 中芯国际(688981.SH) 对比分析"""
import os, sys, json
from datetime import date as _date, datetime
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

sys.path.insert(0, '/Users/bilibili/Desktop/J-TradingAgents')

from tradingagents.dataflows.providers.astock_provider import AstockProvider
from tradingagents.llm_clients import create_llm_client
from langchain_core.messages import HumanMessage, SystemMessage

today = _date.today().strftime("%Y-%m-%d")
stocks = {
    "002371": "北方华创",
    "688981": "中芯国际",
}

print(f"{'='*60}")
print(f"  个股深度分析 — {today} {datetime.now().strftime('%H:%M')}")
print(f"{'='*60}")

astock = AstockProvider()
all_data = {}

for code, name in stocks.items():
    print(f"\n{'─'*60}")
    print(f"📡 {name}({code}) 数据采集")
    print(f"{'─'*60}")
    data_parts = []

    # 1. Realtime quote
    print(f"  ▶ 实时行情...")
    try:
        qt = astock.get_realtime_quotes([code])
        j = json.loads(qt)
        if code in j:
            q = j[code]
            print(f"    价: {q.get('price','N/A')}  涨幅: {q.get('change_pct','N/A')}%  PE: {q.get('pe_ttm','N/A')}  PB: {q.get('pb','N/A')}")
            data_parts.append(f"【实时行情】\n{json.dumps(q, ensure_ascii=False, indent=2)}")
        else:
            print(f"    ⚠ 行情结果: {str(qt)[:200]}")
            data_parts.append(f"【实时行情】\n{qt[:1000]}")
    except Exception as e:
        print(f"    ⚠ {e}")

    # 2. Stock history (近90日)
    print(f"  ▶ 历史K线...")
    try:
        start = _date.fromordinal(_date.today().toordinal()-120)
        hist = astock.get_stock_data(code, start.strftime("%Y-%m-%d"), today)
        print(f"    ✓ {len(hist)} 字符")
        data_parts.append(f"【历史K线(近120日)】\n{hist[:3000]}")
    except Exception as e:
        print(f"    ⚠ {e}")

    # 3. Individual fund flow
    print(f"  ▶ 个股资金流...")
    try:
        flow = astock.get_individual_fund_flow(code)
        print(f"    ✓ {len(flow)} 字符")
        data_parts.append(f"【个股资金流】\n{flow[:2000]}")
    except Exception as e:
        print(f"    ⚠ {e}")

    # 4. Concept belonging
    print(f"  ▶ 所属概念...")
    try:
        concept = astock.get_stock_concept_belonging(code)
        print(f"    ✓ {len(concept)} 字符")
        data_parts.append(f"【所属概念】\n{concept[:2000]}")
    except Exception as e:
        print(f"    ⚠ {e}")

    # 5. News
    print(f"  ▶ 相关新闻...")
    try:
        news = astock.get_news(code, "", today)
        print(f"    ✓ {len(news)} 字符")
        data_parts.append(f"【相关新闻】\n{news[:2000]}")
    except Exception as e:
        print(f"    ⚠ {e}")

    all_data[name] = "\n\n".join(data_parts)
    print(f"  ✓ 数据完成 ({len(data_parts)} 源)")

# ── LLM Analysis ──
print(f"\n{'='*60}")
print(f"🧠 LLM 分析中...")
print(f"{'='*60}")

deep_client = create_llm_client(
    provider=os.getenv("TA_LLM_PROVIDER", "openai"),
    model=os.getenv("TA_LLM_DEEP", "gpt-4o"),
    base_url=os.getenv("TA_BASE_URL", "https://api.openai.com/v1"),
    api_key=os.getenv("TA_API_KEY", ""),
    temperature=0.7,
)
llm = deep_client.get_llm()

system_prompt = """你是一位资深A股半导体设备/制造行业分析师。请对两只股票进行全面对比分析。

## 分析框架

1. **公司基本面对比**：业务定位、产业链位置、核心竞争力
2. **行情与技术面**：近期走势、量价关系、关键价位
3. **资金流向**：主力资金动向、北向资金态度
4. **概念与情绪**：所属概念热度、市场关注度
5. **趋势预测**：明确给出天/周/月三级走势判断
6. **综合结论**：哪只更优、操作建议

## 输出要求
- 结构化对比报告
- 所有判断基于数据
- 必须包含天/周/月三级预测
- 明确的操作建议"""

human_prompt = f"""请对比分析以下两只半导体核心标的。

分析日期：{today}

===== 北方华创(002371) =====
{all_data['北方华创'][:4000]}

===== 中芯国际(688981) =====
{all_data['中芯国际'][:4000]}

请给出完整的对比分析报告，包括天/周/月三级走势预测和操作建议。"""

print(f"  ▶ LLM推理中...\n")
messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
result = ""
for chunk in llm.stream(messages):
    content = chunk.content if hasattr(chunk, "content") else str(chunk)
    result += content
    print(content, end="", flush=True)
print(f"\n\n  ✓ 分析完成")

# ── Save ──
archive_dir = f"results/个股对比/北方华创_中芯国际/{today}"
os.makedirs(archive_dir, exist_ok=True)
with open(f"{archive_dir}/analysis.txt", "w", encoding="utf-8") as f:
    f.write(result)
for name in stocks.values():
    with open(f"{archive_dir}/{name}.txt", "w", encoding="utf-8") as f:
        f.write(all_data[name])
print(f"📁 已保存至: {archive_dir}/")