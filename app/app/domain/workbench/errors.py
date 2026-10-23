from __future__ import annotations


class WorkbenchError(Exception):
    code = "workbench_error"
    http_status = 400

    def __init__(self, message: str, **details: object):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(WorkbenchError):
    code = "not_found"
    http_status = 404


class Forbidden(WorkbenchError):
    code = "forbidden"
    http_status = 403


class WheelHeldByOther(WorkbenchError):
    """方向盘在对方手里：用户在 advisor 期间发 turn，或顾问在 user 期间发 turn（AT-06）。"""

    code = "wheel_held_by_other"
    http_status = 409


class IdempotencyConflict(WorkbenchError):
    """同键异摘要（AT-02）。"""

    code = "idempotency_conflict"
    http_status = 409


class StaleStateVersion(WorkbenchError):
    """比较交换失败：另一个入口先动了（AT-13）。"""

    code = "stale_state_version"
    http_status = 409


class InteractionRejected(WorkbenchError):
    """令牌重复、过期、异键、跨 Task、摘要不符（AT-07）。"""

    code = "interaction_rejected"
    http_status = 409


class BudgetExhausted(WorkbenchError):
    code = "budget_exhausted"
    http_status = 409


class RootOutsideWhitelist(WorkbenchError):
    code = "root_outside_whitelist"
    http_status = 400


class ProjectDirectoryMissing(WorkbenchError):
    """电脑上没有这个文件夹（或那是个文件）：不登记，免得把不存在的目录交给 Codex 当工作目录（0024）。"""

    code = "project_directory_missing"
    http_status = 409


class ProjectPathInvalid(WorkbenchError):
    """项目的目录不合规则：越出工作区、带盘符、往上走。"""

    code = "project_path_invalid"
    http_status = 400


class ProjectExists(WorkbenchError):
    """同一台机器、同一个目录已经有一个项目。"""

    code = "project_exists"
    http_status = 409


class ProjectArchived(WorkbenchError):
    code = "project_archived"
    http_status = 409


class ProjectRequired(WorkbenchError):
    """工作与专家必须在项目里；不属于项目的聊天要先放进一个项目。"""

    code = "project_required"
    http_status = 409


class ProjectBusy(WorkbenchError):
    """这个项目里已经有一件事专家还在做：同一时刻只有一个（所有者 2026-09-29 定）。"""

    code = "project_busy"
    http_status = 409


class ProjectHeldByExpert(WorkbenchError):
    """专家在这个项目里干活期间，别的对话可以聊天，不可以工作（所有者 2026-09-29 定）。"""

    code = "project_held_by_expert"
    http_status = 409


class ConversationChangeRefused(WorkbenchError):
    """对话的种类、归属不能这样改：工作不转回聊天；已经归入项目的不能换项目。"""

    code = "conversation_change_refused"
    http_status = 409


class NoSandbox(WorkbenchError):
    """用户还没有可用的沙箱：先在设置里登记模型 key、拉起沙箱。"""

    code = "no_sandbox"
    http_status = 409


class EnvironmentOffline(WorkbenchError):
    """项目所在的机器不在线。专家要在上面干活，所以交不出去。"""

    code = "environment_offline"
    http_status = 409


class NoSuchExpert(WorkbenchError):
    """没有这位专家，或者它不给用户用。"""

    code = "no_such_expert"
    http_status = 404


class RecordsRefused(WorkbenchError):
    """专家读项目记录被拒：不是处理期间，或者要读的东西不在这个项目里。"""

    code = "records_refused"
    http_status = 403


class RelayIdentityBusy(WorkbenchError):
    code = "relay_identity_busy"
    http_status = 409


class RelayIdentityChanged(WorkbenchError):
    code = "relay_identity_changed"
    http_status = 409


class AgentPairingRejected(WorkbenchError):
    code = "agent_pairing_rejected"
    http_status = 404


class AgentPairingUnavailable(WorkbenchError):
    code = "agent_pairing_unavailable"
    http_status = 503


class AgentPairingBusy(WorkbenchError):
    code = "agent_pairing_busy"
    http_status = 409


class AgentPairingSlowDown(WorkbenchError):
    code = "slow_down"
    http_status = 429


class AgentInstallUnavailable(WorkbenchError):
    code = "agent_download_unavailable"
    http_status = 404


class ExpertNotReady(WorkbenchError):
    """筹备中的专家：进清单让人看，但请不了（所有者 2026-10-13 定先占位）。"""

    code = "expert_not_ready"
    http_status = 409


class KnowbotInvalid(WorkbenchError):
    """请来的分身清单工作台跑不了：格式不对、运行方不是这里、越权的工具或审批（SDD 0027 P4）。"""

    code = "knowbot_invalid"
    http_status = 422


class UserDataUnavailable(WorkbenchError):
    """分身绑定的用户数据暂时读不到：knowlab 不可达或没配好（0027 §3.5）。"""

    code = "user_data_unavailable"
    http_status = 503


class DataChanged(UserDataUnavailable):
    """这次委托钉住的数据在委托开始后被改过、挪走或删了（SDD 0042 第五节第 4 项）。

    不是专家的错：数值验收里记成平台没能核（同读不到）。
    """

    code = "data_changed"
    http_status = 409


class DataRequired(WorkbenchError):
    """这位专家写了数据需求声明，请它要先按声明选好数据（SDD 0042 第五节第 6 项）。"""

    code = "data_required"
    http_status = 422


class DataChoiceInvalid(WorkbenchError):
    """选的数据不合这位专家的声明：缺了必填的、份数不对、格式不收、不在知识库里……"""

    code = "data_choice_invalid"
    http_status = 422


class KnowbotUnavailable(WorkbenchError):
    """knowmart 暂时取不到：只影响新的「请」，在跑的委托读自己钉住的那份。"""

    code = "knowbot_unavailable"
    http_status = 503


class KnowbotTestUnconfigured(WorkbenchError):
    """跑分身测试要的测试账号、测试电脑还没配好（SDD 0027 §3.4）。"""

    code = "test_runtime_unconfigured"
    http_status = 503


class KnowbotTestConflict(WorkbenchError):
    """同一个 run_id 不同内容，或同一个问题给了不同的答案。"""

    code = "test_run_conflict"
    http_status = 409


class KnowbotTestBudgetExhausted(WorkbenchError):
    """这个月的测试费用到上限了（所有者定每月 1000 元）。"""

    code = "test_budget_exhausted"
    http_status = 409


class QuestionNotOpen(WorkbenchError):
    """这个问题不在等人回答（没有、已经答过、或那条用例已经结束）。"""

    code = "question_not_open"
    http_status = 409


class ComputeRefused(WorkbenchError):
    """分身的计算不成立：算式不合白名单、地址不合写法、格子没有或不是数、分母为零（0030 §3.2）。"""

    code = "compute_refused"
    http_status = 422
