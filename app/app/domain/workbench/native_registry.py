"""Codex protocol bindings. Only the owning runtime can report a native action."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NativeAction:
    name: str
    item_type: str | None = None
    request: str | None = None
    interaction: Literal["approval", "question"] | None = None


NATIVE_ACTIONS = (
    NativeAction(
        "command",
        "commandExecution",
        "item/commandExecution/requestApproval",
        "approval",
    ),
    NativeAction(
        "file_change", "fileChange", "item/fileChange/requestApproval", "approval"
    ),
    NativeAction(
        "permissions",
        request="item/permissions/requestApproval",
        interaction="approval",
    ),
    NativeAction("web_search", "webSearch"),
    NativeAction(
        "question", request="item/tool/requestUserInput", interaction="question"
    ),
)
NATIVE_REQUESTS = {
    action.request: action for action in NATIVE_ACTIONS if action.request
}
_NATIVE_ITEMS = {
    action.item_type: action for action in NATIVE_ACTIONS if action.item_type
}


def native_event_payload(method: str, params: dict) -> dict:
    """Annotate actual observations on the original event, never creating an approval."""
    if method not in ("item/started", "item/completed"):
        return params
    item = params.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    action = _NATIVE_ITEMS.get(item_type) if isinstance(item_type, str) else None
    # The registration is computed by KnowBench. Ignore any peer-supplied value.
    if action is None and "registry_action" not in params:
        return params
    payload = dict(params)
    if action:
        payload["registry_action"] = action.name
    else:
        payload.pop("registry_action", None)
    return payload
