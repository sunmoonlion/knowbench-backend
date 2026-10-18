"""模型接入地址（0021 §二之 2）：用户自建或代理的 OpenAI 兼容端点。

只许 https、要有主机名、不许带账号密码、查询串、片段；主机不许是本机、内网、链路本地：
这个地址会配进云端沙箱，沙箱按它去连，填个内网地址等于让沙箱替人探内网。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

MAX_LENGTH = 512
_FORBIDDEN_HOSTS = {"localhost", "localhost.localdomain", "0.0.0.0", "::1"}
_FORBIDDEN_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

# 用户的所有 MCP 来源开关；键不在这里的拒绝。knowbase 2026-10-18 撤掉（数据拓扑：平台不给用户
# 数据，用户的沙箱到不了平台的数；待办账 111），库里旧偏好里还留着的这个键读写时都忽略
MCP_SOURCES: tuple[str, ...] = ()
RETIRED_MCP_SOURCES = frozenset({"knowbase"})


def validate_provider_base_url(value: str) -> str:
    """合格就返回去掉末尾斜杠的地址；不合格抛 ValueError（给 422）。"""
    url = value.strip()
    if not url or len(url) > MAX_LENGTH:
        raise ValueError("base_url must be 1–512 characters")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        raise ValueError("base_url must not contain spaces or control characters")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("base_url must use https")
    if parts.username or parts.password:
        raise ValueError("base_url must not carry credentials")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not carry a query or fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("base_url must name a host")
    if host in _FORBIDDEN_HOSTS or host.endswith(_FORBIDDEN_SUFFIXES):
        raise ValueError("base_url must not point at this machine or a private name")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError("base_url must not point at a private or local address")
    if "." not in host and address is None:
        raise ValueError("base_url host must be a public domain name")
    return url.rstrip("/")


def normalize_mcp_sources(value: dict[str, bool] | None) -> dict[str, bool]:
    """空 = 全开；键只认 MCP_SOURCES 里的；撤掉的来源忽略。"""
    if value is None:
        return dict.fromkeys(MCP_SOURCES, True)
    unknown = set(value) - set(MCP_SOURCES) - RETIRED_MCP_SOURCES
    if unknown:
        raise ValueError(f"unknown mcp source: {', '.join(sorted(unknown))}")
    return {name: bool(value.get(name, True)) for name in MCP_SOURCES}
