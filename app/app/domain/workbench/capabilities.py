"""User capability contracts. Registration, selection and execution are distinct."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.errors import WorkbenchError


def disabled_mcp_config() -> dict[str, Any]:
    """Codex validates transport even for disabled servers; never connect here."""
    return {
        "mcp_servers.sunmoon_capabilities": {
            "enabled": False,
            "url": "http://127.0.0.1:9",
            "enabled_tools": [],
            "http_headers": {},
            "required": False,
        }
    }


class CapabilityError(WorkbenchError):
    code = "capability_unavailable"
    http_status = 409


class CapabilityReauthRequired(CapabilityError):
    code = "capability_reauth_required"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionCreate(Strict):
    request_id: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=1000)
    account_label: str = Field(min_length=1, max_length=100)
    token: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)


class AssetAction(Strict):
    revision: int = Field(ge=1)
    action: Literal[
        "enable", "disable", "disconnect", "default_on", "default_off", "refresh"
    ]


class ConnectionRenew(Strict):
    revision: int = Field(ge=1)
    token: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)


class SkillCreate(Strict):
    request_id: str = Field(min_length=8, max_length=128)
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)
    version: str = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1, max_length=64000)
    dependencies: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_content(self):
        if len(self.content.encode()) > 65536 or "\x00" in self.content:
            raise ValueError("SKILL.md exceeds 64 KiB or contains NUL")
        if not self.content.strip():
            raise ValueError("empty skill")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate dependency")
        return self


class SelectionPut(Strict):
    revision: int = Field(ge=0)
    tools: list[str] = Field(default_factory=list, max_length=50)
    skills: list[str] = Field(default_factory=list, max_length=10)
    purpose: str = Field(default="", max_length=1000)
    skill_mode: Literal["auto", "explicit"] = "explicit"

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.tools)) != len(self.tools) or len(set(self.skills)) != len(
            self.skills
        ):
            raise ValueError("duplicate selection")
        if self.skill_mode == "auto" and self.skills:
            raise ValueError(
                "Automatic Skill scope cannot include an explicit selection"
            )
        return self


class ActionDecision(Strict):
    decision: Literal["accept", "decline"]
    token: str = Field(min_length=16, max_length=256, repr=False)
    subject_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def tool_key(connection_id: str, name: str) -> str:
    return (
        "c_"
        + connection_id.replace("-", "")
        + "_"
        + hashlib.sha256(name.encode()).hexdigest()[:16]
    )


def public_asset(asset: dict) -> dict:
    public = {
        k: v
        for k, v in asset.items()
        if k not in {"secret", "request_id", "request_digest", "owner_actor_id"}
    }

    if asset["kind"] == "connection":
        public["detail"] = {
            **asset["detail"],
            "tools": [
                {**t, "key": tool_key(asset["id"], t["name"])}
                for t in asset["detail"].get("tools", [])
            ],
        }
    if asset["kind"] == "skill":
        public["detail"] = {
            k: v for k, v in asset["detail"].items() if k != "resources"
        }
    return public


def default_selection(assets: list[dict]) -> dict:
    """Freeze defaults once, when the conversation is created."""
    chosen = [a for a in assets if a["status"] == "active" and a["default_selected"]]
    tools = [
        tool_key(a["id"], t["name"])
        for a in chosen
        if a["kind"] == "connection"
        for t in a["detail"].get("tools", [])
    ][:50]
    return {
        "tools": tools,
        "skills": [],
        "skill_mode": "auto",
        "purpose": "",
        "bindings": {a["id"]: a["revision"] for a in chosen},
    }
