"""Launch the real peer app with disposable storage and no outbound inference."""

from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

import intaris
import uvicorn
from intaris.llm import LLMClient
from intaris.server import _get_config, create_app


def main() -> None:
    """Serve the unmodified session API; fail if any inference or connect occurs."""
    safety: dict[str, Any] = {
        "python": sys.executable,
        "intaris_module": intaris.__file__,
        "outbound_attempts": 0,
        "model_calls": 0,
    }

    def forbid_connect(*args: Any, **kwargs: Any) -> None:
        safety["outbound_attempts"] += 1
        raise AssertionError("Outbound networking is forbidden in wire acceptance")

    def forbid_model(*args: Any, **kwargs: Any) -> None:
        safety["model_calls"] += 1
        raise AssertionError("Model inference is forbidden in wire acceptance")

    socket.socket.connect = forbid_connect
    socket.socket.connect_ex = forbid_connect
    LLMClient.generate = forbid_model
    report = Path(os.environ["INTARIS_WIRE_SAFETY"])
    atexit.register(lambda: report.write_text(json.dumps(safety, indent=2)))
    config = _get_config()
    safety.update(
        analysis_enabled=config.analysis.enabled,
        judge_mode=config.judge.mode,
        database=config.db.path,
    )
    assert config.analysis.enabled is False
    assert config.judge.mode == "disabled"
    # Uvicorn restores/re-raises its shutdown signal. Let it finish normally so
    # the interpreter can export the safety ledger after graceful lifespan exit.
    signal.signal(signal.SIGTERM, lambda *_: None)
    uvicorn.run(
        create_app(),
        fd=int(sys.argv[1]),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
