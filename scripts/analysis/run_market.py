"""今日大盘分析脚本"""
import os, sys, json
from datetime import date as _date, datetime
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

sys.path.insert(0, '/Users/bilibili/Desktop/J-TradingAgents')

from tradingagents.dataflows.providers.astock_provider import AstockProvider
from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider
from tradingagents.llm_clients import create_llm_client
from langchain_core.messages import HumanMessage, SystemMessage

today = _date.today().strftime("%Y-%m-%d")
print(f"{'='*60}")
print(f"  A股大盘分析 — {today} {datetime.now().strftime('%H:%M')}")
print(f"{'='*60}")

astock = AstockProvider()
akshare = CnAkshareProvider()
data_parts = []

# 1. Global news
print(f"\n📰 宏观新闻...")
try:
    news = astock.get_global_news(today, look_back_days=3, limit=30)
    print(f"  ✓ {len(news)} 字符")
    data_parts.append(f"【宏观新闻】\n{news[:3000]}")
except Exception as e:
    print(f"  ⚠ {e}")

# 2. Concept board ranking
print(f"📊 概念板块排名...")
try:
    boards = astock.get_concept_boards(top_n=20)
    print(f"  ✓ 获取完成")
    data_parts.append(f"【概念板块排名 TOP20】\n{boards[:2500]}")
except Exception as e:
    print(f"  ⚠ {e}")

# 3. Hot stocks
print(f"🔥 热门个股...")
try:
    hot = astock.get_hot_stocks_xq()
    print(f"  ✓ 获取完成")
    data_parts.append(f"【热门个股】\n{hot[:2000]}")
except Exception as e:
    print(f"  ⚠ {e}")

# 4. Board fund flow
print(f"💰 板块资金流...")
try:
    flow = astock.get_board_fund_flow()
    print(f"  ✓ 获取完成")
    data_parts.append(f"【板块资金流】\n{flow[:2000]}")
except Exception as e:
    print(f"  ⚠ {e}")

combined = "\n\n".join(data_parts)
print(f"\n  ✓ 共 {len(data_parts)} 个数据源")

# ── LLM Analysis ──
print(f"\n🧠 LLM 分析中...")
deep_client = create_llm_client(
    provider=os.getenv("TA_LLM_PROVIDER", "openai"),
    model=os.getenv("TA_LLM_DEEP", "gpt-4o"),
    base_url=os.getenv("TA_BASE_URL", "https://api.openai.com/v1"),
    api_key=os.getenv("TA_API_KEY", ""),
    temperature=0.7,
)
llm = deep_client.get_llm()

system_prompt = """你是一位资深A股大盘分析师。你的任务是基于实时市场数据，对今日大盘进行全面分析。

## 分析框架

1. **大盘总体判断**：今日市场整体强弱、涨跌家数比、成交额变化
2. **领涨/领跌板块分析**：哪些板块在涨、哪些在跌、资金流向
3. **市场情绪与资金面**：涨停/跌停数量、热点持续性、主力资金动向
4. **关键影响因素**：宏观政策、重大事件、外部环境
5. **下午走势预判**：基于上午盘面，判断下午方向
6. **风险提示**：当前市场需要关注的风险点

## 输出要求

- 结构化分析报告，条理清晰
- 所有判断基于数据
- 给出明确的多空判断和置信度
- 约800-1000字"""

human_prompt = f"""请分析今日A股大盘情况。

分析日期：{today}

采集到的市场数据：
{combined}

请给出今日大盘的完整分析报告，特别是下午走势预判。"""

print(f"  ▶ LLM推理中...")
messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
result = ""
for chunk in llm.stream(messages):
    content = chunk.content if hasattr(chunk, "content") else str(chunk)
    result += content
    print(content, end="", flush=True)
print(f"\n\n  ✓ 分析完成")

# ── Save ──
archive_dir = f"results/大盘/{today}"
os.makedirs(archive_dir, exist_ok=True)
with open(f"{archive_dir}/data.txt", "w", encoding="utf-8") as f:
    f.write(combined)
with open(f"{archive_dir}/analysis.txt", "w", encoding="utf-8") as f:
    f.write(result)
print(f"📁 已保存至: {archive_dir}/")