#!/usr/bin/env python3
"""
全量 V3 打分 + 基本面精华信息（阶段一：基本面排序）

一次 LLM 调用同时产出：
  1. 赛道动量三子维度小数分（chain/surge/capital → sector_score 求和）
  2. 基本面精华信息 essence（服务下游30天涨幅竞争辩论）

工程保障：8线程并发、逐只落盘加锁、断点续跑、失败不落盘自动重试。
缓存：.fundamental_v3_scores.json（复用，已缓存但缺 essence 的会重跑补齐）

Prompt 升级 (2026-06):
  - chain/surge 边界判断规则 + 财务交叉验证 + 反例参考
  - essence 质量禁则 (禁止空话/对仗/同义重复)
  - 世界知识注入 (市场格局 + AI算力主线 + 退潮赛道 + 中报窗口)
  - chain/surge 每交易日盘后全量重评 (TTL=1 次日兜底; capital 每日量化, 不依赖 LLM)

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

from picker.scoring import fundamentals_loader as fs
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

# LLM 客户端下沉到 picker.common.llm_client (re-export 保持向后兼容:
#   外部 `from picker.scoring.v3_full_score import _llm` 仍可用)
from picker.common.llm_client import _llm  # noqa: F401

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
6. 异动回流优先：若 what_they_do/growth_drivers/strengths 中含【近期异动驱动】信息（研报归因写入，见"⚡近期异动分析结论"段），应优先据此判断当前赛道热度暴露(chain)，覆盖传统主业标签——异动信息已通过 fundamentals 回流，反映当前市场真实认知

### surge 爆发分 (0.0-10.0) — 30天内股价超额收益概率预估

**爆发分 ≠ growth_score。** growth_score = 公司业务1-3年能否高速成长（中长期潜力，不看股价）；**爆发分 = 这种成长性能否在未来30天内兑现为超额收益（变现概率+时机）**。判别核心=成长动能是否处于【加速拐点】×【催化是否落在30天内可验证】。成长性高但已稳定/已price-in→低分；成长性中等但刚加速+有近端硬催化→高分。先读 growth_drivers/what_they_do（含⚡异动回流段）里的【加速/拐点/订单/产能/价格/份额/预告】信号，据此同时产出 surge 与 essence.core_catalyst（两者必须同源同向）。**price-in 锚定（必读）**：评分前先读注入的【surge 锚定数据】(多窗口涨幅+动量状态) 与估值段(板块相对分位+PEG)——这是判断"利好涨幅已释放多少/后续还剩多少"的硬数据依据；据此权衡"已涨幅(price-in压力) vs 当前加速度(动量支撑) vs 估值(板块分位+PEG, 非绝对PE/PB)"，涨幅本身不机械扣分。

**surge档位规则（4档，核心维度=加速度证据×催化近度）**：
- **8.0-10.0 加速主升档**：growth_drivers含明确【加速拐点】（环比提速/渗透率破临界/订单逐季加速/稼动率快速爬坡/价格拐头向上/产能30天内投产节点）+ 催化在30天内可验证（near：财报预增公告日期/订单交付节点/招标中标公示/投产点火/政策细则/新品发布，须含具体日期或窗口如"7月15日""Q2财报季"）+ 财务交叉印证（营收增速环比上行或毛利率改善）+ 尚未被充分price-in。可给满分。
- **5.5-7.9 温和加速档**：成长性扎实+环比改善趋势确立，但加速不够剧烈 或 催化在1季内（mid）或 催化已有部分被预期。
- **3.0-5.4 平稳/钝化档**：成长性良好但动能平稳（无加速迹象，维持既定增速）或 催化遥远（far）或 锚定数据显示已充分price-in（60日/90日/自低点涨幅均显著 且 momentum_state∈{decelerating,top_reversal}，同时 PE/PB 处板块80分位+ 且 PEG>2 成长消化不掉）。最常见档位。
- **0.0-2.9 失速/虚假加速档**：成长性向下（环比恶化/订单延迟/产能过剩/价格战）或 drivers全是模板空话无落点（删掉公司名对任何同业都成立）或"加速"仅存在于叙事但财务反向。

**交叉验证规则（给分前逐一核对）**：
1. **加速度>绝对水平（灵魂）**：无基本面加速证据 → 即便growth_score=9也上限7.9。**判断"是否在加速"看 drivers + essence.core_catalyst 的实质内容（有无具体数字/环比/订单量/产能投产节点的加速表述），不要做词面 substring 匹配**——词面有无"环比/加速"等词不决定档位，看的是有没有可核验的加速事实："受益于AI需求增长/景气延续"=泛涨≠加速证据（→平稳档）；"Q2订单环比+40%"/"产能爬坡至90%"/"6月量产"/"渗透率从15%升至30%"=加速（可上8+）。配合 essence.catalyst_horizon=near 加权（近端催化本身是加速变现的硬证据）。
2. **催化日期硬要求**：高分档（≥5.5）每条催化必须含具体时间节点（日期或窗口）。只有"有望/预计/持续推进/逐步放量"等无锚点词 → 上限4.5。essence.catalyst_horizon=far 且drivers无30天可验证事件 → 上限5.4。
3. **传导落点检测**：drivers停在"AI算力景气/国产替代大趋势/政策红利"无具体落点（无订单/产能/客户名/份额/价格/稼动率）→ 不构成加速证据，无落点者不超过3分。
4. **客户实证**：声称大客户但无具体产品名(如"800G光模块")+份额/订单金额 → 扣1分（送样测试≠已锁定）。虚假客户=催化不存在=变现概率归零。
5. **财报窗口加权（A股最强短期催化）**：窗口期（1月年报预告/4月一季报/7月中报/10月三季报）有明确预告且超预期→大幅加分（核心得分点）；业绩存疑/无预告→从严；非窗口看订单/产能/政策催化。
6. **需求性质×现金流**：cf_to_profit<0时——若产品属结构性持续景气（世界知识列为主线/景气赛道关键输入+短缺延续多年：HBM/AI光模块/CoWoS/战略金属/存储主控/国产GPU核心环节）→现金流差=扩产备货待涨，不扣分甚至加权；若属投机/泛周期/非主线边缘品→现金流差=赌方向囤货，扣分（减值隐患）。
7. **price-in 锚定判断（档内修正，用【surge 锚定数据】含估值板块分位+PEG 权衡，非机械硬规则）**：
   - 三类权衡公式：已涨幅（price-in压力：60日/90日/自低点涨幅越大越压制）× 当前加速度（动量支撑：momentum_state=accelerating 或 r5 仍强则抵消压力）× 估值（看**板块分位+PEG**，非绝对PE/PB：PE板块低分位=便宜即使绝对值高、PEG<1=成长能消化不算贵、PEG>2=偏贵）。
   - **关键反例（防误杀高增周期股）**：PE绝对值看似高（如52）但板块0分位（最便宜）且 PEG<1（成长能消化）→ 不算贵，不得据此压分；叠加 momentum_state=accelerating + 30天内有硬催化 → 仍可给档内上沿甚至上一档（"涨多"不等于"涨完"，超级周期股右侧加速能持续）。
   - **真该压 price-in 的**：涨幅大 + momentum_state∈{decelerating,top_reversal} + 估值已贵（PE/PB 板块80分位+ 且 PEG>2）+ 催化遥远（far）→ 档内下沿。
   - 不机械扣分原则不变：主升浪初期资金介入是预期差修复信号；近期才被认知的拐点（自低点涨幅小但加速明确）给档内上沿。

**杠杆方向修正**：低净利率(<5%)不设硬上限。净利率持续下行/营收增但利润不增（量增价跌伪成长）→ 扣1.5；利润率处拐点（毛利率高位+净利率从负转正/低回升的扭亏故事）→ 不扣分甚至加权（30天弹性最大品种之一）。

**防共线自检（给分前强制）**：若爆发分与 growth_score 差值<1.0（如growth8.5→爆发8.0）→ 必须重新审视是否只复制了成长性判断而忽略时机。反例A：growth=8.5+30天内有中报预增→爆发8.5；growth=8.5+空窗期无近端催化→爆发3.0（同成长性差5.5）。反例B：growth=9.5远期AI算力龙头但30天纯空窗→爆发3.5；growth=6.5传统主业但30天内产能投产+订单公示双催化→爆发6.0。**price-in 维度追加自检**：若 surge≥7.0 但锚定数据显示 momentum_state∈{decelerating,top_reversal} 且 60日涨幅>50% → 必须重新审视是否高估了剩余涨幅（典型：已大涨+动量衰减+催化空窗的伪主升浪）；反之涨幅巨大但 momentum_state=accelerating 的高分须有近端硬催化支撑，否则视为资金惯性而非基本面加速。

### capital 资金关注度 (0.0-5.0)
此字段由量化系统计算（板块动量+量价因子），你只需判断LLM视角的板块热度，供交叉参考。量化capital会覆盖此值。
- 4.0-5.0: 最热主线(AI算力/光模块/HBM/铜箔/战略金属)
- 2.5-3.9: 二线热点(AI上游材料/被动元件/国产算力/半导体设备/机器人)
- 1.5-2.4: 温和（消费电子/汽车电子/军工/电力电网）
- 0.0-1.4: 冷门/资金流出（传统行业/消费/白酒/地产/油气）

**sector_score 必须严格等于 chain + surge + capital 之和（范围0.0-25.0，保留1位小数）。不要另算、不要归一化。** 用小数拉开同档位区分度。旧赛道退潮品种诚实给低分。

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
{"chain":数,"surge":数,"capital":数,"sector_score":数,"brief":"40字内理由","essence":{"chain_position":"","core_catalyst":"","biggest_bull":"","biggest_bear":"","quality_redline":"","catalyst_horizon":"near"}}

【推理要求】直接判断，推理控制在100字内：说明chain档位+关键边界判断理由、surge档位+财务交叉验证结论。不要复述评分规则原文。

股票数据：
"""

