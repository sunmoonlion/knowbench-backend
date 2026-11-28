"""成稿文件有没有写出来，只认这一轮 Codex 报的 fileChange 路径（0015 §3.3，账 80）。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.workbench.deliverables import (
    DELIVERABLE_RULES,
    HANDBACK_RULES,
    WRITE_FILE_RULES,
    changed_paths,
    deliverable_seen,
    handback_rules,
    placed,
    run_files,
    run_folder,
)


def test_matches_by_path_tail_across_windows_and_posix():
    paths = [
        "C:\\Users\\u\\研究\\恒瑞\\底稿\\财报体检-成稿.md",
        "/home/u/research/hengrui/交回.md",
    ]
    assert deliverable_seen(paths, "底稿/财报体检-成稿.md")
    assert deliverable_seen(paths, "交回.md")
    assert not deliverable_seen(paths, "底稿/交回.md")
    assert not deliverable_seen(paths, "成稿.md")
    assert not deliverable_seen(paths, "")
    assert not deliverable_seen([], "交回.md")


def test_changed_paths_only_from_file_change_items():
    item = {
        "type": "fileChange",
        "changes": [{"path": "/p/a.md", "kind": "add"}, {"kind": "x"}],
    }
    assert changed_paths(item) == ["/p/a.md"]
    assert (
        changed_paths({"type": "commandExecution", "changes": [{"path": "/p/b"}]}) == []
    )
    assert changed_paths(None) == []


def test_the_rules_forbid_ratings_and_raw_json_and_name_the_handback_file():
    assert "buy/sell" in DELIVERABLE_RULES and "raw JSON" in DELIVERABLE_RULES
    assert "交回.md" in HANDBACK_RULES and "怎么恢复" in HANDBACK_RULES


def test_files_are_written_whole_in_the_one_command_shape_codex_catches():
    # 2026-10-14 现网：Windows 上 kimi-k3 没有 apply_patch 工具，补丁少 End Patch、正文不带 +、
    # bash 的 cat 写法交给 PowerShell，一步耗满 15 分钟
    rules = WRITE_FILE_RULES
    assert "*** Begin Patch" in rules and "*** Add File: <path>" in rules
    assert "*** End Patch" in rules and "with `+` in front of it" in rules
    assert "never use `*** Update File`" in rules
    assert (
        "exactly `apply_patch <<'EOF'`" in rules and "nothing before or after" in rules
    )
    assert "Windows PowerShell included" in rules
    assert "Add File" in HANDBACK_RULES


# ---------------------------------------------------------------- 按对话、按次分开存（所有者 2026-10-14）


def test_each_request_writes_into_its_own_folder_under_its_conversation():
    talk = "0ca14efb-4af9-40ae-8066-bd48f1c1af84"
    first = run_folder(
        talk, datetime(2026, 10, 14, 1, 26, 23, tzinfo=UTC), "财务分析与预测"
    )
    again = run_folder(
        talk, datetime(2026, 10, 14, 2, 0, 5, tzinfo=UTC), "财务分析与预测"
    )
    other = run_folder(
        "3dda6292-0000", datetime(2026, 10, 14, 1, 26, 23, tzinfo=UTC), "财务分析与预测"
    )
    # 北京时间，资源管理器里按名字排就是先后
    assert first == "底稿/对话-0ca14efb/2026-10-14-092623-财务分析与预测"
    assert again.startswith("底稿/对话-0ca14efb/") and again != first
    assert other.startswith("底稿/对话-3dda6292/")
    # Windows 文件名里不许有的字符拿掉
    assert run_folder("x", datetime(2026, 1, 1, tzinfo=UTC), 'a:b/c?"').endswith("-abc")


def test_the_files_of_one_request_sit_together_with_its_handback():
    folder = "底稿/对话-0ca14efb/2026-10-14-092623-财务分析与预测"
    assert (
        placed("底稿/财务分析与预测-成稿.md", folder)
        == f"{folder}/财务分析与预测-成稿.md"
    )
    assert placed("底稿/财务分析与预测-成稿.md", None) == "底稿/财务分析与预测-成稿.md"
    assert run_files(["底稿/财务分析与预测-成稿.md"], folder) == [
        f"{folder}/财务分析与预测-成稿.md",
        f"{folder}/交回.md",
    ]
    assert f"`{folder}/交回.md`" in handback_rules(f"{folder}/交回.md")


def test_generating_a_translation_respects_the_authors_requested_format():
    from app.application.workbench.task_runtime import TaskRuntime
    from app.domain.workbench.packs import StepContract

    step = StepContract(
        step_id="translate",
        title="翻译参观须知",
        method_text="只交付面向游客的一段英文译文，保留原段落。",
        output_artifact="translation",
        output_file="notice.md",
    )
    prompt = TaskRuntime.compose(step, {"text": "请翻译参观须知"}, {}, 0)
    assert step.method_text in prompt
    assert "Sections, in this order" not in prompt
    assert "buy/sell recommendation" not in prompt
