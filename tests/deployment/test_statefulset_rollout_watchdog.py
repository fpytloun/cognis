from __future__ import annotations

import argparse
import json
import subprocess

import pytest

import scripts.watch_statefulset_rollout as watchdog
from scripts.watch_statefulset_rollout import RolloutState, unsafe_reason


def _state(**overrides: object) -> RolloutState:
    values = {
        "replicas": 2,
        "ready_replicas": 2,
        "updated_replicas": 2,
        "ready_endpoints": 2,
        "current_revision": "revision-b",
        "update_revision": "revision-b",
        "image": "registry.example/cognis:target",
    }
    values.update(overrides)
    return RolloutState(**values)  # type: ignore[arg-type]


def test_watchdog_accepts_only_a_complete_zero_downtime_rollout() -> None:
    state = _state()

    assert unsafe_reason(state, 200) is None
    assert state.is_complete("registry.example/cognis:target") is True
    assert state.is_complete("registry.example/cognis:other") is False


def test_watchdog_rejects_zero_endpoints_and_more_than_one_unavailable() -> None:
    assert "zero ready endpoints" in str(
        unsafe_reason(_state(ready_replicas=1, ready_endpoints=0), 200)
    )
    assert "more than one" in str(unsafe_reason(_state(ready_replicas=0, ready_endpoints=1), 200))


def test_watchdog_rejects_external_503() -> None:
    assert "503" in str(unsafe_reason(_state(), 503))


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "namespace": "cognis",
        "statefulset": "cognis",
        "service": "cognis",
        "health_url": "https://cognis.example/api/readyz",
        "target_image": "registry.example/cognis:target",
        "argo_namespace": "argocd",
        "application": "cognis",
        "rollback_revision": "previous-revision",
        "failure_threshold": 3,
        "interval_seconds": 1.0,
        "http_timeout_seconds": 1.0,
        "rollout_timeout_seconds": 30.0,
        "rollback_timeout_seconds": 30.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(watchdog.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(watchdog.time, "sleep", clock.sleep)
    return clock


def test_three_consecutive_failures_initiate_and_complete_rollback(monkeypatch) -> None:
    _install_clock(monkeypatch)
    samples = 0
    rollback_calls = 0

    def read_state(*_args, **_kwargs):
        nonlocal samples
        samples += 1
        return _state(ready_replicas=1, ready_endpoints=0)

    def run_rollback(**_kwargs):
        nonlocal rollback_calls
        rollback_calls += 1
        return 0

    monkeypatch.setattr(watchdog, "read_rollout_state", read_state)
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 200)
    monkeypatch.setattr(watchdog, "_run_rollback", run_rollback)
    monkeypatch.setattr(watchdog, "_rollback_is_complete", lambda **_kwargs: True)

    assert watchdog.run_watchdog(_args()) == 2
    assert samples == 3
    assert rollback_calls == 1


def test_successful_sample_resets_failure_threshold(monkeypatch) -> None:
    _install_clock(monkeypatch)
    samples = iter(
        [
            _state(ready_replicas=1, ready_endpoints=0),
            _state(ready_replicas=1, ready_endpoints=0),
            _state(ready_replicas=1, updated_replicas=1),
            _state(ready_replicas=1, ready_endpoints=0),
            _state(ready_replicas=1, ready_endpoints=0),
            _state(ready_replicas=1, ready_endpoints=0),
        ]
    )
    rollback_calls = 0

    def run_rollback(**_kwargs):
        nonlocal rollback_calls
        rollback_calls += 1
        return 0

    monkeypatch.setattr(watchdog, "read_rollout_state", lambda *_args: next(samples))
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 200)
    monkeypatch.setattr(watchdog, "_run_rollback", run_rollback)
    monkeypatch.setattr(watchdog, "_rollback_is_complete", lambda **_kwargs: True)

    assert watchdog.run_watchdog(_args()) == 2
    assert rollback_calls == 1


def test_rollout_timeout_initiates_rollback(monkeypatch) -> None:
    _install_clock(monkeypatch)
    rollback_calls = 0

    def run_rollback(**_kwargs):
        nonlocal rollback_calls
        rollback_calls += 1
        return 0

    monkeypatch.setattr(
        watchdog,
        "read_rollout_state",
        lambda *_args: _state(ready_replicas=1, updated_replicas=1),
    )
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 200)
    monkeypatch.setattr(watchdog, "_run_rollback", run_rollback)
    monkeypatch.setattr(watchdog, "_rollback_is_complete", lambda **_kwargs: True)

    assert watchdog.run_watchdog(_args(rollout_timeout_seconds=2.0)) == 2
    assert rollback_calls == 1


