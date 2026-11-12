"""No-service input contracts. Synthetic tool records are NOT tool readiness evidence."""

from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import os
import secrets
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from tests.e2e import conftest as fixture_owner
from tests.e2e.subprocess_env import ServiceProcess, StackResources, isolated_subprocess_env
from tests.e2e.usage_checkpoint import inputs as input_owner
from tests.e2e.usage_checkpoint.bm25_inputs import (
    ARCHIVE_SHA256,
    MANIFEST_SHA256,
    NAMES,
    REF,
    REVISION,
    SNAPSHOT,
    BM25Data,
    inspect_archive,
    read_bm25,
    sha256,
    write_new,
)
from tests.e2e.usage_checkpoint.inputs import (
    CompanionInputs,
    DirectoryInput,
    FileInput,
    FixtureInputSpec,
    LaunchInputs,
    ToolInput,
    WorkerInputs,
    declared_launch,
)


@pytest.fixture(scope="session")
def bm25_source(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--usage-input-test-data-dir")
    if value is None:
        pytest.skip("Pinned data not explicitly supplied; this is not offline qualification")
    return Path(value)


@pytest.fixture(scope="session")
def bm25_data(bm25_source: Path) -> BM25Data:
    return read_bm25(
        bm25_source / "bm25-cache-input.tar",
        bm25_source / "verified-manifest.json",
        uid=os.geteuid(),
    )


@pytest.fixture
def declaration(tmp_path: Path, bm25_source: Path) -> tuple[Path, FixtureInputSpec]:
    """Create inert file identities, never install/copy/prewarm a tool environment."""
    data = tmp_path / "data"
    cache, tools = data / "new-cache", data / "tools"
    for path in (data, cache, tools):
        path.mkdir(mode=0o700)
    constraints = data / "companion-constraints.txt"
    write_new(constraints, b"packaging==26.3\n")
    installed = []
    for name in ("mnemory", "intaris"):
        receipt = tools / name / "uv-receipt.toml"
        entrypoint = tools / name / "bin" / name
        write_new(receipt, b"# Inert unit-test receipt, not an installed environment\n")
        write_new(entrypoint, b"Inert boundary test bytes; must never be executed\n")
        entrypoint.chmod(0o700)
        installed.append(
            ToolInput(
                name,
                FileInput(str(receipt), sha256(receipt.read_bytes())),
                FileInput(str(entrypoint), sha256(entrypoint.read_bytes())),
            )
        )
    spec = FixtureInputSpec(
        launch_id=secrets.token_hex(16),
        uid=os.geteuid(),
        archive=FileInput(str(bm25_source / "bm25-cache-input.tar"), ARCHIVE_SHA256),
        manifest=FileInput(str(bm25_source / "verified-manifest.json"), MANIFEST_SHA256),
        companions=CompanionInputs(
            str(data),
            DirectoryInput.capture(cache, os.geteuid()),
            DirectoryInput.capture(tools, os.geteuid()),
            FileInput(str(constraints), sha256(constraints.read_bytes())),
            tuple(installed),
        ),
    )
    path = tmp_path / "launch.json"
    write_new(path, spec.bytes())
    return path, spec


@pytest.fixture
def worker(declaration: tuple[Path, FixtureInputSpec], tmp_path: Path):
    path, spec = declaration
    launch = LaunchInputs.load(path, sha256(spec.bytes()))
    result = launch.stage(tmp_path)
    yield result
    result.close()


def test_usage_missing_declaration_fails_without_ambient_fallback(monkeypatch):
    monkeypatch.setenv("HOME", "/work/preparation-home")
    monkeypatch.setenv("UV_TOOL_DIR", "/work/preparation-tools")
    assert declared_launch(enabled=False, spec_path=None, spec_sha256=None) is None
    for path, digest in ((None, None), ("/work/spec.json", None), (None, ARCHIVE_SHA256)):
        with pytest.raises(ValueError, match="Usage requires"):
            declared_launch(enabled=True, spec_path=path, spec_sha256=digest)
    with pytest.raises(ValueError, match="require COGNIS_USAGE_CHECKPOINT"):
        declared_launch(enabled=False, spec_path="/work/spec.json", spec_sha256=None)


def test_usage_fixture_missing_inputs_fails_before_stack(request, monkeypatch):
    monkeypatch.setenv("COGNIS_USAGE_CHECKPOINT", "1")
    monkeypatch.delenv("COGNIS_E2E_CONTROLLER_PROJECT", raising=False)
    with pytest.raises(ValueError, match="Usage requires"):
        fixture_owner.usage_launch.__wrapped__(request)


def test_pinned_supported_data_exact_and_fresh_staging(
    worker: WorkerInputs,
    bm25_data: BM25Data,
    tmp_path: Path,
):
    assert sha256(bm25_data.archive) == ARCHIVE_SHA256
    assert sha256(bm25_data.manifest) == MANIFEST_SHA256
    payload = dict(bm25_data.payload)
    assert set(payload) == {f"{SNAPSHOT}/{name}" for name in NAMES} | {REF}
    assert len(NAMES) == 18 and len(payload) == 19
    assert payload[REF] == REVISION.encode() and payload[f"{SNAPSHOT}/config.json"] == b"{}"
    assert len([name for name in NAMES if name.endswith(".txt")]) == 17
    second = worker.launch.stage(tmp_path)
    try:
        assert second.root != worker.root
        worker.revalidate()
        second.revalidate()
    finally:
        second.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "absolute",
        "traversal",
        "symlink",
        "hardlink",
        "extra",
        "duplicate",
        "content",
        "ref-newline",
        "directory-mode",
        "directory-link",
    ],
)
def test_reject_unsafe_extra_and_changed_archives(bm25_data: BM25Data, mutation: str):
    target = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bm25_data.archive), mode="r:") as source,
        tarfile.open(fileobj=target, mode="w:", format=tarfile.USTAR_FORMAT) as output,
    ):
        members = source.getmembers()
        files = [member for member in members if member.isfile()]
        first_directory = next(member.name for member in members if member.isdir())
        for original in members:
            member = copy.copy(original)
            if original.isdir():
                if original.name == first_directory:
                    if mutation == "directory-mode":
                        member.mode = 0o755
                    elif mutation == "directory-link":
                        member.type, member.linkname = tarfile.SYMTYPE, files[0].name
                output.addfile(member)
                continue
            stream = source.extractfile(original)
            assert stream is not None
            value = stream.read()
            if original.name == files[0].name:
                if mutation == "absolute":
                    member.name = "/outside"
                elif mutation == "traversal":
                    member.name = "../outside"
                elif mutation in {"symlink", "hardlink"}:
                    member.type = tarfile.SYMTYPE if mutation == "symlink" else tarfile.LNKTYPE
                    member.linkname = files[1].name
                    member.size = 0
                    value = b""
                elif mutation == "duplicate":
                    member.name = files[1].name
                elif mutation == "content":
                    value = bytes([value[0] ^ 1]) + value[1:]
            if mutation == "ref-newline" and original.name == REF:
                value += b"\n"
                member.size = len(value)
            output.addfile(member, io.BytesIO(value))
        if mutation == "extra":
            member = tarfile.TarInfo("unlisted-file")
            member.mode, member.uid, member.gid = 0o600, 1001, 1001
            output.addfile(member, io.BytesIO(b""))
    with pytest.raises(ValueError):
        inspect_archive(target.getvalue(), bm25_data.manifest)


