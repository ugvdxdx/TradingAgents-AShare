
import json
import re
import time
import random
import os
import requests
from datetime import datetime
from typing import Dict, List, Optional, Any

from picker import paths
from picker.knowledge.world_knowledge import BUSINESS_WORLD_KNOWLEDGE, get_business_intelligence

# 导入类型
Dict = dict
List = list

# StockAPI 行业数据缓存
_STOCKAPI_INDUSTRY_MAP = None

def _get_stockapi_token():
    """获取 StockAPI token，优先环境变量，其次 .env 文件"""
    token = os.environ.get("STOCKAPI_TOKEN", "")
    if not token:
        env_path = os.path.join(paths.PROJECT_ROOT, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "STOCKAPI_TOKEN":
                            token = v.strip()
                            os.environ["STOCKAPI_TOKEN"] = token
                            break
    return token

def _load_stockapi_industry_map():
    """从 StockAPI 加载全 A 股行业数据，缓存到本地"""
    global _STOCKAPI_INDUSTRY_MAP
    
    if _STOCKAPI_INDUSTRY_MAP is not None:
        return _STOCKAPI_INDUSTRY_MAP
    
    cache_path = os.path.join(paths.PROJECT_ROOT, ".cache", "stockapi_industry.json")
    
    # 先尝试从本地缓存读取
    cached_data = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
        except:
            pass
    
    # 缓存有效（7天内）直接用
    if cached_data:
        try:
            mtime = os.path.getmtime(cache_path)
            if time.time() - mtime < 7 * 86400:
                _STOCKAPI_INDUSTRY_MAP = cached_data
                return _STOCKAPI_INDUSTRY_MAP
        except:
            pass
    
    # 尝试从 StockAPI 拉取
    token = _get_stockapi_token()
    if token:
        try:
            r = requests.get("https://www.stockapi.com.cn/v1/base/all",
                             params={"token": token}, timeout=15)
            d = r.json()
            if d.get("code") == 20000 and d.get("data"):
                _STOCKAPI_INDUSTRY_MAP = {}
                for item in d["data"]:
                    code = str(item.get("api_code", ""))
                    gl = item.get("gl", "")
                    # gl 格式: "行业,子行业,..." 取前两个层级
                    parts = [p for p in gl.split(",") if p]
                    if len(parts) >= 2:
                        industry = parts[0] + "-" + parts[1]
                    elif len(parts) == 1:
                        industry = parts[0]
                    else:
                        continue
                    if code:
                        _STOCKAPI_INDUSTRY_MAP[code] = industry
                # 保存到本地缓存
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(_STOCKAPI_INDUSTRY_MAP, f, ensure_ascii=False)
                return _STOCKAPI_INDUSTRY_MAP
        except:
            pass
    
    # API 拉取失败，回退到旧缓存（即使过期）
    if cached_data:
        _STOCKAPI_INDUSTRY_MAP = cached_data
        return _STOCKAPI_INDUSTRY_MAP
    
    _STOCKAPI_INDUSTRY_MAP = {}
    return _STOCKAPI_INDUSTRY_MAP

# 请求头配置
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0",
]

def _fetch_baike_summary(name: str) -> str:
    """从百科获取企业摘要"""
    try:
        time.sleep(1 + random.random() * 2)
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        url = f"https://baike.baidu.com/item/{name}"
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            html = response.text
            if name not in html[:2000]:
                return ""
            match = re.search(r'<meta name="description" content="([^"]+)"', html)
            if match:
                desc = match.group(1)[:500]
                if "百度百科" not in desc[:50]:
                    return desc
        return ""
    except:
        return ""


def _extract_from_baike(baike_desc: str) -> Dict:
    """从百科描述中提取结构化信息"""
    result = {
        "industry_position": "",
        "strengths": [],
        "keywords": [],
    }
    
    if not baike_desc:
        return result
    
    position_keywords = [
        ("龙头", "行业龙头"),
        ("领先", "行业领先"),
        ("最大", "规模最大"),
        ("第一", "行业第一"),
        ("前三", "行业前三"),
        ("知名", "知名企业"),
        ("上市", "上市公司"),
        ("国有", "国有企业"),
        ("央企", "中央企业"),
    ]
    
    for keyword, desc in position_keywords:
        if keyword in baike_desc:
            result["industry_position"] += desc + "、"
            result["strengths"].append(f"{desc}地位")
    
    return result


