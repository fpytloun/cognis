"""Entry point: python -m cognis.testing.mock_llm [--port PORT] [--scenarios-dir DIR]"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Cognis mock LLM server for e2e testing")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MOCK_LLM_PORT", "8090")),
        help="Port to listen on (default: 8090 or MOCK_LLM_PORT env)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MOCK_LLM_HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=None,
        help="Directory containing *.yaml scenario files",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("MOCK_LLM_LOG_LEVEL", "info"),
        help="Log level (default: info)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    import uvicorn

    from cognis.testing.mock_llm.server import create_app

    app = create_app(scenarios_dir=args.scenarios_dir)

    print(f"Mock LLM server starting on http://{args.host}:{args.port}")
    print("Control plane: POST /__mock/scenario, POST /__mock/active, GET /__mock/scenarios")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
