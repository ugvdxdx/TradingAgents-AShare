#!/usr/bin/env python3
"""
全量 V3 打分 + 基本面精华信息（阶段一：基本面排序）

一次 LLM 调用同时产出：
  1. 赛道动量三子维度小数分（chain/delivery/capital → sector_score 求和）
  2. 基本面精华信息 essence（服务下游30天涨幅竞争辩论）

工程保障：8线程并发、逐只落盘加锁、断点续跑、失败不落盘自动重试。
缓存：.fundamental_v3_scores.json（复用，已缓存但缺 essence 的会重跑补齐）

Prompt 升级 (2026-06):
  - chain/delivery 边界判断规则 + 财务交叉验证 + 反例参考
  - essence 质量禁则 (禁止空话/对仗/同义重复)
  - 世界知识注入 (市场格局 + AI算力主线 + 退潮赛道 + 中报窗口)
  - chain/delivery TTL 7 天自动过期重评 (capital 每日量化, 不依赖 LLM)

⚠️ 前视偏差：fundamentals 快照含最新已兑现叙事。本榜单用于【当前选股】是合理的
   （就是要用最新认知选未来），但不可再用历史涨幅自证。
"""
import os, sys, json, re, time, threading
from datetime import datetime, timedelta
from typing import Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 项目根加进 sys.path (兼容从子目录直接运行 + 部分遗留裸 import)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(override=True)

from picker.scoring import fundamental_scorer as fs
from picker import paths
from picker.scoring.chain_tiers import get_chain_prompt, get_tier_version  # chain 分档动态化 (PROMPT_V3E 档位段 → chain_tier_map.json)

V3_CACHE = paths.V3_CACHE
FUNDAMENTALS_DIR = paths.FUNDAMENTALS_DIR
FUNDAMENTALS_COLD_DIR = paths.FUNDAMENTALS_COLD_DIR  # 冷股存储
KLINE_CACHE_DIR = paths.KLINE_CACHE_DIR


def _find_fundamental(code):
    """在主目录或冷股目录中查找 fundamentals JSON, 返回路径或 None。"""
    for d in [FUNDAMENTALS_DIR, FUNDAMENTALS_COLD_DIR]:
        path = os.path.join(d, f"{code}.json")
        if os.path.exists(path):
            return path
    return None


def _list_all_fundamental_codes():
    """列出主目录+冷股目录的全部 fundamentals 代码。"""
    codes = set()
    for d in [FUNDAMENTALS_DIR, FUNDAMENTALS_COLD_DIR]:
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".json"):
                    codes.add(f[:-5])
    return sorted(codes)

# 直连 OpenAI 客户端，120s 超时（reasoning 模型单次生成可能 >30s，避免被误杀）
_API_KEY = os.environ.get("TA_API_KEY")
_BASE_URL = os.environ.get("TA_BASE_URL")
_MODEL = os.environ.get("TA_LLM_QUICK") or os.environ.get("TA_LLM_DEEP") or "deepseek-v4-pro"
_CLIENT_LOCAL = threading.local()


def _client():
    if not hasattr(_CLIENT_LOCAL, "c"):
        from openai import OpenAI
        _CLIENT_LOCAL.c = OpenAI(api_key=_API_KEY, base_url=_BASE_URL)
    return _CLIENT_LOCAL.c


