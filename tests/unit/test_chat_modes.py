from cognis.core.chat_modes import optional_chat_mode


def test_optional_chat_mode_validates_durable_values_without_calling_type_alias() -> None:
    assert optional_chat_mode("build") == "build"
    assert optional_chat_mode(" PLAN ") == "plan"
    assert optional_chat_mode(None) is None
    assert optional_chat_mode("unsupported") is None
