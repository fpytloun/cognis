from cognis.api.chat_v2.schemas import (
    ArtifactTimelineItem,
    AssistantDeliverableTimelineItem,
    FileDiffRef,
    SourceRef,
    TimelineScope,
    ToolCallTimelineItem,
)
from cognis.api.chat_v2.work_projection import build_work_projection
from cognis.models.tool import (
    NativeToolOperation,
    ToolDefinition,
    ToolMutationKind,
    ToolSource,
    declared_default_semantics,
)


def _source(seq: int) -> SourceRef:
    return SourceRef(
        store="intaris",
        session_id="session-1",
        seq=seq,
        event_type="tool_call",
    )


def _tool(name: str, *, read_only: bool, category: str = "filesystem") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        source=ToolSource(type="mcp", server_id="server-1", raw_tool_name=name),
        read_only=read_only,
        category=category,
    )


def _call(
    name: str,
    *,
    seq: int,
    arguments: dict | None = None,
    result: str | None = "done",
    diffs: list[FileDiffRef] | None = None,
    is_error: bool = False,
) -> ToolCallTimelineItem:
    return ToolCallTimelineItem(
        id=f"tool:{seq}",
        call_id=f"call-{seq}",
        tool_name=name,
        sort_key=f"0001:{seq:08d}",
        source_refs=[_source(seq)],
        created_at=f"2026-01-01T00:00:0{seq}Z",
        status="failed" if is_error else "complete",
        arguments=arguments,
        result_preview=result,
        is_error=is_error,
        file_diffs=diffs or [],
    )


def _project(items, definitions, *, complete_files: bool = False):
    return build_work_projection(
        scope=TimelineScope(
            key="conversation:conversation-1",
            kind="conversation",
            conversation_id="conversation-1",
        ),
        projection_version="chat-v2-test",
        items=items,
        tool_definitions={tool.name: tool for tool in definitions},
        has_more_before=False,
        before_cursor=None,
        server_time="2026-01-01T00:00:10Z",
        complete_files=complete_files,
    )


def test_projects_only_server_classified_mutations_and_redacts_arguments() -> None:
    projection = _project(
        [
            _call("read", seq=1, arguments={"file_path": "/srv/private/a.py"}),
            _call(
                "write",
                seq=2,
                arguments={
                    "path": "/srv/private/a.py",
                    "token": "secret-value",
                    "description": "write with password=hunter2",
                    "content": "not safe to display",
                },
                diffs=[FileDiffRef(path="/srv/private/a.py", diff="+safe")],
            ),
            _call("unknown", seq=3),
        ],
        [_tool("read", read_only=True), _tool("write", read_only=False)],
    )

    assert [event.call_id for event in projection.mutations] == ["call-2"]
    event = projection.mutations[0]
    assert event.arguments == {
        "path": "a.py",
        "token": "[redacted]",
        "description": "write with password=[redacted]",
    }
    assert "content" not in event.arguments
    assert event.file_diffs[0].path == "a.py"
    assert event.file_diffs[0].diff == "+safe"


def test_projects_bounded_command_output_and_existing_recovery_reference() -> None:
    call = _call(
        "bash",
        seq=1,
        arguments={
            "command": "git status",
            "description": "Inspect state",
            "workdir": "/home/user/repo",
            "env": {"TOKEN": "secret"},
        },
        result="x" * 9_000,
    ).model_copy(
        update={
            "truncated": True,
            "has_full_output": True,
            "recovery_call_id": "stored-call-1",
        }
    )
    projection = _project([call], [_tool("bash", read_only=False, category="shell")])

    command = projection.commands[0]
    assert command.command == "git status"
    assert command.workdir == "repo"
    assert command.preview_truncated is True
    assert len(command.preview or "") == 8_000
    assert command.recovery_call_id == "stored-call-1"
    assert command.has_full_output is True


def test_redacts_inline_command_secrets_and_omits_env_diffs() -> None:
    projection = _project(
        [
            _call(
                "bash",
                seq=1,
                arguments={"command": "curl -H 'Authorization: Bearer abc123' ?token=xyz"},
                result="password=hunter2",
            ),
            _call(
                "write",
                seq=2,
                diffs=[FileDiffRef(path=".env.staging", diff="+API_KEY=super-secret")],
            ),
        ],
        [
            _tool("bash", read_only=False, category="shell"),
            _tool("write", read_only=False),
        ],
    )

    assert projection.commands[0].command == (
        "curl -H 'Authorization: Bearer [redacted]' ?token=[redacted]"
    )
    assert projection.commands[0].preview == "password=[redacted]"
    assert projection.mutations[0].file_diffs[0].diff == "… sensitive diff content omitted …"


