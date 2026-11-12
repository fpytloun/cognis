"""Reference-only attachment projection for derived deterministic workflow state."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from cognis.core.tool_output_presentation import is_safe_lazy_artifact_ref
from cognis.core.workflow_rendering import WorkflowRenderError

_REFERENCE_FIELDS = {
    "artifact_id",
    "kind",
    "mime_type",
    "filename",
    "size_bytes",
    "lineage_id",
    "revision",
    "project_id",
    "lazy_artifact_ref",
}
_TRANSFER_FIELDS = {"transfer_receipt", "binary_transfer_grant", "source_artifact_download"}


def project_workflow_attachments(value: object) -> list[dict[str, Any]]:
    """Project references without hydrating, authorizing, signing or mutating tool evidence.

    Native IDs remain references, not access grants; their consumers still resolve
    them through the normal authorization boundary. URL-only and lazy references
    retain their existing identity. Inline bodies require a materialized ID.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowRenderError("workflow attachments must be a list")
    projected: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise WorkflowRenderError("workflow attachment must be a reference object")
        if _TRANSFER_FIELDS.intersection(item):
            raise WorkflowRenderError("workflow attachment has an unmaterialized binary transfer")
        artifact_id = item.get("artifact_id")
        lazy_ref = item.get("lazy_artifact_ref")
        if artifact_id is not None and (
            not isinstance(artifact_id, str) or not artifact_id.strip()
        ):
            raise WorkflowRenderError("workflow attachment has an invalid artifact reference")
        if (
            isinstance(artifact_id, str)
            and artifact_id.startswith("tool_artifact:")
            and not is_safe_lazy_artifact_ref(artifact_id)
        ):
            raise WorkflowRenderError("workflow attachment has an invalid lazy reference")
        if lazy_ref is not None and not is_safe_lazy_artifact_ref(lazy_ref):
            raise WorkflowRenderError("workflow attachment has an invalid lazy reference")
        if "content_b64" in item and (not artifact_id or artifact_id.startswith("tool_artifact:")):
            raise WorkflowRenderError("workflow attachment has an unmaterialized binary body")
        reference = {key: item[key] for key in _REFERENCE_FIELDS if key in item}
        for key, field in reference.items():
            if field is None:
                continue
            if key in {"size_bytes", "revision"}:
                if type(field) is not int or field < 0:
                    raise WorkflowRenderError("workflow attachment has invalid numeric metadata")
            elif not isinstance(field, str):
                raise WorkflowRenderError("workflow attachment has invalid reference metadata")
        if not artifact_id and not lazy_ref:
            url = item.get("url")
            if not isinstance(url, str) or not url:
                raise WorkflowRenderError("workflow attachment has no usable reference")
            # Preserve existing URL-only attachments, but never treat inline data
            # or executor-local paths as a substitute for a materialized artifact.
            parsed = urlsplit(url)
            if not (
                (parsed.scheme in {"http", "https"} and parsed.netloc)
                or (url.startswith("/") and not url.startswith("//"))
            ):
                raise WorkflowRenderError("workflow attachment has an invalid URL reference")
            reference["url"] = url
        # Artifact-backed attachments do not need an expiring signed URL in DB
        # state. Consumers can request one through the authorized artifact tools.
        projected.append(reference)
    return projected
