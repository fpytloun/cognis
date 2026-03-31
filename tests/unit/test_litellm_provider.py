from __future__ import annotations

import pytest

from cognis.models.config import DEFAULT_MODEL_INFO
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider, ModelRouting


async def _session_factory(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, create_session_factory(engine)


@pytest.mark.asyncio
async def test_litellm_provider_resolves_explicit_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    resolved = await provider.resolve_model(explicit_model="gpt-5.4-mini")

    assert resolved == "gpt-5.4-mini"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_uses_model_routing_entry(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(ModelRouting(task_type="default", provider_id=None, model="gpt-4o-mini"))
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_falls_back_to_default_provider_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"default_model": "gpt-4o-mini"},
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)

    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_raises_when_no_model_is_configured(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    with pytest.raises(ValueError, match="No LLM model configured"):
        await provider.resolve_model(task_type="default")

    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_returns_model_info_from_provider_config(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={
                    "default_model": "gpt-4o-mini",
                    "models": [
                        {
                            "model_id": "gpt-4o-mini",
                            "context_window": 12345,
                            "max_output_tokens": 678,
                        }
                    ],
                },
                status="active",
            )
        )
        await session.commit()

    provider = LiteLLMProvider(session_factory)
    model_info = await provider.get_model_info("gpt-4o-mini")

    assert model_info.context_window == 12345
    assert model_info.max_output_tokens == 678
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_returns_default_model_info_when_missing(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    model_info = await provider.get_model_info("missing-model")

    assert model_info == DEFAULT_MODEL_INFO
    await engine.dispose()


def test_litellm_provider_count_messages_tokens_falls_back_when_token_counter_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    provider = LiteLLMProvider(object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "cognis.providers.llm.litellm.litellm.token_counter",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    count = provider.count_messages_tokens(
        [{"role": "user", "content": "hello world"}],
        "gpt-4o-mini",
    )

    assert count > 0


@pytest.mark.asyncio
async def test_litellm_provider_test_provider_sanitizes_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(
            LLMProvider(
                provider_id="default",
                display_name="OpenAI",
                location="controller",
                backend="litellm",
                config={"default_model": "gpt-4o-mini"},
                status="active",
            )
        )
        await session.commit()

    async def _fake_completion(**_: object) -> dict[str, object]:
        raise RuntimeError("api_key=secret-value sk-secret123")

    monkeypatch.setattr("cognis.providers.llm.litellm.litellm.acompletion", _fake_completion)
    provider = LiteLLMProvider(session_factory)
    result = await provider.test_provider("default")

    assert result["ok"] is False
    assert "secret-value" not in str(result["error_detail"])
    assert "sk-secret123" not in str(result["error_detail"])
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_health_reports_degraded_without_model(tmp_path: object) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    provider = LiteLLMProvider(session_factory)

    health = await provider.health()

    assert health.status == "degraded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_provider_cached_resolution_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    async with session_factory() as session:
        session.add(ModelRouting(task_type="default", provider_id=None, model="gpt-4o-mini"))
        await session.commit()

    time_points = iter([1.0, 70.0, 70.0])
    monkeypatch.setattr("cognis.providers.llm.litellm.monotonic", lambda: next(time_points))

    provider = LiteLLMProvider(session_factory)
    assert await provider.resolve_model(task_type="default") == "gpt-4o-mini"

    async with session_factory() as session:
        row = await session.get(ModelRouting, "default")
        assert row is not None
        row.model = "gpt-5.4-mini"
        await session.commit()

    assert await provider.resolve_model(task_type="default") == "gpt-5.4-mini"
    await engine.dispose()


# ---------------------------------------------------------------------------
# _apply_model_prefix tests
# ---------------------------------------------------------------------------


def _make_provider_row(preset: str) -> LLMProvider:
    """Create a minimal LLMProvider ORM instance with a given preset."""
    return LLMProvider(
        provider_id="test",
        display_name="Test",
        location="controller",
        backend="litellm",
        config={"preset": preset, "default_model": "some-model"},
        status="active",
    )


def test_apply_model_prefix_returns_unchanged_when_provider_is_none() -> None:
    assert LiteLLMProvider._apply_model_prefix("gpt-4o", None) == "gpt-4o"


def test_apply_model_prefix_returns_unchanged_when_model_contains_slash() -> None:
    provider = _make_provider_row("openai_compatible")
    assert LiteLLMProvider._apply_model_prefix("ollama/llama3", provider) == "ollama/llama3"


def test_apply_model_prefix_adds_openai_prefix_for_openai_compatible() -> None:
    provider = _make_provider_row("openai_compatible")
    assert LiteLLMProvider._apply_model_prefix("gpt-oss-120b", provider) == "openai/gpt-oss-120b"


def test_apply_model_prefix_adds_litellm_proxy_prefix() -> None:
    provider = _make_provider_row("litellm_proxy")
    assert (
        LiteLLMProvider._apply_model_prefix("gpt-oss-120b", provider)
        == "litellm_proxy/gpt-oss-120b"
    )


def test_apply_model_prefix_no_prefix_for_standard_openai() -> None:
    provider = _make_provider_row("openai")
    assert LiteLLMProvider._apply_model_prefix("gpt-4o", provider) == "gpt-4o"


def test_apply_model_prefix_no_prefix_for_anthropic() -> None:
    provider = _make_provider_row("anthropic")
    assert (
        LiteLLMProvider._apply_model_prefix("claude-sonnet-4-20250514", provider)
        == "claude-sonnet-4-20250514"
    )


def test_apply_model_prefix_no_prefix_for_unknown_preset() -> None:
    provider = _make_provider_row("some_future_preset")
    assert LiteLLMProvider._apply_model_prefix("my-model", provider) == "my-model"


def test_apply_model_prefix_no_prefix_when_preset_missing() -> None:
    provider = LLMProvider(
        provider_id="test",
        display_name="Test",
        location="controller",
        backend="litellm",
        config={"default_model": "my-model"},
        status="active",
    )
    assert LiteLLMProvider._apply_model_prefix("my-model", provider) == "my-model"