def test_mixed_native_tool_read_operation_is_not_a_mutation() -> None:
    definition = ToolDefinition(
        name="manage",
        description="Manage resources",
        source=ToolSource(type="executor"),
        category="agent_management",
        read_only=False,
        native_operations=[
            NativeToolOperation(
                operation="list",
                summary="List",
                mutation_kind=ToolMutationKind.READ,
                input_schema={
                    "type": "object",
                    "properties": {"action": {"const": "list"}},
                    "required": ["action"],
                },
                semantics=declared_default_semantics(ToolMutationKind.READ),
                examples=[{"action": "list"}],
            ),
            NativeToolOperation(
                operation="create",
                summary="Create",
                mutation_kind=ToolMutationKind.CREATE,
                input_schema={
                    "type": "object",
                    "properties": {"action": {"const": "create"}},
                    "required": ["action"],
                },
                semantics=declared_default_semantics(ToolMutationKind.CREATE),
                examples=[{"action": "create"}],
            ),
        ],
    )
    projection = _project(
        [
            _call("manage", seq=1, arguments={"action": "list"}),
            _call("manage", seq=2, arguments={"action": "create"}),
        ],
        [definition],
    )

    assert [event.call_id for event in projection.mutations] == ["call-2"]


def test_latest_deliverable_is_authoritative_and_order_is_stable() -> None:
    deliverables = [
        AssistantDeliverableTimelineItem(
            id="deliverable:first",
            deliverable_id="first",
            format="markdown",
            content="old",
            sort_key="0001:00000001",
            source_refs=[_source(1)],
        ),
        AssistantDeliverableTimelineItem(
            id="deliverable:final",
            deliverable_id="final",
            format="rich",
            title="Final",
            content="canonical",
            sort_key="0001:00000004",
            source_refs=[_source(4)],
        ),
    ]
    projection = _project(
        [
            deliverables[1],
            _call("write", seq=3),
            ArtifactTimelineItem(
                id="artifact:1",
                artifact_id="artifact-1",
                filename="/private/report.pdf",
                sort_key="0001:00000002",
                source_refs=[_source(2)],
            ),
            deliverables[0],
        ],
        [_tool("write", read_only=False)],
    )

    assert projection.final_deliverable is not None
    assert projection.final_deliverable.deliverable_id == "final"
    assert projection.mutations == []
    assert projection.artifacts[0].filename == "report.pdf"
    assert projection.summary.mutations == 0
    assert projection.summary.artifacts == 1


def test_diff_stats_are_exact_before_file_and_content_preview_bounds() -> None:
    large_diff = "\n".join(["+before-bound"] * 10_001 + ["-after-bound"] * 7_003)
    diffs = [
        FileDiffRef(path="src/0.py", diff=large_diff),
        *[FileDiffRef(path=f"src/{index}.py", diff="+added\n-removed") for index in range(1, 25)],
    ]
    projection = _project(
        [_call("write", seq=1, diffs=diffs)],
        [_tool("write", read_only=False)],
    )

    event = projection.mutations[0]
    assert len(event.file_diffs) == 20
    assert event.diffs_truncated is True
    assert event.file_diffs[0].diff.endswith("… diff content truncated …")
    assert event.file_diffs[0].content_truncated is True
    assert event.file_diffs[0].additions == 10_001
    assert event.file_diffs[0].deletions == 7_003
    assert event.total_file_count == 25
    assert event.omitted_file_count == 5
    assert event.additions == 10_025
    assert event.deletions == 7_027
    assert projection.summary.additions == 10_025
    assert projection.summary.deletions == 7_027
    assert projection.summary.changed_files == 25
    assert projection.summary.omitted_files == 5


