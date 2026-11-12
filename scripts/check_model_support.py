"""Credential-free preflight for publicly documented Claude and GPT model support.

Exit codes: 0 verified unchanged, 2 relevant change, 1 incomplete check.
The checked-in public-doc baseline is intentionally separate from the Codex
catalog, which is compared directly to Cognis' bundled catalog.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "scripts/model_support_baseline.json"
CATALOG = "cognis/providers/llm/data/codex_models.json"
SOURCES = {
    "claude": "https://platform.claude.com/docs/en/models/overview.md",
    "openai": "https://developers.openai.com/api/docs/models.md",
    "openai_catalog": "https://developers.openai.com/api/docs/models/all.md",
    "codex": "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json",
}
OPENAI_MODEL_ID = r"gpt-(?:\d|image-\d)[\w.-]*"
CLAUDE_FIELDS = {
    "Claude API ID": "id",
    "Pricing": "pricing",
    "Thinking": "thinking",
    "Default effort": "default_effort",
    "Context window": "context_window",
    "Max output": "max_output",
}
CODEX_FIELDS = (
    "slug",
    "display_name",
    "visibility",
    "supported_in_api",
    "priority",
    "minimal_client_version",
    "context_window",
    "max_context_window",
    "max_output_tokens",
    "input_modalities",
    "supports_pdf_input",
    "supports_file_input",
    "supports_search_tool",
    "available_in_plans",
    "service_tiers",
    "supported_reasoning_levels",
    "default_reasoning_level",
    "reasoning_summary_format",
    "default_reasoning_summary",
    "support_verbosity",
    "default_verbosity",
    "apply_patch_tool_type",
    "web_search_tool_type",
    "tool_mode",
    "supports_parallel_tool_calls",
    "shell_type",
)


def claude_models(markdown: str) -> dict[str, dict[str, str]]:
    """Extract supported model IDs and stable capability fields from the comparison table."""
    rows: dict[str, list[str]] = {}
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cells[0])
        if label in CLAUDE_FIELDS:
            rows[label] = cells[1:]
    ids = rows.get("Claude API ID", [])
    if not ids or any(not re.fullmatch(r"`claude-[a-z0-9-]+`", value) for value in ids):
        raise ValueError("Claude API ID comparison row missing or malformed")
    if any(len(rows.get(field, [])) != len(ids) for field in CLAUDE_FIELDS):
        raise ValueError("Claude comparison table is incomplete")
    result: dict[str, dict[str, str]] = {}
    for index, value in enumerate(ids):
        model_id = value.strip("`")
        if model_id in result:
            raise ValueError(f"duplicate Claude API ID: {model_id}")
        result[model_id] = {
            key: " ".join(rows[field][index].split())
            for field, key in CLAUDE_FIELDS.items()
            if key != "id"
        }
    return result


def openai_models(markdown: str) -> list[str]:
    """Extract numbered GPT and GPT Image API IDs."""
    models = set(
        re.findall(rf"\]\(/api/docs/models/({OPENAI_MODEL_ID})\.md\)", markdown, re.IGNORECASE)
    )
    if not models:
        raise ValueError("OpenAI GPT model links missing")
    return sorted(models)


def openai_catalog(markdown: str) -> dict[str, str]:
    """Extract monitored GPT IDs and explicit lifecycle labels from the full catalog."""
    if not markdown.startswith("# All models\n"):
        raise ValueError("OpenAI full catalog heading missing")
    sections = re.split(r"(?m)^## (.+)\n", markdown)
    if len(sections) < 3:
        raise ValueError("OpenAI full catalog sections missing")
    by_name = dict(zip(sections[1::2], sections[2::2], strict=True))
    if len(by_name) != len(sections[1::2]):
        raise ValueError("OpenAI full catalog has duplicate sections")
    if not {"Flagship models", "More models", "Deprecated models"} <= by_name.keys():
        raise ValueError("OpenAI full catalog sections incomplete")
    for name in ("Flagship models", "More models", "Deprecated models"):
        if not re.search(r"\]\(/api/docs/models/gpt-\d", by_name[name], re.IGNORECASE):
            raise ValueError(f"OpenAI full catalog section empty: {name}")
    result: dict[str, str] = {}
    pattern = re.compile(
        rf"^-\s+\[[^\]]+\]\(/api/docs/models/({OPENAI_MODEL_ID})\.md\):\s*(.*)$",
        re.IGNORECASE,
    )
    for name, section in by_name.items():
        for line in section.splitlines():
            match = pattern.match(line)
            if not match:
                if re.search(rf"\]\(/api/docs/models/{OPENAI_MODEL_ID}", line, re.IGNORECASE):
                    raise ValueError("OpenAI full catalog model entry malformed")
                continue
            model_id, description = match.groups()
            if name == "Deprecated models":
                if description.startswith("Shut down."):
                    status = "shutdown"
                elif description.startswith("Deprecated."):
                    status = "deprecated"
                else:
                    raise ValueError(f"OpenAI lifecycle label missing: {model_id}")
            else:
                status = "listed"
            if model_id in result and result[model_id] != status:
                raise ValueError(f"conflicting OpenAI catalog status: {model_id}")
            result[model_id] = status
    return dict(sorted(result.items()))


def current_gpt(model_id: str) -> bool:
    """Check full details for current GPT families; older IDs still get monitored."""
    if model_id.startswith("gpt-image-"):
        match = re.match(r"^gpt-image-(\d+)(?:\.(\d+))?(?:-|$)", model_id)
        return bool(match and (int(match[1]), int(match[2] or 0)) >= (2, 6))
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(?:-|$)", model_id)
    if not match:
        return False
    return (int(match[1]), int(match[2] or 0)) >= (5, 6)


def openai_model_details(markdown: str, model_id: str) -> dict[str, Any]:
    """Extract support-relevant attributes from an official per-model page."""
    if f"Model ID: `{model_id}`" not in markdown:
        raise ValueError(f"OpenAI model page ID mismatch: {model_id}")
    model_details = markdown.partition("## Model details\n")[2].partition("\n## ")[0]
    if not model_details:
        raise ValueError(f"OpenAI model details missing: {model_id}")

    def section(name: str) -> str:
        return markdown.partition(f"## {name}\n")[2].partition("\n## ")[0]

    if model_id.startswith("gpt-image-"):
        modalities = {}
        for direction in ("Input", "Output"):
            match = re.search(rf"^- {direction} modalities: (.+)$", model_details, re.MULTILINE)
            if match is None:
                raise ValueError(f"OpenAI image modalities missing: {model_id}")
            modalities[f"{direction.lower()}_modalities"] = match[1]
        image_endpoints = dict(
            re.findall(
                r"^\| [^|]+ \| `(v1/[^`]+)` \| (Supported|Not supported) \|",
                section("Endpoints"),
                re.MULTILINE,
            )
        )
        if (
            not {"v1/images/generations", "v1/images/edits", "v1/responses"}
            <= image_endpoints.keys()
        ):
            raise ValueError(f"OpenAI image endpoints incomplete: {model_id}")
        image_pricing = {}
        for kind, labels in (
            ("Text tokens", ("Input", "Cached input")),
            ("Image tokens", ("Input", "Cached input", "Output")),
        ):
            table = section("Pricing").partition(f"### {kind}\n")[2].partition("\n### ")[0]
            prices = {
                label.lower().replace(" ", "_"): match[1]
                for label in labels
                if (
                    match := re.search(
                        rf"^\| {label} \| (\$[\d.]+) \| 1M tokens \|", table, re.MULTILINE
                    )
                )
            }
            if len(prices) != len(labels):
                raise ValueError(f"OpenAI image pricing incomplete: {model_id}")
            image_pricing[kind.lower().replace(" ", "_")] = prices
        return {
            **modalities,
            "endpoints": {
                route: status == "Supported" for route, status in image_endpoints.items()
            },
            "pricing": image_pricing,
        }

    def count(pattern: str) -> int:
        match = re.search(pattern, model_details, re.IGNORECASE | re.MULTILINE)
        if match is None:
            raise ValueError(f"OpenAI token limit missing: {model_id}")
        return int(match[1].replace(",", ""))

    def bullets(name: str) -> list[str]:
        return sorted(
            line.removeprefix("- ").strip()
            for line in section(name).splitlines()
            if line.startswith("- ") and re.fullmatch(r"[\w_-]+", line[2:].strip())
        )

    pricing = section("Pricing")
    prices = {
        label.lower().replace(" ", "_"): match[1]
        for label in ("Input", "Cached input", "Output")
        if (match := re.search(rf"^\| {label} \| ([\$\d.]+) \|", pricing, re.MULTILINE))
    }
    efforts = re.search(
        r"^`reasoning\.effort` supports (.*?)(?:\.|\n## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    endpoints = section("Endpoints")
    responses = re.search(
        r"^\| Responses \| `v1/responses` \| (Supported|Not supported) \|",
        endpoints,
        re.MULTILINE,
    )
    if responses is None:
        raise ValueError(f"OpenAI Responses API status missing: {model_id}")
    input_modalities = re.search(r"^- Input modalities: (.+)$", model_details, re.MULTILINE)
    if input_modalities is None:
        raise ValueError(f"OpenAI input modalities missing: {model_id}")
    details = {
        "context_window": count(r"^- ([\d,]+) context window$"),
        "max_input_tokens": count(r"^- Maximum input tokens: ([\d,]+)$"),
        "max_output_tokens": count(r"^- ([\d,]+) max output tokens$"),
        "input_modalities": input_modalities[1],
        "responses_api": responses[1] == "Supported",
        "pricing": prices,
        "reasoning_efforts": re.findall(r"`([\w]+)`", efforts[1]) if efforts else [],
        "features": bullets("Supported features"),
        "tools": bullets("Supported tools"),
    }
    if model_id == "gpt-6-cyber":
        notice = markdown.partition(f"Model ID: `{model_id}`")[2].partition("## Model details\n")[0]
        details["access_requirements"] = " ".join(notice.split())
    return details


def codex_models(payload: Any) -> dict[str, dict[str, Any]]:
    """Project only support-relevant upstream fields; ignore large prompt templates."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Codex models catalog missing")
    result: dict[str, dict[str, Any]] = {}
    for item in payload["models"]:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            raise ValueError("Codex model entry malformed")
        slug = item["slug"]
        if slug in result:
            raise ValueError(f"duplicate Codex slug: {slug}")
        result[slug] = {key: item.get(key) for key in CODEX_FIELDS if key != "slug"}
        levels = item.get("supported_reasoning_levels", [])
        if not isinstance(levels, list) or any(
            not isinstance(level, dict) or not isinstance(level.get("effort"), str)
            for level in levels
        ):
            raise ValueError(f"Codex reasoning levels malformed: {slug}")
        result[slug]["supported_reasoning_levels"] = [level["effort"] for level in levels]
    if not result:
        raise ValueError("Codex models catalog empty")
    return result


