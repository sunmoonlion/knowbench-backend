"""专家处理期间告诉用户的三样：现在在干什么、为什么、没有卡住。纯函数。"""

from __future__ import annotations

from sample_packs import DATA_QUERY, FIN_REVIEW

from app.domain.workbench.now_view import doing_of, now_view
from app.domain.workbench.pack_view import pack_view
from app.domain.workbench.packs import listed_packs

STEPS = [{**s, "times": 0, "rejected": 0} for s in pack_view(FIN_REVIEW)["steps"]]


def event(kind: str, item: dict | None = None, at: str = "t1") -> dict:
    return {"type": kind, "payload": {"item": item} if item else {}, "created_at": at}


def task(state: str, step: int = 2, **more) -> dict:
    return {"state": state, "current_step": step, **more}


def test_every_step_of_every_listed_expert_says_why():
    """「为什么」是给用户看的。每一步都要有，而且不是把「做什么」再说一遍。"""
    for pack in listed_packs():
        for step in pack.workflow:
            assert len(step.why) >= 12, (pack.profile_id, step.step_id)
            assert step.why != step.summary
            # 方法的原文不外露：说明里不出现方法的句子
            assert step.why not in step.method_text
    assert len(FIN_REVIEW.workflow) + len(DATA_QUERY.workflow) == 12


def test_what_it_is_doing_follows_the_events():
    assert doing_of([]) == ("queued", None)
    assert doing_of([event("turn/started")]) == ("thinking", None)
    calling = event(
        "item/started",
        {"type": "mcpToolCall", "arguments": {"dataset": "sh600276-financials"}},
    )
    assert doing_of([event("turn/started"), calling]) == ("data", "sh600276-financials")
    assert doing_of([calling, event("item/completed", {"type": "mcpToolCall"})]) == (
        "thinking",
        None,
    )
    assert doing_of([event("item/started", {"type": "commandExecution"})])[0] == "file"
    assert doing_of([event("item/started", {"type": "agentMessage"})])[0] == "writing"
    assert doing_of([event("turn/completed")])[0] == "checking"
    # 模型的思考只说「在想」，内容不带出来
    thinking = event(
        "item/started", {"type": "reasoning", "summary": ["secret method"]}
    )
    assert doing_of([thinking]) == ("thinking", None)


def test_now_while_running():
    calling = event(
        "item/started",
        {"type": "mcpToolCall", "arguments": {"dataset": "sh600276-financials"}},
        at="t9",
    )
    now = now_view(task("RUNNING"), STEPS, events=[calling], since="t0", held=True)
    assert now is not None
    assert now["step"] == {
        "index": 3,
        "of": 7,
        "title": "勾稽",
        "summary": "三张表之间、前后两年之间对不对得上",
        "why": FIN_REVIEW.workflow[2].why,
    }
    assert now["doing"] == {"code": "data", "text": "在查数据：sh600276-financials"}
    assert (now["since"], now["last_event_at"], now["held"]) == ("t0", "t9", True)
    assert now["redo"] is None
    assert "secret" not in str(now)


def test_a_redo_is_said_to_be_by_the_rules():
    steps = [dict(s) for s in STEPS]
    steps[4] |= {"times": 2, "rejected": 1}
    now = now_view(task("RUNNING", step=4), steps, events=[], since="t0", held=True)
    assert now is not None
    assert now["redo"] == "上一次没有通过验收，这是第 2 次做。重做是按规矩来，不是出错"
    assert now["doing"]["code"] == "queued" and now["last_event_at"] == "t0"


def test_now_while_waiting_and_after_the_end():
    waiting = now_view(
        task("WAITING", waiting_reason="INPUT"),
        STEPS,
        events=[],
        since="t0",
        held=False,
    )
    assert waiting is not None
    assert waiting["doing"] == {"code": "waiting", "text": "停下来了，在等你决定"}
    offline = now_view(
        task("WAITING", waiting_reason="ENVIRONMENT"),
        STEPS,
        events=[],
        since="t0",
        held=False,
    )
    assert offline is not None and offline["doing"]["code"] == "offline"
    for ended in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"):
        assert now_view(task(ended), STEPS, events=[], since="t0", held=False) is None


def test_waiting_for_data_does_not_claim_the_next_analysis_step_is_running():
    current = task("WAITING", step=0, waiting_reason="DATA")
    now = now_view(current, STEPS, events=[], since="t0", held=False)
    assert now is not None
    assert now["step"]["title"] == "数据准备"
    assert "确认" in now["step"]["why"]
    assert now["step"]["why"] != STEPS[0]["why"]
    assert now["doing"]["code"] == "waiting"
    resumed = now_view(task("RUNNING", step=0), STEPS, events=[], since="t1", held=True)
    assert resumed is not None
    assert resumed["step"]["why"] == STEPS[0]["why"]


def test_every_field_an_expert_hands_back_has_a_word_for_the_user():
    """交回物的字段名是英文。专家包里约定的每一个字段都要有给用户看的叫法。"""
    from app.domain.workbench.field_words import FIELD_WORDS, labels_of

    def keys(value) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys(v)}
        return set()

    for pack in listed_packs():
        for step in pack.workflow:
            missing = keys(step.output_schema) - set(FIELD_WORDS)
            # inputs 里面是科目名，随指标变，不登记
            allowed = {"operate_income", "operate_cost"}
            assert missing <= allowed, (pack.profile_id, step.step_id, missing)
    assert labels_of(
        {"checks": [{"rule": "x", "unknown_one": 1}], "data_version": "v"}
    ) == {
        "checks": "勾稽结果",
        "rule": "规则",
        "data_version": "数据版本",
    }
    assert labels_of("文字") == {}