def test_self_asserted_manifest_never_authenticates_inputs(tmp_path: Path, bm25_data: BM25Data):
    manifest = json.loads(bm25_data.manifest)
    manifest["archive_sha256"] = ARCHIVE_SHA256
    archive_path, manifest_path = tmp_path / "input.tar", tmp_path / "manifest.json"
    write_new(archive_path, bm25_data.archive)
    write_new(manifest_path, json.dumps(manifest).encode())
    with pytest.raises(ValueError, match="source hash mismatch"):
        read_bm25(archive_path, manifest_path, uid=os.geteuid())


@pytest.mark.parametrize(
    "mutation", ["hash", "duplicate-key", "extra-key", "compatibility", "constraints"]
)
def test_launch_binding_rejects_changes(declaration, mutation):
    path, spec = declaration
    expected = sha256(spec.bytes())
    if mutation == "hash":
        value = spec.bytes() + b"\n"
    elif mutation == "duplicate-key":
        value = spec.bytes()[:-1] + b',"schema":1}'
        expected = sha256(value)
    elif mutation == "extra-key":
        document = json.loads(spec.bytes())
        document["unlisted"] = True
        value = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        expected = sha256(value)
    elif mutation == "compatibility":
        value = replace(spec, python="3.11").bytes()
        expected = sha256(value)
    else:
        constraints = Path(spec.companions.constraints.path)
        constraints.write_bytes(b"packaging==26.0\n")
        changed = replace(spec.companions.constraints, sha256=sha256(constraints.read_bytes()))
        value = replace(spec, companions=replace(spec.companions, constraints=changed)).bytes()
        expected = sha256(value)
    path.write_bytes(value)
    with pytest.raises(ValueError):
        LaunchInputs.load(path, expected)


