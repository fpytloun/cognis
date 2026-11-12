"""Declared offline E2E inputs. Pure stdlib; no installers or ambient cache discovery.

The trusted bootstrap seals this spec after its own normal offline tool installs.
The launch separately binds the spec hash. These checks authenticate declared
bytes and lifecycle roots, not an entire dependency closure or installed package
compatibility; those remain bootstrap/admission gates.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from tests.e2e.usage_checkpoint.bm25_inputs import (
    ARCHIVE_SHA256,
    MANIFEST_SHA256,
    BM25Data,
    directory,
    mkdir_private,
    read_bm25,
    read_file,
    sha256,
    stage_bm25,
    strict_json,
    verify_bm25,
    write_new,
)


def _digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Expected explicit SHA-256 identity")


@dataclass(frozen=True)
class FileInput:
    """One explicitly declared file, not a recursive cache/venv inventory."""

    path: str
    sha256: str

    def verify(
        self,
        uid: int,
        *,
        maximum: int = 131072,
        private: bool = False,
        executable: bool = False,
    ) -> bytes:
        _digest(self.sha256)
        value = read_file(
            Path(self.path),
            uid=uid,
            maximum=maximum,
            mode=0o600 if private else None,
            executable=executable,
        )
        if sha256(value) != self.sha256:
            raise ValueError("Declared input file hash mismatch")
        return value


@dataclass(frozen=True)
class DirectoryInput:
    """Bootstrap-owned fresh runtime directory identity; no old-home relocation."""

    path: str
    device: int
    inode: int

    @classmethod
    def capture(cls, path: Path, uid: int) -> DirectoryInput:
        info = directory(path, uid=uid, private=True)
        return cls(str(path), info.st_dev, info.st_ino)

    def verify(self, uid: int) -> None:
        if (
            type(self.device) is not int
            or self.device < 0
            or type(self.inode) is not int
            or self.inode <= 0
        ):
            raise ValueError("Invalid prepared directory identity")
        info = directory(Path(self.path), uid=uid, private=True)
        if (info.st_dev, info.st_ino) != (self.device, self.inode):
            raise ValueError("Prepared runtime directory identity changed")
        if not os.access(self.path, os.W_OK | os.X_OK):
            raise ValueError("Prepared runtime directory must be writable")


@dataclass(frozen=True)
class ToolInput:
    """Pins the normal uv tool receipt and selected console entrypoint only."""

    name: str
    receipt: FileInput
    entrypoint: FileInput

    def verify(self, tools: Path, uid: int) -> None:
        if self.name not in {"mnemory", "intaris"}:
            raise ValueError("Unsupported companion tool")
        if Path(self.receipt.path) != tools / self.name / "uv-receipt.toml":
            raise ValueError("Receipt is not in the declared normal uv tool root")
        if Path(self.entrypoint.path) != tools / self.name / "bin" / self.name:
            raise ValueError("Entrypoint is not in the declared normal uv tool root")
        self.receipt.verify(uid)
        self.entrypoint.verify(uid, executable=True)


@dataclass(frozen=True)
class CompanionInputs:
    """Prepared writable runtime roots, not a HOME or a copied environment."""

    data_root: str
    cache: DirectoryInput
    tools: DirectoryInput
    constraints: FileInput
    installed: tuple[ToolInput, ...]
    preparation: str = "uv-tool-install-offline"

    def verify(self, uid: int) -> bytes:
        root = Path(self.data_root)
        directory(root, uid=uid, private=True)
        if (
            self.preparation != "uv-tool-install-offline"
            or Path(self.cache.path) != root / "new-cache"
            or Path(self.tools.path) != root / "tools"
            or Path(self.constraints.path) != root / "companion-constraints.txt"
            or {tool.name for tool in self.installed} != {"mnemory", "intaris"}
            or len(self.installed) != 2
        ):
            raise ValueError("Invalid declared companion lifecycle inputs")
        self.cache.verify(uid)
        self.tools.verify(uid)
        constraints = self.constraints.verify(uid, private=True)
        packaging = [
            line.strip()
            for line in constraints.decode().splitlines()
            if line.strip().lower().startswith("packaging")
        ]
        if packaging != ["packaging==26.3"]:
            raise ValueError("Companion constraints must retain packaging==26.3")
        for tool in self.installed:
            tool.verify(Path(self.tools.path), uid)
        return constraints


@dataclass(frozen=True)
class FixtureInputSpec:
    """Immutable prepare-time declaration, authenticated by a separate launch hash."""

    launch_id: str
    uid: int
    archive: FileInput
    manifest: FileInput
    companions: CompanionInputs
    schema: int = 1
    python: str = "3.12"
    system: str = "linux"
    machine: str = "x86_64"

    def bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def parse(cls, value: bytes) -> FixtureInputSpec:
        document = strict_json(value)
        try:
            companions = document.pop("companions")
            installed = tuple(
                ToolInput(
                    name=tool["name"],
                    receipt=FileInput(**tool["receipt"]),
                    entrypoint=FileInput(**tool["entrypoint"]),
                )
                for tool in companions.pop("installed")
            )
            spec = cls(
                **{
                    **document,
                    "archive": FileInput(**document["archive"]),
                    "manifest": FileInput(**document["manifest"]),
                    "companions": CompanionInputs(
                        **{
                            **companions,
                            "cache": DirectoryInput(**companions["cache"]),
                            "tools": DirectoryInput(**companions["tools"]),
                            "constraints": FileInput(**companions["constraints"]),
                            "installed": installed,
                        }
                    ),
                }
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid declared fixture input schema") from exc
        if spec.bytes() != value:
            raise ValueError("Input spec must be exact canonical JSON, without extras")
        return spec

    def verify(self) -> tuple[BM25Data, bytes]:
        if (
            type(self.schema) is not int
            or self.schema != 1
            or type(self.uid) is not int
            or self.uid <= 0
            or self.uid != os.geteuid()
            or not re.fullmatch(r"[a-f0-9]{32}", self.launch_id)
            or (self.python, self.system, self.machine) != ("3.12", "linux", "x86_64")
            or self.python != f"{sys.version_info.major}.{sys.version_info.minor}"
            or self.system != sys.platform
            or self.machine != platform.machine()
            or self.archive.sha256 != ARCHIVE_SHA256
            or self.manifest.sha256 != MANIFEST_SHA256
        ):
            raise ValueError("Fixture launch/data/runtime identity mismatch")
        data = read_bm25(Path(self.archive.path), Path(self.manifest.path), uid=self.uid)
        return data, self.companions.verify(self.uid)


@dataclass(frozen=True)
class LaunchInputs:
    """Admitted bytes retained independently of any later source-path mutation."""

    spec: FixtureInputSpec
    spec_bytes: bytes
    spec_sha256: str
    data: BM25Data
    constraints: bytes

    @classmethod
    def load(cls, path: Path, expected_sha256: str) -> LaunchInputs:
        _digest(expected_sha256)
        value = read_file(path, uid=os.geteuid(), maximum=131072, mode=0o600)
        if sha256(value) != expected_sha256:
            raise ValueError("Launch-bound input spec hash mismatch")
        spec = FixtureInputSpec.parse(value)
        data, constraints = spec.verify()
        return cls(spec, value, expected_sha256, data, constraints)

    def stage(self, parent: Path) -> WorkerInputs:
        """Create one fresh private scratch owner; never reuse an existing cache."""
        directory(parent, uid=self.spec.uid, private=True)
        self.spec.verify()
        root = Path(tempfile.mkdtemp(prefix="usage-inputs-", dir=parent))
        try:
            evidence = root / "inputs"
            evidence.mkdir(mode=0o700)
            write_new(evidence / "spec.json", self.spec_bytes)
            write_new(evidence / "bm25.tar", self.data.archive)
            write_new(evidence / "manifest.json", self.data.manifest)
            write_new(evidence / "companion-constraints.txt", self.constraints)
            stage_bm25(root / "fastembed_cache", self.data, uid=self.spec.uid)
            worker = WorkerInputs(self, DirectoryInput.capture(root, self.spec.uid))
            worker.revalidate()
            return worker
        except BaseException:
            shutil.rmtree(root)
            raise


def declared_launch(
    *, enabled: bool, spec_path: str | None, spec_sha256: str | None
) -> LaunchInputs | None:
    """Opt in explicitly; usage never falls back to an ambient home/cache."""
    if not enabled:
        if spec_path is not None or spec_sha256 is not None:
            raise ValueError("Declared inputs require COGNIS_USAGE_CHECKPOINT=1")
        return None
    if not spec_path or not spec_sha256:
        raise ValueError("Usage requires --usage-input-spec and --usage-input-sha256")
    return LaunchInputs.load(Path(spec_path), spec_sha256)


class ChildProcess(Protocol):
    def poll(self) -> int | None: ...


@dataclass
class WorkerInputs:
    """Stack-owned staged data and child lifetime; never global fixture authority."""

    launch: LaunchInputs
    scratch: DirectoryInput
    children: list[ChildProcess] = field(default_factory=list)
    startup_entered: bool = False
    closed: bool = False

    @property
    def root(self) -> Path:
        return Path(self.scratch.path)

    def revalidate(self) -> None:
        if self.closed:
            raise ValueError("Fixture input lifetime already closed")
        spec = self.launch.spec
        self.scratch.verify(spec.uid)
        evidence = self.root / "inputs"
        directory(evidence, uid=spec.uid, private=True)
        if {path.name for path in evidence.iterdir()} != {
            "spec.json",
            "bm25.tar",
            "manifest.json",
            "companion-constraints.txt",
        }:
            raise ValueError("Unexpected worker input evidence")
        value = read_file(evidence / "spec.json", uid=spec.uid, maximum=131072, mode=0o600)
        if value != self.launch.spec_bytes or sha256(value) != self.launch.spec_sha256:
            raise ValueError("Worker launch binding changed")
        held = read_bm25(evidence / "bm25.tar", evidence / "manifest.json", uid=spec.uid)
        verify_bm25(self.root / "fastembed_cache", held, uid=spec.uid)
        constraints = read_file(
            evidence / "companion-constraints.txt", uid=spec.uid, maximum=131072, mode=0o600
        )
        if constraints != self.launch.constraints or constraints != spec.companions.verify(
            spec.uid
        ):
            raise ValueError("Worker companion constraints changed")

    def before_start(self) -> None:
        self.revalidate()
        self.startup_entered = True

    def environment(self, label: str) -> dict[str, str]:
        """Explicit uv roots survive isolated HOMEs; constraints never reach controller."""
        if label not in {"mnemory", "intaris", "mock-llm", "cognis"}:
            raise ValueError("Unknown E2E child owner")
        home = self.root / "homes" / label
        for child in (home, home / ".cache", home / ".config", home / ".local", home / "tmp"):
            mkdir_private(child, uid=self.launch.spec.uid)
        result = {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local"),
            "TMPDIR": str(home / "tmp"),
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_CACHE_DIR": self.launch.spec.companions.cache.path,
            "UV_TOOL_DIR": self.launch.spec.companions.tools.path,
            "FASTEMBED_CACHE_PATH": str(self.root / "fastembed_cache"),
            "HF_HUB_OFFLINE": "1",
        }
        if label in {"mnemory", "intaris"}:
            result["UV_CONSTRAINT"] = str(self.root / "inputs" / "companion-constraints.txt")
            result["UV_BUILD_CONSTRAINT"] = result["UV_CONSTRAINT"]
        elif label in {"cognis", "mock-llm"}:
            result["UV_NO_SYNC"] = "1"
        return result

    def register_child(self, child: ChildProcess) -> None:
        if self.closed:
            raise ValueError("Cannot register a child after teardown")
        self.children.append(child)

    def close_if_unused(self) -> None:
        """Early setup failure cleanup; uncertain/entered startup must use stack teardown."""
        if not self.startup_entered:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        if any(child.poll() is None for child in self.children):
            raise RuntimeError("Refusing scratch cleanup before child teardown")
        self.scratch.verify(self.launch.spec.uid)
        shutil.rmtree(self.root)
        self.closed = True
