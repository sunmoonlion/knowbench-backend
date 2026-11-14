from types import SimpleNamespace

import pytest

from app.interfaces.mcp.workbench_mcp import dispatch


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": None},
        {"error": "bad"},
        {"error": {}},
        {"result": {"content": [], "structuredContent": {"value": float("inf")}}},
        {"result": {}, "error": {}},
        {"result": {"content": "bad"}},
    ],
)
async def test_invalid_relay_response_is_unknown(response):
    async def reply(*args):
        return response

    result, status = await dispatch(
        None, 1, "read_input", {}, SimpleNamespace(call=reply), True
    )
    assert status == "unknown" and result["result"]["isError"] is True


@pytest.mark.parametrize(
    "reply,status",
    [
        ({"result": {"content": [], "isError": False}}, "succeeded"),
        ({"result": {"content": [], "isError": True}}, "failed"),
        ({"error": {"code": -32000, "message": "refused"}}, "failed"),
    ],
)
async def test_explicit_relay_outcome_is_recorded(reply, status):
    async def respond(*args):
        return reply

    result, actual = await dispatch(
        None, 1, "read_input", {}, SimpleNamespace(call=respond), True
    )
    assert actual == status and result == reply