ESSENCE_KEYS = ["chain_position", "core_catalyst", "biggest_bull",
                "biggest_bear", "quality_redline", "catalyst_horizon"]

# 世界知识精简版缓存 (进程级, 避免每只股票读一次文件)
_WORLD_KNOWLEDGE_SLIM: str = ""
_WORLD_KNOWLEDGE_DATE: str = ""


def _load_world_knowledge_slim() -> str:
    """加载世界知识精简版 (注入 V3 评分 prompt)。

    从 world_knowledge_2026_06.md 提取与"产业链位置 + 爆发分(催化变现)"
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

    # 提取关键段落: 只保留与 chain/surge 评分直接相关的部分
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
    # 防御: LLM 返回的 JSON 必须显式包含 chain/surge 字段。
    # 旧模型(deepseek-v4-pro)曾返回缺字段的 JSON (如只有 essence/sector_score),
    # .get(key, 0) 会静默默认成 0 分写入缓存, 污染排名
    # (实测: 688183 旧模型下被误判 0/0/0 → 从 #19 掉到 #530)。
    # 要求字段必须存在, 否则视为解析失败 (保留旧分/触发重试)。
    if "chain" not in r or "surge" not in r:
        return None
    try:
        chain = round(float(r.get("chain", 0)), 1)
        surge = round(float(r.get("surge", 0)), 1)
        capital = round(float(r.get("capital", 0)), 1)
    except (TypeError, ValueError):
        return None
    # sector_score 一律用子维度求和（权威），防模型加错/归一化
    summed = round(chain + surge + capital, 1)
    ess = r.get("essence", {}) or {}
    essence = {k: str(ess.get(k, ""))[:40] for k in ESSENCE_KEYS}
    if essence["catalyst_horizon"] not in ("near", "mid", "far"):
        essence["catalyst_horizon"] = "mid"
    return {
        "chain": chain, "surge": surge, "capital": capital,
        "sector_score": summed,
        "sector_score_model": (round(float(r["sector_score"]), 1)
                               if isinstance(r.get("sector_score"), (int, float)) else None),
        "brief": str(r.get("brief", ""))[:60],
        "essence": essence,
        "chain_scored_date": datetime.now().strftime("%Y-%m-%d"),
        "surge_scored_date": datetime.now().strftime("%Y-%m-%d"),
    }


_SECTOR_VAL_CACHE = None       # {sector: {"pes":[sorted], "pbs":[sorted]}}
_SECTOR_VAL_CODE2SEC = {}      # {code: sector} (用 stock_basic 行业归类, 与快照同源)
_SECTOR_VAL_DATE = ""

# 业绩预告进程级缓存 (当日级失效, 避免每只股读盘; 磁盘源 forecast_fetcher.precompute_pool_forecast 每日刷新)
_FORECAST_CACHE = None         # {code: [rec]}
_FORECAST_DATE = ""


def _current_sector(code, industry=None):
    """个股【当前驱动板块】: 优先用异动归因 sector_tag, 回退 _classify_sector(industry)。

    解决多主业股被 industry 字段带偏: 中天科技 industry="通信/电力设备(海缆/电缆/储能)"
    → _classify_sector 命中"电力/电网"(错), 但其异动归因 sector_tag="MPO光纤/海缆"(当前真实驱动)
    → "光通信/AI算力"(对)。归因只对异动股(r20>25%)有, 非异动股回退 industry —— 不异动的股
    也用不上"当前驱动", industry 即可。空壳归因(summary空)视为无效, 回退。
    """
    try:
        from picker.discovery.attribution import get_attribution_for_code
        attr = get_attribution_for_code(code)
        # 有效归因: summary 非空(防空壳) + 非"概念炒作"(炒作类 sector_tag 不可靠, 会把股错路由到概念板块)
        if attr and attr.get("summary") and attr.get("reason_type") != "概念炒作":
            sec = _classify_sector(attr.get("sector_tag", ""))
            if sec:
                return sec
    except Exception:
        pass
    return _classify_sector(industry if industry is not None else _get_industry(code))


def _load_forecast_cache():
    """读 FORECAST_CACHE (当日级进程缓存)。返回 {code: [rec]} 或 {}。

    磁盘源由 forecast_fetcher.precompute_pool_forecast 每日维护刷新; 评分进程当日复用,
    读失败时置空并标记当日, 避免反复重读 (容错: 无预告则 _compute_forecast_signals 返回 None)。
    """
    global _FORECAST_CACHE, _FORECAST_DATE
    today = datetime.now().strftime("%Y-%m-%d")
    if _FORECAST_CACHE is not None and _FORECAST_DATE == today:
        return _FORECAST_CACHE
    try:
        from picker.paths import FORECAST_CACHE as _FC_PATH
        if os.path.exists(_FC_PATH):
            data = json.load(open(_FC_PATH, encoding="utf-8"))
            _FORECAST_CACHE = data.get("forecasts", {}) or {}
            _FORECAST_DATE = data.get("_updated", "")
            return _FORECAST_CACHE
    except Exception:
        pass
    _FORECAST_CACHE = {}
    _FORECAST_DATE = today
    return _FORECAST_CACHE


def _compute_forecast_signals(code):
    """读该股近期业绩预告 (FORECAST_CACHE) → surge 催化硬数据。返回 dict 或 None。

    业绩预告带公告日期 + 预增幅度 + 预告类型, 直接满足 surge "催化日期硬要求"
    (PROMPT_V3E 高分档催化必须含具体日期, LLM 原本只能从研报文本猜, 现在有真日历)。
    """
    cache = _load_forecast_cache()
    recs = cache.get(code) or cache.get(str(code).zfill(6))
    if not recs:
        return None
    rec = recs[0]  # 公告日期最近的一条 (fetch 时已跨报告期/多指标去重)
    days_since = None
    try:
        days_since = (datetime.now() - datetime.strptime(rec.get("notice_date", "2000-01-01"), "%Y-%m-%d")).days
    except Exception:
        pass
    return {
        "type": rec.get("type", ""),
        "change_pct": rec.get("change_pct"),
        "notice_date": rec.get("notice_date", ""),
        "summary": rec.get("summary", ""),
        "days_since_notice": days_since,
    }


def _load_sector_valuation(force=False):
    """每日一次: fundamentals 池(rich industry 精细归类) + 市场快照 PE/PB → 按板块分组排序。

    用 fundamentals 的 rich industry 归类 (stock_basic 粗类如"半导体"无法精细分),
    PE/PB 从全市场 daily_basic 快照按 ts_code 查。peer 集 = fundamentals 热股池(同板块热门股)。
    进程级缓存(当日复用)。拉取失败返回旧缓存(若有)。返回 by_sector dict 或 None。
    """
    global _SECTOR_VAL_CACHE, _SECTOR_VAL_CODE2SEC, _SECTOR_VAL_DATE
    today = datetime.now().strftime("%Y-%m-%d")
    if _SECTOR_VAL_CACHE is not None and _SECTOR_VAL_DATE == today and not force:
        return _SECTOR_VAL_CACHE
    from picker.data.fundamentals_data import fetch_market_valuation_snapshot, _code_to_ts_code
    import glob
    snap = fetch_market_valuation_snapshot()
    if not snap:
        _SECTOR_VAL_DATE = today  # 失败也标记当日, 避免全池评分反复 fetch (同 _load_forecast_cache 容错)
        return _SECTOR_VAL_CACHE
    by_sec, code2sec = {}, {}
    for fp in glob.glob(os.path.join(FUNDAMENTALS_DIR, "*.json")):
        code = os.path.basename(fp).replace(".json", "")
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        ind = (d.get("business_overview", {}) or {}).get("industry", "") or ""
        sec = _current_sector(code, ind)  # attribution 优先: 多主业股归当前驱动板块(中天科技→光通信非电力)
        if not sec:
            continue
        sv = snap.get(_code_to_ts_code(code))
        if not sv:
            continue
        code2sec[code] = sec
        bucket = by_sec.setdefault(sec, {"pes": [], "pbs": []})
        pe, pb = sv.get("pe_ttm"), sv.get("pb")
        if pe and pe > 0:
            bucket["pes"].append(pe)
        if pb and pb > 0:
            bucket["pbs"].append(pb)
    for sec in by_sec:
        by_sec[sec]["pes"].sort()
        by_sec[sec]["pbs"].sort()
    _SECTOR_VAL_CACHE, _SECTOR_VAL_CODE2SEC, _SECTOR_VAL_DATE = by_sec, code2sec, today
    return by_sec


def _sector_percentile(code, pe, pb):
    """板块相对估值分位。用快照里该股自身的 stock_basic 行业归类(与 peer 同源, 保证一致)。

    返回 {sector, pe_pct, pb_pct, pe_median, pb_median, pe_n} 或 None(无快照/股不在快照)。
    分位=板块内比它低的占比 (0=板块最低/最便宜, 50=中位, 100=最高/最贵)。
    """
    by_sec = _load_sector_valuation()
    if not by_sec:
        return None
    sector = _SECTOR_VAL_CODE2SEC.get(str(code))
    if not sector or sector not in by_sec:
        return None
    sv = by_sec[sector]
    import bisect
    import statistics as st
    def _pct(lst, val):
        return round(bisect.bisect_left(lst, val) / len(lst) * 100) if (val is not None and lst) else None
    return {
        "sector": sector,
        "pe_pct": _pct(sv.get("pes", []), pe),
        "pb_pct": _pct(sv.get("pbs", []), pb),
        "pe_median": round(st.median(sv["pes"]), 1) if sv.get("pes") else None,
        "pb_median": round(st.median(sv["pbs"]), 1) if sv.get("pbs") else None,
        "pe_n": len(sv.get("pes", [])),
    }


def _compute_valuation_signals(code):
    """surge 估值锚定: PE/PB 的板块相对分位 + PEG(增长消化)。读 fundamentals key_metrics。

    返回 {pe_ttm, pb, netprofit_yoy, peg, sector, pe_pct, pb_pct, pe_median, pb_median} 或 None。
    PEG=PE/净利同比: <1 便宜(成長能消化), >2 偏贵; 负增长/亏损时不计算。
    """
    fund_path = os.path.join(FUNDAMENTALS_DIR, f"{code}.json")
    if not os.path.exists(fund_path):
        return None
    try:
        d = json.load(open(fund_path, encoding="utf-8"))
    except Exception:
        return None
    km = (d.get("financial_health") or {}).get("key_metrics") or {}
    pe, pb, g = km.get("pe_ttm"), km.get("pb"), km.get("netprofit_yoy")
    ps = km.get("ps_ttm")
    high_pe = pe is not None and pe > 200  # PE>200=当前微利/亏损边缘, PEG 会失真(分母极小, 任何增速都给巨大PEG)
    peg = round(pe / g, 2) if (pe and g and g > 0 and not high_pe) else None
    sp = _sector_percentile(code, pe, pb) or {}
    if not any([pe, pb, peg, sp]):
        return None
    return {
        "pe_ttm": pe, "pb": pb, "ps_ttm": ps, "netprofit_yoy": g, "peg": peg, "high_pe": high_pe,
        "sector": sp.get("sector"), "pe_pct": sp.get("pe_pct"), "pb_pct": sp.get("pb_pct"),
        "pe_median": sp.get("pe_median"), "pb_median": sp.get("pb_median"),
    }


def _render_surge_signals_block(sig, val_sig=None, forecast_sig=None):
    """把 surge 锚定数据渲染为 LLM 一眼可读段: 价格水位+动量 (sig) + 估值 (val_sig) + 业绩预告 (forecast_sig)。

    估值用板块相对分位+PEG (非绝对 PE/PB), 让 price-in 判断不误伤高增周期股
    (德明利 PE52 看似贵, 实为板块0分位最便宜+PEG0.55)。业绩预告带公告日期+预增幅度,
    直接满足 surge "催化日期硬要求"。任一为 None 时对应部分省略。
    """
    if not sig and not val_sig and not forecast_sig:
        return ""
    lines = []
    if sig:
        r = sig.get("returns", {})
        state_zh = {
            "accelerating": "加速主升(短期仍强)",
            "strong_steady": "强势整理(大涨但短期犹豫)",
            "top_reversal": "见顶回落警示(大涨后急跌)",
            "healthy_uptrend": "健康上行",
            "decelerating": "动量衰减",
            "bottoming": "筑底企稳",
            "weak": "持续走弱",
            "trend_broken": "趋势已破",
        }.get(sig.get("momentum_state"), sig.get("momentum_state", ""))

        def _fmt(v):
            return f"{v:+.0f}%" if isinstance(v, (int, float)) else "N/A"

        fl = r.get("from_low")
        fl_str = (f"自{r.get('low_date', '?')}低点 {_fmt(fl)}" if fl is not None else "自低点 N/A")
        lines.append(f"【surge 锚定数据 (截至 {sig.get('trade_date', '?')}, K线实测 非叙事)】")
        lines.append(f"价格水位: 近5日 {_fmt(r.get('r5'))} / 20日 {_fmt(r.get('r20'))} / "
                     f"60日 {_fmt(r.get('r60'))} / 90日 {_fmt(r.get('r90'))} / {fl_str} | "
                     f"最新收盘 {sig.get('latest_close')}")
        lines.append(f"动量状态: {state_zh} (涨幅不机械扣分, 权衡'已涨幅price-in压力 vs 加速度动量支撑')")
    if val_sig:
        lines.append("【估值水位 (板块相对分位 + 增长消化PEG, 非绝对PE/PB)】")
        sect = val_sig.get("sector") or "未归类"
        pe, pb = val_sig.get("pe_ttm"), val_sig.get("pb")
        pe_pct, pb_pct = val_sig.get("pe_pct"), val_sig.get("pb_pct")
        pe_m, pb_m = val_sig.get("pe_median"), val_sig.get("pb_median")

        def _tag(pct):
            return "板块最便宜" if pct is not None and pct <= 10 else ("板块偏贵" if pct is not None and pct >= 80 else "")
        pe_line = f"PE {pe}" + (f" → 板块{pe_pct:.0f}分位(中位{pe_m}, {_tag(pe_pct)})" if pe_pct is not None else "")
        pb_line = f"PB {pb}" + (f" → 板块{pb_pct:.0f}分位(中位{pb_m}, {_tag(pb_pct)})" if pb_pct is not None else "")
        lines.append(f"{sect}: {pe_line} | {pb_line}")
        peg, g = val_sig.get("peg"), val_sig.get("netprofit_yoy")
        if val_sig.get("high_pe"):
            ps = val_sig.get("ps_ttm")
            ps_str = f"PS={ps}、" if ps else ""
            lines.append(f"PE {pe} 极高(当前微利/亏损边缘, 远期产能/订单定价为主) → PEG不适用, 看{ps_str}营收增速/在手订单而非PE")
        elif peg is not None and g is not None:
            tag = "便宜(成长能消化估值)" if peg < 1 else ("偏贵(增速消化不掉)" if peg > 2 else "中性")
            lines.append(f"PEG={peg} (PE / 净利增速{g:.0f}%) → {tag}")
        elif g is not None and g <= 0:
            lines.append(f"净利同比 {g:.0f}% (负增长, PEG 不适用, 盈利承压)")
        lines.append("判估值贵否须看板块分位+PEG, 勿用绝对PE/PB (高增周期股PE天生高, 如PE52可能实为板块最便宜)。")
    if forecast_sig:
        lines.append("【业绩预告 (硬催化, 含公告日期 — 直接满足'催化日期硬要求')】")
        ftype = forecast_sig.get("type", "?")
        chg = forecast_sig.get("change_pct")
        chg_str = f"{chg:+.0f}%" if isinstance(chg, (int, float)) else "未披露幅度"
        dsn = forecast_sig.get("days_since_notice")
        dsn_str = f" ({dsn}天前公告)" if dsn is not None else ""
        lines.append(f"预告类型: {ftype} | 业绩变动幅度: {chg_str} | 公告日期: {forecast_sig.get('notice_date', '?')}{dsn_str}")
        lines.append(f"业绩变动: {forecast_sig.get('summary', '?')}")
        lines.append("带日期的硬催化: 预增/扭亏+近期公告 → surge 加速主升档强证据; 预减/续亏/增亏 → 失速风险。")
    return "\n\n" + "\n".join(lines) + "\n"


def _call(code):
    sj = fs._build_stock_json(code)
    if not sj:
        return code, {"error": "no_fundamentals"}, 0.0
    t0 = time.time()
    # 注入世界知识 (进程级缓存)。异动信息已通过 fundamentals JSON 回流 (refresh_one 经
    # attribution 写入 what_they_do/growth_drivers), 评分不再 inline 搜异动, 直接从文本读。
    wk_slim = _load_world_knowledge_slim()
    wk_section = ""
    if wk_slim:
        wk_date_line = f" ({_WORLD_KNOWLEDGE_DATE})" if _WORLD_KNOWLEDGE_DATE else ""
        wk_section = f"\n\n【当前市场宏观背景 (来自世界知识{wk_date_line})】\n{wk_slim}\n请将以上宏观背景纳入 chain 产业链位置和 surge 爆发分(30天超额收益概率)的判断, 尤其注意催化时间节点(中报窗口/订单交付节点)对30天变现概率的影响, 以及需求性质(结构性景气vs投机周期)对现金流红线的解读。"
    # surge price-in 锚定数据: 价格/动量 (K线) + 估值板块分位/PEG (Tushare)
    surge_sig = _compute_surge_signals(code)
    val_sig = _compute_valuation_signals(code)
    forecast_sig = _compute_forecast_signals(code)
    surge_block = _render_surge_signals_block(surge_sig, val_sig, forecast_sig)
    # chain 信号: chain_tier_map + 世界知识 + fundamentals JSON(含异动回流) + surge 锚定段
    prompt = get_chain_prompt() + wk_section + sj[:8000] + surge_block
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


# chain/surge 重评节奏: 每交易日盘后全量重评 (TTL=1 即次日视为需重评, step9/main 每日覆盖全池)
CHAIN_TTL_DAYS = 1
SURGE_TTL_DAYS = 1


def needs_run(entry):
    """没分 / 缺essence / chain过期 / surge过期 → 需要跑

    v3 每交易日盘后全量重评: TTL=1 即 chain/surge 次日视为过期,
    step9_rescore/main 每日覆盖全池。不检查 capital (capital 每日量化更新, 不依赖 LLM)。
    """
    if not entry or "sector_score" not in entry:
        return True, "缺分"
    ess = entry.get("essence")
    if not isinstance(ess, dict):
        return True, "缺essence"
    if any(not ess.get(k) for k in ESSENCE_KEYS):
        return True, "缺essence"

    # TTL 过期检查 (TTL=1: 次日即视为需重评 → 每交易日盘后全量重评)
    # 注: 旧缓存无 chain_scored_date 字段 → 视为过期 (首次升级会全量重评一次)。
    # 旧缓存用的是弱 prompt + 无世界知识, 故首跑必全量重评一次。
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=CHAIN_TTL_DAYS)).strftime("%Y-%m-%d")
    chain_date = entry.get("chain_scored_date", "")
    surge_date = entry.get("surge_scored_date", "")

    if not chain_date or chain_date <= cutoff:
        return True, "chain过期"
    if not surge_date or surge_date <= cutoff:
        return True, "surge过期"

    return False, ""


# ══════════════════════════════════════════════════════════
# capital 动态更新 (每次跑 V3 前调用, 用研报板块热度重算)
# ══════════════════════════════════════════════════════════

def _load_kline_df(code, cutoff_date=""):
    """加载个股 K线 DataFrame, cutoff 截断, 按 trade_date 升序。

    _compute_price_factor / _compute_d2_factor / _compute_surge_signals 共用,
    消除重复读 pkl + 截断逻辑。返回 df 或 None(无缓存/过短/截断后过短/异常)。
    """
    try:
        import pickle as _pk
        for suffix in ["_SH.pkl", "_SZ.pkl"]:
            path = os.path.join(KLINE_CACHE_DIR, f"{code}{suffix}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    df = _pk.load(f)
                if df is None or len(df) < 21:
                    return None
                df = df.sort_values("trade_date").reset_index(drop=True)
                if cutoff_date:
                    df = df[df["trade_date"] <= cutoff_date]
                    if len(df) < 21:
                        return None
                return df
        return None
    except Exception:
        return None


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
    # K线加载+cutoff 截断下沉 _load_kline_df (与 d2/surge 共用); 返回 None=用默认 1.0
    df = _load_kline_df(code, cutoff_date)
    if df is None:
        return 1.0
    try:
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


def _compute_surge_signals(code, cutoff_date=""):
    """surge 评分的 price-in / 剩余涨幅锚定数据 (三类信号: 涨幅幅度 + 动量状态之源)。

    与 _compute_price_factor 同源 (共用 _load_kline_df + cutoff 截断), 但返回结构化
    多字段供 surge LLM 推理; 不影响 capital (capital 仍用 _compute_price_factor 的 float)。
    cutoff_date 非空时截断 K线到该日 (回测无前视, 与 _compute_price_factor 同范式)。

    返回:
      {"returns": {"r5","r20","r60","r90","from_low","low_date"},
       "momentum_state": accelerating|strong_steady|top_reversal|healthy_uptrend|
                         decelerating|bottoming|weak|trend_broken,
       "latest_close": float, "trade_date": str}  或 None (数据不足)。

    自低点用「近12个月最低」(≈250交易日), 非全局最低 —— 全局最低对老股失真,
    与 surge 30 天前瞻尺度不匹配; 近12月能反映本轮主升浪起点且对近期走势敏感。
    """
    df = _load_kline_df(code, cutoff_date)
    if df is None:
        return None
    try:
        close = df["close"]
        n = len(close)
        latest = float(close.iloc[-1])
        trade_date = str(df["trade_date"].iloc[-1])

        def _ret(days):
            # close[-1]/close[-(days+1)] - 1, 转百分比; 窗口不足返回 None
            if n >= days + 1:
                return round((latest / float(close.iloc[-(days + 1)]) - 1) * 100, 1)
            return None

        r5 = _ret(5)
        r20 = _ret(20)
        r60 = _ret(60)
        r90 = _ret(90)

        # 自低点: 近 12 个月 (≈250 交易日) 最低
        window = df.tail(250)
        low_pos = int(window["close"].values.argmin())
        low_close = float(window["close"].iloc[low_pos])
        low_date = str(window["trade_date"].iloc[low_pos])
        from_low = round((latest / low_close - 1) * 100, 0) if low_close > 0 else None

        # momentum_state: 与 _compute_price_factor 双窗口表同构, 但输出语义标签 (LLM 直接读懂)
        r20v = r20 if r20 is not None else 0
        r5v = r5 if r5 is not None else 0
        if r20v > 20:
            if r5v > 5:
                state = "accelerating"      # 持续主升浪 + 短期仍强 (001309 当前态)
            elif r5v < -5:
                state = "top_reversal"      # 大涨后急跌 (见顶回落风险)
            else:
                state = "strong_steady"     # 大涨但短期犹豫
        elif r20v > 0:
            state = "healthy_uptrend" if r5v > 0 else "decelerating"
        elif r20v > -10:
            state = "bottoming" if r5v > 0 else "weak"
        else:
            state = "trend_broken"

        return {
            "returns": {"r5": r5, "r20": r20, "r60": r60, "r90": r90,
                        "from_low": from_low, "low_date": low_date},
            "momentum_state": state,
            "latest_close": latest,
            "trade_date": trade_date,
        }
    except Exception:
        return None


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
                    sector = _current_sector(code, industry)  # attribution 优先 (多主业股归当前驱动板块)
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
            sector = _current_sector(code, industry)  # attribution 优先 (与 D2/capital/valuation 一致)
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


def compute_capital_updates(cutoff_date=""):
    """纯计算: 更新 capital 子维度 (G 模式: base+d2×2+pf×2 无封顶), 返回更新后的 cache dict (不写文件)。

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

    # 预算行业 r20 中位数缓存 (供 _compute_d2_factor 用, G 模式必需)
    _build_d2_sector_median_cache(cutoff_date=cutoff_date)

    updated = 0
    for code, entry in cache.items():
        if not isinstance(entry, dict) or "chain" not in entry:
            continue
        industry = _get_industry(code)
        sector = _current_sector(code, industry)  # attribution 优先 (多主业股归当前驱动板块, 与 D2/valuation 一致)
        if not sector:
            continue

        base_capital = _compute_capital_from_momentum(sector, momentum)
        # G 公式 (唯一模式): base + D2(行业相对强度)×2 + price_factor×2, 无封顶
        # 无封顶的理由: 封顶会砍平热门主升浪股, 与温和上涨股拿相同 capital → 区分度丧失
        #   (回测: 无封顶 TOP10涨 +2.06pp, 最差期改善, Spearman 仅微降 -0.003)
        price_factor = _compute_price_factor(code, cutoff_date=cutoff_date)
        d2_factor = _compute_d2_factor(code, cutoff_date=cutoff_date)
        new_capital = round(max(0, base_capital + d2_factor * 2 + price_factor * 2), 1)

        old_capital = entry.get("capital", 0)
        if abs(new_capital - old_capital) >= 0.2:
            entry["capital"] = new_capital
            entry["capital_updated_date"] = datetime.now().strftime("%Y-%m-%d")
            entry["sector_score"] = round(
                entry.get("chain", 0) + entry.get("surge", 0) + new_capital, 1
            )
            updated += 1

    return cache, updated, momentum


