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
    KnowbotDataSource,
    KnowmartSummoner,
    Provisioner,
    RelayAdmin,
    WorkbenchStore,
    WorkbenchStores,
)
from app.application.workbench.agent_download import ReleaseStore
from app.application.workbench.machines import MachineSync
from app.application.workbench.pinned_data import DataSources, PinnedData
from app.application.workbench.runner import Runner
from app.application.workbench.test_runs import (
    KnowbotTestConductor,
    KnowbotTestConfig,
    KnowbotTestService,
)
from app.domain.workbench.pricing import PriceList
from app.infrastructure.external.knowlab_data_client import KnowlabDataClient
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


# 读用户数据的超时：knowlab 第一次读一份两百页的年报要抽十几、二十秒文字，默认 20 秒卡在线上；
# 放宽到 45 秒，仍在模型调用工具的 60 秒以内（K19 预演）
KNOWLAB_DATA_TIMEOUT = 45.0
# 凭运行授权从 knowbase 取一份（最大 50 MB）的超时，同上不超过工具的 60 秒
KNOWBASE_DATA_TIMEOUT = 45.0


@lru_cache(maxsize=1)
def knowbot_data_source() -> KnowbotDataSource:
    """专家读用户的数据（进程内共用，抽出的文字与表按 sha256 缓存在里面）。

    委托钉住的那几份（SDD 0042）：云端经 knowbase、本地经会合点服务流（没配会合点或签名私钥
    就读不到本地的）；旧格式的专家经 knowlab 读作者绑的（0027 §3.5）。
    """
    from app.application.workbench.tokens import TokenIssuer
    from app.infrastructure.documents.table_extract import CachedTableExtractor
    from app.infrastructure.documents.text_extract import CachedTextExtractor
    from app.infrastructure.external.knowbase_user_data import (
        KnowbaseGrantedData,
        KnowbaseUserData,
    )

    settings = get_settings()
    relay_url = settings.workbench_relay_admin_url
    signing_key = settings.workbench_token_signing_key
    agent = (
        build_agent_service(
            relay_url, TokenIssuer(signing_key, issuer=settings.workbench_token_issuer)
        )
        if relay_url and signing_key
        else None
    )
    return DataSources(
        pinned=PinnedData(
            cloud=KnowbaseGrantedData(KnowbaseUserData(timeout=KNOWBASE_DATA_TIMEOUT)),
            agent=agent,
            text=CachedTextExtractor(),
            tables=CachedTableExtractor(),
        ),
        legacy=KnowlabDataClient(timeout=KNOWLAB_DATA_TIMEOUT),
    )


def workbench_store(session: AsyncSession) -> WorkbenchStore:
    """用调用方已有的数据库会话开一本账（网页接口的每个请求一个会话）。"""
    return WorkbenchRepository(session)


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
        data=knowbot_data_source(),
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
) -> DeliverableReader | None:
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
        DeliverableCheck,
        DeliverableReader,
    )
    from app.application.workbench.test_folders import TestFolders
    from app.application.workbench.tokens import TokenIssuer


def build_agent_service(relay_url: str, issuer: TokenIssuer) -> AgentService:
    """0022：工作台到代理的服务流客户端（经会合点 /service）。"""
    from app.infrastructure.workbench.agent_service_ws import WsAgentService

    return WsAgentService(relay_url, issuer)


def test_runtime_config(settings) -> KnowbotTestConfig:
    return KnowbotTestConfig(
        subject=settings.knowbot_test_subject,
        workspace_root=settings.knowbot_test_workspace,
        monthly_limit=settings.knowbot_test_monthly_limit,
    )


def test_run_service(session: AsyncSession, settings) -> KnowbotTestService:
    """作为运行方跑分身测试的三个接口（SDD 0027 §3.4）。"""
    return KnowbotTestService(workbench_store(session), test_runtime_config(settings))


def build_test_conductor(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> KnowbotTestConductor | None:
    """没配测试账号与测试电脑就不起这条线。"""
    config = test_runtime_config(settings)
    if not config.subject or not config.workspace_root:
        return None
    return KnowbotTestConductor(
        workbench_stores(session_factory),
        config,
        folders=build_test_folders(session_factory, settings),
        reader=build_deliverable_reader(
            session_factory,
            relay_url=settings.workbench_relay_admin_url,
            signing_key=settings.workbench_token_signing_key,
            issuer_name=settings.workbench_token_issuer,
        ),
    )


def build_test_folders(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> TestFolders | None:
    """账 129：经会合点服务流请测试电脑建、删每次测试的目录；没打开隔离、没配会合点或签名私钥
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
