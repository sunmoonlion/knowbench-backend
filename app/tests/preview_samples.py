"""预览用的样例：专家各步的交回物（tests/test_preview_fixtures.py 用它造出各种状态）。

格式照专家包（app/domain/workbench/packs.py）。内容像真的，**数字是编的**：
用的是真公司的名字和代码，但每个数都由下面的算式生成，不是这家公司的真实数据。
数据集的说明里写明了这一点，所以底稿的「提醒」里也会带上。
"""

from __future__ import annotations

from typing import Any

SAMPLE_NOTE = "样例数据：数字是为了预览编的，不是这家公司的真实数据"
AS_OF = "2025-12-31"

RULES = (
    ("R01", "资产 = 负债 + 所有者权益"),
    ("R02", "流动资产 + 非流动资产 = 资产总计"),
    ("R03", "流动负债 + 非流动负债 = 负债合计"),
    ("R04", "营业收入 − 营业成本 − 各项费用 = 营业利润（允许其他收益）"),
    ("R05", "利润总额 − 所得税 = 净利润"),
    ("R06", "归母净利润 + 少数股东损益 = 净利润"),
    ("R07", "经营 + 投资 + 筹资现金流 + 汇率影响 = 现金净增加额"),
    ("R08", "期初现金 + 现金净增加额 = 期末现金"),
    ("R09", "利润表净利润 = 现金流量表补充资料里的净利润"),
)

ITEMS = (
    ("income_statement", "operate_income", "营业收入"),
    ("income_statement", "operate_cost", "营业成本"),
    ("income_statement", "net_profit", "净利润"),
    ("cash_flow_statement", "net_operate_cash_flow", "经营活动现金流量净额"),
    ("balance_sheet", "total_assets", "资产总计"),
    ("balance_sheet", "total_liabilities", "负债合计"),
    ("balance_sheet", "total_equity", "所有者权益合计"),
)


def yuan(value: float) -> float:
    return float(round(value, 2))