def difference(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    """Summarize changed keys without returning source documents to the agent."""
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(key for key in new.keys() & old.keys() if new[key] != old[key]),
    }


def read_from_ref(root: Path, ref: str, path: str) -> str:
    """Read repository data at a fetched Git revision without changing the checkout."""
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout


async def fetch_sources() -> dict[str, str]:
    """Fetch only public, allowlisted endpoints; never send provider credentials."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:

        async def fetch(url: str) -> str:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 8_000_000:
                raise ValueError(f"model source too large: {url}")
            return response.text

        pages = await asyncio.gather(*(fetch(url) for url in SOURCES.values()))
    return dict(zip(SOURCES, pages, strict=True))


async def fetch_openai_details(model_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch full official metadata for recent GPT models only, concurrently."""
    current = [model_id for model_id in model_ids if current_gpt(model_id)]
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:

        async def fetch(model_id: str) -> tuple[str, dict[str, Any]]:
            response = await client.get(
                f"https://developers.openai.com/api/docs/models/{model_id}.md"
            )
            response.raise_for_status()
            if len(response.content) > 500_000:
                raise ValueError(f"OpenAI model page too large: {model_id}")
            return model_id, openai_model_details(response.text, model_id)

        return dict(await asyncio.gather(*(fetch(model_id) for model_id in current)))


