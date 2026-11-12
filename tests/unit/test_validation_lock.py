"""Subprocess coverage for the shared-host Make validation boundary."""

from __future__ import annotations

import json
import os
import runpy
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "with_validation_lock.py"


class ValidationLockTests(unittest.TestCase):
    """Exercise actual kernel locking without loading the application graph."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.lock = self.path / "validation.lock"

    def command(self, *args: str, timeout: str = "0") -> list[str]:
        return [
            sys.executable,
            str(SCRIPT),
            "--lock-file",
            str(self.lock),
            "--timeout",
            timeout,
            "--",
            *args,
        ]

    def run_command(self, *args: str, timeout: str = "0") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args, timeout=timeout), capture_output=True, text=True, timeout=10
        )

    def hold(
        self, command: list[str] | None = None, cwd: Path | None = None
    ) -> subprocess.Popen[str]:
        owner = subprocess.Popen(
            command
            or self.command(sys.executable, "-u", "-c", "print('ready', flush=True); input()"),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        def cleanup() -> None:
            with suppress(ProcessLookupError):
                os.killpg(owner.pid, signal.SIGKILL)
            owner.communicate(timeout=5)

        self.addCleanup(cleanup)
        assert owner.stdout is not None
        self.assertTrue(select.select([owner.stdout], [], [], 5)[0], "owner did not become ready")
        self.assertEqual(owner.stdout.readline().strip(), "ready")
        return owner

    def test_contention_reports_owner_and_does_not_start_command(self) -> None:
        owner = self.hold()
        result = self.run_command(sys.executable, "-c", "raise SystemExit(99)")
        self.assertEqual(result.returncode, 75)
        self.assertIn(f"Waiting for validation lock {self.lock}", result.stderr)
        self.assertIn(f"locked by PID {owner.pid}", result.stderr)
        self.assertIn("command was not started", result.stderr)
        self.assertEqual(self.lock.read_text().strip(), str(owner.pid))

    def test_wait_status_repeats_before_timeout(self) -> None:
        owner = self.hold()
        main = runpy.run_path(str(SCRIPT))["main"]
        with (
            mock.patch.object(sys, "argv", self.command(sys.executable, timeout="601")[1:]),
            mock.patch.dict(
                main.__globals__,
                time=SimpleNamespace(
                    monotonic=mock.Mock(side_effect=[100, 100, 399, 400, 700, 701]),
                    sleep=mock.Mock(),
                ),
            ),
            mock.patch("sys.stderr") as stderr,
        ):
            # The real lock stays busy; only elapsed time is simulated.
            result = main()
        self.assertEqual(result, 75)
        messages = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertEqual(messages.count(f"locked by PID {owner.pid}"), 3)
        self.assertIn("waited 0.0s", messages)
        self.assertIn("waited 300.0s", messages)
        self.assertIn("waited 600.0s", messages)
        self.assertNotIn("waited 299.0s", messages)

    def test_default_wait_timeout_is_one_hour(self) -> None:
        self.hold()
        main = runpy.run_path(str(SCRIPT))["main"]
        argv = self.command(sys.executable)[1:]
        index = argv.index("--timeout")
        del argv[index : index + 2]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(
                main.__globals__,
                time=SimpleNamespace(
                    monotonic=mock.Mock(side_effect=[100, 100, 3699, 3700]),
                    sleep=mock.Mock(),
                ),
            ),
            mock.patch("sys.stderr") as stderr,
        ):
            result = main()
        self.assertEqual(result, 75)
        messages = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("waited 3599.0s / 3600s", messages)
        self.assertIn("timed out", messages)

    def test_expired_waiter_does_not_start_after_owner_releases(self) -> None:
        owner = self.hold()
        main = runpy.run_path(str(SCRIPT))["main"]
        with (
            mock.patch.object(sys, "argv", self.command(sys.executable, timeout="1")[1:]),
            mock.patch.dict(
                main.__globals__,
                time=SimpleNamespace(
                    monotonic=mock.Mock(side_effect=[100, 100, 102]),
                    sleep=lambda _: owner.communicate("\n", timeout=5),
                ),
            ),
            mock.patch("os.execvp") as execute,
            mock.patch("sys.stderr"),
        ):
            result = main()
        self.assertEqual(result, 75)
        execute.assert_not_called()

    def test_waiter_deadline_is_rechecked_after_acquisition(self) -> None:
        owner = self.hold()
        main = runpy.run_path(str(SCRIPT))["main"]
        with (
            mock.patch.object(sys, "argv", self.command(sys.executable, timeout="1")[1:]),
            mock.patch.dict(
                main.__globals__,
                time=SimpleNamespace(
                    monotonic=mock.Mock(side_effect=[100, 100, 100.5, 102]),
                    sleep=lambda _: owner.communicate("\n", timeout=5),
                ),
            ),
            mock.patch("os.execvp") as execute,
            mock.patch("sys.stderr"),
        ):
            result = main()
        self.assertEqual(result, 75)
        execute.assert_not_called()

    def test_waiter_runs_after_owner_exits(self) -> None:
        owner = self.hold()
        waiter = subprocess.Popen(
            self.command(sys.executable, "-c", "raise SystemExit(23)", timeout="5"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert waiter.stderr is not None
            self.assertTrue(select.select([waiter.stderr], [], [], 5)[0])
            self.assertIn(f"locked by PID {owner.pid}", waiter.stderr.readline())
            owner.communicate("\n", timeout=5)
            _, stderr = waiter.communicate(timeout=5)
            self.assertEqual(waiter.returncode, 23)
            self.assertIn("Acquired validation lock", stderr)
        finally:
            if waiter.poll() is None:
                waiter.kill()
            waiter.communicate(timeout=5)

    def test_crash_releases_lock_without_deleting_file(self) -> None:
        owner = self.hold()
        inode = self.lock.stat().st_ino
        owner.kill()
        owner.communicate(timeout=5)
        result = self.run_command(sys.executable, "-c", "raise SystemExit(0)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_stale_pid_text_does_not_block_acquisition(self) -> None:
        self.lock.write_text(f"{os.getpid()}\n")
        result = self.run_command(sys.executable, "-c", "raise SystemExit(17)")
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_inheriting_descendant_keeps_lock_after_launcher_exits(self) -> None:
        owner = self.hold(
            self.command(
                sys.executable,
                "-u",
                "-c",
                "import os; "
                "pid = os.fork(); "
                "os._exit(0) if pid else None; "
                "print('ready', flush=True); input()",
            )
        )
        owner.wait(timeout=5)
        result = self.run_command(sys.executable, "-c", "raise SystemExit(99)")
        self.assertEqual(result.returncode, 75, result.stderr)
        owner.communicate("\n", timeout=5)
        self.assertEqual(
            self.run_command(sys.executable, "-c", "raise SystemExit(0)").returncode, 0
        )

    def test_noninheriting_orphan_is_outside_lock_lifetime(self) -> None:
        owner = self.hold(
            self.command(
                sys.executable,
                "-u",
                "-c",
                "import os, subprocess, sys; "
                "subprocess.Popen([sys.executable, '-u', '-c', "
                "\"print('ready', flush=True); input()\"], close_fds=True); os._exit(0)",
            )
        )
        owner.wait(timeout=5)
        # A launcher must stay alive when its children close extra descriptors.
        self.assertEqual(
            self.run_command(sys.executable, "-c", "raise SystemExit(0)").returncode, 0
        )
        owner.communicate("\n", timeout=5)

    def test_signal_is_delivered_to_command(self) -> None:
        owner = self.hold()
        owner.send_signal(signal.SIGTERM)
        owner.communicate(timeout=5)
        self.assertEqual(owner.returncode, -signal.SIGTERM)
        self.assertEqual(
            self.run_command(sys.executable, "-c", "raise SystemExit(0)").returncode, 0
        )

    def test_symlink_lock_fails_closed(self) -> None:
        target = self.path / "untouched"
        target.write_text("preserve")
        self.lock.symlink_to(target)
        result = self.run_command(sys.executable, "-c", "raise SystemExit(99)")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_text(), "preserve")

    def test_exec_failure_releases_lock(self) -> None:
        result = self.run_command(str(self.path / "missing-command"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed", result.stderr)
        self.assertEqual(
            self.run_command(sys.executable, "-c", "raise SystemExit(0)").returncode, 0
        )

    def test_invalid_timeouts_fail_before_running(self) -> None:
        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value):
                result = self.run_command(
                    sys.executable, "-c", "raise SystemExit(99)", timeout=value
                )
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.lock.exists())

    def test_make_targets_share_lock_across_checkouts(self) -> None:
        checkouts = [self.path / "first", self.path / "second"]
        for checkout in checkouts:
            (checkout / "scripts").mkdir(parents=True)
            (checkout / "ui").mkdir()
            shutil.copy2(ROOT / "Makefile", checkout / "Makefile")
            shutil.copy2(SCRIPT, checkout / "scripts" / SCRIPT.name)
        fake_bin = self.path / "bin"
        fake_bin.mkdir()
        for tool in ("uv", "npm"):
            executable = fake_bin / tool
            executable.write_text("#!/bin/sh\nprintf 'ready\\n'\nread answer\n")
            executable.chmod(0o700)
        environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
        # Override only the path for this test; normal Make targets share /tmp.
        prefix = (
            f"VALIDATION_LOCK={sys.executable} scripts/{SCRIPT.name} "
            f"--lock-file {self.lock} --timeout 0 --"
        )
        owner_command = ["make", "--no-print-directory", "-s", "test", prefix]
        with mock.patch.dict(os.environ, environment):
            owner = self.hold(owner_command, checkouts[0])
            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "-s",
                    "test-ui",
                    prefix.replace(f"scripts/{SCRIPT.name}", f"../scripts/{SCRIPT.name}"),
                ],
                cwd=checkouts[1],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Waiting for validation lock", result.stderr)
            self.assertIn("locked by PID", result.stderr)
            self.assertNotIn("ready", result.stdout)
            owner.communicate("\n", timeout=5)

    def test_make_custom_command_preserves_quoting(self) -> None:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-s",
                "validate",
                f"VALIDATION_LOCK={sys.executable} {SCRIPT} --lock-file {self.lock} --",
                f"""COMMAND={sys.executable} -c "print('quoted value')" """,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "quoted value")

    def test_make_focused_selections_preserve_arguments_and_lock(self) -> None:
        fake_bin = self.path / "bin"
        fake_bin.mkdir()
        for tool in ("uv", "npm"):
            executable = fake_bin / tool
            executable.write_text(
                f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            executable.chmod(0o700)
        environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
        cpus = (
            len(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else os.cpu_count() or 1
        )
        cases = [
            (
                "test",
                "PYTEST_ARGS=tests/unit/test_validation_lock.py -q -k deadline",
                [
                    "run",
                    "pytest",
                    "tests/unit/test_validation_lock.py",
                    "-q",
                    "-k",
                    "deadline",
                    "-n",
                    str(max(1, cpus // 2)),
                ],
            ),
            (
                "test-serial",
                "PYTEST_ARGS=tests/unit/test_validation_lock.py -q",
                ["run", "pytest", "tests/unit/test_validation_lock.py", "-q", "-n", "0"],
            ),
            (
                "test-ui",
                "VITEST_ARGS=src/lib/chat-v2/outbox-clone.test.ts --maxWorkers=2",
                ["test", "--", "src/lib/chat-v2/outbox-clone.test.ts", "--maxWorkers=2"],
            ),
        ]
        for target, selection, expected in cases:
            with self.subTest(target=target):
                result = subprocess.run(
                    [
                        "make",
                        "--no-print-directory",
                        "-s",
                        target,
                        selection,
                        f"VALIDATION_LOCK={sys.executable} {SCRIPT} --lock-file {self.lock} --",
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertIn("Acquired validation lock", result.stderr)

    def test_make_worker_default_uses_half_available_cpus(self) -> None:
        python = self.path / "cpu_probe.py"
        python.write_text(
            "import os, sys\n"
            "cpus = int(sys.argv[1])\n"
            "if sys.argv[2] == 'affinity':\n"
            "    os.sched_getaffinity = lambda _: set(range(cpus))\n"
            "elif hasattr(os, 'sched_getaffinity'):\n"
            "    del os.sched_getaffinity\n"
            "os.cpu_count = lambda: cpus or None\n"
            "exec(sys.argv[4])\n"
        )
        environment = dict(os.environ)
        environment.pop("PYTEST_XDIST_WORKERS", None)
        for method in ("affinity", "fallback"):
            for cpus, expected in ((1, 1), (2, 1), (7, 3), (8, 4), (0, 1)):
                with self.subTest(method=method, cpus=cpus):
                    result = subprocess.run(
                        [
                            "make",
                            "--no-print-directory",
                            "-s",
                            "-n",
                            "test",
                            f"PYTHON={sys.executable} {python} {cpus} {method}",
                        ],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(
                        result.stdout.rstrip().endswith(f"-n {expected}"), result.stdout
                    )


if __name__ == "__main__":
    unittest.main()