def get_business_profile(code: str, name: str) -> Dict:
    """获取业务画像"""
    profile_path = f"profiles/{code}.json"
    profile = None
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except:
        pass
    
    # 如果缓存中行业有效（非"其他"），直接返回
    if profile and profile.get("industry") and profile["industry"] != "其他":
        return profile

    # 行业映射
    industry_map = {
        # 银行
        "000001": "金融银行",
        "601288": "金融银行",
        "601398": "金融银行",
        "601988": "金融银行",
        "600036": "金融银行",
        "601166": "金融银行",
        "601658": "金融银行",
        "600000": "金融银行",
        "601998": "金融银行",
        
        # 保险
        "601318": "金融保险",
        "601628": "金融保险",
        "601319": "金融保险",
        
        # 石油石化
        "600028": "石油石化",
        "601857": "石油石化",
        
        # 食品饮料
        "600519": "食品饮料",
        "000858": "食品饮料",
        
        # AI芯片/半导体
        "688981": "AI芯片",
        "688256": "AI芯片",
        "688041": "AI芯片",
        "300308": "AI芯片",
        "603986": "AI芯片",
        "688008": "AI芯片",
        "688012": "AI芯片",
        
        # 锂电池/新能源
        "300750": "锂电池",
        "300274": "新能源",
        
        # 煤炭/能源
        "601088": "煤炭",
        "601225": "煤炭",
        
        # 电力
        "600900": "公用事业",
        
        # 矿业
        "601899": "矿业",
        "603993": "矿业",
        
        # 医药
        "600276": "医药生物",
        "603259": "医药生物",
        
        # 券商
        "600030": "金融券商",
        "300059": "金融券商",
        
        # 汽车
        "002594": "汽车整车",
        "603211": "汽车零部件",
        "000581": "汽车零部件",
        
        # 电子/通信
        "601138": "消费电子",
        "002475": "消费电子",
        "601728": "通信服务",
        "300502": "光通信",
        "300394": "光通信",
        "002384": "消费电子",
        "002916": "消费电子",
        "002463": "消费电子",
        "002938": "消费电子",
        
        # 机械制造
        "600150": "船舶制造",
        "002371": "半导体设备",
        "600031": "机械制造",
        "300400": "机械设备",

        # 家电
        "000333": "家电",

        # 材料
        "600183": "电子材料",

        # 铁路
        "601816": "铁路运输",

        # 安防
        "002415": "安防",

        # 消费零售
        "601888": "消费零售",
    }
    
    # 行业：优先 industry_map，其次 world_knowledge，再次 StockAPI
    industry = industry_map.get(code)
    if not industry or industry == "其他":
        wk = get_business_intelligence(code, name)
        if wk and wk.get("industry"):
            industry = wk["industry"]
    if not industry or industry == "其他":
        sa_map = _load_stockapi_industry_map()
        if sa_map.get(code):
            industry = sa_map[code]
    if not industry:
        industry = "其他"
    
    # 如果有旧 profile 缓存，更新行业后返回
    if profile:
        profile["industry"] = industry
        _save_profile(code, profile)
        return profile
    
    return {
        "code": code,
        "name": name,
        "industry": industry,
        "what_they_do": "",
        "industry_position": "",
        "strengths": [],
        "weaknesses": [],
        "moat_level": "",
        "growth_drivers": [],
        "headwinds": [],
    }


def _save_profile(code: str, profile: Dict):
    """保存业务画像"""
    try:
        with open(f"profiles/{code}.json", "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
    except:
        pass


def _get_prefix(code: str) -> str:
    """获取股票代码前缀"""
    if code.startswith("6"):
        return "sh"
    else:
        return "sz"


def _search_business_intelligence(name: str, code: str = "") -> Dict:
    """通过搜索引擎获取企业世界知识"""
    intelligence = {
        "latest_news": [],
        "key_events": [],
        "industry_trends": [],
        "management_changes": [],
        "strategic_moves": [],
    }
    
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        search_url = "https://www.baidu.com/s"
        params = {
            "wd": f"{name} 2024 2025 战略 转型 业绩 挑战",
            "rn": 10,
            "ie": "utf-8",
        }
        response = requests.get(search_url, params=params, headers=headers, timeout=15)
        
        if response.status_code == 200:
            text = response.text
            news_matches = re.findall(r'<div class="news-title.*?>(.*?)</div>', text, re.DOTALL)
            for match in news_matches[:5]:
                title = re.sub(r'<[^>]+>', '', match).strip()
                if title:
                    intelligence["latest_news"].append(title)
    except:
        pass
    
    return intelligence


def _sina_financial_report(code: str, report_type: str) -> list:
    """新浪财务报表 API"""
    prefix = _get_prefix(code)
    paper_code = f"{prefix}{code}"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code, "source": report_type,
        "type": "0", "page": "1", "num": "20",
    }
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        d = response.json()
        result = d.get("result", {}).get("data", {})
        report_list = result.get("report_list", {})
        # 获取最新报告数据
        if report_list:
            latest_date = sorted(report_list.keys())[-1]
            latest_report = report_list.get(latest_date, {})
            data = latest_report.get("data", [])
            # 转换为字典格式
            return [{item.get("item_field"): item.get("item_value") for item in data}] if data else []
        return []
    except:
        return []


def fetch_financials(code: str) -> Dict:
    """获取财务数据"""
    # 使用新浪API获取财务数据
    income = _sina_financial_report(code, "lrb")  # 利润表
    balance = _sina_financial_report(code, "fzb")  # 资产负债表
    cashflow = _sina_financial_report(code, "llb")  # 现金流量表
    
    return {
        "income": income,
        "balance_sheet": balance,
        "cashflow": cashflow,
    }


def _safe_float(val, default=0):
    """安全转换为float，处理None和空值"""
    if val is None:
        return default
    try:
        return float(val)
    except:
        return default


