"""测试 DCF 折现现金流与假设敏感性分析引擎（WP-9）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.workbench.dcf_valuation import (
    DCFError,
    calculate_dcf,
    sensitivity_matrix,
)


def test_standard_dcf_calculation():
    """验证标准 DCF 计算数值逻辑与精度。"""
    base_fcf = Decimal("10000000000")  # 100 亿元
    growth_rates = [Decimal("0.10"), Decimal("0.08"), Decimal("0.05")]  # 3 年预测期
    wacc = Decimal("0.08")
    terminal_growth = Decimal("0.02")

    res = calculate_dcf(
        base_fcf=base_fcf,
        growth_rates=growth_rates,
        wacc=wacc,
        terminal_growth=terminal_growth,
        net_debt=Decimal("2000000000"),  # 净负债 20 亿
        shares_count=Decimal("1250000000"),  # 12.5 亿股
    )

    assert res.base_fcf == base_fcf
    assert len(res.forecast_fcf) == 3
    assert res.pv_forecast > Decimal(0)
    assert res.terminal_value > Decimal(0)
    assert abs(
        res.enterprise_value - (res.pv_forecast + res.pv_terminal_value)
    ) <= Decimal("0.02")
    assert res.equity_value == res.enterprise_value - Decimal("2000000000")
    assert res.fair_value_per_share is not None
    assert res.fair_value_per_share > Decimal(0)


def test_dcf_wacc_less_than_growth_error():
    """WACC 小于或等于永续增长率时抛出明确异常。"""
    with pytest.raises(DCFError) as exc_info:
        calculate_dcf(
            base_fcf=Decimal("100"),
            growth_rates=[Decimal("0.05")],
            wacc=Decimal("0.02"),
            terminal_growth=Decimal("0.03"),
        )
    assert "must be greater than terminal growth rate" in str(exc_info.value)


def test_dcf_sensitivity_matrix_monotonicity():
    """验证敏感性矩阵生成及金融常识单调性：WACC 越高估值越低，g 越高估值越高。"""
    base_fcf = Decimal("1000")
    growth_rates = [Decimal("0.05"), Decimal("0.05")]
    wacc_list = [Decimal("0.07"), Decimal("0.08"), Decimal("0.09")]
    g_list = [Decimal("0.015"), Decimal("0.020"), Decimal("0.025")]

    matrix = sensitivity_matrix(
        base_fcf=base_fcf,
        growth_rates=growth_rates,
        wacc_list=wacc_list,
        terminal_growth_list=g_list,
        shares_count=Decimal("100"),
    )

    assert len(matrix) == 3
    for row in matrix:
        assert len(row) == 3
        # 同一 WACC 下，永续增长率 g 越高，企业价值 EV 越高
        assert (
            row[0].enterprise_value < row[1].enterprise_value < row[2].enterprise_value
        )

    # 同一 g 下，WACC 越高，企业价值 EV 越低
    assert (
        matrix[0][0].enterprise_value
        > matrix[1][0].enterprise_value
        > matrix[2][0].enterprise_value
    )