def _llm(prompt, max_tokens=2048):
    """调用 LLM, 带自动重试。429 限流用长退避 + 抖动, 其他瞬时错误短退避。

    GLM/BigModel 账户有速率限制, 多 worker 并发时易触发 429
    ("您的账户已达到速率限制")。原 3 次×1.5s 短退避不足以等限流窗口恢复。
    现策略: 429 单独走 10-30s 长退避 + 随机抖动 (防多 worker 同步重试再撞限流),
    最多 5 次; 其他错误仍 3 次短退避。

    Args:
        max_tokens: 输出上限。GLM-5.2 是推理模型, 大输出(如 tier_map JSON)需调高
                    (默认 2048; chain_tiers 等大结构用 4096)。
    """
    import random as _rnd
    last_err = None
    for attempt in range(5):
        try:
            resp = _client().chat.completions.create(
                model=_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=max_tokens, timeout=120,
            )
            msg = resp.choices[0].message
            content = (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")
            if content:
                return content
            last_err = "empty content"
            time.sleep(2)
            continue
        except Exception as e:
            err_str = str(e)
            err_type = type(e).__name__
            last_err = f"{err_type}: {err_str[:120]}"
            if attempt >= 4:
                break
            # 429 限流: 长退避 (限流窗口需较长冷却) + 抖动 (防多 worker 同步)
            is_rate_limit = ("429" in err_str or "RateLimit" in err_type
                             or "速率限制" in err_str or "1302" in err_str)
            if is_rate_limit:
                wait = 10 * (attempt + 1) + _rnd.uniform(0, 8)  # ~10-58s 带抖动
                time.sleep(wait)
            else:
                # 其他瞬时错误: 短退避
                time.sleep(1.5 * (attempt + 1))
    # 全部重试失败, 记录原因便于排查 (不再静默吞掉)
    if last_err:
        print(f"    [LLM] 放弃: {last_err}", flush=True)
    return None

PROMPT_V3E = """你是A股量化研究员，对股票赛道动量评分并提炼30天涨幅辩论精华。

## 赛道动量评分（三子维度，各保留1位小数）

### chain 赛道热度×竞争力 (0.0-10.0) — 为预测收益, 不是产业研究

**chain = 赛道热度(theme级) × 个股核心竞争力。** 档=赛道热度带(可重叠), 档内分数由竞争力定。
从what_they_do判断真实业务+赛道, 不要只看industry标签。

**档位规则 (6档热度带, 档间有意重叠 — 完整动态版见注入的tier_map, 下方为回退)**：
- 8.5-10.0 最热主线带: AI算力绝对主线 (1.6T光模块/HBM/AI主芯片/CoWoS), 资金极致集中
- 7.0-9.0 热门主线带: AI链高景气 (800G光模块/存储/AI电源/光芯片), 业绩兑现中
- 5.5-7.5 温热新兴带: 升温新主题 (金刚石散热/玻璃基板/PCIe Retimer/战略金属/PCB/半导体设备)
- 3.5-5.5 中性带: 稳定需求非热点 (电力/半导体设计/化工/创新药)
- 2.0-4.0 偏冷带: 景气下行 (消费电子/汽车电子/军工/机器人)
- 0.0-2.5 冷门退潮带: 旧赛道 (锂电/白酒/地产/油气/传统矿业), 诚实给低分

**竞争力档内分化 (决定档内具体分, 核心因子)**：
- 龙头/高份额/技术壁垒/绑定顶级客户 → 拿档内高分 (如热门带龙头8.5-9)
- 跟风/份额低/无壁垒/客户弱 → 档内低分 (如热门带跟风7-7.5)
- 重叠区: 强竞争力温热股(7.5)可追平弱竞争力热门股(7) — 竞争力能跨档

**边界判断规则（必须遵守）**：
1. 主业占比校准：公司来自热门赛道的营收占比<30% → chain上限扣1.5分（如消费电子厂兼做AI散热→按消费电子档+1.5，不得直接按热门带高分）
2. 财务交叉验证：声称"核心供应商"但ROE<5%/研发<3% → 上限扣1分（真龙头应有持续投入）
3. 旧赛道但新兴业务：传统主业但有明确高增长新兴业务（如传统铜厂→AI铜箔，需具体产品/客户/产能证据）→ 按新兴业务热度归档，不低于3.0
4. 竞争力交叉验证：声称"龙头/第一"但无具体市占率/份额数据/大客户名 → 不可信, 竞争力分下调（仅凭"国内领先"等空话不算龙头）
5. 热度传导逻辑（见世界知识）纳入判断：如CPO推迟→可插拔光模块热度延续，氮化铝基板→新需求打开

### delivery 业绩兑现度 (0.0-10.0) — 交叉验证财务数据，区分"真兑现"与"画饼"

**档位规则**：
- 8.0-10.0: 顶级大客户(英伟达/谷歌/华为/苹果/特斯拉)+产能扩张+业绩高增(营收增速>30%)
- 5.5-7.9: 有明确客户且已放量(营收增速>15%)，或niche龙头净利率>15%且稳定增长
- 3.0-5.4: 有客户但未放量(营收增速<15%)，或客户集中度高(单一客户>50%)
- 0.0-2.9: 只有概念无订单，或growth_drivers全是"国产替代/一带一路/政策红利"等模板句

**交叉验证规则（必须核对财务数据后给分）**：
1. 利润率红线：净利率<5% → 说明公司是代工/组装模式，即使有英伟达/华为等大客户名也不超过6.0分
2. 增速匹配：声称"业绩高增/爆发"但营收增速<15% → 上限减1.5分（真高增应有数据支撑）
3. 客户名不可信：声称"英伟达供应商"但无具体产品名(如"800G光模块")/份额数据/订单金额 → 减1分（仅凭参加展会/送样测试不算）
4. 模板句检测：growth_drivers只有"国产替代/一带一路/政策红利"等空话，无具体订单/产能/客户名 → 不超过3分
5. ROE校验：ROE>20%且净利率>20%的真龙头 → 若其他条件满足可给到上限；ROE<3% → 上限扣2分
6. 中报窗口加权（6月下旬-7月）：市场对业绩兑现度极度敏感，已预告高增的加分0.3，业绩存疑的从严（从低档）
7. **需求性质判断（关键，必须结合上方注入的世界知识 — 区分结构性景气 vs 投机周期）**：
   现金流红线(cf_to_profit<0/经营现金流为负)是否触发，取决于该股产品的【需求性质】：
   - **结构性持续景气**（世界知识明确该赛道短缺延续多年 + 产品是当前主线/景气赛道的关键输入，如HBM存储/AI光模块/CoWoS/战略金属/存储主控模组等）：现金流为负=扩产备货满足真实爆发需求，是景气向上信号 → **不因现金流扣分**，产能跟上需求者反而可加分。这类"囤货"是待涨的利润蓄水池，不是减值隐患。
   - **投机/泛周期涨价**（蹭热点/产能过剩行业/非主线边缘产品/普通消费电子被动件）：现金流差=滞销或赌方向囤货 → 维持现金流红线扣分 + 标注减值风险。
   判断依据：产品在世界知识中是否被列为【主线/景气赛道关键输入 + 短缺延续多年(如"延续至2028")】。本周期与以往不同：由AI算力/数据中心真实需求驱动，非投机囤货，需据此重新解读现金流。

### capital 资金关注度 (0.0-5.0)
此字段由量化系统计算（板块动量+量价因子），你只需判断LLM视角的板块热度，供交叉参考。量化capital会覆盖此值。
- 4.0-5.0: 最热主线(AI算力/光模块/HBM/铜箔/战略金属)
- 2.5-3.9: 二线热点(AI上游材料/被动元件/国产算力/半导体设备/机器人)
- 1.5-2.4: 温和（消费电子/汽车电子/军工/电力电网）
- 0.0-1.4: 冷门/资金流出（传统行业/消费/白酒/地产/油气）

**sector_score 必须严格等于 chain + delivery + capital 之和（范围0.0-25.0，保留1位小数）。不要另算、不要归一化。** 用小数拉开同档位区分度。旧赛道退潮品种诚实给低分。

## 精华信息（服务30天涨幅竞争辩论，每项≤25字，字段不可重复）

**质量禁则（违反的essence视为无效）**：
- 禁止空话：biggest_bull不得使用"行业景气度高""政策支持""国产替代大趋势"等无具体数据的泛词
- 禁止对仗：biggest_bear不得使用"竞争加剧""宏观不确定性""估值偏高"等任何股票都适用的套话
- 禁止同义重复：bull和bear不能互相矛盾（如bull说"订单饱满"bear说"产能不足"→矛盾），也不能bull=bear换个说法
- 必须具体：bull必须含具体数据（份额%/增速%/价格涨幅/客户名/产能数字），bear必须含具体风险点（库存/解禁/客户流失/技术路线被替代/毛利率下滑）

**字段定义**：
- chain_position: 产业链卡位一句话 (含市占率或排名，如"全球1.6T光模块份额28%第一")
- core_catalyst: 30天内最强上涨催化（仅一条，含时间节点：如"7月中报预告净利润+120%"）
- biggest_bull: 多头最强论据（必须含具体数据，禁止空话）
- biggest_bear: 空头最强攻击点（必须含具体风险点，禁止任何股票都适用的套话）
- quality_redline: 财务质量底线(ROE/净利率/现金流/负债中选最关键的一个数字)
- catalyst_horizon: near(30天内有催化)/mid(1季内)/far(更远或无)

严格输出JSON（essence每个key只出现一次，不要解释）:
{"chain":数,"delivery":数,"capital":数,"sector_score":数,"brief":"40字内理由","essence":{"chain_position":"","core_catalyst":"","biggest_bull":"","biggest_bear":"","quality_redline":"","catalyst_horizon":"near"}}

【推理要求】直接判断，推理控制在100字内：说明chain档位+关键边界判断理由、delivery档位+财务交叉验证结论。不要复述评分规则原文。

股票数据：
"""

ESSENCE_KEYS = ["chain_position", "core_catalyst", "biggest_bull",
                "biggest_bear", "quality_redline", "catalyst_horizon"]

# 世界知识精简版缓存 (进程级, 避免每只股票读一次文件)
_WORLD_KNOWLEDGE_SLIM: str = ""
_WORLD_KNOWLEDGE_DATE: str = ""


def _load_world_knowledge_slim() -> str:
    """加载世界知识精简版 (注入 V3 评分 prompt)。

    从 world_knowledge_2026_06.md 提取与"产业链位置 + 业绩兑现度"
    直接相关的段落: 市场格局 / AI算力主线明细 / 退潮赛道 / 业绩窗口。
    截取到 ~2000 字, 缓存在进程级变量中。

    Returns:
        精简版世界知识文本 (~2000 字), 或空字符串 (文件不存在时)。
    """
    global _WORLD_KNOWLEDGE_SLIM, _WORLD_KNOWLEDGE_DATE
    if _WORLD_KNOWLEDGE_SLIM:
        return _WORLD_KNOWLEDGE_SLIM

    wk_path = paths.WORLD_KNOWLEDGE_MD
    if not os.path.exists(wk_path):
        return ""

    try:
        with open(wk_path, "r", encoding="utf-8") as f:
            full = f.read()
    except Exception:
        return ""

    # 提取更新时间
    for line in full.split("\n")[:5]:
        if "更新时间" in line:
            _WORLD_KNOWLEDGE_DATE = line.strip()
            break

    # 提取关键段落: 只保留与 chain/delivery 评分直接相关的部分
    # 匹配 ## 二级标题段, 跳过不相关的 (如"冷门板块与边缘趋势"只取标题)
    sections = re.split(r"\n(?=## )", full)
    keep_sections = []
    keep_keywords = [
        "市场盘面", "资金特征", "AI算力", "半导体", "主线",
        "退潮", "冷门", "业绩窗口", "投资日历", "中报",
        "新能源.*拐点", "中美贸易", "地缘",
    ]
    skip_keywords = [
        "冷门板块与边缘趋势",  # 只取标题, 不取详情
    ]

    for sec in sections:
        title = sec.split("\n")[0] if sec else ""
        if any(re.search(kw, title) for kw in keep_keywords):
            # 跳过段落改为仅取标题+首段
            if any(kw in title for kw in skip_keywords):
                lines = sec.strip().split("\n")
                # 只保留标题 + 前2行 (够判断是冷门即可)
                keep_sections.append("\n".join(lines[:3]))
            else:
                keep_sections.append(sec)

    # 拼接并截取到 ~2000 字
    slim = "\n\n".join(keep_sections)
    # 进一步压缩: 删除过长段落中中间的行
    if len(slim) > 2500:
        # 保留头尾, 截断中间
        slim = slim[:1200] + "\n\n... (世界知识中段已省略, 完整版见 data/reference/) ...\n\n" + slim[-800:]

    _WORLD_KNOWLEDGE_SLIM = slim[:2500]
    return _WORLD_KNOWLEDGE_SLIM


def _parse(content):
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    m = re.search(r'\{.*\}', text.strip(), re.S)
    if not m:
        return None
    try:
        r = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    # 防御: LLM 返回的 JSON 必须显式包含 chain/delivery 字段。
    # 旧模型(deepseek-v4-pro)曾返回缺字段的 JSON (如只有 essence/sector_score),
    # .get(key, 0) 会静默默认成 0 分写入缓存, 污染排名
    # (实测: 688183 旧模型下被误判 0/0/0 → 从 #19 掉到 #530)。
    # 要求字段必须存在, 否则视为解析失败 (保留旧分/触发重试)。
    if "chain" not in r or "delivery" not in r:
        return None
    try:
        chain = round(float(r.get("chain", 0)), 1)
        delivery = round(float(r.get("delivery", 0)), 1)
        capital = round(float(r.get("capital", 0)), 1)
    except (TypeError, ValueError):
        return None
    # sector_score 一律用子维度求和（权威），防模型加错/归一化
    summed = round(chain + delivery + capital, 1)
    ess = r.get("essence", {}) or {}
    essence = {k: str(ess.get(k, ""))[:40] for k in ESSENCE_KEYS}
    if essence["catalyst_horizon"] not in ("near", "mid", "far"):
        essence["catalyst_horizon"] = "mid"
    return {
        "chain": chain, "delivery": delivery, "capital": capital,
        "sector_score": summed,
        "sector_score_model": (round(float(r["sector_score"]), 1)
                               if isinstance(r.get("sector_score"), (int, float)) else None),
        "brief": str(r.get("brief", ""))[:60],
        "essence": essence,
        "chain_scored_date": datetime.now().strftime("%Y-%m-%d"),
        "delivery_scored_date": datetime.now().strftime("%Y-%m-%d"),
    }


# 归因缓存路径 (新晋股扫描产出, 注入评分 prompt)
ATTR_CACHE = paths.ATTR_CACHE
ATTR_TTL_DAYS = 14  # 归因缓存有效期 (天)


def _check_price_still_supports(code):
    """检查该股当前量价趋势是否仍支持归因 (防止过期归因误导评分)。

    热门赛道波动剧烈，不能用简单的跌幅阈值判断。
    改用"趋势完整性"三条件 (满足任一即认为趋势仍在):
      1. 均线多头: 近5日均价 >= 近20日均价的97% (容许小幅回调)
      2. 高位区间: 当前价 >= 近20日最高价的80% (未深度回调)
      3. 未创新低: 当前价 >= 近20日最低价的105% (未破前低)

    Returns:
        True = 趋势仍支持 (可注入), False = 趋势已破 (应清理)
    """
    try:
        import pickle as _pk
        for suffix in ["_SZ.pkl", "_SH.pkl"]:
            path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    df = _pk.load(f)
                if df is None or len(df) < 21:
                    return True  # K线不足, 不拦截
                df = df.sort_values("trade_date").reset_index(drop=True)
                close = df["close"]
                last = close.iloc[-1]
                ma5 = close.iloc[-5:].mean()
                ma20 = close.iloc[-20:].mean()
                high20 = close.iloc[-20:].max()
                low20 = close.iloc[-20:].min()

                # 条件1: 均线多头 (容许3%偏离)
                if ma5 >= ma20 * 0.97:
                    return True
                # 条件2: 高位区间 (未深度回调, 在最高价80%以上)
                if last >= high20 * 0.80:
                    return True
                # 条件3: 未创新低 (在前低105%以上)
                if last >= low20 * 1.05:
                    return True
                # 三个条件都不满足 → 趋势已破
                return False
        return True  # 无K线, 不拦截
    except Exception:
        return True  # 出错不拦截


def _load_attr_hint(code):
    """从归因缓存读取该股的上涨原因, 作为评分提示注入 prompt。

    三重过期检查:
      1. 缓存日期 TTL (14天)
      2. 该股量价趋势是否仍支持 (均线多头/高位区间/未创新低, 三取一)
      3. 只注入"板块供需/政策催化"类 (个股事件不注入)

    若归因已过期或量价走弱, 返回空字符串 (不注入)。
    """
    if not os.path.exists(ATTR_CACHE):
        return ""
    try:
        cache = json.load(open(ATTR_CACHE))
    except Exception:
        return ""
    entry = cache.get(code)
    if not entry or not entry.get("is_sector_wide"):
        return ""

    # 过期检查 1: 缓存 TTL
    cached_date = entry.get("cached_date", "")
    if cached_date:
        try:
            from datetime import datetime as _dt
            age = (_dt.now() - _dt.strptime(cached_date, "%Y-%m-%d")).days
            if age > ATTR_TTL_DAYS:
                return ""  # 缓存过期, 不注入
        except Exception:
            pass

    # 过期检查 2: 量价是否仍支持
    if not _check_price_still_supports(code):
        return ""  # 股价已走弱, 归因可能过期, 不注入

    tag = entry.get("sector_tag", "")
    summary = entry.get("summary", "")
    reason = entry.get("reason_type", "")
    if not tag and not summary:
        return ""
    return f"\n\n【已确认的板块上涨逻辑 (来自量价+搜索归因, {cached_date}确认)】\n归因类型: {reason}\n细分赛道: {tag}\n核心逻辑: {summary}\n请基于此信息判断产业链位置(chain), 若属于AI算力上游关键材料/元件应给较高chain分。"


# 异动股实时驱动缓存 (治本: chain不单靠可能滞后/漏业务的fundamentals what_they_do,
# 对异动股实时搜"当前在炒什么/跌什么", 纠正传统主业标签盖住新热门暴露的问题)
# 双向: 大涨搜上涨原因(纠正低估), 大跌搜下跌原因(判断基本面恶化vs技术回调)
# 涨跌不对称阈值: 大涨侧设更高(涨>=25%才是真异动), 大跌侧设更低(跌<=-18%就值得关注)
# 同时要求 |r5|>=5% 过滤单日脉冲, 确保趋势有持续性
SURGE_DRIVER_CACHE = os.path.join(paths.DATA_DIR, "caches", "surge_driver_cache.json")
SURGE_UP_THRESHOLD = 25.0       # 大涨异动阈值 (r20 >= 此值)
SURGE_DOWN_THRESHOLD = -18.0    # 大跌异动阈值 (r20 <= 此值)
SURGE_R5_CONFIRM = 5.0          # |r5| > 此值确认非单日脉冲
SURGE_DRIVER_TTL_DAYS = 7       # 驱动缓存有效期
# 全量重评兼容: 每进程搜索上限 (避免544只全量时web search拖垮; 缓存命中不计数)
# 不设跳过开关 — 异动分析对纠正fundamentals滞后很关键, 手动全量也跑 (靠缓存+上限管速度)
_MOVEMENT_SEARCHES_DONE = 0
_MOVEMENT_SEARCH_CAP = int(os.environ.get("SURGE_DRIVER_MAX_SEARCHES", "30"))
# surge cache 内存缓存 (避免544只全量重评时每只都读文件)
_SURGE_CACHE_MEM = None
_SURGE_CACHE_MTIME = 0


def _compute_r20(code):
    """近20日涨幅%, 无K线返回None。"""
    import pickle as _pk
    for suf in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suf}")
        if os.path.exists(p):
            try:
                df = _pk.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                if len(df) >= 21:
                    return round((df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100, 1)
            except Exception:
                pass
    return None


def _compute_r5(code):
    """近5日涨幅%, 无K线返回None。"""
    import pickle as _pk
    for suf in ["_SH.pkl", "_SZ.pkl"]:
        p = os.path.join(KLINE_CACHE_DIR, f"{code}{suf}")
        if os.path.exists(p):
            try:
                df = _pk.load(open(p, "rb")).sort_values("trade_date").reset_index(drop=True)
                if len(df) >= 6:
                    return round((df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100, 1)
            except Exception:
                pass
    return None


def _is_movement_surging(code):
    """异动判断: 涨跌不对称 + r5确认非单日脉冲。

    条件 (同时满足):
      大涨: r20 >= 25%  大跌: r20 <= -18%
      |r5| >= 5% (趋势有持续性, 非单日脉冲)

    Returns (is_surging, r20, r5) or (False, None, None)
    """
    r20 = _compute_r20(code)
    if r20 is None:
        return False, None, None
    # 涨跌不对称判断
    is_surging = r20 >= SURGE_UP_THRESHOLD or r20 <= SURGE_DOWN_THRESHOLD
    if not is_surging:
        return False, r20, None
    # 确认非单日脉冲
    r5 = _compute_r5(code)
    if r5 is None or abs(r5) < SURGE_R5_CONFIRM:
        return False, r20, r5
    return True, r20, r5


def _get_stock_name(code):
    """从 fundamentals 读 name (主/冷目录)。"""
    p = _find_fundamental(code)
    if not p:
        return ""
    try:
        return json.load(open(p)).get("name", "") or code
    except Exception:
        return code


def _search_movement_driver(code, name, direction):
    """web search 异动原因 + LLM 提取核心驱动。direction='上涨'/'下跌'。返回驱动文本或''。"""
    from picker.pipeline.refresh_fundamentals import _web_search
    try:
        raw = _web_search(f"{name} {code} {direction}原因")
    except Exception:
        return ""
    if not raw or len(raw) < 60:
        return ""
    if direction == "上涨":
        task = "近期上涨的核心市场驱动(当前在炒它什么)"
        ex = "AI算力带动特种光纤需求爆发,公司作为光通信龙头受益量价齐升"
    else:
        task = "近期下跌的核心原因"
        ex = "存储价格见顶预期+存货减值风险,主力获利了结"
    prompt = f"""从搜索结果提取这只股票{task}。

股票: {name}({code})
搜索结果:
{raw[:2000]}

第一行就直接输出1句核心原因(如"{ex}"), 50字内。不要推理过程/分析步骤/前言, 直接给结论。"""
    out = _llm(prompt, max_tokens=300)
    if not out:
        return ""
    import re as _re
    lines = [ln.strip() for ln in out.strip().split("\n") if ln.strip()
             and not _re.match(r'^[\d\*\.\-\•]+\s*(分析|目标|要求|提取|步骤|关键|信息|解|结果)', ln.strip())
             and len(ln.strip()) > 8]
    driver = lines[-1] if lines else out.strip()
    driver = _re.sub(r'^[\d\*\.\-\•\s]*(总结|结论|核心|驱动|原因)[\*\：:\s]*', '', driver).strip()
    driver = _re.sub(r'^[\d\*\.\-\•\s]+', '', driver).strip()
    return driver[:150] if driver else ""


def _load_movement_driver(code):
    """对异动股(涨跌不对称+r5确认), 实时web search原因并缓存, 返回注入文本。

    治本: chain评分不单靠fundamentals what_they_do。对异动股额外注入"当前市场驱动":
    - 大涨: 搜索上涨原因, 纠正"传统主业标签盖住新热门"(如中天科技海缆→实际AI光纤)
    - 大跌: 搜索下跌原因, 让LLM判断"基本面恶化(chain/delivery下调) vs 技术回调(维持)"

    异动条件 (涨跌不对称 + 趋势确认):
    - 大涨: r20 >= 25%  大跌: r20 <= -18%
    - |r5| >= 5% (过滤单日脉冲)

    全量重评兼容:
    - 缓存优先 (7d TTL, 命中不搜索) → 日常daily run积累缓存, 全量重评多数命中
    - 搜索上限 (_MOVEMENT_SEARCH_CAP, 默认30/进程) → 全量时超额则跳过, 下次daily再搜
    - V3_SKIP_SURGE_DRIVER=1 → 完全跳过 (手动纯快速全量)
    """
    global _MOVEMENT_SEARCHES_DONE

    is_surging, r20, _r5 = _is_movement_surging(code)
    if not is_surging:
        return ""  # 未异动

    direction = "上涨" if r20 > 0 else "下跌"

    # 缓存优先 (内存缓存, mtime感知 — 避免每只股都读文件)
    global _SURGE_CACHE_MEM, _SURGE_CACHE_MTIME
    cache = _SURGE_CACHE_MEM or {}
    try:
        mt = os.path.getmtime(SURGE_DRIVER_CACHE) if os.path.exists(SURGE_DRIVER_CACHE) else 0
        if mt != _SURGE_CACHE_MTIME:
            cache = json.load(open(SURGE_DRIVER_CACHE)) if mt else {}
            _SURGE_CACHE_MEM = cache
            _SURGE_CACHE_MTIME = mt
    except Exception:
        pass
    entry = cache.get(code)
    if entry:
        try:
            age = (datetime.now() - datetime.strptime(entry["date"], "%Y-%m-%d")).days
            same_dir = entry.get("direction") == direction
            still_moving = entry.get("r20", 0) >= SURGE_UP_THRESHOLD or entry.get("r20", 0) <= SURGE_DOWN_THRESHOLD
            if age <= SURGE_DRIVER_TTL_DAYS and same_dir and still_moving:
                drv = entry.get("driver", "")
                if drv:
                    return _format_movement_injection(drv, direction, entry["r20"], entry["date"])
        except Exception:
            pass

    # 搜索上限检查 (全量兼容: 不让web search拖垮)
    if _MOVEMENT_SEARCHES_DONE >= _MOVEMENT_SEARCH_CAP:
        return ""  # 达上限, 跳过 (下次daily run再搜)

    # 实时搜
    name = _get_stock_name(code)
    _MOVEMENT_SEARCHES_DONE += 1
    driver = _search_movement_driver(code, name, direction)
    if not driver:
        return ""
    cache[code] = {"driver": driver, "date": datetime.now().strftime("%Y-%m-%d"),
                   "r20": r20, "direction": direction}
    _SURGE_CACHE_MEM = cache  # 同步内存缓存
    try:
        json.dump(cache, open(SURGE_DRIVER_CACHE, "w"), ensure_ascii=False, indent=1)
        _SURGE_CACHE_MTIME = os.path.getmtime(SURGE_DRIVER_CACHE)
    except Exception:
        pass
    return _format_movement_injection(driver, direction, r20, datetime.now().strftime("%Y-%m-%d"))


def _format_movement_injection(driver, direction, r20, date):
    """格式化异动驱动注入文本 (区分上涨/下跌的评分指引)。"""
    if direction == "上涨":
        return (f"\n\n【近期市场驱动 (实时搜索, {date}, r20=+{r20}%)】\n{driver}\n"
                f"请优先据此判断该股【当前赛道热度暴露(chain)】, "
                f"即使下方what_they_do未充分体现此业务, 也应按当前驱动归入对应热度档。")
    else:
        return (f"\n\n【近期持续下跌 (实时搜索, {date}, r20={r20}%)】\n下跌原因: {driver}\n"
                f"请判断: 是【基本面恶化/赛道逻辑变化】(chain/delivery应下调) "
                f"还是【技术性回调/获利了结/短期利空】(基本面没变, 维持评分)。"
                f"若基本面实质恶化, delivery/chain须反映; 若仅技术回调, 维持原判。")


# ══════════════════════════════════════════════════════════
# 异动分析: 退场机制 + 维护预填 (供每日维护集成)
# ══════════════════════════════════════════════════════════


def get_surge_driver_for_code(code):
    """读取该股的异动分析结论 (供 fundamentals 生成/刷新两条链路共用)。

    从 surge_driver_cache 读, 7天有效。返回 driver 文本或 ""。
    gen_fundamentals.generate_one 和 refresh_fundamentals.refresh_one 都调本函数,
    保证两条链路的异动注入一致 (全量生成 vs 研报刷新结果连贯)。
    """
    try:
        if not os.path.exists(SURGE_DRIVER_CACHE):
            return ""
        sc = json.load(open(SURGE_DRIVER_CACHE))
        entry = sc.get(code)
        if not entry or not entry.get("driver"):
            return ""
        age = (datetime.now() - datetime.strptime(entry.get("date", "2000-01-01"), "%Y-%m-%d")).days
        if age > SURGE_DRIVER_TTL_DAYS:
            return ""
        return entry["driver"]
    except Exception:
        return ""


def build_surge_fundamentals_section(surge_driver):
    """构建 fundamentals 生成/刷新 prompt 的异动注入段 (共用, 保证两条链路一致)。

    gen_fundamentals 和 refresh_fundamentals 都调本函数 → 注入文案完全一致。
    """
    if not surge_driver:
        return ""
    return f"""
## ⚡ 近期异动分析结论（实时web search）
该股近期有明显异动，市场核心驱动为：{surge_driver}
**重要**: 请在 what_they_do、growth_drivers、strengths 中【充分反映】上述驱动信息。
这是当前市场对该股的真实认知，即使旧文件或行业标签未充分体现，也必须写入。
"""

def _evict_movement_cache(cache, pool_codes=None):
    """退场清理: 删除过期/非池/不再异动的缓存条目。

    退场条件 (满足任一):
      1. >7天旧 (TTL过期, 下次会重新搜)
      2. 非池股票 (移入冷池/退市, 不再跟踪)
      3. 不再异动(涨<25%且跌>-18%) 且 >3天 (趋势可能已结束, 给3天宽限防抖)
    """
    from datetime import timedelta
    pool_set = set(pool_codes) if pool_codes else None
    today = datetime.now()
    evicted = 0
    for code in list(cache.keys()):
        entry = cache.get(code, {})
        try:
            age = (today - datetime.strptime(entry.get("date", "2000-01-01"), "%Y-%m-%d")).days
        except Exception:
            age = 999
        # 退场
        if age > SURGE_DRIVER_TTL_DAYS:
            del cache[code]; evicted += 1; continue
        if pool_set is not None and code not in pool_set:
            del cache[code]; evicted += 1; continue
        r20 = entry.get("r20", 0)
        no_longer_surging = r20 < SURGE_UP_THRESHOLD and r20 > SURGE_DOWN_THRESHOLD
        if no_longer_surging and age > 3:
            del cache[code]; evicted += 1; continue
    return evicted


def precompute_movement_drivers(max_searches=None, verbose=True):
    """维护步骤: 扫描全池异动股, 预填movement driver缓存。

    供每日维护集成 (Step 2.7)。预填后, 评分(_call)直接读缓存(快), 避免inline搜索。
    避免重复: 缓存有效(7d)+方向一致+仍异动 → 跳过。
    退场: 写入前清理过期/非池/不再异动条目。
    异动条件: 涨跌不对称(r20>=25%或<=-18%) + |r5|>=5%趋势确认。
    """
    global _MOVEMENT_SEARCHES_DONE
    if max_searches is None:
        max_searches = _MOVEMENT_SEARCH_CAP

    pool_codes = _list_all_fundamental_codes()
    cache = {}
    if os.path.exists(SURGE_DRIVER_CACHE):
        try:
            cache = json.load(open(SURGE_DRIVER_CACHE))
        except Exception:
            cache = {}

    # 1. 退场清理
    evicted = _evict_movement_cache(cache, pool_codes)

    # 2. 扫描异动股 (涨跌不对称 + r5趋势确认)
    surging = []
    for code in pool_codes:
        is_surging, r20, _ = _is_movement_surging(code)
        if is_surging:
            surging.append((code, r20))
    surging.sort(key=lambda x: -abs(x[1]))  # 按异动幅度降序

    # 3. 预填: 跳过已缓存有效的, 搜索未缓存/过期/方向变的
    searched = 0
    skipped = 0
    for code, r20 in surging:
        direction = "上涨" if r20 > 0 else "下跌"
        entry = cache.get(code)
        if entry:
            try:
                age = (datetime.now() - datetime.strptime(entry["date"], "%Y-%m-%d")).days
                same_dir = entry.get("direction") == direction
                still_surging = entry.get("r20", 0) >= SURGE_UP_THRESHOLD or entry.get("r20", 0) <= SURGE_DOWN_THRESHOLD
                if age <= SURGE_DRIVER_TTL_DAYS and same_dir and still_surging:
                    skipped += 1
                    continue  # 避免重复: 缓存有效
            except Exception:
                pass
        if searched >= max_searches:
            break
        name = _get_stock_name(code)
        driver = _search_movement_driver(code, name, direction)
        if driver:
            cache[code] = {"driver": driver, "date": datetime.now().strftime("%Y-%m-%d"),
                           "r20": r20, "direction": direction}
            searched += 1
            if verbose:
                print(f"    {code} {name[:8]:<8} r20={r20:+.0f}% [{direction}] → {driver[:40]}", flush=True)

    # 4. 写回
    try:
        json.dump(cache, open(SURGE_DRIVER_CACHE, "w"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    _MOVEMENT_SEARCHES_DONE = searched  # 同步计数 (评分时不再重复搜这些)

    if verbose:
        print(f"  [异动分析] 池内异动 {len(surging)} 只 | 新搜 {searched} | 缓存命中跳过 {skipped} | 退场清理 {evicted}", flush=True)
    return {"surging": len(surging), "searched": searched, "skipped": skipped, "evicted": evicted}


def _call(code):
    sj = fs._build_stock_json(code)
    if not sj:
        return code, {"error": "no_fundamentals"}, 0.0
    t0 = time.time()
    # 注入归因提示 (若有)
    attr_hint = _load_attr_hint(code)
    # 注入异动股实时驱动 (双向: 大涨搜上涨原因纠正低估/大跌搜下跌原因判断恶化vs回调)
    surge_driver = _load_movement_driver(code)
    # 注入世界知识 (进程级缓存, 只读一次文件)
    wk_slim = _load_world_knowledge_slim()
    wk_section = ""
    if wk_slim:
        wk_date_line = f" ({_WORLD_KNOWLEDGE_DATE})" if _WORLD_KNOWLEDGE_DATE else ""
        wk_section = f"\n\n【当前市场宏观背景 (来自世界知识{wk_date_line})】\n{wk_slim}\n请将以上宏观背景纳入 chain 产业链位置和 delivery 业绩兑现度的判断, 尤其注意产业链传导逻辑和中报窗口对兑现度的敏感度。"
    # surge_driver 紧跟档位规则 → LLM 看到"热度档系统"+"该股当前实际驱动"相邻, 再读宏观+基本面
    prompt = get_chain_prompt() + surge_driver + wk_section + sj[:8000] + attr_hint
    # 解析失败也重试 (并发下 GLM 偶发返回畸形/截断响应, 非空但解析失败;
    # _llm 只在异常/空内容时重试, 这里对"有内容但解析失败"再给最多3次机会)。
    # 实测: 并发下 ~20% 偶发解析失败, 串行重跑同股可成功 → 解析重试能收敛。
    for _attempt in range(3):
        content = _llm(prompt)
        if not content:
            continue
        parsed = _parse(content)
        if parsed:
            return code, parsed, time.time() - t0
    return code, None, time.time() - t0


# chain/delivery TTL 天数 (超过后自动重评)
CHAIN_TTL_DAYS = 7
DELIVERY_TTL_DAYS = 7


def needs_run(entry):
    """没分 / 缺essence / chain过期 / delivery过期 → 需要跑

    过期检查: chain_scored_date / delivery_scored_date 超过 TTL
    天自动重评。不检查 capital (capital 每日量化更新, 不依赖 LLM)。
    """
    if not entry or "sector_score" not in entry:
        return True, "缺分"
    ess = entry.get("essence")
    if not isinstance(ess, dict):
        return True, "缺essence"
    if any(not ess.get(k) for k in ESSENCE_KEYS):
        return True, "缺essence"

    # TTL 过期检查
    # 注: 旧缓存无 chain_scored_date 字段 → 视为过期 (首次升级后会全量重评一次,
    # 之后正常 TTL 每周只重评少数个股)。这是有意的: 旧缓存用的是弱 prompt + 无世界知识。
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=CHAIN_TTL_DAYS)).strftime("%Y-%m-%d")
    chain_date = entry.get("chain_scored_date", "")
    delivery_date = entry.get("delivery_scored_date", "")

    if not chain_date or chain_date < cutoff:
        return True, "chain过期"
    if not delivery_date or delivery_date < cutoff:
        return True, "delivery过期"

    return False, ""


# ══════════════════════════════════════════════════════════
# capital 动态更新 (每次跑 V3 前调用, 用研报板块热度重算)
# ══════════════════════════════════════════════════════════

# 细分赛道 capital 覆盖表 (方案D: 把大类板块中已降温的细分赛道拆出)
# 优先从 JSON 文件加载 (scan_mispriced.py 可动态更新), 不存在则用默认值
SUB_SECTOR_OVERRIDE_FILE = paths.SUB_SECTOR_OVERRIDE_PATH
_SUB_SECTOR_OVERRIDE_DEFAULT = {
    # 算力基础设施 (IDC/机房/算力租赁) — 光模块暴涨但机房已过剩
    "IDC": 1.5, "算力基础设施": 1.5, "数据中心": 1.5, "算力租赁": 1.5,
    "智算中心": 1.5, "机房": 1.5,
    # AI服务器/整机 — 上游材料更热, 整机环节利润被压
    "服务器": 2.0, "AI服务器": 2.0,
    # 温控/散热整机 — 液冷零件还在涨但整机厂增速放缓
    "温控": 1.8, "精密温控": 1.8,
    # 网络设备/交换机 — 相比光模块热度递减
    "网络设备": 2.0, "交换机": 2.0,
}


def _load_sub_sector_override():
    """加载细分赛道覆盖表 (优先JSON文件, 回退默认值)。"""
    if os.path.exists(SUB_SECTOR_OVERRIDE_FILE):
        try:
            return json.load(open(SUB_SECTOR_OVERRIDE_FILE))
        except Exception:
            pass
    return dict(_SUB_SECTOR_OVERRIDE_DEFAULT)


def _compute_price_factor(code, cutoff_date=""):
    """个股量价趋势因子 (短周期r5 + 长周期r20 双窗口判断)。

    单一 r20 窗口的问题: 无法区分"持续上涨"(健康)和"先涨后跌"(见顶回落)。
    双窗口解决: r5 反映最新方向, r20 反映大趋势, 组合判断更准确。

    | r20趋势 | r5最新 | 判断 | 因子 |
    |---------|--------|------|------|
    | 强(>20%) | 也强(>5%) | 持续主升浪 | 1.3 |
    | 强(>20%) | 回调(<-5%) | 可能见顶回落 | 0.9 ← 关键改进 |
    | 温和(0~20%) | 上行(>0) | 健康上涨 | 1.0~1.2 |
    | 温和(0~20%) | 下行(<0) | 动量衰减 | 0.7~0.9 |
    | 弱(<-10%) | 任何 | 趋势已破 | 0.6 |
    | 回调(-10~0%) | 企稳(>0) | 可能触底 | 0.9 |
    | 回调(-10~0%) | 继续跌 | 持续走弱 | 0.7 |

    Args:
        cutoff_date: 回测截止日。非空时截断 K线到该日再算 r5/r20 (无前视)。
    """
    try:
        import pickle as _pk
        for suffix in ["_SH.pkl", "_SZ.pkl"]:
            path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    df = _pk.load(f)
                if df is None or len(df) < 21:
                    return 1.0
                df = df.sort_values("trade_date").reset_index(drop=True)
                if cutoff_date:
                    df = df[df["trade_date"] <= cutoff_date]
                    if len(df) < 21:
                        return 1.0
                close = df["close"]
                r20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
                r5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0

                # 双窗口组合判断
                if r20 > 20:
                    if r5 > 5:
                        return 1.3   # 持续主升浪
                    elif r5 < -5:
                        return 0.9   # 见顶回落 (关键改进: 不再盲目给1.3)
                    else:
                        return 1.1   # 强势但短期犹豫
                elif r20 > 0:
                    if r5 > 0:
                        return 1.0 + r20 * 0.01  # 健康上涨 1.0~1.2
                    else:
                        return 0.9   # 动量衰减
                elif r20 > -10:
                    if r5 > 0:
                        return 0.9   # 回调中企稳
                    else:
                        return 0.7   # 持续走弱
                else:
                    return 0.6       # 趋势已破
        return 1.0
    except Exception:
        return 1.0


# D2 因子缓存 (G 模式用: 每次更新 capital 时预算一次, 避免逐股重复算行业中位)
_D2_SECTOR_MEDIAN_CACHE: Dict[str, float] = {}


def _compute_d2_factor(code: str, cutoff_date="") -> float:
    """D2 行业相对强度因子: 个股 r20 相对同行业中位数的偏离 → 0.6~1.3。

    G 模式专用。回测验证: 结构性行情(主线明确)时, D2 能区分板块内领涨股。
    实现简化: 用 _D2_SECTOR_MEDIAN_CACHE (由 update_capital 预算), 无缓存则返回 1.0。

    Args:
        cutoff_date: 回测截止日。非空时截断 K线到该日再算 r20。
    """
    try:
        import pickle as _pk
        for suffix in ["_SH.pkl", "_SZ.pkl"]:
            path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    df = _pk.load(f)
                    if df is None or len(df) < 21:
                        return 1.0
                    df = df.sort_values("trade_date").reset_index(drop=True)
                    if cutoff_date:
                        df = df[df["trade_date"] <= cutoff_date]
                        if len(df) < 21:
                            return 1.0
                    close = df["close"]
                    r20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
                    industry = _get_industry(code)
                    sector = _classify_sector(industry)
                    sector_median = _D2_SECTOR_MEDIAN_CACHE.get(sector)
                    if sector_median is None:
                        return 1.0
                    # r20 显著高于行业中位 → 强(>1.0), 显著低于 → 弱(<1.0)
                    if r20 > sector_median + 15:
                        return 1.15
                    elif r20 < sector_median - 10:
                        return 0.85
                    return 1.0
        return 1.0
    except Exception:
        return 1.0


def _classify_sector(industry: str) -> str:
    """用 keyword index 把 industry 归类到标准板块 (D2 用)。

    平局裁决: 命中数相同时, 取命中关键词中"最长"的那个所属板块
    (长关键词更精确, 如"算力芯片"比"半导体"更具体), 仍平则按板块名排序
    (依赖 get_sector_keyword_index 已排序的 key 顺序, 保证跨进程确定性)。
    """
    if not industry:
        return ""
    try:
        from tradingagents.research.normalize import get_sector_keyword_index
        kw_index = get_sector_keyword_index()
    except Exception:
        return ""
    best, best_hit, best_kw_len = "", 0, 0
    for sec, kws in kw_index.items():
        matched = [k for k in kws if k in industry]
        h = len(matched)
        if h <= 0:
            continue
        max_kw_len = max(len(k) for k in matched)
        if h > best_hit or (h == best_hit and max_kw_len > best_kw_len):
            best_hit, best_kw_len, best = h, max_kw_len, sec
    return best


def _build_d2_sector_median_cache(cutoff_date=""):
    """预算所有板块的 r20 中位数 (G 模式: 每次 update_capital 调一次)。

    Args:
        cutoff_date: 回测截止日。非空时截断 K线到该日再算 r20。
    """
    import pickle as _pk
    import statistics as _st
    _D2_SECTOR_MEDIAN_CACHE.clear()
    sector_r20s: Dict[str, list] = {}
    for suffix_pat in ["_SZ.pkl", "_SH.pkl"]:
        import glob as _glob
        for p in _glob.glob(os.path.join(KLINE_CACHE_DIR, f"*{suffix_pat}")):
            code = os.path.basename(p).replace(suffix_pat, "")
            industry = _get_industry(code)
            sector = _classify_sector(industry)
            if not sector:
                continue
            try:
                df = _pk.load(open(p, "rb"))
                if df is None or len(df) < 21:
                    continue
                df = df.sort_values("trade_date").reset_index(drop=True)
                if cutoff_date:
                    df = df[df["trade_date"] <= cutoff_date]
                    if len(df) < 21:
                        continue
                r20 = (df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100
                sector_r20s.setdefault(sector, []).append(r20)
            except Exception:
                continue
    for sec, vals in sector_r20s.items():
        if vals:
            _D2_SECTOR_MEDIAN_CACHE[sec] = _st.median(vals)


def _get_industry(code):
    """从 fundamentals 读 industry 字段 (主目录或冷股目录)。"""
    path = _find_fundamental(code)
    if not path:
        return ""
    try:
        with open(path) as f:
            d = json.load(f)
        return d.get("industry", "") or d.get("business_overview", {}).get("industry", "")
    except Exception:
        return ""


def _compute_capital_from_momentum(sector: str, momentum: dict) -> float:
    """根据板块在研报动量中的位置, 量化计算 capital 分 (0.0-5.0)。

    规则 (与 PROMPT_V3E 的 capital 档位对齐):
      hot_sectors Top1-3:  4.5-5.0 (最热主线)
      hot_sectors Top4-8:  3.5-4.4 (热门)
      hot_sectors Top9+:   2.8-3.4 (温热)
      emerging_sectors:    3.0-3.8 (新兴, 有上升势头)
      中性板块:             2.0-2.7
      cold_sectors:        0.5-1.5 (冷门/被看空)
      无数据:               1.5 (默认中性偏低)
    """
    if not momentum or not sector:
        return 1.5

    hot = momentum.get("hot_sectors", [])
    cold = momentum.get("cold_sectors", [])
    emerging = momentum.get("emerging_sectors", [])

    hot_list = [s["sector"] for s in hot]
    cold_list = [s["sector"] for s in cold]
    emerging_list = [s["sector"] for s in emerging]

    # 热门板块: 按排名递减给分
    if sector in hot_list:
        rank = hot_list.index(sector)
        bull_count = hot[rank].get("bullish_count", 1)
        # Top1-3: 4.5-5.0, Top4-8: 3.5-4.4, Top9+: 2.8-3.4
        if rank < 3:
            return min(5.0, 4.5 + bull_count * 0.05)
        elif rank < 8:
            return min(4.4, 3.5 + bull_count * 0.05)
        else:
            return max(2.8, 3.4 - (rank - 8) * 0.05)

    # 新兴板块
    if sector in emerging_list:
        rank = emerging_list.index(sector)
        return max(3.0, 3.8 - rank * 0.1)

    # 冷门板块
    if sector in cold_list:
        rank = cold_list.index(sector)
        bear_count = cold[rank].get("bearish_count", 1)
        return max(0.5, 1.5 - bear_count * 0.1)

    # 中性
    return 2.0


def compute_capital_updates(mode="D", cutoff_date=""):
    """纯计算: 更新 capital 子维度, 返回更新后的 cache dict (不写文件)。

    供选股流程 (analysts.collect_data) 调用 — 只算不写, 避免文件竞争。
    update_capital() 会调用本函数再落盘。

    Args:
        cutoff_date: 回测截止日。非空时 pf/d2 按 cutoff 截断 K线重算 (量价无前视);
                     base_capital 仍用当前板块 momentum 快照 (研报无可靠历史版, 故不重算)。
                     为空时 (实盘) 全部用最新数据。
    """
    if not os.path.exists(V3_CACHE):
        return None
    try:
        cache = json.load(open(V3_CACHE))
    except Exception:
        return None

    try:
        from tradingagents.research.consumer import get_sector_momentum
        # base 不随 cutoff 重算 (研报无可靠历史版); pf/d2 随 cutoff 截断
        momentum = get_sector_momentum(days=14)
    except Exception:
        return None

    if not momentum.get("hot_sectors"):
        return None

    try:
        from tradingagents.research.normalize import get_sector_keyword_index
        kw_index = get_sector_keyword_index()
    except Exception:
        return None

    def classify(industry):
        # 平局裁决同 _classify_sector: 命中数相同时取命中关键词最长的板块,
        # 保证跨进程确定性 (get_sector_keyword_index 已按板块名排序)。
        if not industry:
            return ""
        best, best_hit, best_kw_len = "", 0, 0
        for sec, kws in kw_index.items():
            matched = [k for k in kws if k in industry]
            h = len(matched)
            if h <= 0:
                continue
            max_kw_len = max(len(k) for k in matched)
            if h > best_hit or (h == best_hit and max_kw_len > best_kw_len):
                best_hit, best_kw_len, best = h, max_kw_len, sec
        return best

    override = _load_sub_sector_override() if mode in ("D", "d") else {}
    override_sorted = sorted(override.items(), key=lambda x: -len(x[0]))

    # G 模式需要预算行业 r20 中位数缓存 (供 _compute_d2_factor 用)
    if mode in ("G", "g"):
        _build_d2_sector_median_cache(cutoff_date=cutoff_date)

    updated = 0
    for code, entry in cache.items():
        if not isinstance(entry, dict) or "chain" not in entry:
            continue
        industry = _get_industry(code)
        sector = classify(industry)
        if not sector:
            continue

        base_capital = _compute_capital_from_momentum(sector, momentum)
        if override_sorted:
            for keyword, cap_val in override_sorted:
                if keyword in industry:
                    base_capital = cap_val
                    break

        if mode in ("B", "D", "b", "d"):
            # 旧公式: base × price_factor (r5/r20 双窗口)
            price_factor = _compute_price_factor(code, cutoff_date=cutoff_date)
            new_capital = round(max(0, min(5.0, base_capital * price_factor)), 1)
        elif mode in ("G", "g"):
            # G 公式: base + D2(行业相对强度)×2 + price_factor×2
            # 回测验证: 结构性行情(主线明确)时优于 A, 研报充分时切换到此模式
            # 无封顶 (min(8.0) 会砍平 21% 的热门主升浪股, 与温和上涨股拿相同 capital;
            #         回测: 无封顶 TOP10涨 +2.06pp, 最差期改善, Spearman 仅微降 -0.003)
            price_factor = _compute_price_factor(code, cutoff_date=cutoff_date)
            d2_factor = _compute_d2_factor(code, cutoff_date=cutoff_date)
            new_capital = round(max(0, base_capital + d2_factor * 2 + price_factor * 2), 1)
        else:
            # A 公式 (默认): 纯 base_capital, 不乘 price_factor
            # 回测验证: 50cutoff 上 ρ 最优, TOP10 均涨高于 base×pf, 逻辑最简
            new_capital = round(base_capital, 1)

        old_capital = entry.get("capital", 0)
        if abs(new_capital - old_capital) >= 0.2:
            entry["capital"] = new_capital
            entry["capital_updated_date"] = datetime.now().strftime("%Y-%m-%d")
            entry["sector_score"] = round(
                entry.get("chain", 0) + entry.get("delivery", 0) + new_capital, 1
            )
            updated += 1

    return cache, updated, momentum


def update_capital(mode="D", persist=True, cutoff_date=""):
    """更新 capital 子维度。

    Args:
        mode: B/D/A 计算模式 (默认D)
        persist: True=写文件(全量评分用), False=只返回不写(选股流程用)
        cutoff_date: 回测截止日。非空时 pf/d2 按 cutoff 截断 K线重算;
                     base_capital 仍用当前 momentum 快照 (研报无可靠历史版)。
    """
    result = compute_capital_updates(mode, cutoff_date=cutoff_date)
    if result is None:
        print("  [capital] 数据不足, 跳过")
        return None

    cache, updated, momentum = result
    total = len([v for v in cache.values() if isinstance(v, dict) and "chain" in v])

    if persist:
        json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)

    hot_names = [s["sector"] for s in momentum.get("hot_sectors", [])[:5]]
    print(f"  [capital] 模式{mode} | 更新 {updated} 只 | 热门: {hot_names}"
          f"{' (已落盘)' if persist else ' (仅内存)'}")
    return cache


# ══════════════════════════════════════════════════════════
# 过热股检测 (高分但量价走弱 → 搜索验证 + 风险标记, 不自动惩罚)
# ══════════════════════════════════════════════════════════

# 过热股风险验证缓存 (避免每日重复搜索)
OVERHEATED_CACHE = paths.OVERHEATED_CACHE
OVERHEATED_TTL_DAYS = 7  # 风险验证缓存有效期


def detect_overheated(cache):
    """检测高分滞涨股, 搜索验证后分类标记 (不自动惩罚 V3 分)。

    与新晋股逻辑镜像互补:
      新晋股 = 低分但涨得好 (被低估) → scan_mispriced 发现 + 保送
      过热股 = 高分但持续下跌 (可能被高估) → 本函数发现 + 搜索验证

    设计原则 (基于实测验证):
      不直接打惩罚! 6 只样本中 5 只 chain 判断准确, 下跌原因各异:
        - 技术性回调 (基本面没变) → capital 的 price_factor 已反映, 无需额外处理
        - 特定风险 (解禁/商誉/融资流出) → 搜索发现后写入风险缓存
        - chain 高估 (行业逻辑变化) → 标记需要重评
      统一 -15% 惩罚会误杀正常回调。

    三重过滤 (筛选候选):
      1. chain >= 6.0  (基本面看似强)
      2. r20 < -5%     (近20日明显下跌)
      3. r5 < 0        (近5日仍在跌)

    处理方式: 只读 cache (不改 V3 分), 输出告警 + 写入风险缓存。
    风险缓存 (.overheated_risk_cache.json) 供后续查询, 后续可接入辩论作为空头证据。
    """
    import pickle as _pk

    # 加载风险缓存 (避免重复搜索)
    risk_cache = {}
    if os.path.exists(OVERHEATED_CACHE):
        try:
            risk_cache = json.load(open(OVERHEATED_CACHE))
        except Exception:
            pass
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=OVERHEATED_TTL_DAYS)).strftime("%Y-%m-%d")

    candidates = []
    for code, entry in cache.items():
        if not isinstance(entry, dict) or entry.get("chain", 0) < 6.0:
            continue
        if entry.get("sector_score", 0) < 12.0:
            continue

        df = None
        for suffix in ["_SH.pkl", "_SZ.pkl"]:
            path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        df = _pk.load(f)
                except Exception:
                    pass
                break
        if df is None or len(df) < 21:
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        close = df["close"]
        r20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
        r5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0

        if r20 < -5 and r5 < 0:
            name = ""
            industry = ""
            try:
                with open(_find_fundamental(code)) as f:
                    d = json.load(f)
                name = d.get("name", "")
                industry = d.get("industry", "") or d.get("business_overview", {}).get("industry", "")
            except Exception:
                pass
            candidates.append({
                "code": code, "name": name, "chain": entry.get("chain", 0),
                "score": entry.get("sector_score", 0), "r20": r20, "r5": r5,
                "industry": industry,
            })

    if not candidates:
        print(f"  [过热检测] 无高分滞涨股")
        return cache

    # 对候选股做搜索验证 (只搜未缓存或过期的)
    print(f"  [过热检测] {len(candidates)} 只候选, 搜索验证中...")
    new_risks = []
    for c in candidates:
        cached = risk_cache.get(c["code"])
        if cached and cached.get("verified_date", "") >= cutoff_date:
            # 缓存有效, 直接用
            risk_type = cached.get("risk_type", "未知")
            summary = cached.get("summary", "")
            print(f"    {c['code']} {c['name']:10} [{risk_type}](缓存) r20={c['r20']:+.0f}% | {summary[:40]}")
        else:
            # 需要搜索验证
            risk_info = _verify_overheated_stock(c["code"], c["name"], c["industry"])
            risk_cache[c["code"]] = {
                **risk_info,
                "verified_date": datetime.now().strftime("%Y-%m-%d"),
                "name": c["name"],
                "chain": c["chain"],
                "r20": c["r20"],
                "r5": c["r5"],
            }
            new_risks.append((c, risk_info))
            print(f"    {c['code']} {c['name']:10} [{risk_info['risk_type']}](新搜) r20={c['r20']:+.0f}% | {risk_info['summary'][:40]}")

    # 保存风险缓存
    json.dump(risk_cache, open(OVERHEATED_CACHE, "w"), ensure_ascii=False, indent=1)

    # chain高估 → 将该股 chain_scored_date 置为过期 (触发 LLM 重评)
    chain_overvalued = 0
    for c in candidates:
        ri = risk_cache.get(c["code"], {})
        if ri.get("risk_type") == "chain高估":
            entry = cache.get(c["code"], {})
            if isinstance(entry, dict):
                entry["chain_scored_date"] = "2000-01-01"  # 强制过期, 立即重评
                chain_overvalued += 1
    if chain_overvalued:
        print(f"  [过热检测] {chain_overvalued} 只标记 chain高估, 已触发 LLM 重评", flush=True)

    return cache