def compute_metrics(fin: Dict) -> Dict:
    """计算财务指标（适配新浪API字段）"""
    income = fin.get("income", [])
    balance = fin.get("balance_sheet", [])
    cashflow = fin.get("cashflow", [])
    
    if not income or not balance:
        return {}
    
    latest_income = income[0]
    latest_balance = balance[0]
    latest_cashflow = cashflow[0] if cashflow else {}
    
    # 新浪API字段映射（支持多种可能的字段名）
    revenue = _safe_float(latest_income.get("BIZTOTINCO")) or _safe_float(latest_income.get("BIZINCO"))
    parent_profit = _safe_float(latest_income.get("PARENETP")) or _safe_float(latest_income.get("NETPROFIT"))
    
    total_assets = max(_safe_float(latest_balance.get("TOTASSET")), 1)
    total_liabilities = _safe_float(latest_balance.get("TOTLIAB"))
    
    # 股东权益：支持多种字段名
    equity = _safe_float(latest_balance.get("PARESHARRIGH")) or \
             _safe_float(latest_balance.get("TOTEQUITY")) or \
             _safe_float(latest_balance.get("TOTSHAREQUI")) or \
             max(total_assets - total_liabilities, 1)
    
    cf_operating = _safe_float(latest_cashflow.get("NETCASHFLOWOPER")) or _safe_float(latest_cashflow.get("NETCASHFLOW"))
    rd_expense = _safe_float(latest_income.get("DEVEEXPE"))
    biz_cost = _safe_float(latest_income.get("BIZCOST"))
    
    # 计算指标
    gross_margin = ((revenue - biz_cost) / revenue * 100) if revenue > 0 and biz_cost > 0 else None
    if gross_margin is not None and gross_margin > 99:
        gross_margin = None
    net_margin = (parent_profit / revenue * 100) if revenue > 0 else 0
    debt_ratio = (total_liabilities / total_assets * 100) if total_assets > 0 else 0
    roe = (parent_profit / equity * 100) if equity > 0 and parent_profit != 0 else 0
    # 过滤异常ROE值（超过100%通常是数据问题）
    roe = min(roe, 100) if roe > 0 else 0
    
    return {
        "revenue": revenue,
        "parent_profit": parent_profit,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "rd_expense": rd_expense,
        "total_assets": total_assets,
        "debt_ratio": debt_ratio,
        "roe": roe,
        "cf_operating_net": cf_operating,
        "cf_to_profit": (cf_operating / parent_profit) if parent_profit != 0 else 0,
    }


def _assess_financial(m: Dict, industry: str) -> Dict:
    """评估财务健康状况"""
    highlights = []
    risks = []
    
    roe = m.get("roe")
    gm = m.get("gross_margin")
    net_margin = m.get("net_margin")
    debt_ratio = m.get("debt_ratio")
    cf_to_profit = m.get("cf_to_profit")
    revenue = m.get("revenue", 0)
    parent_profit = m.get("parent_profit", 0)
    
    # 亮点
    if roe and roe >= 15 and roe < 100:
        highlights.append(f"ROE{roe:.1f}%，盈利能力强")
    if gm and gm >= 30 and gm <= 99:
        highlights.append(f"毛利率{gm:.1f}%，盈利质量高")
    if net_margin and net_margin >= 10 and net_margin <= 50:
        highlights.append(f"净利率{net_margin:.1f}%，盈利水平好")
    
    # 风险
    if roe and roe < 8:
        risks.append(f"ROE仅{roe:.1f}%，资本回报能力较弱")
    if gm and 0 < gm < 18:
        risks.append(f"毛利率{gm:.1f}%低于行业预期（≥18%）")
    if net_margin and net_margin < 5:
        risks.append(f"净利率仅{net_margin:.1f}%，利润很薄")
    
    # 判断健康度
    health = "健康" if (roe and roe >= 10 and debt_ratio and debt_ratio < 70) else "一般"
    
    YI = 100_000_000
    return {
        "health_rating": health,
        "benchmark_ref": "行业平均",
        "highlights": highlights,
        "risks": risks,
        "key_metrics": {
            "revenue_yi": round(m.get("revenue", 0) / YI, 2),
            "net_profit_yi": round(m.get("parent_profit", 0) / YI, 2),
            "gross_margin_pct": m.get("gross_margin"),
            "net_margin_pct": m.get("net_margin"),
            "roe_pct": m.get("roe"),
            "debt_ratio_pct": m.get("debt_ratio"),
            "rd_ratio_pct": (m.get("rd_expense", 0) / m.get("revenue", 1) * 100) if m.get("revenue") else None,
            "rd_expense_yi": round(m.get("rd_expense", 0) / YI, 2) if m.get("rd_expense") else None,
            "operating_cf_yi": round(m.get("cf_operating_net", 0) / YI, 2),
            "cf_to_profit": m.get("cf_to_profit") if m.get("cf_to_profit") and m.get("cf_to_profit") < 100 else None,
        },
    }


def _market_name(code: str) -> str:
    """获取市场名称"""
    if code.startswith("6"):
        return "沪市"
    else:
        return "深市"


def _assess_moat(profile: Dict, raw: Dict) -> str:
    """评估护城河"""
    industry = profile.get("industry", "")
    roe = raw.get("roe", 0)
    
    if roe >= 15:
        return "宽"
    elif roe >= 10:
        return "中"
    else:
        return "窄"


def _overall_rating(profile: Dict, fin_health: Dict, geo: str) -> str:
    """综合评级"""
    health = fin_health.get("health_rating", "一般")
    if health == "健康":
        return "买入"
    else:
        return "观望"


