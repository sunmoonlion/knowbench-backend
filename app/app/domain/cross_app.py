"""一个应用把用户带到另一个应用（跨应用跳转的约定）。

约定只有几条，都在这里：
- 形式是普通链接，在新标签页打开；
- 链接里带业务参数、`from`（从哪个应用来）、`ref`（来处的引用）；
- 链接里不带身份、不带令牌、不带回去的地址。用户是谁，由他在目标应用的登录决定；
- 回去的地址由目标应用按 `from` 在自己的配置里查，把 `ref` 填进去；
- `ref` 是不透明的字符串：目标应用只存、只原样显示，不解释。

链接带来的参数不可信。不认识的应用、不合规则的引用，都当作没带，不报错：
用户只是点了一个链接，不该因为链接不对就看到错误页。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

APP = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
REF = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_URL_CHARS = 1024
REF_SLOT = "{ref}"


@dataclass(frozen=True)
class Origin:
    """用户是从哪个应用带过来的。"""

    app: str
    ref: str | None = None


@dataclass(frozen=True)
class Source:
    """可以把用户带到这里来的一个应用。没登记回跳地址的，页面上不显示「回到原处」。"""

    app: str
    return_url: str | None = None


@dataclass(frozen=True)
class Target:
    """这个应用会把用户带去的一个应用。"""

    app: str
    web_base_url: str


def clean_ref(ref: object) -> str | None:
    return ref if isinstance(ref, str) and REF.fullmatch(ref) else None


def clean_origin(
    app: object, ref: object, *, known_apps: frozenset[str] | set[str]
) -> Origin | None:
    if not isinstance(app, str) or not APP.fullmatch(app) or app not in known_apps:
        return None
    return Origin(app=app, ref=clean_ref(ref))


def return_url(source: Source | None, origin: Origin | None) -> str | None:
    """回到原处的地址。只从配置里来；链接里带的任何地址都不看。"""
    if source is None or origin is None or source.app != origin.app:
        return None
    if source.return_url is None:
        return None
    return source.return_url.replace(REF_SLOT, quote(origin.ref or "", safe=""))


def _absolute_url(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS:
        raise ValueError(f"{field} must be an absolute http(s) URL")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  端口不是数字时在这里报错
    except ValueError as exc:
        raise ValueError(f"{field} must be an absolute http(s) URL") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"{field} must be an absolute http(s) URL")
    return value


def _entries(text: str, *, field: str, allowed: set[str]) -> dict[str, dict]:
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} is not JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be an object")
    for name, entry in parsed.items():
        if not isinstance(name, str) or not APP.fullmatch(name):
            raise ValueError(f"{field} has an invalid application name")
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError(f"{field} has an invalid entry for {name}")
    return parsed


def parse_sources(
    text: str, *, field: str = "CROSS_APP_SOURCES_JSON"
) -> dict[str, Source]:
    """哪些应用可以把用户带到这里，各自的回跳地址。

    例：{"knowbench": {"return_url": "https://…/zh-CN/workbench?ref={ref}"},
         "knowbase": {}}
    """
    sources: dict[str, Source] = {}
    for name, entry in _entries(text, field=field, allowed={"return_url"}).items():
        template = entry.get("return_url")
        if template is None:
            sources[name] = Source(app=name)
            continue
        if not isinstance(template, str):
            raise ValueError(f"{field} return_url of {name} is invalid")
        rest = template.replace(REF_SLOT, "", 1)
        if "{" in rest or "}" in rest:
            raise ValueError(f"{field} return_url of {name} holds more than {{ref}}")
        _absolute_url(template.replace(REF_SLOT, "%3A" * 128), field=field)
        # {ref} 只能出现在地址的路径或参数里，不能出现在主机名里
        if REF_SLOT in (urlsplit(template).netloc or ""):
            raise ValueError(f"{field} return_url of {name} is invalid")
        sources[name] = Source(app=name, return_url=template)
    return sources


def parse_targets(
    text: str, *, field: str = "CROSS_APP_TARGETS_JSON"
) -> dict[str, Target]:
    """这个应用会把用户带去哪些应用，各自网页端的地址。

    例：{"knowfeed": {"web_base_url": "https://knowfeed.example"}}
    """
    targets: dict[str, Target] = {}
    for name, entry in _entries(text, field=field, allowed={"web_base_url"}).items():
        base = _absolute_url(entry.get("web_base_url"), field=field)
        parts = urlsplit(base)
        if parts.query:
            raise ValueError(f"{field} web_base_url of {name} must not hold a query")
        targets[name] = Target(app=name, web_base_url=base.rstrip("/"))
    return targets


def known_apps(sources: Mapping[str, Source]) -> frozenset[str]:
    return frozenset(sources)
