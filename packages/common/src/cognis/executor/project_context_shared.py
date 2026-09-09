"""Compatibility exports for the shared project-context DTOs."""

from cognis.core.project_context import (
    PROJECT_CONTEXT_STATUS_LOADED,
    PROJECT_CONTEXT_STATUS_MISSING,
    PROJECT_INSTRUCTIONS_DYNAMIC_SOURCE,
    PROJECT_METADATA_DYNAMIC_SOURCE,
    ProjectContextEntry,
    ProjectMetadataEntry,
    build_project_instruction_message,
    normalize_project_path,
    project_context_event_data,
    project_context_from_event_data,
    project_instruction_hash,
    project_metadata_event_data,
    project_metadata_from_event_data,
    project_metadata_hash,
)

__all__ = [
    "PROJECT_CONTEXT_STATUS_LOADED",
    "PROJECT_CONTEXT_STATUS_MISSING",
    "PROJECT_INSTRUCTIONS_DYNAMIC_SOURCE",
    "PROJECT_METADATA_DYNAMIC_SOURCE",
    "ProjectContextEntry",
    "ProjectMetadataEntry",
    "build_project_instruction_message",
    "normalize_project_path",
    "project_context_event_data",
    "project_context_from_event_data",
    "project_instruction_hash",
    "project_metadata_event_data",
    "project_metadata_from_event_data",
    "project_metadata_hash",
]