def test_filters_control_plumbing_and_keeps_product_and_external_writes() -> None:
    manage = ToolDefinition(
        name="manage_agents",
        description="Manage agents",
        source=ToolSource(type="builtin"),
        category="agent_management",
        read_only=False,
        native_operations=[
            NativeToolOperation(
                operation="update",
                summary="Update",
                mutation_kind=ToolMutationKind.UPDATE,
                input_schema={
                    "type": "object",
                    "properties": {"action": {"const": "update"}},
                    "required": ["action"],
                },
                semantics=declared_default_semantics(ToolMutationKind.UPDATE),
                examples=[{"action": "update"}],
            )
        ],
    )
    projection = _project(
        [
            _call("attach_artifact", seq=1),
            _call("todo_write", seq=2),
            _call("delegate", seq=3),
            _call("apply_patch", seq=4, is_error=True),
            _call("manage_agents", seq=5, arguments={"action": "update"}),
            _call("send_gmail_message", seq=6, arguments={"operation": "send"}),
            _call("search_gmail_messages", seq=7),
        ],
        [
            _tool("attach_artifact", read_only=False, category="artifact"),
            _tool("todo_write", read_only=False, category="workflow"),
            _tool("delegate", read_only=False, category="orchestration"),
            _tool("apply_patch", read_only=False),
            manage,
            _tool("send_gmail_message", read_only=False, category="mcp"),
            _tool("search_gmail_messages", read_only=False, category="mcp"),
        ],
    )

    assert [event.tool_name for event in projection.mutations] == [
        "manage_agents",
        "send_gmail_message",
    ]
    assert projection.summary.mutations == 2


def test_filters_bash_kill_and_projects_renderer_parity_fields() -> None:
    command = _call(
        "bash",
        seq=1,
        arguments={"command": "git status", "description": "Inspect"},
        result="clean",
    ).model_copy(
        update={
            "display_name": "Run command",
            "evaluation": {
                "decision": "approve",
                "risk": "low",
                "reasoning": "Safe read",
                "token": "not projected",
            },
            "output_size": 5,
        }
    )
    projection = _project(
        [
            command,
            _call("bash", seq=2, arguments={"action": "kill", "command": "kill 42"}),
        ],
        [_tool("bash", read_only=False, category="shell")],
    )

    assert len(projection.commands) == 1
    assert projection.commands[0].display_name == "Run command"
    assert projection.commands[0].arguments == {
        "command": "git status",
        "description": "Inspect",
    }
    assert projection.commands[0].evaluation == {
        "decision": "approve",
        "reasoning": "Safe read",
        "risk": "low",
    }
    assert projection.commands[0].output_size == 5


def test_excludes_controller_rejected_commands_and_file_mutations() -> None:
    rejected_command = _call(
        "bash",
        seq=1,
        arguments={"command": "git status"},
        result='{"status":"retry","reason":"project_instructions_loaded"}',
        is_error=True,
    )
    rejected_patch = _call(
        "apply_patch",
        seq=2,
        arguments={"path": "src/app.py"},
        result='{"status":"retry","reason":"project_instructions_loaded"}',
        diffs=[FileDiffRef(path="src/app.py", diff="+not-applied")],
        is_error=True,
    )

    projection = _project(
        [rejected_command, rejected_patch],
        [
            _tool("bash", read_only=False, category="shell"),
            _tool("apply_patch", read_only=False),
        ],
    )

    assert projection.commands == []
    assert projection.mutations == []
    assert projection.summary.commands == 0
    assert projection.summary.changed_files == 0


def test_projects_running_and_completed_commands_without_empty_rows() -> None:
    completed = _call(
        "bash",
        seq=1,
        arguments={"command": "pytest tests/unit/test_old.py"},
    )
    running = _call(
        "bash",
        seq=3,
        arguments={"command": "pytest tests/unit/test_new.py"},
        result=None,
    ).model_copy(update={"status": "running"})
    empty = _call("bash", seq=4, arguments={"description": "missing command"})
    blank = _call("bash", seq=5, arguments={"command": "  \n "})

    projection = _project(
        [completed, running, empty, blank],
        [_tool("bash", read_only=False, category="shell")],
    )

    assert [(event.command, event.status) for event in projection.commands] == [
        ("pytest tests/unit/test_old.py", "complete"),
        ("pytest tests/unit/test_new.py", "running"),
    ]


def test_excludes_prefixed_file_manipulation_and_empty_tool_names() -> None:
    projection = _project(
        [
            _call("functions.apply_patch", seq=1, arguments={"patchText": "*** Begin Patch"}),
            _call("builtin/edit", seq=2, arguments={"path": "a.py"}),
            _call("", seq=3, arguments={"operation": "create"}),
            _call("send_gmail_message", seq=4),
        ],
        [_tool("send_gmail_message", read_only=False, category="mcp")],
    )

    assert [event.tool_name for event in projection.mutations] == ["send_gmail_message"]


