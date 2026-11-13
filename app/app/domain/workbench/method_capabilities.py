"""Portable step requirements; account credentials and installation IDs stay outside packs."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class McpRequirement(Requirement):
    url: str = Field(min_length=8, max_length=1000)
    name: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def endpoint(self):
        parts = urlsplit(self.url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "MCP requirement must name an HTTPS endpoint without credentials"
            )
        return self


class SkillRequirement(Requirement):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=50)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    required_tools: tuple[McpRequirement, ...] = Field(
        default=(), max_length=30, exclude_if=lambda value: not value
    )


class MethodCapabilities(Requirement):
    tools: tuple[McpRequirement, ...] = Field(default=(), max_length=30)
    skills: tuple[SkillRequirement, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def unique(self):
        if len({(t.url, t.name) for t in self.tools}) != len(self.tools):
            raise ValueError("duplicate MCP requirement")
        if len({s.name for s in self.skills}) != len(self.skills):
            raise ValueError("duplicate Skill requirement")
        declared = {(t.url, t.name) for t in self.tools}
        if any(
            (t.url, t.name) not in declared
            for s in self.skills
            for t in s.required_tools
        ):
            raise ValueError(
                "Skill dependencies must be declared in the method tool scope"
            )
        return self


class AuthorTool(McpRequirement):
    service_name: str
    description: str


class AuthorSkill(SkillRequirement):
    description: str
    unresolved_dependencies: bool = Field(
        default=False, exclude_if=lambda value: not value
    )


class AuthorCapabilityCatalog(Requirement):
    tools: list[AuthorTool]
    skills: list[AuthorSkill]


def matches_tool(requirements: MethodCapabilities, asset: dict, tool: dict) -> bool:
    return any(
        r.url == asset["detail"]["url"] and r.name == tool["name"]
        for r in requirements.tools
    )


def matches_skill(
    requirements: MethodCapabilities, asset: dict, assets: list[dict]
) -> bool:
    from app.domain.workbench.skill_dependencies import skill_dependencies

    dependencies = skill_dependencies(asset, assets)
    needed = {(d["service_url"], d["tool_name"]) for d in dependencies}
    declared = {(t.url, t.name) for t in requirements.tools}
    return any(
        r.name == asset["name"]
        and r.version == asset["detail"]["version"]
        and r.digest == asset["detail"]["digest"]
        and needed <= declared
        and (
            not r.required_tools
            or needed <= {(t.url, t.name) for t in r.required_tools}
        )
        for r in requirements.skills
    )