def _growth_score(profile: Dict, fin_health: Dict, raw: Dict, geo: str) -> float:
    """成长评分"""
    return 5.0


# ========== 增强版分析函数 ==========

def _auto_industry_position(profile: Dict, raw: Dict) -> str:
    """生成自然、有洞察力的行业地位描述"""
    industry = profile.get("industry", "")
    name = profile.get("name", "")
    rev = raw.get("revenue", 0) / 1e8 if raw.get("revenue") else 0
    profit = raw.get("parent_profit", 0) / 1e8 if raw.get("parent_profit") else 0
    is_central_enterprise = "中国" in name or "国家" in name
    
    # 根据行业和规模生成自然的行业地位描述
    if industry == "金融银行":
        if rev >= 3000:
            pos = "国内领先的全国性股份制商业银行，资产规模和盈利能力均位居上市银行前列"
        elif rev >= 1000:
            pos = "作为股份制商业银行，在零售金融和数字化转型方面具有显著优势"
        elif rev >= 500:
            pos = "区域重要的商业银行，在特定业务领域具有竞争实力"
        else:
            pos = "一家综合性商业银行，业务涵盖公司金融、零售金融等多个领域"
    
    elif industry == "金融保险":
        if rev >= 5000:
            pos = "中国最大的综合性金融集团之一，保险业务稳居行业龙头地位"
        elif rev >= 2000:
            pos = "作为大型金融保险集团，在寿险、产险领域均具有较强竞争力"
        elif rev >= 500:
            pos = "国内重要的保险企业，在特定区域或业务线具有优势"
        else:
            pos = "一家综合性保险集团，业务涵盖保险、投资等领域"
    
    elif industry == "石油石化":
        if rev >= 5000:
            pos = "中国三大石油巨头之一，构建了从油气勘探开发到炼油化工、油品销售的完整产业链"
        elif rev >= 2000:
            pos = "国内大型石化企业，在炼油产能和化工产品领域具有显著规模优势"
        elif rev >= 500:
            pos = "区域重要的石化企业，在特定化工产品细分市场具有竞争地位"
        else:
            pos = "综合性石油石化企业，业务涉及原油加工和基础化工产品生产"
    
    elif industry == "食品饮料":
        if rev >= 1000:
            pos = "中国食品饮料行业的绝对龙头，品牌价值和市场份额均处于行业领先水平"
        elif rev >= 300:
            pos = "细分领域龙头企业，在特定品类中具有强大的品牌影响力和定价权"
        elif rev >= 100:
            pos = "国内知名的食品饮料企业，产品线丰富，渠道覆盖广泛"
        else:
            pos = "专注于食品饮料领域的企业，致力于为消费者提供优质产品"
    
    elif industry == "AI芯片":
        if rev >= 500:
            pos = "全球领先的芯片企业，在先进制程工艺和AI芯片领域具有技术领先优势"
        elif rev >= 100:
            pos = "国内领先的芯片设计/制造企业，在特定技术领域具有核心竞争力"
        elif rev >= 50:
            pos = "专注于芯片研发的科技企业，在细分市场具有发展潜力"
        else:
            pos = "芯片领域的科技企业，致力于推动国产替代进程"
    
    elif industry == "医药生物":
        if rev >= 500:
            pos = "国内大型综合性医药集团，研发实力和销售网络均处于行业前列"
        elif rev >= 100:
            pos = "创新型药企，在特定治疗领域具有较强的研发管线储备"
        elif rev >= 50:
            pos = "专注于医药领域的企业，在细分市场具有竞争优势"
        else:
            pos = "医药生物企业，业务涵盖药品研发、生产和销售"
    
    elif industry == "新能源":
        if rev >= 1000:
            pos = "全球新能源行业的领军企业，产业链布局完整，成本优势明显"
        elif rev >= 300:
            pos = "新能源领域的重要参与者，在特定产业链环节具有技术优势"
        elif rev >= 100:
            pos = "专注于新能源领域的企业，在细分赛道具有竞争力"
        else:
            pos = "新能源企业，致力于推动清洁能源发展"
    
    elif industry == "消费电子":
        if rev >= 3000:
            pos = "全球消费电子巨头，产品线丰富，品牌影响力遍及全球市场"
        elif rev >= 500:
            pos = "消费电子领域的重要企业，在特定产品类别中具有竞争力"
        elif rev >= 100:
            pos = "专注于消费电子的科技企业，产品创新能力较强"
        else:
            pos = "消费电子企业，致力于为用户提供优质的电子产品"
    
    elif industry == "公用事业":
        if rev >= 500:
            pos = "区域公用事业龙头，业务稳定，在当地具有垄断性优势"
        elif rev >= 100:
            pos = "区域性公用事业企业，为当地提供稳定的公共服务"
        else:
            pos = "公用事业企业，业务涉及能源供应等领域"
    
    elif industry == "煤炭":
        if rev >= 500:
            pos = "国内大型煤炭企业，资源储量和产能规模均处于行业前列"
        elif rev >= 100:
            pos = "区域性煤炭企业，在当地具有重要地位"
        else:
            pos = "煤炭企业，从事煤炭开采和销售业务"
    
    else:
        pos = f"{industry}行业的一家企业"
    
    # 组合最终描述
    if is_central_enterprise:
        full_pos = f"{name}作为中央企业，{pos}"
    else:
        full_pos = f"{name}是{pos}"
    
    profile["industry_position"] = full_pos[:150]
    return full_pos