def test_excludes_file_manipulation_without_an_applied_diff() -> None:
    projection = _project(
        [
            _call("apply_patch", seq=1, result="No changes applied"),
            _call("write", seq=2, result="No diff captured"),
            _call("send_gmail_message", seq=3),
        ],
        [
            _tool("apply_patch", read_only=False, category="filesystem"),
            _tool("write", read_only=False, category="filesystem"),
            _tool("send_gmail_message", read_only=False, category="mcp"),
        ],
    )

    assert [event.tool_name for event in projection.mutations] == ["send_gmail_message"]
    assert projection.summary.mutations == 1


def test_projects_all_deliverables_and_aggregate_diff_stats() -> None:
    deliverables = [
        AssistantDeliverableTimelineItem(
            id=f"deliverable:{identifier}",
            deliverable_id=identifier,
            format="markdown",
            content=identifier,
            sort_key=f"0001:0000000{index}",
            source_refs=[_source(index)],
        )
        for index, identifier in enumerate(("first", "second"), start=1)
    ]
    projection = _project(
        [
            *deliverables,
            _call(
                "write",
                seq=3,
                arguments={"workdir": "/home/user/repo"},
                diffs=[
                    FileDiffRef(
                        path="/home/user/repo/src/app.py",
                        diff="--- a/src/app.py\n+++ b/src/app.py\n-old\n+new\n+extra",
                    )
                ],
            ),
        ],
        [_tool("write", read_only=False)],
    )

    assert [item.deliverable_id for item in projection.deliverables] == [
        "first",
        "second",
    ]
    assert projection.final_deliverable == projection.deliverables[-1]
    assert projection.summary.deliverables == 2
    assert projection.summary.additions == 2
    assert projection.summary.deletions == 1
    assert projection.mutations[0].file_diffs[0].path == "repo/src/app.py"


def test_recursive_redaction_runs_before_bounds_and_covers_json_text() -> None:
    projection = _project(
        [
            _call(
                "sendGmailMessage",
                seq=1,
                arguments={
                    "operation": "send",
                    "target": {
                        "account": {
                            "access_token": "nested-token",
                            "profile": [{"apiKey": "nested-api-key", "name": "safe"}],
                        }
                    },
                    "description": ('{"password":"json-password","safe":"' + ("x" * 700) + '"}'),
                },
                result='{"secret":"result-secret","status":"sent"}',
            ).model_copy(
                update={
                    "evaluation": {
                        "decision": "allow",
                        "reasoning": '{"api_key":"evaluation-key"}',
                    }
                }
            )
        ],
        [_tool("sendGmailMessage", read_only=False, category="mcp")],
    )

    event = projection.mutations[0]
    rendered = event.model_dump_json()
    assert "nested-token" not in rendered
    assert "nested-api-key" not in rendered
    assert "json-password" not in rendered
    assert "result-secret" not in rendered
    assert "evaluation-key" not in rendered
    assert event.arguments["target"] == {
        "account": {
            "access_token": "[redacted]",
            "profile": [{"apiKey": "[redacted]", "name": "safe"}],
        }
    }
    assert len(str(event.arguments["description"])) <= 501


def test_redaction_consumes_quoted_spaces_escaped_quotes_arrays_and_nested_json_strings() -> None:
    projection = _project(
        [
            _call(
                "sendGmailMessage",
                seq=1,
                arguments={
                    "operation": "send",
                    "description": (
                        'password="two words and \\"quotes\\"" '
                        "api_key=['first value','second value'] "
                        'payload={"nested":"{\\"access_token\\":\\"deep token\\"}"}'
                    ),
                    "target": {
                        "payload": '{"secret":"a value with spaces","items":[{"password":"p q"}]}'
                    },
                },
                result=(
                    '{"message":"authorization: Bearer token with spaces",'
                    '"nested":"{\\"api_key\\":\\"quoted key value\\"}"}'
                ),
            )
        ],
        [_tool("sendGmailMessage", read_only=False, category="mcp")],
    )

    serialized = projection.model_dump_json()
    for secret in (
        "two words",
        "first value",
        "second value",
        "deep token",
        "a value with spaces",
        "p q",
        "quoted key value",
    ):
        assert secret not in serialized


