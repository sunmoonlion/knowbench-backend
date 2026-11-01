"""清单 v2 的专家请来用数据（SDD 0043 第三节、块 6a；0048 第五节；decisions ⑭）。

请专家（本人在场）：在 knowbase 先建一张空清单；作者挑选的数据（冻结的绑定）当场本人签
运行授权、钉住；委托进「定数据」。
第 0 步（advisor）：模型读专家逻辑和用户问题，用 `request_data` 提出要哪些数据，清单出新
修订，委托在步骤之间停下等用户确认。用户确认时能换一份、改主体或期间、去掉、自己加一样；
齐的本人签授权、钉住、接着跑；要补采的交 knowfeed，委托进「等数据」。运行中再要也一样。

等数据（`DataWaiter`，runner 里一条线）：按单号查 knowfeed 的需求单，办完了就请 knowbase
重解析清单、凭本人那次确认续签（所有者 10-27 定「等数据后自动接着跑」）、钉住、委托
接着跑；有没取到的（或确认过了 7 天），停下问用户：照现有的接着做（记「数据不全」），
还是取消。委托取消了就撤需求单。knowfeed 推来需求单办完了（契约 knowfeed-supply-status/v1，
SDD 0045 第 2.5 节）就马上查这一张，不等到点；按单号轮询留着兜底。

预切合（契约 knowfeed-supply-prematch/v1，0043 第 3.3 节第 1 条）：确认面板上待确认的几项
先问 knowfeed 能不能取、许可档、预计多久；确认时回包连同可接受的条件存进 knowbase 确认的
供给条件快照。knowfeed 没配、补采开关关着、调不通，清单照常，只是不显示预计。
用户把 knowbase 挑的那份换成另一份，确认后凭本人令牌写回 knowbase 的偏好（0043 第 3.5 节）。

运行授权的 ref 由服务端在请专家时生成（`knowbench:run:<uuid>`，每件委托唯一，记在清单那一行的
`grant_ref`；独立审 10-30 第二轮）：签授权、续签、交还、模型经工作台读数据、写产出都用它，
不用客户端给的幂等钥匙拼。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores
from app.application.workbench.catalog import PinnedPack
from app.domain.workbench.data_plan import Plan, gap, item_ref, plan
from app.domain.workbench.data_request import DataRequest
from app.domain.workbench.errors import DataChoiceInvalid, WorkbenchError
from app.domain.workbench.states import TASK_TERMINAL
from app.domain.workbench.supply_prematch import (
    CONDITIONS,
    MAX_ITEMS,
    request_items,
    supply_snapshot,
)

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
# 查一件等数据的委托先领下这么久（checking_until）：到点查的线与 knowfeed 推送都先抢，
# 抢到的才往下走，免得两边同时接着跑、多签授权；查完只放开自己领的那一次，查到一半进程停了，
# 过了期限别人再领。期限要长过最坏的一次查：knowfeed 换令牌 20 秒 + 查单 30 秒，knowbase 换
# 令牌 20 秒 + 重解析 45 秒 + 续签 45 秒；httpx 的超时按连接、写、读各算一次，最坏约三倍，
# 合计约 8 分钟，取 10 分钟
CLAIM_LEASE = timedelta(minutes=10)
# 面板上问预切合，整体最多等这么久（每次取委托数据都问；问不到清单照常）
PREMATCH_BUDGET_SECONDS = 5.0
# knowbase 对已交还的 ref 拒签新授权时回的码：委托已经结束，当终态处理
REF_RELEASED = "ref_released"


def preference_consumer(row: dict[str, Any]) -> str:
    """偏好按专家记、不按版本记（`expert:<id>`，去掉 `@版本`）：专家升了版，用户上次换过
    的照样用得上（独立审 10-30；约定写在 knowbench-app contracts/README）。"""
    return str(row["consumer"]).split("@", 1)[0]


def new_run_ref() -> str:
    """一件委托的运行授权 ref：服务端生成、每件唯一，只用 `[A-Za-z0-9._:-]`。"""
    return f"knowbench:run:{uuid.uuid4()}"


class KnowbaseData(Protocol):
    async def resolve(self, user_token: str, **kw: Any) -> dict[str, Any]: ...
    async def resolution(
        self, user_token: str, resolution_id: str, revision: int | None = None
    ) -> dict[str, Any]: ...
    async def confirm(self, user_token: str, **kw: Any) -> dict[str, Any]: ...
    async def run_grant(self, user_token: str, **kw: Any) -> dict[str, Any]: ...
    async def register_session(self, user_token: str) -> dict[str, Any]: ...
    async def revise_needs(
        self, user_token: str, resolution_id: str, needs: list[dict[str, Any]]
    ) -> dict[str, Any]: ...
    async def refresh(self, subject: str, resolution_id: str) -> dict[str, Any]: ...
    async def refresh_mine(
        self, user_token: str, resolution_id: str
    ) -> dict[str, Any]: ...
    async def service_resolution(
        self, subject: str, resolution_id: str, revision: int | None = None
    ) -> dict[str, Any]: ...
    async def add_needs(
        self, subject: str, resolution_id: str, needs: list[dict[str, Any]]
    ) -> dict[str, Any]: ...
    async def grant_by_confirmation(self, **kw: Any) -> dict[str, Any]: ...
    async def release_run_grant(self, grant_id: str) -> None: ...
    async def save_preference(
        self,
        user_token: str,
        *,
        consumer: str,
        key: dict[str, Any],
        material_ids: list[str],
    ) -> dict[str, Any] | None: ...


class SupplyDemands(Protocol):
    def configured(self) -> bool: ...
    async def submit(self, **kw: Any) -> dict[str, Any]: ...
    async def get(self, demand_id: str) -> dict[str, Any]: ...
    async def cancel(self, demand_id: str) -> dict[str, Any]: ...
    async def prematch(
        self, items: list[dict[str, Any]], conditions: dict[str, Any]
    ) -> dict[str, Any]: ...


@dataclass
class Prepared:
    """建委托前定好的：要钉的、清单与等数据的记录（task_id 建委托时补）。"""

    pins: list[dict[str, Any]] = field(default_factory=list)
    grant_id: str | None = None
    resolution_row: dict[str, Any] | None = None


def params_of() -> dict[str, Any]:
    """解析用的任务参数只有任务日期（主体写在每一样上，decisions ⑭）。"""
    return {"task_date": datetime.now(UTC).date().isoformat()}


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

    async def prepare(self, pinned: PinnedPack, *, user_token: UserToken) -> Prepared:
        """建委托之前（本人在场）：先建一张空清单，第 0 步提出的数据在上面出新修订；
        作者挑选的数据（冻结的绑定）这时就本人签授权、钉住。运行授权的 ref 这时生成。"""
        ref = new_run_ref()
        token = await user_token()
        params = params_of()
        resolution = await self.knowbase.resolve(
            token, consumer=pinned.consumer, needs=[], params=params, task_ref=ref
        )
        prepared = Prepared(
            resolution_row={
                "resolution_id": str(resolution["resolution_id"]),
                "revision": int(resolution["revision"]),
                "consumer": pinned.consumer,
                "params": params,
                "grant_ref": ref,
                "picks": {},
                "dropped": [],
                "status": "planning",
                "detail": {},
            }
        )
        bound = plan({"items": []}, [], pinned.bindings)
        if bound.pins:
            prepared.grant_id = await self._grant_by_user(token, ref, bound)
            prepared.pins = _with_grant(bound.pins, prepared.grant_id)
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
        picks: dict[str, dict[str, Any]] | None = None,
        dropped: list[str] | None = None,
        needs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """用户确认专家提出的数据（第 0 步或运行中，本人点的就是这一下）：可以先改
        （`needs` 是改过的整份：换主体、改期间、去掉、自己加一样），可以换一份
        （`picks`）、去掉几样（`dropped`）。齐的本人签一条授权、钉住，缺的交 knowfeed
        （委托接着等）；去掉的、补采不了的记「数据不全」，照做。"""
        task_id = str(task["id"])
        row = await repo.get_task_resolution(task_id)
        pending = [r for r in pending_requests(row) if r.get("status") == "pending"]
        if row is None or not pending:
            raise DataChoiceInvalid("nothing is waiting for your confirmation")
        if row.get("resolution_id") is None:
            raise DataChoiceInvalid("confirm the data of this test in knowlab")
        if row["status"] == "waiting":
            raise DataChoiceInvalid("data for this task is still being fetched")
        token = await user_token()
        detail = dict(row.get("detail") or {})
        ids = {r["id"] for r in pending}
        asked = [n for r in pending for n in request_needs(r)]
        if needs is not None:
            asked = _revised(needs, pending[0]["id"])
            earlier = [
                n
                for r in detail.get("requests") or []
                if r.get("id") not in ids
                for n in request_needs(r)
            ]
            latest = await self.knowbase.revise_needs(
                token,
                str(row["resolution_id"]),
                [n.model_dump(mode="json") for n in earlier + asked],
            )
            detail["requests"] = _replace_needs(detail, pending, asked)
        else:
            latest = await self.knowbase.resolution(token, str(row["resolution_id"]))
        draft = plan(
            _scoped(latest, asked),
            asked,
            picks=picks or {},
            dropped=dropped or [],
            supply_enabled=self.supply_enabled,
        )
        if draft.problems:
            raise DataChoiceInvalid("; ".join(draft.problems))
        switched = _switched(_scoped(latest, asked), picks or {}, dropped or [])
        detail["requests"] = [
            {**r, "picks": picks or {}, "dropped": dropped or []}
            if r.get("id") in ids
            else r
            for r in detail.get("requests") or []
        ]
        if draft.confirm:
            # 快照用面板那次问到的（用户看到的就是它），不在确认时再同步问；改过的清单出的
            # 新条目面板没问过，就没有这几项
            confirmed = await self.knowbase.confirm(
                token,
                items=[
                    {"key": c["key"], "need_key": c["need_key"]} for c in draft.confirm
                ],
                task_ref=str(row["grant_ref"]),
                resolution_id=str(row["resolution_id"]),
                revision=int(latest["revision"]),
                supply=supply_snapshot(_shown_prematch(detail, draft.confirm)),
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
                # 齐的先签、先钉：等数据的时候已经能读的照样能读
                pins = await self._sign_ready(repo, task_id, token, row, draft)
                detail["requests"] = _mark(detail, ids, "supplying")
                detail["phase"] = {"requests": sorted(ids)}
                detail["waiting_for"] = [c["label"] for c in draft.confirm]
                detail["incomplete"] = (
                    list(detail.get("incomplete") or []) + draft.incomplete
                )
                detail["gaps"] = list(detail.get("gaps") or []) + draft.gaps
                async with repo.transaction():
                    if pins:
                        await repo.pin_task_data(task_id, pins)
                    await repo.update_task_resolution(
                        task_id,
                        revision=int(latest["revision"]),
                        confirmation_id=str(confirmed["confirmation_id"]),
                        demand_id=str(demand["demand_id"]),
                        deadline=_parse_time(confirmed.get("expires_at")),
                        status="waiting",
                        detail=detail,
                        checks=0,
                        next_check_at=self.now() + CHECK_BACKOFF[0],
                    )
                await self._remember(token, preference_consumer(row), switched)
                return {"status": "supplying", "waiting_for": detail["waiting_for"]}
            latest = await self.knowbase.refresh(
                owner_subject, str(row["resolution_id"])
            )
            draft = plan(
                _scoped(latest, asked),
                asked,
                picks=picks or {},
                dropped=dropped or [],
                after_supply=True,
            )
        pins = await self._sign_ready(repo, task_id, token, row, draft)
        from app.application.workbench.ledger import Ledger

        detail["requests"] = _mark(detail, ids, "done")
        detail["incomplete"] = list(detail.get("incomplete") or []) + draft.incomplete
        detail["gaps"] = list(detail.get("gaps") or []) + draft.gaps
        async with repo.transaction():
            if pins:
                await repo.pin_task_data(task_id, pins)
            await repo.update_task_resolution(
                task_id, revision=int(latest["revision"]), detail=detail
            )
            await Ledger(repo).resume_data(task_id)
        await self._remember(token, preference_consumer(row), switched)
        return {"status": "ready", "added": len(pins)}

    async def decline_requests(
        self, repo: WorkbenchStore, task: dict[str, Any]
    ) -> dict[str, Any]:
        """用户不要专家提出的数据，或者没取到的照现有的接着做：委托接着做，缺的记
        「数据不全」。"""
        from app.application.workbench.ledger import Ledger

        task_id = str(task["id"])
        row = await repo.get_task_resolution(task_id)
        open_ = [
            r
            for r in pending_requests(row)
            if r.get("status") in ("pending", "missing")
        ]
        if row is None or not open_:
            raise DataChoiceInvalid("nothing is waiting for your confirmation")
        detail = dict(row.get("detail") or {})
        detail["requests"] = _mark(detail, {r["id"] for r in open_}, "declined")
        detail["incomplete"] = list(detail.get("incomplete") or []) + [
            item
            for r in open_
            for item in (
                r.get("missing")
                or [f"{label}：你没要" for label in r.get("labels") or []]
            )
        ]
        # 照现有的接着做：缺的每一条带上是不是必需的（必需的缺了才算「数据不全」）
        detail["gaps"] = list(detail.get("gaps") or []) + [
            g
            for r in open_
            for g in (
                (r.get("missing_gaps") or [])
                if r.get("missing")
                else [gap(f"{n.name}：你没要", n.required) for n in request_needs(r)]
            )
        ]
        async with repo.transaction():
            await repo.update_task_resolution(task_id, detail=detail)
            if row["status"] != "waiting":
                await Ledger(repo).resume_data(task_id)
        return {"status": "declined"}

    async def pending_items(
        self, row: dict[str, Any] | None, user_token: UserToken
    ) -> dict[str, list[dict[str, Any]]]:
        """等用户确认的几张请求，每一样解析到哪、候选有哪些（页面上换、改、去掉用）；
        条目带编号（由这里算，页面不照算）。"""
        pending = [r for r in pending_requests(row) if r.get("status") == "pending"]
        if row is None or not pending or row.get("resolution_id") is None:
            return {}
        latest = await self.knowbase.resolution(
            await user_token(), str(row["resolution_id"])
        )
        out: dict[str, list[dict[str, Any]]] = {}
        needs: list[DataRequest] = []
        for r in pending:
            scoped = _scoped(latest, request_needs(r))
            needs += request_needs(r)
            out[str(r["id"])] = [
                {**entry, "ref": item_ref(entry)} for entry in scoped.get("items") or []
            ]
        entries = [e for items in out.values() for e in items]
        try:
            matched = await asyncio.wait_for(
                self._prematch(entries, needs), PREMATCH_BUDGET_SECONDS
            )
        except TimeoutError:
            log.warning("prematch with knowfeed took too long; listing without it")
            matched = None
        answers = {a["ref"]: a for a in (matched or {}).get("items") or []}
        for entry in entries:
            if entry["ref"] in answers:
                entry["prematch"] = answers[entry["ref"]]
        return out

    async def _prematch(
        self, entries: list[dict[str, Any]], needs: list[DataRequest]
    ) -> dict[str, Any] | None:
        """待确认的几项问 knowfeed（0043 第 3.3 节第 1 条）。补采没开（含 knowfeed 没配）
        不问：那时待确认的一律「暂不能补采」，显示预计反倒误导。问不到回 None，不挡。"""
        if not self.supply_enabled or self.supply is None:
            return None
        items = request_items(entries, needs)
        if not items:
            return None
        answers: list[dict[str, Any]] = []
        try:
            for start in range(0, len(items), MAX_ITEMS):
                found = await self.supply.prematch(
                    items[start : start + MAX_ITEMS], dict(CONDITIONS)
                )
                answers += list(found.get("items") or [])
        except Exception:  # noqa: BLE001 — 预切合只是给人看的预计，问不到清单照常
            log.warning("prematch with knowfeed failed; listing without estimates")
            return None
        # 每项带上问的那条描述键（快照里看得出问的是什么）
        asked_keys = {i["ref"]: i.get("key") for i in items}
        answers = [{**a, "key": asked_keys.get(a.get("ref"))} for a in answers]
        return {"contract": "knowfeed-supply-prematch/v1", "items": answers}

    async def recheck(
        self, repo: WorkbenchStore, task: dict[str, Any], user_token: UserToken
    ) -> dict[str, Any]:
        """「传好了，重新查」：用户在 knowbase 上传了采不来的那份，凭本人令牌请 knowbase
        重解析（已定的不动，没定的重找），面板再取就是新的结果。"""
        task_id = str(task["id"])
        row = await repo.get_task_resolution(task_id)
        pending = [r for r in pending_requests(row) if r.get("status") == "pending"]
        if row is None or not pending or row.get("resolution_id") is None:
            raise DataChoiceInvalid("nothing is waiting for your confirmation")
        fresh = await self.knowbase.refresh_mine(
            await user_token(), str(row["resolution_id"])
        )
        async with repo.transaction():
            await repo.update_task_resolution(task_id, revision=int(fresh["revision"]))
        return {"status": "rechecked", "revision": int(fresh["revision"])}

    async def _remember(
        self, token: str, consumer: str, switched: list[tuple[dict[str, Any], str]]
    ) -> None:
        """换过的记成偏好（用户 × 使用者 × 描述 → 资料，0043 第 3.5 节）。只是下次解析的
        排序输入，写不成不挡这次确认。"""
        for key, material_id in switched:
            try:
                await self.knowbase.save_preference(
                    token, consumer=consumer, key=key, material_ids=[material_id]
                )
            except Exception:  # noqa: BLE001 — 偏好写不成记一行
                log.warning("could not record the preference in knowbase")

    async def _sign_ready(
        self,
        repo: WorkbenchStore,
        task_id: str,
        token: str,
        row: dict[str, Any],
        draft: Plan,
    ) -> list[dict[str, Any]]:
        """已经齐的、还没钉的：本人签一条追加的授权。"""
        pins = await new_pins(repo, task_id, draft.pins)
        if not pins:
            return []
        grant_id = await self._grant_by_user(
            token, str(row["grant_ref"]), Plan(pins=pins)
        )
        return _with_grant(pins, grant_id)

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


# —— 专家提出要哪些数据：第 0 步与运行中（0048 第五节 request_data，decisions ⑭）——

ALL_FORMATS = ["pdf", "excel", "csv", "text", "markdown", "html"]
# 一次最多提这么多样（knowbase 一张清单最多 20 样、展开后 200 条）
MAX_REQUEST_NEEDS = 20


def pending_requests(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    """还没办完的请求：等用户确认的、在补采的、没取到等用户定的。"""
    requests = ((row or {}).get("detail") or {}).get("requests") or []
    return [
        r for r in requests if r.get("status") in ("pending", "supplying", "missing")
    ]


def holds_task(row: dict[str, Any] | None) -> bool:
    """委托要不要停在「等数据」：在补采，或专家要的新数据还没确认。"""
    return row is not None and (
        row["status"] == "waiting" or bool(pending_requests(row))
    )


def request_needs(request: dict[str, Any]) -> list[DataRequest]:
    return [DataRequest.model_validate(n) for n in request.get("needs") or []]


async def request_data(
    repo: WorkbenchStore,
    knowbase: KnowbaseData,
    task: dict[str, Any],
    asked: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """专家提出要哪些数据（第 0 步，或运行中发现还要一份没钉的）：清单出新修订、列进
    用户的确认清单，任务在步骤边界停下等用户确认。模型不能替用户确认（0043 第 3.3 节
    第 6 条）。主体每样自己写（可以是别家公司，也可以不限主体）。"""
    task_id = str(task["id"])
    row = await repo.get_task_resolution(task_id)
    if row is None:
        raise DataChoiceInvalid("this task does not use listed data")
    if not isinstance(asked, list) or not 1 <= len(asked) <= MAX_REQUEST_NEEDS:
        raise DataChoiceInvalid(f"ask for 1 to {MAX_REQUEST_NEEDS} items")
    detail = dict(row.get("detail") or {})
    requests = list(detail.get("requests") or [])
    number = len(requests) + 1
    needs = [_asked_need(a, number, n) for n, a in enumerate(asked, start=1)]
    if row.get("resolution_id") is None:
        # 测试委托（SDD 0046 第三节）：不经运行方的账号解析，报给 knowlab 由作者确认
        return await _ask_the_tester(repo, task_id, detail, requests, needs, now)
    subject = await repo.subject_of_actor(str(task["owner_actor_id"]))
    if subject is None:
        raise DataChoiceInvalid("this account is not known to the data service")
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


class SubjectDirectory(Protocol):
    def configured(self) -> bool: ...
    async def securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...


async def find_subject(directory: SubjectDirectory, name: object) -> dict[str, Any]:
    """第 0 步「查主体」：按名字查证券代码（knowfeed 的证券名录），免得模型自己编。"""
    query = str(name or "").strip()
    if not 1 <= len(query) <= 40:
        raise DataChoiceInvalid("give a name or code of 1 to 40 characters")
    if not directory.configured():
        return {
            "found": [],
            "message": "The directory is not available here; ask the user for the code.",
        }
    try:
        found = await directory.securities(query, 10)
    except Exception as exc:  # noqa: BLE001 — 查不到不挡，照实说
        log.warning(
            "subject lookup failed code=%s", getattr(exc, "code", type(exc).__name__)
        )
        return {
            "found": [],
            "message": "The directory did not answer; try again or ask the user.",
        }
    return {
        "found": [
            {"subject": str(f.get("subject")), "name": str(f.get("name"))}
            for f in found
            if isinstance(f, dict)
        ]
    }


async def _ask_the_tester(
    repo: WorkbenchStore,
    task_id: str,
    detail: dict[str, Any],
    requests: list[dict[str, Any]],
    needs: list[DataRequest],
    now: datetime | None,
) -> dict[str, Any]:
    request = {
        "id": f"r{len(requests) + 1}",
        "needs": [n.model_dump(mode="json") for n in needs],
        "labels": [n.name for n in needs],
        "statuses": [],
        "status": "pending",
        "requested_at": (now or datetime.now(UTC)).isoformat(),
    }
    detail["requests"] = requests + [request]
    async with repo.transaction():
        await repo.update_task_resolution(task_id, detail=detail)
    return {
        "status": "listed",
        "request": request["id"],
        "items": [{"name": n.name, "status": "to_confirm"} for n in needs],
        "message": (
            "Listed for the author of this test to confirm; you cannot read it before "
            "that. Finish this step with the data you have and say what is missing."
        ),
    }


def _asked_need(asked: dict[str, Any], number: int, n: int) -> DataRequest:
    if not isinstance(asked, dict):
        raise DataChoiceInvalid("each item is an object")
    period = asked.get("period") or {"kind": "none"}
    reason = str(asked.get("reason") or "").strip()
    try:
        return DataRequest.model_validate(
            {
                "key": f"req{number}_{n}",
                "name": (reason or str(asked.get("doc_type")))[:80],
                **({"subject": asked["subject"]} if asked.get("subject") else {}),
                "doc_type": asked.get("doc_type"),
                "period": period,
                "formats": ALL_FORMATS,
                "required": asked.get("essential") is True,
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


def _revised(given: list[dict[str, Any]], request_id: str) -> list[DataRequest]:
    """用户改过的整份（换主体、改期间、去掉、自己加一样）：自己加的没有 key，这里按
    请求编号补上；一样一个 key。"""
    if not isinstance(given, list) or not 1 <= len(given) <= MAX_REQUEST_NEEDS:
        raise DataChoiceInvalid(f"keep 1 to {MAX_REQUEST_NEEDS} items")
    out: list[DataRequest] = []
    for n, raw in enumerate(given, start=1):
        if not isinstance(raw, dict):
            raise DataChoiceInvalid("each item is an object")
        body = {"formats": ALL_FORMATS, **raw}
        body.setdefault("key", f"{request_id}_u{n}")
        body.setdefault("name", str(body.get("doc_type") or "")[:80])
        try:
            out.append(DataRequest.model_validate(body))
        except ValidationError as exc:
            where = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DataChoiceInvalid(
                "this item is not in the vocabulary: " + ", ".join(where[:5])
            ) from None
    if len({d.key for d in out}) != len(out):
        raise DataChoiceInvalid("each item needs its own key")
    return out


def _replace_needs(
    detail: dict[str, Any], pending: list[dict[str, Any]], needs: list[DataRequest]
) -> list[dict[str, Any]]:
    """改过的整份记到第一张待确认的请求上；同时待确认的别的几张并进来。"""
    first, rest = pending[0]["id"], {r["id"] for r in pending[1:]}
    out = []
    for r in detail.get("requests") or []:
        if r.get("id") == first:
            r = {
                **r,
                "needs": [n.model_dump(mode="json") for n in needs],
                "labels": [n.name for n in needs],
                "revised": True,
            }
        elif r.get("id") in rest:
            r = {**r, "status": "merged"}
        out.append(r)
    return out


def _scoped(resolution: dict[str, Any], needs: list[DataRequest]) -> dict[str, Any]:
    keys = {n.key for n in needs}
    return {
        **resolution,
        "items": [
            i for i in resolution.get("items") or [] if i.get("need_key") in keys
        ],
    }


def _switched(
    resolution: dict[str, Any],
    picks: dict[str, dict[str, Any]],
    dropped: list[str],
) -> list[tuple[dict[str, Any], str]]:
    """用户把 knowbase 挑的那份换成了另一份的（已定的条目才算「换」；有歧义的选一份不算，
    0043 第 3.5 节「已定的换成候选里的另一份……换过的记成偏好」）：（描述键，换成的资料）。
    同一项既换了又去掉的不算（最后没用它）。"""
    out: list[tuple[dict[str, Any], str]] = []
    gone = set(dropped)
    for item in resolution.get("items") or []:
        ref = item_ref(item)
        pick = picks.get(ref)
        if pick is None or ref in gone or item.get("status") != "resolved":
            continue
        chosen = {
            (str(c.get("material_id")), int(c.get("version_no") or 0))
            for c in item.get("chosen") or []
        }
        mine = (str(pick.get("material_id")), int(pick.get("version_no") or 0))
        if mine not in chosen and item.get("key"):
            out.append((dict(item["key"]), mine[0]))
    return out


def _shown_prematch(
    detail: dict[str, Any], confirming: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """面板上那次问到的预切合（`detail.prematch`，取委托数据时记下），只留这次确认的几项。"""
    shown = detail.get("prematch") or {}
    items = [shown[c["ref"]] for c in confirming if c.get("ref") in shown]
    if not items:
        return None
    return {"contract": "knowfeed-supply-prematch/v1", "items": items}


def _mark(detail: dict[str, Any], ids: set[str], status: str) -> list[dict[str, Any]]:
    return [
        {**r, "status": status} if r.get("id") in ids else r
        for r in detail.get("requests") or []
    ]


async def new_pins(
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
        interval_seconds: float = 15.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.stores = stores
        self.knowbase = knowbase
        self.supply = supply
        self.interval_seconds = interval_seconds
        self.now = now

    async def sweep(self) -> int:
        async with self.stores() as repo:
            due = await repo.due_task_resolutions(self.now())
        handled = 0
        for row in due:
            # 领一件查一件：推送那边正领着的、别的进程刚领走的，这里抢不到就让开；
            # 不先把一批都领下（领着没查的那几件会白占着期限）
            async with self.stores() as repo:
                async with repo.transaction():
                    mine = await repo.claim_task_resolution(
                        str(row["task_id"]),
                        now=self.now(),
                        until=self.now() + CLAIM_LEASE,
                    )
            if mine is not None:
                handled += await self.check_now([mine])
        return handled

    async def claim(
        self, demand_id: str, *, owner_subject: str
    ) -> list[dict[str, Any]]:
        """knowfeed 推来这张需求单办完了（契约 knowfeed-supply-status/v1）：把等它的、眼下
        没人在查的几件领下来。领着的时候到点查的线不碰；马上查没办成，过了期限照旧由它兜底。"""
        async with self.stores() as repo:
            async with repo.transaction():
                return await repo.claim_demand_resolutions(
                    demand_id,
                    owner_subject=owner_subject,
                    now=self.now(),
                    until=self.now() + CLAIM_LEASE,
                )

    async def check_now(self, rows: list[dict[str, Any]]) -> int:
        """查这几件（都是领到了的：到点的、或推送领下的）。照旧以按单号查到的为准（推送
        可能重发、晚到）。"""
        handled = 0
        for row in rows:
            try:
                await self._check(row)
                handled += 1
            except Exception as exc:  # noqa: BLE001 — 一件出错不挡别的
                await self._on_error(row, exc)
            finally:
                await self._unclaim(row)
        return handled

    async def _unclaim(self, row: dict[str, Any]) -> None:
        """只放开自己领的那一次（期限值对得上）：过了期限别人已经领走的不碰。"""
        mine = row.get("checking_until")
        if mine is None:
            return
        try:
            async with self.stores() as repo:
                async with repo.transaction():
                    await repo.release_resolution_claim(str(row["task_id"]), mine)
        except Exception:  # noqa: BLE001 — 放不开就等期限过
            log.warning("could not release the claim task=%s", row["task_id"])

    async def _on_error(self, row: dict[str, Any], exc: Exception) -> None:
        status = int(getattr(exc, "status", 0) or 0)
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        if 400 <= status < 500:
            # knowbase、knowfeed 明说不行（确认对不上、清单不是他的……）：不再等
            log.warning("data wait refused task=%s code=%s", row["task_id"], code)
            await self._fail(row, f"数据服务拒绝了：{code}", row.get("detail") or {})
        else:
            log.warning("data wait check failed task=%s code=%s", row["task_id"], code)
            await self._later(row)

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
        """补采办完了：重解析，凭本人那次确认续签、钉住已到的；都到了接着跑，有没到的
        停下问用户（照现有的接着做，还是取消）。"""
        task_id = str(row["task_id"])
        async with self.stores() as repo:
            task = await repo.get_task(task_id)
            subject = await repo.subject_of_actor(str(task["owner_actor_id"]))
        if subject is None:
            await self._fail(row, "找不到委托人的账号", detail)
            return
        fresh = await self.knowbase.refresh(subject, str(row["resolution_id"]))
        ids = set(((row.get("detail") or {}).get("phase") or {}).get("requests") or [])
        asked = [
            r
            for r in (row.get("detail") or {}).get("requests") or []
            if r.get("id") in ids
        ]
        needs = [n for r in asked for n in request_needs(r)]
        picks = {k: v for r in asked for k, v in (r.get("picks") or {}).items()}
        dropped = [d for r in asked for d in r.get("dropped") or []]
        done = plan(
            _scoped(fresh, needs),
            needs,
            picks=picks,
            dropped=dropped,
            after_supply=True,
        )
        async with self.stores() as repo:
            done.pins = await new_pins(repo, task_id, done.pins)
        known = set((row.get("detail") or {}).get("incomplete") or [])
        detail = {
            **detail,
            "requests": [
                {
                    **r,
                    "status": "missing",
                    "missing": done.missing,
                    "missing_gaps": done.missing_gaps,
                }
                if r.get("id") in ids and done.missing
                else ({**r, "status": "done"} if r.get("id") in ids else r)
                for r in (row.get("detail") or {}).get("requests") or []
            ],
            "phase": {},
            "waiting_for": [],
            "incomplete": list((row.get("detail") or {}).get("incomplete") or [])
            + [i for i in done.incomplete if i not in done.missing and i not in known],
            "gaps": list((row.get("detail") or {}).get("gaps") or [])
            + [
                g
                for g in done.gaps
                if g["item"] not in done.missing and g["item"] not in known
            ],
        }
        revision = int(fresh["revision"])
        pins: list[dict[str, Any]] = []
        grant_id = None
        if done.pins:
            try:
                grant = await self.knowbase.grant_by_confirmation(
                    subject=subject,
                    confirmation_id=str(row["confirmation_id"]),
                    resolution_id=str(row["resolution_id"]),
                    revision=revision,
                    ref=str(row["grant_ref"]),
                    items=done.grant_items,
                    ttl_seconds=GRANT_TTL_SECONDS,
                )
            except Exception as exc:
                if getattr(exc, "code", None) != REF_RELEASED:
                    raise
                # knowbase 说这个 ref 已经交还、不再签：委托已经结束的当终态收掉；没结束的
                # （不该交还却交还了）停下问用户，不悄悄取消
                async with self.stores() as repo:
                    state = (await repo.get_task(task_id))["state"]
                if state in TASK_TERMINAL:
                    await self._settle(task_id, "cancelled", row, {})
                else:
                    await self._fail(
                        row, f"数据服务拒绝了：{REF_RELEASED}", row.get("detail") or {}
                    )
                return
            grant_id = str(grant["grant_id"])
            pins = _with_grant(done.pins, grant_id)
        from app.application.workbench.ledger import Ledger

        kept = False
        async with self.stores() as repo:
            async with repo.transaction():
                current = await repo.get_task_resolution(task_id, for_update=True)
                ended = (await repo.get_task(task_id))["state"] in TASK_TERMINAL
                if current is None or current["status"] != "waiting" or ended:
                    # 别处已经办过（重复、乱序的检查不重复开跑），或委托在续签的时候结束了
                    # （交还那条线可能已经按 ref 交还过）：刚签的那条不钉、马上交还
                    if ended and current is not None and current["status"] == "waiting":
                        await repo.update_task_resolution(
                            task_id, status="cancelled", next_check_at=None
                        )
                else:
                    kept = True
                if kept:
                    if pins:
                        await repo.pin_task_data(task_id, pins)
                    await repo.update_task_resolution(
                        task_id,
                        status="ready",
                        revision=revision,
                        detail=detail,
                        next_check_at=None,
                    )
                    if not done.missing:
                        await Ledger(repo).resume_data(task_id)
        if not kept and grant_id is not None:
            try:
                await self.knowbase.release_run_grant(grant_id)
            except Exception:  # noqa: BLE001 — 交还不成，到期也自己失效
                log.warning("could not hand back an unused grant task=%s", task_id)

    async def _fail(self, row: dict[str, Any], reason: str, detail: dict[str, Any]):
        """取不来了（确认过了 7 天、数据服务明说不行）：停下问用户，照现有的接着做
        （记「数据不全」）还是取消。"""
        task_id = str(row["task_id"])
        ids = set(((row.get("detail") or {}).get("phase") or {}).get("requests") or [])
        async with self.stores() as repo:
            async with repo.transaction():
                current = await repo.get_task_resolution(task_id, for_update=True)
                if current is None or current["status"] != "waiting":
                    return
                await repo.update_task_resolution(
                    task_id,
                    status="ready",
                    detail={
                        **detail,
                        "requests": [
                            {
                                **r,
                                "status": "missing",
                                "missing": [
                                    f"{label}：{reason}"
                                    for label in r.get("labels") or []
                                ],
                                "missing_gaps": [
                                    gap(f"{n.name}：{reason}", n.required)
                                    for n in request_needs(r)
                                ],
                            }
                            if r.get("id") in ids
                            else r
                            for r in (current.get("detail") or {}).get("requests") or []
                        ],
                        "phase": {},
                        "waiting_for": [],
                        "reason": reason,
                    },
                    next_check_at=None,
                )

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
