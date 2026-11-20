"""向 knowmart 取分身清单（SDD 0027 P4，服务身份 knowmart:summon）。

所有者由调用方从用户自己的登录会话里取来；knowmart 只给这个所有者的、运行方是 knowbench 的、
没停用的版本，别的一律 404。环境变量见 service_client.py（`KNOWMART_INTERNAL_URL`、
`KNOWMART_SUMMON_SERVICE_*`），由部署从后端 config 的 service_clients 生成。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from app.application.errors.exceptions import BadGatewayError, ServiceUnavailableError
from app.domain.workbench.errors import KnowbotUnavailable, NoSuchExpert
from app.domain.workbench.formal_method_generated import MethodRef
from app.infrastructure.external.service_client import ServiceClient, service_relation

NOT_FOUND = frozenset({"knowbot_not_found", "not_found"})


class KnowmartSummonClient:
    def __init__(self, environ: Mapping[str, str] | None = None, **client_options: Any):
        self._environ = environ
        self._options = client_options
        self._client: ServiceClient | None = None

    def _connect(self) -> ServiceClient:
        if self._client is None:
            relation = service_relation("knowmart", "summon", self._environ)
            if relation is None:
                raise KnowbotUnavailable("summoning from knowmart is not configured")
            self._client = ServiceClient(relation, **self._options)
        return self._client

    async def eligible_formal_refs(
        self, subject: str, references: list[dict]
    ) -> set[MethodRef]:
        allowed = set()
        for offset in range(0, len(references), 100):
            batch = references[offset : offset + 100]
            try:
                _, body = await self._connect().request(
                    "POST",
                    "/formal-methods/eligible",
                    json={"owner_subject": subject, "references": batch},
                )
            except (BadGatewayError, ServiceUnavailableError) as exc:
                raise KnowbotUnavailable(
                    "Formal method listing is unavailable"
                ) from exc
            if (
                not isinstance(body, dict)
                or body.get("owner_subject") != subject
                or not isinstance(body.get("allowed"), list)
            ):
                raise KnowbotUnavailable("KnowMart returned an invalid use decision")
            try:
                permitted = set(
                    MethodRef.model_validate(ref) for ref in body["allowed"]
                )
            except ValueError as exc:
                raise KnowbotUnavailable(
                    "Invalid reference in KnowMart use decision"
                ) from exc
            if not permitted.issubset(MethodRef.model_validate(ref) for ref in batch):
                raise KnowbotUnavailable("KnowMart returned a different method version")
            allowed.update(permitted)
        return allowed

    async def incubation_tools(self, subject, *, reference=None, after=None, limit=30):
        from pydantic import ValidationError

        from app.domain.workbench.incubation_tools import IncubationGrants

        query = {
            "subject": subject,
            "after": after,
            "limit": limit,
            "reference": reference.model_dump(mode="json") if reference else None,
        }
        try:
            _, raw = await self._connect().request(
                "POST", "/formal-methods/incubation-tools", json=query
            )
            page = IncubationGrants.model_validate(raw)
        except (BadGatewayError, ServiceUnavailableError, ValidationError) as exc:
            raise KnowbotUnavailable("Incubation permissions are unavailable") from exc
        if (
            page.subject != subject
            or len(page.items) > limit
            or any(
                g.owner_subject == subject or (reference and g.reference != reference)
                for g in page.items
            )
            or (reference and (page.next or len(page.items) > 1))
        ):
            raise KnowbotUnavailable(
                "KnowMart returned a different incubation permission"
            )
        return page

    async def summon(
        self, *, owner_subject: str, knowbot_id: str, version: str
    ) -> dict[str, Any]:
        path = (
            f"/owners/{quote(owner_subject, safe='')}/knowbots/"
            f"{quote(knowbot_id, safe='')}@{quote(version, safe='')}"
        )
        try:
            _, body = await self._connect().request("GET", path)
        except BadGatewayError as exc:
            if exc.code in NOT_FOUND:
                raise NoSuchExpert("no such knowbot", knowbot=knowbot_id) from None
            raise KnowbotUnavailable("knowmart refused the summon") from None
        except ServiceUnavailableError:
            raise KnowbotUnavailable("knowmart is unavailable") from None
        if (
            not isinstance(body, dict)
            or body.get("knowbot_id") != knowbot_id
            or body.get("version") != version
            or not isinstance(body.get("manifest"), dict)
            or not isinstance(body.get("digest"), str)
        ):
            raise KnowbotUnavailable("knowmart answered outside the contract")
        return {
            "manifest": body["manifest"],
            "digest": body["digest"],
            "name": body.get("name"),
            "tagline": body.get("tagline"),
        }
