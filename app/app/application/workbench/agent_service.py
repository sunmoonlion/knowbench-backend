"""工作台 → 本地代理的服务流（0022，账 87）：经会合点 `/service` 读白名单里的文件、读写代理侧设置。

工作台这头只是搬运工：签一张短期 `role=workbench` 令牌连会合点，会合点把流配到该用户的代理，
之后一问一答 JSON-RPC。能不能读、能不能改，全由代理在用户电脑上判（白名单、放宽须本机确认）；
这里不缓存文件内容，不替代理做任何判断。不起进程、不写用户文件。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.workbench.errors import WorkbenchError

SERVICE_PROTOCOL = 1
SERVICE_SOFTWARE = "workbench"
MAX_FRAME_BYTES = 1024 * 1024  # 0022：单帧 ≤ 1 MiB
MAX_READ_BYTES = (
    512 * 1024
)  # fs/readFile 一次 ≤ 512 KiB 原文：base64 后约 683 KiB，稳在 1 MiB 帧内（astra 2026-10-12 实测 1 MiB 会超）
MAX_PATH_CHARS = 1024
CALL_TIMEOUT_SECONDS = 30.0
# 远程登录 link 的电脑上拉仓库（账 165）：本地 link 那头拉仓库最多 15 分钟（localProjects.ts CLONE_TIMEOUT_MS），这里多等一分钟
CLONE_TIMEOUT_SECONDS = 16 * 60.0


class AgentServiceUnconfigured(WorkbenchError):
    """没配会合点地址或签名私钥：这条路不存在。"""

    code = "agent_service_unconfigured"
    http_status = 503


class AgentOffline(WorkbenchError):
    """这个用户现在没有在线的代理（或还没配对过）。"""

    code = "agent_offline"
    http_status = 503


class AgentOutdated(WorkbenchError):
    """代理在线，但它的版本没有服务流（控制通道 hello 没报 service-v1）。"""

    code = "agent_outdated"
    http_status = 409


class AgentBusy(WorkbenchError):
    """会合点说这个用户的服务流已到上限。"""

    code = "agent_busy"
    http_status = 503


class AgentServiceFailed(WorkbenchError):
    """连不上会合点、超时、回包不成形。"""

    code = "agent_service_failed"
    http_status = 502


class AgentRefused(WorkbenchError):
    """代理按规矩拒了这一问（白名单外、太大、找不到、限速、本机没同意……）。code 与 http_status 按代理回的错误码定。"""

    def __init__(self, rpc_code: int, message: str) -> None:
        code, status = RPC_ERRORS.get(rpc_code, ("agent_refused", 502))
        if rpc_code == PROJECT_REFUSED:
            # 新建项目被拒（账 165）：代理把 LocalProjects 的代码放在 message 里，认得的照搬给网页
            code, status = (
                (message, PROJECT_ERRORS[message])
                if message in PROJECT_ERRORS
                else ("project_failed", 502)
            )
        self.code = code
        self.http_status = status
        super().__init__(message, rpc_code=rpc_code)


# 0022 第三节的错误码 → 给浏览器的 code 与状态
RPC_ERRORS: dict[int, tuple[str, int]] = {
    -32010: ("outside_whitelist", 403),
    -32011: ("too_large", 413),
    -32012: ("not_found", 404),
    -32013: ("rate_limited", 429),
    -32014: ("local_denied", 403),
    -32602: ("invalid_params", 400),
    -32601: ("method_not_found", 502),
}

# 新建项目被拒（账 165，sunmoon-link service.ts SERVICE_PROJECT_REFUSED）：message 是 LocalProjects 的代码
PROJECT_REFUSED = -32020
PROJECT_ERRORS: dict[str, int] = {
    "invalid_params": 400,
    "invalid_url": 400,
    "outside_whitelist": 403,
    "local_read_only": 403,
    "not_found": 404,
    "repo_not_found": 404,
    "not_a_directory": 409,
    "target_not_empty": 409,
    "host_key_unknown": 409,
    "auth_required": 401,
    "ssh_auth_failed": 401,
    "clone_failed": 502,
    "clone_timeout": 504,
    "git_missing": 503,
    "local_failed": 503,
}

REJECT_REASONS: dict[str, type[WorkbenchError]] = {
    "agent offline": AgentOffline,
    "agent has no service channel": AgentOutdated,
    "too many service streams": AgentBusy,
}


def validate_path(path: str) -> str:
    """用户电脑上的绝对路径原样转给代理；这里只挡明显不像路径的东西，白名单由代理判。"""
    if not isinstance(path, str) or not path.strip():
        raise WorkbenchError("path is required")
    if len(path) > MAX_PATH_CHARS or "\x00" in path or "\n" in path or "\r" in path:
        raise WorkbenchError("path is not acceptable")
    return path


class AgentService(Protocol):
    async def call(
        self,
        relay_user: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]: ...
