"""Per-dispatch digests of scripts a shell command will run; never a decision.

When a bash command invokes an interpreter on a file, the bytes that execute
are not in the command. This resolves the file's SHA-256 on the selected
executor immediately before dispatch so guardrails can compare it with a
clause that names a hash. It is a pre-execution snapshot, not an attestation
of what later executes: the file can change between the read and the run,
and the clause's trust in the hash is bounded by that window. The executor
reads the exact path without following a final symlink and refuses
non-regular or oversized files. A missing or failed digest is unknown, never
permission; a malformed executor reply discards the whole observation; no
result is cached.

Script identification is conservative. Only the single script operand of a
recognised interpreter in a plain positional form is hashed; interpreter modes
that execute something other than that file (`-c`, `-m`, `-e`, `--eval`,
stdin `-`, heredocs) and shells (`bash x.sh` is fine, `bash -c` is not) make
the segment unknown. A wrong file must never be hashed; skipping is safe.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from typing import Any

from cognis.guardrails_redaction import redact_runtime_evidence

# Interpreters whose first positional operand is the script file, with the
# flags that are KNOWN to leave that true (a closed allow-list per
# interpreter). Any other flag, including combined or attached forms such as
# `-c'code'`, `-Wignore` or `-mpkg`, makes the segment unknown: an unknown
# flag may change what executes, and skipping is safe while guessing is not.
_SAFE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-B", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v"}),
    "node": frozenset({"--no-warnings", "--no-deprecation", "--trace-warnings"}),
    "deno": frozenset({"run", "--quiet", "-q"}),
    "bun": frozenset({"run"}),
    "ruby": frozenset({"-w", "-W0", "-W1", "-W2"}),
    "perl": frozenset({"-w", "-W", "-T", "-t"}),
    "php": frozenset({"-q", "-n"}),
    "bash": frozenset({"-e", "-u", "-x", "-o", "pipefail"}),
    "sh": frozenset({"-e", "-u", "-x"}),
    "zsh": frozenset({"-e", "-u", "-x"}),
}
# Flags that consume the next token as a value that is NOT the script, and
# are known not to change what executes.
_SAFE_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-W", "-X"}),
    "bash": frozenset({"-o"}),
    "sh": frozenset({"-o"}),
    "zsh": frozenset({"-o"}),
}
_SCRIPT_SUFFIX = re.compile(r"\.(py|mjs|cjs|js|ts|rb|pl|php|sh|bash|zsh)$")
_HEREDOC = re.compile(r"<<-?\s*['\"]?\w")
_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
# Executor per-path statuses. Anything else is a contract violation.
_KNOWN_STATUSES = frozenset(
    {"ok", "missing", "symlink", "not_regular", "too_large", "unreadable", "unsupported"}
)
MAX_PATHS = 8


def _script_operand(tokens: list[str]) -> str | None:
    """The single script file a recognised interpreter invocation runs, or None."""
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    head = tokens[0].rsplit("/", 1)[-1]
    key = "python" if re.fullmatch(r"python[0-9.]*", head) else head
    safe = _SAFE_FLAGS.get(key)
    if safe is None:
        return None
    safe_value = _SAFE_VALUE_FLAGS.get(key, frozenset())
    rest = tokens[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            index += 1
            break
        if token in safe:
            index += 1
            continue
        if token in safe_value:
            index += 2
            continue
        if token.startswith("-"):
            # Unknown, mode-changing, or attached-form flag: not a plain run.
            return None
        break
    if index >= len(rest):
        return None
    operand = rest[index]
    if operand.startswith("/") and _SCRIPT_SUFFIX.search(operand):
        return operand
    return None


def _segments(command: str) -> list[list[str]]:
    """Quote-aware simple-command segments, or [] if the command cannot be read.

    ``shlex`` with ``punctuation_chars`` keeps shell operators as their own
    tokens while honouring quotes, but it also strips the quotes, so a quoted
    operator (``';'``) comes out identical to a real one. Rather than track
    quote provenance per token, any quoted or escaped operator character in the
    raw command makes the whole command unknown. Redirections and subshell
    syntax are not modelled: a segment containing them is dropped.
    """
    if _quoted_operator(command):
        return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            segments.append([])
        elif set(token) <= set(";&|"):
            # Operator run shlex did not fold (e.g. ';;'): treat as separator.
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s and not any(_REDIRECT.search(t) for t in s)]


_REDIRECT = re.compile(r"^[<>]|^\d*[<>]|\$\(|^`|\($")


def _quoted_operator(command: str) -> bool:
    """True if any ``;``, ``&`` or ``|`` sits inside quotes or after a backslash."""
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            if char in ";&|":
                return True
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is None:
            if char in "'\"":
                quote = char
        elif char == quote:
            quote = None
        elif char in ";&|":
            return True
    return quote is not None


def executed_script_paths(command: str) -> list[str]:
    """Absolute script paths an interpreter in ``command`` will run, in order.

    Any heredoc in the command makes the whole command unknown. Segmentation is
    quote-aware; a segment with redirection or substitution syntax is skipped.
    """
    if _HEREDOC.search(command):
        return []
    found: list[str] = []
    for tokens in _segments(command):
        operand = _script_operand(tokens)
        if operand and operand not in found:
            found.append(operand)
    return found[:MAX_PATHS]


async def executed_script_evidence(
    executor: Any,
    *,
    command: Any,
    user_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Digest the scripts ``command`` runs on the selected executor, if any."""
    if not isinstance(command, str) or not command:
        return None
    paths = executed_script_paths(command)
    if not paths:
        return None
    executor_id = getattr(executor, "executor_id", None)
    evidence: dict[str, Any] = {
        "coverage": "unknown",
        "source": "executor_file_digest",
        "authority": "context_only",
        "user_id": user_id,
        "session_id": session_id,
        "executor_id": executor_id if isinstance(executor_id, str) else None,
        "paths": paths,
    }
    lookup = getattr(executor, "file_digests", None)
    if not callable(lookup) or not isinstance(executor_id, str) or not executor_id:
        return {**evidence, "reason": "lookup_unavailable"}
    try:
        result = await asyncio.wait_for(lookup(paths=paths), timeout=5.0)
    except Exception:
        return {**evidence, "reason": "lookup_unavailable"}
    digests = result.get("digests") if isinstance(result, dict) else None
    if not isinstance(digests, dict):
        return {**evidence, "reason": "invalid_result"}
    # Records, not path-keyed dicts: the guardrails context projection bounds
    # dict keys to 120 characters, and managed asset paths are longer than
    # that. A path as a value is never truncated below the string limit.
    verified: list[dict[str, Any]] = []
    unverified: list[dict[str, str]] = []
    for path in paths:
        row = digests.get(path)
        status = row.get("status") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or not isinstance(status, str)
            or status not in _KNOWN_STATUSES
        ):
            # A reply that does not follow the executor contract is not partial
            # evidence; none of it can be trusted to be bound to these paths.
            return {**evidence, "reason": "invalid_result"}
        if status == "ok":
            sha = row.get("sha256")
            size = row.get("size_bytes")
            if (
                not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                return {**evidence, "reason": "invalid_result"}
            verified.append({"path": path, "sha256": sha, "size_bytes": size})
        else:
            unverified.append({"path": path, "status": str(status)})
    redacted: dict[str, Any] = redact_runtime_evidence(
        {
            **evidence,
            "coverage": "complete" if not unverified else "partial",
            "verified": verified,
            "unverified": unverified,
        }
    )
    return redacted
