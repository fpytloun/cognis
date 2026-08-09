"""Generate local-only credentials for the Docker Compose HA E2E stack."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / ".local" / "cognis-ha-e2e"
BUNDLE_DIR = OUTPUT_DIR / "bundles"
CURRENT_LINK = OUTPUT_DIR / "current"
KEY_DIR = CURRENT_LINK / "keys"
ENV_FILE = CURRENT_LINK / "compose.env"
EXECUTOR_TOKEN_DIR = CURRENT_LINK / "executors"


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    _atomic_write(
        path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def _write_public_key(path: Path, key: ec.EllipticCurvePublicKey) -> None:
    _atomic_write(
        path,
        key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def _bundle_is_active(staging_dir: Path) -> bool:
    if not CURRENT_LINK.is_symlink():
        return False
    return CURRENT_LINK.resolve(strict=False) == staging_dir.resolve(strict=False)


def prepare(*, force: bool) -> None:
    if ENV_FILE.exists() and not force:
        EXECUTOR_TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
        print(f"Reusing HA E2E credentials in {OUTPUT_DIR.relative_to(ROOT)}")
        return

    previous_umask = os.umask(0o077)
    staging_dir = BUNDLE_DIR / secrets.token_hex(16)
    temporary_link: Path | None = None
    try:
        BUNDLE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        OUTPUT_DIR.chmod(0o700)
        BUNDLE_DIR.chmod(0o700)
        staging_keys = staging_dir / "keys"
        staging_keys.mkdir(parents=True, mode=0o700)
        (staging_dir / "executors").mkdir(mode=0o700)
        private_key = ec.generate_private_key(ec.SECP256R1())
        _write_private_key(staging_keys / "private.pem", private_key)
        _write_public_key(staging_keys / "public.pem", private_key.public_key())
        _atomic_write(
            staging_keys / "secrets.key",
            base64.urlsafe_b64encode(secrets.token_bytes(32)),
        )

        values = {
            "COGNIS_HA_POSTGRES_PASSWORD": secrets.token_urlsafe(24),
            "COGNIS_HA_MINIO_ACCESS_KEY": f"cognis-{secrets.token_hex(8)}",
            "COGNIS_HA_MINIO_SECRET_KEY": secrets.token_urlsafe(32),
            "COGNIS_HA_ARTIFACT_SIGNING_SECRET": secrets.token_urlsafe(48),
        }
        _atomic_write(
            staging_dir / "compose.env",
            "".join(f"{name}={value}\n" for name, value in values.items()).encode(),
        )
        temporary_link = OUTPUT_DIR / f".current.{secrets.token_hex(8)}.tmp"
        os.symlink(staging_dir.relative_to(OUTPUT_DIR), temporary_link)
        os.replace(temporary_link, CURRENT_LINK)
    finally:
        os.umask(previous_umask)
        if temporary_link is not None:
            temporary_link.unlink(missing_ok=True)
        if not _bundle_is_active(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
    print(f"Generated HA E2E credentials in {OUTPUT_DIR.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace credentials; retained HA data must be removed at the same time",
    )
    args = parser.parse_args()
    prepare(force=args.force)


if __name__ == "__main__":
    main()
