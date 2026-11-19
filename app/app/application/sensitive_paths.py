"""Redact bearer capabilities embedded in onboarding endpoint paths."""

import re

_OAUTH_CALLBACK = re.compile(
    r"(/api/workbench/capabilities/oauth/[^/?#\s]+/callback)\?[^\s#]*"
)
_INSTALL_PATH = re.compile(
    r"(/api/agent-install/)[^/?#]+(/(?:script|package))(?:\?[^#]*)?"
)
_AGENT_ONBOARDING_PREFIXES = (
    "/api/agent-pairing/",
    "/api/agent-install/",
    "/api/workbench/agent-pairing/",
    "/api/workbench/agent/install-command",
)


def is_agent_onboarding_path(value: str) -> bool:
    return value.startswith(_AGENT_ONBOARDING_PREFIXES)


def redact_sensitive_path(value: str) -> str:
    value = _OAUTH_CALLBACK.sub(r"\1", value)
    return _INSTALL_PATH.sub(r"\1[redacted]\2", value)
