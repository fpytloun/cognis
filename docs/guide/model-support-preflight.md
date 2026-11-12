# Credential-free model-support check

From a Cognis checkout, run:

```sh
uv run python scripts/check_model_support.py
```

The command checks public Anthropic and OpenAI API documentation and the
upstream Codex model catalog. It does not use provider credentials or the
authenticated model-list APIs. It compares relevant Codex capabilities directly
with `cognis/providers/llm/data/codex_models.json`, and compares public model
IDs, documented Claude attributes, and current GPT model-page capabilities
and pricing with the reviewed `scripts/model_support_baseline.json`. Large
upstream Codex instruction templates and unrelated editorial documentation
changes do not trigger an update.

GPT Image IDs (`gpt-image-*`) are monitored alongside numbered GPT IDs,
including lifecycle labels. GPT Image 2.6 and newer model pages are checked
for input/output modalities, native endpoint support, and separate text/image
token prices. Image models do not have the chat context/output-token fields;
missing image metadata is an incomplete check, not a silent no-op.

The OpenAI overview is an abbreviated index, not a retirement list. The full
catalog supplies tracked IDs and their `listed`, `deprecated`, or explicitly
`shutdown` documentation status. The output also identifies IDs missing from
the overview; that observation alone is not a status change. A missing tracked
or indexed ID in the full catalog, incomplete catalog structure, or an
unrecognized lifecycle label makes the check incomplete rather than removing
the ID. These documentation labels do not change runtime model support.

For the scheduled check, use:

```sh
uv run python scripts/check_model_support.py --fetch-origin
```

This fetches the latest Cognis `origin/main` and compares **that revision**,
without moving the checked-out branch or changing local files. Access to a
private Git origin can still require normal Git authentication; the public model
sources do not require API keys. The output is one compact JSON object:

| Exit | Status | Meaning |
|---|---|---|
| `0` | `no_change` | All required public sources checked; no relevant difference. Silent scheduled exit. |
| `2` | `change` | Investigate differences and implement full support if warranted. |
| `1` | `incomplete` | Network, parse, Git, or baseline failure. Never treat as no-op. |

After verifying a public-document change, explicitly update the baseline from
a task-owned checkout with `--refresh-baseline` and commit it alongside the
model-support change (or a documented decision that no code change is needed).
Do not run that flag on the scheduled read-only preflight. A newer npm CLI
version alone is not evidence of a new model or a reason to bump Cognis' client
headers; inspect compatibility versions when a model change is found.
Baseline schema 3 records full-catalog lifecycle labels; schema 2 remains
readable so its first reviewed refresh can establish those labels.

`gpt-6-cyber` is recorded in the documentation baseline but runtime support is
deferred: it requires Daybreak Red approval and a restricted request policy.
Runtime support remains deferred. Its documented model-page access notice,
capabilities, and lifecycle remain monitored for changes; a
baseline entry does not imply runtime support.

Public documentation is not an account-specific availability guarantee. Codex
subscription models and OpenAI API models are separate surfaces. The check
intentionally does not access credentials, prove entitlement, probe live
inference, or infer undocumented capability values.