class Company:
    """一家样例公司三年的数。每个数都从几个参数算出来，所以三张表对得上。"""

    def __init__(
        self,
        name: str,
        code: str,
        market: str,
        *,
        revenue: float,
        growth: tuple[float, float],
        gross: tuple[float, float, float],
        net: tuple[float, float, float],
        leverage: float,
        years: tuple[int, int, int] = (2023, 2024, 2025),
    ) -> None:
        self.name, self.code, self.market = name, code, market
        self.years = years
        self.dataset = f"{market}{code}-financials"
        self.version = f"{self.dataset}-sample{code}"
        revenues = [revenue, revenue * (1 + growth[0])]
        revenues.append(revenues[1] * (1 + growth[1]))
        self.figures: dict[int, dict[str, float]] = {}
        for i, year in enumerate(years):
            income = yuan(revenues[i])
            assets = yuan(income * 1.9)
            liabilities = yuan(assets * leverage)
            profit = yuan(income * net[i])
            self.figures[year] = {
                "operate_income": income,
                "operate_cost": yuan(income * (1 - gross[i])),
                "net_profit": profit,
                "net_operate_cash_flow": yuan(profit * 1.12),
                "total_assets": assets,
                "total_liabilities": liabilities,
                "total_equity": yuan(assets - liabilities),
            }

    # ---------------- 七步 ----------------
    def question(self) -> str:
        return f"{self.name} {self.years[0]} 到 {self.years[-1]} 年的盈利能力怎么样？"

    def scope(self) -> dict[str, Any]:
        return {
            "company": self.name,
            "security_code": self.code,
            "periods": [str(y) for y in self.years],
            "report_types": ["年报"],
            "aspects": ["盈利能力"],
            "assumptions": [],
            "question": self.question(),
        }

    def profile(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "not_ingested": False,
            "security_code": self.code,
            "data_version": self.version,
            "as_of": AS_OF,
            "notes": [
                SAMPLE_NOTE,
                f"{self.years[1]} 年年报的数是追溯调整后的口径：公司当年变更了收入确认的会计政策",
            ],
            "periods": [
                {
                    "fiscal_year": year,
                    "report_type": "年报",
                    "report_date": f"{year}-12-31",
                    "basis": "追溯调整后" if year == self.years[1] else "原始披露",
                    "verified": True,
                    "official_disclosed_date": f"{year + 1}-03-28",
                }
                for year in self.years
            ],
            "basis_breaks": [
                {
                    "between": [str(self.years[0]), str(self.years[1])],
                    "basis": ["原始披露", "追溯调整后"],
                },
                {
                    "between": [str(self.years[1]), str(self.years[2])],
                    "basis": ["追溯调整后", "原始披露"],
                },
            ],
        }

    def reconcile(self, *, unbalanced: bool = False) -> dict[str, Any]:
        checks = [
            {
                "rule_id": rule_id,
                "rule": rule,
                "periods_checked": len(self.years),
                "unbalanced": 0,
                "unbalanced_periods": [],
            }
            for rule_id, rule in RULES
        ]
        if unbalanced:
            checks[0]["unbalanced"] = 1
            checks[0]["unbalanced_periods"] = [f"{self.years[1]}-12-31"]
        return {
            "checks": checks,
            "continuity": [
                {
                    "fiscal_year": self.years[1],
                    "diff": 1204.0,
                    "note": f"{self.years[1]} 年年报的数是追溯调整后的口径：公司当年变更了收入确认的会计政策",
                }
            ],
            "unexplained_breaks": [],
            "data_version": self.version,
        }

    def basis(self, year: int) -> str:
        return "追溯调整后" if year == self.years[1] else "原始披露"

    def facts(self, *, empty: bool = False) -> dict[str, Any]:
        table = [
            {
                "fiscal_year": year,
                "report_type": "年报",
                "report_date": f"{year}-12-31",
                "basis": self.basis(year),
                "statement": statement,
                "item": item,
                "display_name": display,
                "value": self.figures[year][item],
                "unit": "元",
            }
            for year in self.years
            for statement, item, display in ITEMS
        ]
        return {
            "table": [] if empty else table,
            "sql": [
                "SELECT report_date, basis, operate_income, operate_cost, net_profit"
                " FROM income_statement WHERE report_type = '年报'"
                f" AND fiscal_year BETWEEN {self.years[0]} AND {self.years[-1]}",
                "SELECT report_date, basis, total_assets, total_liabilities, total_equity"
                " FROM balance_sheet WHERE report_type = '年报'"
                f" AND fiscal_year BETWEEN {self.years[0]} AND {self.years[-1]}",
            ],
            "data_version": self.version,
        }

    def metrics(self, *, empty: bool = False) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for i, year in enumerate(self.years):
            f = self.figures[year]

            def row(name, display, value, formula, inputs, *, reason="", _year=year):
                return {
                    "fiscal_year": _year,
                    "report_type": "年报",
                    "basis": self.basis(_year),
                    "metric_name": name,
                    "display_name": display,
                    "value": None if reason else round(value, 4),
                    "unit": "比率",
                    "formula": formula,
                    "inputs": inputs,
                    "applicable": not reason,
                    "reason_if_not": reason,
                    "source": "query_metric",
                }

            income, cost, profit = (
                f["operate_income"],
                f["operate_cost"],
                f["net_profit"],
            )
            rows.append(
                row(
                    "gross_margin",
                    "毛利率",
                    (income - cost) / income,
                    "(营业收入 − 营业成本) ÷ 营业收入",
                    {"operate_income": income, "operate_cost": cost},
                )
            )
            rows.append(
                row(
                    "net_margin",
                    "净利率",
                    profit / income,
                    "净利润 ÷ 营业收入",
                    {"net_profit": profit, "operate_income": income},
                )
            )
            rows.append(
                row(
                    "debt_to_assets",
                    "资产负债率",
                    f["total_liabilities"] / f["total_assets"],
                    "负债合计 ÷ 资产总计",
                    {
                        "total_liabilities": f["total_liabilities"],
                        "total_assets": f["total_assets"],
                    },
                )
            )
            # 要用到上一年的：第一年没有上一年；口径不同的两年不放在一起算
            if i == 0:
                reason = "要用上一年的期末数，范围内没有"
            else:
                reason = "口径不同"
            previous = self.figures[self.years[i - 1]] if i else f
            rows.append(
                row(
                    "roe",
                    "净资产收益率",
                    profit / ((f["total_equity"] + previous["total_equity"]) / 2),
                    "净利润 ÷ 期初与期末所有者权益的平均",
                    {"net_profit": profit, "total_equity": f["total_equity"]},
                    reason=reason,
                )
            )
        return {
            "table": [] if empty else rows,
            "sql": [],
            "data_version": self.version,
        }

    def crosscheck(self, *, mismatch: bool = False) -> dict[str, Any]:
        compared = ("operate_income", "net_profit", "total_assets", "total_equity")
        matched = [
            {
                "fiscal_year": year,
                "item": item,
                "basis": self.basis(year),
                "value": self.figures[year][item],
                "source_report": f"{self.name}{year}年年度报告",
                "page": 6,
            }
            for year in self.years
            for item in compared
        ]
        mismatched = []
        if mismatch:
            moved = matched.pop()
            mismatched.append(
                {
                    "fiscal_year": moved["fiscal_year"],
                    "item": moved["item"],
                    "basis": moved["basis"],
                    "official": yuan(moved["value"] + 120000.0),
                    "dataset": moved["value"],
                    "source_report": moved["source_report"],
                    "page": moved["page"],
                }
            )
        return {
            "matched": matched,
            "mismatched": mismatched,
            "not_covered": [],
            "coverage": (
                f"范围内 {len(self.years)} 期年报全部对照；"
                "现金流量表的项目年报的「主要会计数据」表里没有，没有对照"
            ),
            "data_version": self.version,
        }

    def percent(self, year: int, name: str) -> str:
        f = self.figures[year]
        value = {
            "gross": (f["operate_income"] - f["operate_cost"]) / f["operate_income"],
            "net": f["net_profit"] / f["operate_income"],
        }[name]
        return f"{value * 100:.1f}%"

    def note(self) -> dict[str, Any]:
        first, middle, last = self.years
        gross = "、".join(self.percent(y, "gross") for y in self.years)
        net = "、".join(self.percent(y, "net") for y in self.years)
        return {
            "answer": (
                f"{self.name} {first} 到 {last} 年年报：毛利率分别为 {gross}，"
                f"净利率分别为 {net}。{middle} 年的数是追溯调整后的口径，"
                f"与 {first}、{last} 年的原始披露口径不同，三年之间不能直接相比。"
                "本次体检只列事实与口径，不给投资建议。"
            ),
            "tables": [],
            "observations": [
                f"营业收入 {first} 到 {last} 年逐年增加",
                f"毛利率 {last} 年高于 {first} 年",
                "经营活动现金流量净额三年都高于净利润",
            ],
            "caveats": [
                SAMPLE_NOTE,
                f"{first} 年与 {middle} 年之间、{middle} 年与 {last} 年之间口径不同："
                f"{middle} 年年报的数是追溯调整后的口径，公司当年变更了收入确认的会计政策",
                "净资产收益率三年都标为不适用：第一年没有上一年的期末数，后两年与上一年口径不同",
                f"{middle} 年期初现金与上一年期末现金相差 1,204.00 元，数据集的说明里有解释",
            ],
            "limitations": [
                "没有做附注与经营数据",
                "现金流量表的项目没有与公司披露的原文对照",
            ],
            "citations": [
                f"{self.dataset}，数据版本 {self.version}，数据到 {AS_OF}",
                f"{self.name}{first}年、{middle}年、{last}年年度报告，第 6 页「主要会计数据」",
            ],
            "data_version": self.version,
            "conclusion": "",
        }

    def all_steps(self) -> list[dict[str, Any]]:
        return [
            self.scope(),
            self.profile(),
            self.reconcile(),
            self.facts(),
            self.metrics(),
            self.crosscheck(),
            self.note(),
        ]