def test_trusted_roots_cover_unix_windows_outside_and_same_suffix_without_collision() -> None:
    projection = _project(
        [
            _call(
                "bash",
                seq=1,
                arguments={"command": "pwd", "workdir": "/srv/alpha/repo"},
            ),
            _call(
                "bash",
                seq=2,
                arguments={"command": "cd", "workdir": "/opt/beta/repo"},
            ),
            _call(
                "bash",
                seq=3,
                arguments={"command": "cd", "workdir": r"C:\Users\dev\windows-repo"},
            ),
            _call(
                "write",
                seq=4,
                diffs=[
                    FileDiffRef(path="/srv/alpha/repo/src/app.py", diff="+alpha"),
                    FileDiffRef(path="/opt/beta/repo/src/app.py", diff="+beta"),
                    FileDiffRef(
                        path=r"C:\Users\dev\windows-repo\src\app.py",
                        diff="+windows",
                    ),
                    FileDiffRef(path="/private/untrusted/src/app.py", diff="+outside"),
                ],
            ),
        ],
        [
            _tool("bash", read_only=False, category="shell"),
            _tool("write", read_only=False),
        ],
    )

    diffs = projection.mutations[0].file_diffs
    assert [diff.path for diff in diffs] == [
        "repo/src/app.py",
        "repo/src/app.py",
        "windows-repo/src/app.py",
        "app.py",
    ]
    assert diffs[0].path_id != diffs[1].path_id
    assert diffs[0].root_name == diffs[1].root_name == "repo"
    assert diffs[2].root_name == "windows-repo"
    assert diffs[3].root_name is None


def test_absolute_file_paths_derive_safe_hierarchies_and_shared_identities() -> None:
    actual_path = "/home/riker/src/cognis-hotfix/ui/src/routes/a/+page.svelte"
    projection = _project(
        [
            _call(
                "apply_patch",
                seq=1,
                diffs=[
                    FileDiffRef(path=actual_path, diff="+absolute"),
                    FileDiffRef(
                        path="/home/riker/src/cognis-hotfix/ui/src/routes/b/+page.svelte",
                        diff="+sibling",
                    ),
                ],
            ),
            _call(
                "write",
                seq=2,
                arguments={"workdir": "/home/riker/src/cognis-hotfix"},
                diffs=[
                    FileDiffRef(
                        path="ui/src/routes/a/+page.svelte",
                        diff="+relative",
                    )
                ],
            ),
            _call(
                "apply_patch",
                seq=3,
                diffs=[
                    FileDiffRef(
                        path="/Users/alice/src/other-repo/app/main.py",
                        diff="+mac",
                    ),
                    FileDiffRef(
                        path=r"C:\Users\Dev\src\windows-repo\app\main.py",
                        diff="+windows",
                    ),
                    FileDiffRef(path="/srv/alpha/repo/app/main.py", diff="+alpha"),
                    FileDiffRef(path="/opt/beta/repo/app/main.py", diff="+beta"),
                ],
            ),
            _call(
                "write",
                seq=4,
                arguments={"workdir": r"c:\users\dev\src\windows-repo"},
                diffs=[FileDiffRef(path="APP/main.py", diff="+windows-relative")],
            ),
        ],
        [
            _tool("apply_patch", read_only=False),
            _tool("write", read_only=False),
        ],
        complete_files=True,
    )

    diffs = [diff for mutation in projection.mutations for diff in mutation.file_diffs]
    by_preview = {diff.diff: diff for diff in diffs}
    assert by_preview["+absolute"].path == ("src/cognis-hotfix/ui/src/routes/a/+page.svelte")
    assert by_preview["+sibling"].path == ("src/cognis-hotfix/ui/src/routes/b/+page.svelte")
    assert by_preview["+absolute"].path_id == by_preview["+relative"].path_id
    assert by_preview["+absolute"].root_name == "src"
    assert by_preview["+absolute"].root_id == by_preview["+relative"].root_id
    assert by_preview["+absolute"].path != by_preview["+sibling"].path
    assert by_preview["+mac"].path == "src/other-repo/app/main.py"
    assert by_preview["+windows"].path == "src/windows-repo/app/main.py"
    assert by_preview["+windows"].path_id == by_preview["+windows-relative"].path_id
    assert by_preview["+alpha"].path == "alpha/repo/app/main.py"
    assert by_preview["+beta"].path == "beta/repo/app/main.py"
    assert (
        len(
            {
                by_preview["+mac"].root_id,
                by_preview["+windows"].root_id,
                by_preview["+alpha"].root_id,
                by_preview["+beta"].root_id,
            }
        )
        == 4
    )
    serialized = projection.model_dump_json()
    assert "/home/riker" not in serialized
    assert "/Users/alice" not in serialized
    assert "C:/Users/Dev" not in serialized


