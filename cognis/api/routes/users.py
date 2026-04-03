"""Admin user management routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_admin
from cognis.api.models import CursorPage, UserCreateRequest, UserResponse, UserUpdateRequest
from cognis.store.queries import (
    count_admins,
    create_user,
    delete_user_cascade,
    disable_user,
    enable_user,
    get_user,
    list_users,
    update_user,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

VALID_ROLES = {"admin", "user", "viewer", "service"}


def _user_response(user: object) -> UserResponse:
    return UserResponse(
        email=user.email,  # type: ignore[attr-defined]
        name=user.name,  # type: ignore[attr-defined]
        role=user.role,  # type: ignore[attr-defined]
        is_active=user.is_active,  # type: ignore[attr-defined]
        created_at=user.created_at,  # type: ignore[attr-defined]
        updated_at=getattr(user, "updated_at", None),
        last_login_at=getattr(user, "last_login_at", None),
        disabled_at=getattr(user, "disabled_at", None),
        disabled_by=getattr(user, "disabled_by", None),
    )


@router.get("/users", response_model=CursorPage[UserResponse])
async def admin_list_users(
    request: Request,
    include_disabled: bool = False,
    limit: int = 100,
) -> CursorPage[UserResponse]:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        users = await list_users(session, include_disabled=include_disabled, limit=limit)
    return CursorPage(
        items=[_user_response(u) for u in users],
        cursor=None,
        has_more=False,
    )


@router.post("/users", response_model=UserResponse, status_code=201)
async def admin_create_user(request: Request, payload: UserCreateRequest) -> UserResponse:
    require_admin(request)
    if payload.role not in VALID_ROLES:
        raise api_exception(400, "validation_error", f"Invalid role: {payload.role}")

    app_state = request.app.state
    async with app_state.session_factory() as session:
        existing = await get_user(session, payload.email)
        if existing is not None:
            raise api_exception(409, "conflict", f"User {payload.email} already exists")
        password_hash = app_state.password_hasher.hash(payload.password)
        user = await create_user(
            session,
            email=payload.email,
            name=payload.name,
            password_hash=password_hash,
            role=payload.role,
        )
        await session.commit()
        await session.refresh(user)
    return _user_response(user)


@router.get("/users/{email}", response_model=UserResponse)
async def admin_get_user(request: Request, email: str) -> UserResponse:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        user = await get_user(session, email)
    if user is None:
        raise api_exception(404, "not_found", f"User {email} not found")
    return _user_response(user)


@router.patch("/users/{email}", response_model=UserResponse)
async def admin_update_user(
    request: Request, email: str, payload: UserUpdateRequest
) -> UserResponse:
    require_admin(request)
    if payload.role is not None and payload.role not in VALID_ROLES:
        raise api_exception(400, "validation_error", f"Invalid role: {payload.role}")

    async with request.app.state.session_factory() as session:
        # Guard: cannot demote the last admin
        if payload.role is not None and payload.role != "admin":
            target = await get_user(session, email)
            if target is not None and target.role == "admin":
                admin_count = await count_admins(session)
                if admin_count <= 1:
                    raise api_exception(
                        400, "validation_error", "Cannot demote the last admin user"
                    )

        user = await update_user(session, email, name=payload.name, role=payload.role)
        if user is None:
            raise api_exception(404, "not_found", f"User {email} not found")
        await session.commit()
        await session.refresh(user)
    return _user_response(user)


@router.post("/users/{email}/disable", response_model=UserResponse)
async def admin_disable_user(request: Request, email: str) -> UserResponse:
    admin = require_admin(request)
    if admin.email == email:
        raise api_exception(400, "validation_error", "Cannot disable yourself")

    async with request.app.state.session_factory() as session:
        target = await get_user(session, email)
        if target is None:
            raise api_exception(404, "not_found", f"User {email} not found")
        if not target.is_active:
            raise api_exception(400, "validation_error", "User is already disabled")
        # Guard: cannot disable the last admin
        if target.role == "admin":
            admin_count = await count_admins(session)
            if admin_count <= 1:
                raise api_exception(400, "validation_error", "Cannot disable the last admin user")
        user = await disable_user(session, email, disabled_by=admin.email)
        await session.commit()
        await session.refresh(user)
    return _user_response(user)


@router.post("/users/{email}/enable", response_model=UserResponse)
async def admin_enable_user(request: Request, email: str) -> UserResponse:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        target = await get_user(session, email)
        if target is None:
            raise api_exception(404, "not_found", f"User {email} not found")
        if target.is_active:
            raise api_exception(400, "validation_error", "User is already active")
        user = await enable_user(session, email)
        await session.commit()
        await session.refresh(user)
    return _user_response(user)


@router.delete("/users/{email}")
async def admin_delete_user(request: Request, email: str, confirm: bool = False) -> dict[str, bool]:
    admin = require_admin(request)
    if admin.email == email:
        raise api_exception(400, "validation_error", "Cannot delete yourself")
    if not confirm:
        raise api_exception(
            400,
            "confirmation_required",
            "Pass ?confirm=true to permanently delete this user and all their data",
        )

    async with request.app.state.session_factory() as session:
        target = await get_user(session, email)
        if target is None:
            raise api_exception(404, "not_found", f"User {email} not found")
        # Guard: cannot delete the last admin
        if target.role == "admin":
            admin_count = await count_admins(session)
            if admin_count <= 1:
                raise api_exception(400, "validation_error", "Cannot delete the last admin user")
        await delete_user_cascade(session, email)
        await session.commit()
    return {"ok": True}