def _auto_strengths(profile: Dict, fin_health: Dict, raw: Dict = None) -> List[str]:
    """生成深入、有洞察力的竞争优势分析"""
    strengths = []
    industry = profile.get("industry", "")
    name = profile.get("name", "")
    code = profile.get("code", "")
    fin = fin_health.get("key_metrics", {})
    highlights = fin_health.get("highlights", [])
    raw = raw or {}

    rev = fin.get("revenue_yi") or 0
    net_profit = fin.get("net_profit_yi") or 0
    roe = fin.get("roe_pct")
    gm = fin.get("gross_margin_pct")
    debt = fin.get("debt_ratio_pct")
    rd_ratio = fin.get("rd_ratio_pct")

    world_knowledge = get_business_intelligence(code, name)
    if world_knowledge.get("strengths"):
        for kw in world_knowledge["strengths"]:
            if kw and kw not in strengths:
                strengths.append(kw)
        profile["strengths"] = strengths[:5]
        return strengths[:5]

    industry_strengths = {
        "金融银行": [
            lambda: f"净利润{net_profit:.0f}亿元，盈利能力位居上市银行前列" if net_profit > 100 else None,
            lambda: "零售业务优势明显，客户基础深厚" if "零售" in name or "平安" in name else None,
            lambda: "综合金融服务能力强，协同效应显著" if "集团" in name else None,
            "资本充足率较高，抗风险能力强",
            "数字化转型领先，线上渠道占比高",
        ],
        "金融保险": [
            lambda: f"净利润{net_profit:.0f}亿元，行业盈利能力领先" if net_profit > 500 else None,
            "保险业务规模大，客户基数庞大",
            "综合金融牌照齐全，协同效应强",
            "投资能力强，资产配置多元化",
        ],
        "石油石化": [
            lambda: f"营收规模{rev:.0f}亿元，国内三大石油巨头之一" if rev > 3000 else None,
            "油气全产业链一体化布局，从勘探到销售全覆盖",
            "原油储备丰富，供应链稳定",
            "炼化规模优势明显，成本控制能力强",
            "中央企业背景，政策支持力度大",
        ],
        "食品饮料": [
            lambda: f"净利润{net_profit:.0f}亿元，行业盈利能力领先" if net_profit > 50 else None,
            lambda: f"毛利率{gm:.1f}%，品牌溢价能力强" if gm and gm > 50 else None,
            "品牌历史悠久，消费者认可度高",
            "渠道网络遍布全国，市场覆盖广",
            "产品矩阵丰富，满足多元化需求",
        ],
        "AI芯片": [
            lambda: f"研发投入{fin.get('rd_expense_yi'):.0f}亿元，技术创新能力强" if fin.get("rd_expense_yi") else None,
            "技术壁垒高，研发周期长，先发优势明显",
            "客户粘性高，替换成本大",
            "国产替代趋势下，政策支持力度大",
        ],
        "医药生物": [
            lambda: f"研发投入占比{rd_ratio:.1f}%，创新驱动发展" if rd_ratio and rd_ratio > 5 else None,
            "产品线丰富，研发管线储备充足",
            "销售网络覆盖广，学术推广能力强",
            "一致性评价进展顺利，集采影响可控",
        ],
        "新能源": [
            "产业链布局完整，成本优势明显",
            "技术路线领先，产能规模大",
            "下游需求旺盛，行业增长空间大",
            "碳中和政策支持，长期发展确定性高",
        ],
        "消费电子": [
            "供应链管理能力强，响应速度快",
            "技术研发投入大，产品迭代快",
            "品牌影响力强，全球市场份额高",
            "智能制造水平领先，生产效率高",
        ],
        "公用事业": [
            "业务稳定，现金流充裕",
            "区域垄断优势明显，市场地位稳固",
            "政策监管下收益稳定，分红比例高",
            "资产负债率合理，财务结构稳健",
        ],
        "煤炭": [
            lambda: f"产能规模{rev:.0f}亿元级别，行业龙头地位稳固" if rev > 500 else None,
            "资源储量丰富，成本优势明显",
            "运输网络完善，销售渠道稳定",
            "长协合同占比高，业绩确定性强",
        ],
    }

    if roe and roe >= 15:
        strengths.append(f"ROE{roe:.1f}%，资本回报能力显著优于行业平均")
    elif roe and roe >= 10:
        strengths.append(f"ROE{roe:.1f}%，维持稳健的资本回报水平")
    
    if gm and 50 <= gm <= 99:
        strengths.append(f"毛利率{gm:.1f}%，体现较强的产品定价权")
    elif gm and 30 <= gm < 99:
        strengths.append(f"毛利率{gm:.1f}%，盈利质量较高")
    
    if debt is not None and debt < 50:
        strengths.append(f"资产负债率{debt:.1f}%，财务结构稳健")
    
    if rd_ratio and rd_ratio >= 5:
        strengths.append(f"研发费率{rd_ratio:.1f}%，持续投入研发创新")
    
    if industry in industry_strengths:
        for item in industry_strengths[industry]:
            if callable(item):
                result = item()
                if result:
                    strengths.append(result)
            else:
                strengths.append(item)
    
    if "中国" in name or "国家" in name:
        strengths.append("中央企业地位，政策支持力度大")
    
    seen = set()
    final = []
    for s in strengths:
        if s and s not in seen:
            seen.add(s)
            final.append(s)
    
    profile["strengths"] = final[:5]
    return final[:5]


