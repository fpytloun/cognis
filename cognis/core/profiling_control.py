"""Finite HA profiling commands in bounded Redis records, separate from observations."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import socket
import time
import uuid
from typing import Any

from cognis import json_codec as json
from cognis.core.controller_directory import ControllerInstanceDirectory
from cognis.core.profiling_capture import CaptureOptions, LocalCapture
from cognis.core.redis_service import RedisService, RedisUnavailableError
from cognis.core.resource_attribution import MemorySummary, _integer

logger = logging.getLogger(__name__)
PREFIX = "cognis:{profiling}:"
ACTIVE = PREFIX + "active"
INDEX = PREFIX + "index"
TERMINAL = {"completed", "stopped", "failed", "interrupted"}

# All keys share one Redis hash tag. Four retained captures, eight exact owners
# each; no unbounded request-id or incarnation-key growth.
ADMIT = """
local existing = redis.call('HGET', KEYS[3], 'meta')
if existing then
  if cjson.decode(existing).digest == ARGV[2] then return existing end
  return 'conflict'
end
local active = redis.call('GET', KEYS[1])
if active then
  local meta = redis.call('HGET', ARGV[4] .. active, 'meta')
  if meta then
    for _, owner in ipairs(cjson.decode(meta).owners) do
      local status = redis.call('HGET', ARGV[4] .. active, 'status:' .. owner)
      if not status then return 'busy' end
      local state = cjson.decode(status).state
      if state ~= 'completed' and state ~= 'stopped' and state ~= 'failed'
          and state ~= 'interrupted' then return 'busy' end
    end
  end
end
local entries = cjson.decode(redis.call('GET', KEYS[2]) or '[]')
table.insert(entries, ARGV[1])
while #entries > 4 do
  redis.call('DEL', ARGV[4] .. table.remove(entries, 1))
end
redis.call('SET', KEYS[2], cjson.encode(entries), 'EX', 3600)
redis.call('HSET', KEYS[3], 'meta', ARGV[3], 'desired', 'start')
redis.call('EXPIRE', KEYS[3], 3600)
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[5])
return ARGV[3]
"""
STOP = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'desired', 'stop')
return 1
"""
OBSERVE = """
local meta = redis.call('HGET', KEYS[1], 'meta')
if not meta then return 0 end
local allowed = false
for _, owner in ipairs(cjson.decode(meta).owners) do
  if owner == ARGV[1] then allowed = true end
end
if not allowed then return 0 end
redis.call('HSET', KEYS[1], 'status:' .. ARGV[1], ARGV[2])
if ARGV[3] ~= '' then redis.call('HSET', KEYS[1], 'report:' .. ARGV[1], ARGV[3]) end
return 1
"""
READ = "return redis.call('HGETALL', KEYS[1])"


def capture_key(capture_id: str) -> str:
    return PREFIX + "capture:" + str(uuid.UUID(capture_id))


class ProfilingConflict(ValueError):
    """Conflicting capture identity or concurrent capture."""


