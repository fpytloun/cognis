"""Fast witness for the native registration boundary reached by real app startup."""

import pytest
from starlette.requests import Request

from cognis.api.routes.auth import me, router
from cognis.store.models import User
from cognis.tools.builtin.workflow import QUERY_USAGE_TOOL
from cognis.tools.registry import RegisteredTool, ToolRegistry
from tests.e2e.usage_checkpoint.seed import MEMBER


def test_query_usage_registers_with_the_real_native_registry() -> None:
    """The usage tool must not prevent an ordinary in-process executor from starting."""
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=QUERY_USAGE_TOOL))
    assert registry.get("query_usage") is not None


@pytest.mark.asyncio
async def test_auth_me_producer_returns_flat_identity_for_the_browser_contract() -> None:
    """Exercise the actual producer; browser acceptance supplies real authentication."""
    user = User(email=MEMBER, name="Usage browser member", role="user", is_active=True)
    request = Request({"type": "http", "state": {"user": user}})
    route = next(route for route in router.routes if route.path == "/api/auth/me")
    assert route.endpoint is me
    response = await me(request)
    assert response == {"email": MEMBER, "name": user.name, "role": "user"}
    assert "user" not in response
