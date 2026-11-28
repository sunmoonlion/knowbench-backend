"""Browse exact admitted versions by market taxonomy within a scene's existing rights."""

from collections import Counter

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.method_search import search_terms


async def browse(registry, subject, scene, query):
    if registry.market is None:
        raise CapabilityError("Method categories are unavailable")
    taxonomy = (await registry.market.directory_labels(subject, [])).categories
    categories = (
        [item.model_dump() for item in taxonomy[query.kind]] if query.kind else []
    )
    empty = {"items": [], "next": None, "total": 0, "categories": categories}
    if not query.kind or (scene == "shared" and query.kind != "tool"):
        return empty
    codes = {
        c["code"]
        for c in categories
        if not query.category or query.category in (c["code"], c["group"])
    }
    if not codes:
        return empty
    repo = registry.repo
    async with repo.transaction(independent=True):
        owner = await repo.actor_for_subject(subject)
    if owner is None:
        return empty

    terms = search_terms(query.question or "")
    page_key = digest([subject, scene, query.kind, sorted(codes), sorted(terms)])
    if scene == "planning" and query.offset:
        async with repo.transaction():
            saved = await repo.directory_page(
                owner, query.kind, page_key, query.offset, query.limit
            )
        if saved is not None:
            page = await _current_page(
                registry, subject, owner, query, saved, categories, terms
            )
            if page is not None:
                return page

    offset = 0
    after = None
    counts = Counter()
    chosen = {}
    installations = {}
    while True:
        if scene == "shared":
            page = await registry.shared_tools(subject, after=after, limit=100)
            batch = [
                (MethodRef.model_validate(item["reference"]), "tool")
                for item in page["items"]
            ]
            installations.update(
                {
                    ref: bool(item.get("selected"))
                    for (ref, _), item in zip(batch, page["items"], strict=True)
                }
            )
            more = page["next"] is not None
            if more and after and page["next"] <= after:
                raise CapabilityError("Incubation cursor did not advance")
            after = page["next"]
        else:
            eligible = None
            if scene == "planning":
                pins, more = await registry.market.planning_window(subject, offset, 100)
                eligible = [r.model_dump(mode="json") for r in pins]
            async with repo.transaction():
                rows = await repo.registry_method_candidates(
                    owner,
                    100,
                    offset if scene == "owned" else 0,
                    latest=scene == "owned",
                    public=scene == "planning",
                    eligible=eligible,
                )
            if scene == "owned":
                more = len(rows) == 100
            batch = [
                (MethodRef.model_validate(registry.reference(row)), row["method_kind"])
                for row in rows
            ]
            offset += 100
        refs = [r for r, k in batch if k == query.kind]
        if refs:
            labels = await registry.market.directory_labels(subject, refs)
            by_ref = {
                item.reference: item.category
                for item in labels.items
                if item.kind == query.kind
            }
            for ref in refs:
                category = by_ref.get(ref, "uncategorized")
                counts[category] += 1
                if category in codes:
                    chosen[ref] = category
        if not more:
            break
    async with repo.transaction():
        if scene == "planning" and len(chosen) > query.limit:
            ordered = await repo.registry_method_candidates(
                owner,
                len(chosen),
                0,
                public=True,
                terms=terms,
                identities_only=True,
                eligible=[r.model_dump(mode="json") for r in chosen],
            )
            entries = []
            for row in ordered:
                ref = MethodRef.model_validate(registry.reference(row))
                entries.append(
                    dict(reference=ref.model_dump(mode="json"), category=chosen[ref])
                )
            await repo.save_directory_pages(
                owner, query.kind, page_key, entries, counts
            )

        rows = (
            await repo.registry_method_candidates(
                owner,
                query.limit,
                query.offset,
                public=scene != "owned",
                terms=terms,
                eligible=[r.model_dump(mode="json") for r in chosen],
            )
            if chosen
            else []
        )
    for row in rows:
        ref = MethodRef.model_validate(registry.reference(row))
        row["category"] = chosen[ref]
        row["usable"] = installations.get(ref, True)
    return {
        "items": rows,
        "total": len(chosen),
        "categories": [{**c, "count": counts[c["code"]]} for c in categories],
        "next": query.offset + query.limit
        if query.offset + query.limit < len(chosen)
        else None,
    }


async def _current_page(registry, subject, owner, query, saved, categories, terms):
    # Only the ordering and facet counts are snapshots. Recheck this page's rights,
    # classification and local admission before reading any candidate description.
    entries = saved["items"]
    refs = [MethodRef.model_validate(item["reference"]) for item in entries]
    raw = [r.model_dump(mode="json") for r in refs]
    if refs:
        if await registry.market.planning_formal_refs(subject, raw) != set(refs):
            return None
        labels = await registry.market.directory_labels(subject, refs)
        classified = {
            item.reference: item.category
            for item in labels.items
            if item.kind == query.kind
        }
        if any(
            classified.get(r, "uncategorized") != item["category"]
            for r, item in zip(refs, entries, strict=True)
        ):
            return None
    async with registry.repo.transaction():
        rows = (
            await registry.repo.registry_method_candidates(
                owner, query.limit, 0, public=True, terms=terms, eligible=raw
            )
            if refs
            else []
        )
    by_ref = {
        MethodRef.model_validate(registry.reference(r)): r
        for r in rows
        if r["method_kind"] == query.kind
    }
    if set(by_ref) != set(refs):
        return None
    total = saved["total"]
    return {
        "items": [
            {**by_ref[r], "category": item["category"], "usable": True}
            for r, item in zip(refs, entries, strict=True)
        ],
        "total": total,
        "categories": [
            {**c, "count": saved["counts"].get(c["code"], 0)} for c in categories
        ],
        "next": query.offset + query.limit
        if query.offset + query.limit < total
        else None,
    }
