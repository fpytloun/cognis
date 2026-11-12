"""Per-dispatch script digests: read-only evidence, never permission."""

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.executed_script_evidence import (
    executed_script_evidence,
    executed_script_paths,
)
from cognis.executor.file_digest import MAX_DIGEST_BYTES, file_digests

SKILL = "/home/user/.cognis/skill_assets/" + "a" * 64 + "/" + "b" * 64 + "/" + "c" * 64


@pytest.mark.parametrize(
    "command,expected",
    [
        (
            f"'{SKILL}/python-env/bin/python' -B '{SKILL}/tandem_control.py'",
            [f"{SKILL}/tandem_control.py"],
        ),
        ("python3 /tmp/report.py --flag", ["/tmp/report.py"]),
        ("node /repo/tools/inspect.mjs && git status", ["/repo/tools/inspect.mjs"]),
        ("FOO=1 BAR=2 python3 /srv/x.py", ["/srv/x.py"]),
        ("cd /repo && python3 /repo/a.py; python3 /repo/b.py", ["/repo/a.py", "/repo/b.py"]),
        ("python3 relative.py", []),
        ("python3 -c 'print(1)'", []),
        ("uv run pytest tests", []),
        ("bash -c 'echo hi'", []),
        ("ls -la /tmp", []),
        ("python3 - <<'PY'\nprint(1)\nPY", []),
        ("python3 /tmp/notascript", []),
        # Interpreter modes where the first positional is NOT the script.
        ("python3 -m pytest /tmp/test_x.py", []),
        ("python3 -c 'import sys' /tmp/x.py", []),
        ("node -e 'x()' /tmp/y.mjs", []),
        ("node --eval 'x()' /tmp/y.mjs", []),
        ("node -r /tmp/preload.js /tmp/main.js", []),  # preload alters what runs
        ("python3 -W ignore /tmp/x.py", ["/tmp/x.py"]),
        ("python3 -X dev /tmp/x.py", ["/tmp/x.py"]),
        ("python3 -- /tmp/x.py", ["/tmp/x.py"]),
        ("python3 /tmp/x.py -- /tmp/other.py", ["/tmp/x.py"]),
        ("bash /tmp/run.sh", ["/tmp/run.sh"]),
        ("bash -s < /tmp/run.sh", []),
        ("uv run /tmp/x.py", []),  # uv is not a plain interpreter
        ("python3 /tmp/x.py <<EOF\nin\nEOF", []),  # any heredoc: skip
        ("cat /tmp/x.py | python3", []),
        ("python3.12 /tmp/x.py", ["/tmp/x.py"]),
        # Attached / combined option forms: not a plain run, never guess.
        ("python3 -c'print(1)' /tmp/report.py", []),
        ("python3 -Wignore /tmp/x.py", []),
        ("python3 -mpytest /tmp/x.py", []),
        ("python3 -BX dev /tmp/x.py", []),
        ("python3 --unknown-flag /tmp/x.py", []),
        # Operators inside quotes are text, not segment boundaries.
        ("printf '%s\\n' 'text; python3 /tmp/report.py; text'", []),
        ("echo 'a && python3 /tmp/x.py'", []),
        ('echo "a | python3 /tmp/x.py"', []),
        # A quoted standalone operator is indistinguishable after shlex strips
        # quotes; any quoted/escaped operator makes the whole command unknown.
        ("printf '%s\\n' ';' python3 /tmp/report.py", []),
        ("printf '%s' '&' python3 /tmp/report.py", []),
        ('echo "|" ; python3 /tmp/x.py', []),
        ("echo \\; python3 /tmp/x.py", []),
        ("echo 'unterminated ; python3 /tmp/x.py", []),
        # Quotes that contain no operator are fine.
        ("echo 'hello' ; python3 /tmp/x.py", ["/tmp/x.py"]),
        # Real operators still separate.
        ("echo a; python3 /tmp/x.py", ["/tmp/x.py"]),
        ("python3 /tmp/x.py & python3 /tmp/y.py", ["/tmp/x.py", "/tmp/y.py"]),
        # Redirection and substitution make the segment unknown.
        ("python3 /tmp/x.py > /tmp/out", []),
        ("python3 /tmp/x.py 2>&1", []),
        ("python3 $(echo /tmp/x.py)", []),
        ('python3 "$(cat /tmp/path)"', []),
        # Safe known flags still allow the run.
        ("bash -e -u /tmp/run.sh", ["/tmp/run.sh"]),
        ("bash -o pipefail /tmp/run.sh", ["/tmp/run.sh"]),
        ("node --no-warnings /tmp/m.mjs", ["/tmp/m.mjs"]),
    ],
)
def test_executed_script_paths(command, expected):
    assert executed_script_paths(command) == expected