def update_capital(persist=True, cutoff_date=""):
    """更新 capital 子维度 (G 模式: base+d2×2+pf×2 无封顶)。

    Args:
        persist: True=写文件(全量评分用), False=只返回不写(选股流程用)
        cutoff_date: 回测截止日。非空时 pf/d2 按 cutoff 截断 K线重算;
                     base_capital 仍用当前 momentum 快照 (研报无可靠历史版)。
    """
    result = compute_capital_updates(cutoff_date=cutoff_date)
    if result is None:
        print("  [capital] 数据不足, 跳过")
        return None

    cache, updated, momentum = result
    total = len([v for v in cache.values() if isinstance(v, dict) and "chain" in v])

    if persist:
        json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)

    hot_names = [s["sector"] for s in momentum.get("hot_sectors", [])[:5]]
    print(f"  [capital] G模式(base+d2×2+pf×2 无封顶) | 更新 {updated} 只 | 热门: {hot_names}"
          f"{' (已落盘)' if persist else ' (仅内存)'}")
    return cache


def main():
    # ── capital 动态更新 (每次跑 V3 前先更新, 0次LLM调用) ──
    print("═" * 60)
    print("Step 0: 更新 capital 子维度 (G 模式)")
    print("═" * 60)
    update_capital()
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
    reason_counts = {"缺分": 0, "缺essence": 0, "chain过期": 0, "surge过期": 0}
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
                # 保留 Step0 的 quant capital: r 里的 capital 是 LLM 占位值(0-5, prompt 告知会被量化覆盖),
                # 整条覆盖会冲掉 Step0 算好的 G模式 quant capital(实测 688012 被覆盖 9.6→3.8)。
                preserved_cap = cache.get(code, {}).get("capital")
                cache[code] = r
                if preserved_cap is not None:
                    cache[code]["capital"] = preserved_cap
                    # sector_score 重算 (_parse 用占位 capital 算的, 须用 quant 重算)
                    cache[code]["sector_score"] = round(
                        r.get("chain", 0) + r.get("surge", 0) + preserved_cap, 1)
                json.dump(cache, open(V3_CACHE, "w"), ensure_ascii=False, indent=1)
                if "sector_score" not in r:
                    print(f"[{n}/{len(todo)}] {code} 无fundamentals", flush=True)
                    continue
                el = time.time() - t_start
                eta = (len(todo) - n) / max(n / el, 0.001)
                ess = r["essence"]
                print(f"[{n}/{len(todo)}] {code} V3={r['sector_score']:>4.1f} "
                      f"[{r['chain']}+{r['surge']}+{r['capital']}] {ess['catalyst_horizon']} "
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
