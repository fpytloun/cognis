"""Entry point for ``python -m cognis.executor``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Cognis executor runner")
    parser.add_argument(
        "--controller-url",
        required=True,
        help="WebSocket URL of the Cognis controller (e.g. wss://host:8080/api/executor/ws)",
    )
    parser.add_argument("--token", required=True, help="JWT authentication token")
    parser.add_argument(
        "--executor-id", required=True, help="Executor ID (must match DB registration)"
    )
    parser.add_argument(
        "--tools",
        default="*",
        help="Comma-separated tool names or '*' for all (default: '*')",
    )
    parser.add_argument(
        "--inference-endpoint",
        default=None,
        help="Local LLM endpoint URL (e.g. http://localhost:11434/v1)",
    )
    parser.add_argument(
        "--inference-model",
        default=None,
        help="Default model for inference (e.g. llama3.2)",
    )
    parser.add_argument(
        "--config-json",
        default=None,
        help="Full ExecutorConfig as JSON (used by subprocess spawner)",
    )

    args = parser.parse_args()

    # Enforce wss:// for non-localhost URLs
    url = args.controller_url
    if not url.startswith("wss://") and not _is_localhost(url):
        print(  # noqa: T201 — CLI output
            "ERROR: Remote executor connections require wss:// (TLS). "
            "Use ws:// only for localhost connections.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Read token + secrets from stdin if available (subprocess mode).
    # Format: first line is JWT token (optional), remaining is secrets JSON.
    stdin_token: str | None = None
    secrets: dict[str, str] = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                lines = raw.split("\n", 1)
                first_line = lines[0].strip()
                # If first line looks like a JWT (contains dots), treat as token
                if first_line and "." in first_line:
                    stdin_token = first_line
                    remainder = lines[1].strip() if len(lines) > 1 else ""
                else:
                    remainder = raw.strip()
                if remainder:
                    secrets = json.loads(remainder)
        except (json.JSONDecodeError, OSError):
            pass

    from cognis.executor.runner import ExecutorRunner
    from cognis.models.tool import ExecutorConfig, InferenceConfig

    inference: InferenceConfig | None = None
    if args.inference_endpoint:
        inference = InferenceConfig(
            type="openai_compatible",
            endpoint=args.inference_endpoint,
            default_model=args.inference_model,
            models=[args.inference_model] if args.inference_model else [],
        )

    # Resolve token: stdin takes priority (subprocess mode), then CLI arg
    effective_token = stdin_token or args.token

    if args.config_json:
        config = ExecutorConfig.model_validate_json(args.config_json)
        # Merge stdin token and secrets
        if effective_token:
            config.controller_token = effective_token
        if secrets:
            config.secrets.update(secrets)
    else:
        config = ExecutorConfig(
            executor_id=args.executor_id,
            controller_url=url,
            controller_token=effective_token,
            inference=inference,
            secrets=secrets,
            metadata={"enabled_tools": args.tools},
        )

    runner = ExecutorRunner(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run())


def _is_localhost(url: str) -> bool:
    """Check if a URL points to localhost."""
    for prefix in ("ws://localhost", "ws://127.0.0.1", "ws://[::1]", "ws://0.0.0.0"):
        if url.startswith(prefix):
            return True
    return False


if __name__ == "__main__":
    main()
