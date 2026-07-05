from cognis.providers.llm.codex import codex_catalog_model_info


def test_codex_gpt5_catalog_models_enable_native_pdf_input() -> None:
    for model_id in (
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
