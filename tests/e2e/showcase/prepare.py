"""Prepare a fresh, private showcase runtime without starting any services."""

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--attest", action="store_true")
    args = parser.parse_args()
    root = args.runtime.resolve()
    if args.attest:
        attest(root)
        return
    repo = Path(__file__).resolve().parents[3]
    if root.is_relative_to(repo) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)):
        raise ValueError("Runtime must be outside the checkout and use a shell-safe absolute path")
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    token_dir = root / "executor-token"
    token_dir.mkdir(mode=0o700)
    sockets = []
    ports = {}
    try:
        for key in (
            "QDRANT_PORT",
            "MNEMORY_PORT",
            "MNEMORY_MGMT_PORT",
            "INTARIS_PORT",
            "COGNIS_PORT",
            "MOCK_LLM_PORT",
        ):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports[key] = sock.getsockname()[1]
    finally:
        for sock in sockets:
            sock.close()
    project = "lumen-" + secrets.token_hex(6)
    owner_password = secrets.token_urlsafe(32)
    reviewer_password = secrets.token_urlsafe(32)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    values = {
        **ports,
        "SHOWCASE_TOKEN_DIR": token_dir,
        "SHOWCASE_RUNTIME_DIR": root,
        "COGNIS_IMAGE": f"{project}-controller:local",
        "COGNIS_EXECUTOR_IMAGE": f"{project}-executor:local",
        "QDRANT_IMAGE": "qdrant/qdrant@sha256:45f8e3ddc2570a4d029877e1b5ec1045c19b3852b4e22a55c7f43b05aea0ca89",
        "MNEMORY_IMAGE": "ghcr.io/fpytloun/mnemory@sha256:e7a9f524cfbaca52ce5b9b7bc6e82f2819b4d8d05b5dd870b3d9e538d57fb960",
        "INTARIS_IMAGE": "ghcr.io/fpytloun/intaris@sha256:f60ebda5080f8ddbdd2c2184fefe805ca4b4c23413fa0abc10b8575c5085c8f1",
        "COGNIS_LOCAL_ADMIN_EMAIL": "mara@lumen.example.com",
        "COGNIS_LOCAL_ADMIN_PASSWORD": owner_password,
        "COGNIS_PUBLIC_BASE_URL": f"http://127.0.0.1:{ports['COGNIS_PORT']}",
    }

    def private_write(path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)

    private_write(root / "compose.env", "".join(f"{k}={v}\n" for k, v in values.items()))
    private_write(
        root / "private.env",
        f"SHOWCASE_OWNER_PASSWORD={owner_password}\nSHOWCASE_REVIEWER_PASSWORD={reviewer_password}\n",
    )
    private_write(
        root / "runtime.json",
        json.dumps(
            {"project": project, "revision": revision, "base_url": values["COGNIS_PUBLIC_BASE_URL"]}
        )
        + "\n",
    )
    print(f"Prepared {root}; Compose project {project}")


def attest(root):
    """Bind the runtime record to the containers that Compose actually started."""
    path = root / "runtime.json"
    facts = json.loads(path.read_text())
    values = dict(line.split("=", 1) for line in (root / "compose.env").read_text().splitlines())
    # Early runtimes can predate SHOWCASE_RUNTIME_DIR; Compose receives it here.
    env = dict(os.environ, SHOWCASE_RUNTIME_DIR=str(root))
    repo = Path(__file__).resolve().parents[3]
    command = [
        "docker",
        "compose",
        "--project-name",
        facts["project"],
        "--env-file",
        str(root / "compose.env"),
        "-f",
        "compose.local.yml",
        "-f",
        "compose.e2e.yml",
        "-f",
        "tests/e2e/showcase/compose.yml",
    ]
    image_ids = {}
    images = {}
    for service, key in (
        ("cognis", "COGNIS_IMAGE"),
        ("qdrant", "QDRANT_IMAGE"),
        ("mnemory", "MNEMORY_IMAGE"),
        ("intaris", "INTARIS_IMAGE"),
        ("mock-llm", None),
    ):
        container = subprocess.check_output(
            [*command, "ps", "-q", service], cwd=repo, env=env, text=True
        ).strip()
        if not container:
            raise RuntimeError(f"Showcase service {service} is not running")
        image_id = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Image}}", container], text=True
        ).strip()
        image_ids[service] = image_id
        if key and service != "cognis":
            digests = json.loads(
                subprocess.check_output(
                    ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id],
                    text=True,
                )
            )
            if values[key] not in digests:
                raise RuntimeError(f"Running {service} image does not match its pinned digest")
            images[service] = values[key]
    facts.update(
        image_id=image_ids["cognis"],
        image_ids=image_ids,
        ports={
            key: int(values[key])
            for key in (
                "QDRANT_PORT",
                "MNEMORY_PORT",
                "MNEMORY_MGMT_PORT",
                "INTARIS_PORT",
                "COGNIS_PORT",
                "MOCK_LLM_PORT",
            )
        },
        companion_images=images,
    )
    temporary = root / "runtime.json.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(facts, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
    print("Recorded running controller and companion image identities.")


if __name__ == "__main__":
    main()
