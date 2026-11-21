"""Management and author selection against the same provider-owned records."""

import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_auth_routes_security import FakeAuthService
from test_auth_routes_security import session as browser_session
from test_service_function_publication_db import CONTRACT
from test_source_licenses import license_record
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_methods import FormalMethods
from app.bootstrap.api import create_app
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_admission_generated import FormalMethodSource
from app.domain.workbench.service_functions import FunctionRevision, ServiceFunction
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.infrastructure.workbench.service_functions import register_revision
from app.interfaces.http.middleware import auth
from core.config import Settings

BASE = "/api/admin/v1/method-skill-center/functions"


def revision(provider="knowbase", version=1, **changes):
    return FunctionRevision.build(
        provider,
        "query_facts",
        version,
        ServiceFunction.model_validate(CONTRACT | changes),
    )


def pin(r):
    return {
        "kind": "service_function",
        "source": r.provider,
        "entry": r.name,
        "version": str(r.version),
        "digest": r.digest,
    }


async def test_author_requires_an_incubation_registration_and_exact_current_license(db):
    owner = uuid4()
    first = revision()
    other = revision("knowfeed")
    native = revision(version=2, scenes=["native"])
    granted = [
        license_record(pin(r), record_id=f"test-{i}")
        for i, r in enumerate((first, other, native))
    ]
    async with db() as s:
        repo = WorkbenchRepository(s)
        for r in (first, native):
            await register_revision(s, r)
        async with s.begin():
            for actor, subject in ((owner, "author"), (uuid4(), "other")):
                await s.execute(
                    text(
                        "INSERT INTO auth_user(id,issuer,subject,roles,scopes) VALUES (:id,'test',:subject,'[]','[]')"
                    ),
                    dict(id=actor, subject=subject),
                )
        methods = FormalMethods(repo, source_licenses=granted)
        assert (await methods.sources("other"))["items"] == []
        [source] = (await methods.sources("author"))["items"]
        assert pin(first).items() <= source.items()
        parsed = FormalMethodSource.model_validate(source)
        async with repo.transaction():
            assert await methods._source(str(owner), "author", parsed)
        revoked = FormalMethods(
            repo, source_licenses=[granted[0].model_copy(update={"status": "revoked"})]
        )
        assert (await revoked.sources("author"))["items"] == []
        async with repo.transaction():
            with pytest.raises(CapabilityError):
                await revoked._source(str(owner), "author", parsed)


async def test_management_finds_exact_pins_without_exposing_private_method_body(
    db, monkeypatch
):
    app = create_app(Settings())

    async def database():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = database
    allowed = browser_session("admin", "knowbench:admin")
    monkeypatch.setattr(
        auth, "admin_auth_service", FakeAuthService({"manage": allowed})
    )
    a, b, c = revision(), revision(version=2), revision("knowfeed")
    run, owner = uuid4(), uuid4()
    async with db() as s:
        for r in (a, b, c):
            await register_revision(s, r)
        async with s.begin():
            await s.execute(
                text("""INSERT INTO workbench_formal_validations
                (run_id,owner_actor_id,caller,request_id,request_digest,request,result)
                VALUES (:run,:owner,'knowlab',:request,'test','{}','{}')"""),
                dict(run=run, owner=owner, request=uuid4()),
            )
            # Synthetic frozen projections exercise lookup independently of the future trial runner.
            for title, src in (
                ("exact", pin(a)),
                ("later", pin(b)),
                ("other", pin(c)),
                ("different digest", pin(a) | {"digest": b.digest}),
                ("connection", pin(a) | {"kind": "mcp"}),
            ):
                doc = {
                    "draft": {
                        "source": src,
                        "definition": {
                            "capability_declaration": {"title": title},
                            "private": "sensitive draft",
                        },
                    }
                }
                await s.execute(
                    text("""INSERT INTO workbench_formal_methods(method_id,version,owner_actor_id,caller,digest,document,run_id)
                        VALUES(:id,1,:owner,'knowlab',:digest,CAST(:doc AS jsonb),:run)"""),
                    dict(
                        id=uuid4(),
                        owner=owner,
                        digest=a.digest,
                        doc=json.dumps(doc),
                        run=run,
                    ),
                )
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        assert (await http.get(BASE)).status_code == 401
        http.cookies.set("sunmoonai_knowbench_web_sid", "manage")
        assert (await http.get(BASE)).status_code == 401
        http.cookies.set("sunmoonai_knowbench_admin_sid", "manage")
        first = await http.get(BASE, params={"q": "knowbase/query", "limit": 1})
        assert first.status_code == 200, first.text
        assert first.json()["items"][0]["version"] == 2 and first.json()["has_more"]
        second = await http.get(
            BASE, params={"q": "knowbase/query", "limit": 1, "offset": 1}
        )
        assert (
            second.json()["items"][0]["digest"] == a.digest
            and not second.json()["has_more"]
        )
        assert len((await http.get(BASE, params={"q": c.digest})).json()["items"]) == 1
        res = await http.get(BASE + "/knowbase/query_facts/1")
        assert res.json()["revision"] == a.model_dump()
        assert [m["title"] for m in res.json()["references"]["items"]] == ["exact"]
        assert (
            "sensitive draft" not in res.text
            and "no-store" in res.headers["cache-control"]
        )
        assert (await http.get(BASE + "/knowbase/query_facts/33")).status_code == 404