@pytest.mark.parametrize(
    "mutation",
    [
        "model-extra",
        "model-directory",
        "model-link",
        "model-ref",
        "spec",
        "evidence-extra",
        "archive",
        "constraints",
        "receipt",
        "entrypoint",
        "runtime-root",
    ],
)
def test_worker_revalidation_precedes_any_child_start(worker, mutation, monkeypatch, tmp_path):
    root = worker.root
    model = root / "fastembed_cache"
    if mutation == "model-extra":
        write_new(model / "unlisted-file", b"unsupported data")
    elif mutation == "model-directory":
        (model / "unlisted-directory").mkdir(mode=0o700)
    elif mutation == "model-link":
        target = model / REF
        target.unlink()
        target.symlink_to(root / "inputs" / "manifest.json")
    elif mutation == "model-ref":
        (model / REF).write_bytes(REVISION.encode() + b"\n")
    elif mutation == "evidence-extra":
        write_new(root / "inputs" / "unlisted-file", b"unlisted")
    elif mutation in {"spec", "archive", "constraints"}:
        name = {
            "spec": "spec.json",
            "archive": "bm25.tar",
            "constraints": "companion-constraints.txt",
        }[mutation]
        (root / "inputs" / name).write_bytes(b"changed")
    elif mutation in {"receipt", "entrypoint"}:
        tool = worker.launch.spec.companions.installed[0]
        Path(getattr(tool, mutation).path).write_bytes(b"changed")
    else:
        cache = Path(worker.launch.spec.companions.cache.path)
        cache.rename(cache.with_name("retained-cache"))
        cache.mkdir(mode=0o700)

    def forbidden(*args, **kwargs):
        pytest.fail("Popen must not be reached with invalid declared inputs")

    monkeypatch.setattr(fixture_owner.subprocess, "Popen", forbidden)
    with pytest.raises((ValueError, OSError)):
        fixture_owner._start_proc(
            ["uvx", "mnemory"], {}, label="mnemory", log_dir=tmp_path, inputs=worker
        )
    assert not (tmp_path / "mnemory.log").exists()