async def check(
    root: Path, ref: str | None = None, *, bootstrap: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compare live public sources with the repository's authoritative baseline."""

    def repo_text(path: str) -> str:
        return read_from_ref(root, ref, path) if ref else (root / path).read_text()

    try:
        old_docs: dict[str, Any] = json.loads(repo_text(BASELINE))
    except FileNotFoundError:
        if not bootstrap:
            raise
        old_docs = {"schema_version": 3, "claude": {}, "openai": [], "openai_details": {}}
    if old_docs.get("schema_version") not in ({1, 2, 3} if bootstrap else {2, 3}):
        raise ValueError("unsupported model preflight baseline")
    if not isinstance(old_docs.get("claude"), dict) or not isinstance(old_docs.get("openai"), list):
        raise ValueError("model preflight baseline is malformed")
    old_catalog = codex_models(json.loads(repo_text(CATALOG)))
    pages = await fetch_sources()
    index_ids = set(openai_models(pages["openai"]))
    catalog = openai_catalog(pages["openai_catalog"])
    missing = (set(old_docs["openai"]) | index_ids) - catalog.keys()
    if missing:
        raise ValueError(f"OpenAI full catalog missing tracked or indexed IDs: {sorted(missing)}")
    openai_ids = sorted(catalog)
    old_status = old_docs.get("openai_status", {})
    if not isinstance(old_status, dict) or any(
        key not in old_docs["openai"] or value not in {"listed", "deprecated", "shutdown"}
        for key, value in old_status.items()
    ):
        raise ValueError("OpenAI baseline statuses malformed")
    if old_docs["schema_version"] == 3 and set(old_status) != set(old_docs["openai"]):
        raise ValueError("OpenAI baseline statuses incomplete")
    live_docs: dict[str, Any] = {
        "schema_version": 3,
        "claude": claude_models(pages["claude"]),
        "openai": openai_ids,
        "openai_status": catalog,
        "openai_details": await fetch_openai_details(openai_ids),
    }
    live_catalog = codex_models(json.loads(pages["codex"]))
    diff = {
        "claude": difference(old_docs["claude"], live_docs["claude"]),
        "openai": difference(
            {key: True for key in old_docs["openai"]},
            {key: True for key in live_docs["openai"]},
        ),
        "openai_details": difference(
            old_docs.get("openai_details", {}),
            live_docs["openai_details"],
        ),
        "openai_status": difference(old_status, catalog),
        "codex": difference(old_catalog, live_catalog),
    }
    changed = any(values for category in diff.values() for values in category.values())
    return {
        "status": "change" if changed else "no_change",
        "update_required": changed,
        "check_complete": True,
        "differences": diff,
        "openai_missing_from_index": sorted(catalog.keys() - index_ids),
        "openai_catalog_status": catalog,
        "sources": SOURCES,
        "baseline_ref": ref or "working_tree",
    }, live_docs


def main() -> int:
    """Run from any directory in a Cognis checkout with machine-readable output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Cognis checkout to inspect")
    parser.add_argument(
        "--fetch-origin",
        action="store_true",
        help="Fetch origin/main and compare against that revision, leaving the checkout unchanged",
    )
    parser.add_argument(
        "--refresh-baseline",
        action="store_true",
        help="Explicitly write the public-doc baseline in the local checkout after a full check",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.fetch_origin:
            subprocess.run(
                ["git", "-C", str(root), "fetch", "origin", "main"],
                capture_output=True,
                text=True,
                check=True,
                timeout=45,
            )
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "origin/main"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
        else:
            revision = None
        if args.refresh_baseline and args.fetch_origin:
            parser.error("--refresh-baseline cannot modify a checkout compared to origin/main")
        result, live_docs = asyncio.run(check(root, revision, bootstrap=args.refresh_baseline))
        if revision is not None:
            result["baseline_revision"] = revision
        if args.refresh_baseline:
            (root / BASELINE).write_text(json.dumps(live_docs, indent=2) + "\n")
            result["baseline_ref"] = "working_tree_refreshed"
        print(json.dumps(result, sort_keys=True))
        return 2 if result["update_required"] else 0
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "incomplete",
                    "check_complete": False,
                    "update_required": True,
                    "error": str(exc),
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