def _auto_weaknesses(profile: Dict, fin_health: Dict) -> List[str]:
    """生成深入、有洞察力的劣势分析"""
    weaknesses = []
    industry = profile.get("industry", "")
    code = profile.get("code", "")
    name = profile.get("name", "")
    fin = fin_health.get("key_metrics", {})
    risks = fin_health.get("risks", [])
    
    world_knowledge = get_business_intelligence(code, name)
    if world_knowledge.get("weaknesses"):
        for w in world_knowledge["weaknesses"]:
            if w and w not in weaknesses:
                weaknesses.append(w)
        profile["weaknesses"] = weaknesses[:4]
        return weaknesses[:4]
    
    roe = fin.get("roe_pct")
    gm = fin.get("gross_margin_pct")
    debt = fin.get("debt_ratio_pct")

    if roe and roe < 8:
        weaknesses.append(f"ROE仅{roe:.1f}%，资本回报能力偏弱")
    
    if gm and gm < 20:
        weaknesses.append(f"毛利率{gm:.1f}%偏低，盈利能力受限")
    
    if debt is not None and debt > 70:
        weaknesses.append(f"资产负债率{debt:.1f}%较高，财务风险需关注")
    
    industry_weaknesses = {
        "金融银行": [
            "利率市场化持续压缩净息差",
            "资产质量受宏观经济周期影响较大",
            "同业竞争激烈，差异化优势不明显",
            "房地产贷款占比高，信用风险集中",
        ],
        "金融保险": [
            "利率下行影响投资收益",
            "保险业务竞争激烈，费用率上升",
            "监管趋严，合规成本增加",
        ],
        "石油石化": [
            lambda: f"净利率仅{fin.get('net_margin_pct'):.1f}%，利润空间狭窄" if fin.get('net_margin_pct') and fin.get('net_margin_pct') < 5 else None,
            "原油价格波动对业绩影响大",
            "新能源转型投入大，短期业绩承压",
            "环保要求趋严，合规成本上升",
        ],
        "食品饮料": [
            "原材料价格波动影响毛利率",
            "市场竞争激烈，营销费用高",
            "消费疲软背景下需求承压",
            "渠道变革带来不确定性",
        ],
        "AI芯片": [
            "技术迭代快，研发投入压力大",
            "国际竞争激烈，高端市场受制于人",
            "产业链配套不完善，依赖进口设备",
            "投资周期长，回报不确定性大",
        ],
        "医药生物": [
            "集采政策导致产品价格下降",
            "研发失败风险高，投入产出不确定",
            "医保控费持续，盈利空间受压",
            "国际化进程面临挑战",
        ],
        "新能源": [
            "行业产能过剩，价格竞争激烈",
            "上游原材料价格波动大",
            "补贴退坡影响盈利能力",
            "技术路线迭代风险",
        ],
        "消费电子": [
            "行业周期性强，需求波动大",
            "技术迭代快，产品生命周期短",
            "供应链全球化带来不确定性",
            "贸易摩擦影响出口",
        ],
        "公用事业": [
            "价格管制严格，盈利空间受限",
            "投资周期长，资金占用量大",
            "环保标准提高，运营成本上升",
        ],
        "煤炭": [
            "新能源替代压力大，长期需求下行",
            "环保政策趋严，产能受限",
            "运输成本高，区域竞争激烈",
            "安全生产压力大",
        ],
    }
    
    if industry in industry_weaknesses:
        for item in industry_weaknesses[industry]:
            if callable(item):
                result = item()
                if result:
                    weaknesses.append(result)
            else:
                weaknesses.append(item)
    
    for risk in risks[:2]:
        if risk not in weaknesses:
            weaknesses.append(risk)
    
    seen = set()
    final = []
    for w in weaknesses:
        if w and w not in seen:
            seen.add(w)
            final.append(w)
    
    profile["weaknesses"] = final[:4]
    return final[:4]




