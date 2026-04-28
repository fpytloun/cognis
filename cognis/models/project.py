"""Project domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectStatus(StrEnum):
    """Project lifecycle states."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectSource(BaseModel):
    """Repository/source hint associated with a project."""

    source_id: str
    project_id: str
    name: str
    local_path: str | None = None
    remote_url: str | None = None
    default_branch: str | None = None
    credential_ref: str | None = None
    instructions: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectGrant(BaseModel):
    """User-to-user sharing grant for a project."""

    grant_id: str
    project_id: str
    grantee_type: Literal["user", "group"] = "user"
    grantee_user_email: str | None = None
    grantee_group_id: str | None = None
    permission: Literal["use"] = "use"
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None = None
    note: str | None = None


class Project(BaseModel):
    """First-class project context."""

    project_id: str
    owner_email: str
    name: str
    description: str | None = None
    instructions: str | None = None
    default_workflow_id: str | None = None
    avatar_image_id: str | None = None
    avatar_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: ProjectStatus = ProjectStatus.ACTIVE
    sources: list[ProjectSource] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    grants: list[ProjectGrant] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
