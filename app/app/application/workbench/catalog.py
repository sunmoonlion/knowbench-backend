"""专家从哪来（SDD 0027 P4 的分身目录）：内置专家包，或用户从 knowmart 请来的分身。

内置的按 id 与版本在代码里找。分身先钉住（workbench_knowbot_pins，只插不改），建委托时把
这一版的专家包快照与清单摘要写进委托；之后编排、底稿、验收只读委托上的快照，所以 knowmart
停了、分身停用了，在跑的委托都不受影响（0027 §3.3、§四）。分身只归请它的人：钉表按所有者查。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.application.ports.workbench import KnowmartSummoner, WorkbenchStore
from app.domain.workbench import knowbots
from app.domain.workbench.data_needs import BadNeeds, DataNeed, needs_of
from app.domain.workbench.errors import ExpertNotReady, KnowbotInvalid, NoSuchExpert
from app.domain.workbench.packs import ExpertPack, find_pack

SOURCE_BUILTIN = "builtin"
SOURCE_KNOWMART = "knowmart"
# 测试中的草稿：快照同分身，只在这次测试的委托上（SDD 0027 §3.4）
SOURCE_TEST = "knowlab-test"
SNAPSHOT_SOURCES = frozenset({SOURCE_KNOWMART, SOURCE_TEST})

_snapshots: dict[tuple[str, str], ExpertPack] = {}


@dataclass(frozen=True)
class PinnedPack:
    pack: ExpertPack
    digest: str
    source: str = SOURCE_KNOWMART
    # 数据需求声明（SDD 0042）：有的话请它要先选数据；旧格式、测试中的草稿是空的
    needs: tuple[DataNeed, ...] = ()

    def task_columns(self) -> dict[str, Any]:
        """建委托时写进委托的三列。"""
        return {
            "pack_source": self.source,
            "pack_digest": self.digest,
            "pack_snapshot": self.pack.model_dump(mode="json"),
        }


def pack_for_task(task: dict[str, Any]) -> ExpertPack | None:
    """这件委托用的专家包。分身读委托上的快照（同一份快照只解析一次）。"""
    if task.get("pack_source") in SNAPSHOT_SOURCES:
        snapshot, digest = task.get("pack_snapshot"), task.get("pack_digest")
        if not isinstance(snapshot, dict) or not digest:
            return None
        key = (str(task.get("profile_id") or ""), str(digest))
        if key not in _snapshots:
            if len(_snapshots) > 512:
                _snapshots.clear()
            _snapshots[key] = ExpertPack.model_validate(snapshot)
        return _snapshots[key]
    return find_pack(str(task.get("profile_id") or ""), task.get("profile_version"))


def expert_name(row: dict[str, Any]) -> str:
    """给用户看的专家名字。row 可以是整条委托，也可以是带 pack_source、pack_name 的列表行。"""
    if row.get("pack_source") in SNAPSHOT_SOURCES:
        if row.get("pack_name"):
            return str(row["pack_name"])
        pack = pack_for_task(row)
        return (pack.name or pack.title) if pack else ""
    pack = find_pack(str(row.get("profile_id") or ""), row.get("profile_version"))
    return (pack.name or pack.title) if pack else ""


def pack_from_pin(pin: dict[str, Any]) -> PinnedPack:
    knowbot_id, version = str(pin["knowbot_id"]), str(pin["version"])
    pack = knowbots.pack_from_manifest(
        pin["manifest"], knowbot_id=knowbot_id, version=version
    )
    try:
        needs = tuple(needs_of(pin["manifest"]))
    except BadNeeds as exc:
        raise KnowbotInvalid(str(exc), knowbot=knowbot_id) from None
    return PinnedPack(pack=pack, digest=str(pin["digest"]), needs=needs)


async def pinned_pack(
    repo: WorkbenchStore, *, owner_actor_id: str, profile_id: str
) -> PinnedPack:
    """这个人钉住的这一版分身。别人的、没钉过的都是「没有这位专家」。"""
    ref = knowbots.parse_profile(profile_id)
    if ref is None:
        raise NoSuchExpert("no such expert", profile_id=profile_id)
    pin = await repo.get_knowbot_pin(
        owner_actor_id=owner_actor_id, knowbot_id=ref[0], version=ref[1]
    )
    if pin is None:
        raise NoSuchExpert("no such expert", profile_id=profile_id)
    return pack_from_pin(pin)


def builtin_on_view(profile_id: str) -> ExpertPack:
    """能看的内置专家：清单里的都能看，包括筹备中的。"""
    pack = find_pack(profile_id)
    if pack is None or pack.internal:
        raise NoSuchExpert("no such expert", profile_id=profile_id)
    return pack


def builtin_on_offer(profile_id: str) -> ExpertPack:
    """能请的内置专家：筹备中的看得见、请不了（所有者 2026-10-13 定先占位）。"""
    return _ready(builtin_on_view(profile_id), profile_id)


def _ready(pack: ExpertPack, profile_id: str) -> ExpertPack:
    if pack.status != "ready":
        raise ExpertNotReady(
            "this expert is still being prepared", profile_id=profile_id
        )
    return pack


async def viewable_expert(
    repo: WorkbenchStore, *, owner_actor_id: str, profile_id: str
) -> ExpertPack:
    """能看的专家：内置的，或这个人钉住的分身。"""
    if knowbots.is_knowbot(profile_id):
        return (
            await pinned_pack(
                repo, owner_actor_id=owner_actor_id, profile_id=profile_id
            )
        ).pack
    return builtin_on_view(profile_id)


async def offerable_expert(
    repo: WorkbenchStore, *, owner_actor_id: str, profile_id: str
) -> ExpertPack:
    """能请的专家：内置的（筹备中的请不了），或这个人钉住的分身。"""
    pack = await viewable_expert(
        repo, owner_actor_id=owner_actor_id, profile_id=profile_id
    )
    return _ready(pack, profile_id)


async def my_knowbots(repo: WorkbenchStore, *, owner_actor_id: str) -> list[ExpertPack]:
    """这个人请过的分身。钉住后工作台的校验变严了的那一版不再列出（也请不了）。"""
    out = []
    for pin in await repo.list_knowbot_pins(owner_actor_id=owner_actor_id):
        try:
            out.append(pack_from_pin(pin).pack)
        except KnowbotInvalid:
            continue
    return out


async def pin_knowbot(
    repo: WorkbenchStore,
    summoner: KnowmartSummoner,
    *,
    owner_actor_id: str,
    owner_subject: str,
    ref: str,
) -> ExpertPack:
    """用户从 knowmart 选定回来（`knowbot=<分身>@<版本>`）：按他的会话向 knowmart 核一次，
    摘要对得上、工作台跑得了，才钉住。伪造的、别人的都是「没有这位专家」。"""
    parsed = knowbots.parse_ref(ref)
    if parsed is None:
        raise NoSuchExpert("no such knowbot", knowbot=ref)
    knowbot_id, version = parsed
    got = await summoner.summon(
        owner_subject=owner_subject, knowbot_id=knowbot_id, version=version
    )
    manifest, digest = got.get("manifest"), got.get("digest")
    if (
        not isinstance(manifest, dict)
        or not isinstance(digest, str)
        or digest != knowbots.manifest_digest(manifest)
    ):
        raise KnowbotInvalid(
            "the knowbot digest does not match its manifest", knowbot=knowbot_id
        )
    pack = knowbots.pack_from_manifest(manifest, knowbot_id=knowbot_id, version=version)
    async with repo.transaction():
        await repo.pin_knowbot(
            owner_actor_id=owner_actor_id,
            owner_subject=owner_subject,
            knowbot_id=knowbot_id,
            version=version,
            digest=digest,
            manifest=manifest,
        )
    return pack
