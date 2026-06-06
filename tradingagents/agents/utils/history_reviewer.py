"""历史复盘工具 — 读取历史分析/裁决，对比当前行情，生成复盘评语。"""
import os
import json
import re
from datetime import date as _date, datetime
from typing import Optional, Dict, Any, List


class HistoryReviewer:
    """读取历史分析文档，结合最新股价计算偏差，生成复盘报告。"""

    def __init__(self, results_dir: str = "results"):
        self.results_dir = results_dir

    # ── 个股历史查找（TradingAgents 完整分析） ──

    def find_stock_history(self, ticker: str) -> List[Dict[str, Any]]:
        """扫描 results/{ticker}/ 目录，按日期排序返回历史分析摘要。"""
        base = os.path.join(self.results_dir, ticker)
        if not os.path.isdir(base):
            return []
        entries = []
        for d in sorted(os.listdir(base), reverse=True):
            dd = os.path.join(base, d)
            if not os.path.isdir(dd):
                continue
            summary = self._load_stock_summary(dd)
            if summary:
                entries.append({"date": d, **summary})
        return entries

    def _load_stock_summary(self, day_dir: str) -> Optional[Dict]:
        """从 summary.md 或 risk_judge/ 提取历史结论。"""
        # Try summary.md first
        summary_md = os.path.join(day_dir, "summary.md")
        if os.path.isfile(summary_md):
            return self._parse_summary_md(summary_md)
        # Try risk_judge final_decision
        final = os.path.join(day_dir, "07_risk_judge", "final_decision.md")
        if os.path.isfile(final):
            return self._parse_summary_md(final)
        return None

    def _parse_summary_md(self, path: str) -> Dict:
        text = open(path, encoding="utf-8").read()
        result = {"raw_text": text[:2000]}

        # Direction
        m = re.search(r'<!--\s*VERDICT:\s*(\{.*?\})\s*-->', text, re.DOTALL)
        if m:
            try:
                payload = json.loads(m.group(1))
                result["direction"] = payload.get("direction", "")
                result["reason"] = payload.get("reason", "")
            except json.JSONDecodeError:
                pass

        # Fallback direction keywords
        if "direction" not in result:
            for kw in ["看多", "偏多", "买入", "BUY"]:
                if kw in text:
                    result["direction"] = "看多"
                    break
            if "direction" not in result:
                for kw in ["看空", "偏空", "卖出", "SELL"]:
                    if kw in text:
                        result["direction"] = "看空"
                        break
            if "direction" not in result:
                result["direction"] = "中性"

        # Extract price targets
        price_pat = re.findall(r'目标价[：:]\s*([\d.]+)', text)
        if price_pat:
            result["target_price"] = float(price_pat[-1])
        stop_pat = re.findall(r'止损价[：:]\s*([\d.]+)', text)
        if stop_pat:
            result["stop_price"] = float(stop_pat[-1])

        return result

    # ── 板块历史查找 ──

    def find_sector_history(self, keyword: str) -> List[Dict[str, Any]]:
        """扫描 results/{keyword}/ 目录，读取 summary.json。"""
        base = os.path.join(self.results_dir, keyword)
        if not os.path.isdir(base):
            return []
        entries = []
        for d in sorted(os.listdir(base), reverse=True):
            dd = os.path.join(base, d)
            if not os.path.isdir(dd):
                continue
            summary_json = os.path.join(dd, "summary.json")
            if os.path.isfile(summary_json):
                try:
                    data = json.load(open(summary_json, encoding="utf-8"))
                    entries.append({"date": d, **data})
                except (json.JSONDecodeError, Exception):
                    continue
        return entries

    # ── 快速分析历史查找 ──

    def find_quick_analysis_history(self, name: str, code: str) -> List[Dict[str, Any]]:
        """扫描 results/个股/{name}_{code}/ 目录。"""
        base = os.path.join(self.results_dir, "个股", f"{name}_{code}")
        if not os.path.isdir(base):
            return []
        entries = []
        for d in sorted(os.listdir(base), reverse=True):
            dd = os.path.join(base, d)
            if not os.path.isdir(dd):
                continue
            analysis_txt = os.path.join(dd, "analysis.txt")
            if os.path.isfile(analysis_txt):
                text = open(analysis_txt, encoding="utf-8").read()
                entries.append({"date": d, "raw_text": text[:2000]})
        return entries

    # ── 生成复盘报告 ──

    def generate_review(self, history: List[Dict[str, Any]]) -> str:
        """将历史记录拼接为复盘文本，供 LLM 分析。"""
        if not history:
            return ""
        parts = ["【历史分析复盘】"]
        for h in history:
            date_str = h.get("date", "?")
            direction = h.get("direction", h.get("方向", "?"))
            reason = h.get("reason", h.get("核心结论", h.get("raw_text", "")))[:200]
            target = h.get("target_price", h.get("target", ""))
            stop = h.get("stop_price", h.get("stop", ""))
            line = f"  [{date_str}] 方向={direction}"
            if target:
                line += f" 目标={target}"
            if stop:
                line += f" 止损={stop}"
            line += f"\n  核心: {reason[:120]}"
            parts.append(line)
        return "\n".join(parts)

    def generate_review_prompt(self, history: List[Dict[str, Any]], current_price: float = None) -> str:
        """生成可直接喂给 LLM 的复盘上下文。"""
        if not history:
            return ""
        lines = ["## 历史分析复盘（用于结合历史做出判断）"]
        lines.append("")
        lines.append("以下是该标的历史分析记录（按日期倒序）：")
        lines.append("")
        for h in history:
            date_str = h.get("date", "?")
            direction = h.get("direction", h.get("方向", "?"))
            reason = h.get("reason", h.get("核心结论", h.get("raw_text", "")))[:200]
            target = h.get("target_price", "")
            raw = h.get("raw_text", "")[:500]
            # 板块summary.json可能有short_term/mid_term/long_term而非reason
            short = h.get("short_term", "")
            mid = h.get("mid_term", "")
            long_term = h.get("long_term", "")
            position = h.get("position", "")

            lines.append(f"### {date_str} 分析")
            lines.append(f"- 方向判断: {direction}")
            if reason:
                lines.append(f"- 核心逻辑: {reason}")
            if target:
                lines.append(f"- 目标价: {target}")
            if short and not reason:
                lines.append(f"- 短期预测: {short[:100]}")
            if mid and not reason:
                lines.append(f"- 中期预测: {mid[:100]}")
            if long_term and not reason:
                lines.append(f"- 长期预测: {long_term[:100]}")
            if position:
                lines.append(f"- 仓位建议: {position}")
            lines.append(f"- 原文摘要: {raw}")
            lines.append("")

        if current_price:
            # 如果有最新价，计算与历史目标价的偏差
            for h in history:
                target = h.get("target_price")
                if target and current_price:
                    deviation = (current_price - target) / target * 100
                    lines.append(f"  vs 历史目标({h['date']}): 目标={target}，当前={current_price}，偏差={deviation:+.1f}%")

        lines.append("请结合上述历史分析的判断结果，与当前市场数据进行对比，给出本次分析的独立判断，")
        lines.append("并在分析报告中包含一段【历史复盘】章节，评价过去判断的准确性。")
        lines.append("")
        return "\n".join(lines)


# ── 快捷函数 ──

def create_stock_review_context(ticker: str, current_price: float = None) -> str:
    """一站式生成个股历史复盘上下文。"""
    reviewer = HistoryReviewer()
    history = reviewer.find_stock_history(ticker)
    if not history:
        return ""
    return reviewer.generate_review_prompt(history, current_price)


def create_sector_review_context(keyword: str) -> str:
    """一站式生成板块历史复盘上下文。"""
    reviewer = HistoryReviewer()
    history = reviewer.find_sector_history(keyword)
    if not history:
        return ""
    return reviewer.generate_review_prompt(history)


def create_quick_review_context(name: str, code: str) -> str:
    """一站式生成快速分析历史复盘上下文。"""
    reviewer = HistoryReviewer()
    history = reviewer.find_quick_analysis_history(name, code)
    if not history:
        return ""
    return reviewer.generate_review_prompt(history)