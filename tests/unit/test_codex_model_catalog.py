from cognis.providers.llm.codex import (
    CODEX_CLIENT_VERSION,
    bundled_codex_model_entries,
    codex_catalog_model_info,
    load_bundled_codex_catalog,
)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def test_codex_gpt5_catalog_models_enable_native_pdf_input() -> None:
    for model_id in (
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "gpt-5.3-codex",
        "gpt-5.2",
    ):
        info = codex_catalog_model_info(model_id)

        assert info is not None
        assert info["supports_pdf_input"] is True
        assert info["supports_file_input"] is False
        assert info["source"] == "codex_catalog"


def test_codex_gpt56_catalog_models_expose_native_ultra_reasoning() -> None:
    sol_info = codex_catalog_model_info("gpt-5.6-sol")
    terra_info = codex_catalog_model_info("gpt-5.6-terra")
    luna_info = codex_catalog_model_info("gpt-5.6-luna")

    assert sol_info is not None
    assert terra_info is not None
    assert luna_info is not None
    assert sol_info["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert terra_info["reasoning_efforts"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert luna_info["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert sol_info["supports_tool_search"] is True
    assert sol_info["supports_openai_apply_patch"] is True
    assert sol_info["openai_apply_patch_tool_type"] == "freeform"


def test_codex_client_version_covers_bundled_visible_catalog() -> None:
    client_version = _version_tuple(CODEX_CLIENT_VERSION)

    for item in load_bundled_codex_catalog().values():
        if item.get("supported_in_api") is False:
            continue
        if str(item.get("visibility") or "").strip().lower() in {"hide", "hidden"}:
            continue
        minimal = item.get("minimal_client_version")
        assert isinstance(minimal, str)
        assert client_version >= _version_tuple(minimal), item["slug"]


def test_bundled_codex_entries_exclude_hidden_upstream_models() -> None:
    model_ids = {entry["model_id"] for entry in bundled_codex_model_entries()}

    assert "codex-auto-review" not in model_ids