def test_executed_script_paths_is_bounded():
    command = " ; ".join(f"python3 /tmp/s{i}.py" for i in range(20))
    assert len(executed_script_paths(command)) == 8


async def evidence(executor, command="python3 /tmp/report.py"):
    return await executed_script_evidence(
        executor,
        command=command,
        user_id="owner@example.com",
        session_id="session_x",
    )


@pytest.mark.asyncio
async def test_verified_digest_is_reported_with_provenance():
    executor = SimpleNamespace(
        executor_id="executor_a",
        file_digests=AsyncMock(
            return_value={
                "digests": {
                    "/tmp/report.py": {"status": "ok", "sha256": "f" * 64, "size_bytes": 10}
                }
            }
        ),
    )
    result = await evidence(executor)
    assert result["coverage"] == "complete"
    assert result["source"] == "executor_file_digest"
    assert result["authority"] == "context_only"
    assert result["executor_id"] == "executor_a"
    assert result["verified"] == [{"path": "/tmp/report.py", "sha256": "f" * 64, "size_bytes": 10}]
    assert result["unverified"] == []
    executor.file_digests.assert_awaited_once_with(paths=["/tmp/report.py"])


@pytest.mark.asyncio
async def test_long_asset_paths_survive_the_guardrails_context_projection():
    """Paths are values, not dict keys: the 120-char key bound cannot merge them."""
    from cognis.core.tool_router import _guardrails_context_value

    a = f"{SKILL}/tool_a.py"
    b = f"{SKILL}/tool_b.py"
    assert len(a) > 120 and a[:120] == b[:120]
    executor = SimpleNamespace(
        executor_id="executor_a",
        file_digests=AsyncMock(
            return_value={
                "digests": {
                    a: {"status": "ok", "sha256": "1" * 64, "size_bytes": 1},
                    b: {"status": "ok", "sha256": "2" * 64, "size_bytes": 2},
                }
            }
        ),
    )
    result = await evidence(executor, command=f"python3 {a}; python3 {b}")
    projected = _guardrails_context_value(result)
    assert [row["path"] for row in projected["verified"]] == [a, b]
    assert {row["sha256"] for row in projected["verified"]} == {"1" * 64, "2" * 64}


@pytest.mark.asyncio
async def test_no_interpreter_script_yields_no_evidence_and_no_lookup():
    executor = SimpleNamespace(executor_id="executor_a", file_digests=AsyncMock())
    assert await evidence(executor, command="git status") is None
    assert await evidence(executor, command=None) is None
    executor.file_digests.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["missing", "symlink", "not_regular", "too_large", "unreadable"])
