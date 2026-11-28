"""Exercise publication and consumption of the same clarification in KnowBench."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from app.application.workbench import test_runs as runs
from app.domain.workbench.test_runs import Answer, RunCase


def clarification(question, labels):
    return {
        "id": str(uuid4()),
        "session_id": "session",
        "kind": "question",
        "created_at": "2026-11-26T04:00:00Z",
        "prompt": {
            "subject": {
                "questions": [
                    {
                        "id": question,
                        "question": "选哪个统计口径？",
                        "options": [
                            {"label": label, "description": ""} for label in labels
                        ],
                    }
                ]
            }
        },
    }


async def roundtrip(monkeypatch, interaction, index):
    (published,) = runs._questions_of(interaction, set())
    choice = Answer(
        question_id=published["question_id"],
        option_id=published["options"][index]["id"],
        answered_by="user",
    )

    async def lookup(run_id, question_id):
        if question_id == choice.question_id:
            return {"answer": {"option_id": choice.option_id}}
        return None

    repo = SimpleNamespace(
        get_test_answer=AsyncMock(side_effect=lookup),
        interaction_token=AsyncMock(return_value="test"),
    )
    handoff = AsyncMock()
    monkeypatch.setattr(runs, "answer_interaction", handoff)
    handled = await runs.KnowbotTestConductor(None)._respond_if_ready(
        repo,
        {"run_id": "run"},
        RunCase(case_id="c1", input={"question": "选口径"}),
        interaction,
        SimpleNamespace(actor_id="author"),
    )
    return published, handled, handoff


async def test_published_long_question_reference_can_be_answered(monkeypatch):
    key = "revenue_reporting_scope_" * 5
    published, handled, handoff = await roundtrip(
        monkeypatch, clarification(key, ["合并"]), 0
    )
    assert len(published["question_id"]) <= 128
    assert handled
    handoff.assert_awaited_once()


async def test_two_similar_long_labels_roundtrip_separately(monkeypatch):
    prefix = "年度财务报表的披露口径" * 6
    labels = [prefix + "按合并报表", prefix + "按母公司报表"]
    interaction = clarification("scope", labels)
    for i in range(2):
        published, handled, handoff = await roundtrip(monkeypatch, interaction, i)
        assert [option["label"] for option in published["options"]] == labels
        assert handled
        assert handoff.await_args.kwargs["response"]["answers"]["scope"] == labels[i]
