#!/usr/bin/env python3
"""
全量 V3 打分 + 基本面精华信息（阶段一：基本面排序）

一次 LLM 调用同时产出：
  1. 赛道动量三子维度小数分（chain/delivery/capital → sector_score 求和）
  2. 基本面精华信息 essence（服务下游30天涨幅竞争辩论）

工程保障：8线程并发、逐只落盘加锁、断点续跑、失败不落盘自动重试。
缓存：.fundamental_v3_scores.json（复用，已缓存但缺 essence 的会重跑补齐）

⚠️ 前视偏差：fundamentals 快照含最新已兑现叙事。本榜单用于【当前选股】是合理的
   （就是要用最新认知选未来），但不可再用历史涨幅自证。
"""
import os, sys, json, re, time, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# 项目根加进 sys.path (兼容从子目录直接运行 + 部分遗留裸 import)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(override=True)

from picker.scoring import fundamental_scorer as fs
from picker import paths

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


def _llm(prompt):
    """调用 LLM, 带自动重试 (针对并发连接层瞬时错误)。

    原实现 except Exception: return None 会吞掉所有错误, 导致并发时
    连接超时/重置被静默丢弃。这里改为: 瞬时错误重试最多 3 次, 仍失败才返回 None。
    """
    last_err = None
    for attempt in range(3):
        try:
            resp = _client().chat.completions.create(
                model=_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=2048, timeout=120,
            )
            msg = resp.choices[0].message
            content = (msg.content or "") or (getattr(msg, "reasoning_content", "") or "")
            if content:
                return content
            last_err = "empty content"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            # 瞬时错误: 短退避后重试
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    # 3 次都失败, 记录原因便于排查 (不再静默吞掉)
    if last_err:
        print(f"    [LLM] 放弃: {last_err}", flush=True)
    return None

PROMPT_V3E = """你是A股量化研究员，对股票赛道动量评分并提炼30天涨幅辩论精华。

## 赛道动量评分（三子维度，各保留1位小数）
- chain 产业链位置(0.0-10.0): AI算力最核心(1.6T光模块/HBM/CoWoS/AI主芯片)→8.5-10.0；AI上游关键材料/元件(电子特气/CMP/HBM前驱体/钨/光刻胶/MLCC高端粉体/TLVR电感/AI铜箔)→7.0-8.4；次核心(PCB/铜连接/液冷/光芯片/空芯光纤)→6.0-6.9；半导体设备材料→5.0-5.9；消费电子/汽车电子→3.0-4.9；产业链外独立成长→1.0-2.9；旧赛道退潮(锂电/白酒/地产/传统矿业)→0.0
- delivery 业绩兑现度(0.0-10.0): 顶级大客户(英伟达/谷歌/华为/苹果)+产能扩张+业绩高增兑现→8.0-10.0；有客户业绩放量→5.5-7.9；有客户未放量→3.0-5.4；只有概念无订单→0.0-2.9
- capital 资金关注度(0.0-5.0): 最热主线(AI算力/光模块/HBM)→4.0-5.0；二线(AI上游材料/被动元件/国产算力/半导体设备/机器人)→2.5-3.9；消费电子/汽车电子→1.5-2.4；冷门→0.0-1.4

**sector_score 必须严格等于 chain + delivery + capital 之和（范围0.0-25.0，保留1位小数）。不要另算、不要归一化。** 用小数拉开同档位区分度。旧赛道退潮品种诚实给低分。

## 精华信息（服务30天涨幅竞争辩论，每项≤25字，字段不可重复）
- chain_position: 产业链卡位一句话
- core_catalyst: 30天内最强上涨催化（仅一条）
- biggest_bull: 多头最强论据
- biggest_bear: 空头最强攻击点
- quality_redline: 财务质量底线(ROE/净利率/健康度)
- catalyst_horizon: near(30天内有催化)/mid(1季内)/far(更远或无)

严格输出JSON（essence每个key只出现一次，不要解释）:
{"chain":数,"delivery":数,"capital":数,"sector_score":数,"brief":"40字内理由","essence":{"chain_position":"","core_catalyst":"","biggest_bull":"","biggest_bear":"","quality_redline":"","catalyst_horizon":"near"}}

【推理要求】直接判断，推理控制在80字内：仅说明产业链档位归属和兑现度档位，不要复述评分规则原文，不要逐字数essence字数。

股票数据：
"""

ESSENCE_KEYS = ["chain_position", "core_catalyst", "biggest_bull",
                "biggest_bear", "quality_redline", "catalyst_horizon"]


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


def _call(code):
    sj = fs._build_stock_json(code)
    if not sj:
        return code, {"error": "no_fundamentals"}, 0.0
    t0 = time.time()
    # 注入归因提示 (若有)
    attr_hint = _load_attr_hint(code)
    content = _llm(PROMPT_V3E + sj[:8000] + attr_hint)
    return code, _parse(content), time.time() - t0


def needs_run(entry):
    """没分 或 有分但缺完整essence → 需要跑"""
    if not entry or "sector_score" not in entry:
        return True
    ess = entry.get("essence")
    if not isinstance(ess, dict):
        return True
    return any(not ess.get(k) for k in ESSENCE_KEYS)


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


def _compute_price_factor(code):
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


def compute_capital_updates(mode="D"):
    """纯计算: 更新 capital 子维度, 返回更新后的 cache dict (不写文件)。

    供选股流程 (analysts.collect_data) 调用 — 只算不写, 避免文件竞争。
    update_capital() 会调用本函数再落盘。
    """
    if not os.path.exists(V3_CACHE):
        return None
    try:
        cache = json.load(open(V3_CACHE))
    except Exception:
        return None

    try:
        from tradingagents.research.consumer import get_sector_momentum
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
        if not industry:
            return ""
        best, best_hit = "", 0
        for sec, kws in kw_index.items():
            h = sum(1 for k in kws if k in industry)
            if h > best_hit:
                best_hit, best = h, sec
        return best

    override = _load_sub_sector_override() if mode in ("D", "d") else {}
    override_sorted = sorted(override.items(), key=lambda x: -len(x[0]))

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
            price_factor = _compute_price_factor(code)
            new_capital = round(max(0, min(5.0, base_capital * price_factor)), 1)
        else:
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


def update_capital(mode="D", persist=True):
    """更新 capital 子维度。

    Args:
        mode: B/D/A 计算模式 (默认D)
        persist: True=写文件(全量评分用), False=只返回不写(选股流程用)
    """
    result = compute_capital_updates(mode)
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

    todo = [c for c in codes if needs_run(cache.get(c))]
    print(f"全量 {len(codes)} 只 | 已完整(含essence) {len(codes)-len(todo)} | 待跑 {len(todo)}", flush=True)

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