HENGRUI = Company(
    "恒瑞医药",
    "600276",
    "sh",
    revenue=22_820_000_000,
    growth=(0.084, 0.112),
    gross=(0.846, 0.859, 0.862),
    net=(0.188, 0.201, 0.214),
    leverage=0.09,
)
NINGDE = Company(
    "宁德时代",
    "300750",
    "sz",
    revenue=400_900_000_000,
    growth=(-0.097, 0.121),
    gross=(0.229, 0.244, 0.251),
    net=(0.117, 0.145, 0.152),
    leverage=0.66,
)
AIRPORT = Company(
    "上海机场",
    "600009",
    "sh",
    revenue=11_050_000_000,
    growth=(0.118, 0.092),
    gross=(0.166, 0.224, 0.263),
    net=(0.085, 0.157, 0.183),
    leverage=0.37,
)
BYD = Company(
    "比亚迪",
    "002594",
    "sz",
    revenue=602_300_000_000,
    growth=(0.290, 0.163),
    gross=(0.202, 0.194, 0.198),
    net=(0.052, 0.054, 0.057),
    leverage=0.75,
)
MERCHANTS = Company(
    "招商银行",
    "600036",
    "sh",
    revenue=339_100_000_000,
    growth=(-0.005, 0.021),
    gross=(0.5, 0.5, 0.5),
    net=(0.436, 0.441, 0.446),
    leverage=0.9,
)


# ---------------- 问数 ----------------
WULIANGYE_QUESTION = "五粮液近五年的毛利率分别是多少？"


