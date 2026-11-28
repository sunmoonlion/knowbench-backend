import json

import pytest
from pydantic import ValidationError
from sample_packs import FIN_FORECAST

from app.application.workbench.acceptance import judge
from app.domain.workbench.packs import AcceptanceRule


@pytest.mark.parametrize(
    ("count", "rows", "expected"),
    [
        (0, [], True),
        (2, [], True),
        (3, [{"period": "2025-01"}], True),
        (6, [{"period": "2025-01"}], True),
        (2, [{"period": "2025-01"}], False),
        (3, [], False),
        (-1, [], False),
        (True, [], False),
        ("2", [], False),
        (2.5, [], False),
    ],
)
def test_sample_forecast_window(count, rows, expected):
    step = next(s for s in FIN_FORECAST.workflow if s.step_id == "forecast")
    result = {
        "method": "两期不足以预测",
        "window": {"count": count},
        "assumptions": ["已排除未完月份", "窗口不足三期"],
        "rows": rows,
        "backtest": None,
        "sql": ["fixture query"],
    }
    assert judge(json.dumps(result), step.acceptance).ok is expected
    if count == 2 and expected:
        for change in (
            {"method": " "},
            {"assumptions": ["", " "]},
            {"backtest": {}},
            {"sql": []},
        ):
            assert not judge(json.dumps(result | change), step.acceptance).ok


def test_invalid_assertion_at_admission():
    for expression in ("unknown()", 'num("items[0]")', 'num("x") ** 3'):
        with pytest.raises(ValidationError):
            AcceptanceRule(kind="assertion", expression=expression)
