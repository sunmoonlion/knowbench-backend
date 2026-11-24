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
        return await self._use_refs(subject, references, include_acquired=False)

    async def directory_labels(self, subject, references):
        from app.domain.workbench.method_search import DirectoryLabels

        try:
            _, body = await self._connect().request(
                "POST",
                "/formal-methods/directory-labels",
                json={
                    "owner_subject": subject,
                    "references": [r.model_dump(mode="json") for r in references],
                },
            )
            labels = DirectoryLabels.model_validate(body)
            items = [item.reference for item in labels.items]
            if (
                labels.subject != subject
                or set(labels.categories) != {"tool", "expert"}
                or any(ref not in references for ref in items)
                or len({(ref.method_id, ref.version, ref.digest) for ref in items})
                != len(items)
            ):
                raise ValueError("Unexpected category receipt")
            return labels
        except (BadGatewayError, ServiceUnavailableError, ValueError) as exc:
            raise KnowbotUnavailable("Method categories are unavailable") from exc

    async def category_matches(self, subject, terms):
        # A broad facet offers little discrimination. Skip its bonus entirely
        # instead of favoring whichever 100 versions happen to sort first.
        refs, more = await self.planning_window(subject, 0, 100, topic_terms=terms)
        return [] if more else [r.model_dump(mode="json") for r in refs]

    async def planning_window(self, subject, offset, limit, *, topic_terms=None):
        try:
            _, body = await self._connect().request(
                "POST",
                "/formal-methods/planning-window",
                json={
                    "owner_subject": subject,
                    "offset": offset,
                    "limit": limit,
                    **({"topic_terms": topic_terms} if topic_terms else {}),
                },
            )
            if (
                not isinstance(body, dict)
                or body.get("owner_subject") != subject
                or type(body.get("more")) is not bool
            ):
                raise ValueError("invalid subject or continuation")
            raw = body.get("references")
            if (
                not isinstance(raw, list)
                or len(raw) > limit
                or (body["more"] and len(raw) != limit)
            ):
                raise ValueError("invalid window size")
            refs = [MethodRef.model_validate(ref) for ref in raw]
            if len(set(refs)) != len(refs):
                raise ValueError("duplicate version in planning window")
            return refs, body["more"]
        except (BadGatewayError, ServiceUnavailableError, ValueError) as exc:
            raise KnowbotUnavailable(
                "KnowMart planning qualification is unavailable"
            ) from exc

    async def planning_formal_refs(self, subject, references):
        return await self._use_refs(subject, references, include_acquired=True)

    async def _use_refs(self, subject, references, *, include_acquired):
        allowed = set()
        for offset in range(0, len(references), 100):
            batch = references[offset : offset + 100]
            try:
                _, body = await self._connect().request(
                    "POST",
                    "/formal-methods/eligible",
                    json={
                        "owner_subject": subject,
                        "references": batch,
                        **({"include_acquired": True} if include_acquired else {}),
                    },
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

    async def incubation_tools(
        self, subject, *, reference=None, after=None, limit=30, topic_terms=None
    ):
        from pydantic import ValidationError

        from app.domain.workbench.incubation_tools import IncubationGrants

        query = {
            "subject": subject,
            **({"topic_terms": topic_terms} if topic_terms else {}),
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
