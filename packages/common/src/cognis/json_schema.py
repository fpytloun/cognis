"""JSON Schema validation helpers with external reference loading disabled."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry


def offline_draft202012_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Build a validator that resolves local refs but never fetches URLs."""

    return Draft202012Validator(schema, registry=Registry())