async def test_non_ok_status_is_unverified_not_a_digest(status):
    executor = SimpleNamespace(
        executor_id="executor_a",
        file_digests=AsyncMock(return_value={"digests": {"/tmp/report.py": {"status": status}}}),
    )
    result = await evidence(executor)
    assert result["coverage"] == "partial"
    assert result["verified"] == []
    assert result["unverified"] == [{"path": "/tmp/report.py", "status": status}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {"status": "ok", "sha256": "NOT-HEX", "size_bytes": 1},
        {"status": "ok", "sha256": "f" * 64},  # missing size
        {"status": "ok", "sha256": "f" * 64, "size_bytes": True},
        {"status": "ok", "sha256": "f" * 64, "size_bytes": -1},
        {"status": "weird"},
        {"status": []},  # non-string status must not raise
        {"status": {}},
        "not a dict",
        None,  # path absent from reply
    ],
)
async def test_contract_violation_discards_the_whole_observation(row):
    """A reply off-contract is not partial evidence; nothing in it is bound."""
    digests = {} if row is None else {"/tmp/report.py": row}
    executor = SimpleNamespace(
        executor_id="executor_a",
        file_digests=AsyncMock(return_value={"digests": digests}),
    )
    result = await evidence(executor)
    assert result["coverage"] == "unknown"
    assert result["reason"] == "invalid_result"
    assert "verified" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "executor",
    [
        SimpleNamespace(executor_id="executor_a"),  # no file_digests capability
        SimpleNamespace(executor_id=None, file_digests=AsyncMock()),  # no identity
        SimpleNamespace(executor_id="executor_a", file_digests=AsyncMock(side_effect=OSError("x"))),
        SimpleNamespace(executor_id="executor_a", file_digests=AsyncMock(return_value="junk")),
    ],
)
async def test_lookup_failure_is_unknown_never_permission(executor):
    result = await evidence(executor)
    assert result["coverage"] == "unknown"
    assert result["reason"] in {"lookup_unavailable", "invalid_result"}
    assert "verified" not in result


@pytest.mark.asyncio
async def test_executor_file_digest_reads_regular_files_only(tmp_path):
    target = tmp_path / "tool.py"
    target.write_bytes(b"print('ok')\n")
    link = tmp_path / "link.py"
    os.symlink(target, link)
    directory = tmp_path / "dir.py"
    directory.mkdir()
    big = tmp_path / "big.py"
    with big.open("wb") as stream:
        stream.truncate(MAX_DIGEST_BYTES + 1)
    result = await file_digests(
        {"paths": [str(target), str(link), str(directory), str(big), str(tmp_path / "nope.py")]}
    )
    digests = result["digests"]
    assert digests[str(target)] == {
        "status": "ok",
        "sha256": hashlib.sha256(b"print('ok')\n").hexdigest(),
        "size_bytes": 12,
    }
    assert digests[str(link)]["status"] == "symlink"
    assert digests[str(directory)]["status"] == "not_regular"
    assert digests[str(big)]["status"] == "too_large"
    assert digests[str(tmp_path / "nope.py")]["status"] == "missing"


@pytest.mark.asyncio
async def test_executor_file_digest_does_not_block_on_a_fifo(tmp_path):
    """A FIFO with no writer must be rejected immediately, not hang the thread."""
    import asyncio

    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    result = await asyncio.wait_for(file_digests({"paths": [str(fifo)]}), timeout=2.0)
    assert result["digests"][str(fifo)]["status"] == "not_regular"


@pytest.mark.asyncio
async def test_executor_file_digest_caps_a_file_that_grows_during_read(tmp_path, monkeypatch):
    """stat passes, bytes keep coming: the streaming cap still refuses it."""
    import cognis.executor.file_digest as module

    target = tmp_path / "grow.py"
    target.write_bytes(b"x" * 10)
    real_read = os.read
    served = {"n": 0}

    def endless(fd, n):
        served["n"] += 1
        return b"y" * n if served["n"] < 10_000 else b""

    monkeypatch.setattr(module.os, "read", endless)
    try:
        result = await file_digests({"paths": [str(target)]})
    finally:
        monkeypatch.setattr(module.os, "read", real_read)
    assert result["digests"][str(target)]["status"] == "too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [[], ["relative.py"], [1], ["/a"] * 9, None])
async def test_executor_file_digest_rejects_bad_requests(paths):
    with pytest.raises(ValueError):
        await file_digests({"paths": paths})


def test_fs_digest_is_replay_safe_and_in_process_exposes_it():
    from cognis.executor.unary_dedup import is_replay_safe_unary_method
    from cognis.providers.executor.in_process import InProcessExecutorConnection

    assert is_replay_safe_unary_method("fs.digest")
    assert callable(getattr(InProcessExecutorConnection, "file_digests", None))
    assert isinstance(getattr(InProcessExecutorConnection, "executor_id", None), property)
