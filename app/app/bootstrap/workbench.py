"""工作台的组装：把基础设施层的实现接到应用层的端口上。

应用层不认识数据库会话、WebSocket 客户端这些具体的东西；接口层、命令行、后台角色都从这里取接好的对象。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.workbench import (
    AppServerConnector,
    EventPublisher,
    KnowmartSummoner,
    Provisioner,
    RelayAdmin,
    WorkbenchStore,
    WorkbenchStores,
)
from app.application.workbench.agent_download import ReleaseStore
from app.application.workbench.delegation import DelegationService
from app.application.workbench.grant_release import GrantReleaser
from app.application.workbench.incubations import (
    IncubationConductor,
    IncubationFolders,
    IncubationService,
)
from app.application.workbench.machines import MachineSync
from app.application.workbench.runner import Runner
from app.application.workbench.test_runs import (
    KnowbotTestConductor,
    KnowbotTestService,
)
from app.domain.workbench.pricing import PriceList
from app.infrastructure.external.knowmart_client import KnowmartSummonClient
from app.infrastructure.workbench.agent_release import S3ReleaseStore
from app.infrastructure.workbench.app_server_client import WsAppServerConnector
from app.infrastructure.workbench.provisioner import HttpProvisioner, ProvisionerConfig
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.relay_admin import WsRelayAdmin
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)
from core.config import get_settings


def agent_release_store(settings) -> ReleaseStore:
    storage = settings.workbench_agent_release_storage
    return S3ReleaseStore(
        endpoint=storage.endpoint,
        region=storage.region,
        ca_file=storage.ca_file,
        access_key=storage.access_key.get_secret_value(),
        secret_key=storage.secret_key.get_secret_value(),
    )


@lru_cache(maxsize=1)
def knowmart_summoner() -> KnowmartSummoner:
    """向 knowmart 取分身清单（令牌缓存在里面，进程内共用）。"""
    return KnowmartSummonClient()


# 凭运行授权读 knowbase 的超时：不超过模型调用工具的 60 秒
KNOWBASE_DATA_TIMEOUT = 45.0


def build_grant_releaser(
    session_factory: async_sessionmaker[AsyncSession],
) -> GrantReleaser:
    """委托结束后交还运行授权（runner 里一条线）：走 knowbase user-data/v2 的交还接口。"""
    return GrantReleaser(workbench_stores(session_factory), knowbase_v2().release_run)


@lru_cache(maxsize=1)
def knowbase_v2():
    """knowbase 资料库第四版（解析、确认、运行授权、会话、数据 MCP；进程内共用）。"""
    from app.infrastructure.external.knowbase_data_v2 import KnowbaseDataV2

    return KnowbaseDataV2(timeout=KNOWBASE_DATA_TIMEOUT)


@lru_cache(maxsize=1)
def knowmart_catalog():
    """knowmart 目录（替用户「找专家」，关系 catalog；进程内共用）。"""
    from app.infrastructure.external.ai_summon_clients import KnowmartCatalog

    return KnowmartCatalog()


@lru_cache(maxsize=1)
def knowmart_demands():
    """knowmart 需求（用户点「发布」后代发，关系 demand；进程内共用）。"""
    from app.infrastructure.external.ai_summon_clients import KnowmartDemands

    return KnowmartDemands()


@lru_cache(maxsize=1)
def knowlab_incubation_requests():
    """knowlab 孵化请求（「孵化一个专家」，关系 incubation-request；进程内共用）。"""
    from app.infrastructure.external.ai_summon_clients import KnowlabIncubationRequests

    return KnowlabIncubationRequests()


def find_expert_service(session: AsyncSession):
    """AI 请专家（请专家方案设计第 4.9 节）：找专家那一轮、取结果、挂需求、孵化请求。"""
    from app.application.workbench.find_expert import FindExpertService

    return FindExpertService(
        workbench_store(session),
        directory=knowmart_catalog(),
        demands=knowmart_demands(),
        incubations=knowlab_incubation_requests(),
    )


@lru_cache(maxsize=1)
def knowfeed_supply():
    """knowfeed 的需求单（请专家要补采的；进程内共用）。"""
    from app.infrastructure.external.knowfeed_supply import KnowfeedSupply

    return KnowfeedSupply()


def expert_data():
    """清单 v2 的专家请来用数据：解析、确认、补采、开跑（SDD 0043 块 6）。"""
    from app.application.workbench.expert_data import ExpertData

    return ExpertData(
        knowbase_v2(),
        knowfeed_supply(),
        supply_enabled=get_settings().workbench_data_supply_enabled,
    )


def _run_reader():
    """工作台凭委托的运行授权读 knowbase（数值验收回查）；没配签名密钥就没有。"""
    from app.application.workbench.run_reader import RunReader
    from app.application.workbench.tokens import TokenIssuer

    settings = get_settings()
    if not settings.workbench_token_signing_key:
        return None
    return RunReader(
        knowbase_v2(),
        TokenIssuer(
            settings.workbench_token_signing_key,
            issuer=settings.workbench_token_issuer,
        ),
    )


def build_data_waiter(session_factory: async_sessionmaker[AsyncSession]):
    """等数据的委托：查需求单，齐了重解析、续签、钉住、接着跑（runner 里一条线）。"""
    from app.application.workbench.expert_data import DataWaiter

    return DataWaiter(
        workbench_stores(session_factory),
        knowbase_v2(),
        knowfeed_supply(),
    )


def build_output_writer(session_factory: async_sessionmaker[AsyncSession], settings):
    """委托交付的成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节，runner 里一条线）：
    要经会合点读用户电脑上的成稿，没配会合点或签名私钥就没有这条线（返回 None，待写的留着）；
    开关 WORKBENCH_OUTPUT_WRITE_ENABLED 关着也没有（knowbase 认出处依据的那一版上线后再开）。"""
    if not settings.workbench_output_write_enabled:
        return None
    reader = build_deliverable_reader(
        session_factory,
        relay_url=settings.workbench_relay_admin_url,
        signing_key=settings.workbench_token_signing_key,
        issuer_name=settings.workbench_token_issuer,
    )
    if reader is None:
        return None
    from app.application.workbench.outputs import OutputWriter

    return OutputWriter(workbench_stores(session_factory), reader, knowbase_v2())


def workbench_store(session: AsyncSession) -> WorkbenchStore:
    """用调用方已有的数据库会话开一本账（网页接口的每个请求一个会话）。"""
    return WorkbenchRepository(session)


def web_workbench_store(session: AsyncSession) -> WorkbenchStore:
    """Browser surface excludes knowlab test objects; runtime keeps the full ledger."""
    return WorkbenchRepository(session, browser_view=True)


def workbench_stores(
    session_factory: async_sessionmaker[AsyncSession],
) -> WorkbenchStores:
    return SqlWorkbenchStores(session_factory)


def app_server_connector() -> AppServerConnector:
    return WsAppServerConnector()


def event_publisher(redis, prefix: str) -> EventPublisher:
    """`redis` 给 None 时什么都不发（测试、评测）。"""
    return RedisPublisher(redis, prefix)


def build_provisioner(
    *,
    url: str,
    token: str,
    model_provider: str = "",
    model: str = "",
    provider_base_url: str = "",
) -> Provisioner:
    return HttpProvisioner(
        ProvisionerConfig(
            url=url,
            token=token,
            model_provider=model_provider,
            model=model,
            provider_base_url=provider_base_url,
        )
    )


def build_relay_admin(url: str, token: str) -> RelayAdmin:
    return WsRelayAdmin(url, token)


def build_runner(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    publisher: EventPublisher,
    runner_id: str,
    environment_key: str = "user-pc",
    poll_seconds: float = 1.0,
    records: bool = False,
    prices: PriceList | None = None,
    deliverables: DeliverableCheck | None = None,
) -> Runner:
    return Runner(
        workbench_stores(session_factory),
        connector=app_server_connector(),
        publisher=publisher,
        runner_id=runner_id,
        environment_key=environment_key,
        poll_seconds=poll_seconds,
        records=records,
        prices=prices,
        deliverables=deliverables,
        reader=_run_reader(),
        capabilities=capability_service,
    )


def build_deliverable_check(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    relay_url: str | None,
    signing_key: str | None,
    issuer_name: str,
) -> DeliverableCheck | None:
    """0022 段 4：成稿硬检查要经会合点服务流问代理；没配会合点或签名私钥就只有软检查（返回 None）。"""
    if not (relay_url and signing_key):
        return None
    from app.application.workbench.deliverable_check import AgentDeliverableCheck
    from app.application.workbench.tokens import TokenIssuer

    return AgentDeliverableCheck(
        build_agent_service(relay_url, TokenIssuer(signing_key, issuer=issuer_name)),
        workbench_stores(session_factory),
    )


def build_deliverable_reader(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    relay_url: str | None,
    signing_key: str | None,
    issuer_name: str,
) -> AgentDeliverableCheck | None:
    """SDD 0037：测试的每条用例跑完时经会合点服务流读回成稿；没配会合点或签名私钥就不读（返回 None）。"""
    if not (relay_url and signing_key):
        return None
    from app.application.workbench.deliverable_check import AgentDeliverableCheck
    from app.application.workbench.tokens import TokenIssuer

    return AgentDeliverableCheck(
        build_agent_service(relay_url, TokenIssuer(signing_key, issuer=issuer_name)),
        workbench_stores(session_factory),
    )


def build_machine_sync(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    relay_admin_url: str | None,
    relay_admin_token: str | None,
    interval_seconds: float,
) -> MachineSync | None:
    """没配会合点管理通道，或间隔是 0，就不同步（返回 None）。"""
    if not (relay_admin_url and relay_admin_token) or interval_seconds <= 0:
        return None
    return MachineSync(
        workbench_stores(session_factory),
        build_relay_admin(relay_admin_url, relay_admin_token),
        interval_seconds=interval_seconds,
    )


if TYPE_CHECKING:
    from app.application.workbench.agent_service import AgentService
    from app.application.workbench.deliverable_check import (
        AgentDeliverableCheck,
        DeliverableCheck,
    )
    from app.application.workbench.test_folders import TestFolders
    from app.application.workbench.tokens import TokenIssuer


def build_agent_service(relay_url: str, issuer: TokenIssuer) -> AgentService:
    """0022：工作台到代理的服务流客户端（经会合点 /service）。"""
    from app.infrastructure.workbench.agent_service_ws import WsAgentService

    return WsAgentService(relay_url, issuer)


def _postgres_sessions() -> async_sessionmaker[AsyncSession]:
    from app.infrastructure.storage.postgres import get_postgres

    return get_postgres().session_factory


def dynamic_market_owners() -> frozenset[str]:
    return frozenset(get_settings().knowbot_dynamic_market_owners)


def delegation_service(session: AsyncSession) -> DelegationService:
    return DelegationService(
        web_workbench_store(session),
        dynamic_method_owners=dynamic_market_owners(),
        directory=knowmart_catalog(),
    )


def test_run_service(session: AsyncSession) -> KnowbotTestService:
    """作为运行方跑分身测试的三个接口（SDD 0027 §3.4）：都在作者的账号下跑（decisions ⑯）。"""
    settings = get_settings()
    return KnowbotTestService(
        workbench_store(session),
        dynamic_method_owners=frozenset(settings.knowbot_dynamic_test_owners),
        release_grant=knowbase_v2().release_run_grant,
        # 取消时删这次测试的目录（同推进器收尾）；没打开隔离就没有目录
        folders=(
            build_test_folders(_postgres_sessions(), settings)
            if settings.knowbot_test_isolate
            else None
        ),
    )


def build_test_conductor(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> KnowbotTestConductor:
    """推进在跑的测试（在作者的电脑上）；不再要平台的测试账号与测试电脑（decisions ⑯）。"""
    return KnowbotTestConductor(
        workbench_stores(session_factory),
        folders=build_test_folders(session_factory, settings),
        reader=build_deliverable_reader(
            session_factory,
            relay_url=settings.workbench_relay_admin_url,
            signing_key=settings.workbench_token_signing_key,
            issuer_name=settings.workbench_token_issuer,
        ),
        release_grant=knowbase_v2().release_run_grant,
    )


def build_test_folders(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> TestFolders | None:
    """账 129：经会合点服务流请作者的电脑建、删每次测试的目录；没打开隔离、没配会合点或签名私钥
    就不建，测试在共用的工作区里跑。"""
    relay_url = settings.workbench_relay_admin_url
    signing_key = settings.workbench_token_signing_key
    if not (settings.knowbot_test_isolate and relay_url and signing_key):
        return None
    from app.application.workbench.test_folders import AgentTestFolders
    from app.application.workbench.tokens import TokenIssuer

    return AgentTestFolders(
        build_agent_service(
            relay_url,
            TokenIssuer(signing_key, issuer=settings.workbench_token_issuer),
        ),
        workbench_stores(session_factory),
    )


def incubation_service(session: AsyncSession) -> IncubationService:
    """作为运行方跑孵化的一轮的三个接口（专家孵化方案第八节）：在作者的账号下跑（decisions ⑯）。"""
    return IncubationService(workbench_store(session))


def build_incubation_conductor(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> IncubationConductor:
    """推进在跑的孵化（在作者的电脑上、他的「孵化」项目里）。"""
    return IncubationConductor(
        workbench_stores(session_factory),
        folders=build_incubation_folders(session_factory, settings),
    )


def build_incubation_folders(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> IncubationFolders | None:
    """经会合点服务流请作者的本地 link 建「孵化」目录；没配会合点或签名私钥就不建。"""
    relay_url = settings.workbench_relay_admin_url
    signing_key = settings.workbench_token_signing_key
    if not (relay_url and signing_key):
        return None
    from app.application.workbench.incubations import AgentIncubationFolders
    from app.application.workbench.tokens import TokenIssuer

    return AgentIncubationFolders(
        build_agent_service(
            relay_url,
            TokenIssuer(signing_key, issuer=settings.workbench_token_issuer),
        ),
        workbench_stores(session_factory),
    )


def capability_service(repo):
    from cryptography.fernet import Fernet

    from app.application.workbench.capabilities import Capabilities
    from app.infrastructure.workbench.capability_mcp import RemoteMcp

    settings = get_settings()
    cipher = (
        Fernet(settings.workbench_credential_key.encode())
        if settings.workbench_credential_key
        else None
    )
    return Capabilities(
        repo,
        RemoteMcp(tuple(settings.workbench_capability_mcp_urls)),
        cipher,
        runtime_url=settings.workbench_capability_mcp_url,
    )
