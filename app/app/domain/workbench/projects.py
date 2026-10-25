"""工作区、项目、对话的种类（PRD/apps/investment.md 第二、七节）。不依赖框架、数据库与网络。

工作区是用户机器上的一个根目录；项目是它下面的一个子目录；对话分聊天与工作。
用户的机器可能是 Windows，也可能是 Linux、macOS：路径怎么拼，跟着工作区自己的写法走。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from app.domain.workbench.errors import ProjectPathInvalid

MAX_TITLE_CHARS = 200
DEFAULT_TITLE_CHARS = 30
MAX_PATH_CHARS = 400
MAX_SEGMENT_CHARS = 120

_WINDOWS_ROOT = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
_FORBIDDEN_IN_SEGMENT = re.compile(r'[<>:"|?*\\\x00-\x1f\x7f]')
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE
)


# Codex 0.155.1 认的审批策略。设置页里还能选 on-failure，它已经不认了：按 on-request 发。
APPROVAL_POLICIES = frozenset({"untrusted", "on-request", "never"})
DEFAULT_APPROVAL_POLICY = "on-request"


def approval_policy(chosen: str | None) -> str:
    return chosen if chosen in APPROVAL_POLICIES else DEFAULT_APPROVAL_POLICY


class ConversationKind(StrEnum):
    CHAT = "chat"  # 一问一答。不改文件，不跑命令
    WORK = "work"  # 代理在项目里动手做。必须属于一个项目
    # 专家：服务端先编排（选专家包、拆步、起 Task）再进入对话。必须属于一个项目。
    # 一个项目里三种对话混放，可有多位专家，同一时刻只跑一位（0017，所有者 2026-10-11）
    EXPERT = "expert"


# 碰文件系统的两种：工作、专家
HANDS_ON: frozenset[ConversationKind] = frozenset(
    {ConversationKind.WORK, ConversationKind.EXPERT}
)


def is_windows_root(root: str) -> bool:
    return bool(_WINDOWS_ROOT.match(root))


def clean_relative_path(raw: str | None) -> str:
    """项目在工作区下的相对路径，用 `/` 分段。空串表示工作区本身。

    不许往上走、不许绝对路径、不许带盘符或别的系统上不能用的字符：
    同一条规则在三种系统上都成立，换一台机器项目还能对上。
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ProjectPathInvalid("project path must be text")
    text = raw.strip()
    if text in ("", "."):
        return ""
    if len(text) > MAX_PATH_CHARS:
        raise ProjectPathInvalid("project path is too long")
    if text.startswith(("/", "\\", "~")) or _WINDOWS_ROOT.match(text):
        raise ProjectPathInvalid("project path must be relative to the workspace")
    segments = text.rstrip("/").split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ProjectPathInvalid("project path must not contain empty, . or ..")
        if len(segment) > MAX_SEGMENT_CHARS:
            raise ProjectPathInvalid("a folder name in the project path is too long")
        if _FORBIDDEN_IN_SEGMENT.search(segment):
            raise ProjectPathInvalid("project path contains a character not allowed")
        if segment != segment.strip() or segment.endswith("."):
            raise ProjectPathInvalid(
                "a folder name must not start or end with a space, or end with a dot"
            )
        if _WINDOWS_RESERVED.match(segment):
            raise ProjectPathInvalid("project path contains a reserved name")
    return "/".join(segments)


def project_dir(workspace_root: str, relative: str) -> str:
    """项目在用户机器上的完整目录。分隔符跟着工作区的写法走。"""
    if not relative:
        return workspace_root
    if is_windows_root(workspace_root):
        return workspace_root.rstrip("\\/") + "\\" + relative.replace("/", "\\")
    return workspace_root.rstrip("/") + "/" + relative


