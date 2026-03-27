from __future__ import annotations

from cognis.models.agent import AgentPermissions
from cognis.models.tool import Permission


def test_tool_permissions_exact_match_wins() -> None:
    permissions = AgentPermissions(
        tool_permissions={"shell": Permission.DENY, "*": Permission.ALLOW}
    )

    assert permissions.resolve_permission("shell") is Permission.DENY


def test_tool_permissions_glob_match_uses_longest_pattern() -> None:
    permissions = AgentPermissions(
        tool_permissions={"filesystem/*": Permission.ALLOW, "filesystem/delete_*": Permission.DENY}
    )

    assert permissions.resolve_permission("filesystem/delete_file") is Permission.DENY
    assert permissions.resolve_permission("filesystem/read_file") is Permission.ALLOW


def test_tool_permissions_fall_back_to_evaluate() -> None:
    permissions = AgentPermissions(tool_permissions={})

    assert permissions.resolve_permission("unknown/tool") is Permission.EVALUATE


def test_legacy_permissions_deny_then_allow() -> None:
    permissions = AgentPermissions(
        tool_permissions={}, denied_tools=["shell"], allowed_tools=["filesystem/*"]
    )

    assert permissions.resolve_permission("shell") is Permission.DENY
    assert permissions.resolve_permission("filesystem/read_file") is Permission.ALLOW
    assert permissions.resolve_permission("other") is Permission.EVALUATE


def test_new_permissions_override_legacy_lists() -> None:
    permissions = AgentPermissions(
        tool_permissions={"shell": Permission.ALLOW},
        denied_tools=["shell"],
    )

    assert permissions.resolve_permission("shell") is Permission.ALLOW
