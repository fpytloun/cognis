"""Validate synthetic frontend packets against the immutable public JSON schemas.

This imports JSON Schema tooling only, never Cognis or the mutable API checkout.
Run under the repository's shared validation lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections.abc import Iterator
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

ROOT = pathlib.Path(__file__).resolve().parent / "contracts"
HISTORICAL_HASHES = {
    "usage-query-contract.json": "ebdc69a21ade97f5fbef04b3130b62c85988c21b2398249a0bc13d2b4de08be4",
    "usage-query-api.md": "0a278f28368e9e36853f4afa3249b02dd3dfd101d08fd6275db2cc679a0fe9a4",
}


def load_schemas(contract: str, root: pathlib.Path = ROOT) -> dict[str, Any]:
    """Read only frozen export files, verifying their generated hash pins first."""
    directory = root
    hashes = HISTORICAL_HASHES
    if contract == "current":
        pin = json.loads((root / "current.json").read_bytes())
        assert pin["format_version"] == 1
        directory = root / pin["directory"]
        assert directory.parent == root
        hashes = pin["sha256"]
        assert set(hashes) == {
            "usage-query-contract.json",
            "usage-query-api.md",
            "provenance.json",
        }
    for name, expected in hashes.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Contract SHA-256 mismatch: {name}")
    if contract == "current":
        manifest = json.loads((directory / "provenance.json").read_bytes())
        assert manifest["format_version"] == 1
        assert manifest["model_source_commit"][:12] == directory.name
        assert manifest["files"] == {
            name: hashes[name] for name in ("usage-query-contract.json", "usage-query-api.md")
        }
    return json.loads((directory / "usage-query-contract.json").read_bytes())


def leaves(error: ValidationError) -> Iterator[ValidationError]:
    """Retain concrete branch failures rather than redundant null-union errors."""
    if error.context:
        for child in error.context:
            yield from leaves(child)
    elif error.schema.get("type") != "null":
        yield error


def main() -> None:
    """Validate current packets by default; retain the original historical export."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", choices=("current", "historical"), default="current")
    args = parser.parse_args()
    schemas = load_schemas(args.contract)
    packets = json.load(sys.stdin)
    failures = []
    for packet in packets:
        validator = Draft202012Validator(schemas[packet["schema"]], format_checker=FormatChecker())
        for error in validator.iter_errors(packet["value"]):
            for leaf in leaves(error):
                failures.append(f"{packet['schema']} {list(leaf.absolute_path)}: {leaf.message}")
    if failures:
        sys.stderr.write("\n".join(failures[:20]) + "\n")
        sys.exit(1)
    print(
        f"Validated {len(packets)} packets against the hash-frozen {args.contract} public contract."
    )


if __name__ == "__main__":
    main()
