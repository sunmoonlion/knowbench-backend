"""清单 v2 的专家请来用数据（SDD 0043 第三节、块 6；0048 第五节）。

请专家：填任务参数（主体，必要时覆盖期间）→ knowbase 按声明解析 → 开跑前列出（可换、
有歧义的必须选、可选的可去掉）→ 待确认的用户点一次确认 → 能开跑的本人签运行授权、
钉住、开跑；要补采的交 knowfeed，委托进「等数据」。

等数据（`DataWaiter`，runner 里一条线）：按单号查 knowfeed 的需求单，办完了就请 knowbase
重解析清单、凭本人那次确认续签（所有者 10-27 定「等数据后自动接着跑」）、钉住、委托
接着跑；必填的没取到、确认过了 7 天，委托失败并说明缺什么。委托取消了就撤需求单。

运行授权的 ref 是 `knowbench:<幂等钥匙>`：模型经工作台读数据时，工作台按委托找到它。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores
from app.application.workbench.catalog import PinnedPack
from app.domain.workbench.data_needs_v2 import DataNeed
from app.domain.workbench.data_plan import Plan, item_ref, plan
from app.domain.workbench.errors import DataChoiceInvalid, WorkbenchError
from app.domain.workbench.states import TASK_TERMINAL

log = logging.getLogger(__name__)

UserToken = Callable[[], Awaitable[str]]
# 委托的运行授权：knowbase 最长给 7 天；委托结束由 runner 交还
GRANT_TTL_SECONDS = 7 * 24 * 3600
# 查需求单的间隔：逐次拉长，最长 5 分钟
CHECK_BACKOFF = (
    timedelta(seconds=20),
    timedelta(seconds=40),
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
)
OPEN_DEMAND = ("accepted", "fetching")


class KnowbaseData(Protocol):
    async def resolve(self, user_token: str, **kw: Any) -> dict[str, Any]: ...
    async def resolution(
        self, user_token: str, resolution_id: str, revision: int | None = None
    ) -> dict[str, Any]: ...
    async def confirm(self, user_token: str, **kw: Any) -> dict[str, Any]: ...
    async def run_grant(self, user_token: str, **kw: Any) -> dict[str, Any]: ...
    async def register_session(self, user_token: str) -> dict[str, Any]: ...
    async def refresh(self, subject: str, resolution_id: str) -> dict[str, Any]: ...
    async def service_resolution(
        self, subject: str, resolution_id: str, revision: int | None = None
    ) -> dict[str, Any]: ...
    async def add_needs(
        self, subject: str, resolution_id: str, needs: list[dict[str, Any]]
    ) -> dict[str, Any]: ...
    async def grant_by_confirmation(self, **kw: Any) -> dict[str, Any]: ...
    async def release_run_grant(self, grant_id: str) -> None: ...


class SupplyDemands(Protocol):
    def configured(self) -> bool: ...
    async def submit(self, **kw: Any) -> dict[str, Any]: ...
    async def get(self, demand_id: str) -> dict[str, Any]: ...
    async def cancel(self, demand_id: str) -> dict[str, Any]: ...


@dataclass
class Prepared:
    """建委托前定好的：要钉的、清单与等数据的记录（task_id 建委托时补）。"""

    pins: list[dict[str, Any]] = field(default_factory=list)
    grant_id: str | None = None
    resolution_row: dict[str, Any] | None = None


def params_of(subjects: list[str], period: tuple[date | None, date | None]):
    return {
        "subjects": subjects,
        "task_date": datetime.now(UTC).date().isoformat(),
        "period_start": period[0].isoformat() if period[0] else None,
        "period_end": period[1].isoformat() if period[1] else None,
    }


class ExpertData:
    def __init__(
        self,
        knowbase: KnowbaseData,
        supply: SupplyDemands | None,
        *,
        supply_enabled: bool,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.knowbase = knowbase
        self.supply = supply
        # fail-closed：开关开着、knowfeed 也配好了才补采
        self.supply_enabled = bool(
            supply_enabled and supply is not None and supply.configured()
        )
        self.now = now

    async def listing(
        self,
        pinned: PinnedPack,
        *,
        subjects: list[str],
        period: tuple[date | None, date | None],
        user_token: UserToken,
    ) -> dict[str, Any]:
        """开跑前列出：按声明解析，回清单与每一项的去向（照 knowbase 的结果）。"""
        resolution = await self.knowbase.resolve(
            await user_token(),
            consumer=pinned.consumer,
            needs=[n.model_dump(mode="json") for n in pinned.needs_v2],
            params=params_of(subjects, period),
        )
        draft = plan(
            resolution,
            list(pinned.needs_v2),
            pinned.bindings,
            supply_enabled=self.supply_enabled,
        )
        # 每个条目带上编号：页面换选、去掉时按它回传（编号由这里算，页面不照算）
        for entry in resolution.get("items") or []:
            entry["ref"] = item_ref(entry)
        return {
            "resolution": resolution,
            "supply_enabled": self.supply_enabled,
            "bound": sum(1 for p in draft.pins if p["basis"] == "frozen_binding"),
            "to_confirm": len(draft.confirm),
            "problems": draft.problems,
            "incomplete": draft.incomplete,
        }

    async def prepare(
        self,
        pinned: PinnedPack,
        *,
        choice: dict[str, Any],
        owner_subject: str,
        ref: str,
        idempotency_key: str,
        user_token: UserToken,
    ) -> Prepared:
        """建委托之前：按用户在清单上的选择定计划。都齐了本人签授权；要补采的确认、
        提需求单，委托建好后进「等数据」。没写声明、只带作者绑定的，签绑定那几份。"""
        if not pinned.needs_v2:
            bound = plan({"items": []}, [], pinned.bindings)
            prepared = Prepared()
            if bound.pins:
                prepared.grant_id = await self._grant_by_user(
                    await user_token(), ref, bound
                )
                prepared.pins = _with_grant(bound.pins, prepared.grant_id)
            return prepared
        resolution_id = str(choice.get("resolution_id") or "")
        revision = int(choice.get("revision") or 0)
        if not resolution_id or revision < 1:
            raise DataChoiceInvalid("list the data first, then ask the expert")
        picks = dict(choice.get("picks") or {})
        dropped = list(choice.get("dropped") or [])
        token = await user_token()
        resolution = await self.knowbase.resolution(token, resolution_id, revision)
        if resolution.get("consumer") != pinned.consumer:
            raise DataChoiceInvalid("this list was made for another expert")
        draft = plan(
            resolution,
            list(pinned.needs_v2),
            pinned.bindings,
            picks=picks,
            dropped=dropped,
            supply_enabled=self.supply_enabled,
        )
        if draft.problems:
            raise DataChoiceInvalid("; ".join(draft.problems))
        row: dict[str, Any] = {
            "resolution_id": resolution_id,
            "revision": revision,
            "consumer": pinned.consumer,
            "params": resolution.get("params") or {},
            "grant_ref": ref,
            "picks": picks,
            "dropped": dropped,
            "status": "ready",
            "detail": {"incomplete": draft.incomplete},
        }
        if draft.confirm:
            return await self._confirm_and_supply(
                draft, row, token, owner_subject, idempotency_key, pinned
            )
        prepared = Prepared(resolution_row=row)
        if draft.pins:
            prepared.grant_id = await self._grant_by_user(token, ref, draft)
            prepared.pins = _with_grant(draft.pins, prepared.grant_id)
        return prepared

    async def release(self, prepared: Prepared) -> None:
        """建委托没成：刚签的授权交还、刚提的需求单撤掉（不成也不挡，到期自己失效）。"""
        try:
            if prepared.grant_id:
                await self.knowbase.release_run_grant(prepared.grant_id)
            demand = (prepared.resolution_row or {}).get("demand_id")
            if demand and self.supply is not None:
                await self.supply.cancel(str(demand))
        except Exception:  # noqa: BLE001 — 交还失败不挡，记一行
            log.warning("could not hand back an unused grant or demand")

    async def confirm_requests(
        self,
        repo: WorkbenchStore,
        task: dict[str, Any],
        *,
        owner_subject: str,
        user_token: UserToken,
    ) -> dict[str, Any]:
        """用户在页面上确认专家运行中新要的数据：这几项单独确认（本人点的就是这一下），
        齐的本人签一条追加的授权、追加钉住，缺的交 knowfeed（委托接着等）。"""
        task_id = str(task["id"])
        row = await repo.get_task_resolution(task_id)
        pending = [r for r in pending_requests(row) if r.get("status") == "pending"]
        if row is None or not pending:
            raise DataChoiceInvalid("nothing is waiting for your confirmation")
        if row["status"] == "waiting":
            raise DataChoiceInvalid("data for this task is still being fetched")
        token = await user_token()
        latest = await self.knowbase.resolution(token, str(row["resolution_id"]))
        needs = [n for r in pending for n in request_needs(r)]
        scoped = _scoped(latest, needs)
        draft = plan(scoped, needs, supply_enabled=self.supply_enabled)
        detail = dict(row.get("detail") or {})
        ids = {r["id"] for r in pending}
        if draft.confirm:
            confirmed = await self.knowbase.confirm(
                token,
                items=[
                    {"key": c["key"], "need_key": c["need_key"]} for c in draft.confirm
                ],
                task_ref=str(row["grant_ref"]),
                resolution_id=str(row["resolution_id"]),
                revision=int(latest["revision"]),
            )
            supply = [
                b["item_id"]
                for b in confirmed.get("branches") or []
                if b.get("branch") == "supply"
            ]
            if supply:
                assert self.supply is not None
                by_id = {str(i.get("item_id")): i for i in confirmed.get("items") or []}
                demand = await self.supply.submit(
                    owner=owner_subject,
                    confirmation_id=str(confirmed["confirmation_id"]),
                    idempotency_key=f"kb-{task_id}-{'-'.join(sorted(ids))}"[:128],
                    task_ref=str(row["grant_ref"]),
                    proposer="expert",
                    chain=[str(row["consumer"]), task_id],
                    items=[
                        {
                            "confirmation_item": i,
                            "key": by_id[i]["key"],
                            "readings": [str(by_id[i].get("reading") or "original")],
                        }
                        for i in supply
                    ],
                )
                detail["requests"] = _mark(detail, ids, "supplying")
                detail["phase"] = {"requests": sorted(ids)}
                detail["waiting_for"] = [c["label"] for c in draft.confirm]
                async with repo.transaction():
                    await repo.update_task_resolution(
                        task_id,
                        confirmation_id=str(confirmed["confirmation_id"]),
                        demand_id=str(demand["demand_id"]),
                        deadline=_parse_time(confirmed.get("expires_at")),
                        status="waiting",
                        detail=detail,
                        checks=0,
                        next_check_at=self.now() + CHECK_BACKOFF[0],
                    )
                return {"status": "supplying", "waiting_for": detail["waiting_for"]}
            latest = await self.knowbase.refresh(
                owner_subject, str(row["resolution_id"])
            )
            draft = plan(_scoped(latest, needs), needs, after_supply=True)
        pins = await _new_pins(repo, task_id, draft.pins)
        if pins:
            grant_id = await self._grant_by_user(
                token, str(row["grant_ref"]), Plan(pins=pins)
            )
            pins = _with_grant(pins, grant_id)
        from app.application.workbench.ledger import Ledger

        detail["requests"] = _mark(detail, ids, "done")
        detail["incomplete"] = list(detail.get("incomplete") or []) + draft.incomplete
        async with repo.transaction():
            if pins:
                await repo.pin_task_data(task_id, pins)
            await repo.update_task_resolution(task_id, detail=detail)
            await Ledger(repo).resume_data(task_id)
        return {"status": "ready", "added": len(pins)}

    async def decline_requests(
        self, repo: WorkbenchStore, task: dict[str, Any]
    ) -> dict[str, Any]:
        """用户不要专家新要的数据：委托照现有的接着做。"""
        from app.application.workbench.ledger import Ledger

        task_id = str(task["id"])
        row = await repo.get_task_resolution(task_id)
        pending = [r for r in pending_requests(row) if r.get("status") == "pending"]
        if row is None or not pending:
            raise DataChoiceInvalid("nothing is waiting for your confirmation")
        detail = dict(row.get("detail") or {})
        detail["requests"] = _mark(detail, {r["id"] for r in pending}, "declined")
        async with repo.transaction():
            await repo.update_task_resolution(task_id, detail=detail)
            if row["status"] != "waiting":
                await Ledger(repo).resume_data(task_id)
        return {"status": "declined"}

    async def _confirm_and_supply(
        self,
        draft: Plan,
        row: dict[str, Any],
        token: str,
        owner_subject: str,
        idempotency_key: str,
        pinned: PinnedPack,
    ) -> Prepared:
        assert self.supply is not None
        confirmed = await self.knowbase.confirm(
            token,
            items=[{"key": c["key"], "need_key": c["need_key"]} for c in draft.confirm],
            task_ref=row["grant_ref"],
            resolution_id=row["resolution_id"],
            revision=row["revision"],
        )
        confirmation_id = str(confirmed["confirmation_id"])
        row["confirmation_id"] = confirmation_id
        row["deadline"] = _parse_time(confirmed.get("expires_at"))
        supply = [
            b["item_id"]
            for b in confirmed.get("branches") or []
            if b.get("branch") == "supply"
        ]
        items_by_id = {str(i.get("item_id")): i for i in confirmed.get("items") or []}
        if not supply:
            # 都已立即就绪（公开原件层里已有、当场记了引用）：重解析后本人签
            fresh = await self.knowbase.refresh(owner_subject, row["resolution_id"])
            again = plan(
                fresh,
                list(pinned.needs_v2),
                pinned.bindings,
                picks=row["picks"],
                dropped=row["dropped"],
                after_supply=True,
            )
            if again.problems:
                raise DataChoiceInvalid("; ".join(again.problems))
            row["revision"] = int(fresh["revision"])
            row["detail"] = {"incomplete": again.incomplete}
            prepared = Prepared(resolution_row=row)
            if again.pins:
                prepared.grant_id = await self._grant_by_user(
                    token, row["grant_ref"], again
                )
                prepared.pins = _with_grant(again.pins, prepared.grant_id)
            return prepared
        demand = await self.supply.submit(
            owner=owner_subject,
            confirmation_id=confirmation_id,
            idempotency_key=f"kb-{idempotency_key}"[:128],
            task_ref=row["grant_ref"],
            proposer="user",
            chain=[pinned.consumer],
            items=[
                {
                    "confirmation_item": item_id,
                    "key": items_by_id[item_id]["key"],
                    "readings": [
                        str(items_by_id[item_id].get("reading") or "original")
                    ],
                }
                for item_id in supply
            ],
        )
        row["demand_id"] = str(demand["demand_id"])
        row["status"] = "waiting"
        row["next_check_at"] = self.now() + CHECK_BACKOFF[0]
        row["detail"] = {
            "incomplete": draft.incomplete,
            "waiting_for": [c["label"] for c in draft.confirm],
            "demand_status": demand.get("status"),
        }
        return Prepared(resolution_row=row)

    async def _grant_by_user(self, token: str, ref: str, draft: Plan) -> str:
        grant = await self.knowbase.run_grant(
            token,
            process="delegation",
            ref=ref,
            items=draft.grant_items,
            ttl_seconds=GRANT_TTL_SECONDS,
        )
        return str(grant["grant_id"])


# —— 会话（0048 第五节）——

# 会话 24 小时；离到期不到 1 小时就重登一段
SESSION_RENEW_BEFORE = timedelta(hours=1)


async def ensure_data_session(
    repo: WorkbenchStore,
    knowbase: KnowbaseData,
    session: dict[str, Any],
    user_token: UserToken,
    *,
    now: datetime | None = None,
) -> str | None:
    """用户在对话里发消息时：这段对话在 knowbase 登记过会话、还没快到期就照用，否则用
    本人令牌登记一段（范围是他放开给 knowbench 的加公开原件；模型首读一份时 knowbase
    当场签小授权）。登记不成不挡聊天，回 None。"""
    current = now or datetime.now(UTC)
    found = session.get("knowbase_session_id")
    expires = session.get("knowbase_session_expires_at")
    if found and expires is not None and expires - SESSION_RENEW_BEFORE > current:
        return str(found)
    try:
        made = await knowbase.register_session(await user_token())
    except Exception as exc:  # noqa: BLE001 — 读不了数据也照样能聊
        log.warning(
            "could not open a data session code=%s",
            getattr(exc, "code", type(exc).__name__),
        )
        return None
    expires_at = _parse_time(made.get("expires_at")) or current + timedelta(hours=24)
    async with repo.transaction():
        await repo.set_knowbase_session(
            str(session["id"]), str(made["session_id"]), expires_at
        )
    return str(made["session_id"])


def data_session_note(session: dict[str, Any], now: datetime) -> str | None:
    """这一轮给模型的说明：这段对话能读用户的数据、怎么读。没登记、过期了就不说。"""
    expires = session.get("knowbase_session_expires_at")
    if not session.get("knowbase_session_id") or expires is None or expires <= now:
        return None
    conversation = str(session["id"])
    return (
        "You can read the user's data in this conversation: what they show to this "
        "app and public disclosures. List it with "
        f'list_library(conversation="{conversation}"); find a public document by its '
        "exact description with lookup; read one with "
        f'read_input(conversation="{conversation}", material_id=…, what=pages|text); '
        f'take figures with query_facts(conversation="{conversation}") from what you '
        "have read; find passages with search_evidence. Quote the source of every "
        "figure and passage."
    )


# —— 运行中专家要新数据（0048 第五节 request_data）——

ALL_FORMATS = ["pdf", "excel", "csv", "text", "markdown", "html"]
MAX_REQUEST_NEEDS = 5


def pending_requests(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    """还没办完的请求：等用户确认的、在补采的。"""
    requests = ((row or {}).get("detail") or {}).get("requests") or []
    return [r for r in requests if r.get("status") in ("pending", "supplying")]


def holds_task(row: dict[str, Any] | None) -> bool:
    """委托要不要停在「等数据」：在补采，或专家要的新数据还没确认。"""
    return row is not None and (
        row["status"] == "waiting" or bool(pending_requests(row))
    )


def request_needs(request: dict[str, Any]) -> list[DataNeed]:
    return [DataNeed.model_validate(n) for n in request.get("needs") or []]


async def request_data(
    repo: WorkbenchStore,
    knowbase: KnowbaseData,
    task: dict[str, Any],
    asked: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """专家运行中发现还要一份没钉的：清单出新修订、列进用户的确认清单，任务在步骤边界
    停下等用户确认。模型不能替用户确认（0043 第 3.3 节第 6 条）。"""
    task_id = str(task["id"])
    row = await repo.get_task_resolution(task_id)
    if row is None:
        raise DataChoiceInvalid("this task does not use listed data")
    if not isinstance(asked, list) or not 1 <= len(asked) <= MAX_REQUEST_NEEDS:
        raise DataChoiceInvalid(f"ask for 1 to {MAX_REQUEST_NEEDS} items")
    subject = await repo.subject_of_actor(str(task["owner_actor_id"]))
    if subject is None:
        raise DataChoiceInvalid("this account is not known to the data service")
    detail = dict(row.get("detail") or {})
    requests = list(detail.get("requests") or [])
    number = len(requests) + 1
    needs = [_asked_need(a, number, n) for n, a in enumerate(asked, start=1)]
    try:
        fresh = await knowbase.add_needs(
            subject,
            str(row["resolution_id"]),
            [n.model_dump(mode="json") for n in needs],
        )
    except WorkbenchError:
        raise
    except Exception as exc:
        translated = refusal(exc)
        if translated is None:
            raise
        raise translated from None
    keys = {n.key for n in needs}
    items = [i for i in fresh.get("items") or [] if i.get("need_key") in keys]
    request = {
        "id": f"r{number}",
        "revision": int(fresh["revision"]),
        "needs": [n.model_dump(mode="json") for n in needs],
        "labels": [n.name for n in needs],
        "statuses": [str(i.get("status")) for i in items],
        "status": "pending",
        "requested_at": (now or datetime.now(UTC)).isoformat(),
    }
    detail["requests"] = requests + [request]
    async with repo.transaction():
        await repo.update_task_resolution(task_id, detail=detail)
    return {
        "status": "listed",
        "request": request["id"],
        "items": [
            {"name": n.name, "status": st}
            for n, st in zip(needs, request["statuses"], strict=False)
        ],
        "message": (
            "Listed for the user to confirm; you cannot read it before that. Finish "
            "this step with the data you have and say what is missing. After the user "
            "confirms, list_inputs will show the new items."
        ),
    }


def _asked_need(asked: dict[str, Any], number: int, n: int) -> DataNeed:
    if not isinstance(asked, dict):
        raise DataChoiceInvalid("each item is an object")
    period = asked.get("period") or {"kind": "none"}
    reason = str(asked.get("reason") or "").strip()
    try:
        return DataNeed.model_validate(
            {
                "key": f"req{number}_{n}",
                "name": (reason or str(asked.get("doc_type")))[:80],
                "doc_type": asked.get("doc_type"),
                "period": period,
                "formats": ALL_FORMATS,
                "required": False,
                "min_count": 1,
                "max_count": int(asked.get("max_count") or 1),
                "reading": asked.get("reading") or "original",
                "description": reason[:500],
                **({"topics": asked["topics"]} if asked.get("topics") else {}),
                **({"edition": asked["edition"]} if asked.get("edition") else {}),
            }
        )
    except ValidationError as exc:
        where = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise DataChoiceInvalid(
            "this item is not in the vocabulary: " + ", ".join(where[:5])
        ) from None


def _scoped(resolution: dict[str, Any], needs: list[DataNeed]) -> dict[str, Any]:
    keys = {n.key for n in needs}
    return {
        **resolution,
        "items": [
            i for i in resolution.get("items") or [] if i.get("need_key") in keys
        ],
    }


def _mark(detail: dict[str, Any], ids: set[str], status: str) -> list[dict[str, Any]]:
    return [
        {**r, "status": status} if r.get("id") in ids else r
        for r in detail.get("requests") or []
    ]


async def _new_pins(
    repo: WorkbenchStore, task_id: str, pins: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """追加的钉住去掉已经钉过的那几份（同一份同一版不钉两次）。"""
    have = {
        (str(p["item_id"]), int(p["version_no"] or 0))
        for p in await repo.list_task_data(task_id)
        if p.get("item_id")
    }
    return [p for p in pins if (p["item_id"], p["version_no"]) not in have]


def _with_grant(pins: list[dict[str, Any]], grant_id: str) -> list[dict[str, Any]]:
    return [{**p, "grant_id": grant_id, "grant_kind": "delegation"} for p in pins]


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class DataWaiter:
    """等数据的委托（runner 里一条线，隔一会儿扫一遍到点该查的）。"""

    def __init__(
        self,
        stores: WorkbenchStores,
        knowbase: KnowbaseData,
        supply: SupplyDemands,
        *,
        pinned_of: Callable[[WorkbenchStore, dict[str, Any]], Awaitable[PinnedPack]],
        interval_seconds: float = 15.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.stores = stores
        self.knowbase = knowbase
        self.supply = supply
        self.pinned_of = pinned_of
        self.interval_seconds = interval_seconds
        self.now = now

    async def sweep(self) -> int:
        async with self.stores() as repo:
            due = await repo.due_task_resolutions(self.now())
        handled = 0
        for row in due:
            try:
                await self._check(row)
                handled += 1
            except Exception as exc:  # noqa: BLE001 — 一件出错不挡别的
                status = int(getattr(exc, "status", 0) or 0)
                code = str(getattr(exc, "code", "") or type(exc).__name__)
                if 400 <= status < 500:
                    # knowbase、knowfeed 明说不行（确认对不上、清单不是他的……）：不再等
                    log.warning(
                        "data wait refused task=%s code=%s", row["task_id"], code
                    )
                    await self._fail(
                        row, f"数据服务拒绝了：{code}", row.get("detail") or {}
                    )
                else:
                    log.warning(
                        "data wait check failed task=%s code=%s", row["task_id"], code
                    )
                    await self._later(row)
        return handled

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("data waiter starting interval=%ss", self.interval_seconds)
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — 这条线不能因为一次出错就停
                log.exception("data waiter sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    async def _check(self, row: dict[str, Any]) -> None:
        task_id = str(row["task_id"])
        if row["task_state"] in TASK_TERMINAL:
            # 委托取消了、失败了：撤需求单（共享的作业照跑，别人还在等）
            if row.get("demand_id"):
                try:
                    await self.supply.cancel(str(row["demand_id"]))
                except Exception:  # noqa: BLE001
                    log.warning("could not cancel demand task=%s", task_id)
            await self._settle(task_id, "cancelled", row, {})
            return
        demand = await self.supply.get(str(row["demand_id"]))
        status = str(demand.get("status"))
        detail = {**(row.get("detail") or {}), "demand_status": status}
        if status in OPEN_DEMAND:
            deadline = row.get("deadline")
            if deadline is not None and self.now() >= deadline:
                await self._fail(row, "确认已过 7 天，数据还没取到", detail)
                return
            async with self.stores() as repo:
                async with repo.transaction():
                    await repo.update_task_resolution(
                        task_id,
                        detail=detail,
                        checks=int(row["checks"]) + 1,
                        next_check_at=self.now() + _backoff(int(row["checks"]) + 1),
                    )
            return
        await self._resume(row, detail)

    async def _resume(self, row: dict[str, Any], detail: dict[str, Any]) -> None:
        task_id = str(row["task_id"])
        async with self.stores() as repo:
            task = await repo.get_task(task_id)
            subject = await repo.subject_of_actor(str(task["owner_actor_id"]))
            pinned = await self.pinned_of(repo, task)
        if subject is None:
            await self._fail(row, "找不到委托人的账号", detail)
            return
        fresh = await self.knowbase.refresh(subject, str(row["resolution_id"]))
        phase = (row.get("detail") or {}).get("phase") or {}
        if phase.get("requests"):
            # 补的是专家运行中新要的：只看那几项，缺的照做（记不完整），已钉的不重钉
            ids = set(phase["requests"])
            needs = [
                n
                for r in (row.get("detail") or {}).get("requests") or []
                if r.get("id") in ids
                for n in request_needs(r)
            ]
            done = plan(_scoped(fresh, needs), needs, after_supply=True)
            async with self.stores() as repo:
                done.pins = await _new_pins(repo, task_id, done.pins)
            detail = {
                **detail,
                "requests": _mark(row.get("detail") or {}, ids, "done"),
                "phase": {},
                "incomplete": list((row.get("detail") or {}).get("incomplete") or [])
                + done.incomplete,
            }
        else:
            done = plan(
                fresh,
                list(pinned.needs_v2),
                pinned.bindings,
                picks=dict(row.get("picks") or {}),
                dropped=list(row.get("dropped") or []),
                after_supply=True,
            )
            if done.problems:
                await self._fail(row, "；".join(done.problems), detail)
                return
            detail = {**detail, "incomplete": done.incomplete}
        revision = int(fresh["revision"])
        pins: list[dict[str, Any]] = []
        if done.pins:
            grant = await self.knowbase.grant_by_confirmation(
                subject=subject,
                confirmation_id=str(row["confirmation_id"]),
                resolution_id=str(row["resolution_id"]),
                revision=revision,
                ref=str(row["grant_ref"]),
                items=done.grant_items,
                ttl_seconds=GRANT_TTL_SECONDS,
            )
            pins = _with_grant(done.pins, str(grant["grant_id"]))
        from app.application.workbench.ledger import Ledger

        async with self.stores() as repo:
            async with repo.transaction():
                current = await repo.get_task_resolution(task_id, for_update=True)
                if current is None or current["status"] != "waiting":
                    return  # 别处已经办过（重复、乱序的检查不重复开跑）
                if pins:
                    await repo.pin_task_data(task_id, pins)
                await repo.update_task_resolution(
                    task_id,
                    status="ready",
                    revision=revision,
                    detail=detail,
                    next_check_at=None,
                )
                await Ledger(repo).resume_data(task_id)

    async def _fail(self, row: dict[str, Any], reason: str, detail: dict[str, Any]):
        from app.application.workbench.ledger import Ledger

        task_id = str(row["task_id"])
        phase = (row.get("detail") or {}).get("phase") or {}
        async with self.stores() as repo:
            async with repo.transaction():
                current = await repo.get_task_resolution(task_id, for_update=True)
                if current is None or current["status"] != "waiting":
                    return
                if phase.get("requests"):
                    # 专家运行中新要的没取到：委托照现有的接着做，缺的记下
                    ids = set(phase["requests"])
                    await repo.update_task_resolution(
                        task_id,
                        status="ready",
                        detail={
                            **detail,
                            "requests": _mark(
                                current.get("detail") or {}, ids, "failed"
                            ),
                            "phase": {},
                            "reason": reason,
                        },
                        next_check_at=None,
                    )
                    await Ledger(repo).resume_data(task_id)
                    return
                await repo.update_task_resolution(
                    task_id,
                    status="failed",
                    detail={**detail, "reason": reason},
                    next_check_at=None,
                )
                await Ledger(repo).fail_waiting_data(task_id, reason)

    async def _settle(
        self, task_id: str, status: str, row: dict[str, Any], extra: dict[str, Any]
    ) -> None:
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.update_task_resolution(
                    task_id,
                    status=status,
                    detail={**(row.get("detail") or {}), **extra},
                    next_check_at=None,
                )

    async def _later(self, row: dict[str, Any]) -> None:
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.update_task_resolution(
                    str(row["task_id"]),
                    checks=int(row["checks"]) + 1,
                    next_check_at=self.now() + _backoff(int(row["checks"]) + 1),
                )


def _backoff(checks: int) -> timedelta:
    return CHECK_BACKOFF[min(checks, len(CHECK_BACKOFF) - 1)]


def refusal(exc: Exception) -> WorkbenchError | None:
    """knowbase、knowfeed 的 4xx 拒绝转成工作台的话（网页按 code 说人话）；别的原样。"""
    status = int(getattr(exc, "status", 0) or 0)
    if not 400 <= status < 500:
        return None
    code = str(getattr(exc, "code", "") or "refused")
    if code in ("not_shown", "no_reference", "public_access_closed", "not_bound"):
        return DataChoiceInvalid(f"a chosen item cannot be read: {code}")
    if code in ("resolution_not_found", "confirmation_not_found"):
        return DataChoiceInvalid("this list is gone; list the data again")
    return DataChoiceInvalid(f"the data service refused: {code}")
