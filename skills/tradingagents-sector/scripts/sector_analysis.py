"""A-share sector/concept board analysis tool.

Usage:
    python sector_analysis.py search <keyword>       Search concept boards by keyword
    python sector_analysis.py rank [top_n]             Concept board ranking by change%
    python sector_analysis.py stocks <board_code>      Constituent stocks of a board
    python sector_analysis.py belong <stock_code>      Concept/industry belonging of a stock
    python sector_analysis.py fund_flow              Industry fund flow ranking
    python sector_analysis.py analysis <keyword>      One-stop sector analysis (data only)
    python sector_analysis.py deep <keyword>           Multi-agent deep analysis with debate + report
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from dotenv import load_dotenv
load_dotenv()

from tradingagents.dataflows.providers.astock_provider import AstockProvider

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def search(keyword: str):
    """Search concept boards by keyword."""
    provider = AstockProvider()
    print(f"=== 搜索概念板块: '{keyword}' ===\n")
    try:
        result = provider.search_concept_board(keyword)
        print(result)
    except NotImplementedError as e:
        print(f"[astock] 搜索失败: {e}")
        print("[提示] 网络问题，尝试备用方案...")
        _fallback_search(keyword)


def rank(top_n: int = 20):
    """Concept board ranking."""
    provider = AstockProvider()
    print(f"=== 概念板块涨跌幅排名 (TOP {top_n}) ===\n")
    try:
        result = provider.get_concept_boards(top_n=top_n)
        print(result)
    except NotImplementedError as e:
        print(f"[astock] 排名获取失败: {e}")


def stocks(board_code: str):
    """Constituent stocks of a concept board."""
    provider = AstockProvider()
    print(f"=== 板块 {board_code} 成分股分析 ===\n")
    try:
        result = provider.get_concept_board_stocks(board_code)
        print(result)
    except NotImplementedError as e:
        print(f"[astock] 成分股获取失败: {e}")


def belong(stock_code: str):
    """Concept/industry/region belonging of a stock."""
    provider = AstockProvider()
    print(f"=== 个股概念归属: {stock_code} ===\n")
    try:
        result = provider.get_stock_concept_belonging(stock_code)
        print(result)
    except NotImplementedError as e:
        print(f"[astock] 概念归属获取失败: {e}")


def fund_flow():
    """Industry fund flow ranking."""
    provider = AstockProvider()
    print("=== 行业板块资金流向排名 ===\n")
    try:
        result = provider.get_board_fund_flow()
        print(result)
    except NotImplementedError as e:
        print(f"[astock] 资金流向获取失败: {e}")


def analysis(keyword: str):
    """One-stop sector analysis report."""
    provider = AstockProvider()
    print(f"{'='*60}")
    print(f"=== 板块综合分析: {keyword} ===")
    print(f"{'='*60}\n")

    # 1. Search for the board
    print("【1/5】搜索板块...")
    board_code = None
    board_name = None
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "200", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": "m:90+t:3",
            "fields": "f2,f3,f12,f13,f14,f104,f105,f6,f140",
        }
        import requests as _req
        r = _req.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            print(f"未找到与'{keyword}'相关的概念板块。")
            return

        keyword_lower = keyword.lower()
        matched = []
        for item in items:
            name = item.get("f14", "")
            if keyword_lower in name.lower():
                matched.append(item)

        if not matched:
            print(f"未找到与'{keyword}'匹配的概念板块。")
            return

        best = matched[0]
        board_code = best.get("f12", "")
        board_name = best.get("f14", "")
        change_pct = best.get("f3", 0)
        up_count = best.get("f104", 0)
        down_count = best.get("f105", 0)
        amount = best.get("f6", 0)
        leader = best.get("f140", "")

        print(f"板块: {board_name}    代码: {board_code}")
        print(f"涨跌幅: {change_pct}%    上涨: {up_count}    下跌: {down_count}")
        print(f"成交额: {amount / 1e8:.2f}亿元    领涨股: {leader}")
    except Exception as e:
        print(f"板块搜索失败: {e}")
        return

    if not board_code:
        return

    # 2. Get constituent stocks
    print(f"\n【2/5】成分股分析 (TOP 10)...")
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "50", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"b:{board_code}+f:!50",
            "fields": "f2,f3,f4,f12,f13,f14,f6,f15,f16,f20",
        }
        import requests as _req
        r = _req.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if items:
            total = d.get("data", {}).get("total", len(items))
            sorted_items = sorted(items, key=lambda x: float(x.get("f3", 0)), reverse=True)
            top10 = sorted_items[:10]
            print(f"共 {total} 只成分股，涨幅 TOP 10：\n")
            print(f"{'排名':<4}{'代码':<8}{'名称':<10}{'涨跌幅':>8}{'现价':>10}{'成交额(亿)':>12}")
            print("-" * 56)
            for i, item in enumerate(top10):
                name = item.get("f14", "")
                code = item.get("f12", "")
                chg = item.get("f3", 0)
                price = item.get("f2", 0)
                amt = float(item.get("f6", 0)) / 1e8
                turnover = item.get("f20", 0)
                print(f"{i+1:<4}{code:<8}{name:<10}{chg:>7.2f}%{price:>10.2f}{amt:>12.2f}")

            # Bottom 5
            bottom5 = sorted_items[-5:]
            if len(sorted_items) > 10:
                print(f"\n跌幅 TOP 5：\n")
                print(f"{'排名':<4}{'代码':<8}{'名称':<10}{'涨跌幅':>8}{'现价':>10}{'成交额(亿)':>12}")
                print("-" * 56)
                for i, item in enumerate(bottom5):
                    name = item.get("f14", "")
                    code = item.get("f12", "")
                    chg = item.get("f3", 0)
                    price = item.get("f2", 0)
                    amt = float(item.get("f6", 0)) / 1e8
                    print(f"{total-4+i:<4}{code:<8}{name:<10}{chg:>7.2f}%{price:>10.2f}{amt:>12.2f}")
    except Exception as e:
        print(f"成分股获取失败: {e}")

    # 3. Board fund flow
    print(f"\n【3/5】行业板块资金流向 (TOP 10)...")
    try:
        result = provider.get_board_fund_flow()
        print(result)
    except NotImplementedError as e:
        print(f"[astock] {e}")

    # 4. Concept board ranking context
    print(f"\n【4/5】概念板块整体排名 (TOP 10)...")
    try:
        result = provider.get_concept_boards(top_n=10)
        print(result)
    except NotImplementedError as e:
        print(f"[astock] {e}")

    # 5. Key takeaways
    print(f"\n【5/5】分析总结")
    print(f"板块 {board_name}({board_code}) 当日涨跌幅 {change_pct}%")
    if items:
        avg_chg = sum(float(it.get("f3", 0)) for it in items) / len(items)
        print(f"成分股平均涨跌幅: {avg_chg:.2f}%")
    print(f"\n* 以上数据仅供参考，不构成投资建议 *")


def _fallback_search(keyword: str):
    """Fallback: use akshare if astock fails."""
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_em()
        if df is None or df.empty:
            return
        matched = df[df["板块名称"].str.contains(keyword, na=False)]
        if matched.empty:
            print(f"未找到与'{keyword}'相关的概念板块。")
        else:
            print(f"概念板块搜索'{keyword}'结果（共{len(matched)}个）：\n")
            print(matched[["板块名称", "板块代码"]].to_string(index=False))
    except ImportError:
        print("[提示] 需要安装 akshare: pip install akshare")
    except Exception as e:
        print(f"备用搜索失败: {e}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]

    commands = {
        "search": lambda: search(args[0] if args else "航天"),
        "rank": lambda: rank(int(args[0]) if args else 20),
        "stocks": lambda: stocks(args[0] if args else "BK0903"),
        "belong": lambda: belong(args[0] if args else "002371"),
        "fund_flow": lambda: fund_flow(),
        "analysis": lambda: analysis(args[0] if args else "商业航天"),
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"未知命令: {cmd}")
        print(f"可用命令: {', '.join(commands.keys())}")
        print(__doc__)


if __name__ == "__main__":
    main()