def test_shallow_home_paths_use_generic_labels_without_usernames() -> None:
    projection = _project(
        [
            _call(
                "apply_patch",
                seq=1,
                diffs=[
                    FileDiffRef(path="/home/riker/file.py", diff="+linux"),
                    FileDiffRef(path="/Users/alice/file.py", diff="+mac"),
                    FileDiffRef(path=r"C:\Users\Dev\file.py", diff="+windows"),
                ],
            )
        ],
        [_tool("apply_patch", read_only=False)],
        complete_files=True,
    )

    diffs = projection.mutations[0].file_diffs
    assert [diff.path for diff in diffs] == ["~/file.py", "~/file.py", "~/file.py"]
    assert all(diff.root_label == "~" for diff in diffs)
    assert len({diff.root_id for diff in diffs}) == 3
    serialized = projection.model_dump_json()
    for username in ("riker", "alice", "Dev"):
        assert username not in serialized


def test_complete_files_lexically_normalize_equivalent_absolute_and_relative_paths() -> None:
    projection = _project(
        [
            _call(
                "apply_patch",
                seq=1,
                diffs=[
                    FileDiffRef(path="/repo/a.py", diff="+plain"),
                    FileDiffRef(path="/repo/dir/../a.py", diff="+parent"),
                    FileDiffRef(path="/repo//./a.py", diff="+separators"),
                    FileDiffRef(path=r"C:\Repo\dir\..\a.py", diff="+windows"),
                    FileDiffRef(path="c:/repo//./A.py", diff="+windows-case"),
                    FileDiffRef(path=r"C:\Repo\..\outside.py", diff="+windows-root"),
                    FileDiffRef(
                        path=r"C:\Repo\..\..\outside.py",
                        diff="+windows-beyond-root",
                    ),
                    FileDiffRef(
                        path=r"c:\\repo\\..\\.\\outside.py",
                        diff="+windows-repeated",
                    ),
                    FileDiffRef(path=r"C:\outside.py", diff="+windows-direct"),
                    FileDiffRef(path="/repo/../../outside.py", diff="+outside"),
                    FileDiffRef(path="outside.py", diff="+relative-outside"),
                ],
            ),
            _call(
                "write",
                seq=2,
                arguments={"workdir": "/repo"},
                diffs=[FileDiffRef(path="dir/../a.py", diff="+relative")],
            ),
            _call(
                "write",
                seq=3,
                arguments={"workdir": r"C:\Repo"},
                diffs=[FileDiffRef(path=r"dir\..\A.py", diff="+windows-relative")],
            ),
        ],
        [
            _tool("apply_patch", read_only=False),
            _tool("write", read_only=False),
        ],
        complete_files=True,
    )

    by_preview = {
        diff.diff: diff for mutation in projection.mutations for diff in mutation.file_diffs
    }
    unix_ids = {
        by_preview[key].path_id for key in ("+plain", "+parent", "+separators", "+relative")
    }
    windows_ids = {
        by_preview[key].path_id for key in ("+windows", "+windows-case", "+windows-relative")
    }
    assert len(unix_ids) == 1
    assert len(windows_ids) == 1
    assert {by_preview[key].path for key in ("+plain", "+parent", "+separators", "+relative")} == {
        "repo/a.py"
    }
    assert {by_preview[key].path for key in ("+windows", "+windows-case", "+windows-relative")} == {
        "Repo/a.py",
    }
    drive_root_ids = {
        by_preview[key].path_id
        for key in (
            "+windows-root",
            "+windows-beyond-root",
            "+windows-repeated",
            "+windows-direct",
        )
    }
    assert len(drive_root_ids) == 1
    assert {
        by_preview[key].path
        for key in (
            "+windows-root",
            "+windows-beyond-root",
            "+windows-repeated",
            "+windows-direct",
        )
    } == {"outside.py"}
    assert all(
        by_preview[key].root_label != "Unscoped"
        for key in (
            "+windows-root",
            "+windows-beyond-root",
            "+windows-repeated",
            "+windows-direct",
        )
    )
    assert by_preview["+relative-outside"].root_label == "Unscoped"
    assert by_preview["+relative-outside"].path_id not in drive_root_ids
    assert by_preview["+outside"].path_id not in unix_ids
    assert by_preview["+outside"].root_id != by_preview["+plain"].root_id


