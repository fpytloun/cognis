"""Regression tests for the credential-free model-support preflight."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_model_support as preflight

CLAUDE_TABLE = """\
| Feature | Claude Sol | Claude Luna |
| :--- | :--- | :--- |
| [Pricing](https://example.org) | $3 / input MTok | $1 / input MTok |
| Claude API ID | `claude-sol-6` | `claude-luna-6` |
| Thinking | Adaptive | Extended |
| Default effort | `high` | `medium` |
| Context window | 1M tokens | 200K tokens |
| Max output | 128K tokens | 32K tokens |
"""
OPENAI_INDEX = """\
- [GPT-6 Sol](/api/docs/models/gpt-6-sol.md): model
- [GPT-6 Luna](/api/docs/models/gpt-6-luna.md): model
- [GPT-Image](/api/docs/models/gpt-image-2.md): image
"""
OPENAI_CATALOG = """\
# All models
## Flagship models
- [GPT-6 Sol](/api/docs/models/gpt-6-sol.md): model
## More models
- [GPT-6 Luna](/api/docs/models/gpt-6-luna.md): model
- [GPT-Image](/api/docs/models/gpt-image-2.md): image
## Deprecated models
- [GPT-4.1](/api/docs/models/gpt-4.1.md): Deprecated. Older model
- [GPT-4](/api/docs/models/gpt-4.md): Shut down. Older model
"""
CODEX_CATALOG = {
    "models": [
        {
            "slug": "gpt-6-sol",
            "context_window": 400000,
            "model_messages": {"instructions_template": "long prompt"},
            "supported_reasoning_levels": [{"effort": "high", "description": "High"}],
        }
    ]
}
OPENAI_PAGE = """\
Model ID: `gpt-6-sol`
`reasoning.effort` supports `low`, `medium`, and `high`.
## Model details
- Input modalities: text, image
- 1,000,000 context window
- Maximum input tokens: 872,000
- 128,000 max output tokens
## Pricing
| Input | $2 | 1M tokens |
| Cached input | $0.2 | 1M tokens |
| Output | $10 | 1M tokens |
## Endpoints
| Responses | `v1/responses` | Supported |
## Supported features
- image_input
- streaming
## Supported tools
- apply_patch
- tool_search
"""


def prepare_checkout(root: Path) -> None:
    """Write a minimal checked-in baseline and catalog into a test checkout."""
    (root / "scripts").mkdir()
    (root / "cognis/providers/llm/data").mkdir(parents=True)
    (root / preflight.BASELINE).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "claude": preflight.claude_models(CLAUDE_TABLE),
                "openai": sorted(preflight.openai_catalog(OPENAI_CATALOG)),
                "openai_details": {
                    "gpt-6-sol": preflight.openai_model_details(OPENAI_PAGE, "gpt-6-sol")
                },
            }
        )
    )
    (root / preflight.CATALOG).write_text(json.dumps(CODEX_CATALOG))


def test_extracts_public_ids_and_rejects_incomplete_claude_table() -> None:
    assert sorted(preflight.claude_models(CLAUDE_TABLE)) == ["claude-luna-6", "claude-sol-6"]
    assert preflight.openai_models(OPENAI_INDEX) == ["gpt-6-luna", "gpt-6-sol", "gpt-image-2"]
    assert preflight.openai_catalog(OPENAI_CATALOG) == {
        "gpt-4": "shutdown",
        "gpt-4.1": "deprecated",
        "gpt-6-luna": "listed",
        "gpt-6-sol": "listed",
        "gpt-image-2": "listed",
    }
    with pytest.raises(ValueError, match="incomplete"):
        preflight.claude_models(
            CLAUDE_TABLE.replace("| Max output | 128K tokens | 32K tokens |\n", "")
        )
    with pytest.raises(ValueError, match="missing"):
        preflight.openai_models("No API catalog here")


def test_codex_projection_ignores_instruction_and_reasoning_prose() -> None:
    previous = preflight.codex_models(CODEX_CATALOG)
    changed = json.loads(json.dumps(CODEX_CATALOG))
    changed["models"][0]["model_messages"]["instructions_template"] = "different prompt"
    changed["models"][0]["supported_reasoning_levels"][0]["description"] = "New prose"
    assert previous == preflight.codex_models(changed)
    changed["models"][0]["context_window"] = 1000000
    assert preflight.difference(previous, preflight.codex_models(changed))["changed"] == [
        "gpt-6-sol"
    ]
    changed["models"][0]["context_window"] = 400000
    changed["models"][0]["supports_pdf_input"] = True
    assert preflight.difference(previous, preflight.codex_models(changed))["changed"] == [
        "gpt-6-sol"
    ]


def test_check_noop_and_new_public_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepare_checkout(tmp_path)
    pages = {
        "claude": CLAUDE_TABLE,
        "openai": OPENAI_INDEX,
        "openai_catalog": OPENAI_CATALOG,
        "codex": json.dumps(CODEX_CATALOG),
    }

    async def fake_sources() -> dict[str, str]:
        return pages

    monkeypatch.setattr(preflight, "fetch_sources", fake_sources)

    async def fake_details(model_ids: list[str]) -> dict[str, dict[str, object]]:
        return {"gpt-6-sol": preflight.openai_model_details(OPENAI_PAGE, "gpt-6-sol")}

    monkeypatch.setattr(preflight, "fetch_openai_details", fake_details)
    result, baseline = asyncio.run(preflight.check(tmp_path))
    assert result["status"] == "change"  # Initial schema-2 lifecycle observations.
    assert result["check_complete"] is True
    assert result["differences"]["openai"]["removed"] == []
    assert result["openai_missing_from_index"] == ["gpt-4", "gpt-4.1"]
    assert baseline["openai_status"]["gpt-4"] == "shutdown"
    assert baseline["openai_status"]["gpt-4.1"] == "deprecated"
    (tmp_path / preflight.BASELINE).write_text(json.dumps(baseline))
    stable, refreshed = asyncio.run(preflight.check(tmp_path))
    assert stable["status"] == "no_change"
    assert refreshed == baseline

    pages["openai_catalog"] = OPENAI_CATALOG.replace(
        "Deprecated. Older model", "Shut down. Older model"
    )
    changed_status, _ = asyncio.run(preflight.check(tmp_path))
    assert changed_status["differences"]["openai_status"]["changed"] == ["gpt-4.1"]
    pages["openai_catalog"] = OPENAI_CATALOG

    pages["claude"] = CLAUDE_TABLE.replace(
        "| Claude API ID | `claude-sol-6` | `claude-luna-6` |",
        "| Claude API ID | `claude-sol-7` | `claude-luna-6` |",
    )
    changed, _ = asyncio.run(preflight.check(tmp_path))
    assert changed["status"] == "change"
    assert changed["differences"]["claude"]["added"] == ["claude-sol-7"]
    pages["claude"] = CLAUDE_TABLE

    async def changed_details(model_ids: list[str]) -> dict[str, dict[str, object]]:
        return {
            "gpt-6-sol": preflight.openai_model_details(
                OPENAI_PAGE.replace("| Input | $2 |", "| Input | $3 |"), "gpt-6-sol"
            )
        }

    monkeypatch.setattr(preflight, "fetch_openai_details", changed_details)
    updated, _ = asyncio.run(preflight.check(tmp_path))
    assert updated["differences"]["openai_details"]["changed"] == ["gpt-6-sol"]


def test_check_source_failure_cannot_return_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare_checkout(tmp_path)

    async def failing_sources() -> dict[str, str]:
        raise preflight.httpx.ConnectError("public source unavailable")

    monkeypatch.setattr(preflight, "fetch_sources", failing_sources)
    with pytest.raises(preflight.httpx.ConnectError):
        asyncio.run(preflight.check(tmp_path))


@pytest.mark.parametrize("model_id", ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
def test_image_release_and_endpoint_price_changes_are_not_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model_id: str
) -> None:
    """Exercise discovery, detail fetching and baseline transitions through check()."""
    prepare_checkout(tmp_path)
    image_page = f"""\
Model ID: `{model_id}`
## Model details
- Input modalities: text, image
- Output modalities: image
## Pricing
### Text tokens
| Input | $5 | 1M tokens |
| Cached input | $1.25 | 1M tokens |
### Image tokens
| Input | $8 | 1M tokens |
| Cached input | $2 | 1M tokens |
| Output | $30 | 1M tokens |
## Endpoints
| Responses | `v1/responses` | Not supported |
| Image generation | `v1/images/generations` | Supported |
| Image edit | `v1/images/edits` | Supported |
"""
    pages = {
        preflight.SOURCES["claude"]: CLAUDE_TABLE,
        preflight.SOURCES["openai"]: OPENAI_INDEX,
        preflight.SOURCES["openai_catalog"]: OPENAI_CATALOG,
        preflight.SOURCES["codex"]: json.dumps(CODEX_CATALOG),
        "https://developers.openai.com/api/docs/models/gpt-6-sol.md": OPENAI_PAGE,
        "https://developers.openai.com/api/docs/models/gpt-6-luna.md": OPENAI_PAGE.replace(
            "gpt-6-sol", "gpt-6-luna"
        ),
        f"https://developers.openai.com/api/docs/models/{model_id}.md": image_page,
    }
    client = preflight.httpx.AsyncClient
    transport = preflight.httpx.MockTransport(
        lambda request: preflight.httpx.Response(200, text=pages[str(request.url)])
    )
    monkeypatch.setattr(
        preflight.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs)
    )
    _, baseline = asyncio.run(preflight.check(tmp_path))
    (tmp_path / preflight.BASELINE).write_text(json.dumps(baseline))
    # The release need not appear in the abbreviated index.
    pages[preflight.SOURCES["openai_catalog"]] = OPENAI_CATALOG.replace(
        "## More models\n",
        f"## More models\n- [New image](/api/docs/models/{model_id}.md): image\n",
    )
    result, observed = asyncio.run(preflight.check(tmp_path))
    assert result["status"] == "change"
    assert result["differences"]["openai"]["added"] == [model_id]
    assert result["differences"]["openai_details"]["added"] == [model_id]
    assert observed["openai_details"][model_id] == {
        "input_modalities": "text, image",
        "output_modalities": "image",
        "endpoints": {
            "v1/responses": False,
            "v1/images/generations": True,
            "v1/images/edits": True,
        },
        "pricing": {
            "text_tokens": {"input": "$5", "cached_input": "$1.25"},
            "image_tokens": {"input": "$8", "cached_input": "$2", "output": "$30"},
        },
    }
    (tmp_path / preflight.BASELINE).write_text(json.dumps(observed))
    stable, _ = asyncio.run(preflight.check(tmp_path))
    assert stable["status"] == "no_change"
    pages[f"https://developers.openai.com/api/docs/models/{model_id}.md"] = image_page.replace(
        "$30", "$40"
    ).replace(
        "| Image edit | `v1/images/edits` | Supported |",
        "| Image edit | `v1/images/edits` | Not supported |",
    )
    changed, _ = asyncio.run(preflight.check(tmp_path))
    assert changed["differences"]["openai_details"]["changed"] == [model_id]
    pages[f"https://developers.openai.com/api/docs/models/{model_id}.md"] = image_page.replace(
        "| Output | $30 | 1M tokens |\n", ""
    )
    with pytest.raises(ValueError, match="image pricing incomplete"):
        asyncio.run(preflight.check(tmp_path))


def test_deferred_model_remains_monitored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepare_checkout(tmp_path)
    cyber_page = OPENAI_PAGE.replace(
        "Model ID: `gpt-6-sol`",
        "Model ID: `gpt-6-cyber`\n\nDaybreak Red approval is required.",
    ).replace("`reasoning.effort` supports `low`, `medium`, and `high`.\n", "")
    cyber_details = preflight.openai_model_details(cyber_page, "gpt-6-cyber")
    baseline_path = tmp_path / preflight.BASELINE
    baseline = json.loads(baseline_path.read_text())
    baseline["schema_version"] = 3
    baseline["openai"].append("gpt-6-cyber")
    baseline["openai_status"] = preflight.openai_catalog(OPENAI_CATALOG)
    baseline["openai_status"]["gpt-6-cyber"] = "listed"
    baseline["openai_details"]["gpt-6-cyber"] = cyber_details
    baseline_path.write_text(json.dumps(baseline))
    pages = {
        "claude": CLAUDE_TABLE,
        "openai": OPENAI_INDEX,
        "openai_catalog": OPENAI_CATALOG.replace(
            "## More models\n",
            "## More models\n- [Cyber](/api/docs/models/gpt-6-cyber.md): model\n",
        ),
        "codex": json.dumps(CODEX_CATALOG),
    }
    details = {
        "gpt-6-sol": preflight.openai_model_details(OPENAI_PAGE, "gpt-6-sol"),
        "gpt-6-cyber": cyber_details,
    }

    async def fake_sources() -> dict[str, str]:
        return pages

    async def fake_details(model_ids: list[str]) -> dict[str, dict[str, object]]:
        return details

    monkeypatch.setattr(preflight, "fetch_sources", fake_sources)
    monkeypatch.setattr(preflight, "fetch_openai_details", fake_details)
    result, observed = asyncio.run(preflight.check(tmp_path))
    assert result["status"] == "no_change"
    assert observed["openai_status"]["gpt-6-cyber"] == "listed"
    assert observed["openai_details"]["gpt-6-cyber"]["access_requirements"] == (
        "Daybreak Red approval is required."
    )

    pages["openai_catalog"] = pages["openai_catalog"].replace(
        "## More models\n",
        "## More models\n- [New](/api/docs/models/gpt-6-new.md): model\n",
    )
    result, _ = asyncio.run(preflight.check(tmp_path))
    assert result["differences"]["openai"]["added"] == ["gpt-6-new"]
    assert result["status"] == "change"
    pages["openai_catalog"] = pages["openai_catalog"].replace(
        "- [New](/api/docs/models/gpt-6-new.md): model\n", ""
    )

    details["gpt-6-cyber"] = preflight.openai_model_details(
        cyber_page.replace("Daybreak Red approval is required.", "Access is open."),
        "gpt-6-cyber",
    )
    result, _ = asyncio.run(preflight.check(tmp_path))
    assert result["differences"]["openai_details"]["changed"] == ["gpt-6-cyber"]
    details["gpt-6-cyber"] = cyber_details
    pages["openai_catalog"] = (
        pages["openai_catalog"]
        .replace(
            "- [Cyber](/api/docs/models/gpt-6-cyber.md): model\n",
            "",
        )
        .replace(
            "## Deprecated models\n",
            "## Deprecated models\n- [Cyber](/api/docs/models/gpt-6-cyber.md): Deprecated. model\n",
        )
    )
    result, observed = asyncio.run(preflight.check(tmp_path))
    assert result["differences"]["openai_status"]["changed"] == ["gpt-6-cyber"]
    assert observed["openai_status"]["gpt-6-cyber"] == "deprecated"
    pages["openai_catalog"] = pages["openai_catalog"].replace(
        "- [Cyber](/api/docs/models/gpt-6-cyber.md): Deprecated. model\n", ""
    )
    with pytest.raises(ValueError, match="missing tracked"):
        asyncio.run(preflight.check(tmp_path))


@pytest.mark.parametrize(
    "catalog",
    [
        OPENAI_CATALOG.replace("## Deprecated models", "## Archived models"),
        OPENAI_CATALOG.replace(
            "- [GPT-4](/api/docs/models/gpt-4.md): Shut down. Older model\n", ""
        ),
        OPENAI_CATALOG.replace("Deprecated. Older model", "Unknown. Older model"),
    ],
)
def test_incomplete_full_catalog_never_removes_tracked_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalog: str
) -> None:
    prepare_checkout(tmp_path)

    async def fake_sources() -> dict[str, str]:
        return {
            "claude": CLAUDE_TABLE,
            "openai": OPENAI_INDEX,
            "openai_catalog": catalog,
            "codex": json.dumps(CODEX_CATALOG),
        }

    monkeypatch.setattr(preflight, "fetch_sources", fake_sources)
    with pytest.raises(ValueError, match="OpenAI"):
        asyncio.run(preflight.check(tmp_path))


def test_missing_model_page_metadata_is_not_a_noop() -> None:
    with pytest.raises(ValueError, match="token limit missing"):
        preflight.openai_model_details(
            OPENAI_PAGE.replace("- 128,000 max output tokens\n", ""), "gpt-6-sol"
        )


def test_openai_reasoning_efforts_include_values_wrapped_across_lines() -> None:
    markdown = OPENAI_PAGE.replace(
        "`reasoning.effort` supports `low`, `medium`, and `high`.",
        "`reasoning.effort` supports `low`, `medium`, `high`,\n`xhigh`, and `max`.",
    )

    assert preflight.openai_model_details(markdown, "gpt-6-sol")["reasoning_efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_read_from_ref_does_not_change_working_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> object:
        assert command == [
            "git",
            "-C",
            str(tmp_path),
            "show",
            "origin/main:scripts/model_support_baseline.json",
        ]
        assert kwargs["check"] is True
        return type("Result", (), {"stdout": '{"schema_version": 1}'})()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    assert preflight.read_from_ref(tmp_path, "origin/main", preflight.BASELINE) == (
        '{"schema_version": 1}'
    )


def test_pinned_revision_ignores_dirty_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare_checkout(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Preflight Test",
            "-c",
            "user.email=preflight-test@localhost",
            "commit",
            "-qm",
            "test: baseline",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    (tmp_path / preflight.BASELINE).write_text('{"schema_version": 2, "openai": []}')
    (tmp_path / preflight.CATALOG).write_text('{"models": []}')

    async def fake_sources() -> dict[str, str]:
        return {
            "claude": CLAUDE_TABLE,
            "openai": OPENAI_INDEX,
            "openai_catalog": OPENAI_CATALOG,
            "codex": json.dumps(CODEX_CATALOG),
        }

    async def fake_details(model_ids: list[str]) -> dict[str, dict[str, object]]:
        return {"gpt-6-sol": preflight.openai_model_details(OPENAI_PAGE, "gpt-6-sol")}

    monkeypatch.setattr(preflight, "fetch_sources", fake_sources)
    monkeypatch.setattr(preflight, "fetch_openai_details", fake_details)
    result, _ = asyncio.run(preflight.check(tmp_path, revision))
    assert result["status"] == "change"
    assert result["baseline_ref"] == revision


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("no_change", 0), ("change", 2)],
)
def test_cli_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(preflight.sys, "argv", ["check_model_support.py"])

    async def fake_check(
        root: Path, ref: str | None, *, bootstrap: bool = False
    ) -> tuple[dict[str, object], dict[str, object]]:
        assert ref is None
        return {"status": status, "update_required": status == "change"}, {}

    monkeypatch.setattr(preflight, "check", fake_check)
    assert preflight.main() == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_cli_incomplete_check_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(preflight.sys, "argv", ["check_model_support.py"])

    async def fake_check(
        root: Path, ref: str | None, *, bootstrap: bool = False
    ) -> tuple[dict[str, object], dict[str, object]]:
        raise preflight.httpx.ConnectError("public source unavailable")

    monkeypatch.setattr(preflight, "check", fake_check)
    assert preflight.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "incomplete"
    assert payload["update_required"] is True
