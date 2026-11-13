"""Portable tool needs and owned account choices; discovery never grants access."""

from typing import Literal

from app.domain.workbench.capabilities import Strict, public_asset, tool_key


class DependencyCandidate(Strict):
    key: str
    connection_id: str
    connection_name: str
    account_label: str
    status: str


class SkillDependency(Strict):
    service_url: str | None
    tool_name: str
    legacy_tool_key: str | None = None
    status: Literal[
        "available",
        "selected",
        "selection_required",
        "connection_required",
        "connection_inactive",
    ]
    candidates: list[DependencyCandidate]


def skill_dependencies(
    asset: dict, assets: list[dict], selected_tools=None
) -> list[dict]:
    """Only pass assets obtained from the authenticated owner's repository query."""
    tools = [
        (a, t, tool_key(a["id"], t["name"]))
        for a in assets
        if a["kind"] == "connection"
        for t in a["detail"].get("tools", [])
    ]
    needs = [
        (r["url"], r["name"], None)
        for r in asset["detail"].get("tool_requirements", [])
    ]
    for key in asset["detail"].get("dependencies", []):
        pair = next(((a, t) for a, t, k in tools if k == key), None)
        needs.append(
            (
                pair[0]["detail"]["url"] if pair else None,
                pair[1]["name"] if pair else "",
                key,
            )
        )
    results = []
    for url, name, legacy_key in needs:
        candidates = [
            {
                "key": key,
                "connection_id": a["id"],
                "connection_name": a["name"],
                "account_label": a["detail"]["account_label"],
                "status": a["status"],
            }
            for a, t, key in tools
            if (
                key == legacy_key
                if legacy_key
                else a["detail"]["url"] == url and t["name"] == name
            )
        ]
        active = [c for c in candidates if c["status"] == "active"]
        if not candidates:
            status = "connection_required"
        elif not active:
            status = "connection_inactive"
        elif selected_tools is None:
            status = "available"
        elif any(c["key"] in selected_tools for c in active):
            status = "selected"
        else:
            status = "selection_required"
        results.append(
            {
                "service_url": url,
                "tool_name": name,
                "legacy_tool_key": legacy_key,
                "status": status,
                "candidates": candidates,
            }
        )
    return results


def missing_skill_dependencies(asset: dict, assets: list[dict], selected_tools) -> bool:
    return any(
        d["status"] != "selected"
        for d in skill_dependencies(asset, assets, selected_tools)
    )


def public_skill_asset(asset: dict, assets: list[dict], selected_tools=None) -> dict:
    result = public_asset(asset)
    if asset["kind"] == "skill":
        result["detail"] = {
            **result["detail"],
            "dependency_status": skill_dependencies(asset, assets, selected_tools),
        }
    return result