def test_legacy_combined_projection_keeps_nested_workdir_identity() -> None:
    projection = _project(
        [
            _call(
                "bash",
                seq=1,
                arguments={"command": "pwd", "workdir": "/repo"},
            ),
            _call(
                "bash",
                seq=2,
                arguments={"command": "pwd", "workdir": "/repo/packages/app"},
            ),
            _call(
                "write",
                seq=3,
                diffs=[FileDiffRef(path="/repo/packages/app/src/main.py", diff="+nested")],
            ),
        ],
        [
            _tool("bash", read_only=False, category="shell"),
            _tool("write", read_only=False),
        ],
    )

    diff = projection.mutations[0].file_diffs[0]
    assert diff.path == "app/src/main.py"
    assert diff.root_name == "app"
    assert diff.relative_path == "src/main.py"


def test_relative_parent_paths_remain_unscoped_and_do_not_alias_safe_paths() -> None:
    projection = _project(
        [
            _call(
                "apply_patch",
                seq=1,
                diffs=[
                    FileDiffRef(path="../a.py", diff="+parent"),
                    FileDiffRef(path="a.py", diff="+safe"),
                ],
            )
        ],
        [_tool("apply_patch", read_only=False)],
        complete_files=True,
    )

    parent, safe = projection.mutations[0].file_diffs
    assert parent.path == "a.py"
    assert parent.root_id is None
    assert safe.path == "Unscoped/a.py"
    assert safe.root_label == "Unscoped"
    assert parent.path_id != safe.path_id


def test_projection_emits_explicit_rooted_and_unbound_relative_identities() -> None:
    path = "/srv/authorized/repo/src/app.py"
    with_root = _project(
        [
            _call(
                "bash",
                seq=1,
                arguments={"command": "pwd", "workdir": "/srv/authorized/repo"},
            ),
            _call("write", seq=2, diffs=[FileDiffRef(path=path, diff="+one")]),
        ],
        [
            _tool("bash", read_only=False, category="shell"),
            _tool("write", read_only=False),
        ],
    )
    without_root = _project(
        [_call("write", seq=2, diffs=[FileDiffRef(path="src/app.py", diff="+one")])],
        [_tool("write", read_only=False)],
    )

    rooted = with_root.mutations[0].file_diffs[0]
    unrooted = without_root.mutations[0].file_diffs[0]
    assert rooted.path == "repo/src/app.py"
    assert rooted.root_id is not None
    assert rooted.root_label == "repo"
    assert rooted.relative_path == "src/app.py"
    assert rooted.path_id == f"{rooted.root_id}:src/app.py"
    assert unrooted.path == "Unscoped/src/app.py"
    assert unrooted.root_id is None
    assert unrooted.root_label == "Unscoped"
    assert unrooted.relative_path == "src/app.py"
    assert unrooted.path_id.startswith("unbound:")
    assert "/srv/authorized" not in with_root.model_dump_json()
    assert "/srv/authorized" not in without_root.model_dump_json()


def test_file_diffs_require_completed_meaningful_file_mutations() -> None:
    complete = _call(
        "write",
        seq=1,
        diffs=[FileDiffRef(path="complete.py", diff="+complete")],
    )
    running = _call(
        "write",
        seq=2,
        result=None,
        diffs=[FileDiffRef(path="running.py", diff="+running")],
    ).model_copy(update={"status": "running"})
    failed = _call(
        "write",
        seq=3,
        is_error=True,
        diffs=[FileDiffRef(path="failed.py", diff="+failed")],
    )
    read_only = _call(
        "read",
        seq=4,
        diffs=[FileDiffRef(path="read.py", diff="+read")],
    )
    control = _call(
        "attach_artifact",
        seq=5,
        diffs=[FileDiffRef(path="control.py", diff="+control")],
    )
    projection = _project(
        [complete, running, failed, read_only, control],
        [
            _tool("write", read_only=False),
            _tool("read", read_only=True),
            _tool("attach_artifact", read_only=False, category="artifact"),
        ],
    )

    assert [event.call_id for event in projection.mutations] == ["call-1"]
    assert projection.summary.changed_files == 1


