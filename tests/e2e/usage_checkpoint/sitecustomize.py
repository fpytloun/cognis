"""Opt-in subprocess network fence and source attestation for this test only."""

from __future__ import annotations

import atexit
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

if os.environ.get("COGNIS_USAGE_CHECKPOINT") == "1":
    output = Path(os.environ["COGNIS_USAGE_RUN_DIR"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check_address(address: Any) -> None:
        if not isinstance(address, tuple):
            return
        host = address[0]
        if host in (None, "localhost", b"localhost"):
            return
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            with (output / f"blocked-egress-{os.getpid()}.jsonl").open("a") as stream:
                stream.write(json.dumps({"host": str(host), "port": address[1]}) + "\n")
            raise PermissionError("Usage checkpoint permits loopback network only")

    def connect(self: socket.socket, address: Any) -> Any:
        check_address(address)
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check_address(address)
        return original_connect_ex(self, address)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        check_address((host, port))
        return original_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo

    def attest() -> bool:
        required = (
            "cognis.api.app",
            "cognis.api.routes.usage",
            "cognis.core.usage.query_service",
            "cognis.store.usage.models",
            "cognis.models.usage",
        )
        if not all(name in sys.modules for name in required):
            return False
        origins = {name: str(Path(sys.modules[name].__file__).resolve()) for name in required}
        assets = sys.modules.get("cognis.ui_assets")
        build = assets.resolve_ui_build_dir() if assets else None
        facts = {
            "pid": os.getpid(),
            "cwd": str(Path.cwd().resolve()),
            "python": sys.executable,
            "modules": origins,
            "database_url": os.environ.get("DATABASE_URL"),
            "data_dir": os.environ.get("COGNIS_DATA_DIR"),
            "ui_build": str(build) if build else None,
            "auth_override": False,
            "loopback_only": True,
        }
        (output / f"python-{os.getpid()}.json").write_text(json.dumps(facts, indent=2))
        return True

    def await_imports() -> None:
        for _ in range(1200):
            if attest():
                return
            time.sleep(0.1)

    threading.Thread(target=await_imports, daemon=True).start()
    atexit.register(attest)