def _verify_overheated_stock(code, name, industry):
    """搜索验证过热股的下跌原因, 分类标记。

    Returns:
        {risk_type, summary}
        risk_type: 技术回调 / 特定风险 / chain高估 / 未知
    """
    try:
        from picker.discovery.scan_mispriced import web_search, _llm_quick
    except Exception:
        return {"risk_type": "未知", "summary": "搜索模块不可用"}

    query = f"{name} {code} 股价下跌原因 2026年6月"
    search_text = web_search(query)

    if not search_text or len(search_text) < 50:
        return {"risk_type": "未知", "summary": "搜索无结果"}

    prompt = f"""你是A股研究员。这只股票近期下跌, 请判断下跌原因类型。

股票: {name}({code}) 行业: {industry}

搜索结果:
{search_text[:1500]}

请判断下跌属于哪种类型, 用|分隔输出:
TYPE|技术回调 或 特定风险 或 chain高估 或 未知
SUMMARY|30字内一句话原因

类型定义:
- 技术回调: 基本面没变, 只是涨幅过大后的正常回调/获利回吐/高管减持等
- 特定风险: 有具体的负面事件 (解禁/商誉减值/融资流出/政策变化/业绩不及预期)
- chain高估: 行业逻辑发生变化, 之前的产业链定位不再成立 (如技术路线被淘汰/市场风格切换)
- 未知: 无法判断"""

    result = _llm_quick(prompt)
    parsed = {"risk_type": "未知", "summary": ""}
    for line in result.strip().split("\n"):
        line = line.strip()
        if line.startswith("TYPE|"):
            parsed["risk_type"] = line.split("|", 1)[1].strip()
        elif line.startswith("SUMMARY|"):
            parsed["summary"] = line.split("|", 1)[1].strip()
    return parsed


