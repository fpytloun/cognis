"""Qualify native artifact publish/save over real local TCP without whole-file fixtures.

Run: uv run python scripts/benchmark_binary_transfers.py --size-mib 1024
This uses a disposable database/object directory and never contacts a live service.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path
from time import perf_counter

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from cognis.api.routes.artifacts import router as artifact_router
from cognis.api.routes.transfers import router as transfer_router
from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.artifact_revisions import ContentActor, write_artifact_document
from cognis.core.binary_transfers import BinaryTransferService
from cognis.models.tool import ExecutorHandle
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import create_agent, create_conversation, create_session, create_user
from cognis.tools.executor.document import handle_artifact_publish
from cognis.tools.executor.filesystem import handle_artifact_save
from cognis.tools.registry import ToolExecutionContext
from cognis.transfers.client import file_io
from cognis.transfers.models import DownloadDescriptor, TransferReceipt


def _source(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with path.open("wb") as target:
        remaining = size
        while remaining:
            chunk = os.urandom(min(1024 * 1024, remaining))
            target.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _rss() -> int:
    # Resident pages exclude filesystem page cache; Linux benchmark executor.
    return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")


async def benchmark(size: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="cognis-binary-benchmark-") as directory:
        root = Path(directory)
        if shutil.disk_usage(root).free < size * 5:
            raise RuntimeError("Insufficient temporary disk for a safe benchmark")
        source = root / "source.bin"
        expected_hash = await file_io(_source, source, size)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        engine = create_engine(f"sqlite+aiosqlite:///{root}/benchmark.db")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = create_session_factory(engine)
        owner = "binary-benchmark@example.com"
        async with sessions() as db:
            await create_user(db, email=owner, name="Binary benchmark", password_hash="test-hash")
            await create_agent(db, agent_id="binary-benchmark", owner_email=owner, name="Benchmark")
            await create_conversation(
                db, owner, "binary-benchmark", "web", conversation_id="binary-benchmark"
            )
            await create_session(
                db, "binary-benchmark", owner, "binary-benchmark", session_id="binary-benchmark"
            )
            await db.commit()
        store = ArtifactStore(
            ArtifactStoreConfig(
                path=str(root / "objects"), base_url=base_url, signing_secret=os.urandom(32).hex()
            )
        )
        service = BinaryTransferService(sessions, store)
        app = FastAPI()
        app.state.session_factory = sessions
        app.state.artifact_store = store
        app.state.binary_transfers = service
        app.include_router(transfer_router)
        app.include_router(artifact_router)

        @app.post("/benchmark/raw")
        async def raw(request: Request) -> dict:
            count, digest = await store.async_receive_stream(
                "artifacts",
                "benchmark-raw",
                "source.bin",
                request.stream(),
                "application/octet-stream",
                owner,
                expected_size=size,
            )
            return {"size_bytes": count, "sha256": digest}

        @app.get("/benchmark/ping")
        async def ping() -> dict:
            return {"alive": True}

        @app.get("/benchmark/raw")
        async def raw_download() -> StreamingResponse:
            return StreamingResponse(
                store.async_read_range("artifacts", "benchmark-raw", "source.bin", 0, size - 1),
                headers={"Content-Length": str(size)},
            )

        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", ws="none"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        baseline_rss = _rss()
        peak_rss = baseline_rss
        probe_latencies = []
        stop = asyncio.Event()

        async def probe() -> None:
            nonlocal peak_rss
            async with httpx.AsyncClient() as http:
                while not stop.is_set():
                    start = perf_counter()
                    response = await http.get(f"{base_url}/benchmark/ping")
                    response.raise_for_status()
                    probe_latencies.append(perf_counter() - start)
                    peak_rss = max(peak_rss, _rss())
                    await asyncio.sleep(0.02)

        probe_task = asyncio.create_task(probe())
        try:

            async def source_body():
                with source.open("rb") as stream:
                    while chunk := await file_io(stream.read, 1024 * 1024):
                        yield chunk

            started = perf_counter()
            async with httpx.AsyncClient(timeout=None) as http:
                baseline = await http.post(f"{base_url}/benchmark/raw", content=source_body())
                baseline.raise_for_status()
                assert baseline.json()["sha256"] == expected_hash
            raw_seconds = perf_counter() - started
            started = perf_counter()
            raw_digest = hashlib.sha256()
            with (root / "raw-download.bin").open("wb") as target:

                def write_raw(chunk: bytes) -> None:
                    target.write(chunk)
                    raw_digest.update(chunk)

                async with (
                    httpx.AsyncClient(timeout=None) as http,
                    http.stream("GET", f"{base_url}/benchmark/raw") as response,
                ):
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        await file_io(write_raw, chunk)
                await file_io(target.flush)
                await file_io(os.fsync, target.fileno())
            assert raw_digest.hexdigest() == expected_hash
            raw_download_seconds = perf_counter() - started
            await store.async_delete_object("artifacts", "benchmark-raw")
            scope_id, grant = await service.issue(
                owner_email=owner,
                agent_id="binary-benchmark",
                conversation_id="binary-benchmark",
                session_id="binary-benchmark",
                call_id="binary-benchmark",
                tool_name="artifact_publish",
                executor_id="binary-benchmark",
            )
            context = ToolExecutionContext(
                executor_handle=ExecutorHandle(
                    executor_id="binary-benchmark", executor_type="websocket"
                ),
                runtime_metadata={
                    "binary_transfer_grant": grant.model_dump(),
                    "workspace_root": str(root),
                },
            )
            started = perf_counter()
            result = await handle_artifact_publish({"path": str(source)}, context)
            assert not result.is_error, result.output
            upload_seconds = perf_counter() - started
            wire_bytes = len(result.model_dump_json().encode())
            assert wire_bytes < 4096
            assert "content_b64" not in result.model_dump_json()
            receipt = TransferReceipt.model_validate(result.attachments[0]["transfer_receipt"])
            assert receipt.sha256 == expected_hash
            await service.close_scope(scope_id)
            staged = await service.claim(
                receipt,
                owner_email=owner,
                session_id="binary-benchmark",
                call_id="binary-benchmark",
            )
            row, _ = await write_artifact_document(
                session_factory=sessions,
                artifact_store=store,
                actor=ContentActor(
                    owner, "binary-benchmark", "binary-benchmark", "binary-benchmark"
                ),
                staged=staged,
                filename=staged.filename,
                mime_type=staged.mime_type,
                kind="file",
                purpose="artifact_publish",
            )
            descriptor = DownloadDescriptor(
                url=await store.async_get_public_url(row.namespace, row.object_id, row.filename),
                size_bytes=size,
                sha256=expected_hash,
                filename=row.filename,
            )
            started = perf_counter()
            saved = await handle_artifact_save(
                {
                    "file_path": str(root / "downloaded.bin"),
                    "source_artifact_id": row.artifact_id,
                    "source_artifact_download": descriptor.model_dump(),
                },
                context,
            )
            assert not saved.is_error, saved.output
            download_seconds = perf_counter() - started
            mib = size / (1024 * 1024)
            return {
                "size_bytes": size,
                "sha256": expected_hash,
                "wire_result_bytes": wire_bytes,
                "upload_seconds": upload_seconds,
                "download_seconds": download_seconds,
                "upload_mib_s": mib / upload_seconds,
                "download_mib_s": mib / download_seconds,
                "raw_upload_mib_s": mib / raw_seconds,
                "raw_download_mib_s": mib / raw_download_seconds,
                "upload_fraction_of_raw": raw_seconds / upload_seconds,
                "download_fraction_of_raw": raw_download_seconds / download_seconds,
                "combined_client_server_rss_growth_mib": (peak_rss - baseline_rss) / (1024 * 1024),
                "max_control_probe_latency_seconds": max(probe_latencies),
                "topology": "local real TCP; filesystem backend; client and controller share a process",
            }
        finally:
            stop.set()
            await probe_task
            server.should_exit = True
            await server_task
            listener.close()
            await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mib", type=int, default=1024)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(args.size_mib * 1024 * 1024)), indent=2))
