"""Read-only SHA-256 of a regular file on this executor; never an authorization.

The controller asks for this before dispatching a command that runs a script,
so guardrails can compare a pre-execution snapshot of the bytes against a
clause that names a hash. It is a snapshot taken at dispatch, not an
attestation of what later executes: the file can change between this read
and the run. It opens the exact path without following a final symlink and
without blocking on a FIFO, refuses anything that is not a regular file, and
caps the bytes it will read even if the file grows during the read. It
performs no writes, no retries and no caching.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import stat
from typing import Any

MAX_DIGEST_BYTES = 32 * 1024 * 1024
MAX_PATHS = 8
_CHUNK = 1024 * 1024


async def file_digests(params: dict[str, Any]) -> dict[str, Any]:
    """Digest each requested absolute path; per-path outcome, no exceptions."""
    paths = params.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_PATHS:
        raise ValueError("file digest requires 1 to 8 absolute paths")
    if not all(isinstance(path, str) and os.path.isabs(path) for path in paths):
        raise ValueError("file digest paths must be absolute strings")
    results = await asyncio.gather(*(asyncio.to_thread(_digest_one, path) for path in paths))
    return {"digests": dict(zip(paths, results, strict=True))}


def _digest_one(path: str) -> dict[str, Any]:
    if os.name != "posix":
        return {"status": "unsupported"}
    # O_NONBLOCK makes opening a FIFO return immediately instead of waiting for
    # a writer; fstat then rejects it. Regular files ignore the flag.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as exc:
        return {"status": "symlink" if exc.errno == errno.ELOOP else "unreadable"}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return {"status": "not_regular"}
        if info.st_size > MAX_DIGEST_BYTES:
            return {"status": "too_large", "size_bytes": info.st_size}
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DIGEST_BYTES:
                # Grew past the cap while being read: not a stable artifact.
                return {"status": "too_large", "size_bytes": total}
            digest.update(chunk)
        return {"status": "ok", "sha256": digest.hexdigest(), "size_bytes": total}
    except OSError:
        return {"status": "unreadable"}
    finally:
        os.close(descriptor)
