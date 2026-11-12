# Frozen usage query exports

`current.json` is the canonical current-export pointer and SHA-256 pin set.
The source-commit-named directory contains a newly generated schema, the actual
committed API specification, and `provenance.json`. Provenance records the full
model source commit/tree, model/spec blob identities and SHA-256 hashes, exporter
hash, Pydantic version, and unchanged request-validation/response-serialization
modes. The generator hash identifies the exporter separately from the model
source commit; it does not claim that the new exporter existed in that commit.

The original top-level `usage-query-contract.json` and `usage-query-api.md`
remain historical exports with their original bytes and pins. They are not the
current export and must not be overwritten or relabelled.

## Regeneration

Use an existing Python 3.12 environment with the locked dependencies and source
roots for this checkout. No app, database, browser, package sync, or download is
needed. Under the repository's normal validation lock:

```sh
PYTHONPATH=.:packages/common/src:packages/executor/src \
make validate VALIDATION_LOCK_TIMEOUT=30 \
  COMMAND='uv run --no-sync --offline python scripts/export_usage_query_contract.py --source-commit <commit> --output ui/e2e/usage/contracts/<commit[:12]>/usage-query-contract.json --publish-current'
```

The exporter refuses imported models from another checkout or model/spec bytes
that differ from the selected committed source. It derives the artifacts and
pins together; do not hand-edit schemas or duplicate current hash constants in
consumers. Regeneration is deterministic with the recorded generator and
Pydantic version. A later source export gets its own commit-named directory.

`validate-fixtures.py` reads frozen files only and defaults to the current
export. `--contract historical` explicitly selects the preserved original
interface. Both paths retain strict JSON Schema field, enum, number, and format
validation. `tests/unit/test_usage_query_export.py` checks model equality,
deterministic bytes/pins, source provenance, real DTO packets, historical
compatibility, and negative validation.