def split_under_roots(directory: str, roots: list[str]) -> tuple[str, str] | None:
    """已有的完整目录落在哪个工作区下：返回（工作区，相对路径）。落不进任何一个返回 None。

    有几个工作区都包含它时取最长的那个。用在迁移旧会话、和仍按完整目录建会话的旧入口。
    """
    best: tuple[str, str] | None = None
    for root in roots:
        windows = is_windows_root(root)
        trimmed = root.rstrip("\\/" if windows else "/") or root
        left, right = (
            (directory.lower(), trimmed.lower())
            if windows
            else (
                directory,
                trimmed,
            )
        )
        if left == right:
            relative = ""
        elif left.startswith(right) and directory[len(trimmed)] in (
            "\\/" if windows else "/"
        ):
            relative = directory[len(trimmed) + 1 :].replace("\\", "/").strip("/")
        else:
            continue
        if best is None or len(trimmed) > len(best[0].rstrip("\\/")):
            best = (root, relative)
    return best


def clean_title(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    return text[:MAX_TITLE_CHARS]


def default_title(first_message: str) -> str:
    """对话的名字默认取第一句话的开头。不为起名字去调模型。"""
    text = " ".join((first_message or "").split())
    if len(text) <= DEFAULT_TITLE_CHARS:
        return text
    return text[:DEFAULT_TITLE_CHARS].rstrip() + "…"


def default_project_title(relative: str, workspace_root: str) -> str:
    source = relative or workspace_root.rstrip("\\/")
    name = re.split(r"[\\/]", source)[-1] or source
    return name[:MAX_TITLE_CHARS]


class Mode(StrEnum):
    """这一轮是在哪种情形下发的。四样设置与给模型的说明都由它定。"""

    CHAT = "chat"  # 聊天：没有项目，或者项目所在的机器不在线
    CHAT_IN_PROJECT = "chat_in_project"
    WORK = "work"
    EXPERT = "expert"


# 一条线只有一个 Codex，每一轮都由账房发起、记账、计费。
# 子代理会另起一条账里没有的线，「目标」会让线自己接着跑：两样都在账外花钱，所以关掉。
# 模型一次看得到的工具结果上限（Codex 的 tool_output_token_limit；字节模式下按 4 字节一记折算）。
# 不设时 Codex 对不认识的模型只给 1 万字节，超出的从中间省掉：年报一页的中间、三张表的中间几十行
# 模型都看不到（2026-10-19 K19 第 2 稿第 6 步「主要会计数据」表读不出，待办账 137）
TOOL_OUTPUT_TOKENS = 25_000
# 工作台交给模型的一个工具结果不超过这么多字节，免得被 Codex 从中间省掉（留出余量）
TOOL_RESULT_MAX_BYTES = TOOL_OUTPUT_TOKENS * 4 * 9 // 10

THREAD_CONFIG: dict[str, Any] = {
    "tool_output_token_limit": TOOL_OUTPUT_TOKENS,
    "features.multi_agent": False,
    "features.goals": False,
    # 让模型能在一轮中间问人（request_user_input → 提问卡，approval.md「Codex 反过来问用户」）。
    # 0.155.1 里是开发中开关；升 Codex 版本要重验它还在不在，不在了就退回文字提问
    "features.default_mode_request_user_input": True,
}

# 被拒后不绕路：Codex 没有这条内置规则，只能在每一轮工作线与顾问步骤的输入里写死（approval.md「被拒之后」）
NO_DETOUR = (
    "If a command or file change of yours is declined, do not reach the same effect another "
    "way. Say what was declined and what therefore cannot be done; continue only with the parts "
    "that do not depend on it; if nothing is left, stop."
)


def mode_of(
    kind: ConversationKind | str,
    *,
    directory: str | None,
    environment_key: str | None,
    environment_online: bool,
    expert: bool = False,
) -> Mode:
    kind = ConversationKind(kind)
    in_project = directory is not None and environment_key is not None
    if kind in HANDS_ON or expert:
        if not in_project:
            raise ValueError("work and expert turns need a project")
        return Mode.EXPERT if (expert or kind is ConversationKind.EXPERT) else Mode.WORK
    if in_project and environment_online:
        return Mode.CHAT_IN_PROJECT
    return Mode.CHAT


WINDOWS_CONSOLE_NOTE = (
    " The machine runs Windows and its console code page is not UTF-8, so raw "
    "PowerShell or cmd output reaches you garbled. Start every PowerShell "
    "-Command script with `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
    "$OutputEncoding=New-Object System.Text.UTF8Encoding($false);` "
    "and every cmd /c command with `chcp 65001>nul &`; prefer PowerShell. Do "
    "this from the first command, do not wait to see garbled text."
)

# 2026-10-17 所有者的工作对话生成 pptx，四次才成：没先看 python-pptx 装没装；把大段 Python 塞进 PowerShell
# 的双引号；在 Windows 上把 apply_patch 当成一个程序去调；脚本经管道交给 python，中文成了问号（代理侧另修）。
# 专家步骤的成稿有 WRITE_FILE_RULES，普通的工作对话没有，所以在能改文件的情形里把这几条写死
WINDOWS_WORK_NOTE = (
    " This Windows machine has no bash, and apply_patch is not a program on it (or on "
    "any machine): it is a command form Codex itself catches. To create "
    "or change a file, run one shell command that is exactly `apply_patch <<'EOF'`, a "
    "newline, the patch text, a newline, `EOF`, with nothing before or after it in the "
    "same command; Codex catches that command itself. To run anything longer than one "
    "line (a Python script, for example), first write it to a file that way, then run "
    "the file (`python make_slides.py`); never put a long script inside quotes and never "
    "pipe a script into an interpreter. Before you use a library or a program, check "
    'that it is there (for example `python -c "import pptx"`); if it is missing, tell '
    "the user what is missing and ask before installing anything."
)


def windows_console_note(directory: str | None) -> str:
    """Windows 中文机上 PowerShell/cmd 往管道里写 GBK，Codex 按 UTF-8 读就是乱码。

    Codex 自带的「PowerShell 先切 UTF-8」只在它本机 shell 是 PowerShell 时才加；我们的
    app-server 在 Linux 沙箱里，命令转到 Windows 执行端，这一步就没有（2026-10-12 所有者
    实测每次第一条命令都花）。代理侧的根治见账 88；这里先明说给模型。
    """
    return WINDOWS_CONSOLE_NOTE if directory and is_windows_root(directory) else ""


def windows_work_note(directory: str | None) -> str:
    """能改文件的情形（工作、专家）在 Windows 机器上：怎么写文件、怎么跑长脚本、先看依赖在不在。"""
    return WINDOWS_WORK_NOTE if directory and is_windows_root(directory) else ""


def mode_note(mode: Mode | str, *, directory: str | None) -> str:
    """换了情形之后，插进线里给模型看的一段说明。

    同一条线上先聊天、后进项目、再转成工作，Codex 会换工具与权限，但不会告诉模型
    「你现在有执行命令的工具了」。模型记得自己先前说过没有，就一直以为没有
    （2026-09-29 在 Codex 0.155.1 与 kimi-k3 上见到）。所以换情形时明说一次。
    """
    mode = Mode(mode)
    if mode is Mode.CHAT:
        return (
            "[workbench] This conversation is now in CHAT mode with no machine "
            "attached. For this and the following turns you have no shell and no "
            "access to files. Answer from the conversation and from the data tools. "
            "If the user asks you to read or change files, say that this needs a "
            "project and work mode."
        )
    if mode is Mode.CHAT_IN_PROJECT:
        return (
            "[workbench] This conversation is now in CHAT mode inside the project "
            f"directory {directory} on the user's machine. For this and the "
            "following turns the shell tool IS available, whatever was said earlier "
            "in this conversation: use it to read files in that directory. The file "
            "system is read-only and nobody will be asked for approval, so do not "
            "try to create, change or delete anything. If the user asks for a "
            "change, say that this conversation has to be switched to work mode."
            + windows_console_note(directory)
        )
    return (
        "[workbench] This conversation is now in WORK mode inside the project "
        f"directory {directory} on the user's machine. For this and the following "
        "turns the shell tool IS available, whatever was said earlier in this "
        "conversation. You may read files, and create or change files inside the "
        "project directory. Anything outside the project directory is off limits. "
        + NO_DETOUR
        + windows_console_note(directory)
        + windows_work_note(directory)
    )


def orchestrator_cwd(directory: str | None) -> str | None:
    """顶层 `cwd` 只在执行端也是 POSIX 路径时才发。

    app-server 在沙箱（Linux）里把顶层 `cwd` 当成它自己的本地路径解析：Windows 机器上的
    `C:\\Users\\…` 不是 POSIX 绝对路径，会被拼成 `/data/C:\\Users\\…`，权限里随之多出一条
    解析不了的路径，Windows 代理把命令全拒了（2026-10-07 luna 联调查出）。执行端的目录由
    `environments[].cwd` 带过去，那一项按「POSIX 或 Windows 绝对路径」各自解析，不靠顶层。
    """
    if directory is None or not directory.startswith("/"):
        return None
    return directory


def _top_cwd(directory: str | None) -> dict[str, str]:
    cwd = orchestrator_cwd(directory)
    return {"cwd": cwd} if cwd else {}


def turn_settings(
    kind: ConversationKind | str,
    *,
    directory: str | None,
    environment_key: str | None,
    environment_online: bool,
    approval: str | None,
    expert: bool = False,
    test_round: bool = False,
) -> dict[str, Any]:
    """这一轮发给 Codex 的四样：执行环境、目录、沙箱策略、审批。每一轮都带全。

    同一条线上可以先聊天、再工作、再交给专家，所以不能指望上一轮留下的设置：
    这一轮是什么，就明明白白发什么。页面传来的设置一概不用（F-PROJ-03）。

    test_round：分身测试的一轮。测试电脑上整个 Pod 共用 /tmp，上一次测试写的下一次读得到、
    写满还会被驱逐（Fable 评审 P2-14），所以 /tmp 不给写，临时文件只写 TMPDIR：测试电脑把
    它设到 `<工作区>/runs/.tmp`，开测前随别的目录一起清掉、再建。TMPDIR 照旧可写，没改
    TMPDIR 的测试电脑上它还是 /tmp，行为不变。
    """
    mode = mode_of(
        kind,
        directory=directory,
        environment_key=environment_key,
        environment_online=environment_online,
        expert=expert,
    )
    if mode in (Mode.WORK, Mode.EXPERT):
        return {
            "environments": [{"environmentId": environment_key, "cwd": directory}],
            **_top_cwd(directory),
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
                **(
                    {"excludeSlashTmp": True, "excludeTmpdirEnvVar": False}
                    if test_round
                    else {}
                ),
            },
            "approvalPolicy": approval_policy(approval),
        }
    # 聊天：只读，越出只读的动作直接被拒，不问用户
    settings: dict[str, Any] = {
        "environments": [],
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        "approvalPolicy": "never",
    }
    if mode is Mode.CHAT_IN_PROJECT:
        settings["environments"] = [
            {"environmentId": environment_key, "cwd": directory}
        ]
        settings.update(_top_cwd(directory))
    return settings


def thread_settings(
    kind: ConversationKind | str,
    *,
    directory: str | None,
    environment_key: str | None,
    approval: str | None,
    model: str | None = None,
) -> dict[str, Any]:
    """起一条线时的设置。之后每一轮还会各自带全，这里只是让线一开始就是对的。"""
    kind = ConversationKind(kind)
    in_project = directory is not None and environment_key is not None
    if kind in HANDS_ON and not in_project:
        raise ValueError("work and expert need a project")
    settings: dict[str, Any] = {
        "environments": (
            [{"environmentId": environment_key, "cwd": directory}] if in_project else []
        ),
        "sandbox": "workspace-write" if kind in HANDS_ON else "read-only",
        "approvalPolicy": (approval_policy(approval) if kind in HANDS_ON else "never"),
    }
    settings["config"] = dict(THREAD_CONFIG)
    if in_project:
        settings.update(_top_cwd(directory))
    if model:
        settings["model"] = model
    return settings
