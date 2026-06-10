# OfficeCLI executor-native Office tools

Cognis exposes Office document tooling through the `office` tool group. The
public API is curated (`office_read`, `office_get`, `office_query`,
`office_validate`, `office_render`, `office_create`, `office_patch`) and does
not expose raw OfficeCLI shell execution.

## Certified runtime

OfficeCLI is an executor-local capability. Cognis currently certifies
`v1.0.102` only. Unknown versions do not expose Office tools. Runtime
executor configuration appends the `office_*` tools only after the executor has
resolved a certified, checksum-verified OfficeCLI runtime. Static API discovery
may list the curated Office tool schemas for management/documentation, but that
does not bypass per-executor runtime gating.

Certified assets:

- Linux x64: `officecli-linux-x64`,
  `d58438a2d701ec68685bb04bb043b546696b1620f141e670eaf438dae5898a66`
- Linux arm64: `officecli-linux-arm64`,
  `dedca5682cad211df9c75886936b441475cf7840a8bd6974dcbd2278c7f1d1a1`
- macOS arm64: `officecli-mac-arm64`,
  `964b23466549681f5283c922cc535d8914d8d089d453bb5d0a100cec6c5fe206`
- macOS x64: `officecli-mac-x64`,
  `99be36950b43782cc0e02ba6be4afe5db3d6b4788f0fb32853f4d3ead5950b78`

The runtime resolver checks the platform, version, and SHA256 hash. If the
certified binary is missing and auto-install is enabled, it downloads only the
pinned release asset into a versioned cache path under
`$COGNIS_DATA_DIR/cache/officecli/<version>/<platform>/officecli`.

Every OfficeCLI subprocess sets `OFFICECLI_SKIP_UPDATE=1`.

## Artifact behavior

Office tools accept either a local `source_path` or `source_artifact_id`.
Artifact bytes are hidden from guardrails; guardrails see only safe metadata
such as filename, MIME type, and size. The executor receives base64 content
after authorization and materializes it into a temporary directory.

Mutating tools work on a temp copy by default and return a new artifact unless
an explicit `output_path`/`publish_artifact=false` behavior is requested. When
artifact publishing is disabled and no output path is supplied, create/patch
handlers write to a persistent executor workspace-relative output path rather
than returning a deleted temporary path. Use `expected_sha256` or
`expected_base_sha256` to reject stale inputs; hashes are computed over the
exact materialized bytes.

## Version bump checklist

1. Update `OFFICECLI_CERTIFIED_VERSION` and asset checksums in
   `cognis/tools/executor/officecli/manifest.py`.
2. Update Docker build checksums in `Dockerfile.executor`.
3. Re-run installer/manifest/handler tests.
4. Run gated golden OfficeCLI tests against DOCX/XLSX/PPTX when available.
5. Document any command-shape or output differences.
