"""A separate, version-pinned permission to inspect the presence of one column."""

from uuid import UUID

from pydantic import Field

from app.application.workbench.external_claim import ExternalClaim
from app.application.workbench.temporary_planning import ROUND, current, record
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_method_generated import Digest
from app.domain.workbench.match_requirements import digest
from app.domain.workbench.ordinary_expert import MaterialPin
from app.domain.workbench.temporary_plan import read_choice

PERMISSION = "task/temporary_probe_permission"
CLAIM = "task/temporary_probe_claim"
OBSERVED = "task/temporary_probe_result"


class Probe(MaterialPin):
    review_digest: Digest
    planning_task: UUID
    sheet: str = Field(default="", max_length=120)
    column: str = Field(min_length=1, max_length=120)


async def run(service, owner, task_id, command, library, token):
    repo = service.repo
    if not service.planning_enabled(await repo.subject_of_actor(owner)):
        raise NotFound("Planning probe is unavailable")
    body = command.model_dump(mode="json")
    consent = digest([task_id, body])
    claim = ExternalClaim(CLAIM)
    # A concurrent replay waits for the same receipt. A lost worker leaves a
    # bounded lease; only this consent can ever reclaim it.
    while True:
        async with repo.transaction():
            task, _ = await current(
                repo, owner, task_id, command.review_digest, lock=True
            )
            active = await repo.latest_task_payload(task_id, ROUND)
            if not active or active["planning_task"] != str(command.planning_task):
                raise StaleStateVersion("This is not the active planning round")
            child = await repo.get_task(active["planning_task"], owner_actor_id=owner)
            artifact = await repo.get_artifact(
                task_id=active["planning_task"], name="find_expert"
            )
            if (
                child["state"] != "SUCCEEDED"
                or not artifact
                or read_choice(child, artifact["content"]).outcome != "clarify"
            ):
                raise StaleStateVersion(
                    "Only a completed clarification may inspect an authorized header"
                )
            prior = await repo.latest_task_payload(task_id, PERMISSION)
            found = await repo.latest_task_payload(task_id, OBSERVED)
            if found and found["consent"] == consent:
                return found
            if prior and prior["consent"] != consent:
                raise StaleStateVersion(
                    "The probe budget for this request has been used"
                )
            if not prior:
                await record(repo, task, PERMISSION, {"consent": consent, **body})
            if await claim.acquire(repo, task, consent):
                break
        await claim.wait()
    try:
        result = await library.check_planning_column(await token(), body)
        expected = {
            k: body[k]
            for k in ("material_id", "version_no", "sha256", "sheet", "column")
        }
        if (
            not isinstance(result, dict)
            or result.get("status") not in ("present", "missing", "unknown")
            or ({k: v for k, v in result.items() if k != "status"} != expected)
        ):
            raise StaleStateVersion(
                "KnowBase did not verify the approved material version"
            )
        receipt = {
            "consent": consent,
            "result": result,
            "planning_task": active["planning_task"],
        }
    except Exception:
        await claim.failed(repo, owner, task_id, consent)
        raise
    async with repo.transaction():
        task, _ = await current(repo, owner, task_id, command.review_digest, lock=True)
        if await repo.latest_task_payload(task_id, ROUND) != active:
            raise StaleStateVersion("Planning round changed during the probe")
        existing = await repo.latest_task_payload(task_id, OBSERVED)
        if existing:
            if existing["consent"] != consent:
                raise StaleStateVersion("Probe permission changed")
            return existing
        if not await claim.owns(repo, task_id):
            raise StaleStateVersion("Another attempt reclaimed this probe")
        await record(repo, task, OBSERVED, receipt)
        return receipt
