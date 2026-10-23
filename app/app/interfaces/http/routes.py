from fastapi import APIRouter

from app.interfaces.endpoints.agent_onboarding_routes import (
    owner_router as agent_onboarding_owner_router,
)
from app.interfaces.endpoints.agent_onboarding_routes import (
    public_router as agent_onboarding_public_router,
)
from app.interfaces.endpoints.knowbot_runtime_routes import (
    router as knowbot_runtime_router,
)
from app.interfaces.endpoints.workbench_routes import router as workbench_router
from app.interfaces.http.admin.auth import router as admin_auth_router
from app.interfaces.http.admin.diagnostics import router as admin_diagnostics_router
from app.interfaces.http.admin.missing_data import (
    router as admin_missing_data_router,
)
from app.interfaces.http.internal.delivery_metrics import (
    router as delivery_metrics_router,
)
from app.interfaces.http.web.auth import router as web_auth_router
from app.interfaces.http.web.cross_app import router as web_cross_app_router
from app.interfaces.http.web.interactions import router as web_interactions_router
from app.interfaces.http.web.workbench_cloud_library import (
    router as workbench_cloud_library_router,
)
from app.interfaces.http.web.workbench_expert import (
    router as workbench_expert_router,
)
from app.interfaces.http.web.workbench_projects import (
    router as workbench_projects_router,
)
from app.interfaces.mcp.workbench_mcp import router as workbench_mcp_router

router = APIRouter()
router.include_router(admin_auth_router)
router.include_router(web_auth_router)
router.include_router(admin_diagnostics_router)
router.include_router(admin_missing_data_router)
router.include_router(delivery_metrics_router)
router.include_router(web_interactions_router)
router.include_router(web_cross_app_router)
router.include_router(workbench_router)
router.include_router(agent_onboarding_public_router)
router.include_router(agent_onboarding_owner_router)
router.include_router(workbench_projects_router)
router.include_router(workbench_expert_router)
# 云端知识库（SDD 0042）：取代原来按交回物算出的「知识库」页
router.include_router(workbench_cloud_library_router)
router.include_router(workbench_mcp_router)
router.include_router(knowbot_runtime_router)
