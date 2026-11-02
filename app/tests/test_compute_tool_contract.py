"""The advertised MCP input contract accepts reconciliation symbols and rejects invalid names."""

import pytest
from jsonschema import Draft202012Validator

from app.interfaces.mcp.workbench_mcp import TOOLS


@pytest.mark.parametrize(
    "name,valid",
    [
        ("A", True),
        ("L", True),
        ("TLE", True),
        ("rev_2024", True),
        ("a", True),
        ("1A", False),
        ("A.B", False),
        ("A-B", False),
        ("Ａ", False),
        ("A\n", False),
        ("A" * 33, False),
    ],
)
def test_compute_identifier_contract(name, valid):
    validator = Draft202012Validator(TOOLS["compute"]["inputSchema"])
    body = {
        "task": "test",
        "name": "check",
        "expression": name,
        "inputs": {name: "fact:00000000-0000-0000-0000-000000000001"},
    }
    assert validator.is_valid(body) is valid
