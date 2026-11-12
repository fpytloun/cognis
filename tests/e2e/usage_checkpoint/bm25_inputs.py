"""Stdlib-only verification for a pinned BM25 data snapshot.

No Hub metadata, model weights, cache discovery, installer or archive extraction.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REVISION = "22b8d2af71a76161e18dd432d2cee0eefa66e412"
ARCHIVE_SHA256 = "9a2ec963d4240c6958b0340f78a622e6042ebf14d42685beca366c2f3b4d8b27"
MANIFEST_SHA256 = "d8351b3c97e0863673788c8d502c2edb0323b7bafe1f05a133b9312985628e11"
REPOSITORY = "models--Qdrant--bm25"
SNAPSHOT = f"{REPOSITORY}/snapshots/{REVISION}"
REF = f"{REPOSITORY}/refs/main"
NAMES = frozenset(
    {
        "config.json",
        "arabic.txt",
        "danish.txt",
        "dutch.txt",
        "english.txt",
        "finnish.txt",
        "french.txt",
        "german.txt",
        "greek.txt",
        "hungarian.txt",
        "italian.txt",
        "norwegian.txt",
        "portuguese.txt",
        "romanian.txt",
        "russian.txt",
        "spanish.txt",
        "swedish.txt",
        "turkish.txt",
    }
)


def sha256(value: bytes) -> str:
    """Hash bytes, never an untrusted manifest's assertion of its own identity."""
    return hashlib.sha256(value).hexdigest()


def strict_json(value: bytes) -> dict:
    """Reject duplicate keys as well as malformed JSON."""

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    result = json.loads(value, object_pairs_hook=pairs)
    if not isinstance(result, dict):
        raise ValueError("Expected JSON object")
    return result


