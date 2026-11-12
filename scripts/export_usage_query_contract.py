"""Export the canonical usage API/tool contracts for frontend integration."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pydantic

from cognis.core.usage import query_models
from cognis.models import usage as shared_usage

ROOT = Path(__file__).resolve().parents[1]
MODEL_SOURCES = (
    "cognis/core/usage/query_models.py",
    "packages/common/src/cognis/models/usage.py",
)
SPEC_SOURCE = "docs/specs/usage-query-api.md"

_REQUESTS = (
    "UsageQuery",
    "UsageAttemptQuery",
    "UsageSessionQuery",
    "UsageCatalogRequest",
    "UsageToolArguments",
    "UsageSummaryRequest",
    "UsageFacetRequest",
    "ProviderQuotaHistoryQuery",
)
_RESPONSES = (
    "UsageQueryResponse",
    "UsageAttemptPage",
    "UsageRequestPage",
    "UsageSessionPage",
    "UsageCatalog",
    "UsageSummaryResponse",
    "ProviderQuotaHistory",
    "UsageFacetResponse",
)


def contract_bytes() -> bytes:
    """Generate request-validation and response-serialization schemas unchanged."""
    contracts = {
        name: getattr(query_models, name).model_json_schema(mode=mode)
        for names, mode in ((_REQUESTS, "validation"), (_RESPONSES, "serialization"))
        for name in names
    }
    return _json_bytes(contracts)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _git(*arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def bundle_bytes(source_commit: str) -> dict[str, bytes]:
    """Bind a fresh export to verified committed model/spec bytes and its generator."""
    commit = _git("rev-parse", "--verify", f"{source_commit}^{{commit}}").decode().strip()
    tree = _git("rev-parse", f"{commit}^{{tree}}").decode().strip()
    for module, relative in zip((query_models, shared_usage), MODEL_SOURCES, strict=True):
        if Path(module.__file__).resolve() != ROOT / relative:
            raise RuntimeError(f"Model import is outside the exporting checkout: {relative}")

    sources = {}
    for relative in (*MODEL_SOURCES, SPEC_SOURCE):
        committed = _git("show", f"{commit}:{relative}")
        if (ROOT / relative).read_bytes() != committed:
            raise RuntimeError(f"Checkout does not match committed source: {relative}")
        sources[relative] = {
            "git_blob": _git("rev-parse", f"{commit}:{relative}").decode().strip(),
            "sha256": hashlib.sha256(committed).hexdigest(),
        }

    files = {
        "usage-query-contract.json": contract_bytes(),
        "usage-query-api.md": (ROOT / SPEC_SOURCE).read_bytes(),
    }
    manifest = {
        "format_version": 1,
        "model_source_commit": commit,
        "model_source_tree": tree,
        "sources": sources,
        "generator": {
            "path": "scripts/export_usage_query_contract.py",
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "pydantic_version": pydantic.__version__,
            "request_mode": "validation",
            "response_mode": "serialization",
        },
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }
    files["provenance.json"] = _json_bytes(manifest)
    return files


def current_pin_bytes(files: dict[str, bytes]) -> bytes:
    """Generate the single current-export pointer and all frozen byte hashes."""
    manifest = json.loads(files["provenance.json"])
    return _json_bytes(
        {
            "format_version": 1,
            "directory": manifest["model_source_commit"][:12],
            "sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        }
    )


def main() -> None:
    """Write JSON Schema without importing persistence or constructing an app."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--source-commit",
        help="Also export the committed spec and provenance beside usage-query-contract.json",
    )
    parser.add_argument(
        "--publish-current",
        action="store_true",
        help="Write current.json hash pins above the source-commit-named bundle directory",
    )
    args = parser.parse_args()
    if args.publish_current and not args.source_commit:
        parser.error("--publish-current requires --source-commit")
    if args.source_commit:
        if args.output.name != "usage-query-contract.json":
            parser.error("A provenance bundle requires the filename usage-query-contract.json")
        files = bundle_bytes(args.source_commit)
    else:
        files = {args.output.name: contract_bytes()}
    if args.publish_current:
        source = json.loads(files["provenance.json"])["model_source_commit"]
        if args.output.parent.name != source[:12]:
            parser.error(
                "The current bundle directory must be the first 12 source-commit characters"
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (args.output.parent / name).write_bytes(raw)
    if args.publish_current:
        (args.output.parent.parent / "current.json").write_bytes(current_pin_bytes(files))


if __name__ == "__main__":
    main()
