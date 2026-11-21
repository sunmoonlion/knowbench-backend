"""沙箱供给器的 HTTP 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.application.workbench.provisioning import (
    InstructionPrepareNotSent,
    ProvisionerFailed,
    ProvisioningUnavailable,
)


@dataclass(frozen=True)
class ProvisionerConfig:
    url: str
    token: str
    model_provider: str = ""
    model: str = ""
    provider_base_url: str = ""


class HttpProvisioner:
    def __init__(
        self,
        config: ProvisionerConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url,
            headers={"Authorization": f"Bearer {self.config.token}"},
            timeout=60,
            transport=self.transport,
        )

    async def upsert(self, user: str, spec: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as c:
            try:
                r = await c.put(f"/sandboxes/{user}", json=spec)
            except httpx.HTTPError as exc:
                raise ProvisionerFailed("provisioner unreachable") from exc
        if r.status_code != 200:
            raise ProvisionerFailed(f"provisioner returned {r.status_code}")
        return r.json()

    async def status(self, user: str) -> dict[str, Any]:
        async with self._client() as c:
            try:
                r = await c.get(f"/sandboxes/{user}")
            except httpx.HTTPError as exc:
                raise ProvisionerFailed("provisioner unreachable") from exc
        if r.status_code != 200:
            raise ProvisionerFailed(f"provisioner returned {r.status_code}")
        return r.json()

    async def delete(self, user: str, purge: bool = False) -> dict[str, Any]:
        async with self._client() as c:
            try:
                r = await c.delete(
                    f"/sandboxes/{user}", params={"purge": "true"} if purge else None
                )
            except httpx.HTTPError as exc:
                raise ProvisionerFailed("provisioner unreachable") from exc
        if r.status_code != 200:
            raise ProvisionerFailed(f"provisioner returned {r.status_code}")
        return r.json()


class InstructionUnits:
    """Access the private instruction provisioner with the platform service credential."""

    def __init__(self, url, token, transport=None):
        self.url, self.token, self.transport = url, token, transport

    async def request(
        self, owner, task, method, path, *, body=None, params=None, absent=False
    ):
        async with httpx.AsyncClient(
            base_url=self.url,
            transport=self.transport,
            trust_env=False,
            timeout=30,
            headers={"Authorization": f"Bearer {self.token}"},
        ) as client:
            try:
                result = await client.request(
                    method,
                    f"/v1/instruction-executions/{owner}/{task}/{path}",
                    json=body,
                    params=params,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if method == "POST" and path == "prepare":
                    raise InstructionPrepareNotSent(
                        "Instruction reservation was not sent"
                    ) from exc
                raise ProvisionerFailed("Instruction provisioner unavailable") from exc
            except httpx.HTTPError as exc:
                raise ProvisionerFailed("Instruction provisioner unavailable") from exc
        if path == "prepare" and result.status_code == 503:
            # Only these explicit pre-creation refusals prove no reservation exists.
            try:
                detail = result.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            if isinstance(detail, dict) and detail.get("code") in (
                "instruction_prepare_disabled",
                "instruction_control_origin_invalid",
            ):
                raise ProvisioningUnavailable(
                    "Instruction provisioner is not configured"
                )
        if absent and result.status_code == 404:
            return None
        if result.status_code != 200:
            raise ProvisionerFailed(
                f"Instruction provisioner returned {result.status_code}"
            )
        return result.json()
