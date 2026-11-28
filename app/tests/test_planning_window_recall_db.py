"""Catalog-window experiment with the C01–C15 finance designs and a sparse grant set.

Rows exercise the registration search projection only. They are not certified
finance methods or production traffic. The actor's grants stay identical while
the searchable catalog grows and the query and expected targets stay fixed.
"""

from types import SimpleNamespace
from uuid import UUID

from formal_plan_support import setup_plan
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_expert_intake import directory_for
from app.application.workbench.registry_catalog import SceneRegistry
from app.domain.workbench.formal_expert_match import frozen_scope
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.method_search import PlanningQuery, search_terms
from app.infrastructure.workbench.repository import WorkbenchRepository

# Expected targets fixed before evaluating the production ranking query.
CASES = (
    ("报表四式与口径核对", "报表勾稽与报表期间检查", "核对这份年报的报表勾稽关系"),
    ("历史增长与盈利", "增长率和利润率的历史变化", "今年利润率和增长率分别变动了多少"),
    ("现金与利润差异", "从经营现金流解释利润差额", "经营现金流为什么跟净利润差这么多"),
    ("流动性与营运周期", "短债覆盖与现金周转天数", "算一下现金周转天数，短债能覆盖吗"),
    ("业务结构与集中度", "分部收入与大客户集中度", "大客户集中度上升了吗"),
    ("资本投入与研发", "研发投入强度与资本化率", "研发投入有多少被资本化了"),
    ("原季节性预测与回测", "历史季度滚动预测与回测", "用历史季度数据回测预测误差"),
    ("三表情景预测", "情景假设和三张报表联动", "收入下降时三张报表联动会怎样"),
    ("DCF 情景估值", "现金流折现和终值敏感性", "用现金流折现算一组终值敏感性"),
    ("相对估值观察与情景", "同行倍数与相对估值", "这家公司的估值和同行倍数相差多少"),
    ("股东及关联事项", "关联交易与股东持股变动", "查一下关联交易和股东持股变动"),
    ("商誉压力情景", "商誉减值对利润的压力", "商誉减值对今年利润有多大影响"),
    ("审计与或有事项", "审计意见与或有负债", "整理审计意见里提到的或有负债"),
    ("资本回报与竞争证据", "资本回报变化与竞争优势", "资本回报是否支持它有竞争优势"),
    ("本题报告成稿", "按交付要求汇总报告与证据", "把各项分析和证据汇总成报告"),
)


