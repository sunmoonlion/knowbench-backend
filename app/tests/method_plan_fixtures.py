"""Synthetic compositions of the author-confirmed non-financial catalog."""

import json
from pathlib import Path

from app.domain.workbench.method_catalog import DynamicMethodCatalog
from app.domain.workbench.method_plan import MethodPlan


def catalog():
    fixture = Path(__file__).parent / "fixtures/dynamic-methods-manifest.json"
    return DynamicMethodCatalog.model_validate(json.loads(fixture.read_text())["pack"])


def proposal(*, parent=0, review=False):
    steps = [
        {
            "instance_id": "inspect_1",
            "method_id": "inspect",
            "inputs": [],
            "tools": ["read_input"],
            "reason": "核对给定文档",
        }
    ]
    if review:
        steps.append(
            {
                "instance_id": "review_1",
                "method_id": "review",
                "inputs": [{"slot": "inspect", "instance_id": "inspect_1"}],
                "tools": ["read_input"],
                "reason": "这次问题要求复核",
            }
        )
    steps.append(
        {
            "instance_id": "report_1",
            "method_id": "report",
            "inputs": [{"slot": "inspect", "instance_id": "inspect_1"}],
            "tools": [],
            "reason": "交回作者规定的最终成果",
        }
    )
    return MethodPlan.model_validate(
        {
            "contract": "knowbench/method-plan/v1",
            "parent_version": parent,
            "reason": "依据用户问题组合作者方法",
            "steps": steps,
            "final_outputs": {"report": "report_1"},
        }
    )