def test_kubectl_sample_failures_trigger_rollback(monkeypatch) -> None:
    _install_clock(monkeypatch)
    failure = subprocess.CalledProcessError(1, ["kubectl"])
    monkeypatch.setattr(
        watchdog,
        "read_rollout_state",
        lambda *_args: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(watchdog, "_run_rollback", lambda **_kwargs: 0)
    monkeypatch.setattr(watchdog, "_rollback_is_complete", lambda **_kwargs: True)

    assert watchdog.run_watchdog(_args()) == 2


def test_rollback_initiation_failure_returns_three(monkeypatch) -> None:
    _install_clock(monkeypatch)
    monkeypatch.setattr(
        watchdog,
        "read_rollout_state",
        lambda *_args: _state(ready_replicas=1, ready_endpoints=0),
    )
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 503)
    monkeypatch.setattr(watchdog, "_run_rollback", lambda **_kwargs: 1)

    assert watchdog.run_watchdog(_args()) == 3


def test_rollback_completion_retries_then_verifies_restored_endpoint(monkeypatch) -> None:
    _install_clock(monkeypatch)
    completion = iter([False, False, True])
    monkeypatch.setattr(
        watchdog,
        "read_rollout_state",
        lambda *_args: _state(ready_replicas=1, ready_endpoints=0),
    )
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 503)
    monkeypatch.setattr(watchdog, "_run_rollback", lambda **_kwargs: 0)
    monkeypatch.setattr(
        watchdog,
        "_rollback_is_complete",
        lambda **_kwargs: next(completion),
    )

    assert watchdog.run_watchdog(_args()) == 2


def test_rollback_completion_timeout_returns_three(monkeypatch) -> None:
    _install_clock(monkeypatch)
    monkeypatch.setattr(
        watchdog,
        "read_rollout_state",
        lambda *_args: _state(ready_replicas=1, ready_endpoints=0),
    )
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 503)
    monkeypatch.setattr(watchdog, "_run_rollback", lambda **_kwargs: 0)
    monkeypatch.setattr(watchdog, "_rollback_is_complete", lambda **_kwargs: False)

    assert watchdog.run_watchdog(_args(rollback_timeout_seconds=2.0)) == 3


def test_rollback_completion_requires_argo_health_and_ready_endpoint(monkeypatch) -> None:
    application = {
        "spec": {"source": {"targetRevision": "previous-revision"}},
        "status": {
            "sync": {"revision": "previous-revision", "status": "Synced"},
            "health": {"status": "Healthy"},
        },
    }
    monkeypatch.setattr(watchdog, "_kubectl_json", lambda _args: application)
    monkeypatch.setattr(watchdog, "health_status", lambda *_args: 200)
    monkeypatch.setattr(
        watchdog,
        "read_rollout_state",
        lambda *_args: _state(ready_endpoints=0),
    )

    assert (
        watchdog._rollback_is_complete(
            argo_namespace="argocd",
            application="cognis",
            revision="previous-revision",
            namespace="cognis",
            statefulset="cognis",
            service="cognis",
            health_url="https://cognis.example/api/readyz",
            http_timeout_seconds=1.0,
        )
        is False
    )

    monkeypatch.setattr(watchdog, "read_rollout_state", lambda *_args: _state())
    assert (
        watchdog._rollback_is_complete(
            argo_namespace="argocd",
            application="cognis",
            revision="previous-revision",
            namespace="cognis",
            statefulset="cognis",
            service="cognis",
            health_url="https://cognis.example/api/readyz",
            http_timeout_seconds=1.0,
        )
        is True
    )


def test_rollback_command_uses_structured_kubectl_arguments(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(watchdog.subprocess, "run", run)

    assert (
        watchdog._run_rollback(
            argo_namespace="argocd",
            application="cognis",
            revision="revision;touch /tmp/not-run",
        )
        == 0
    )
    assert observed["arguments"][0:5] == [
        "kubectl",
        "-n",
        "argocd",
        "patch",
        "application",
    ]
    assert observed["kwargs"] == {"check": False}
    patch = observed["arguments"][observed["arguments"].index("-p") + 1]
    assert json.loads(patch)["spec"]["source"]["targetRevision"] == ("revision;touch /tmp/not-run")