def test_deliverable_content_is_an_explicit_recoverable_preview() -> None:
    markdown = "markdown " * 8_000
    rich_fallback = "rich fallback " * 8_000
    projection = _project(
        [
            AssistantDeliverableTimelineItem(
                id="deliverable:markdown",
                deliverable_id="markdown",
                format="markdown",
                content=markdown,
                sort_key="0001",
                source_refs=[_source(1)],
            ),
            AssistantDeliverableTimelineItem(
                id="deliverable:rich",
                deliverable_id="rich",
                format="rich",
                content=rich_fallback,
                render_metadata={"payload": {"blocks": [{"content": "x" * 20_000}]}},
                sort_key="0002",
                source_refs=[_source(2)],
            ),
        ],
        [],
    )

    assert [item.deliverable_id for item in projection.deliverables] == [
        "markdown",
        "rich",
    ]
    for deliverable in projection.deliverables:
        assert deliverable.content_preview_truncated is True
        assert deliverable.recoverable is True
        assert len(deliverable.content or "") <= 4_000
    assert "x" * 1_000 not in projection.deliverables[1].model_dump_json()


def test_artifacts_deduplicate_by_id_in_canonical_order() -> None:
    projection = _project(
        [
            ArtifactTimelineItem(
                id="artifact:first",
                artifact_id="same",
                filename="old.txt",
                sort_key="0001",
                source_refs=[_source(1)],
            ),
            ArtifactTimelineItem(
                id="artifact:other",
                artifact_id="other",
                filename="other.txt",
                sort_key="0002",
                source_refs=[_source(2)],
            ),
            ArtifactTimelineItem(
                id="artifact:new",
                artifact_id="same",
                filename="new.txt",
                sort_key="0003",
                source_refs=[_source(3)],
            ),
        ],
        [],
    )

    assert [(item.artifact_id, item.filename) for item in projection.artifacts] == [
        ("other", "other.txt"),
        ("same", "new.txt"),
    ]
    assert projection.summary.artifacts == 2


def test_page_projection_has_bounded_aggregate_preview_and_file_stat_budget() -> None:
    calls = [
        _call(
            "write",
            seq=index,
            arguments={
                "description": "description " * 2_000,
                "target": [{"safe": "value " * 500} for _ in range(100)],
            },
            result="output " * 4_000,
            diffs=[
                FileDiffRef(
                    path=f"/repo-{index}/src/file-{file_index}.py",
                    diff=("+added\n-removed\n" * 4_000),
                )
                for file_index in range(20)
            ],
        )
        for index in range(1, 51)
    ]
    projection = _project(calls, [_tool("write", read_only=False)])
    serialized = projection.model_dump_json().encode("utf-8")

    assert len(serialized) < 512_000
    assert (
        sum(
            len(diff.diff.encode("utf-8"))
            for event in projection.mutations
            for diff in event.file_diffs
        )
        < 100_000
    )
    assert sum(len(event.file_stats) for event in projection.mutations) == 200
    assert sum(event.omitted_file_stat_count for event in projection.mutations) == 800
    assert any(event.file_stats_recoverable for event in projection.mutations)


def test_external_mutation_classification_handles_metadata_camel_case_and_failures() -> None:
    definition = ToolDefinition(
        name="gmail",
        description="Gmail",
        source=ToolSource(type="local_mcp", raw_tool_name="sendGmailMessage"),
        category="mcp",
        read_only=False,
        native_operations=[
            NativeToolOperation(
                operation="send",
                summary="Send",
                mutation_kind=ToolMutationKind.CREATE,
                input_schema={"type": "object"},
                semantics=declared_default_semantics(ToolMutationKind.CREATE),
                examples=[{"operation": "send"}],
            ),
            NativeToolOperation(
                operation="search",
                summary="Search",
                mutation_kind=ToolMutationKind.READ,
                input_schema={"type": "object"},
                semantics=declared_default_semantics(ToolMutationKind.READ),
                examples=[{"operation": "search"}],
            ),
        ],
    )
    projection = _project(
        [
            _call("gmail", seq=1, arguments={"operation": "send"}),
            _call("gmail", seq=2, arguments={"operation": "search"}),
            _call("gmail", seq=3, arguments={"operation": "ambiguous"}),
            _call("gmail", seq=4, arguments={"operation": "send"}, is_error=True),
            _call("sendGmailMessage", seq=5),
            _call("sendGmailMessage", seq=6, is_error=True),
            _call("searchGmailMessages", seq=7),
        ],
        [definition],
    )

    assert [event.call_id for event in projection.mutations] == ["call-1", "call-5"]
    assert all(event.status == "complete" for event in projection.mutations)
