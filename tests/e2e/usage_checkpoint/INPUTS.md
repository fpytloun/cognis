# Declared offline fixture inputs

This is a consumption boundary, not preparation or runtime permission. It starts
no installer, build, service or browser and discovers no ambient cache. The
parent still owns all dependency, package, frozen-sync, pip, full-app build and
namespace admission gates. An input declaration is not proof that those passed.
Runtime sources remain frozen until the parent integrates this test-only change.

## Ownership and launch contract

1. Trusted bootstrap verifies the two approved BM25 source files with
   `bm25_inputs.read_bm25(archive, manifest, uid=...)`. Both modules are pure stdlib
   and importable with Python `-S`, before application dependencies exist.
2. Bootstrap creates new private namespace-owned runtime directories
   `/work/data/new-cache` and `/work/data/tools`. It installs Mnemory and Intaris
   normally with offline `uv tool install`, using verified companion/build
   constraints. **Never copy or patch a tool venv.** Installation, complete
   dependency closure and real uv/uvx compatibility are bootstrap responsibilities.
3. After preparation, bootstrap constructs frozen `FixtureInputSpec` objects:
   - fresh `launch_id` (`secrets.token_hex(16)`), nonroot worker `uid`;
   - `archive` and `manifest`: `FileInput(path, sha256)`, with the fixed hashes below;
   - `companions`: `CompanionInputs(data_root, cache, tools, constraints, installed)`;
   - `cache` and `tools`: `DirectoryInput.capture(path, uid)`, binding device/inode,
     not just directory names;
   - constraints: `/work/data/companion-constraints.txt`, explicit `FileInput` hash;
   - exactly two `ToolInput(name, receipt, entrypoint)` records: each
     `/work/data/tools/{name}/uv-receipt.toml` and
     `/work/data/tools/{name}/bin/{name}` with bootstrap-observed SHA-256 identities.
     These are the **normal install outputs**, not fabricated Hub metadata or
     replacement entrypoints.
4. `spec.verify()` checks the declaration. `spec.bytes()` is its exact canonical
   JSON (sorted keys, compact separators, no trailing newline). Bootstrap writes
   it as an owned single-link regular file mode `0600`, seals the launch's
   expected SHA-256 separately, and passes `--usage-input-spec PATH` plus
   `--usage-input-sha256 HASH`. A sibling self-asserted manifest/hash is not trust.
   Only the trusted bootstrap/admission owner may issue this launch binding.
5. The `usage_launch` fixture loads that binding before `e2e_stack` enters.
   Usage enabled without either argument fails; ordinary non-usage E2E retains
   its defaults. Inputs are reverified, then staged only in a new `usage-inputs-`
   directory below the stack's private pytest scratch. Copied evidence preserves
   the original archive, manifest, spec and constraints bytes.
6. The worker revalidates the launch binding, complete small model tree,
   constraints, prepared directory identities and declared tool files before the
   append proxy and before each subprocess starts. It never installs, prewarms,
   repairs metadata, copies environments or traverses a uv cache/tool environment.

Schema 1 supports Linux x86_64, Python 3.12 and the declared nonroot UID.
All source files reject symlink ancestors, symlinks, hardlinks, special files,
group/world writes and unbounded content. Runtime directory roots must be private
and writable. Tool entrypoints must be executable. Spec JSON rejects duplicate or
extra keys and noncanonical encodings.

The directory identities and individually declared file hashes do **not** attest
every installed dependency, Python ABI, package version or cache record. The
trusted parent must establish freshness, ordinary installation provenance,
offline closure and exclusion of concurrent preparers before sealing. A string
such as `preparation="uv-tool-install-offline"` cannot independently prove an
installation happened. Do not promote this limited revalidation into admission.

## Supported data only

- Archive: `bm25-cache-input.tar`
  SHA-256 `9a2ec963d4240c6958b0340f78a622e6042ebf14d42685beca366c2f3b4d8b27`.
- Manifest: `verified-manifest.json`
  SHA-256 `d8351b3c97e0863673788c8d502c2edb0323b7bafe1f05a133b9312985628e11`.
- Revision: `22b8d2af71a76161e18dd432d2cee0eefa66e412`.
- Exactly `{}` config, 17 UTF-8 stopword files, and the exact newline-free
  `refs/main`. Every size/hash is checked; stopwords also retain Git blob hashes.
- No weights or additional Hub metadata. Reject extra, unsafe, duplicate, linked
  or mismatched members. Inspect held tar bytes; never call tar extraction.

The narrow verifier is ported from frozen Git object
`da896cb39dba50642fb79c81a59ab82886de7a86`'s `expiry_inputs.py` verified-data
boundary. No old launcher, clock or cache-copy implementation is reused.

## Subprocess and cleanup contract

`tests/e2e/subprocess_env.py` owns the E2E child environment and typed process/log
resources. Usage supplies explicit inputs to `_start_proc`; it does not inject
private process fields or publish a global authority object.

