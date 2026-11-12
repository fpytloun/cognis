"""Refresh the bundled LiteLLM model price and context catalog.

Run from a Cognis checkout as an explicit, reviewed release step:

    uv run python scripts/update_model_prices.py

Cognis never downloads this catalog at runtime. The snapshot is loaded from
disk at startup on top of the catalog shipped with the pinned LiteLLM package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SOURCE = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
TARGET = Path(__file__).resolve().parents[1] / "cognis/providers/llm/data/litellm_model_prices.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=SOURCE)
    args = parser.parse_args()
    with urllib.request.urlopen(args.source, timeout=60) as response:  # noqa: S310
        raw = response.read()
    models = json.loads(raw)
    if not isinstance(models, dict) or len(models) < 1000:
        print("Unexpected catalog shape; snapshot not updated", file=sys.stderr)
        return 1
    models.pop("sample_spec", None)
    snapshot = {
        "source": args.source,
        "fetched_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "models": {name: models[name] for name in sorted(models) if isinstance(models[name], dict)},
    }
    TARGET.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {len(snapshot['models'])} models to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