def test_explicit_env_survives_distinct_fresh_homes(worker, monkeypatch, tmp_path):
    captured = []

    class Child:
        pid = 1

        def poll(self):
            return 0

    def capture(command, **kwargs):
        captured.append((command, kwargs["env"]))
        return Child()

    monkeypatch.setattr(fixture_owner.subprocess, "Popen", capture)
    monkeypatch.setenv("HOME", str(tmp_path / "preparation-home"))
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "ambient-tools"))
    monkeypatch.setenv("UV_CONSTRAINT", str(tmp_path / "controller-constraints"))
    source_fence = str(Path(fixture_owner.__file__).parent / "usage_checkpoint")
    controller_env = str(tmp_path / "fresh-controller-environment")
    monkeypatch.setenv("PYTHONPATH", source_fence)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", controller_env)
    monkeypatch.setenv(
        "COGNIS_E2E_CONTROLLER_PROJECT", str(Path(fixture_owner.__file__).resolve().parents[2])
    )
    for label in ("mnemory", "intaris", "cognis", "mock-llm"):
        command = (
            ["uvx", label, "--help"] if label in {"mnemory", "intaris"} else ["uv", "run", "python"]
        )
        child = fixture_owner._start_proc(
            command,
            {"UV_TOOL_DIR": str(tmp_path / "ambient-tools")},
            label=label,
            log_dir=tmp_path,
            inputs=worker,
        )
        child.stop()
    homes = []
    for (command, env), label in zip(
        captured, ("mnemory", "intaris", "cognis", "mock-llm"), strict=True
    ):
        homes.append(env["HOME"])
        assert Path(env["HOME"]).is_relative_to(worker.root)
        assert env["UV_TOOL_DIR"] == worker.launch.spec.companions.tools.path
        assert env["PYTHONPATH"] == source_fence
        assert env["UV_CACHE_DIR"] == worker.launch.spec.companions.cache.path
        assert env["UV_OFFLINE"] == "1" and env["UV_PYTHON_DOWNLOADS"] == "never"
        if label in {"mnemory", "intaris"}:
            tool = next(
                tool for tool in worker.launch.spec.companions.installed if tool.name == label
            )
            assert command == [tool.entrypoint.path, "--help"]
            assert Path(env["UV_CONSTRAINT"]).read_bytes() == b"packaging==26.3\n"
            assert env["UV_BUILD_CONSTRAINT"] == env["UV_CONSTRAINT"]
            assert "UV_PROJECT_ENVIRONMENT" not in env
            assert "UV_NO_SYNC" not in env
        else:
            assert command[:2] == ["uv", "run"]
            assert env["UV_NO_SYNC"] == "1" and "UV_CONSTRAINT" not in env
            assert "UV_BUILD_CONSTRAINT" not in env
            assert env["UV_PROJECT_ENVIRONMENT"] == controller_env
    assert len(set(homes)) == 4


@pytest.mark.parametrize("label", ["mnemory", "intaris"])
def test_non_declared_companion_commands_are_unchanged(label, monkeypatch, tmp_path):
    captured = []

    class Child:
        pid = 1

        def poll(self):
            return 0

    def capture(command, **kwargs):
        captured.append(command)
        return Child()

    monkeypatch.setattr(fixture_owner.subprocess, "Popen", capture)
    command = ["uvx", label, "--help"]
    child = fixture_owner._start_proc(command, {}, label=label, log_dir=tmp_path)
    child.stop()
    assert captured == [command]


def test_usage_fixture_rejects_non_namespace_roots(worker, request, monkeypatch):
    monkeypatch.setenv("COGNIS_USAGE_CHECKPOINT", "1")
    monkeypatch.delenv("COGNIS_E2E_CONTROLLER_PROJECT", raising=False)
    monkeypatch.setattr(fixture_owner, "declared_launch", lambda **kwargs: worker.launch)
    with pytest.raises(ValueError, match="requires fresh /work/data/new-cache"):
        fixture_owner.usage_launch.__wrapped__(request)


@pytest.mark.parametrize("cache_path", ["/work/data/cache", "/work/data/new-cache"])
def test_namespace_binding_accepts_only_admitted_cache(worker, request, monkeypatch, cache_path):
    companions = worker.launch.spec.companions
    bound = replace(
        worker.launch,
        spec=replace(
            worker.launch.spec,
            companions=replace(
                companions,
                data_root="/work/data",
                cache=replace(companions.cache, path=cache_path),
                tools=replace(companions.tools, path="/work/data/tools"),
            ),
        ),
    )
    monkeypatch.setenv("COGNIS_USAGE_CHECKPOINT", "1")
    monkeypatch.delenv("COGNIS_E2E_CONTROLLER_PROJECT", raising=False)
    monkeypatch.setattr(fixture_owner, "declared_launch", lambda **kwargs: bound)
    if cache_path == "/work/data/new-cache":
        assert fixture_owner.usage_launch.__wrapped__(request) is bound
    else:
        with pytest.raises(ValueError, match="requires fresh /work/data/new-cache"):
            fixture_owner.usage_launch.__wrapped__(request)


