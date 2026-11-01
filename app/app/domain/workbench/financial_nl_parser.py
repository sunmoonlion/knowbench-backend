"""开放财务自然语言提问解析与指标映射引擎（WP-8）。

职责：
1. 从用户自然语言输入中抽取出主体、报告期、会计科目或财务指标。
2. 映射到《统一科目表》标准化科目代码（IS.*/BS.*/CF.*）与确定性指标公式。
3. 严格边界防护：对金融业机构进行专业模板重定向拒绝；对无法明确判定的提问输出 `undecidable`，杜绝大模型臆造。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# 常见 A 股主体词表映射
SUBJECT_SYNONYMS: dict[str, str] = {
    "贵州茅台": "600519.SH",
    "茅台": "600519.SH",
    "600519": "600519.SH",
    "600519.SH": "600519.SH",
    "宁德时代": "300750.SZ",
    "宁王": "300750.SZ",
    "300750": "300750.SZ",
    "300750.SZ": "300750.SZ",
    "比亚迪": "002594.SZ",
    "002594": "002594.SZ",
    "五粮液": "000858.SZ",
    "000858": "000858.SZ",
    "中国平安": "601318.SH",
    "平安银行": "000001.SZ",
    "000001": "000001.SZ",
    "中信证券": "600030.SH",
    "工商银行": "601398.SH",
}

# 金融机构黑名单（不适用普通制造业/一般企业分析模板）
FINANCIAL_INSTITUTIONS: frozenset[str] = frozenset(
    {"000001.SZ", "601318.SH", "600030.SH", "601398.SH"}
)

# 财务指标与统一科目映射
METRIC_MAPPINGS: dict[str, str] = {
    "营业收入": "IS.REVENUE",
    "营收": "IS.REVENUE",
    "营业总收入": "IS.REVENUE",
    "营业成本": "IS.COST",
    "成本": "IS.COST",
    "净利润": "IS.NET_PROFIT",
    "归母净利润": "IS.NET_PROFIT_PARENT",
    "研发费用": "IS.RND",
    "研发支出": "IS.RND",
    "资产总计": "BS.TOTAL_ASSETS",
    "总资产": "BS.TOTAL_ASSETS",
    "负债合计": "BS.TOTAL_LIABILITIES",
    "总负债": "BS.TOTAL_LIABILITIES",
    "资产负债率": "debt_ratio",
    "销售毛利率": "gross_margin",
    "毛利率": "gross_margin",
    "净利率": "net_margin",
    "ROE": "roe_simple",
    "净资产收益率": "roe_simple",
    "经营活动现金流量净额": "CF.NET_OPERATING",
    "经营现金流": "CF.NET_OPERATING",
    "资本开支": "CF.CAPEX",
    "CAPEX": "CF.CAPEX",
    "自由现金流": "free_cash_flow",
    "FCF": "free_cash_flow",
    "CAGR": "cagr",
    "复合年增长率": "cagr",
    "复合增长率": "cagr",
}


@dataclass(frozen=True)
class ParsedFinancialQuery:
    status: Literal["resolved", "undecidable", "refused_financial_institution"]
    subject: str | None = None
    report_date: str | None = None
    metrics: tuple[str, ...] = field(default_factory=tuple)
    expression: str | None = None
    reason: str | None = None
    raw_query: str = ""


class FinancialNLParser:
    """开放财务自然语言分流器。"""

    def parse(self, query: str) -> ParsedFinancialQuery:
        cleaned = query.strip()
        if not cleaned:
            return ParsedFinancialQuery(
                status="undecidable",
                reason="Query is empty",
                raw_query=query,
            )

        # 1. 提取主体
        subject = self._extract_subject(cleaned)
        if not subject:
            return ParsedFinancialQuery(
                status="undecidable",
                reason="Cannot identify target listed company subject from query",
                raw_query=query,
            )

        # 2. 金融机构门禁：严禁套用一般企业财务报表模板
        if subject in FINANCIAL_INSTITUTIONS:
            return ParsedFinancialQuery(
                status="refused_financial_institution",
                subject=subject,
                reason="Target is a financial institution (bank/insurance/securities); general manufacturing template does not apply.",
                raw_query=query,
            )

        # 3. 提取期间
        report_date = self._extract_period(cleaned)

        # 4. 提取指标与算式
        metrics, expression = self._extract_metrics(cleaned)

        if not metrics:
            return ParsedFinancialQuery(
                status="undecidable",
                subject=subject,
                report_date=report_date,
                reason="No recognizable financial indicators or accounting subjects found",
                raw_query=query,
            )

        return ParsedFinancialQuery(
            status="resolved",
            subject=subject,
            report_date=report_date,
            metrics=tuple(metrics),
            expression=expression,
            raw_query=query,
        )

    def _extract_subject(self, text: str) -> str | None:
        # 直接匹配股票代码（如 600519.SH 或 600519）
        code_match = re.search(
            r"\b([0-369]\d{5}(?:\.(?:SH|SZ|BJ))?)\b", text, re.IGNORECASE
        )
        if code_match:
            raw_code = code_match.group(1).upper()
            if raw_code in SUBJECT_SYNONYMS:
                return SUBJECT_SYNONYMS[raw_code]
            if "." not in raw_code:
                # 默认映射规则
                return (
                    f"{raw_code}.SH" if raw_code.startswith("6") else f"{raw_code}.SZ"
                )
            return raw_code

        # 匹配同义词
        for name, code in SUBJECT_SYNONYMS.items():
            if name in text:
                return code
        return None

    def _extract_period(self, text: str) -> str | None:
        # 匹配年份 + 中报/半年报/三季报/年报
        year_match = re.search(r"(20\d{2})", text)
        if not year_match:
            return None
        year = year_match.group(1)

        if any(kw in text for kw in ("中报", "半年报", "上半年", "二季度", "Q2")):
            return f"{year}-06-30"
        if any(kw in text for kw in ("一季报", "一季度", "Q1")):
            return f"{year}-03-31"
        if any(kw in text for kw in ("三季报", "三季度", "前三季度", "Q3")):
            return f"{year}-09-30"
        # 默认归为年报
        return f"{year}-12-31"

    def _extract_metrics(self, text: str) -> tuple[list[str], str | None]:
        matched: list[str] = []
        expression = None

        for kw, code in METRIC_MAPPINGS.items():
            if kw in text:
                if code not in matched:
                    matched.append(code)

        # 复合算式推导
        if "free_cash_flow" in matched or "FCF" in matched:
            expression = "CF.NET_OPERATING - CF.CAPEX"
        elif "gross_margin" in matched:
            expression = "(IS.REVENUE - IS.COST) / IS.REVENUE"
        elif "debt_ratio" in matched:
            expression = "BS.TOTAL_LIABILITIES / BS.TOTAL_ASSETS"
        elif "roe_simple" in matched:
            expression = "IS.NET_PROFIT_PARENT / BS.EQUITY_PARENT"
        elif "cagr" in matched:
            expression = "((end / start) ** (1 / n)) - 1"

        return matched, expression
