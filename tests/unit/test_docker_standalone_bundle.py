"""Regression test for the standalone deliverable bundle shipping in Docker.

Root cause (fixed): the root `Dockerfile` only ran `npm run build` and only
copied `ui/build` into the runtime image. The interactive standalone bundle
(`ui/standalone-build`, built by `npm run build:standalone`) was never
produced or copied, so `resolve_standalone_manifest()` always returned
`None` in production containers and `/view` silently fell back to the
static (non-interactive) Python renderer for every deliverable.

A full Docker build is too slow for the unit test suite, so this test
statically asserts the Dockerfile's `ui-build` stage builds the standalone
bundle and the runtime stage copies it to the exact path
`cognis.ui_assets.resolve_standalone_build_dir()` expects
(`<repo_root>/ui/standalone-build`).
"""

from __future__ import annotations

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _dockerfile_text() -> str:
    assert _DOCKERFILE.is_file(), f"Dockerfile not found at {_DOCKERFILE}"
    return _DOCKERFILE.read_text(encoding="utf-8")


def test_ui_build_stage_builds_the_standalone_bundle() -> None:
    text = _dockerfile_text()
    assert "npm run build:standalone" in text, (
        "Dockerfile ui-build stage must run `npm run build:standalone` so the "
        "interactive deliverable bundle exists to copy into the runtime image."
    )


def test_runtime_stage_copies_the_standalone_bundle_to_the_expected_path() -> None:
    text = _dockerfile_text()
    assert "COPY --from=ui-build /app/ui/standalone-build ./ui/standalone-build" in text, (
        "Runtime stage must copy the standalone bundle to ./ui/standalone-build "
        "(WORKDIR /app), matching cognis.ui_assets.resolve_standalone_build_dir()."
    )


def test_standalone_build_ordering_is_after_the_regular_ui_build() -> None:
    text = _dockerfile_text()
    regular_build_index = text.index("npm run build\n")
    standalone_build_index = text.index("npm run build:standalone")
    assert regular_build_index < standalone_build_index


def test_dockerignore_does_not_block_standalone_build_output() -> None:
    """The standalone build output must not be excluded from the runtime
    stage's `COPY ui/ ./ui/` in a way that could shadow the later
    `COPY --from=ui-build` bundle copy (belt-and-suspenders hygiene: the
    stage copy always wins, but a stale host build should never leak in)."""

    dockerignore = _DOCKERFILE.parent / ".dockerignore"
    assert dockerignore.is_file()
    lines = {line.strip() for line in dockerignore.read_text(encoding="utf-8").splitlines()}
    assert "ui/standalone-build" in lines
