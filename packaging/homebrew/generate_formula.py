"""Generate a release-ready Cognis executor Homebrew formula."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

PLACEHOLDER_WORDS = ("placeholder", "example", "changeme", "your-", "fake", "dummy", "invalid")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_placeholder(value: str, *, field: str) -> None:
    lowered = value.lower()
    if not value or any(word in lowered for word in PLACEHOLDER_WORDS):
        raise ValueError(f"{field} must be a real, non-placeholder value")
    if field.endswith("sha256") and (not SHA256_RE.fullmatch(value) or len(set(value)) == 1):
        raise ValueError(f"{field} must be a real SHA-256 digest")


def validate_asset_url(
    url: str, *, version: str, architecture: str, development: bool = False
) -> str:
    _reject_placeholder(url, field="asset URL")
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        if not development:
            raise ValueError("file:// asset URLs require --development")
        if not parsed.path or not Path(unquote(parsed.path)).is_file():
            raise ValueError("file:// asset URL must name an existing asset")
        return url
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"github.com", "www.github.com"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public asset URLs must be immutable HTTPS URLs without query strings")
    expected_name = f"cognis-executor-{version}-macos-{architecture}.tar.gz"
    if (
        "/releases/download/" not in parsed.path
        or f"/v{version}/" not in parsed.path
        or not parsed.path.endswith(expected_name)
    ):
        raise ValueError("public asset URL must be an immutable versioned architecture asset URL")
    return url


def _ruby(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate_formula(
    *,
    version: str,
    arm64_url: str,
    arm64_sha256: str,
    x86_64_url: str,
    x86_64_sha256: str,
    head_url: str | None = None,
    development: bool = False,
) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must be a semantic version")
    validate_asset_url(arm64_url, version=version, architecture="arm64", development=development)
    validate_asset_url(x86_64_url, version=version, architecture="x86_64", development=development)
    _reject_placeholder(arm64_sha256, field="arm64_sha256")
    _reject_placeholder(x86_64_sha256, field="x86_64_sha256")
    if head_url is not None:
        _reject_placeholder(head_url, field="head URL")
        if urlsplit(head_url).scheme not in {"https", "ssh", "git"}:
            raise ValueError("head URL must use HTTPS, SSH, or Git")
    head = f'\n  head {_ruby(head_url)}, branch: "main"\n' if head_url else ""
    return f"""class CognisExecutor < Formula
  desc "Standalone remote executor for Cognis"
  homepage "https://github.com/fpytloun/cognis"
  version {_ruby(version)}{head}
  if Hardware::CPU.arm?
    url {_ruby(arm64_url)}
    sha256 {_ruby(arm64_sha256)}
  else
    url {_ruby(x86_64_url)}
    sha256 {_ruby(x86_64_sha256)}
  end

  depends_on "cairo"
  depends_on "gdk-pixbuf"
  depends_on "libffi"
  depends_on "node"
  depends_on "pango"
  depends_on "python@3.12"
  depends_on "uv"

  preserve_rpath

  def install
    if build.head?
      system formula_opt_bin("uv")/"uv", "pip", "install", "--prefix", libexec,
             "--python", formula_opt_bin("python@3.12")/"python3.12",
             "packages/common", "packages/executor[full]"
    else
      libexec.install Dir["payload/*"]
    end
    (bin/"cognis-executor").write <<~SH
      #!/bin/bash
      set -euo pipefail
      export PYTHONNOUSERSITE=1
      export PYTHONPATH="#{{libexec}}/lib/python3.12/site-packages"
      export PATH="#{{libexec}}/bin:#{{opt_bin}}:#{{formula_opt_bin("python@3.12")}}:#{{formula_opt_bin("uv")}}:#{{formula_opt_bin("node")}}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
      exec "#{{formula_opt_bin("python@3.12")}}/python3.12" -m cognis.executor "$@"
    SH
  end

  def caveats
    <<~EOS
      Configure this executor with:
        cognis-executor configure

      Manage the per-user launchd service with:
        brew services start cognis-executor
        brew services list
        brew services stop cognis-executor
        cognis-executor logs --follow

      Configuration and workspace data stay outside Homebrew's prefix.
    EOS
  end

  service do
    run opt_bin/"cognis-executor"
    keep_alive true
    working_dir Dir.home
    log_path var/"log/cognis-executor.log"
    error_log_path var/"log/cognis-executor.error.log"
    process_type :interactive
  end

  test do
    assert_match "Cognis executor", shell_output("#{{bin}}/cognis-executor --help")
  end
end
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arm64-url")
    parser.add_argument("--arm64-asset", type=Path)
    parser.add_argument("--arm64-sha256", required=True)
    parser.add_argument("--x86-64-url", "--x86_64-url", dest="x86_64_url")
    parser.add_argument("--x86-64-asset", "--x86_64-asset", dest="x86_64_asset", type=Path)
    parser.add_argument("--x86-64-sha256", "--x86_64-sha256", dest="x86_64_sha256", required=True)
    parser.add_argument("--head-url")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    asset_urls: dict[str, str | None] = {
        "arm64": args.arm64_url,
        "x86_64": args.x86_64_url,
    }
    for architecture, asset in (("arm64", args.arm64_asset), ("x86_64", args.x86_64_asset)):
        if asset is not None:
            if not args.development:
                raise SystemExit(f"{architecture} asset paths require --development")
            if not asset.is_file():
                raise SystemExit(f"{architecture} asset does not exist: {asset}")
            if asset_urls[architecture] is not None:
                raise SystemExit(f"provide either a {architecture} URL or asset path, not both")
            asset_urls[architecture] = asset.resolve().as_uri()
        if asset_urls[architecture] is None:
            raise SystemExit(f"provide a {architecture} asset URL or path")
    formula = generate_formula(
        version=args.version,
        arm64_url=asset_urls["arm64"] or "",
        arm64_sha256=args.arm64_sha256,
        x86_64_url=asset_urls["x86_64"] or "",
        x86_64_sha256=args.x86_64_sha256,
        head_url=args.head_url,
        development=args.development,
    )
    if args.output:
        args.output.write_text(formula, encoding="utf-8")
    else:
        print(formula, end="")


if __name__ == "__main__":
    main()