class ProfilingControl:
    """One capture per process, one admitted cluster capture, no auth bypass."""

    def __init__(
        self,
        redis: RedisService,
        directory: ControllerInstanceDirectory,
        owner_id: str,
        memory_summary: MemorySummary,
    ) -> None:
        self.redis = redis
        self.directory = directory
        self.owner_id = owner_id
        self.memory_summary = memory_summary
        self.enabled = os.getenv("COGNIS_PROFILING_ALLOWED", "true").lower() in {"true", "1"}
        self.local: LocalCapture | None = None
        self.task: asyncio.Task[None] | None = None
        self.stopping = asyncio.Event()
        self._published: tuple[str, str] | None = None
        self.last_error: str | None = None
        selected = os.getenv("COGNIS_PROFILING_BOOT_POD", "")
        level = os.getenv("COGNIS_PROFILING_BOOT_LEVEL", "").lower()
        legacy_boot = not level
        if not level:
            memory = os.getenv("COGNIS_MEMORY_PROFILING", "").lower() in {"true", "1"}
            resource = os.getenv("COGNIS_RESOURCE_PROFILING", "").lower() in {"true", "1"}
            memory &= os.getenv("COGNIS_MEMORY_PROFILE_POD", "") in {"", socket.gethostname()}
            resource &= os.getenv("COGNIS_RESOURCE_PROFILE_POD", "") in {"", socket.gethostname()}
            level = (
                "full"
                if memory and resource
                else "allocations"
                if memory
                else "cpu"
                if resource
                else ""
            )
        self.boot_options = (
            CaptureOptions(
                level=level,
                duration_seconds=_integer(
                    "COGNIS_PROFILING_BOOT_DURATION_SECONDS",
                    _integer("COGNIS_RESOURCE_PROFILE_DURATION_SECONDS", 120, 10, 600),
                    10,
                    600,
                ),
                sample_interval_ms=_integer("COGNIS_RESOURCE_PROFILE_SAMPLE_MS", 100, 50, 1000),
                allocation_interval_seconds=_integer(
                    "COGNIS_MEMORY_PROFILE_INTERVAL_SECONDS", 60, 30, 300
                ),
                traceback_depth=_integer("COGNIS_MEMORY_PROFILE_TRACEBACK_DEPTH", 5, 1, 10),
                allocation_snapshots=not legacy_boot
                or os.getenv("COGNIS_MEMORY_PROFILE_SNAPSHOTS", "").lower() in {"true", "1"},
            )
            if level in {"light", "cpu", "allocations", "full"}
            and selected in {"", socket.gethostname()}
            else None
        )
        self.boot_targets = [owner_id] if selected or legacy_boot else None
        self.boot_at = time.monotonic() + _integer(
            "COGNIS_PROFILING_BOOT_DELAY_SECONDS",
            _integer("COGNIS_RESOURCE_PROFILE_DELAY_SECONDS", 300, 0, 3600),
            0,
            3600,
        )
        self.boot_id = str(uuid.uuid4())

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        result = await self.redis.eval(script, keys=keys, args=args)
        if result is None:
            raise RedisUnavailableError("Profiling coordination unavailable")
        return result

    async def discover(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "allowed": self.enabled,
            "ready_owners": await self.directory.list_ready_owners(),
            "levels": ["light", "cpu", "allocations", "full"],
            "local": self.local.status() if self.local else None,
            "coordination_error": self.last_error,
        }

    async def admit(
        self,
        capture_id: str,
        options: CaptureOptions,
        targets: list[str] | None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise PermissionError("Profiling disabled by deployment")
        capture_id = str(uuid.UUID(capture_id))
        digest = hashlib.sha256(
            json.encode(
                {
                    "options": options.model_dump(),
                    "targets": sorted(targets) if targets else "all",
                },
                sort_keys=True,
            )
        ).hexdigest()
        # A retry resolves the original owner snapshot, not replacement processes.
        previous = await self.read(capture_id, missing_ok=True)
        if previous:
            if previous["meta"]["digest"] != digest:
                raise ProfilingConflict("Capture ID already has different options")
            return previous
        ready = await self.directory.list_ready_owners()
        owners = sorted(set(targets or ready))
        if not owners or len(owners) > 8 or not set(owners).issubset(ready):
            raise ValueError("Select one to eight currently ready controller owners")
        now = time.time()
        meta = {
            "capture_id": capture_id,
            "digest": digest,
            "owners": owners,
            "options": options.model_dump(),
            "created_at": now,
            "expires_at": now + options.duration_seconds + 10,
            "report_expires_at": now + 3600,
        }
        result = await self._eval(
            ADMIT,
            [ACTIVE, INDEX, capture_key(capture_id)],
            [
                capture_id,
                digest,
                json.encode(meta),
                PREFIX + "capture:",
                options.duration_seconds + 70,
            ],
        )
        if result in (b"busy", b"conflict", "busy", "conflict"):
            raise ProfilingConflict("Another capture is active or ID conflicts")
        logger.info("profiling_capture_admitted capture_id=%s targets=%s", capture_id, len(owners))
        return await self.read(capture_id)

    async def read(self, capture_id: str, *, missing_ok: bool = False) -> dict[str, Any]:
        flat = await self._eval(READ, [capture_key(capture_id)], [])
        if not flat:
            if missing_ok:
                return {}
            raise KeyError("Capture expired or not found")
        entries = {
            (key.decode() if isinstance(key, bytes) else key): value
            for key, value in zip(flat[::2], flat[1::2], strict=True)
        }
        meta = json.decode(entries["meta"])
        desired = entries["desired"]
        result = {
            "meta": meta,
            "desired": desired.decode() if isinstance(desired, bytes) else desired,
            "controllers": {},
        }
        for owner in meta["owners"]:
            raw = entries.get("status:" + owner)
            observation = json.decode(raw) if raw else {"state": "pending"}
            observation["stale"] = (
                observation["state"] not in TERMINAL
                and time.time() - observation.get("observed_at", 0) > 20
            )
            result["controllers"][owner] = observation
        result["reports"] = {
            owner: json.decode(entries["report:" + owner])
            for owner in meta["owners"]
            if "report:" + owner in entries
        }
        return result

    async def request_stop(self, capture_id: str) -> dict[str, Any]:
        result = await self._eval(STOP, [capture_key(capture_id)], [])
        if not result:
            raise KeyError("Capture expired or not found")
        logger.info("profiling_capture_stop_requested capture_id=%s", capture_id)
        return await self.read(capture_id)

    async def start(self) -> None:
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self._run(), name="profiling-control")

    async def reconcile(self) -> None:
        active = await self.redis.get(ACTIVE)
        if not self.redis.available:
            raise RedisUnavailableError("Profiling coordination unavailable")
        command = None
        if active:
            capture_id = active.decode()
            # Read only metadata/desired, never large reports in the idle control loop.
            raw = await self._eval(
                "return redis.call('HMGET', KEYS[1], 'meta', 'desired')",
                [capture_key(capture_id)],
                [],
            )
            if raw and raw[0]:
                command = json.decode(raw[0])
                command["desired"] = raw[1].decode() if isinstance(raw[1], bytes) else raw[1]
        local = self.local
        if local and local.state in TERMINAL:
            # An outage may have left the only complete report in this process.
            # Do not replace it with a successor before publication succeeds.
            await self._retire(local)
        if command and self.owner_id in command["owners"]:
            self.boot_options = None
        elif self.boot_options and time.monotonic() >= self.boot_at and not command:
            await self.admit(self.boot_id, self.boot_options, self.boot_targets)
            self.boot_options = None
            return
        if (
            local
            and local.state not in TERMINAL
            and (
                not command
                or command["capture_id"] != local.capture_id
                or command["desired"] == "stop"
                or time.time() >= command["expires_at"]
                or self.owner_id not in command["owners"]
                or not self.enabled
            )
        ):
            await self._retire(local)
        if (
            command
            and self.owner_id in command["owners"]
            and (local is None or local.capture_id != command["capture_id"])
            and (command["desired"] == "stop" or time.time() >= command["expires_at"])
        ):
            self.local = LocalCapture(
                command["capture_id"],
                CaptureOptions.model_validate(command["options"]),
                self.memory_summary,
            )
            self.local.state = "stopped"
            self.local.finished_at = time.time()
            self._published = None
        elif (
            command
            and self.enabled
            and self.owner_id in command["owners"]
            and command["desired"] == "start"
            and time.time() < command["expires_at"]
            and (local is None or local.capture_id != command["capture_id"])
        ):
            options = CaptureOptions.model_validate(command["options"])
            remaining = max(0, int(command["expires_at"] - time.time() - 10))
            if remaining < 1:
                return
            options = options.model_copy(
                update={"duration_seconds": min(options.duration_seconds, remaining)}
            )
            self.local = LocalCapture(command["capture_id"], options, self.memory_summary)
            self.local.start()
            self._published = None
        if self.local:
            await self._publish(self.local)

    async def _retire(self, capture: LocalCapture) -> None:
        """The only predecessor retirement boundary: stop, join, then publish.

        The caller retains self.local on any publication error. A successor
        cannot take ownership until the terminal evidence has been written.
        """
        if capture.state not in TERMINAL:
            capture.state = "stopping"
            try:
                await self._publish(capture)
            finally:
                await capture.stop()
        await self._publish(capture)

    async def _publish(self, capture: LocalCapture) -> None:
        terminal = capture.state in TERMINAL
        marker = (capture.capture_id, capture.state)
        if terminal and self._published == marker:
            return
        status = {**capture.status(), "observed_at": time.time(), "owner_id": self.owner_id}
        report = b""
        if terminal:
            report = json.encode(capture.report)
            if len(report) > 131072:
                capture.report["truncated"] = True
                omitted = capture.report.setdefault(
                    "omitted", {"cpu_stack_rows": 0, "allocation_samples": 0, "memory_samples": 0}
                )
                # Keep endpoint evidence and hottest stacks before intermediate samples.
                memory = capture.report.get("resources", {}).get("memory", [])
                while len(report) > 131072 and len(memory) > 2:
                    memory.pop(1)
                    omitted["memory_samples"] += 1
                    report = json.encode(capture.report)
                allocations = capture.report["allocations"]
                while len(report) > 131072 and len(allocations) > 2:
                    allocations.pop(1)
                    omitted["allocation_samples"] += 1
                    report = json.encode(capture.report)
                cpu = capture.report.get("resources", {}).get("cpu") or {}
                stacks = cpu.get("top_endpoint_stacks", [])
                while len(report) > 131072 and stacks:
                    stacks.pop()
                    omitted["cpu_stack_rows"] += 1
                    report = json.encode(capture.report)
                if len(report) > 131072:
                    report = json.encode({"truncated": True, "errors": ["report_size_exceeded"]})
        await self._eval(
            OBSERVE,
            [capture_key(capture.capture_id)],
            [self.owner_id, json.encode(status), report],
        )
        self._published = marker
        if terminal:
            logger.info(
                "profiling_capture_finished capture_id=%s state=%s",
                capture.capture_id,
                capture.state,
            )

    async def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.reconcile()
                self.last_error = None
            except Exception:
                self.last_error = "coordination_unavailable"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=5)

    async def stop(self) -> None:
        self.stopping.set()
        if self.task is not None:
            from cognis.core.profiling_capture import finish

            await finish(self.task)
        if self.local:
            with contextlib.suppress(RedisUnavailableError):
                await self._retire(self.local)
