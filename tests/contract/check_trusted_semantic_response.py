"""Run with Mnemory's environment and repository on PYTHONPATH.

This local-only contract check uses the candidate's actual canonical journal.
"""

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from cognis.providers.memory.evidence import (  # noqa: E402
    TrustedEventRejectedError,
    derive_evidence_root,
)
from cognis.providers.memory.mnemory import MnemoryProvider  # noqa: E402
from tests.test_trusted_semantic import llm_responses, run, service  # noqa: E402


async def main():
    for size in (1000, 1001, 40000):
        svc = service()
        svc._config.memory.max_input_length = size - 1
        body = {
            "version": 1,
            "actor": {"user_id": "user-1", "owner_id": "owner-1"},
            "event": {
                "id": "local-contract:1",
                "event_hash": "a" * 64,
                "cognis_session_id": "local-contract",
                "conversation_id": "local-contract",
                "turn_id": "local-contract",
            },
            "messages": [{"role": "user", "content": "Ž" * size}],
        }

        class Response:
            def __init__(self, payload):
                self.payload = payload
                self.status_code = 422 if payload["status"] == "rejected" else 200

            def json(self):
                return {"detail": self.payload} if self.status_code == 422 else self.payload

            def raise_for_status(self):
                assert self.status_code == 200

        class Client:
            def __init__(self, svc):
                self.service = svc

            async def post(self, path, *, json, headers):
                result = run(
                    self.service,
                    derive_evidence_root(json),
                    json["messages"][0]["content"],
                    "evidence" if "/evidence/" in path else "ingest",
                )
                return Response(result)

        provider = MnemoryProvider("https://mnemory.test", object())
        provider.client = Client(svc)
        rejections = []
        for method in (provider.remember_evidence, provider.remember_user_event):
            try:
                await method(body, "local-contract-token")
            except TrustedEventRejectedError as exc:
                assert exc.source == body
                rejections.append(exc.rejection)
            else:
                raise AssertionError("Rejection incorrectly reported as success")
        assert rejections[0] == rejections[1]
        assert rejections[0].operation_id == svc.revisions.operations.evidence_operation_id(
            protocol="mnemory.trusted-semantic.v1",
            user_id=body["actor"]["user_id"],
            owner_id=body["actor"]["owner_id"],
            evidence_root_id=derive_evidence_root(body),
        )
        assert svc.vector._client.count("memories").count == 0
        print(f"{size}: actual canonical cross-route rejection retained without mutation")

    svc = service()
    provider = MnemoryProvider("https://mnemory.test", object())
    provider.client = Client(svc)
    for seq, text in enumerate(
        ("Qdrant je jediná databáze Mnemory.", "Mnemory používá výhradně databázi Qdrant."),
        1,
    ):
        body["event"]["id"] = f"local-contract:{seq}"
        body["event"]["event_hash"] = str(seq) * 64
        body["messages"][0]["content"] = text
        llm_responses(svc, text, "ADD" if seq == 1 else "CONFIRM")
        outcome = await provider.remember_evidence(body, "local-contract-token")
        assert outcome.status == "accepted"
        assert outcome.result["results"][0]["event"] == ("ADD" if seq == 1 else "CONFIRM")
        assert (await provider.remember_user_event(body, "local-contract-token"))[
            "status"
        ] == "replayed"
    points, _ = svc.vector._client.scroll("memories")
    assert len(points) == 1
    assert points[0].payload["validation_count"] == 1
    assert (await provider.remember_evidence(body, "local-contract-token")).status == "replayed"
    print("two independent paraphrases: ADD, CONFIRM once, paired replay")


if __name__ == "__main__":
    asyncio.run(main())
