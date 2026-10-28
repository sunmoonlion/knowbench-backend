"""样例专家 FIN_REVIEW（tests/sample_packs.py）七步各一份合格的交回物，给测委托机制的测试当
假 app-server 的回复。原在 test_fin_review_pack、test_fin_review_advisor_db（那两份测的是已撤
的内置专家本身，10-28 随它删了）。"""

from __future__ import annotations

import json

SCOPE = {
    "company": "上海机场",
    "security_code": "600009",
    "periods": ["2023", "2024", "2025"],
    "report_types": ["年报"],
    "aspects": ["收入", "利润"],
    "question": "…",
}
PROFILE = {
    "dataset": "sh600009-financials",
    "not_ingested": False,
    "security_code": "600009",
    "data_version": "sh600009-financials-39a395bfa6f16b67",
    "as_of": "2026-06-30",
    "notes": ["2021 年年报行为追溯调整后口径"],
    "periods": [
        {
            "fiscal_year": 2025,
            "report_type": "年报",
            "basis": "原始披露",
            "verified": True,
            "official_disclosed_date": "2026-03-28",
        }
    ],
    "basis_breaks": [],
}
RECONCILE = {
    "checks": [
        {"rule_id": f"R0{i}", "rule": "…", "periods_checked": 3, "unbalanced": 0}
        for i in range(1, 10)
    ],
    "continuity": [],
    "unexplained_breaks": [],
    "data_version": PROFILE["data_version"],
}
FACTS = {
    "table": [
        {
            "fiscal_year": 2025,
            "report_type": "年报",
            "basis": "原始披露",
            "item": "operate_income",
            "value": 1.0,
            "unit": "元",
        }
    ],
    "data_version": PROFILE["data_version"],
}
METRICS = {
    "table": [
        {
            "fiscal_year": 2025,
            "metric_name": "gross_margin",
            "value": 0.3,
            "applicable": True,
        }
    ],
    "data_version": PROFILE["data_version"],
}
CROSSCHECK = {
    "matched": [{"fiscal_year": 2025, "item": "operate_income"}],
    "mismatched": [],
    "not_covered": [],
    "coverage": "范围内 3 期年报全部对照",
    "data_version": PROFILE["data_version"],
}
NOTE = {
    "answer": "2025 年营业收入 1 元（原始披露）。",
    "tables": [],
    "observations": ["营业收入较上年增加"],
    "caveats": ["本次范围内没有口径断点、未核实期间或不适用的指标"],
    "limitations": ["不含附注"],
    "citations": ["sh600009-financials，数据版本 sh600009-financials-39a395bfa6f16b67"],
    "data_version": PROFILE["data_version"],
    "conclusion": "",
}

ALL = [SCOPE, PROFILE, RECONCILE, FACTS, METRICS, CROSSCHECK, NOTE]


def say(content) -> str:  # type: ignore[no-untyped-def]
    return json.dumps(content, ensure_ascii=False)
