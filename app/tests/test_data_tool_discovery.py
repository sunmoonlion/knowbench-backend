"""Native discovery reads current center records even when the data provider is offline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_service_function_management_db import material_read_revision

from app.application.workbench.tool_registry import registered_functions
from app.domain.workbench.native_functions import BINDINGS


async def test_discovery_refreshes_center_bindings_and_omits_formal_only_reader():
    current = material_read_revision()
    catalog = AsyncMock(side_effect=[[current], [], []])
    repo = SimpleNamespace(scene_function_revisions=catalog)
    assert "read_method_input" not in BINDINGS
    assert "read_input" in await registered_functions(repo, data_available=True)
    assert await registered_functions(repo, data_available=True) == {}
    assert await registered_functions(repo, data_available=False) == {}
    assert "read_input" not in catalog.await_args.args[0]
