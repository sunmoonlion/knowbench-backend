"""工作台的端口：应用层需要外界做的事。

应用层只认这里的接口；实现在 `app/infrastructure/workbench/`，在 `app/bootstrap/workbench.py` 里接上。
规则见 k8s 仓 `tree-build/SDD/architecture/engineering.md`「工程结构」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import Any, Protocol

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[Any, str, dict[str, Any]], Awaitable[None]]


class AppServerError(RuntimeError):
    """沙箱里的 app-server 对一次请求答复了错误。"""

    def __init__(self, method: str, error: dict[str, Any]):
        super().__init__(f"{method}: {error.get('message', error)}")
        self.method = method
        self.error = error


class AppServerConnection(Protocol):
    """到一个沙箱的 app-server 的一条连接。"""

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60,  # noqa: ASYNC109
    ) -> Any: ...

    async def respond(self, rid: Any, result: dict[str, Any]) -> None: ...

    async def respond_error(self, rid: Any, code: int, message: str) -> None: ...


class EventPublisher(Protocol):
    """把事件实时推给正在看的页面。推不出去不算错：事件已经在账里，页面重连会补。"""

    def session_channel(self, session_id: str) -> str: ...

    def commands_channel(self) -> str: ...

    async def publish(self, channel: str, payload: dict[str, Any]) -> None: ...


class Cipher(Protocol):
    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, token: bytes) -> bytes: ...


class RelayAdmin(Protocol):
    """会合点的管理通道。"""

    async def set_tokens(self, user: str, agent: str, sandbox: str) -> None: ...

    async def revoke(self, user: str) -> None: ...

    async def set_public_key(self, pem: str) -> None: ...

    async def revoke_jti(self, jtis: list[str]) -> None: ...

    async def permission_receipts(self, receipts: list[dict[str, str]]) -> None:
        """仅在本机权限报告记账事务提交后确认；不是向代理授予权限。"""
        ...

    async def agents(self) -> dict[str, dict[str, Any]]:
        """现在连着会合点的本地代理：会合点用户名 → {codex, software, since, machine}。

        machine 是代理自己报的 {name, roots, ceiling}；老代理不报，是空字典。
        """
        ...


class SandboxModel(Protocol):
    """新建沙箱时默认用哪个模型。"""

    @property
    def model_provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_base_url(self) -> str: ...


class Provisioner(Protocol):
    """沙箱供给器：按用户建、查、删沙箱。"""

    @property
    def config(self) -> SandboxModel: ...

    async def upsert(self, user: str, spec: dict[str, Any]) -> dict[str, Any]: ...

    async def status(self, user: str) -> dict[str, Any]: ...

    async def delete(self, user: str, purge: bool = False) -> dict[str, Any]: ...


class AppServerConnector(Protocol):
    def open(
        self,
        sandbox: Mapping[str, Any],
        *,
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
    ) -> AppServerConnection:
        """按沙箱记录里的地址与令牌引用建一条还没接通的连接。令牌引用由实现去解。"""
        ...


class WorkbenchStore(Protocol):
    """工作台的账：环境、沙箱、会话、事件、命令、委托、待办、交回物、偏好、凭据。

    一个实例对应一次工作单元。状态改动是比较并交换；业务上合不合法不在这里判断。
    """

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """可嵌套。最外层退出时提交，出错时回滚。"""
        ...

    async def commit(self) -> None:
        """把这个工作单元里到目前为止的改动提交。"""
        ...

    async def flush(self) -> None:
        """把改动送到数据库但不提交：让同一个工作单元里后面的读看得到前面的写。"""
        ...

    async def register_environment(
        self,
        *,
        owner_actor_id: str,
        name: str,
        agent_version: str | None,
        codex_version: str | None,
        roots: list[str],
        ceiling: dict,
    ) -> str: ...

    async def list_environments(
        self, *, owner_actor_id: str
    ) -> list[dict[str, Any]]: ...

    async def get_environment(
        self, environment_id: str, *, owner_actor_id: str | None = None
    ) -> dict[str, Any]: ...

    async def set_environment_status(
        self, environment_id: str, status: str
    ) -> None: ...

    async def report_environment(
        self,
        *,
        owner_actor_id: str,
        name: str,
        agent_version: str | None,
        codex_version: str | None,
        roots: list[str],
        ceiling: dict,
    ) -> str:
        """本地代理报到：这个人名下同名的机器有就更新、没有就建，置为在线。返回机器的 id。"""
        ...

    async def set_environments_offline(
        self, owner_actor_id: str, *, except_id: str | None = None
    ) -> int:
        """把这个人名下还标着在线的机器置为离线（`except_id` 那台除外）。返回改了几台。"""
        ...

    async def list_relay_identities(self) -> list[dict[str, Any]]:
        """所有签发过会合点身份的人：`owner_actor_id` 与 `relay_user`。不含令牌。"""
        ...

    async def register_sandbox(
        self,
        *,
        owner_actor_id: str,
        app_server_url: str,
        token_ref: str,
        codex_version: str | None,
    ) -> str: ...

    async def get_sandbox(
        self, sandbox_id: str, *, owner_actor_id: str | None = None
    ) -> dict[str, Any]: ...

    async def list_sandboxes(self, *, owner_actor_id: str) -> list[dict[str, Any]]: ...

    async def current_sandbox(self, owner_actor_id: str) -> dict[str, Any] | None: ...

    async def create_project(
        self,
        *,
        owner_actor_id: str,
        environment_id: str,
        workspace_root: str,
        path: str,
        title: str,
    ) -> str:
        """同一个人、同一台机器、同一个目录已有没归档的项目时抛 `ProjectExists`。"""
        ...

    async def get_project(
        self,
        project_id: str,
        *,
        owner_actor_id: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]: ...

    async def find_project(
        self,
        *,
        owner_actor_id: str,
        environment_id: str,
        workspace_root: str,
        path: str,
    ) -> dict[str, Any] | None: ...

    async def list_projects(
        self, *, owner_actor_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]: ...

    async def update_project(
        self,
        project_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> None: ...

    async def create_session(
        self,
        *,
        owner_actor_id: str,
        environment_id: str | None,
        sandbox_id: str,
        project_root: str | None,
        thread_settings: dict,
        kind: str = "work",
        project_id: str | None = None,
        title: str | None = None,
    ) -> str: ...

    async def attach_session_project(
        self,
        session_id: str,
        *,
        project_id: str,
        environment_id: str,
        project_root: str,
    ) -> None: ...

    async def set_session_kind(self, session_id: str, kind: str) -> None: ...

    async def set_session_title(self, session_id: str, title: str | None) -> None: ...

    async def set_session_title_if_empty(self, session_id: str, title: str) -> None: ...

    async def active_task_of_project(self, project_id: str) -> dict[str, Any] | None:
        """这个项目里专家还在做的那件事；没有就是 None。"""
        ...

    async def next_queued_task(self, project_id: str) -> dict[str, Any] | None: ...
    async def get_session(
        self,
        session_id: str,
        *,
        owner_actor_id: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]: ...

    async def list_sessions(
        self,
        *,
        owner_actor_id: str,
        project_id: str | None = None,
        without_project: bool = False,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]: ...
    async def archive_session(self, session_id: str, *, archived: bool) -> None: ...

    async def set_session_thread(self, session_id: str, thread_id: str) -> None: ...

    async def touch_session(self, session_id: str) -> None: ...

    async def cas_session_wheel(
        self,
        session_id: str,
        *,
        expected_version: int,
        wheel: str,
        active_task_id: str | None,
    ) -> int: ...

    async def append_event(
        self,
        *,
        session_id: str,
        kind: str,
        event_type: str,
        payload: dict,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def append_event_once(
        self,
        *,
        session_id: str,
        kind: str,
        event_type: str,
        payload: dict,
        same: dict[str, Any],
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    async def list_events(
        self, *, session_id: str, after_cursor: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    async def conversation_digests(
        self, *, project_id: str, owner_actor_id: str
    ) -> list[dict[str, Any]]: ...

    async def list_record_events(
        self, *, session_id: str, types: tuple[str, ...]
    ) -> list[dict[str, Any]]: ...

    async def list_owner_interactions(
        self, *, owner_actor_id: str, status: str | None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def interaction_opened_cursor(
        self, *, session_id: str, interaction_id: str
    ) -> int | None: ...

    async def list_task_events(
        self, task_id: str, *, types: tuple[str, ...]
    ) -> list[dict[str, Any]]: ...

    async def summarize_missing_data(self, *, limit: int) -> list[dict[str, Any]]: ...

    async def list_turn_items(
        self, *, session_id: str, turn_ids: list[str]
    ) -> list[dict[str, Any]]: ...

    async def artifact_contents(self, task_id: str) -> dict[str, Any]: ...

    async def find_task_by_idempotency(
        self, *, tenant: str, owner_actor_id: str, profile_id: str, idempotency_key: str
    ) -> dict[str, Any] | None: ...

    async def insert_task(self, task: dict[str, Any]) -> None: ...

    async def get_task(
        self,
        task_id: str,
        *,
        owner_actor_id: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]: ...

    async def list_tasks(
        self,
        *,
        owner_actor_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def list_nonterminal_tasks(self) -> list[dict[str, Any]]: ...

    async def queue_environment_recovery(
        self, environment_id: str, *, owner_actor_id: str
    ) -> int:
        """为这个人的这台机器上的环境等待任务排探测命令；已有待执行探测时不重复。"""
        ...

    async def cas_task(
        self, task_id: str, *, expected_version: int, **fields: Any
    ) -> int:
        """比较交换更新 Task 的若干列；返回新 state_version。"""
        ...

    async def insert_attempt(self, attempt: dict[str, Any]) -> None: ...

    async def get_attempt(
        self, attempt_id: str, *, for_update: bool = False
    ) -> dict[str, Any]: ...

    async def update_attempt(self, attempt_id: str, **fields: Any) -> None: ...

    async def list_attempts(self, task_id: str) -> list[dict[str, Any]]: ...

    async def insert_interaction(
        self,
        *,
        session_id: str,
        task_id: str | None,
        attempt_id: str | None,
        kind: str,
        prompt: dict,
        subject_digest: str | None,
        token: str,
        target_state_version: int | None,
        expires_at,
    ) -> str: ...

    async def get_interaction(
        self, interaction_id: str, *, for_update: bool = False
    ) -> dict[str, Any]: ...

    async def list_interactions(
        self, *, session_id: str, status: str | None = "pending"
    ) -> list[dict[str, Any]]: ...

    async def consume_interaction(
        self, interaction_id: str, *, response: dict, responded_by: str
    ) -> bool:
        """原子消费：只有 pending 且未过期的那一行会被改；返回是否消费成功。"""
        ...

    async def cancel_pending_interactions(
        self, *, task_id: str, reason: str
    ) -> int: ...

    async def put_artifact(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        name: str,
        kind: str,
        content: Any,
        workspace_path: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_artifact(
        self, *, task_id: str, name: str, version: int | None = None
    ) -> dict[str, Any] | None: ...

    async def list_artifacts(self, task_id: str) -> list[dict[str, Any]]: ...

    async def list_artifacts_with_content(self, task_id: str) -> list[dict[str, Any]]:
        """底稿页用：每个名字取最新版本，带内容。"""
        ...

    async def get_prefs(self, owner_actor_id: str) -> dict[str, Any]: ...

    # ---- 长期记忆（0019 §四）----
    async def list_memories(
        self, owner_actor_id: str, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]: ...

    async def insert_memory(
        self, *, owner_actor_id: str, kind: str, text: str, source: dict | None
    ) -> dict[str, Any]: ...

    async def update_memory(
        self,
        memory_id: str,
        *,
        owner_actor_id: str,
        text: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None: ...

    async def delete_memory(self, memory_id: str, *, owner_actor_id: str) -> bool: ...

    async def clear_memories(self, owner_actor_id: str) -> int: ...

    async def put_prefs(
        self,
        owner_actor_id: str,
        *,
        model: str | None,
        approval_policy: str,
        mcp_sources: dict[str, bool] | None = None,
    ) -> dict[str, Any]: ...

    async def add_credential(
        self,
        *,
        owner_actor_id: str,
        sandbox_id: str | None,
        provider: str,
        ciphertext: str,
        hint: str,
        base_url: str | None = None,
    ) -> dict[str, Any]: ...

    async def list_credentials(self, owner_actor_id: str) -> list[dict[str, Any]]: ...

    async def revoke_credential(
        self, credential_id: str, *, owner_actor_id: str
    ) -> bool: ...

    def relay_identity_operation(
        self, owner_actor_id: str
    ) -> AbstractAsyncContextManager[None]:
        """Reject concurrent identity mutations; hold through external registration and commits."""
        ...

    async def create_agent_pairing(
        self, row: dict[str, Any], *, pending_limit: int = 500
    ) -> bool: ...

    async def get_agent_pairing(
        self, pairing_id: str, *, for_update: bool = False
    ) -> dict[str, Any] | None: ...

    async def get_pending_agent_pairing_by_code(
        self, code_hash: str, *, for_update: bool = False
    ) -> dict[str, Any] | None: ...

    async def update_agent_pairing(
        self, pairing_id: str, *, expected_status: str, **fields: Any
    ) -> bool: ...

    async def mark_agent_pairing_delivered(
        self, pairing_id: str
    ) -> dict[str, Any] | None: ...

    async def current_online_environment(
        self, owner_actor_id: str
    ) -> dict[str, Any] | None: ...

    async def append_agent_audit_event(
        self,
        *,
        owner_actor_id: str,
        event_type: str,
        pairing_id: str,
        metadata: dict[str, Any],
    ) -> None: ...

    async def create_agent_install_credential(
        self, *, owner_actor_id: str, token_hash: str, expires_at
    ) -> None: ...

    async def consume_agent_install_credential(
        self, token_hash: str, *, now
    ) -> dict[str, Any] | None: ...

    async def cleanup_agent_onboarding(self, *, now) -> dict[str, int]: ...

    async def get_relay_identity(
        self, owner_actor_id: str
    ) -> dict[str, Any] | None: ...

    async def put_relay_identity(
        self,
        owner_actor_id: str,
        *,
        relay_user: str,
        agent_token_ciphertext: str,
        sandbox_token_ciphertext: str,
    ) -> None: ...

    async def mark_relay_registered(self, owner_actor_id: str) -> None: ...

    async def count_live_provisioned_sandboxes(self) -> int:
        """F-SBX-08：按需拉起、且没回收的沙箱数（全体用户）。"""
        ...

    async def has_live_provisioned_sandbox(self, owner_actor_id: str) -> bool: ...

    async def upsert_provisioned_sandbox(
        self,
        *,
        owner_actor_id: str,
        app_server_url: str,
        token_ref: str,
        codex_version: str | None,
        relay_user: str,
        status: str,
    ) -> str: ...

    async def set_sandbox_status(self, sandbox_id: str, status: str) -> None: ...

    async def active_credential(self, owner_actor_id: str) -> dict[str, Any] | None: ...

    async def ledger_append(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        entry: str,
        amount: Decimal,
        tokens: dict | None = None,
        note: str | None = None,
        actor_id: str | None = None,
    ) -> None: ...

    async def ledger_list(self, task_id: str) -> list[dict[str, Any]]: ...

    async def log_approval(
        self,
        *,
        session_id: str,
        task_id: str | None,
        request_id: str,
        method: str,
        summary: dict,
        decision: str,
        source: str,
        interaction_id: str | None = None,
        wheel: str | None = None,
        step_id: str | None = None,
    ) -> None: ...

    async def list_approvals(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """审批日志，按时间。每一次决定一行，不论放行还是拒绝、策略还是用户（approval.md「看得见」）。"""
        ...

    async def insert_tool_grant(
        self,
        *,
        session_id: str,
        task_id: str | None,
        category: str,
        command_prefix: list[str] | None,
        granted_by: str,
    ) -> str: ...

    async def list_tool_grants(
        self, *, session_id: str, task_id: str | None
    ) -> list[dict[str, Any]]:
        """「这个任务里都允许」记下的授权：有 Task 的按 Task；自驾的按对话，且只认最近一次交接之后的。"""
        ...

    async def enqueue_command(
        self, *, session_id: str, sandbox_id: str, kind: str, payload: dict
    ) -> str: ...

    async def acquire_leases(self, *, runner_id: str, ttl_seconds: int) -> list[str]:
        """给有待办命令、或有委托要人推（排队、在跑、等审批）的沙箱拿租约：没有租约、租约已过期、或本来就是这个名字的才拿得到；返回拿到的沙箱 id。哪些是本进程新拿到的，由 runner 自己记（0034）。"""
        ...

    async def renew_leases(self, *, runner_id: str, ttl_seconds: int) -> list[str]:
        """续本 runner 的租约；返回仍归本 runner 的沙箱 id（丢了的就不在里面）。"""
        ...

    async def sandbox_held(self, sandbox_id: str) -> bool:
        """这个沙箱现在有没有 runner 握着（租约没过期）。给页面一个真的「没有掉线」的信号。"""
        ...

    async def release_leases(self, *, runner_id: str) -> None:
        # 到期而不是删除：运行身份对这张表只有 SELECT/INSERT/UPDATE（数据库策略不发 DELETE）
        ...

    async def expire_pending_tool_approvals(
        self, *, sandbox_id: str, reason: str
    ) -> list[dict[str, Any]]:
        """接管一个沙箱时：上一个 runner 手里未决的工具审批作废（app-server 那边的请求已随连接消失，Codex 会重发）。"""
        ...

    async def unsettled_tasks(self, sandbox_ids: list[str]) -> list[dict[str, Any]]:
        """这些沙箱上要有人推的委托：受理了还没排进项目队列的、核验中的、排队的、在跑的、
        等着 Codex 那一轮里的请求（审批、反问）的。
        带 `request_id`（等的那一问）与 `drive_queued`（已有没做完的 task.drive）。"""
        ...

    async def projects_with_stalled_queue(self) -> list[str]:
        """有委托在排队、却没有委托占着的项目（前一位到了终态，放行那一刻没做成）。"""
        ...

    async def expired_turn_holds(self) -> list[dict[str, Any]]:
        """结束了、但 Codex 里那一轮一直没确认停下、已过 TURN_HOLD_SECONDS 的委托（SDD 0034 §三.1）。"""
        ...

    async def claim_commands(
        self, *, claimed_by: str, limit: int = 20
    ) -> list[dict[str, Any]]: ...

    async def finish_command(
        self, command_id: str, *, error: str | None = None
    ) -> None: ...

    async def requeue_stale_commands(self, *, older_than_seconds: int = 300) -> int: ...

    async def get_session_by_thread(self, thread_id: str) -> dict[str, Any] | None: ...

    async def get_interaction_by_request(
        self, *, session_id: str, request_id: str
    ) -> dict[str, Any] | None: ...

    # ---------- 请来的分身（SDD 0027 P4）----------
    async def pin_knowbot(
        self,
        *,
        owner_actor_id: str,
        owner_subject: str,
        knowbot_id: str,
        version: str,
        digest: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """钉住一版：已有同一版就原样返回（摘要不同抛 KnowbotInvalid），只插不改。"""
        ...

    async def get_knowbot_pin(
        self, *, owner_actor_id: str, knowbot_id: str, version: str
    ) -> dict[str, Any] | None: ...

    async def list_knowbot_pins(self, *, owner_actor_id: str) -> list[dict[str, Any]]:
        """这个人请过的分身，新的在前。"""
        ...

    async def test_run_for_task(self, task_id: str) -> dict[str, Any] | None:
        """这件委托是哪一次分身测试的（没有就是 None）。"""
        ...

    # ---------- 作为运行方跑分身测试（SDD 0027 §3.4）----------
    async def actor_for_subject(self, subject: str) -> str | None:
        """Casdoor 用户 id → 本地 actor id（测试账号用）；没登录过就是 None。"""
        ...

    async def interaction_token(
        self, *, session_id: str, interaction_id: str
    ) -> str | None:
        """交互的令牌只在 interaction/opened 事件里出现一次：测试代答时从那里取。"""
        ...

    async def insert_test_run(
        self, run: dict[str, Any], cases: list[dict[str, Any]]
    ) -> None: ...

    async def get_test_run(
        self, run_id: str, *, for_update: bool = False
    ) -> dict[str, Any] | None: ...

    async def list_test_runs(
        self, *, statuses: tuple[str, ...]
    ) -> list[dict[str, Any]]: ...

    async def update_test_run(self, run_id: str, **changes: Any) -> None: ...

    async def list_test_cases(self, run_id: str) -> list[dict[str, Any]]:
        """按顺序。"""
        ...

    async def update_test_case(
        self, run_id: str, case_id: str, **changes: Any
    ) -> None: ...

    async def get_test_answer(
        self, run_id: str, question_id: str
    ) -> dict[str, Any] | None: ...

    async def insert_test_answer(
        self, run_id: str, question_id: str, answer: dict[str, Any], answered_by: str
    ) -> None: ...

    async def committed_test_spend(self, since: Any) -> Decimal:
        """这个月测试已花的与在跑的测试的上限之和（每月测试费用上限用）。"""
        ...

    async def expired_test_runs(self, now: Any) -> list[dict[str, Any]]:
        """测试产出保留期满、该删的。"""
        ...

    async def delete_session_tree(self, session_id: str) -> None:
        """删一段对话与它下面的一切（只给测试产出到期清理用）。"""
        ...

    async def purge_test_run(self, run_id: str) -> None:
        """删掉这次测试建的委托、对话与交回物；测试结果留着。"""
        ...

    # ---------- 知识库（SDD 0011）----------
    async def list_owner_step_artifacts(
        self, *, owner_actor_id: str
    ) -> list[dict[str, Any]]:
        """这个人全部委托里的交回物（不带内容）：每个版本一条，带所属委托的 project_id、session_id。"""
        ...

    async def library_overlay(
        self, *, owner_actor_id: str
    ) -> dict[str, dict[str, Any]]:
        """用户对自己资料动过的那几条：资料标识 → {title, deleted_at}。"""
        ...

    async def put_library_overlay(
        self,
        *,
        owner_actor_id: str,
        item_id: str,
        title: str | None = None,
        deleted: bool | None = None,
    ) -> None:
        """改名（title 给非 None）或拿掉 / 放回（deleted 给非 None）。"""
        ...


class KnowmartSummoner(Protocol):
    """向 knowmart 取这个所有者的这一版分身清单（服务身份 knowmart:summon，SDD 0027 P4）。

    返回 {"manifest", "digest", "name", "tagline"}。没有、不是他的、停用的、运行方不是这里的，
    都抛 NoSuchExpert；knowmart 不可达或没配好抛 KnowbotUnavailable。
    """

    async def summon(
        self, *, owner_subject: str, knowbot_id: str, version: str
    ) -> dict[str, Any]: ...


class KnowbotDataSource(Protocol):
    """读分身绑定的用户数据（knowlab，服务身份 knowlab:read，契约 knowbot-data/v1，0027 §3.5）。

    purpose 是契约里的用途：`{"kind": "knowbot", ...}` 或 `{"kind": "test", "run_id"}`。
    knowlab 按自己的记录再核一次，不符抛 NotFound；不可达或没配好抛 UserDataUnavailable。
    """

    async def list(self, purpose: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def read(
        self, purpose: dict[str, Any], dataset_id: str, page: int
    ) -> dict[str, Any]: ...

    async def table(
        self, purpose: dict[str, Any], dataset_id: str, sheet: str | None, page: int
    ) -> dict[str, Any]:
        """一张表的行标签、列标签与原文（0030 §3.1）；不是表格抛 UserDataUnavailable。"""
        ...

    async def cells(
        self, purpose: dict[str, Any], cells: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """按（dataset_id、sheet、row、column）取格子，按次序回 found、raw、number、version、sha256。"""
        ...


class WorkbenchStores(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[WorkbenchStore]:
        """开一个工作单元，给出它的账。退出时连接归还。"""
        ...
