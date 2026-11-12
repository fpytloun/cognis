"""HTTP adapters for shared executor management operations."""

from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import require_current_user
from cognis.api.executor_runtime import schedule_executor_reconfigure
from cognis.api.models import (
    ExecutorConfigResponse,
    ExecutorCreateRequest,
    ExecutorTokenResponse,
    ExecutorUpdateRequest,
)
from cognis.core import executor_management as service
from cognis.core.management_access import ManagementActor

router = APIRouter(tags=["executors"])


def management_dependencies(app: Any) -> service.ExecutorManagementDependencies:
    """Bind executor runtime effects at the HTTP/application composition boundary."""

    def trigger(executor_id: str) -> None:
        reconciler = getattr(app.state, "local_model_reconciler", None)
        if reconciler is not None:
            reconciler.trigger(executor_id=executor_id)

    websocket = app.state.providers.executor.websocket
    return service.ExecutorManagementDependencies(
        session_factory=app.state.session_factory,
        websocket=websocket,
        fence=lambda executor_id: service.fence_executor_connection(
            getattr(app.state, "executor_connection_ownership", None),
            websocket,
            executor_id,
        ),
        reconfigure=lambda executor_id: schedule_executor_reconfigure(app, executor_id),
        trigger_reconcile=trigger,
        sign_token=app.state.providers.auth.sign_executor_token,
        credentials=getattr(app.state.providers, "credentials", None),
    )


def _actor(request: Request) -> ManagementActor:
    user = require_current_user(request)
    return ManagementActor(user.email, user.role)


@router.get("/api/v1/executors", response_model=list[ExecutorConfigResponse])
async def list_executors_route(request: Request) -> list[ExecutorConfigResponse]:
    return await service.list_executors_route(management_dependencies(request.app), _actor(request))


@router.get("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def get_executor_route(request: Request, executor_id: str) -> ExecutorConfigResponse:
    return await service.get_executor_route(
        management_dependencies(request.app), _actor(request), executor_id
    )


@router.post("/api/v1/executors", response_model=ExecutorConfigResponse, status_code=201)
async def create_executor_route(
    request: Request, body: ExecutorCreateRequest
) -> ExecutorConfigResponse:
    return await service.create_executor_route(
        management_dependencies(request.app), _actor(request), body
    )


@router.put("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def update_executor_route(
    request: Request, executor_id: str, body: ExecutorUpdateRequest
) -> ExecutorConfigResponse:
    return await service.update_executor_route(
        management_dependencies(request.app), _actor(request), executor_id, body
    )


@router.delete("/api/v1/executors/{executor_id}", status_code=204)
async def delete_executor_route(request: Request, executor_id: str) -> None:
    await service.delete_executor_route(
        management_dependencies(request.app), _actor(request), executor_id
    )


@router.post(
    "/api/v1/executors/{executor_id}/reconfigure",
    response_model=ExecutorConfigResponse,
    status_code=202,
)
async def reconfigure_executor_route(request: Request, executor_id: str) -> ExecutorConfigResponse:
    return await service.reconfigure_executor_route(
        management_dependencies(request.app), _actor(request), executor_id
    )


@router.post("/api/v1/executors/{executor_id}/token", response_model=ExecutorTokenResponse)
async def generate_executor_token_route(
    request: Request, executor_id: str
) -> ExecutorTokenResponse:
    return await service.generate_executor_token_route(
        management_dependencies(request.app), _actor(request), executor_id
    )
