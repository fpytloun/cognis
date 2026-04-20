"""Shared skill version, asset, and export helpers."""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import posixpath
import zipfile
from dataclasses import dataclass
from typing import Any

from cognis.models.skill import SkillAssetRef, SkillExportData, SkillToolSpec
from cognis.store.queries import (
    create_skill_asset,
    create_skill_version,
    get_artifact_record,
    get_skill_version,
    list_skill_assets,
)
from cognis.tools.skill_parser import compute_content_hash, export_cognis_yaml

_TEXT_ASSET_EXTENSIONS = {
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".env",
    ".go",
    ".h",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".lua",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_ASSET_CONTENT_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/sql",
    "application/toml",
    "application/x-httpd-php",
    "application/x-python-code",
    "application/x-sh",
    "application/xml",
    "application/yaml",
    "application/zsh",
    "image/svg+xml",
    "text/css",
    "text/csv",
    "text/html",
    "text/javascript",
    "text/markdown",
    "text/plain",
    "text/x-python",
    "text/xml",
}


@dataclass(slots=True)
class PreparedSkillAsset:
    filename: str
    asset_id: str
    artifact_namespace: str
    artifact_object_id: str
    content_hash: str
    size_bytes: int
    content_type: str
    content: bytes | None = None


def normalize_skill_tools(value: Any) -> list[dict[str, Any]] | None:
    """Validate skill tool specs and return a canonical list payload."""

    if value is None:
        return None
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, dict):
        raw_items = [value]
    else:
        raise ValueError("Skill tools must be a list of objects")

    normalized: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Skill tools must be a list of objects")
        normalized.append(SkillToolSpec.model_validate(raw).model_dump(mode="json"))
    return normalized