- Each service receives a distinct new HOME/XDG/TMPDIR under the same scratch.
- `UV_CACHE_DIR=/work/data/new-cache`, `UV_TOOL_DIR=/work/data/tools` remain explicit
  across those fresh HOMEs. Neither is inferred from the preparation HOME.
- Mnemory/Intaris retain exact bare `uvx mnemory` / `uvx intaris` invocation.
  `UV_CONSTRAINT` and `UV_BUILD_CONSTRAINT` point to the verified copied
  companion constraints (`packaging==26.3`), with offline/no-Python-download policy.
- Controller/mock remain canonical `uv run`; `UV_NO_SYNC=1` is explicit.
  Companion constraints do not reach them. Controller packaging 26.0 is separate
  from companion/build 26.3 and remains a bootstrap gate. The existing
  source/network-fence PYTHONPATH and controller `UV_PROJECT_ENVIRONMENT` are
  preserved; the latter must identify the parent's fresh approved environment.
  Another controller workspace is rejected; this checkout's existing source
  attestation remains required.
- `FASTEMBED_CACHE_PATH` points only to the worker's verified model scratch, with
  Hub offline mode. No ambient HOME/cache fallback or download is authorized.

Stack finalization is registered before proxy creation. Teardown closes proxy,
clients, then processes in reverse order and their explicitly owned logs.
Only afterward does input cleanup verify its original root identity and remove
that scratch. Any uncertain/live child or failed teardown retains scratch rather
than deleting live inputs. Failures still attempt other child teardown.
Unused scratch is cleaned after early setup failure. Prepared cache/tools belong
to the **parent namespace lease**, not this scratch owner; parent releases those
private volumes only after the stack/children end. No old path is cleaned.

## No-service checks

Positive tests require explicitly supplied approved data. Without that test
argument they report skips, **not qualification**; with an absent/invalid supplied
path they fail. Qualification itself never skips for missing declared inputs.
Tool metadata in these unit tests is inert synthetic data, not an installation,
prewarming or tool-readiness claim.

```sh
: "${BM25_INPUT_DIR:?Set BM25_INPUT_DIR to the approved fixture directory}"
make validate VALIDATION_LOCK_TIMEOUT=30 COMMAND="env UV_OFFLINE=1 UV_NO_SYNC=1 PYTHONPATH=.:packages/common/src:packages/executor/src .venv/bin/python -m pytest tests/e2e/usage_checkpoint/test_inputs.py --usage-input-test-data-dir='$BM25_INPUT_DIR' -q -o addopts= --tb=short"
```

This does not waive separate approval for any namespace, dependency/tool install,
full-app build, service, browser or screenshot qualification.

## Preparing sealed inputs

The admitted cache graph preserves `/work/data/new-cache` and its logical
`/work/data/companion` wheel root, including 77 internal links. Do not rewrite
those roots, add a `/work/data/cache` alias, copy a seed environment, or export
another cache. This fixture never traverses or repairs that graph.

- Keep dependency and Node archive/toolkit preparation records with the
  qualification's private evidence. Verify their hashes and declared wheel,
  source-build and closure receipts before admitting the inputs. Prepared
  inputs are not tool-bootstrap, build or browser proof.
- An explicit native-browser variant must supply paired trusted launch fields
  `COGNIS_E2E_CHROMIUM_EXECUTABLE_PATH` and `COGNIS_E2E_CHROMIUM_SHA256`.
  Record the actual executable version and hash; do not claim it is the bundled
  Playwright revision. The canonical Python bridge inherits both fields through
  `os.environ` for both phases; Playwright config remains the sole browser-binding
  owner. Preparing inputs does not load that configuration or start a browser.

## Historical no-service validation

These results predate the portable command above. They are not qualification of
this release candidate or evidence that the edited command was rerun.

- First admitted batch: sealed preparation records verified; stdlib-only
  import and scoped Ruff passed. Pytest reported **20 passed, 29 setup errors**
  in 31.34s. The verifier incorrectly counted only files, rejecting the pinned
  archive's required parent-directory entries. This is a real failed receipt,
  not a passing baseline. Fail-fast prevented that batch's TypeScript step.
- One explicitly approved evidence-based correction restored the frozen reader's
  exact parent-directory allowlist: `0700`, zero-size directories, unchanged
  `0600` file payloads, UID/GID checks and all hash/path/duplicate/link/extra
  rejection. New negatives cover changed directory modes and linked directories.
- Corrected batch: **51 passed in 30.88s**, no skips/errors, covering
  `test_inputs.py`, `test_expansion.py`, `tests/unit/test_usage_browser_seed.py`
  and `tests/unit/test_usage_browser_app_contract.py`, with the explicit pinned
  BM25 input directory. Stdlib import via Python `-S`, scoped Ruff check/format,
  and strict ES2022/ESNext/Bundler TypeScript for both existing usage browser
  specs passed in the same normally locked batch (`VALIDATION_LOCK_TIMEOUT=30`).

Neither batch started services, tools, browsers, containers or builds, loaded
Playwright configuration, installed/downloaded dependencies or changed auth.
These are development-checkout checks, not fresh-namespace runtime acceptance.
