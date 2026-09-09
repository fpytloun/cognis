from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import uvicorn

from cognis.cli import serve as serve_module


def test_serve_disables_uvicorn_proxy_header_rewriting(monkeypatch: object) -> None:
    app = object()
    run_server = Mock()
    monkeypatch.setattr(  # type: ignore[attr-defined]
        serve_module,
        "load_config",
        lambda: SimpleNamespace(host="127.0.0.1", port=8080),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        serve_module,
        "create_app",
        lambda *, shutdown_coordinator: app,
    )
    monkeypatch.setattr(serve_module, "run_server", run_server)  # type: ignore[attr-defined]

    serve_module.serve()

    server_config, coordinator = run_server.call_args.args
    assert isinstance(server_config, uvicorn.Config)
    assert server_config.app is app
    assert server_config.host == "127.0.0.1"
    assert server_config.port == 8080
    assert server_config.proxy_headers is False
    assert coordinator is not None
