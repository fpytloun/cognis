"""Skill CRUD, import/export, and versioning routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user
from cognis.api.models import (
    SkillCreateRequest,
    SkillExportResponse,
    SkillImportRequest,
    SkillResponse,
    SkillUpdateRequest,
    SkillVersionResponse,
)
from cognis.core.system_skills import get_system_skill_default
from cognis.store.queries import (
    create_skill,
    create_skill_version,
    delete_skill,
    get_next_version_number,
    get_skill_scoped,
    get_skill_version,
    list_skill_versions,
    list_skills,
    reset_skill_to_defaults,
    set_current_version,
    update_skill,
)
from cognis.tools.skill_parser import (
    compute_content_hash,
    export_cognis_yaml,
    export_skill_md,
    parse_skill_content,
)

router = APIRouter(tags=["skills"])


def _version_to_response(row: Any) -> SkillVersionResponse:
    return SkillVersionResponse(
        version_id=row.version_id,
        skill_id=row.skill_id,
        version_number=row.version_number,
        content_hash=row.content_hash,
        schema_version=row.schema_version,
        instructions=row.instructions,
        tools=row.tools,
        prompt_templates=row.prompt_templates,
        secret_placeholders=row.secret_placeholders,
        source_url=row.source_url,
        resolved_url=row.resolved_url,
        commit_sha=row.commit_sha,
        import_checksum=row.import_checksum,
        imported_at=row.imported_at,
        import_format=row.import_format,
        asset_manifest=row.asset_manifest,
        created_at=row.created_at,
    )


def _skill_to_response(row: Any, version_row: Any | None = None) -> SkillResponse:
    current_version = _version_to_response(version_row) if version_row else None
    return SkillResponse(
        skill_id=row.skill_id,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        tools=row.tools,
        prompt_templates=row.prompt_templates,
        tags=row.tags,
        auto_load=row.auto_load,
        is_system=getattr(row, "is_system", False),
        source=row.source,
        current_version_id=row.current_version_id,
        current_version=current_version,
        owner_email=row.owner_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/v1/skills", response_model=list[SkillResponse])
async def list_skills_route(request: Request) -> list[SkillResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_skills(session, owner_email=user.email)
        # Hydrate current versions for all skills that have one
        version_map: dict[str, Any] = {}
        for row in rows:
            if row.current_version_id:
                ver = await get_skill_version(session, row.current_version_id)
                if ver:
                    version_map[row.skill_id] = ver
    return [_skill_to_response(row, version_map.get(row.skill_id)) for row in rows]


@router.get("/api/v1/skills/{skill_id}", response_model=SkillResponse)
async def get_skill_route(request: Request, skill_id: str) -> SkillResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        version_row = None
        if row.current_version_id:
            version_row = await get_skill_version(session, row.current_version_id)
    return _skill_to_response(row, version_row)


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
        # Create initial version
        content_hash = compute_content_hash(body.instructions, body.tools, body.prompt_templates)
        version_row = await create_skill_version(
            session,
            skill_id=row.skill_id,
            version_number=1,
            content_hash=content_hash,
            instructions=body.instructions,
            tools=body.tools,
            prompt_templates=body.prompt_templates,
            secret_placeholders=body.secret_placeholders,
        )
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id
        await session.commit()
    return _skill_to_response(row, version_row)


@router.put("/api/v1/skills/{skill_id}", response_model=SkillResponse)
async def update_skill_route(
    request: Request, skill_id: str, body: SkillUpdateRequest
) -> SkillResponse:
    user = require_current_user(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise api_exception(400, "validation_error", "No fields to update")
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        try:
            row = await update_skill(session, skill_id, owner_email=user.email, **updates)
        except ValueError as exc:
            message = str(exc)
            if message == "Cannot modify system skills directly":
                raise api_exception(403, "forbidden", message) from exc
            raise api_exception(400, "validation_error", message) from exc
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")

        # Create new version if content changed
        version_row = None
        content_fields = {"instructions", "tools", "prompt_templates", "secret_placeholders"}
        if content_fields & set(updates.keys()):
            instructions = updates.get("instructions", row.instructions)
            tools = updates.get("tools", row.tools)
            templates = updates.get("prompt_templates", row.prompt_templates)
            # Preserve existing secret_placeholders if not explicitly updated
            prev_placeholders = None
            if row.current_version_id:
                prev_ver = await get_skill_version(session, row.current_version_id)
                if prev_ver:
                    prev_placeholders = prev_ver.secret_placeholders
            secret_placeholders = updates.get("secret_placeholders", prev_placeholders)
            content_hash = compute_content_hash(instructions, tools, templates)
            next_num = await get_next_version_number(session, skill_id)
            version_row = await create_skill_version(
                session,
                skill_id=skill_id,
                version_number=next_num,
                content_hash=content_hash,
                instructions=instructions,
                tools=tools,
                prompt_templates=templates,
                secret_placeholders=secret_placeholders,
            )
            await set_current_version(session, skill_id, version_row.version_id)
            row.current_version_id = version_row.version_id
        elif row.current_version_id:
            version_row = await get_skill_version(session, row.current_version_id)

        await session.commit()
    return _skill_to_response(row, version_row)


@router.delete("/api/v1/skills/{skill_id}", status_code=204)
async def delete_skill_route(request: Request, skill_id: str) -> None:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        if row.is_system:
            raise api_exception(403, "forbidden", "System skills cannot be deleted")
        try:
            deleted = await delete_skill(session, skill_id, owner_email=user.email)
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if not deleted:
            raise api_exception(404, "not_found", "Skill not found")
        await session.commit()


@router.post("/api/v1/skills/{skill_id}/reset", response_model=SkillResponse)
async def reset_skill_route(request: Request, skill_id: str) -> SkillResponse:
    user = require_current_user(request)
    if user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can reset system skills")
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        if not row.is_system:
            raise api_exception(403, "forbidden", "Only system skills can be reset")
        defaults = get_system_skill_default(skill_id)
        if defaults is None:
            raise api_exception(404, "not_found", "System skill defaults not found")

        current_hash = compute_content_hash(
            row.instructions,
            row.tools,
            row.prompt_templates,
        )
        default_hash = compute_content_hash(
            str(defaults["instructions"]),
            defaults.get("tools"),
            defaults.get("prompt_templates"),
        )
        if current_hash == default_hash:
            version_row = None
            if row.current_version_id:
                version_row = await get_skill_version(session, row.current_version_id)
            return _skill_to_response(row, version_row)

        row = await reset_skill_to_defaults(
            session,
            skill_id,
            name=str(defaults["name"]),
            description=(
                str(defaults["description"]) if defaults.get("description") is not None else None
            ),
            instructions=str(defaults["instructions"]),
            tools=defaults.get("tools"),
            prompt_templates=defaults.get("prompt_templates"),
            tags=list(defaults["tags"]),
            auto_load=False,
        )
        assert row is not None

        content_hash = compute_content_hash(row.instructions, row.tools, row.prompt_templates)
        next_num = await get_next_version_number(session, skill_id)
        version_row = await create_skill_version(
            session,
            skill_id=skill_id,
            version_number=next_num,
            content_hash=content_hash,
            instructions=row.instructions,
            tools=row.tools,
            prompt_templates=row.prompt_templates,
            secret_placeholders=None,
        )
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id
        await session.commit()
    return _skill_to_response(row, version_row)


# --- Versions ---


@router.get(
    "/api/v1/skills/{skill_id}/versions",
    response_model=list[SkillVersionResponse],
)
async def list_skill_versions_route(request: Request, skill_id: str) -> list[SkillVersionResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        versions = await list_skill_versions(session, skill_id)
    return [_version_to_response(v) for v in versions]


# --- Import / Export ---


@router.post("/api/v1/skills/import", response_model=SkillResponse, status_code=201)
async def import_skill_route(request: Request, body: SkillImportRequest) -> SkillResponse:
    """Import a skill from URL or inline content."""
    user = require_current_user(request)

    if body.url:
        from cognis.tools.skill_import import import_skill_from_url

        try:
            skill_data, provenance = await import_skill_from_url(body.url)
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
    elif body.content:
        try:
            skill_data = parse_skill_content(body.content, format=body.format)
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        provenance = None
    else:
        raise api_exception(400, "validation_error", "Either 'url' or 'content' is required")

    name = body.name or skill_data.get("name") or "Imported Skill"
    instructions = skill_data.get("instructions", "")
    tools = skill_data.get("tools")
    templates = skill_data.get("prompt_templates")
    tags = body.tags or skill_data.get("tags") or []
    secret_placeholders = skill_data.get("secret_placeholders")

    content_hash = compute_content_hash(instructions, tools, templates)

    async with request.app.state.session_factory() as session:
        row = await create_skill(
            session,
            name=name,
            description=skill_data.get("description"),
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
            tags=tags,
            auto_load=body.auto_load,
            source="imported",
            owner_email=user.email,
        )
        version_row = await create_skill_version(
            session,
            skill_id=row.skill_id,
            version_number=1,
            content_hash=content_hash,
            instructions=instructions,
            tools=tools,
            prompt_templates=templates,
            secret_placeholders=secret_placeholders,
            source_url=provenance.source_url if provenance else None,
            resolved_url=provenance.resolved_url if provenance else None,
            commit_sha=provenance.commit_sha if provenance else None,
            import_checksum=provenance.import_checksum if provenance else None,
            imported_at=provenance.imported_at if provenance else None,
            import_format=provenance.import_format if provenance else None,
        )
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id
        await session.commit()
    return _skill_to_response(row, version_row)


@router.post(
    "/api/v1/skills/{skill_id}/export",
    response_model=SkillExportResponse,
)
async def export_skill_route(
    request: Request,
    skill_id: str,
    format: str = "skill_md",
) -> SkillExportResponse:
    """Export a skill as SKILL.md or Cognis YAML."""
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        version_row = None
        if row.current_version_id:
            version_row = await get_skill_version(session, row.current_version_id)

    from cognis.models.skill import ImportProvenance, SkillAssetRef, SkillExportData

    provenance = None
    asset_manifest: list[SkillAssetRef] = []
    if version_row:
        if version_row.source_url:
            provenance = ImportProvenance(
                source_url=version_row.source_url,
                resolved_url=version_row.resolved_url,
                commit_sha=version_row.commit_sha,
                import_checksum=version_row.import_checksum,
                imported_at=version_row.imported_at,
                import_format=version_row.import_format,
            )
        if version_row.asset_manifest:
            asset_manifest = [SkillAssetRef.model_validate(a) for a in version_row.asset_manifest]

    export_data = SkillExportData(
        name=row.name,
        description=row.description,
        tags=row.tags or [],
        auto_load=row.auto_load,
        instructions=version_row.instructions if version_row else row.instructions,
        tools=version_row.tools or [] if version_row else row.tools or [],
        prompt_templates=version_row.prompt_templates or {}
        if version_row
        else row.prompt_templates or {},
        secret_placeholders=version_row.secret_placeholders or [] if version_row else [],
        provenance=provenance,
        asset_manifest=asset_manifest,
    )

    if format == "cognis_yaml":
        content = export_cognis_yaml(export_data)
        filename = f"{row.name.lower().replace(' ', '-')}.yaml"
    else:
        content = export_skill_md(export_data)
        filename = "SKILL.md"

    return SkillExportResponse(format=format, content=content, filename=filename)