def normalize_prompt_templates(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("prompt_templates must be an object")
    return {str(key): str(item) for key, item in value.items()}


def normalize_secret_placeholders(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("secret_placeholders must be a list of strings")
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_skill_asset_filename(filename: str) -> str:
    candidate = filename.replace("\\", "/").strip()
    if not candidate:
        raise ValueError("Skill asset filename is required")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."}:
        raise ValueError("Skill asset filename is invalid")
    if normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError("Skill asset filename must stay within the skill package")
    if "/../" in f"/{normalized}":
        raise ValueError("Skill asset filename must stay within the skill package")
    return normalized[2:] if normalized.startswith("./") else normalized


def is_text_like_skill_asset(filename: str, content_type: str) -> bool:
    lower_type = content_type.split(";", 1)[0].strip().lower()
    if lower_type.startswith("text/"):
        return True
    if lower_type in _TEXT_ASSET_CONTENT_TYPES:
        return True
    guessed = mimetypes.guess_type(filename)[0]
    if guessed and guessed.startswith("text/"):
        return True
    suffix = mimetypes.guess_extension(lower_type) or ""
    if suffix in _TEXT_ASSET_EXTENSIONS:
        return True
    filename_lower = filename.lower()
    return any(filename_lower.endswith(ext) for ext in _TEXT_ASSET_EXTENSIONS)


def build_asset_manifest_from_rows(rows: list[Any]) -> list[SkillAssetRef]:
    return [
        SkillAssetRef(
            filename=row.filename,
            asset_id=row.asset_id,
            artifact_namespace=row.artifact_namespace,
            artifact_object_id=row.artifact_object_id,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            content_type=row.content_type,
        )
        for row in rows
    ]


async def load_skill_asset_refs(
    session: Any,
    version_row: Any,
    *,
    artifact_store: Any | None = None,
    ttl_seconds: int | None = None,
) -> list[SkillAssetRef]:
    rows = await list_skill_assets(session, version_row.version_id)
    if rows:
        refs = build_asset_manifest_from_rows(rows)
    else:
        refs = [
            SkillAssetRef.model_validate(item)
            for item in (version_row.asset_manifest or [])
            if isinstance(item, dict)
        ]
    if artifact_store is None:
        return refs
    hydrated: list[SkillAssetRef] = []
    for ref in refs:
        data = ref.model_dump(mode="json")
        try:
            data["url"] = await artifact_store.async_get_public_url(
                ref.artifact_namespace,
                ref.artifact_object_id,
                ref.filename,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            data["url"] = None
        hydrated.append(SkillAssetRef.model_validate(data))
    return hydrated


async def resolve_current_skill_version(session: Any, skill_row: Any) -> Any | None:
    if not skill_row.current_version_id:
        return None
    return await get_skill_version(session, skill_row.current_version_id)


async def prepare_skill_assets(
    session: Any,
    artifact_store: Any,
    *,
    owner_email: str,
    skill_id: str,
    assets: list[dict[str, Any]] | None,
    allow_binary: bool,
) -> list[PreparedSkillAsset]:
    if not assets:
        return []

    prepared: list[PreparedSkillAsset] = []
    seen_filenames: set[str] = set()
    for raw in assets:
        if not isinstance(raw, dict):
            raise ValueError("Skill assets must be a list of objects")
        filename = normalize_skill_asset_filename(str(raw.get("filename") or ""))
        if filename in seen_filenames:
            raise ValueError(f"Duplicate skill asset filename: {filename}")
        seen_filenames.add(filename)

        existing_asset_id = str(raw.get("existing_asset_id") or "").strip()
        source_artifact_id = str(raw.get("source_artifact_id") or "").strip()
        inline_content = raw.get("content")
        inline_b64 = raw.get("content_b64")
        provided = [
            bool(existing_asset_id),
            bool(source_artifact_id),
            inline_content is not None,
            inline_b64 is not None,
        ]
        if sum(1 for item in provided if item) != 1:
            raise ValueError(
                "Each skill asset must specify exactly one of existing_asset_id, "
                "source_artifact_id, content, or content_b64"
            )

        if existing_asset_id:
            from cognis.store.queries import get_skill_asset

            asset_row = await get_skill_asset(session, existing_asset_id)
            if asset_row is None:
                raise ValueError(f"Skill asset '{existing_asset_id}' not found")
            version_row = await get_skill_version(session, asset_row.skill_version_id)
            if version_row is None or version_row.skill_id != skill_id:
                raise ValueError("existing_asset_id must belong to the same skill")
            if not allow_binary and not is_text_like_skill_asset(
                filename, asset_row.content_type
            ):
                raise ValueError(
                    f"Agent-managed skill assets must be text or script files: {filename}"
                )
            prepared.append(
                PreparedSkillAsset(
                    filename=filename,
                    asset_id=artifact_store.generate_id("sa"),
                    artifact_namespace=asset_row.artifact_namespace,
                    artifact_object_id=asset_row.artifact_object_id,
                    content_hash=asset_row.content_hash,
                    size_bytes=asset_row.size_bytes,
                    content_type=asset_row.content_type,
                )
            )
            continue

        if source_artifact_id:
            record = await get_artifact_record(session, source_artifact_id)
            if record is None:
                raise ValueError(f"Artifact '{source_artifact_id}' not found")
            if record.owner_email not in {None, owner_email}:
                raise ValueError("Artifact belongs to a different user")
            content, resolved_type = await artifact_store.async_load(
                record.namespace,
                record.object_id,
                record.filename,
            )
            content_type = str(raw.get("content_type") or record.mime_type or resolved_type)
        else:
            if inline_content is not None:
                content = str(inline_content).encode("utf-8")
            else:
                try:
                    content = base64.b64decode(str(inline_b64), validate=True)
                except Exception as exc:
                    raise ValueError("content_b64 must be valid base64") from exc
            content_type = str(raw.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")

        if not allow_binary and not is_text_like_skill_asset(filename, content_type):
            raise ValueError(
                f"Agent-managed skill assets must be text or script files: {filename}"
            )

        prepared.append(
            PreparedSkillAsset(
                filename=filename,
                asset_id=artifact_store.generate_id("sa"),
                artifact_namespace="skills",
                artifact_object_id=artifact_store.generate_id("ska"),
                content_hash=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                content_type=content_type,
                content=content,
            )
        )
    return prepared


async def create_skill_version_with_assets(
    session: Any,
    artifact_store: Any,
    *,
    skill_id: str,
    version_number: int,
    owner_email: str,
    instructions: str,
    tools: list[dict[str, Any]] | None,
    prompt_templates: dict[str, Any] | None,
    secret_placeholders: list[str] | None,
    assets: list[dict[str, Any]] | None,
    allow_binary_assets: bool,
    source_url: str | None = None,
    resolved_url: str | None = None,
    commit_sha: str | None = None,
    import_checksum: str | None = None,
    imported_at: Any | None = None,
    import_format: str | None = None,
    schema_version: int = 1,
) -> Any:
    normalized_tools = normalize_skill_tools(tools)
    normalized_templates = normalize_prompt_templates(prompt_templates)
    normalized_placeholders = normalize_secret_placeholders(secret_placeholders)
    prepared_assets = await prepare_skill_assets(
        session,
        artifact_store,
        owner_email=owner_email,
        skill_id=skill_id,
        assets=assets,
        allow_binary=allow_binary_assets,
    )

    for asset in prepared_assets:
        if asset.content is None:
            continue
        await artifact_store.async_save(
            asset.artifact_namespace,
            asset.artifact_object_id,
            asset.filename,
            asset.content,
            asset.content_type,
            owner_email=owner_email,
        )

    asset_manifest = [
        SkillAssetRef(
            filename=asset.filename,
            asset_id=asset.asset_id,
            artifact_namespace=asset.artifact_namespace,
            artifact_object_id=asset.artifact_object_id,
            content_hash=asset.content_hash,
            size_bytes=asset.size_bytes,
            content_type=asset.content_type,
        ).model_dump(mode="json", exclude_none=True)
        for asset in prepared_assets
    ]

    content_hash = compute_content_hash(
        instructions,
        normalized_tools,
        normalized_templates,
        normalized_placeholders,
        asset_manifest,
    )
    version_row = await create_skill_version(
        session,
        skill_id=skill_id,
        version_number=version_number,
        content_hash=content_hash,
        instructions=instructions,
        tools=normalized_tools,
        prompt_templates=normalized_templates,
        secret_placeholders=normalized_placeholders,
        source_url=source_url,
        resolved_url=resolved_url,
        commit_sha=commit_sha,
        import_checksum=import_checksum,
        imported_at=imported_at,
        import_format=import_format,
        asset_manifest=asset_manifest,
        schema_version=schema_version,
    )
    for asset in prepared_assets:
        await create_skill_asset(
            session,
            asset_id=asset.asset_id,
            skill_version_id=version_row.version_id,
            filename=asset.filename,
            artifact_namespace=asset.artifact_namespace,
            artifact_object_id=asset.artifact_object_id,
            content_hash=asset.content_hash,
            size_bytes=asset.size_bytes,
            content_type=asset.content_type,
        )
    return version_row


def asset_refs_to_inputs(refs: list[SkillAssetRef]) -> list[dict[str, Any]]:
    return [
        {
            "existing_asset_id": ref.asset_id,
            "filename": ref.filename,
        }
        for ref in refs
    ]


async def load_export_assets(
    session: Any,
    artifact_store: Any,
    version_row: Any,
) -> tuple[list[SkillAssetRef], dict[str, bytes]]:
    refs = await load_skill_asset_refs(session, version_row)
    assets: dict[str, bytes] = {}
    for ref in refs:
        content, _content_type = await artifact_store.async_load(
            ref.artifact_namespace,
            ref.artifact_object_id,
            ref.filename,
        )
        assets[ref.filename] = content
    return refs, assets


def export_cognis_package(data: SkillExportData, assets: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill.yaml", export_cognis_yaml(data))
        for filename, content in assets.items():
            asset_path = f"assets/{normalize_skill_asset_filename(filename)}"
            archive.writestr(asset_path, content)
    return buffer.getvalue()


def parse_cognis_package(content: bytes) -> tuple[dict[str, Any], str | None]:
    from cognis.tools.skill_parser import parse_cognis_yaml

    with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
        skill_member = "skill.yaml" if "skill.yaml" in archive.namelist() else "skill.yml"
        if skill_member not in archive.namelist():
            raise ValueError("Skill package must include skill.yaml")
        skill_data = parse_cognis_yaml(archive.read(skill_member).decode("utf-8"))
        assets: list[dict[str, Any]] = []
        manifest = skill_data.get("asset_manifest") or []
        if manifest:
            for raw in manifest:
                ref = SkillAssetRef.model_validate(raw)
                asset_member = f"assets/{normalize_skill_asset_filename(ref.filename)}"
                if asset_member not in archive.namelist():
                    raise ValueError(f"Skill package is missing asset: {ref.filename}")
                assets.append(
                    {
                        "filename": ref.filename,
                        "content_b64": base64.b64encode(archive.read(asset_member)).decode("ascii"),
                        "content_type": ref.content_type,
                    }
                )
        else:
            for member in archive.namelist():
                if not member.startswith("assets/") or member.endswith("/"):
                    continue
                filename = normalize_skill_asset_filename(member[len("assets/") :])
                assets.append(
                    {
                        "filename": filename,
                        "content_b64": base64.b64encode(archive.read(member)).decode("ascii"),
                        "content_type": mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                    }
                )
        skill_data["assets"] = assets
        return skill_data, skill_member
