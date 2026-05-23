"""Skill CRUD, import/export, versioning, and asset-aware routes."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import (
    api_exception,
    check_agent_access,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import (
    SkillAssetResponse,
    SkillCreateRequest,
    SkillDecompositionPreviewResponse,
    SkillExportResponse,
    SkillImportRequest,
    SkillResponse,
    SkillUpdateRequest,
    SkillVersionResponse,
)
from cognis.core.system_skills import get_system_skill_default
from cognis.logging import get_logger
from cognis.models.skill import ImportProvenance, SkillExportData
from cognis.store.queries import (
    create_skill,
    delete_skill,
    get_agent,
    get_next_version_number,
    get_skill_scoped,
    list_skill_versions,
    list_skills,
    reset_skill_to_defaults,
    set_current_version,
    update_agent,
    update_skill,
)
from cognis.tools.skill_import import import_skill_from_url
from cognis.tools.skill_parser import export_cognis_yaml, export_skill_md, parse_skill_content
from cognis.tools.skill_service import (
    asset_refs_to_inputs,
    compute_decomposition_source_hash,
    create_skill_version_with_assets,
    export_cognis_package,
    load_export_assets,
    load_skill_asset_refs,
    normalize_linked_tool_ids,
    normalize_prompt_templates,
    normalize_secret_placeholders,
    normalize_skill_steps,
    normalize_skill_tools,
    parse_cognis_package,
    resolve_current_skill_version,
)
from cognis.tools.skills import raw_skill_tools_to_definitions

logger = get_logger(__name__)

router = APIRouter(tags=["skills"])


async def _bind_skill_to_agent(
    request: Request, session: Any, agent_id: str | None, skill_id: str
) -> None:
    """Attach ``skill_id`` to ``agent_id`` if requested and not already present."""

    if not agent_id:
        return
    agent = await get_agent(session, agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    await check_agent_access(request, agent, required="edit")
    skills = dict(agent.skills or {})
    items = skills.get("items")
    if not isinstance(items, list):
        items = []
    if not any(isinstance(item, dict) and item.get("skill_id") == skill_id for item in items):
        items.append({"skill_id": skill_id, "enabled": True})
        skills["items"] = items
        await update_agent(session, agent_id, updates={"skills": skills})


def _coerce_tools_list(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return None


def _asset_to_response(ref: Any) -> SkillAssetResponse:
    payload = ref.model_dump(mode="json") if hasattr(ref, "model_dump") else ref
    return SkillAssetResponse.model_validate(payload)


def _version_to_response(
    row: Any,
    *,
    asset_refs: list[Any] | None = None,
) -> SkillVersionResponse:
    current_source_hash = compute_decomposition_source_hash(
        str(row.instructions or ""),
        tools=_coerce_tools_list(getattr(row, "tools", None)),
        linked_tool_ids=getattr(row, "linked_tool_ids", None) or [],
        prompt_templates=getattr(row, "prompt_templates", None) or {},
        secret_placeholders=getattr(row, "secret_placeholders", None) or [],
        asset_manifest=getattr(row, "asset_manifest", None) or [],
    )
    decomposition_source_hash = getattr(row, "decomposition_source_hash", None)
    return SkillVersionResponse(
        version_id=row.version_id,
        skill_id=row.skill_id,
        version_number=row.version_number,
        content_hash=row.content_hash,
        schema_version=row.schema_version,
        instructions=row.instructions,
        tools=_coerce_tools_list(row.tools),
        linked_tool_ids=getattr(row, "linked_tool_ids", None) or [],
        prompt_templates=row.prompt_templates,
        secret_placeholders=row.secret_placeholders,
        steps=[item for item in (getattr(row, "steps", None) or []) if isinstance(item, dict)],
        decomposition_source_hash=decomposition_source_hash,
        decomposition_stale=bool(
            decomposition_source_hash and decomposition_source_hash != current_source_hash
        ),
        source_url=row.source_url,
        resolved_url=row.resolved_url,
        commit_sha=row.commit_sha,
        import_checksum=row.import_checksum,
        imported_at=row.imported_at,
        import_format=row.import_format,
        asset_manifest=[_asset_to_response(asset) for asset in (asset_refs or [])],
        created_at=row.created_at,
    )


def _decomposition_inputs_changed(
    *,
    instructions: str,
    tools: list[dict[str, Any]] | None,
    linked_tool_ids: list[str] | None,
    prompt_templates: dict[str, Any] | None,
    secret_placeholders: list[str] | None,
    asset_inputs: list[dict[str, Any]] | None,
    current_instructions: str,
    current_tools: list[dict[str, Any]] | None,
    current_linked_tool_ids: list[str] | None,
    current_templates: dict[str, Any] | None,
    current_placeholders: list[str] | None,
    current_asset_inputs: list[dict[str, Any]] | None,
) -> bool:
    """Return whether decomposition-driving skill content changed."""

    return any(
        [
            instructions != current_instructions,
            (tools or []) != (current_tools or []),
            (linked_tool_ids or []) != (current_linked_tool_ids or []),
            (prompt_templates or {}) != (current_templates or {}),
            (secret_placeholders or []) != (current_placeholders or []),
            (asset_inputs or []) != (current_asset_inputs or []),
        ]
    )


def _skill_to_response(
    row: Any,
    *,
    version_row: Any | None = None,
    asset_refs: list[Any] | None = None,
) -> SkillResponse:
    current_version = (
        _version_to_response(version_row, asset_refs=asset_refs)
        if version_row is not None
        else None
    )
    instructions = version_row.instructions if version_row is not None else row.instructions
    tools = _coerce_tools_list(version_row.tools if version_row is not None else row.tools)
    prompt_templates = (
        version_row.prompt_templates if version_row is not None else row.prompt_templates
    )
    steps = [item for item in (getattr(version_row, "steps", None) or []) if isinstance(item, dict)]
    return SkillResponse(
        skill_id=row.skill_id,
        name=row.name,
        description=row.description,
        instructions=instructions,
        tools=tools,
        linked_tool_ids=row.linked_tool_ids,
        prompt_templates=prompt_templates,
        steps=steps,
        tags=row.tags,
        attach_to_all_agents=row.auto_load,
        auto_load=row.auto_load,
        is_system=getattr(row, "is_system", False),
        source=row.source,
        current_version_id=row.current_version_id,
        current_version=current_version,
        owner_email=row.owner_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _resolve_attach_to_all_agents(
    body: SkillCreateRequest | SkillUpdateRequest | SkillImportRequest,
) -> bool:
    if getattr(body, "attach_to_all_agents", None) is not None:
        return bool(body.attach_to_all_agents)
    legacy = getattr(body, "auto_load", None)
    return bool(legacy) if legacy is not None else False


async def _load_skill_response(request: Request, session: Any, row: Any) -> SkillResponse:
    artifact_store = request.app.state.artifact_store
    version_row = await resolve_current_skill_version(session, row)
    asset_refs = []
    if version_row is not None:
        asset_refs = await load_skill_asset_refs(
            session, version_row, artifact_store=artifact_store
        )
    return _skill_to_response(row, version_row=version_row, asset_refs=asset_refs)


async def _enqueue_skill_tool_classifications(request: Request, row: Any, version_row: Any) -> None:
    queue = getattr(request.app.state, "tool_classification_queue", None)
    if queue is None:
        return
    tool_defs = raw_skill_tools_to_definitions(
        skill_id=row.skill_id,
        version_id=getattr(version_row, "version_id", None),
        content_hash=getattr(version_row, "content_hash", None),
        tools=getattr(version_row, "tools", None),
    )
    if not tool_defs:
        return
    try:
        await queue.enqueue_tools(tool_defs, owner_email=row.owner_email)
    except Exception:
        logger.warning(
            "Failed to enqueue skill tool classifications",
            extra={
                "extra_data": {
                    "skill_id": row.skill_id,
                    "version_id": getattr(version_row, "version_id", None),
                }
            },
            exc_info=True,
        )


def _provenance_from_payload(
    data: dict[str, Any], fallback_format: str | None = None
) -> ImportProvenance | None:
    raw = data.get("provenance")
    if not isinstance(raw, dict):
        if fallback_format is None:
            return None
        return ImportProvenance(import_format=fallback_format)
    payload = dict(raw)
    if fallback_format and not payload.get("import_format"):
        payload["import_format"] = fallback_format
    return ImportProvenance.model_validate(payload)


def _asset_inputs_from_request(items: list[Any] | None) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    return [
        item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
        for item in items
    ]


def _canonical_asset_inputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for item in items:
        payload = dict(item)
        if payload.get("existing_asset_id"):
            payload.pop("content_type", None)
        canonical.append(payload)
    return canonical


def _export_warnings(format_name: str, export_data: SkillExportData) -> list[str]:
    warnings: list[str] = []
    if format_name == "skill_md":
        if export_data.asset_manifest:
            warnings.append(
                "SKILL.md export drops embedded asset files. Use cognis_package for a full-fidelity export."
            )
        if export_data.prompt_templates:
            warnings.append(
                "SKILL.md export preserves prompt_templates as Cognis-only frontmatter. Other runtimes may ignore them."
            )
        if export_data.secret_placeholders:
            warnings.append(
                "SKILL.md export preserves secret_placeholders as Cognis-only frontmatter. Other runtimes may ignore them."
            )
    if format_name == "cognis_yaml" and export_data.asset_manifest:
        warnings.append(
            "Cognis YAML export includes asset metadata only. Use cognis_package for a portable export with files included."
        )
    return warnings


@router.get("/api/v1/skills", response_model=list[SkillResponse])
async def list_skills_route(request: Request) -> list[SkillResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_skills(session, owner_email=user.email)
        responses = [await _load_skill_response(request, session, row) for row in rows]
    return responses


@router.get("/api/v1/skills/{skill_id}", response_model=SkillResponse)
async def get_skill_route(request: Request, skill_id: str) -> SkillResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        return await _load_skill_response(request, session, row)


@router.post("/api/v1/skills", response_model=SkillResponse, status_code=201)
async def create_skill_route(request: Request, body: SkillCreateRequest) -> SkillResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        tools = normalize_skill_tools(body.tools)
        linked_tool_ids = normalize_linked_tool_ids(body.linked_tool_ids) or []
        prompt_templates = normalize_prompt_templates(body.prompt_templates)
        secret_placeholders = normalize_secret_placeholders(body.secret_placeholders)
        steps = normalize_skill_steps(body.steps)
        assets = _asset_inputs_from_request(body.assets)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    async with request.app.state.session_factory() as session:
        row = await create_skill(
            session,
            name=body.name,
            description=body.description,
            instructions=body.instructions,
            tools=tools,
            linked_tool_ids=linked_tool_ids,
            prompt_templates=prompt_templates,
            tags=body.tags,
            auto_load=_resolve_attach_to_all_agents(body),
            owner_email=user.email,
        )
        try:
            version_row = await create_skill_version_with_assets(
                session,
                request.app.state.artifact_store,
                skill_id=row.skill_id,
                version_number=1,
                owner_email=user.email,
                instructions=body.instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=prompt_templates,
                secret_placeholders=secret_placeholders,
                steps=steps,
                assets=assets,
                allow_binary_assets=True,
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id
        await _bind_skill_to_agent(request, session, body.agent_id, row.skill_id)
        await session.commit()
        await _enqueue_skill_tool_classifications(request, row, version_row)
        return await _load_skill_response(request, session, row)


@router.put("/api/v1/skills/{skill_id}", response_model=SkillResponse)
async def update_skill_route(
    request: Request, skill_id: str, body: SkillUpdateRequest
) -> SkillResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    from cognis.core.workflow_composition import decompose_skill_material

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise api_exception(400, "validation_error", "No fields to update")
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        current_version = await resolve_current_skill_version(session, row)
        current_assets = (
            await load_skill_asset_refs(session, current_version)
            if current_version is not None
            else []
        )

        try:
            instructions = body.instructions
            if instructions is None:
                instructions = (
                    current_version.instructions
                    if current_version is not None
                    else row.instructions
                )
            tools = (
                normalize_skill_tools(body.tools)
                if body.tools is not None
                else (current_version.tools if current_version is not None else row.tools)
            )
            linked_tool_ids = (
                normalize_linked_tool_ids(body.linked_tool_ids)
                if body.linked_tool_ids is not None
                else (
                    getattr(current_version, "linked_tool_ids", None)
                    if current_version is not None
                    else row.linked_tool_ids or []
                )
            )
            prompt_templates = (
                normalize_prompt_templates(body.prompt_templates)
                if body.prompt_templates is not None
                else (
                    current_version.prompt_templates
                    if current_version is not None
                    else row.prompt_templates
                )
            )
            secret_placeholders = (
                normalize_secret_placeholders(body.secret_placeholders)
                if body.secret_placeholders is not None
                else (current_version.secret_placeholders if current_version is not None else None)
            )
            steps = (
                normalize_skill_steps(body.steps)
                if body.steps is not None
                else (
                    getattr(current_version, "steps", None) if current_version is not None else None
                )
            )
            asset_inputs = (
                _asset_inputs_from_request(body.assets)
                if body.assets is not None
                else asset_refs_to_inputs(current_assets)
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc

        metadata_updates: dict[str, Any] = {}
        if body.name is not None:
            metadata_updates["name"] = body.name
        if body.description is not None:
            metadata_updates["description"] = body.description
        if body.tags is not None:
            metadata_updates["tags"] = body.tags
        if body.linked_tool_ids is not None:
            metadata_updates["linked_tool_ids"] = linked_tool_ids
        if body.attach_to_all_agents is not None or body.auto_load is not None:
            metadata_updates["auto_load"] = _resolve_attach_to_all_agents(body)

        current_instructions = (
            current_version.instructions if current_version is not None else row.instructions
        )
        current_tools = current_version.tools if current_version is not None else row.tools
        current_templates = (
            current_version.prompt_templates
            if current_version is not None
            else row.prompt_templates
        )
        current_placeholders = (
            current_version.secret_placeholders if current_version is not None else None
        )
        current_steps = (
            getattr(current_version, "steps", None) if current_version is not None else None
        )
        comparable_asset_inputs = _canonical_asset_inputs(asset_inputs)
        current_asset_inputs = _canonical_asset_inputs(asset_refs_to_inputs(current_assets))
        current_source_hash = compute_decomposition_source_hash(
            instructions,
            tools=tools,
            linked_tool_ids=linked_tool_ids,
            prompt_templates=prompt_templates,
            secret_placeholders=secret_placeholders,
            asset_manifest=(
                comparable_asset_inputs
                if body.assets is not None
                else getattr(current_version, "asset_manifest", None) or []
            ),
        )
        if (
            body.steps is not None
            and body.decomposition_source_hash is not None
            and body.decomposition_source_hash != current_source_hash
        ):
            raise api_exception(
                409,
                "stale_decomposition_preview",
                "Skill content changed after the decomposition preview was generated. Refresh the preview before saving.",
            )
        decomposition_inputs_changed = _decomposition_inputs_changed(
            instructions=instructions,
            tools=tools,
            linked_tool_ids=linked_tool_ids,
            prompt_templates=prompt_templates,
            secret_placeholders=secret_placeholders,
            asset_inputs=comparable_asset_inputs,
            current_instructions=current_instructions,
            current_tools=current_tools,
            current_linked_tool_ids=(
                getattr(current_version, "linked_tool_ids", None)
                if current_version is not None
                else row.linked_tool_ids or []
            ),
            current_templates=current_templates,
            current_placeholders=current_placeholders,
            current_asset_inputs=current_asset_inputs,
        )
        if body.steps is None and decomposition_inputs_changed and current_steps:
            llm = getattr(request.app.state.providers, "llm", None)
            if llm is None:
                raise api_exception(
                    503,
                    "service_unavailable",
                    "LLM provider is not available for decomposition refresh",
                )
            try:
                decomposition = await decompose_skill_material(
                    llm=llm,
                    skill_id=row.skill_id,
                    name=body.name or row.name,
                    description=(
                        body.description if body.description is not None else row.description
                    ),
                    instructions=instructions,
                    tools=tools or [],
                    linked_tool_ids=linked_tool_ids,
                    prompt_templates=prompt_templates or {},
                    secret_placeholders=secret_placeholders or [],
                    asset_manifest=comparable_asset_inputs,
                    existing_steps=current_steps,
                    previous_instructions=current_instructions,
                    previous_tools=_coerce_tools_list(current_tools) or [],
                    previous_prompt_templates=current_templates or {},
                    previous_secret_placeholders=current_placeholders or [],
                    previous_asset_manifest=current_asset_inputs,
                )
            except TimeoutError as exc:
                raise api_exception(
                    504,
                    "timeout",
                    "Timed out while refreshing the saved skill decomposition.",
                ) from exc
            except ValueError as exc:
                raise api_exception(
                    502,
                    "provider_error",
                    f"Skill decomposition refresh returned invalid output: {exc}",
                ) from exc
            except Exception as exc:
                raise api_exception(
                    502,
                    "provider_error",
                    f"Failed to refresh the saved skill decomposition: {exc}",
                ) from exc
            steps = decomposition.steps
        content_changed = (
            (body.instructions is not None and instructions != current_instructions)
            or (body.tools is not None and (tools or []) != (current_tools or []))
            or (
                body.prompt_templates is not None
                and (prompt_templates or {}) != (current_templates or {})
            )
            or (
                body.secret_placeholders is not None
                and (secret_placeholders or []) != (current_placeholders or [])
            )
            or (
                body.linked_tool_ids is not None
                and (linked_tool_ids or [])
                != (
                    getattr(current_version, "linked_tool_ids", None)
                    if current_version is not None
                    else row.linked_tool_ids or []
                )
            )
            or (steps or []) != (current_steps or [])
            or (body.assets is not None and comparable_asset_inputs != current_asset_inputs)
        )
        if content_changed:
            metadata_updates.update(
                {
                    "instructions": instructions,
                    "tools": tools,
                    "prompt_templates": prompt_templates,
                }
            )

        try:
            updated_row = await update_skill(
                session,
                skill_id,
                owner_email=user.email,
                **metadata_updates,
            )
        except ValueError as exc:
            message = str(exc)
            if message == "Cannot modify system skills directly":
                raise api_exception(403, "forbidden", message) from exc
            raise api_exception(400, "validation_error", message) from exc
        if updated_row is None:
            raise api_exception(404, "not_found", "Skill not found")

        version_row = None
        if content_changed:
            try:
                version_row = await create_skill_version_with_assets(
                    session,
                    request.app.state.artifact_store,
                    skill_id=skill_id,
                    version_number=await get_next_version_number(session, skill_id),
                    owner_email=user.email,
                    instructions=instructions,
                    tools=tools,
                    linked_tool_ids=linked_tool_ids,
                    prompt_templates=prompt_templates,
                    secret_placeholders=secret_placeholders,
                    steps=steps,
                    assets=asset_inputs,
                    allow_binary_assets=True,
                    source_url=current_version.source_url if current_version is not None else None,
                    resolved_url=current_version.resolved_url
                    if current_version is not None
                    else None,
                    commit_sha=current_version.commit_sha if current_version is not None else None,
                    import_checksum=current_version.import_checksum
                    if current_version is not None
                    else None,
                    imported_at=current_version.imported_at
                    if current_version is not None
                    else None,
                    import_format=current_version.import_format
                    if current_version is not None
                    else None,
                )
            except ValueError as exc:
                raise api_exception(400, "validation_error", str(exc)) from exc
            await set_current_version(session, skill_id, version_row.version_id)
            updated_row.current_version_id = version_row.version_id
        await _bind_skill_to_agent(request, session, body.agent_id, skill_id)
        await session.commit()
        if content_changed and version_row is not None:
            await _enqueue_skill_tool_classifications(request, updated_row, version_row)
        return await _load_skill_response(request, session, updated_row)


@router.delete("/api/v1/skills/{skill_id}", status_code=204)
async def delete_skill_route(request: Request, skill_id: str) -> None:
    forbid_mutation_for_viewer(request)
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
    forbid_mutation_for_viewer(request)
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
        current_version = await resolve_current_skill_version(session, row)
        current_tools = current_version.tools if current_version is not None else row.tools
        current_templates = (
            current_version.prompt_templates
            if current_version is not None
            else row.prompt_templates
        )
        current_steps = (
            getattr(current_version, "steps", None) if current_version is not None else None
        )
        expected_decomposition_hash = (
            compute_decomposition_source_hash(
                str(defaults["instructions"]),
                tools=normalize_skill_tools(defaults.get("tools")),
                linked_tool_ids=normalize_linked_tool_ids(defaults.get("linked_tool_ids")) or [],
                prompt_templates=normalize_prompt_templates(defaults.get("prompt_templates")),
                secret_placeholders=[],
                asset_manifest=[],
            )
            if defaults.get("steps")
            else None
        )
        current_decomposition_hash = (
            getattr(current_version, "decomposition_source_hash", None)
            if current_version is not None
            else None
        )
        current_assets = (
            await load_skill_asset_refs(session, current_version)
            if current_version is not None
            else []
        )
        if (
            row.name == str(defaults["name"])
            and row.description
            == (str(defaults["description"]) if defaults.get("description") is not None else None)
            and (current_version.instructions if current_version is not None else row.instructions)
            == str(defaults["instructions"])
            and current_tools == normalize_skill_tools(defaults.get("tools"))
            and (row.linked_tool_ids or [])
            == (normalize_linked_tool_ids(defaults.get("linked_tool_ids")) or [])
            and current_templates == normalize_prompt_templates(defaults.get("prompt_templates"))
            and (current_steps or []) == (defaults.get("steps") or [])
            and current_decomposition_hash == expected_decomposition_hash
            and (row.tags or []) == list(defaults["tags"])
            and row.auto_load == bool(defaults.get("auto_load", False))
            and not current_assets
        ):
            return await _load_skill_response(request, session, row)
        try:
            row = await reset_skill_to_defaults(
                session,
                skill_id,
                name=str(defaults["name"]),
                description=str(defaults["description"])
                if defaults.get("description") is not None
                else None,
                instructions=str(defaults["instructions"]),
                tools=normalize_skill_tools(defaults.get("tools")),
                linked_tool_ids=normalize_linked_tool_ids(defaults.get("linked_tool_ids")),
                prompt_templates=normalize_prompt_templates(defaults.get("prompt_templates")),
                tags=list(defaults["tags"]),
                auto_load=bool(defaults.get("auto_load", False)),
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        assert row is not None
        try:
            version_row = await create_skill_version_with_assets(
                session,
                request.app.state.artifact_store,
                skill_id=skill_id,
                version_number=await get_next_version_number(session, skill_id),
                owner_email=user.email,
                instructions=row.instructions,
                tools=row.tools,
                linked_tool_ids=normalize_linked_tool_ids(defaults.get("linked_tool_ids")) or [],
                prompt_templates=row.prompt_templates,
                secret_placeholders=None,
                steps=defaults.get("steps") if isinstance(defaults.get("steps"), list) else None,
                assets=None,
                allow_binary_assets=True,
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id
        await session.commit()
        await _enqueue_skill_tool_classifications(request, row, version_row)
        return await _load_skill_response(request, session, row)


@router.get("/api/v1/skills/{skill_id}/versions", response_model=list[SkillVersionResponse])
async def list_skill_versions_route(request: Request, skill_id: str) -> list[SkillVersionResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        versions = await list_skill_versions(session, skill_id)
        return [
            _version_to_response(
                version,
                asset_refs=await load_skill_asset_refs(
                    session,
                    version,
                    artifact_store=request.app.state.artifact_store,
                ),
            )
            for version in versions
        ]


@router.post(
    "/api/v1/skills/{skill_id}/decompose-preview",
    response_model=SkillDecompositionPreviewResponse,
)
async def decompose_skill_preview_route(
    request: Request,
    skill_id: str,
) -> SkillDecompositionPreviewResponse:
    user = require_current_user(request)
    from cognis.core.workflow_composition import decompose_skill_material

    llm = getattr(request.app.state.providers, "llm", None)
    if llm is None:
        raise api_exception(503, "service_unavailable", "LLM provider is not available")

    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        version_row = await resolve_current_skill_version(session, row)
        instructions = version_row.instructions if version_row is not None else row.instructions
        tools = (
            _coerce_tools_list(version_row.tools if version_row is not None else row.tools) or []
        )
        prompt_templates = (
            version_row.prompt_templates if version_row is not None else row.prompt_templates
        ) or {}
        secret_placeholders = (
            list(getattr(version_row, "secret_placeholders", None) or [])
            if version_row is not None
            else []
        )
        asset_manifest = [
            item
            for item in (getattr(version_row, "asset_manifest", None) or [])
            if isinstance(item, dict)
        ]

    try:
        preview = await decompose_skill_material(
            llm=llm,
            skill_id=row.skill_id,
            name=row.name,
            description=row.description,
            instructions=instructions,
            tools=tools,
            linked_tool_ids=row.linked_tool_ids or [],
            prompt_templates=prompt_templates,
            secret_placeholders=secret_placeholders,
            asset_manifest=asset_manifest,
        )
    except ValueError as exc:
        raise api_exception(
            502,
            "provider_error",
            f"Skill decomposition returned invalid output: {exc}",
        ) from exc
    except TimeoutError as exc:
        raise api_exception(
            504,
            "timeout",
            "Timed out while generating the decomposition preview. Try again, or use a faster classifier model.",
        ) from exc

    return SkillDecompositionPreviewResponse(
        skill_id=row.skill_id,
        source_hash=compute_decomposition_source_hash(
            instructions,
            tools=tools,
            linked_tool_ids=row.linked_tool_ids or [],
            prompt_templates=prompt_templates,
            secret_placeholders=secret_placeholders,
            asset_manifest=asset_manifest,
        ),
        rationale=preview.rationale,
        steps=preview.steps,
    )


@router.post(
    "/api/v1/skills/{skill_id}/versions/{version_id}/restore", response_model=SkillResponse
)
async def restore_skill_version_route(
    request: Request,
    skill_id: str,
    version_id: str,
) -> SkillResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        if row.is_system:
            raise api_exception(403, "forbidden", "System skills cannot be restored")
        versions = {
            version.version_id: version for version in await list_skill_versions(session, skill_id)
        }
        version_row = versions.get(version_id)
        if version_row is None:
            raise api_exception(404, "not_found", "Skill version not found")
        await set_current_version(session, skill_id, version_id)
        row.current_version_id = version_id
        row.instructions = version_row.instructions
        row.tools = version_row.tools
        row.linked_tool_ids = getattr(version_row, "linked_tool_ids", None) or []
        row.prompt_templates = version_row.prompt_templates
        await session.commit()
        await _enqueue_skill_tool_classifications(request, row, version_row)
        return await _load_skill_response(request, session, row)


@router.post("/api/v1/skills/import", response_model=SkillResponse, status_code=201)
async def import_skill_route(request: Request, body: SkillImportRequest) -> SkillResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)

    provenance: ImportProvenance | None = None
    try:
        if body.url:
            skill_data, provenance = await import_skill_from_url(body.url)
        elif body.content_b64:
            try:
                raw_content = base64.b64decode(body.content_b64, validate=True)
            except Exception as exc:
                raise api_exception(
                    400, "validation_error", "content_b64 must be valid base64"
                ) from exc
            import_format = body.format or (
                "cognis_package" if (body.filename or "").lower().endswith(".zip") else None
            )
            if import_format == "cognis_package":
                skill_data, _member = parse_cognis_package(raw_content)
                provenance = _provenance_from_payload(skill_data, "cognis_package")
            else:
                text_content = raw_content.decode("utf-8")
                skill_data = parse_skill_content(text_content, format=import_format)
                provenance = _provenance_from_payload(skill_data, import_format)
        elif body.content:
            skill_data = parse_skill_content(body.content, format=body.format)
            provenance = _provenance_from_payload(skill_data, body.format)
        else:
            raise api_exception(
                400,
                "validation_error",
                "One of 'url', 'content', or 'content_b64' is required",
            )
        name = body.name or str(skill_data.get("name") or "Imported Skill")
        instructions = str(skill_data.get("instructions") or "")
        tools = normalize_skill_tools(skill_data.get("tools"))
        linked_tool_ids = (
            normalize_linked_tool_ids(
                body.linked_tool_ids
                if body.linked_tool_ids is not None
                else skill_data.get("linked_tool_ids")
            )
            or []
        )
        prompt_templates = normalize_prompt_templates(skill_data.get("prompt_templates"))
        secret_placeholders = normalize_secret_placeholders(skill_data.get("secret_placeholders"))
        steps = normalize_skill_steps(skill_data.get("steps"))
        tags = body.tags or skill_data.get("tags") or []
        assets = skill_data.get("assets")
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc

    async with request.app.state.session_factory() as session:
        row = await create_skill(
            session,
            name=name,
            description=skill_data.get("description"),
            instructions=instructions,
            tools=tools,
            linked_tool_ids=linked_tool_ids,
            prompt_templates=prompt_templates,
            tags=tags,
            auto_load=_resolve_attach_to_all_agents(body),
            source="imported",
            owner_email=user.email,
        )
        try:
            version_row = await create_skill_version_with_assets(
                session,
                request.app.state.artifact_store,
                skill_id=row.skill_id,
                version_number=1,
                owner_email=user.email,
                instructions=instructions,
                tools=tools,
                linked_tool_ids=linked_tool_ids,
                prompt_templates=prompt_templates,
                secret_placeholders=secret_placeholders,
                steps=steps,
                assets=assets if isinstance(assets, list) else None,
                allow_binary_assets=True,
                source_url=provenance.source_url if provenance else None,
                resolved_url=provenance.resolved_url if provenance else None,
                commit_sha=provenance.commit_sha if provenance else None,
                import_checksum=provenance.import_checksum if provenance else None,
                imported_at=provenance.imported_at if provenance else None,
                import_format=provenance.import_format if provenance else body.format,
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        await set_current_version(session, row.skill_id, version_row.version_id)
        row.current_version_id = version_row.version_id
        await session.commit()
        await _enqueue_skill_tool_classifications(request, row, version_row)
        return await _load_skill_response(request, session, row)


@router.post("/api/v1/skills/{skill_id}/export", response_model=SkillExportResponse)
async def export_skill_route(
    request: Request,
    skill_id: str,
    format: str = "skill_md",
) -> SkillExportResponse:
    user = require_current_user(request)
    if format not in {"skill_md", "cognis_yaml", "cognis_package"}:
        raise api_exception(400, "validation_error", "Unsupported export format")
    async with request.app.state.session_factory() as session:
        row = await get_skill_scoped(session, skill_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Skill not found")
        version_row = await resolve_current_skill_version(session, row)
        asset_refs: list[Any] = []
        asset_bytes: dict[str, bytes] = {}
        if version_row is not None:
            asset_refs, asset_bytes = await load_export_assets(
                session,
                request.app.state.artifact_store,
                version_row,
            )

    provenance = None
    if version_row is not None and version_row.source_url:
        provenance = ImportProvenance(
            source_url=version_row.source_url,
            resolved_url=version_row.resolved_url,
            commit_sha=version_row.commit_sha,
            import_checksum=version_row.import_checksum,
            imported_at=version_row.imported_at,
            import_format=version_row.import_format,
        )

    export_data = SkillExportData(
        name=row.name,
        description=row.description,
        tags=row.tags or [],
        linked_tool_ids=row.linked_tool_ids or [],
        auto_load=row.auto_load,
        instructions=version_row.instructions if version_row is not None else row.instructions,
        tools=version_row.tools or [] if version_row is not None else row.tools or [],
        prompt_templates=version_row.prompt_templates or {}
        if version_row is not None
        else row.prompt_templates or {},
        secret_placeholders=version_row.secret_placeholders or []
        if version_row is not None
        else [],
        steps=[
            item
            for item in ((version_row.steps if version_row is not None else None) or [])
            if isinstance(item, dict)
        ],
        decomposition_source_hash=(
            version_row.decomposition_source_hash if version_row is not None else None
        ),
        provenance=provenance,
        asset_manifest=asset_refs,
    )
    warnings = _export_warnings(format, export_data)

    if format == "cognis_package":
        content = export_cognis_package(export_data, asset_bytes)
        safe_name = row.name.lower().replace(" ", "-")
        return SkillExportResponse(
            format=format,
            content_b64=base64.b64encode(content).decode("ascii"),
            content_type="application/zip",
            filename=f"{safe_name}.cognis-skill.zip",
            warnings=warnings,
        )
    if format == "cognis_yaml":
        content = export_cognis_yaml(export_data)
        filename = f"{row.name.lower().replace(' ', '-')}.yaml"
    else:
        content = export_skill_md(export_data)
        filename = "SKILL.md"
    return SkillExportResponse(
        format=format,
        content=content,
        content_type="text/plain; charset=utf-8",
        filename=filename,
        warnings=warnings,
    )