def directory(path: Path, *, uid: int, private: bool = False) -> os.stat_result:
    """Check the declared directory without accepting symlink ancestors."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Input path must be absolute and normalized")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Input directory contains a link or non-directory")
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != uid or mode & 0o022 or (private and mode != 0o700):
        raise ValueError("Input directory ownership/mode mismatch")
    return info


def read_file(
    path: Path,
    *,
    uid: int,
    maximum: int,
    mode: int | None = None,
    executable: bool = False,
) -> bytes:
    """Open every component no-follow; hold the checked regular file while reading."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Input file path must be absolute and normalized")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            bits = stat.S_IMODE(before.st_mode)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != uid
                or before.st_nlink != 1
                or bits & 0o7022
                or (mode is not None and bits != mode)
                or (executable and not bits & 0o100)
                or before.st_size > maximum
            ):
                raise ValueError("Input file type/ownership/mode/size mismatch")
            value = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if (
                len(value) != before.st_size
                or len(value) > maximum
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError("Input file changed while reading")
            return value
    finally:
        os.close(fd)


@dataclass(frozen=True)
class BM25Data:
    """Held, authenticated input bytes and the exact supported cache payload."""

    archive: bytes
    manifest: bytes
    payload: tuple[tuple[str, bytes], ...]


def inspect_archive(archive: bytes, manifest: bytes) -> tuple[tuple[str, bytes], ...]:
    """Inspect held bytes; the outer reader authenticates both whole-file hashes."""
    document = strict_json(manifest)
    if (
        document.get("repository") != "Qdrant/bm25"
        or document.get("revision") != REVISION
        or document.get("cache_root") != "/work/data/tmp/fastembed_cache"
    ):
        raise ValueError("Unsupported BM25 identity")
    entries = document.get("files", [])
    if len(entries) != len(NAMES) or {entry["path"] for entry in entries} != NAMES:
        raise ValueError("Unsupported BM25 file allowlist")
    expected = {f"{SNAPSHOT}/{entry['path']}": entry for entry in entries}
    expected[REF] = {"size": len(REVISION), "sha256": sha256(REVISION.encode())}
    directories = {
        str(parent)
        for name in expected
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    payload = {}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        members = source.getmembers()
        if len(members) != len(expected) + len(directories):
            raise ValueError("Unexpected BM25 archive member count")
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.as_posix() != member.name
                or member.name not in expected.keys() | directories
                or member.name in seen
                or member.issym()
                or member.islnk()
                or member.pax_headers
                or member.linkname
                or member.issparse()
                or member.uid != 1001
                or member.gid != 1001
            ):
                raise ValueError("Unsafe/extra/duplicate BM25 archive member")
            seen.add(member.name)
            if member.name in directories:
                if not member.isdir() or member.mode != 0o700 or member.size != 0:
                    raise ValueError("Invalid BM25 archive directory")
                continue
            if not member.isfile() or member.mode != 0o600:
                raise ValueError("Invalid BM25 archive file")
            entry = expected[member.name]
            if member.size != entry["size"]:
                raise ValueError("BM25 archive size mismatch")
            stream = source.extractfile(member)
            assert stream is not None
            value = stream.read(member.size + 1)
            if len(value) != member.size or sha256(value) != entry["sha256"]:
                raise ValueError("BM25 archive content mismatch")
            value.decode("utf-8")
            if member.name == REF:
                if value != REVISION.encode():
                    raise ValueError("BM25 ref must be exact and newline-free")
            elif member.name.endswith("/config.json"):
                if value != b"{}":
                    raise ValueError("Unsupported BM25 config")
            else:
                blob = b"blob " + str(len(value)).encode() + b"\0" + value
                if hashlib.sha1(blob).hexdigest() != entry["git_blob_sha1"]:
                    raise ValueError("BM25 Git blob mismatch")
            payload[member.name] = value
    if seen != expected.keys() | directories:
        raise ValueError("Missing BM25 archive members")
    return tuple(sorted(payload.items()))


def read_bm25(archive: Path, manifest: Path, *, uid: int) -> BM25Data:
    """Authenticate only the approved two source files, not ambient caches."""
    archive_bytes = read_file(archive, uid=uid, maximum=65536, mode=0o600)
    manifest_bytes = read_file(manifest, uid=uid, maximum=32768, mode=0o600)
    if sha256(archive_bytes) != ARCHIVE_SHA256 or sha256(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("BM25 source hash mismatch")
    return BM25Data(archive_bytes, manifest_bytes, inspect_archive(archive_bytes, manifest_bytes))


def write_new(path: Path, value: bytes) -> None:
    """Write only a new regular file below freshly owned scratch."""
    mkdir_private(path.parent, uid=os.geteuid())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)


def mkdir_private(path: Path, *, uid: int) -> None:
    """Create each missing component privately, not mkdir's default 755 parents."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for child in reversed(missing):
        child.mkdir(mode=0o700)
    directory(path, uid=uid, private=True)


def stage_bm25(root: Path, data: BM25Data, *, uid: int) -> None:
    """Materialize exact regular files; never invoke tar extraction or Hub lookup."""
    root.mkdir(mode=0o700)
    for relative, value in data.payload:
        write_new(root / relative, value)
    verify_bm25(root, data, uid=uid)


def verify_bm25(root: Path, data: BM25Data, *, uid: int) -> None:
    """Check every file and directory of this small owned model tree, no other roots."""
    expected = dict(data.payload)
    directories = {"."}
    for relative in expected:
        directories.update(
            str(parent) for parent in PurePosixPath(relative).parents if str(parent) != "."
        )
    actual_files = set()
    actual_directories = set()
    for parent, dirs, files in os.walk(root, followlinks=False):
        path = Path(parent)
        directory(path, uid=uid, private=True)
        actual_directories.add(str(path.relative_to(root)))
        for name in dirs:
            directory(path / name, uid=uid, private=True)
        for name in files:
            child = path / name
            relative = str(child.relative_to(root))
            if (
                relative not in expected
                or read_file(child, uid=uid, maximum=65536, mode=0o600) != expected[relative]
            ):
                raise ValueError("Unexpected/changed staged BM25 file")
            actual_files.add(relative)
    if actual_files != set(expected) or actual_directories != directories:
        raise ValueError("Incomplete/extra staged BM25 tree")