async def test_authorized_top_eight_before_and_after_catalog_growth(db):
    ctx = await setup_plan(db, accept=False)
    authorized = set()

    class Market:
        async def planning_window(self, subject, offset, limit):
            assert not session.in_transaction() and limit <= 100
            refs = sorted(authorized, key=lambda ref: ref.method_id)
            return refs[offset : offset + limit], len(refs) > offset + limit

        async def planning_formal_refs(self, subject, refs):
            assert not session.in_transaction()
            return {MethodRef.model_validate(ref) for ref in refs} & authorized

    async with db() as session:
        repo = WorkbenchRepository(session)
        registry = SceneRegistry(SimpleNamespace(market=Market()), repo)

        async def insert(index, title, purpose, *, draft=ctx.request.draft):
            definition = draft.definition.model_dump(mode="json", by_alias=True)
            definition["capability_declaration"].update(title=title, purpose=purpose)
            definition = type(ctx.request.draft.definition).model_validate(definition)
            ref = MethodRef(
                method_id=UUID(int=index), version=1, digest=definition.digest()
            )
            await repo.insert_formal_method(
                {
                    **ref.model_dump(),
                    "owner_actor_id": OWNER,
                    "caller": "knowlab",
                    "run_id": ctx.leaf.validation.run_id,
                    "document": {
                        "reference": ref.model_dump(mode="json"),
                        "visibility": "public",
                        "draft": {
                            "allowed_callers": ["host"],
                            "definition": definition.model_dump(
                                mode="json", by_alias=True
                            ),
                        },
                    },
                }
            )
            return ref

        async with repo.transaction():
            for i, (title, purpose, _) in enumerate(CASES, 1):
                authorized.add(await insert(i, title, purpose))

        async def measure():
            first_hits = baseline_hits = 0
            max_pages = 0
            for i, (_, _, question) in enumerate(CASES, 1):
                query = PlanningQuery(stage="expert", question=question)
                async with repo.transaction():
                    baseline = await repo.registry_method_candidates(
                        OWNER,
                        8,
                        0,
                        public=True,
                        experts_only=True,
                        terms=search_terms(question),
                        eligible=[ref.model_dump(mode="json") for ref in authorized],
                    )
                target = UUID(int=i)
                baseline_hits += any(row["method_id"] == target for row in baseline)
                offset = pages = 0
                while True:
                    page = await registry.planning_versions(
                        OWNER, 8, offset, query=query
                    )
                    pages += 1
                    assert all(
                        MethodRef.model_validate(registry.reference(row)) in authorized
                        for row in page["items"]
                    )
                    if any(row["method_id"] == target for row in page["items"]):
                        first_hits += pages == 1
                        break
                    assert page["next"] is not None and page["next"] > offset
                    offset = page["next"]
                max_pages = max(pages, max_pages)
            return first_hits, baseline_hits, max_pages

        assert await measure() == (15, 15, 1)
        # 105 newer public entries visible to other buyers only.
        async with repo.transaction():
            for i in range(16, 121):
                await insert(i, "单证归档", "整理归档发票的原始单证")
        assert await measure() == (15, 15, 1)

        # Include every public distractor in the buyer's rights. A strong
        # match past the first authorization window must precede weaker experts.
        async with repo.transaction():
            for i in range(520, 620):
                authorized.add(await insert(i, "通用专家", "常规说明"))
        # Move the unique target beyond a full window in market ordering.
        async with repo.transaction():
            target = await insert(2000, "专项验收报表", "专项验收报表")
        authorized.add(target)
        page = await registry.planning_versions(
            OWNER, 8, 0, query=PlanningQuery(stage="expert", question="专项验收报表")
        )
        assert page["items"][0]["method_id"] == target.method_id

        # 96 qualified tool versions fill the first window of an 8-candidate query.
        authorized.clear()
        async with repo.transaction():
            for i in range(121, 217):
                authorized.add(
                    await insert(i, "归档工具", "整理单据", draft=ctx.leaf.draft)
                )
            expert = await insert(217, "归档专家", "整理单据并核对")
            authorized.add(expert)
        first = await directory_for(registry, OWNER, "整理单据", None)
        assert first["more"] is False
        assert [item["reference"] for item in first["items"]] == [
            expert.model_dump(mode="json")
        ]
        definition = await registry.planning_definition(
            OWNER, expert, experts_only=True
        )
        preview = frozen_scope(definition)
        assert definition.digest() == expert.digest
        assert [s["id"] for s in preview["steps"]] == ["first", "second"]
        assert preview["steps"][1]["depends_on"] == ("first",)
        assert preview["outputs"][0]["step"] == "second"
        authorized.remove(expert)
        assert await directory_for(registry, OWNER, "整理单据", None, offset=96) == {
            "items": [],
            "next": None,
        }


