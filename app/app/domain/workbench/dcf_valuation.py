"""DCF 折现现金流与假设敏感性分析引擎（WP-9）。

职责：
1. 基于基期自由现金流（FCF）、显性预测期增长率、加权平均资本成本（WACC）与永续增长率（g），
   以 Decimal 高精度十进制进行确定性折现求值。
2. 支持企业价值（EV）到股权价值（Equity Value）及每股合理价值（Fair Value per Share）换算。
3. 提供 WACC 与永续增长率 g 的双变量敏感性分析矩阵。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


class DCFError(Exception):
    """DCF 计算异常。"""


@dataclass(frozen=True)
class DCFResult:
    base_fcf: Decimal
    forecast_fcf: tuple[Decimal, ...]
    pv_forecast: Decimal
    terminal_value: Decimal
    pv_terminal_value: Decimal
    enterprise_value: Decimal
    equity_value: Decimal
    fair_value_per_share: Decimal | None
    wacc: Decimal
    terminal_growth: Decimal


@dataclass(frozen=True)
class SensitivityCell:
    wacc: Decimal
    terminal_growth: Decimal
    enterprise_value: Decimal
    fair_value_per_share: Decimal | None


def calculate_dcf(
    base_fcf: Decimal,
    growth_rates: list[Decimal],
    wacc: Decimal,
    terminal_growth: Decimal,
    *,
    net_debt: Decimal = Decimal(0),
    shares_count: Decimal | None = None,
) -> DCFResult:
    """计算确定性 DCF 估值。"""
    if wacc <= terminal_growth:
        raise DCFError(
            f"WACC ({wacc}) must be greater than terminal growth rate ({terminal_growth})"
        )
    if wacc <= Decimal(0):
        raise DCFError(f"WACC must be positive, got {wacc}")

    forecast_fcf: list[Decimal] = []
    pv_forecast = Decimal(0)
    current_fcf = base_fcf

    # 1. 显性预测期
    for t, g in enumerate(growth_rates, start=1):
        current_fcf = current_fcf * (Decimal(1) + g)
        forecast_fcf.append(current_fcf)
        discount_factor = (Decimal(1) + wacc) ** t
        pv_forecast += current_fcf / discount_factor

    n = len(growth_rates)
    last_fcf = forecast_fcf[-1] if forecast_fcf else base_fcf

    # 2. 永续终值计算 (Gordon Growth Model)
    terminal_value = (last_fcf * (Decimal(1) + terminal_growth)) / (
        wacc - terminal_growth
    )
    pv_terminal_value = terminal_value / ((Decimal(1) + wacc) ** n)

    # 3. 企业价值与股权价值
    enterprise_value = pv_forecast + pv_terminal_value
    equity_value = enterprise_value - net_debt

    fair_value_per_share = None
    if shares_count and shares_count > Decimal(0):
        fair_value_per_share = (equity_value / shares_count).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    return DCFResult(
        base_fcf=base_fcf,
        forecast_fcf=tuple(forecast_fcf),
        pv_forecast=pv_forecast.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        terminal_value=terminal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        pv_terminal_value=pv_terminal_value.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        enterprise_value=enterprise_value.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        equity_value=equity_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        fair_value_per_share=fair_value_per_share,
        wacc=wacc,
        terminal_growth=terminal_growth,
    )


def sensitivity_matrix(
    base_fcf: Decimal,
    growth_rates: list[Decimal],
    wacc_list: list[Decimal],
    terminal_growth_list: list[Decimal],
    *,
    net_debt: Decimal = Decimal(0),
    shares_count: Decimal | None = None,
) -> list[list[SensitivityCell]]:
    """生成 WACC 与永续增长率 g 的双变量敏感性矩阵。"""
    matrix: list[list[SensitivityCell]] = []

    for wacc in wacc_list:
        row: list[SensitivityCell] = []
        for g in terminal_growth_list:
            if wacc <= g:
                # 异常参数组合跳过或置空
                continue
            res = calculate_dcf(
                base_fcf=base_fcf,
                growth_rates=growth_rates,
                wacc=wacc,
                terminal_growth=g,
                net_debt=net_debt,
                shares_count=shares_count,
            )
            row.append(
                SensitivityCell(
                    wacc=wacc,
                    terminal_growth=g,
                    enterprise_value=res.enterprise_value,
                    fair_value_per_share=res.fair_value_per_share,
                )
            )
        matrix.append(row)

    return matrix
