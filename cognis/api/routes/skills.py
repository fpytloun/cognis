"""Skill CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user
from cognis.api.models import SkillCreateRequest, SkillResponse, SkillUpdateRequest
from cognis.store.queries import create_skill, delete_skill, get_skill, list_skills, update_skill

router = APIRouter(tags=["skills"])


def _skill_to_response(row: object) -> SkillResponse:
    return SkillResponse(
        skill_id=row.skill_id,  # type: ignore[attr-defined]
        name=row.name,  # type: ignore[attr-defined]
        description=row.description,  # type: ignore[attr-defined]
        instructions=row.instructions,  # type: ignore[attr-defined]
        tools=row.tools,  # type: ignore[attr-defined]
        prompt_templates=row.prompt_templates,  # type: ignore[attr-defined]
        tags=row.tags,  # type: ignore[attr-defined]
        auto_load=row.auto_load,  # type: ignore[attr-defined]
        source=row.source,  # type: ignore[attr-defined]
        owner_email=row.owner_email,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


@router.get("/api/v1/skills", response_model=list[SkillResponse])
async def list_skills_route(request: Request) -> list[SkillResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_skills(session, owner_email=user.email)
    return [_skill_to_response(row) for row in rows]


@router.get("/api/v1/skills/{skill_id}", response_model=SkillResponse)
async def get_skill_route(request: Request, skill_id: str) -> SkillResponse:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill(session, skill_id)
    if row is None:
        raise api_exception(404, "not_found", "Skill not found")
    return _skill_to_response(row)


@router.post("/api/v1/skills", response_model=SkillResponse, status_code=201)
async def create_skill_route(request: Request, body: SkillCreateRequest) -> SkillResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await create_skill(
            session,
            name=body.name,
            description=body.description,
            instructions=body.instructions,
            tools=body.tools,
            prompt_templates=body.prompt_templates,
            tags=body.tags,
            auto_load=body.auto_load,
            owner_email=user.email,
        )
        await session.commit()
    return _skill_to_response(row)


@router.put("/api/v1/skills/{skill_id}", response_model=SkillResponse)
async def update_skill_route(
    request: Request, skill_id: str, body: SkillUpdateRequest
) -> SkillResponse:
    require_current_user(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise api_exception(400, "validation_error", "No fields to update")
    async with request.app.state.session_factory() as session:
        try:
            row = await update_skill(session, skill_id, **updates)
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        await session.commit()
    return _skill_to_response(row)


@router.delete("/api/v1/skills/{skill_id}", status_code=204)
async def delete_skill_route(request: Request, skill_id: str) -> None:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        try:
            deleted = await delete_skill(session, skill_id)
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if not deleted:
            raise api_exception(404, "not_found", "Skill not found")
        await session.commit()