async def test_late_tool_in_ten_thousand_and_unauthorized_exact_title(db):
    """Real index and both scene queries, with exact synthetic grants."""
    import json

    from sqlalchemy import text

    ctx = await setup_plan(db, accept=False)
    count = 10000
    async with db() as session:
        repo = WorkbenchRepository(session)

        def seed_document(title, purpose):
            base = ctx.leaf.draft.definition
            definition = base.model_copy(
                update={
                    "capability_declaration": base.capability_declaration.model_copy(
                        update={"title": title, "purpose": purpose}
                    )
                }
            )
            digest = definition.digest()
            return {
                "visibility": "public",
                "reference": {"version": 1, "digest": digest},
                "draft": {
                    "allowed_callers": ["host"],
                    "definition": definition.document(),
                },
            }

        ordinary = seed_document("整理出货单", "保留仓库货运记录")
        target = seed_document(
            "期初余额连续性核验", "比较期初余额与上期末余额并返回差额"
        )
        private = seed_document(
            "期初期末余额连续性核验", "期初余额与上期末余额比较差额"
        )

        async def insert(doc, low, high, owner):
            await session.execute(
                text("""
                INSERT INTO workbench_formal_methods(method_id, version, digest, owner_actor_id, caller, document, run_id)
                SELECT id, 1, :digest, :owner, 'knowlab',
                       jsonb_set(CAST(:doc AS jsonb), '{reference,method_id}', to_jsonb(id::text)), :run
                FROM (SELECT lpad(to_hex(n),32,'0')::uuid AS id FROM generate_series(CAST(:low AS int),CAST(:high AS int)) n) ids
            """),
                {
                    "digest": doc["reference"]["digest"],
                    "owner": owner,
                    "doc": json.dumps(doc),
                    "run": ctx.leaf.validation.run_id,
                    "low": low,
                    "high": high,
                },
            )

        async with repo.transaction():
            await insert(ordinary, 1, count - 1, OWNER)
            await insert(target, count, count, OWNER)
            await insert(private, count + 1, count + 1, str(UUID(int=99999)))
            await session.execute(text("ANALYZE workbench_formal_methods"))

        class Market:
            windows = 0
            removed = None

            async def planning_window(self, subject, offset, limit):
                self.windows += 1
                return [
                    MethodRef(
                        method_id=UUID(int=n),
                        version=1,
                        digest=(target if n == count else ordinary)["reference"][
                            "digest"
                        ],
                    )
                    for n in range(1 + offset, 1 + min(count, offset + limit))
                    if n != self.removed
                ], offset + limit < count

            async def directory_labels(self, subject, refs):
                from app.domain.workbench.method_search import DirectoryLabels

                assert not session.in_transaction() and len(refs) <= 100
                return DirectoryLabels.model_validate(
                    {
                        "subject": subject,
                        "categories": {
                            "tool": [
                                dict(
                                    group="data",
                                    group_name="数据",
                                    code="data.check",
                                    name="核对",
                                ),
                                dict(
                                    group="uncategorized",
                                    group_name="未分类",
                                    code="uncategorized",
                                    name="未分类",
                                ),
                            ],
                            "expert": [],
                        },
                        "items": [
                            {"reference": r, "kind": "tool", "category": "data.check"}
                            for r in refs
                            if count - 30 <= r.method_id.int <= count
                        ],
                    }
                )

            async def planning_formal_refs(self, subject, refs):
                return {
                    r
                    for v in refs
                    if (r := MethodRef.model_validate(v)).method_id.int <= count
                    and r.method_id.int != self.removed
                }

        market = Market()
        registry = SceneRegistry(SimpleNamespace(market=market), repo)
        q = "期初余额和上期末余额差多少"
        page = await registry.planning_versions(
            OWNER, 10, 0, query=PlanningQuery(stage="composition", question=q)
        )
        assert len(page["items"]) == 10 and page["items"][0]["method_id"].int == count
        assert all(row["method_id"].int <= count for row in page["items"])
        assert market.windows == 1
        async with repo.transaction():
            owned = await registry.author_versions("author", 10, 0, goal=q)
            assert owned[0]["method_id"].int == count
            assert all(row["method_id"].int <= count for row in owned)
        from app.application.workbench.directory_browse import browse
        from app.domain.workbench.method_search import DirectoryBrowse

        for scene in ("planning", "owned"):
            query = DirectoryBrowse(kind="tool", category="data", limit=30, question=q)
            first = await browse(registry, "author", scene, query)
            assert (
                first["total"] == 31
                and first["next"] == 30
                and first["items"][0]["method_id"].int == count
            )
            before = market.windows
            last = await browse(
                registry,
                "author",
                scene,
                query.model_copy(update={"category": "data.check", "offset": 30}),
            )
            if scene == "planning":
                assert market.windows == before
                cached_id = last["items"][0]["method_id"].int
            assert len(last["items"]) == 1 and last["next"] is None
            assert len({r["method_id"] for r in first["items"] + last["items"]}) == 31
        # The ranking snapshot does not give continuing permission after withdrawal.
        market.removed = cached_id
        before = market.windows
        refreshed = await browse(
            registry, "author", "planning", query.model_copy(update={"offset": 30})
        )
        assert refreshed["total"] == 30 and refreshed["items"] == []
        assert market.windows > before
        # Equal lexical scores alternate two class hits and one cross-class candidate.
        async with repo.transaction():
            pins, _ = await market.planning_window("author", 0, 10)
            args = [r.model_dump(mode="json") for r in pins]
            tied = await repo.registry_method_candidates(
                OWNER, 6, 0, eligible=args, terms=["整理"], topic_refs=args[:5]
            )
            assert [r["method_id"].int for r in tied] == [1, 2, 6, 3, 4, 7]
