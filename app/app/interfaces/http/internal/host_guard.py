"""内部接口只认集群内的地址（审读第 1 条）。

内部接口和网页、管理端的接口挂在同一个后端上，公网入口把整个 `/api` 转进来。
公网地址来的请求就当没有这个接口（404），不等服务令牌去挡：集群内的调用一律走
`<应用>-tls.<命名空间>.svc.cluster.local`（traefik service-access）或
`<应用>-api` 服务名，本地联调走回环地址。
"""

from __future__ import annotations

from fastapi import Request

from app.application.errors.exceptions import NotFoundError

CLUSTER_SUFFIX = ".svc.cluster.local"
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def is_internal_host(host: str | None) -> bool:
    name = (host or "").lower().rstrip(".")
    return name.endswith(CLUSTER_SUFFIX) or name in LOOPBACK


async def internal_host_only(request: Request) -> None:
    if not is_internal_host(request.url.hostname):
        raise NotFoundError()
