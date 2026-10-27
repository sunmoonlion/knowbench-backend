"""工作台自己凭这件委托的运行授权读 knowbase（SDD 0048、0030、0036 在第四版上的对应）。

- `compute` 的输入可以是事实编号（`fact:<行>` 标准化层、`raw:<行>` 原样层，模型从
  `query_facts` 拿到）：工作台按编号回 knowbase 取数，模型不抄数；
- 数值验收 `sourced` 的地址是事实编号时同样回查；`quoted` 引的是第四版资料
  （`{material_id, page, text}`）时，取那一页的按页文字找原话。

经 knowbase 数据 MCP 读，令牌当场签、只许这一个工具；授权是这件委托的
（`knowbench:<幂等钥匙>`），委托没钉的读不到。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.data_relay import run_ref
from app.application.workbench.tokens import TokenIssuer
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import (
    ComputeRefused,
    RecordsRefused,
    UserDataUnavailable,
)

FACT_REF = re.compile(
    r"^(?:fact|raw):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
MATERIAL_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")
READER_SANDBOX = "workbench"
_SPACE = re.compile(r"\s+")


def is_fact_ref(value: object) -> bool:
    return isinstance(value, str) and bool(FACT_REF.match(value))


class KnowbaseDataMcp(Protocol):
    async def data_mcp(self, token: str, message: dict[str, Any]) -> dict[str, Any]: ...


class RunReader:
    def __init__(self, knowbase: KnowbaseDataMcp, issuer: TokenIssuer) -> None:
        self.knowbase = knowbase
        self.issuer = issuer

    async def facts(
        self, repo: WorkbenchStore, task: dict[str, Any], refs: list[str]
    ) -> dict[str, Decimal]:
        """按事实编号取数；不在这件委托的授权里、没有、不是数，都回 ComputeRefused。"""
        wanted = list(dict.fromkeys(refs))
        got = await self._call(repo, task, "read_facts", {"refs": wanted})
        rows = {str(f.get("ref")): f for f in got.get("facts") or []}
        values: dict[str, Decimal] = {}
        for ref in wanted:
            row = rows.get(ref)
            if row is None:
                raise ComputeRefused(f"no such fact in this task's data: {ref}")
            number = calc.decimal_of(row.get("value"))
            if number is None:
                raise ComputeRefused(f"the fact has no value: {ref}")
            values[ref] = number
        return values

    async def quotes(
        self, repo: WorkbenchStore, task: dict[str, Any], quotes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """每条引文在那份资料那一页的按页文字里找得到吗（不计空白）。"""
        pages: dict[tuple[str, int], str | None] = {}
        out = []
        for q in quotes:
            key = (str(q["material_id"]), int(q["page"]))
            if key not in pages:
                pages[key] = await self._page(repo, task, *key)
            text = pages[key]
            if text is None:
                out.append({"found": False, "reason": "page_not_readable"})
                continue
            needle = _SPACE.sub("", str(q["text"]))
            hit = bool(needle) and needle in _SPACE.sub("", text)
            out.append(
                {"found": True} if hit else {"found": False, "reason": "not_found"}
            )
        return out

    async def _page(
        self, repo: WorkbenchStore, task: dict[str, Any], material_id: str, page: int
    ) -> str | None:
        try:
            got = await self._call(
                repo,
                task,
                "read_input",
                {
                    "material_id": material_id,
                    "what": "pages",
                    "page_from": page,
                    "page_to": page,
                },
            )
        except ComputeRefused:
            return None
        found = [p for p in got.get("pages") or [] if int(p.get("page") or 0) == page]
        return str(found[0].get("text") or "") if found else None

    async def _call(
        self,
        repo: WorkbenchStore,
        task: dict[str, Any],
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        subject = await repo.subject_of_actor(str(task["owner_actor_id"]))
        if subject is None:
            raise RecordsRefused("this account is not known to the data service")
        token = self.issuer.data_token(
            subject, sandbox=READER_SANDBOX, tools=[name]
        ).token
        try:
            reply = await self.knowbase.data_mcp(
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": {**args, "run": run_ref(task)},
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 — 取不到是平台的事
            raise UserDataUnavailable("the data service cannot be reached") from exc
        if reply.get("error"):
            raise UserDataUnavailable("the data service refused the call")
        result = reply.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or [{}]
            raise ComputeRefused(str(content[0].get("text") or "refused")[:300])
        return result.get("structuredContent") or {}
