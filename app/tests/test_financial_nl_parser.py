"""测试开放财务自然语言分流器与指标映射引擎（WP-8）。"""

from __future__ import annotations

from app.domain.workbench.financial_nl_parser import FinancialNLParser


def test_parse_maotai_revenue_and_margin():
    """解析贵州茅台 2024 年报营收与毛利率。"""
    parser = FinancialNLParser()
    res = parser.parse("请计算贵州茅台2024年的营业收入和销售毛利率")
    assert res.status == "resolved"
    assert res.subject == "600519.SH"
    assert res.report_date == "2024-12-31"
    assert "IS.REVENUE" in res.metrics
    assert "gross_margin" in res.metrics
    assert res.expression == "(IS.REVENUE - IS.COST) / IS.REVENUE"


def test_parse_catl_midyear_rnd():
    """解析宁德时代 2024 半年报研发费用。"""
    parser = FinancialNLParser()
    res = parser.parse("宁德时代2024上半年研发费用是多少？")
    assert res.status == "resolved"
    assert res.subject == "300750.SZ"
    assert res.report_date == "2024-06-30"
    assert "IS.RND" in res.metrics


def test_parse_free_cash_flow():
    """解析自由现金流及确定性表达式推导。"""
    parser = FinancialNLParser()
    res = parser.parse("比亚迪2023年自由现金流FCF是多少")
    assert res.status == "resolved"
    assert res.subject == "002594.SZ"
    assert res.report_date == "2023-12-31"
    assert "free_cash_flow" in res.metrics
    assert res.expression == "CF.NET_OPERATING - CF.CAPEX"


def test_refuse_financial_institution_bank():
    """金融行业机构提问明确拒绝套用通用模板。"""
    parser = FinancialNLParser()
    res = parser.parse("请分析平安银行2024年营业成本和毛利率")
    assert res.status == "refused_financial_institution"
    assert res.subject == "000001.SZ"
    assert "financial institution" in (res.reason or "")


def test_undecidable_queries():
    """无法识别主体或财务指标时返回 undecidable，严禁幻觉臆造。"""
    parser = FinancialNLParser()

    # 无指标
    res1 = parser.parse("贵州茅台今天天气怎么样")
    assert res1.status == "undecidable"
    assert "No recognizable financial indicators" in (res1.reason or "")

    # 无已知主体
    res2 = parser.parse("请分析一家不知名公司的净利润")
    assert res2.status == "undecidable"
    assert "Cannot identify target" in (res2.reason or "")

    # 空输入
    res3 = parser.parse("   ")
    assert res3.status == "undecidable"
