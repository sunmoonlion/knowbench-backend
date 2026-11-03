from app.domain.workbench.data_planning import MAX_TOOL_CALLS, DataPlanningGuard


def item(i, *, tool="list_inputs", args=None, result=None):
    return {
        "id": str(i),
        "type": "mcpToolCall",
        "server": "data",
        "tool": tool,
        "arguments": args or {},
        "result": result or {"inputs": []},
    }


def test_repeated_empty_inputs_are_bounded_but_replayed_events_are_not_counted():
    guard = DataPlanningGuard()
    for _ in range(10):
        guard.observe(item(1))
    assert guard.failure is None and guard.calls == 1
    guard.observe(item(2))
    assert guard.failure is None
    guard.observe(item(3))
    assert guard.failure == "repeated_result"


def test_different_arguments_or_results_make_progress():
    guard = DataPlanningGuard()
    for i in range(10):
        guard.observe(item(i, args={"name": str(i)}))
        guard.observe(item(i + 10, result={"inputs": [str(i)]}))
    assert guard.failure is None


def test_normal_discovery_and_proposal_do_not_trigger_guard():
    guard = DataPlanningGuard()
    for i, tool in enumerate(
        [
            "list_inputs",
            "find_subject",
            "discover_source_tools",
            "lookup",
            "request_data",
        ]
    ):
        guard.observe(item(i, tool=tool))
    assert guard.failure is None


def test_varied_searches_and_unbounded_tool_calls_have_independent_limits():
    search = DataPlanningGuard()
    for i in range(3):
        search.observe({"id": str(i), "type": "webSearch", "query": str(i)})
    assert search.failure == "web_search_limit"
    guard = DataPlanningGuard()
    for i in range(MAX_TOOL_CALLS):
        guard.observe(item(i, args={"query": str(i)}))
    assert guard.failure == "tool_limit"