def _auto_growth_drivers(profile: Dict, fin_health: Dict) -> List[str]:
    """生成深入、有洞察力的成长驱动分析"""
    drivers = []
    industry = profile.get("industry", "")
    code = profile.get("code", "")
    name = profile.get("name", "")
    fin = fin_health.get("key_metrics", {})
    highlights = fin_health.get("highlights", [])
    
    world_knowledge = get_business_intelligence(code, name)
    if world_knowledge.get("growth_drivers"):
        for kw in world_knowledge["growth_drivers"]:
            if kw and kw not in drivers:
                drivers.append(kw)
        profile["growth_drivers"] = drivers[:3]
        return drivers[:3]
    
    for h in highlights[:2]:
        if h and h not in drivers:
            drivers.append(h)
    
    industry_drivers = {
        "金融银行": [
            "零售业务转型，财富管理业务增长",
            "数字化银行布局，提升运营效率",
            "中间业务收入占比提升",
            "资产规模稳健扩张",
        ],
        "金融保险": [
            "代理人渠道改革见效",
            "健康险业务增长",
            "投资端优化，收益率提升",
            "数字化转型提升效率",
        ],
        "石油石化": [
            "油气价格高位运行，上游勘探利润弹性大",
            "炼化一体化升级，降本增效",
            "新能源转型带来长期增长空间",
            "海外业务拓展",
        ],
        "食品饮料": [
            "消费升级带动高端产品增长",
            "渠道下沉，拓展低线城市市场",
            "产品创新，推陈出新",
            "品牌出海，国际化布局",
        ],
        "AI芯片": [
            "AI算力需求爆发，芯片需求激增",
            "国产替代加速，进口替代空间大",
            "先进封装技术突破",
            "数据中心建设加速",
        ],
        "医药生物": [
            "创新药出海取得进展",
            "集采后市场份额提升",
            "医美、CXO等新兴业务增长",
            "老龄化带来医疗需求增长",
        ],
        "新能源": [
            "碳中和政策推动，行业景气度高",
            "技术进步带来成本下降",
            "储能、绿氢等新业务拓展",
            "海外市场需求增长",
        ],
        "消费电子": [
            "AI赋能，智能硬件创新",
            "折叠屏等新品类爆发",
            "汽车电子业务拓展",
            "供应链优化，降本增效",
        ],
        "公用事业": [
            "新能源发电装机增长",
            "电价改革带来盈利改善",
            "资产注入预期",
            "分红比例提升",
        ],
        "煤炭": [
            "能源安全背景下，煤炭作为基础能源地位稳固",
            "产能整合提升行业集中度",
            "长协价格机制稳定盈利",
            "煤电联营协同效应",
        ],
    }
    
    if industry in industry_drivers:
        for driver in industry_drivers[industry]:
            if driver not in drivers:
                drivers.append(driver)
    
    profile["growth_drivers"] = drivers[:3]
    return drivers[:3]


def _auto_headwinds(profile: Dict, fin_health: Dict) -> List[str]:
    """生成深入、有洞察力的风险因素分析"""
    headwinds = []
    industry = profile.get("industry", "")
    code = profile.get("code", "")
    name = profile.get("name", "")
    fin = fin_health.get("key_metrics", {})
    risks = fin_health.get("risks", [])
    
    world_knowledge = get_business_intelligence(code, name)
    if world_knowledge.get("headwinds"):
        for h in world_knowledge["headwinds"]:
            if h and h not in headwinds:
                headwinds.append(h)
        profile["headwinds"] = headwinds[:6]
        return headwinds[:6]
    
    for risk in risks[:2]:
        if risk not in headwinds:
            headwinds.append(risk)
    
    industry_headwinds = {
        "金融银行": [
            "宏观经济下行导致资产质量恶化",
            "利率下行压缩净息差",
            "房地产风险暴露",
            "金融科技公司竞争加剧",
        ],
        "金融保险": [
            "利率下行影响投资收益",
            "监管政策变化",
            "自然灾害导致赔付增加",
        ],
        "石油石化": [
            "国际油价大幅下跌",
            "新能源替代加速",
            "地缘政治冲突影响供应链",
            "环保政策趋严",
        ],
        "食品饮料": [
            "消费疲软，需求不及预期",
            "原材料成本上涨",
            "食品安全事件风险",
            "渠道变革冲击",
        ],
        "AI芯片": [
            "美国出口管制进一步收紧",
            "全球半导体周期下行",
            "技术研发不及预期",
            "产能过剩风险",
        ],
        "医药生物": [
            "集采政策持续推进",
            "医保控费常态化",
            "研发失败风险",
            "国际化受阻",
        ],
        "新能源": [
            "产能过剩导致价格战",
            "补贴退坡影响盈利",
            "上游原材料价格波动",
            "技术路线迭代风险",
        ],
        "消费电子": [
            "全球消费电子需求疲软",
            "贸易摩擦影响出口",
            "库存高企",
            "技术创新不及预期",
        ],
        "公用事业": [
            "电价下调压力",
            "环保投入增加",
            "项目延期风险",
            "利率上行增加财务成本",
        ],
        "煤炭": [
            "新能源替代加速",
            "环保政策趋严",
            "进口煤冲击",
            "安全生产事故风险",
        ],
    }
    
    if industry in industry_headwinds:
        for hw in industry_headwinds[industry]:
            if hw not in headwinds:
                headwinds.append(hw)
    
    profile["headwinds"] = headwinds[:3]
    return headwinds[:3]


