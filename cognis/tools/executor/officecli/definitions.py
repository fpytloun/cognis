"""Curated Office document tool definitions backed by OfficeCLI."""

from __future__ import annotations

from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolCapability, ToolSource

_SOURCE = ToolSource(type="executor")
_OFFICE_FORMATS = ["docx", "xlsx", "pptx"]
_VIEWS = ["outline", "stats", "issues", "text", "annotated", "html", "json"]
_RENDERS = ["html", "screenshot", "svg", "pdf", "forms"]


def _common_source() -> dict[str, object]:
    return {
        "source_path": {
            "type": "string",
            "description": "Local executor Office file path. Provide exactly one of source_path or source_artifact_id.",
        },
        "source_artifact_id": {
            "type": "string",
            "description": "Cognis artifact id or deliverable id containing an Office file. Provide exactly one of source_path or source_artifact_id.",
        },
        "expected_sha256": {
            "type": "string",
            "description": "Optional SHA256 expected for the exact materialized input bytes.",
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 300,
            "description": "Optional OfficeCLI subprocess timeout.",
        },
    }


OFFICE_READ_TOOL = ToolDefinition(
    name="office_read",
    description="Read a DOCX/XLSX/PPTX file using OfficeCLI into text, outline, annotated, stats, issues, HTML, or JSON views.",
    parameters={
        "type": "object",
        "properties": {
            **_common_source(),
            "view": {
                "type": "string",
                "enum": _VIEWS,
                "description": "View mode. Defaults to text.",
            },
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "max_lines": {"type": "integer"},
            "issue_type": {"type": "string", "enum": ["format", "content", "structure"]},
            "limit": {"type": "integer"},
            "page": {"type": "integer"},
        },
    },
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=True,
    capabilities=[ToolCapability.READ],
    timeout_seconds=90,
    max_result_size=200_000,
)

OFFICE_GET_TOOL = ToolDefinition(
    name="office_get",
    description="Retrieve an object/path from a DOCX/XLSX/PPTX file with structured JSON when supported.",
    parameters={
        "type": "object",
        "properties": {
            **_common_source(),
            "object_path": {
                "type": "string",
                "description": "OfficeCLI path, e.g. /body/p[1] or /Sheet1/A1.",
            },
            "depth": {"type": "integer", "minimum": 0, "maximum": 20},
            "json": {"type": "boolean", "description": "Request JSON output. Defaults true."},
        },
        "required": ["object_path"],
    },
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=True,
    capabilities=[ToolCapability.READ],
    timeout_seconds=90,
    max_result_size=200_000,
)

OFFICE_QUERY_TOOL = ToolDefinition(
    name="office_query",
    description="Query/select parts of a DOCX/XLSX/PPTX file using OfficeCLI selectors.",
    parameters={
        "type": "object",
        "properties": {
            **_common_source(),
            "selector": {"type": "string", "description": "OfficeCLI CSS-like selector."},
            "limit": {"type": "integer"},
            "json": {"type": "boolean", "description": "Request JSON output. Defaults true."},
        },
        "required": ["selector"],
    },
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=True,
    capabilities=[ToolCapability.READ],
    timeout_seconds=90,
    max_result_size=200_000,
)

OFFICE_VALIDATE_TOOL = ToolDefinition(
    name="office_validate",
    description="Validate Office package/document integrity and return structured errors and warnings where supported.",
    parameters={"type": "object", "properties": _common_source()},
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=True,
    capabilities=[ToolCapability.READ],
    timeout_seconds=90,
    max_result_size=200_000,
)

OFFICE_RENDER_TOOL = ToolDefinition(
    name="office_render",
    description="Render/export OfficeCLI preview outputs such as HTML, screenshots, SVG, PDF, or form JSON as artifacts.",
    parameters={
        "type": "object",
        "properties": {
            **_common_source(),
            "render": {"type": "string", "enum": _RENDERS, "description": "Render/export mode."},
            "output_filename": {"type": "string"},
            "purpose": {"type": "string"},
            "page": {"type": "integer"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        },
        "required": ["render"],
    },
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=True,
    capabilities=[ToolCapability.READ],
    timeout_seconds=180,
    max_result_size=50_000,
)

_OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "enum": ["set", "add", "remove"]},
        "path": {"type": "string"},
        "parent": {"type": "string"},
        "selector": {"type": "string"},
        "type": {"type": "string"},
        "props": {"type": "object", "additionalProperties": True},
        "before": {"type": "string"},
        "after": {"type": "string"},
        "index": {"type": "integer"},
        "from_path": {"type": "string"},
    },
    "required": ["verb"],
}

OFFICE_CREATE_TOOL = ToolDefinition(
    name="office_create",
    description="Create a DOCX/XLSX/PPTX file, optionally applying structured OfficeCLI operations, and return an artifact by default.",
    parameters={
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": _OFFICE_FORMATS},
            "operations": {"type": "array", "items": _OPERATION_SCHEMA},
            "output_path": {"type": "string"},
            "output_filename": {"type": "string"},
            "purpose": {"type": "string"},
            "publish_artifact": {"type": "boolean"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        },
    },
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=False,
    non_bypassable=True,
    capabilities=[ToolCapability.WRITE],
    timeout_seconds=180,
    max_result_size=50_000,
)

OFFICE_PATCH_TOOL = ToolDefinition(
    name="office_patch",
    description="Apply structured OfficeCLI set/add/remove operations to a temp copy by default and return a new artifact.",
    parameters={
        "type": "object",
        "properties": {
            **_common_source(),
            "expected_base_sha256": {
                "type": "string",
                "description": "Optional stale-edit guard checked against exact materialized source bytes.",
            },
            "operations": {"type": "array", "items": _OPERATION_SCHEMA},
            "output_path": {"type": "string"},
            "output_filename": {"type": "string"},
            "purpose": {"type": "string"},
            "publish_artifact": {"type": "boolean"},
            "in_place": {"type": "boolean"},
            "validate": {
                "type": "boolean",
                "description": "Run office_validate on the patched output. Defaults true.",
            },
        },
        "required": ["operations"],
    },
    source=_SOURCE,
    category="office",
    profile_group="office",
    read_only=False,
    non_bypassable=True,
    capabilities=[ToolCapability.READ, ToolCapability.WRITE],
    timeout_seconds=180,
    max_result_size=50_000,
)


def office_tool_definitions() -> list[ToolDefinition]:
    return [
        OFFICE_READ_TOOL,
        OFFICE_GET_TOOL,
        OFFICE_QUERY_TOOL,
        OFFICE_VALIDATE_TOOL,
        OFFICE_RENDER_TOOL,
        OFFICE_CREATE_TOOL,
        OFFICE_PATCH_TOOL,
    ]
