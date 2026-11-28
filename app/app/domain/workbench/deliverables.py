"""专家步骤的成稿文件（0015 §3.3，账 80）。

成稿写进项目目录，是给用户的唯一一份；工作台只留证据。现在能看到的「写了没有」只有一条路：
这一步的 turn 里 Codex 报过的 `fileChange` 项（补丁改的文件）。命令行写的看不见，所以缺了不算
验收不过，只记一条事件、不填 `workspace_path`；硬检查等本地代理开出文件通道（账 87）之后用
`fs/getMetadata` 做。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

# 各专家包成稿步骤共用的写法要求（原 PRD 七的底稿区块规则改写成对文件的要求）
DELIVERABLE_RULES = (
    "Write the deliverable as a Markdown file for the user, in the user's language. "
    "Sections, in this order: 回答 (the answer in plain words); 数据 (every number as a "
    "table with its source, period and 口径, and a footnote mark pointing to the source); "
    "专家验过什么 (what you checked and how); 局限 (what you could not do or verify); "
    "出处 (one line per source: dataset or file path and the date). "
    "Never give a rating, a target price or a buy/sell recommendation. "
    "Do not paste raw JSON into the file."
)

# 文件怎么写（2026-10-14 现网，Windows + kimi-k3）：Codex 不认识的模型没有 apply_patch 工具，只能经命令行
# 调；Codex 只在整条命令恰好是 `apply_patch <<'EOF' … EOF` 时自己接住（PowerShell 也一样，按 bash 语法
# 解析），别的写法都原样交给 PowerShell，报找不到 apply_patch。模型少写 End Patch、正文行不带 +、改用 bash
# 的 cat 写法，一步耗满 15 分钟。所以这里把补丁格式与命令的样子写死；整份 Add File 会覆盖已有文件，不依赖原文
WRITE_FILE_RULES = (
    "How to write each file: one patch that adds the file with its whole content. Add File "
    "replaces a file that already exists, so never use `*** Update File` for these files "
    "(an earlier version will not match). The patch text is exactly: a line "
    "`*** Begin Patch`; a line `*** Add File: <path>`; then every line of the content with "
    "`+` in front of it (an empty line becomes a lone `+`); a last line `*** End Patch`. "
    "If you have an apply_patch tool, call it with this text. Otherwise run through the "
    "shell tool one command that is exactly `apply_patch <<'EOF'`, a newline, the patch "
    "text, a newline, `EOF` - nothing before or after it in the same command: no pipe, no "
    "here-string, no `cat`, no redirect. Codex catches this command itself on every system, "
    "Windows PowerShell included, and records the change; do not search for an apply_patch "
    "program and do not write the file any other way. If the patch is rejected, read the "
    "error, fix the patch text and send the same command again. Write the files first; "
    "your final message is the JSON only."
)

HANDBACK_FILE = "交回.md"


def handback_rules(path: str = HANDBACK_FILE) -> str:
    return (
        f"Also write `{path}` (path relative to the project directory), the same way (one "
        "Add File hunk; it replaces an earlier one), for the user to keep: 做了什么 (each step "
        "in one line); 验过什么 (what was checked); 未知 (what stays unknown); 改了哪些文件 "
        "(every file you created or changed in this project, one per line); 怎么恢复 (how to "
        "undo the changes if the user wants to)."
    )


HANDBACK_RULES = handback_rules()

# 每请一次专家，底稿写进自己的一格，不覆盖别的对话、也不覆盖同一段对话里前几次的（所有者 2026-10-14）：
# 底稿/对话-<对话编号前 8 位>/<开始时间>-<专家名>/。同一次里返工才覆盖（那是同一份稿子的改稿）。
# 时间按北京时间写：用户在国内，文件夹名是给人在资源管理器里认的
DOSSIER_ROOT = "底稿"
_BEIJING = timezone(timedelta(hours=8))
_NOT_IN_NAMES = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def run_folder(session_id: str, started: datetime, expert: str) -> str:
    """这一次委托的底稿放在项目目录下哪一格（相对路径，正斜杠）。"""
    talk = str(session_id).replace("-", "")[:8]
    when = started.astimezone(_BEIJING).strftime("%Y-%m-%d-%H%M%S")
    name = _NOT_IN_NAMES.sub("", expert).strip(" .") or "专家"
    return f"{DOSSIER_ROOT}/对话-{talk}/{when}-{name}"


def task_folder(task, expert: str) -> str:
    folder = run_folder(str(task["session_id"]), task["created_at"], expert)
    if task.get("dossier_version", 1) == 2:
        return f"{folder}/任务-{task['id']}"
    return folder


def placed(output_file: str, folder: str | None) -> str:
    """专家包里写的成稿文件名放进这一次的那一格。没有那一格（只拼说明看的时候）就原样。"""
    if not folder:
        return output_file
    if len(folder.split("/")) == 4 and folder.rsplit("/", 1)[1].startswith("任务-"):
        return f"{folder}/{output_file}"
    return f"{folder}/{output_file.replace(chr(92), '/').rsplit('/', 1)[-1]}"


def run_files(outputs: Iterable[str], folder: str) -> list[str]:
    """这一次会写出的全部文件：各步的成稿，最后是交回。"""
    return [*(placed(f, folder) for f in outputs), f"{folder}/{HANDBACK_FILE}"]


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()


def deliverable_seen(paths: Iterable[str], output_file: str) -> bool:
    """这一轮 Codex 报过的 fileChange 路径里有没有 `output_file`。路径按结尾比：报的是绝对路径。"""
    wanted = _norm(output_file)
    if not wanted:
        return False
    for raw in paths:
        path = _norm(str(raw or ""))
        if path == wanted or path.endswith("/" + wanted):
            return True
    return False


def changed_paths(item: Any) -> list[str]:
    """一个 `fileChange` 条目里改到的路径；别的条目给空。"""
    if not isinstance(item, dict) or item.get("type") != "fileChange":
        return []
    return [
        str(change.get("path"))
        for change in item.get("changes") or []
        if isinstance(change, dict) and change.get("path")
    ]
