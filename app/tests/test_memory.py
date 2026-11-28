"""长期记忆第一种（0019 §四）：拼给模型看的那一段。"""

from app.domain.workbench.memory import memory_note


def test_only_enabled_facts_and_within_the_limit():
    assert memory_note([]) is None
    assert memory_note([{"text": "看扣非", "enabled": False}]) is None
    note = memory_note(
        [
            {"text": "关注银行股", "enabled": True},
            {"text": "看扣非净利润", "enabled": True},
        ]
    )
    assert note is not None and "- 关注银行股" in note and "- 看扣非净利润" in note
    assert note.startswith("[workbench] About this user")


def test_long_lists_are_cut_at_a_whole_fact_and_say_how_many_are_left():
    facts = [{"text": f"事实{i} " + "x" * 60, "enabled": True} for i in range(12)]
    note = memory_note(facts, limit=300)
    assert note is not None and len(note) < 420
    assert "more facts not shown" in note


def test_long_first_fact_and_omission_notice_fit_the_actual_budget():
    note = memory_note([{"text": "甲" * 400}, {"text": "第二条"}])
    assert note and len(note) <= 300 and "甲…" in note and "1 more facts" in note


def test_blank_memory_text_is_rejected_at_input():
    import pytest
    from pydantic import ValidationError

    from app.interfaces.endpoints.workbench_routes import MemoryIn, MemoryPatch

    for model in (MemoryIn, MemoryPatch):
        with pytest.raises(ValidationError):
            model(text="  ")
        assert model(text=" 甲 ").text == "甲"