def _auto_geopolitical_risks(profile: Dict) -> Dict:
    """生成地缘政治风险与助力分析"""
    risks = []
    opportunities = []
    industry_momentum = []
    code = profile.get("code", "")
    name = profile.get("name", "")
    industry = profile.get("industry", "")

    world_knowledge = get_business_intelligence(code, name)
    if world_knowledge.get("geopolitical_risks"):
        for r in world_knowledge["geopolitical_risks"]:
            if r and r not in risks:
                risks.append(r)
    if world_knowledge.get("geopolitical_opportunities"):
        for o in world_knowledge["geopolitical_opportunities"]:
            if o and o not in opportunities:
                opportunities.append(o)

    ai_chips = ["AI芯片", "半导体", "芯片", "半导体设备"]
    new_energy = ["锂电池", "新能源", "汽车整车"]
    carbon_neutral = ["公用事业", "煤炭", "石油石化"]
    consumer = ["消费零售", "食品饮料", "家电"]
    finance = ["金融银行", "金融保险", "金融券商"]
    machinery = ["机械制造", "船舶制造"]
    tourism = ["消费零售", "旅游零售"]
    optical = ["光通信", "通信服务"]

    if any(ind in industry for ind in ai_chips):
        industry_momentum.extend([
            "AI大模型爆发驱动算力芯片需求激增",
            "全球AI算力投资进入爆发期",
            "国产替代加速，政策支持AI芯片发展",
        ])
    if any(ind in industry for ind in new_energy):
        industry_momentum.extend([
            "全球碳中和加速，新能源产业持续高增长",
            "锂电池成本持续下降，储能市场爆发",
            "石油涨价加速新能源汽车替代",
        ])
    if any(ind in industry for ind in carbon_neutral):
        industry_momentum.extend([
            "碳中和政策推动能源结构转型",
            "煤炭消费达峰，清洁能源替代加速",
            "欧盟碳关税倒逼出口企业绿色转型",
        ])
    if any(ind in industry for ind in consumer):
        industry_momentum.extend([
            "中国消费市场升级与分化并存",
            "消费结构从实物向服务转型",
            "国货品牌崛起替代进口品牌",
        ])
    if any(ind in industry for ind in finance):
        industry_momentum.extend([
            "数字化金融重塑银行业竞争格局",
            "财富管理行业进入黄金发展期",
            "利率市场化接近尾声，银行转型压力大",
        ])
    if any(ind in industry for ind in machinery):
        industry_momentum.extend([
            "一带一路沿线国家基建需求释放",
            "全球工程机械电动化浪潮",
            "新能源工程机械放量，电动化转型",
        ])
    if any(ind in industry for ind in tourism):
        industry_momentum.extend([
            "中国出境游加速复苏",
            "海南自贸港建设推动旅游零售",
            "消费升级带动高端消费回流",
        ])
    if any(ind in industry for ind in optical):
        industry_momentum.extend([
            "AI算力爆发驱动高速光模块需求激增",
            "400G/800G光模块放量，数据中心升级",
            "5G建设持续推进，光通信需求增长",
        ])

    profile["geopolitical_risks"] = risks
    profile["geopolitical_opportunities"] = opportunities
    profile["industry_momentum"] = industry_momentum[:3]
    return {"risks": risks, "opportunities": opportunities, "industry_momentum": industry_momentum[:3]}


def _generate_summary(profile: Dict, fin_health: Dict) -> str:
    """生成世界知识的一句话总结"""
    name = profile.get("name", "")
    industry = profile.get("industry", "")
    strengths = profile.get("strengths", [])
    weaknesses = profile.get("weaknesses", [])
    growth_drivers = profile.get("growth_drivers", [])
    headwinds = profile.get("headwinds", [])
    geo_risks = profile.get("geopolitical_risks", [])
    
    summary_parts = []
    
    if strengths:
        s = strengths[0]
        if len(s) > 50:
            s = s[:50] + "..."
        summary_parts.append(f"{name}的核心优势在于{s}")
    
    if weaknesses:
        w = weaknesses[0]
        if len(w) > 50:
            w = w[:50] + "..."
        summary_parts.append(f"但面临挑战：{w}")
    
    if growth_drivers:
        d = growth_drivers[0]
        if len(d) > 50:
            d = d[:50] + "..."
        summary_parts.append(f"未来增长动力：{d}")
    
    if headwinds:
        h = headwinds[0]
        if len(h) > 50:
            h = h[:50] + "..."
        summary_parts.append(f"主要风险：{h}")
    
    if geo_risks:
        g = geo_risks[0]
        if len(g) > 50:
            g = g[:50] + "..."
        summary_parts.append(f"地缘风险：{g}")
    
    geo_opps = profile.get("geopolitical_opportunities", [])
    if geo_opps:
        o = geo_opps[0]
        if len(o) > 50:
            o = o[:50] + "..."
        summary_parts.append(f"地缘机遇：{o}")
    
    return "；".join(summary_parts) if summary_parts else f"{name}当前处于{industry}行业，整体状况一般"


# ═══════════════════════════════════════════════════════════
# DEPRECATED: analyze_one() 的写入能力已于 2026-06 废弃。
# 旧规则型（新浪/百科 API）生成路径质量低于 gen_fundamentals.py（LLM+Tushare），
# 且用硬编码相对路径不走 paths.py，容易写错目录。
#
# 保留 load_fundamentals() 作为只读工具（内部使用）。
# 所有新生成请走 picker.pipeline.gen_fundamentals.generate_one() 或
# picker.pipeline.refresh_fundamentals.refresh_one()。
# ═══════════════════════════════════════════════════════════

def load_fundamentals(code: str) -> Optional[Dict]:
    """加载缓存的基本面数据（只读，已废弃写入路径）。"""
    from picker import paths
    fund_path = os.path.join(paths.FUNDAMENTALS_DIR, f"{code}.json")
    if not os.path.exists(fund_path):
        # fallback: 冷股池
        cold_path = os.path.join(paths.COLD_FUNDAMENTALS_DIR, f"{code}.json")
        if os.path.exists(cold_path):
            fund_path = cold_path
        else:
            return None
    try:
        with open(fund_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