def data_query_steps() -> list[dict[str, Any]]:
    version = "sz000858-financials-sample000858"
    margins = {2021: 0.7535, 2022: 0.7542, 2023: 0.7579, 2024: 0.7705, 2025: 0.7731}
    rows = [
        {"fiscal_year": y, "report_type": "年报", "gross_margin": m}
        for y, m in margins.items()
    ]
    return [
        {
            "question": "五粮液 2021 至 2025 年年报的毛利率，按年列出",
            "entities": ["五粮液（000858）"],
            "metrics": ["毛利率"],
            "period": "2021 至 2025 年，年报",
        },
        {
            "sql": (
                "SELECT fiscal_year, report_type,"
                " (operate_income - operate_cost) / operate_income AS gross_margin"
                " FROM income_statement WHERE report_type = '年报'"
                " AND fiscal_year BETWEEN 2021 AND 2025 ORDER BY fiscal_year"
            ),
            "columns": [
                {"name": "fiscal_year", "definition": "会计年度"},
                {
                    "name": "gross_margin",
                    "definition": "(营业收入 − 营业成本) ÷ 营业收入，比率，0.25 表示 25%",
                },
            ],
        },
        {"rows": rows, "row_count": len(rows), "data_version": version},
        {
            "table": [
                {"年度": str(y), "毛利率": f"{m * 100:.2f}%", "报告": "年报"}
                for y, m in margins.items()
            ],
            "notes": [SAMPLE_NOTE, "五年都有值，没有缺的"],
        },
        {
            "answer": "五粮液 2021 至 2025 年年报的毛利率分别为 75.35%、75.42%、75.79%、77.05%、77.31%。",
            "table": [
                {"年度": str(y), "毛利率": f"{m * 100:.2f}%"}
                for y, m in margins.items()
            ],
            "citations": [f"sz000858-financials，数据版本 {version}"],
            "limitations": [SAMPLE_NOTE, "只有年报，没有季报与中报"],
            "conclusion": "",
        },
    ]


# ---------------- 专家每一步用了哪些工具（过程里显示的） ----------------
def tool(name: str, dataset: str | None = None, **arguments: Any) -> dict[str, Any]:
    given = {**({"dataset": dataset} if dataset else {}), **arguments}
    return {
        "type": "mcpToolCall",
        "server": "sunmoon_knowbase",
        "tool": name,
        "status": "completed",
        "arguments": given,
        "result": {"content": [{"type": "text", "text": "（样例里不带查询的结果）"}]},
        "error": None,
    }


def fin_review_tools(company: Company) -> dict[str, list[dict[str, Any]]]:
    d = company.dataset
    first, last = company.years[0], company.years[-1]
    between = f"fiscal_year BETWEEN {first} AND {last}"
    return {
        "profile": [
            tool("list_datasets"),
            tool("describe_schema", d),
            tool("run_sql", d, sql="SELECT key, value FROM dataset_metadata"),
            tool(
                "run_sql",
                d,
                sql="SELECT fiscal_year, report_type, basis, verified FROM income_statement"
                f" WHERE report_type = '年报' AND {between}",
            ),
            tool(
                "run_sql",
                d,
                sql=f"SELECT report_date, disclosed_date FROM disclosure_calendar WHERE {between}",
            ),
        ],
        "reconcile": [
            tool(
                "run_sql",
                d,
                sql="SELECT rule_id, rule, tolerance FROM reconciliation_rules",
            ),
            *[
                tool(
                    "run_sql",
                    d,
                    sql=f"/* {rule_id} {rule} */ SELECT report_date, diff FROM check_{rule_id.lower()}"
                    f" WHERE {between}",
                )
                for rule_id, rule in RULES
            ],
        ],
        "extract": [
            tool("describe_schema", d, table="income_statement"),
            *[tool("run_sql", d, sql=sql) for sql in company.facts()["sql"]],
        ],
        "metrics": [
            tool("metric_definitions", d),
            tool(
                "query_metric",
                d,
                metrics=["gross_margin", "net_margin", "debt_to_assets", "roe"],
                filters={"report_type": "年报"},
            ),
        ],
        "crosscheck": [
            tool(
                "run_sql",
                d,
                sql="SELECT fiscal_year, item, basis, value, source_report, page"
                f" FROM official_key_figures WHERE {between}",
            )
        ],
    }


def data_query_tools() -> dict[str, list[dict[str, Any]]]:
    d = "sz000858-financials"
    return {
        "sql_generate": [tool("describe_schema", d), tool("metric_definitions", d)],
        "sql_execute": [tool("run_sql", d, sql=data_query_steps()[1]["sql"])],
    }


def missing_dataset_call(code: str, market: str = "sh") -> dict[str, Any]:
    """聊天里查了一家没有数据的公司：工具答没有这个数据集。"""
    dataset = f"{market}{code}-financials"
    return {
        "type": "mcpToolCall",
        "server": "sunmoon_knowbase",
        "tool": "describe_schema",
        "status": "failed",
        "arguments": {"dataset": dataset},
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": f"unknown dataset: {dataset}; call list_datasets to see what exists",
                }
            ]
        },
        "error": None,
    }