def test_non_usage_environment_defaults_are_unchanged():
    inherited = {
        "HOME": "/work/home",
        "UV_NO_SYNC": "1",
        "UV_TOOL_DIR": "/work/tools",
        "PYTHONPATH": "/work/src",
        "COGNIS_PORT": "8080",
        "DATABASE_URL": "sqlite://",
    }
    assert isolated_subprocess_env(inherited, {"COGNIS_PORT": "8081"}) == {
        **{key: value for key, value in inherited.items() if key != "DATABASE_URL"},
        "COGNIS_PORT": "8081",
    }


def test_shared_stack_keeps_raw_process_contract_without_starting_services():
    """Guard the shared fixture's public Popen handle, including PTC's .args access."""
    tree = ast.parse(inspect.getsource(fixture_owner.e2e_stack.__wrapped__))
    construction = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "E2EStack"
    )
    exposed = next(
        keyword.value for keyword in construction.keywords if keyword.arg == "intaris_process"
    )
    assert isinstance(exposed, ast.Attribute) and exposed.attr == "process"
    assert isinstance(exposed.value, ast.Name) and exposed.value.id == "intaris_proc"


def test_matching_but_unsupported_runtime_tuple_rejected(declaration, monkeypatch):
    _, spec = declaration
    monkeypatch.setattr(input_owner.platform, "machine", lambda: "aarch64")
    with pytest.raises(ValueError, match="runtime identity mismatch"):
        replace(spec, machine="aarch64").verify()


def test_teardown_order_and_live_child_retains_scratch(worker):
    events = []

    class Child:
        alive = True
        pid = 1

        def poll(self):
            return None if self.alive else 0

        def send_signal(self, value):
            events.append("stop-child")
            assert worker.root.exists()
            self.alive = False

        def wait(self, timeout):
            events.append("wait-child")
            return 0

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(self.name)
            assert worker.root.exists()

    child = Child()
    worker.before_start()
    worker.register_child(child)
    worker.close_if_unused()
    assert worker.root.exists()
    with pytest.raises(RuntimeError, match="before child teardown"):
        worker.close()
    resources = StackResources(
        inputs=worker,
        processes=[("mnemory", ServiceProcess(child, io.StringIO()))],
        clients=[Resource("client")],
        append_proxy=Resource("proxy"),
    )
    resources.close()
    assert events == ["proxy", "client", "stop-child", "wait-child"]
    assert not worker.root.exists()
    # Prepared runtime inputs belong to the namespace/bootstrap, not this scratch owner.
    assert Path(worker.launch.spec.companions.tools.path).exists()
    worker.close()


def test_teardown_failure_attempts_children_but_retains_scratch(worker):
    events = []

    class FailedProxy:
        def close(self):
            events.append("proxy-failed")
            raise RuntimeError("proxy teardown incomplete")

    class StoppedChild:
        def stop(self):
            events.append("child-stopped")

    worker.before_start()
    resources = StackResources(
        inputs=worker, processes=[("mnemory", StoppedChild())], append_proxy=FailedProxy()
    )
    with pytest.raises(ExceptionGroup, match="scratch retained"):
        resources.close()
    assert events == ["proxy-failed", "child-stopped"] and worker.root.exists()


def test_unused_scratch_cleanup_and_root_replacement_guard(worker, tmp_path):
    original = worker.root
    retained = tmp_path / "retained-owned-scratch"
    original.rename(retained)
    original.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity changed"):
        worker.close()
    assert retained.exists() and original.exists()
    original.rmdir()
    retained.rename(original)
    worker.close_if_unused()
    assert not original.exists()


def test_input_source_links_and_hardlinks_rejected(bm25_data, tmp_path):
    archive, manifest = tmp_path / "input.tar", tmp_path / "manifest.json"
    write_new(archive, bm25_data.archive)
    write_new(manifest, bm25_data.manifest)
    link = tmp_path / "linked-input"
    link.symlink_to(archive)
    with pytest.raises(OSError):
        read_bm25(link, manifest, uid=os.geteuid())
    link.unlink()
    os.link(archive, link)
    with pytest.raises(ValueError, match="type/ownership/mode"):
        read_bm25(archive, manifest, uid=os.geteuid())
