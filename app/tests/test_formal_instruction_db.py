"""Licensed instruction preparation and an opt-in real, network-none Codex rehearsal."""

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from test_capabilities_db import OWNER, service
from test_formal_method_admission_db import setup
from test_formal_method_routes import validation_body
from test_source_licenses import license_record
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_methods import FormalMethods
from app.domain.workbench.capabilities import AssetAction, CapabilityError, SkillCreate
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_adapter import host_kind, retained_registration
from app.domain.workbench.formal_execution import validate_values
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodValidationRequest,
)
from app.domain.workbench.instruction_requests import instruction_job
from app.domain.workbench.pricing import Price, cost_of, usage_of
from app.infrastructure.workbench.repository import WorkbenchRepository

PROBES = Path(__file__).resolve().parents[1] / "scripts/probes"
ENTRY = "Return the supplied status in the summary field."
ARGS = {"status": "ready"}


def test_instruction_cannot_reuse_an_mcp_dispatch_or_its_evidence():
    req = FormalMethodValidationRequest.model_validate(validation_body())
    with pytest.raises(CapabilityError, match="No formal host adapter"):
        host_kind(req.draft.source)
    with pytest.raises(CapabilityError, match="No formal host adapter"):
        retained_registration(req.draft, {"name": "SKILL.md", "inputSchema": {}})


async def install(db):
    await setup(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        asset = await service(repo).create_skill(
            OWNER,
            SkillCreate(
                request_id="instruction-rehearsal",
                name="Summary",
                description="Synthetic test",
                version="1",
                content=ENTRY,
            ),
        )
        source = next(
            s
            for s in (await FormalMethods(repo).sources("author"))["items"]
            if s["kind"] == "skill_instruction"
        )
        license = license_record(source)
        svc = FormalMethods(repo, source_licenses=(license,))
        source = next(
            s
            for s in (await svc.sources("author"))["items"]
            if s["kind"] == "skill_instruction"
        )
        doc = validation_body()
        doc["draft"]["source"] = source
        step = doc["draft"]["definition"]["procedure"]
        step["implementation"].update(
            {k: source[k] for k in ("kind", "version", "source", "digest", "entry")}
        )
        step["inputs"] = {
            "status": {**step["outputs"]["summary"], "description": "Input status"}
        }
        req = FormalMethodValidationRequest.model_validate(doc)
        pending = await svc.validate(req, caller="knowlab")
        assert pending["status"] == "pending" and pending["receipt"] is None
        return license, req, asset


async def test_instruction_preparation_rechecks_stored_run_owner_rights_and_entry(db):
    license, req, asset = await install(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = FormalMethods(repo, source_licenses=(license,))
        prepare = svc.prepare_instruction_trial
        assert await prepare("author", req.run_id, ARGS, caller="knowlab") == (
            req,
            ENTRY,
        )
        for subject, caller in [("other", "knowlab"), ("author", "unrelated")]:
            with pytest.raises(NotFound):
                await prepare(subject, req.run_id, ARGS, caller=caller)
        for args in ({}, {"status": False}, {**ARGS, "extra": 0}):
            with pytest.raises(ValueError):
                await prepare("author", req.run_id, args, caller="knowlab")
        with pytest.raises(CapabilityError):
            await FormalMethods(repo).prepare_instruction_trial(
                "author", req.run_id, ARGS, caller="knowlab"
            )
        doc = req.model_dump(mode="json", by_alias=True)
        doc["run_id"] = str(uuid4())
        doc["draft"]["definition"]["procedure"]["max_model_turns"] = 2
        two = FormalMethodValidationRequest.model_validate(doc)
        await svc.validate(two, caller="knowlab")
        with pytest.raises(CapabilityError, match="single-turn"):
            await prepare("author", two.run_id, ARGS, caller="knowlab")
        await service(repo).action(
            OWNER,
            asset["id"],
            AssetAction(action="disable", revision=asset["revision"]),
        )
        with pytest.raises(CapabilityError, match="Installed source changed"):
            await prepare("author", req.run_id, ARGS, caller="knowlab")


@pytest.mark.skipif(
    os.getenv("RUN_CODEX_INSTRUCTION_PROBE") != "1",
    reason="requires pinned local Codex image",
)
async def test_real_single_turn_instruction_on_isolated_codex(db):
    license, req, _ = await install(db)
    async with db() as session:
        svc = FormalMethods(WorkbenchRepository(session), source_licenses=(license,))
        retained, instructions = await svc.prepare_instruction_trial(
            "author", req.run_id, ARGS, caller="knowlab"
        )
    step = retained.draft.definition.procedure
    job = instruction_job(retained, instructions, ARGS)
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:exec",
        "-e",
        "HOME=/tmp",
        "-e",
        "CODEX_HOME=/tmp/probe-home",
        "-v",
        f"{PROBES}:/probe:ro",
        "--entrypoint",
        "python3",
        "luna-local/sandbox:codex-0.170.0",
        "/probe/codex_instruction.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(json.dumps(job).encode()), 90
    )
    assert proc.returncode == 0, stderr.decode()
    runs = {r["case"]: r for r in json.loads(stdout)}
    valid = runs["valid"]
    assert valid["requests"] == 1 and valid["refused"] == 0 and not valid["denied"]
    assert validate_values(step.outputs, json.loads(valid["final"])) == {
        "summary": "ready"
    }
    with pytest.raises(ValueError, match="schema"):
        validate_values(step.outputs, json.loads(runs["invalid_output"]["final"]))
    multiple, stopped = runs["question"], runs["question_bounded"]
    assert (
        multiple["requests"] == 2
        and multiple["items"] == valid["items"]
        and not multiple["denied"]
    )
    assert stopped["requests"] == stopped["refused"] == 1
    assert stopped["status"] == "failed" and stopped["final"] is None
    price = Price(
        Decimal("2"), Decimal("1"), Decimal("2"), Decimal("4"), "Synthetic test rate"
    )
    for r in runs.values():
        assert r["tools"] == ["request_user_input"]
        assert r["usage"]["totalTokens"] == 2 * r["requests"]
        assert (
            cost_of(usage_of(r["usage"]), price) == Decimal("0.000006") * r["requests"]
        )
    async with db() as session:
        result = await FormalMethods(WorkbenchRepository(session)).validation(
            "author", str(req.run_id), caller="knowlab"
        )
        assert result["status"] == "pending" and result["receipt"] is None
