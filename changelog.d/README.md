# Release fragments

Cognis uses [Towncrier](https://towncrier.readthedocs.io/) so parallel changes
do not compete for one changelog section. One fragment describes one notable
user/operator-facing change, not one commit.

Create `changelog.d/<unique-kebab-case-name>.<category>.md`. Categories are
`added`, `changed`, `fixed`, `security`, `deprecated`, `removed`, and `upgrade`.
Write concise English prose without a heading or leading bullet. State what
changed for users; explain action required for breaking changes in an additional
`upgrade` fragment. Do not include secrets, private URLs, customer data, or
internal investigation logs. Combine related fixes into a coherent note.

Add a fragment for features, observable bug fixes, security changes,
deprecations/removals, and changes to supported configuration or deployment.
Skip pure refactors, test-only changes, typo fixes, and documentation that
only explains existing behavior. Never invent issue numbers. Name the file
after the change when no issue exists.

## During development

```sh
uv sync --all-packages --all-extras
uv run python scripts/changelog.py
uv run python scripts/changelog.py --check
```

Commit the fragment and generated `ui/src/lib/generated/changelog.json`
together. The index contains the upcoming preview plus finalized history.
The app reads this bundled index; it never needs GitHub, Git, or Python at
runtime. Do not edit the generated JSON. CI checks fragment validity and index
freshness, not whether every commit has a fragment.

## Preparing and finalizing a release

1. Review the fragment preview and documentation against the changes since the
   previous **private** `internal-v*` tag. Public history is curated separately.
2. Set `tool.cognis.changelog.next_version` in `pyproject.toml` to the intended
   version. Preview/export notes without modifying history:

   ```sh
   uv run python scripts/changelog.py --version 0.18.0
   uv run python scripts/changelog.py --since 0.16.0 --version 0.18.0
   ```

3. Only when finalizing, use the actual release date and agreed version:

   ```sh
   uv run towncrier build --version 0.18.0 --date YYYY-MM-DD --yes
   uv run python scripts/changelog.py
   ```

   Towncrier prepends the release to `CHANGELOG.md` and consumes its fragments.
   Review and commit those deletions. Advance `next_version` before adding new
   fragments. Update and verify package versions separately; note generation
   does not tag, publish, deploy, or bump packages.

4. Use `scripts/changelog.py --version <version>` output as the GitHub release
   body. It is the same Markdown shown in the app. A historical published
   section is the canonical archive; correct it explicitly rather than
   re-generating it from today's commits.

Unconsumed fragments describe upcoming changes. They are a preview, not a claim
that a release has shipped.
