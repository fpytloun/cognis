from cognis.api.app import _effective_api_limits


def test_api_limits_preserve_configured_values_outside_e2e() -> None:
    assert _effective_api_limits(e2e_mode=False, read=600, write=200) == (600, 200)


def test_api_limits_raise_runtime_capacity_only_in_e2e() -> None:
    assert _effective_api_limits(e2e_mode=True, read=600, write=200) == (60_000, 60_000)
