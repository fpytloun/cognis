"""Source-bound feedback handled by the isolated deterministic mock runtime."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, NoReturn
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.config import load_config
from cognis.core.feedback.primitives import Actor, Payloads, Tokens, digest
from cognis.core.feedback.sources import Sources, utf16_length
from cognis.core.feedback.threads import Threads
from cognis.models import feedback as dto
from cognis.models.feedback_routes import PRIVATE_PREFIX
from cognis.store import queries


async def seed_feedback(
    factory: async_sessionmaker[AsyncSession],
    owner: str,
    deliverable_id: str,
    rich_authoring: dict[str, Any],
    *,
    owner_password: str,
) -> dict[str, dict[str, str]]:
    """Create an anchored comment and execute its revision through real admission."""
    async with factory() as db:
        providers = await queries.list_llm_providers(db)
    if len(providers) != 1 or providers[0].config.get("api_base") != "http://mock-llm:8090/v1":
        raise RuntimeError("Showcase execution requires the isolated mock-only provider")
    config = load_config()
    store = ArtifactStore(
        ArtifactStoreConfig(
            backend=config.artifact_backend,
            path=str(config.artifact_path),
            s3_endpoint=config.artifact_s3_endpoint,
            s3_access_key=config.artifact_s3_access_key,
            s3_secret_key=config.artifact_s3_secret_key,
            s3_bucket=config.artifact_s3_bucket,
            s3_region=config.artifact_s3_region,
            max_size_bytes=config.artifact_max_size_bytes,
        )
    )

    async def no_event_source(email: str, source: dto.FeedbackSourceRef) -> NoReturn:
        raise ValueError("This fixture only supports document sources")

    tokens = Tokens(uuid4().hex)
    sources = Sources(factory, store, tokens, no_event_source)
    payloads = Payloads(factory, store)
    threads = Threads(sources, payloads, tokens)
    actor = Actor.user(owner)
    context = dto.StandaloneFeedbackContext(kind="standalone")
    source_ref = dto.DocumentSourceRef(kind="document", content_ref=deliverable_id)
    evidence = await sources.load(owner, source_ref)
    assert evidence.rich_payload is not None
    assert evidence.producer_agent_id is not None
    blocks = evidence.rich_payload["blocks"]
    index = next(i for i, block in enumerate(blocks) if block.get("type") == "markdown")
    block_id = f"/blocks/{index}"
    scope = dto.RichFieldTextScope(
        kind="rich_field", block_id=block_id, identity="revision_local", field_path=["content"]
    )
    text = sources.rich_field_text(evidence.rich_payload, scope)
    quote = "Invite returning customers to early access first."
    start = text.index(quote)
    selection = dto.TextRange(
        representation="feedback-text-v1",
        text_digest=digest(text.encode()),
        start=utf16_length(text[:start]),
        end=utf16_length(text[: start + len(quote)]),
        exact=quote,
    )
    resolved = await sources.resolve(
        actor, dto.ResolveSourceRequest(source=source_ref, context=context)
    )
    comment = "Add Friday's early-access opening date to the invitation."
    thread = await threads.create(
        actor,
        dto.ThreadCreateRequest(
            creation_key=uuid4().hex,
            context=context,
            anchor=dto.AnchorInput(
                source_token=resolved.source_token,
                content_digest=resolved.content_digest,
                locator=dto.RichLocator(
                    kind="rich",
                    block_id=block_id,
                    identity="revision_local",
                    field_path=["content"],
                    range=selection,
                ),
            ),
            body=comment,
        ),
    )
    revised = copy.deepcopy(rich_authoring)
    revised["blocks"][-1]["content"] += (
        "\n\n**Early access opens Friday.** Explore the collection before the wider launch."
    )
    write = {"action": "rich", "payload": revised, "supersedes": deliverable_id}
    reply = "Added Friday's early-access opening date and kept the public launch separate."
    instruction = "Revise the early-access invitation and add Friday's opening date."
    calls = [
        ("search_tools", {"query": "comment_reply write_deliverable", "limit": 5}),
        ("validate_tool_call", {"tool": "builtin:write_deliverable", "arguments": write}),
        ("call_tool", {"tool": "builtin:write_deliverable", "arguments": write}),
        (
            "call_tool",
            {
                "tool": "builtin:comment_reply",
                "arguments": {"comment_id": thread.thread_id, "reply": reply},
            },
        ),
    ]
    scenario = {
        "id": "lumen-showcase-feedback",
        "trigger": instruction,
        "description": "Real local orchestration with deterministic mock responses; no real inference.",
        "turns": [
            {
                "role": "assistant",
                "steps": [
                    {
                        "type": "tool_call",
                        "call_id": f"call_{thread.thread_id}_{index}",
                        "name": name,
                        "arguments": arguments,
                    }
                ],
            }
            for index, (name, arguments) in enumerate(calls)
        ]
        + [
            {
                "role": "assistant",
                "steps": [
                    {
                        "type": "text",
                        "chunks": ["The revised launch plan makes Friday early access explicit."],
                    }
                ],
            }
        ],
    }
    async with httpx.AsyncClient(base_url="http://mock-llm:8090", timeout=30) as mock:
        (await mock.post("/__mock/scenario", json=scenario)).raise_for_status()
    async with httpx.AsyncClient(base_url="http://cognis:8080", timeout=30) as client:
        login = await client.post(
            "/api/auth/login", json={"email": owner, "password": owner_password, "mode": "native"}
        )
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        preflight_response = await client.post(
            f"{PRIVATE_PREFIX}/preflights",
            json=dto.FeedbackPreflightRequest(
                context=context,
                selection=[
                    dto.SelectedVersion(
                        entry_version_id=thread.entries[0].entry_version_id,
                        expected_entry_version=thread.entries[0].version,
                    )
                ],
                destination=dto.NewFollowupDestination(
                    kind="new_followup",
                    agent_id=evidence.producer_agent_id,
                    project_id=evidence.project_id,
                    title="Autumn invitation feedback",
                ),
                update_mode="revise_source",
                overall_message=instruction,
            ).model_dump(mode="json"),
        )
        preflight_response.raise_for_status()
        preflight = dto.FeedbackPreflight.model_validate(preflight_response.json())
        submitted = await client.post(
            f"{PRIVATE_PREFIX}/submissions",
            json=dto.SubmissionCreateRequest(
                creation_key=uuid4().hex,
                preflight_id=preflight.preflight_id,
                manifest_hash=preflight.manifest_hash,
            ).model_dump(mode="json"),
        )
        submitted.raise_for_status()
        submission_id = submitted.json()["submission_id"]
        async with asyncio.timeout(90):
            while True:
                detail = await client.get(f"{PRIVATE_PREFIX}/submissions/{submission_id}")
                detail.raise_for_status()
                receipt = detail.json()["receipt"]
                if receipt["work"] == "completed":
                    break
                if receipt["delivery"] == "rejected" or receipt["work"] in {
                    "failed",
                    "cancelled",
                    "unavailable",
                }:
                    raise RuntimeError(
                        f"Mock feedback execution did not complete: {receipt['work']}"
                    )
                await asyncio.sleep(0.2)
        results_response = await client.get(f"{PRIVATE_PREFIX}/submissions/{submission_id}/results")
        results_response.raise_for_status()
        [result] = results_response.json()["items"]
        assert result["relation"] == "revises_source"
        admission = detail.json()["receipt"]["admission"]
        assert admission["kind"] == "conversation_turn"
        feedback_conversation = admission["conversation_id"]
        answered = await client.get(f"{PRIVATE_PREFIX}/threads/{thread.thread_id}")
        answered.raise_for_status()
        assert any(entry["body"] == reply for entry in answered.json()["entries"])
    return {
        "resources": {
            "comment_text": comment,
            "comment_thread_id": thread.thread_id,
            "feedback_submission_id": submission_id,
            "feedback_conversation_id": feedback_conversation,
            "feedback_reply_text": reply,
            "feedback_reply_status": "executed_local_mock",
            "feedback_revised_deliverable_id": result["content_ref"],
            "feedback_provenance": "Actual local mock execution; no real model inference",
        },
        "routes": {
            "comments": f"/library/deliverable/{deliverable_id}",
            "feedback": f"/chat/{feedback_conversation}",
        },
    }