def main():
    # capital 模式: D=拆分+量价(默认, 最精准), B=板块×量价(零维护), A=纯板块
    capital_mode = os.environ.get("CAPITAL_MODE", "D")

    # ── capital 动态更新 (每次跑 V3 前先更新, 0次LLM调用) ──
    print("═" * 60)
    print(f"Step 0: 更新 capital 子维度 (模式{capital_mode})")
    print("═" * 60)
    update_capital(mode=capital_mode)
    print()

    codes = sorted(f[:-5] for f in os.listdir(FUNDAMENTALS_DIR) if f.endswith(".json"))
    cache = {}
    if os.path.exists(V3_CACHE):
        try:
            cache = json.load(open(V3_CACHE))
        except Exception:
            cache = {}

    # needs_run 现在返回 (bool, reason); 统计各原因
    todo_all = [(c, *needs_run(cache.get(c))) for c in codes]
    todo = [c for c, need, _reason in todo_all if need]
    # 统计细分
    reason_counts = {"缺分": 0, "缺essence": 0, "chain过期": 0, "delivery过期": 0}
    for _c, _need, _reason in todo_all:
        if _need and _reason in reason_counts:
            reason_counts[_reason] += 1
    parts = [f"{v}只{k}" for k, v in reason_counts.items() if v > 0]
    reason_summary = " / ".join(parts) if parts else ""
    print(f"全量 {len(codes)} 只 | 已完整 {len(codes)-len(todo)} | 待跑 {len(todo)}  {f'({reason_summary})' if reason_summary else ''}", flush=True)

    MAX_WORKERS = int(os.environ.get("V3_WORKERS", "8"))
    lock = threading.Lock()
    done = [0]
    fail = [0]
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_call, c): c for c in todo}
        for fut in as_completed(futures):
            code, r, dt = fut.result()
            with lock:
                done[0] += 1
                n = done[0]
                if not r:
                    fail[0] += 1
                    print(f"[{n}/{len(todo)}] {code} 失败/解析失败 ({dt:.0f}s)", flush=True)
                    continue
                cache[code] = r
                json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)
                if "sector_score" not in r:
                    print(f"[{n}/{len(todo)}] {code} 无fundamentals", flush=True)
                    continue
                el = time.time() - t_start
                eta = (len(todo) - n) / max(n / el, 0.001)
                ess = r["essence"]
                print(f"[{n}/{len(todo)}] {code} V3={r['sector_score']:>4.1f} "
                      f"[{r['chain']}+{r['delivery']}+{r['capital']}] {ess['catalyst_horizon']} "
                      f"{dt:.0f}s ETA{eta/60:.0f}m | {ess['core_catalyst']}", flush=True)

    el = time.time() - t_start
    print(f"\n完成: 成功 {done[0]-fail[0]}, 失败 {fail[0]}, 耗时 {el/60:.1f}m", flush=True)

    # Top50 榜单
    scored = [(c, v) for c, v in cache.items() if "sector_score" in v]
    scored.sort(key=lambda x: -x[1]["sector_score"])
    print(f"\n{'='*70}\n  阶段一产出：基本面排序 Top50\n{'='*70}", flush=True)
    for i, (code, v) in enumerate(scored[:50], 1):
        name = ""
        try:
            name = json.load(open(os.path.join(FUNDAMENTALS_DIR, f"{code}.json"))).get("name", "")
        except Exception:
            pass
        print(f"  {i:>2}. {code} {name:<8} {v['sector_score']:>4.1f} "
              f"{v['essence']['catalyst_horizon']:<4} | {v['essence']['core_catalyst']}", flush=True)


if __name__ == "__main__":
    main()
