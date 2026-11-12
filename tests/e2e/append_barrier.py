"""Fixture-owned, loopback-only real Intaris forwarding with one append gate.

Control is an in-process object, not an HTTP control API. The fixture must run
the ASGI app on its owned loopback server and close it in teardown.
"""

import asyncio
import socket
import threading
from contextlib import asynccontextmanager
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from cognis.testing.barrier import RequestBarrier


class AppendBarrierProxy:
    def __init__(self, upstream: str, *, transport=None) -> None:
        parsed = urlsplit(upstream)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected fixture-owned loopback Intaris origin")
        self.client = httpx.AsyncClient(
            base_url=upstream,
            follow_redirects=False,
            timeout=30,
            transport=transport,
            trust_env=False,
        )
        self.target: tuple[str, str] | None = None
        self.barrier: RequestBarrier | None = None
        self._request_metadata_lock = threading.Lock()
        self._request_metadata: list[dict[str, object]] = []

        @asynccontextmanager
        async def lifespan(_app):
            try:
                yield
            finally:
                await self.close()

        self.app = Starlette(
            routes=[
                Route(
                    "/{path:path}", self.forward, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
                )
            ],
            lifespan=lifespan,
        )

    def arm(self, session_id: str, client_message_id: str) -> RequestBarrier:
        if self.barrier is not None:
            raise RuntimeError("Only one append barrier may be armed")
        if not session_id or "/" in session_id or not client_message_id:
            raise ValueError("Expected fixture-issued session and admission identities")
        self.target = (session_id, client_message_id)
        self.barrier = RequestBarrier()
        return self.barrier

    async def close(self) -> None:
        if self.barrier:
            self.barrier.cancel()
        await self.client.aclose()

    def snapshot_request_metadata(self) -> list[dict[str, object]]:
        with self._request_metadata_lock:
            return [dict(item) for item in self._request_metadata]

    def _record_request_metadata(self, metadata: dict[str, object]) -> None:
        with self._request_metadata_lock:
            self._request_metadata.append(metadata)

    async def forward(self, request: Request) -> Response:
        if request.client is None or request.client.host != "127.0.0.1":
            return Response(status_code=403)
        if (
            not request.url.path.startswith("/api/")
            and not (request.method == "GET" and request.url.path == "/health")
        ) or "\\" in request.url.path:
            return Response(status_code=400)
        raw = await request.body()
        if (
            self.target
            and request.method == "POST"
            and request.url.path == f"/api/v1/session/{self.target[0]}/events"
        ):
            events = await request.json()
            if any(
                event.get("type") == "user_message"
                and event.get("data", {}).get("client_message_id") == self.target[1]
                for event in events
            ):
                assert self.barrier is not None
                await self.barrier.hold()
        # Preserve body, query, and auth. Only hop-specific headers are removed.
        headers = [
            (k, v)
            for k, v in request.headers.raw
            if k.lower() not in (b"host", b"connection", b"transfer-encoding")
        ]
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query
        target = self.client.base_url
        if request.url.path == "/health":
            operation = "health"
        elif request.url.path.endswith("/events"):
            operation = "record_events" if request.method == "POST" else "read_events"
        else:
            operation = "other"
        started = perf_counter()
        try:
            response = await self.client.request(
                request.method,
                path,
                headers=headers,
                content=raw,
            )
        except Exception as exc:
            self._record_request_metadata(
                {
                    "operation": operation,
                    "method": request.method,
                    "scheme": target.scheme,
                    "host": target.host,
                    "port": target.port,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((perf_counter() - started) * 1000, 1),
                }
            )
            raise
        self._record_request_metadata(
            {
                "operation": operation,
                "method": request.method,
                "scheme": target.scheme,
                "host": target.host,
                "port": target.port,
                "status_code": response.status_code,
                "duration_ms": round((perf_counter() - started) * 1000, 1),
            }
        )
        result = Response(content=response.content, status_code=response.status_code)
        result.raw_headers = [
            (k, v)
            for k, v in response.headers.raw
            if k.lower()
            not in (b"connection", b"transfer-encoding", b"content-encoding", b"content-length")
        ]
        result.headers["content-length"] = str(len(response.content))
        return result


class AppendProxyServer:
    """Own one loopback listener and event loop; controls never cross HTTP."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.failure: BaseException | None = None

    def _run(self) -> None:
        import uvicorn

        async def serve():
            self.loop = asyncio.get_running_loop()
            self.proxy = AppendBarrierProxy(self.upstream)
            ready = self.ready

            class FixtureServer(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets=sockets)
                    ready.set()

            self.server = FixtureServer(
                uvicorn.Config(
                    self.proxy.app,
                    host="127.0.0.1",
                    access_log=False,
                    log_level="critical",
                )
            )
            await self.server.serve(sockets=[self.socket])

        try:
            asyncio.run(serve())
        except BaseException as exc:
            self.failure = exc
            self.ready.set()

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(30):
            raise TimeoutError("Append proxy startup failed")
        if self.failure:
            raise RuntimeError("Append proxy startup failed") from self.failure

    def _call(self, function, *args):
        async def invoke():
            return function(*args)

        return asyncio.run_coroutine_threadsafe(invoke(), self.loop).result(timeout=30)

    def arm(self, session_id: str, client_message_id: str) -> None:
        self._call(self.proxy.arm, session_id, client_message_id)

    def wait_entered(self) -> None:
        async def wait():
            assert self.proxy.barrier is not None
            async with asyncio.timeout(30):
                await self.proxy.barrier.entered.wait()

        asyncio.run_coroutine_threadsafe(wait(), self.loop).result(timeout=35)

    def release(self) -> None:
        self._call(self.proxy.barrier.release)

    def request_metadata(self) -> list[dict[str, object]]:
        return self._call(self.proxy.snapshot_request_metadata)

    def cancel(self) -> None:
        def cancel():
            if self.proxy.barrier:
                self.proxy.barrier.cancel()
            self.proxy.barrier = None
            self.proxy.target = None

        self._call(cancel)

    def close(self) -> None:
        if hasattr(self, "loop") and self.thread.is_alive():
            self.cancel()
            self.loop.call_soon_threadsafe(setattr, self.server, "should_exit", True)
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                raise TimeoutError("Append proxy did not stop")
        self.socket.close()
