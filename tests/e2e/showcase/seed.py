"""Create the first, explicitly illustrative Lumen project and Intaris stream."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from sqlalchemy import select

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.config import load_config
from cognis.core.feedback.primitives import Payloads
from cognis.models.agent import AgentCapabilities
from cognis.models.session import SessionEvent
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.guardrails.intaris import IntarisProvider
from cognis.runtime_context import current_agent_id, current_agent_owner_email, current_user_email
from cognis.security import create_password_hasher
from cognis.store.database import create_engine, create_session_factory
from cognis.store.feedback_models import (
    FeedbackAnchorRow,
    FeedbackEntryRow,
    FeedbackEntryVersionRow,
    FeedbackSubjectRow,
    FeedbackSubmissionRow,
    FeedbackThreadRow,
)
from cognis.store.models import ArtifactRecordRow, DeliverableRow, Session
from cognis.store.project_queries import get_project_agent, upsert_project_agent
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_project,
    create_project_grant,
    create_session,
    create_task,
    create_task_comment,
    create_user,
    get_active_project_grant,
    get_agent,
    get_conversation,
    get_project,
    get_schedule,
    get_task,
    get_user,
    list_task_comments,
    update_conversation_active_session,
)
from cognis.store.usage.models import UsageAttemptRow
from cognis.ui_assets import resolve_ui_build_dir

OWNER = "mara@lumen.example.com"
REVIEWER = "eli@lumen.example.com"
AGENT = "atlas"
PROJECT = "proj_lumen_autumn"
CONVERSATION = "conv_lumen_autumn"
SESSION = "sess_lumen_autumn"
TASK = "task_lumen_autumn"
FIXTURE_VERSION = 4
PROVENANCE = "Illustrative deterministic fixtures; no live inference"
RUNTIME = Path("/showcase-runtime")
FIXTURE_DIR = Path("/showcase")


def fixture_digest():
    digest = hashlib.sha256()
    for name in (
        "seed.py",
        "content.py",
        "feedback.py",
        "usage.py",
        "workspace.py",
        "task_outcomes.py",
        "autumn-planner.html",
        "avatars/atlas.webp",
        "avatars/forge.webp",
        "avatars/iris.webp",
        "avatars/vela.webp",
    ):
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256((FIXTURE_DIR / name).read_bytes()).digest())
    return digest.hexdigest()


async def materialize_work(password, conversation_id=CONVERSATION):
    async with httpx.AsyncClient(base_url="http://cognis:8080", timeout=20) as client:
        login = await client.post(
            "/api/auth/login",
            json={"email": OWNER, "password": password, "mode": "native"},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        refresh = await client.post(
            "/api/v1/work/refresh",
            headers=headers,
            json={
                "scope": {
                    "kind": "conversation",
                    "key": f"conversation:{conversation_id}",
                    "conversation_id": conversation_id,
                }
            },
        )
        refresh.raise_for_status()
        for _ in range(120):
            response = await client.get(
                f"/api/v1/chat/v2/conversations/{conversation_id}/work",
                params={"category": "files"},
                headers=headers,
            )
            response.raise_for_status()
            if any(
                item.get("path", "").endswith("launch/pricing.py")
                and item.get("path_generation_id")
                for mutation in response.json().get("mutations", [])
                for item in mutation.get("file_stats", [])
            ):
                return
            await asyncio.sleep(0.5)
    raise RuntimeError("Timed out waiting for illustrative Work projection")


async def publish_manifest(factory, provider, completed=None):
    # Create fixture content only after the core metadata has committed.
    from content import seed_content
    from feedback import seed_feedback
    from usage import seed_usage
    from workspace import seed_workspace

    runtime = json.loads((RUNTIME / "runtime.json").read_text())
    if not runtime.get("image_id"):
        raise RuntimeError("Runtime image identity missing; run prepare.py --attest first")
    private = dict(
        line.split("=", 1) for line in (RUNTIME / "private.env").read_text().splitlines()
    )
    if completed is None:
        content = await seed_content(
            factory, provider, OWNER, REVIEWER, AGENT, PROJECT, CONVERSATION, SESSION, FIXTURE_DIR
        )
        feedback = await seed_feedback(
            factory,
            OWNER,
            content["resources"]["deliverable_id"],
            content["rich_authoring"],
            owner_password=private["SHOWCASE_OWNER_PASSWORD"],
        )
        workspace = await seed_workspace(
            factory,
            provider,
            owner=OWNER,
            password=private["SHOWCASE_OWNER_PASSWORD"],
            autumn_project=PROJECT,
            fixture_dir=FIXTURE_DIR,
        )
        if os.environ.get("SHOWCASE_TASK_OUTCOMES") == "1":
            from task_outcomes import seed_task_outcomes

            workspace["task_outcomes"] = await seed_task_outcomes(factory, provider, OWNER, PROJECT)
        work_items = [
            {
                "agent_id": AGENT,
                "project_id": PROJECT,
                "conversation_id": CONVERSATION,
                "session_id": SESSION,
                "weight": 5,
                "tokens": 9000,
            },
            *(
                {**item, "weight": weight, "tokens": tokens}
                for key, weight, tokens in (
                    ("weekly-plan", 3, 6000),
                    ("staging-health", 6, 14000),
                    ("partner-newsletter", 4, 7000),
                    ("saved-carts", 9, 22000),
                    ("six-week-review", 2, 12000),
                    ("executor-capacity", 2, 8000),
                )
                for item in (workspace["conversations"][key],)
            ),
            *(
                {**item, "weight": weight, "tokens": tokens, "first_day": 18}
                for key, weight, tokens in (
                    ("readiness-brief", 3, 16000),
                    ("upgrade-rehearsal", 7, 26000),
                    ("rounding-fix", 5, 30000),
                    ("status-report", 2, 9000),
                )
                for item in (workspace["tasks"][key],)
            ),
        ]
        usage = await seed_usage(factory, OWNER, work_items)
        content = {
            "resources": {**content["resources"], **feedback["resources"]},
            "routes": {**content["routes"], **feedback["routes"], "usage": "/usage"},
            "usage": {
                "start": usage["start"],
                "end": usage["end"],
                "expected_requests": usage["attempts"],
            },
            "workspace": workspace,
        }
        content["routes"]["rich"] = (
            f"/library/deliverable/{content['resources']['feedback_revised_deliverable_id']}"
        )
    else:
        content = completed
    if set(content) != {"resources", "routes", "usage", "workspace"}:
        raise RuntimeError("Invalid showcase content contract")
    if not {"rich_title", "comment_text"} <= content["resources"].keys():
        raise RuntimeError("Showcase content resources are incomplete")
    if any(not isinstance(value, str) or not value for value in content["resources"].values()):
        raise RuntimeError("Showcase resource values must be nonempty strings")
    if set(content["usage"]) != {"start", "end", "expected_requests"} or any(
        not isinstance(content["usage"][key], str) or not content["usage"][key]
        for key in ("start", "end")
    ):
        raise RuntimeError("Showcase usage interval is incomplete")
    routes = {
        "setup": "/getting-started",
        "chat": f"/chat/{CONVERSATION}",
        "work": f"/chat/{CONVERSATION}",
        "tasks": f"/tasks/{TASK}",
        "project": f"/projects/{PROJECT}",
        **content["routes"],
    }
    if set(routes) != {
        "setup",
        "chat",
        "work",
        "tasks",
        "project",
        "rich",
        "html",
        "usage",
        "comments",
        "feedback",
    }:
        raise RuntimeError("Showcase routes are incomplete")
    if any(
        not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
        for path in routes.values()
    ):
        raise RuntimeError("Showcase routes must be same-origin absolute paths")
    ui = resolve_ui_build_dir()
    if ui is None:
        raise RuntimeError("The running image has no built frontend")
    digest = hashlib.sha256()
    for path in sorted(p for p in ui.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(ui)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    manifest = {
        "schema_version": 1,
        "base_url": runtime["base_url"],
        "source_revision": runtime["revision"],
        "fixture_version": FIXTURE_VERSION,
        "provenance": PROVENANCE,
        "project_id": PROJECT,
        "routes": {key: urljoin(runtime["base_url"], path) for key, path in routes.items()},
        "resources": {
            "project_id": PROJECT,
            "agent_id": AGENT,
            "conversation_id": CONVERSATION,
            "session_id": SESSION,
            "task_id": TASK,
            "work_file": "pricing.py",
            **content["resources"],
        },
        "usage": content["usage"],
        "workspace": content["workspace"],
        "fixture_input_sha256": fixture_digest(),
        "feature_coverage": {"feedback_agent_reply": True},
        "users": {"owner_email": OWNER, "reviewer_email": REVIEWER},
        "runtime": {
            "image_id": runtime["image_id"],
            "image_ids": runtime["image_ids"],
            "frontend_sha256": digest.hexdigest(),
            "ports": runtime["ports"],
            "companion_images": runtime["companion_images"],
        },
    }
    target = RUNTIME / "manifest.json"
    temporary = RUNTIME / "manifest.json.tmp"
    serialized = json.dumps(manifest, indent=2) + "\n"
    secrets = private
    if any(value in serialized for value in secrets.values() if value):
        raise RuntimeError("Refusing to publish a manifest containing private credentials")
    if completed is None:
        async with factory.begin() as db:
            project = await get_project(db, PROJECT)
            if project is None or project.metadata_json != {
                "showcase_fixture_version": FIXTURE_VERSION,
                "fixture_input_sha256": fixture_digest(),
                "state": "seeding",
            }:
                raise RuntimeError("Showcase completion marker conflicts")
            project.metadata_json = {
                **project.metadata_json,
                "state": "complete",
                "content": content,
            }
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(serialized)
    directory_owner = RUNTIME.stat()
    os.chown(temporary, directory_owner.st_uid, directory_owner.st_gid)
    os.replace(temporary, target)
    print("Validated showcase and published safe manifest.")


async def main():
    private = dict(
        line.split("=", 1) for line in (RUNTIME / "private.env").read_text().splitlines()
    )
    config = load_config()
    engine = create_engine(config.database_url)
    factory = create_session_factory(engine)
    provider = IntarisProvider(
        config.intaris_url,
        JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path),
    )
    try:
        if sys.argv[1:]:
            raise RuntimeError("Unsupported showcase seed arguments")
        async with factory() as db:
            owner = await get_user(db, OWNER)
            if (
                owner is None
                or not owner.is_active
                or owner.role != "admin"
                or not create_password_hasher().verify(
                    owner.password_hash, private["SHOWCASE_OWNER_PASSWORD"]
                )
            ):
                raise RuntimeError("Showcase owner is missing or credentials conflict")
            existing = [
                await get_user(db, REVIEWER),
                await get_agent(db, AGENT),
                await get_project(db, PROJECT),
                await get_conversation(db, CONVERSATION),
                await get_task(db, TASK),
            ]
            if any(row is not None for row in existing):
                if not all(row is not None for row in existing):
                    raise RuntimeError("Partial showcase core; refusing to recover automatically")
                reviewer, agent, project, conversation, task = existing
                grant = await get_active_project_grant(db, PROJECT, REVIEWER)
                assignment = await get_project_agent(db, PROJECT, AGENT)
                session_row = await db.get(Session, SESSION)
                comments = await list_task_comments(db, TASK)
                valid = (
                    reviewer.is_active
                    and reviewer.role == "user"
                    and create_password_hasher().verify(
                        reviewer.password_hash, private["SHOWCASE_REVIEWER_PASSWORD"]
                    )
                    and agent.owner_email == OWNER
                    and agent.status == "active"
                    and project.owner_email == OWNER
                    and project.status == "active"
                    and project.metadata_json.get("showcase_fixture_version") == FIXTURE_VERSION
                    and project.metadata_json.get("fixture_input_sha256") == fixture_digest()
                    and project.metadata_json.get("state") == "complete"
                    and conversation.user_email == OWNER
                    and conversation.agent_id == AGENT
                    and conversation.project_id == PROJECT
                    and conversation.active_session_id == SESSION
                    and session_row is not None
                    and session_row.conversation_id == CONVERSATION
                    and session_row.intaris_session_id == SESSION
                    and session_row.user_email == OWNER
                    and session_row.agent_id == AGENT
                    and task.created_by == OWNER
                    and task.agent_id == AGENT
                    and task.project_id == PROJECT
                    and grant is not None
                    and grant.permission == "member"
                    and grant.granted_by == OWNER
                    and assignment is not None
                    and assignment.added_by == OWNER
                    # workspace.py makes Atlas the project's default agent once the core exists.
                    and assignment.is_default
                    and assignment.executor_scope == "owner_executor"
                    and any(
                        comment.author_email == REVIEWER
                        and comment.body == "Please make the early-access invitation clearer."
                        for comment in comments
                    )
                )
                if not valid:
                    raise RuntimeError("Showcase core conflicts with fixture version or ownership")
                completed = project.metadata_json.get("content")
                if not isinstance(completed, dict):
                    raise RuntimeError("Showcase completion references are missing")
                refs = completed.get("resources", {})
                original = await db.get(DeliverableRow, refs.get("original_deliverable_id"))
                revised = await db.get(DeliverableRow, refs.get("deliverable_id"))
                feedback_result = await db.get(
                    DeliverableRow, refs.get("feedback_revised_deliverable_id")
                )
                html = await db.get(ArtifactRecordRow, refs.get("html_artifact_id"))
                figure = await db.get(ArtifactRecordRow, refs.get("figure_artifact_id"))
                thread = await db.get(FeedbackThreadRow, refs.get("comment_thread_id"))
                if not all((original, revised, feedback_result, html, figure, thread)):
                    raise RuntimeError("Completed showcase resources are missing")
                if not all(
                    row.owner_email == OWNER
                    and row.owner_agent_id == AGENT
                    and row.conversation_id == CONVERSATION
                    and row.session_id == SESSION
                    and row.project_id == PROJECT
                    and row.status == "buffered"
                    and row.format == "rich"
                    and row.content_mime == "text/markdown"
                    and row.rich_key
                    for row in (original, revised)
                ) or not (
                    original.revision == 1
                    and original.lineage_id == original.deliverable_id
                    and original.supersedes_deliverable_id is None
                    and revised.revision == 2
                    and revised.lineage_id == original.deliverable_id
                    and revised.supersedes_deliverable_id == original.deliverable_id
                ):
                    raise RuntimeError(
                        "Completed showcase Rich ownership, status or lineage conflicts"
                    )
                if not (
                    feedback_result.owner_email == OWNER
                    and feedback_result.owner_agent_id == AGENT
                    and feedback_result.project_id == PROJECT
                    and feedback_result.format == "rich"
                    and feedback_result.revision == 3
                    and feedback_result.lineage_id == original.deliverable_id
                    and feedback_result.supersedes_deliverable_id == revised.deliverable_id
                ):
                    raise RuntimeError("Completed showcase mock revision conflicts")
                for row, mime in ((html, "text/html"), (figure, "image/png")):
                    if not (
                        row.owner_email == OWNER
                        and row.owner_agent_id == AGENT
                        and row.conversation_id == CONVERSATION
                        and row.session_id == SESSION
                        and row.project_id == PROJECT
                        and row.status == "attached"
                        and row.deleted_at is None
                        and row.mime_type == mime
                    ):
                        raise RuntimeError(
                            "Completed showcase artifact ownership, status or MIME conflicts"
                        )
                # Published deliverables also have independently soft-deletable library records.
                for row in (original, revised, feedback_result):
                    published = await db.get(ArtifactRecordRow, row.deliverable_id)
                    if not (
                        published is not None
                        and published.owner_email == OWNER
                        and published.owner_agent_id == AGENT
                        and published.status == "attached"
                        and published.deleted_at is None
                        and published.object_id == row.storage_object_id
                        and published.namespace == row.storage_namespace
                    ):
                        raise RuntimeError("Completed showcase published document is unavailable")
                subject = await db.get(FeedbackSubjectRow, thread.subject_id)
                anchor = await db.get(FeedbackAnchorRow, thread.anchor_id)
                entries = list(
                    await db.scalars(
                        select(FeedbackEntryRow)
                        .where(
                            FeedbackEntryRow.thread_id == thread.thread_id,
                            FeedbackEntryRow.author_id == OWNER,
                            FeedbackEntryRow.reply_to_entry_id.is_(None),
                        )
                        .order_by(FeedbackEntryRow.created_at, FeedbackEntryRow.entry_id)
                    )
                )
                if not (
                    thread.author_id == OWNER
                    and thread.state == "open"
                    and not thread.hidden
                    and subject is not None
                    and subject.owner_email == OWNER
                    and subject.project_id == PROJECT
                    and subject.source
                    == {"kind": "document", "content_ref": revised.deliverable_id}
                    and subject.lineage_id == original.deliverable_id
                    and anchor is not None
                    and anchor.subject_id == subject.subject_id
                    and anchor.content_digest == subject.content_digest
                    and anchor.created_by == OWNER
                    and entries
                    and entries[0].author_id == OWNER
                ):
                    raise RuntimeError("Completed showcase feedback source or owner conflicts")
                entry_version = await db.get(FeedbackEntryVersionRow, entries[0].head_version_id)
                if entry_version is None or entry_version.entry_id != entries[0].entry_id:
                    raise RuntimeError("Completed showcase feedback entry is unavailable")
                submission = await db.get(FeedbackSubmissionRow, refs.get("feedback_submission_id"))
                answer = (
                    await db.execute(
                        select(FeedbackEntryRow).where(
                            FeedbackEntryRow.thread_id == thread.thread_id,
                            FeedbackEntryRow.author_id == f"agent:{AGENT}",
                        )
                    )
                ).scalar_one_or_none()
                if not (
                    submission is not None
                    and submission.sender_email == OWNER
                    and submission.destination_agent_id == AGENT
                    and submission.destination_task_id is None
                    and submission.destination_conversation_id
                    == refs.get("feedback_conversation_id")
                    == feedback_result.conversation_id
                    and submission.delivery == "accepted"
                    and submission.request_id
                    and answer is not None
                ):
                    raise RuntimeError("Completed showcase admitted feedback is unavailable")
                answer_version = await db.get(FeedbackEntryVersionRow, answer.head_version_id)
                if answer_version is None:
                    raise RuntimeError("Completed showcase agent reply is unavailable")
                from usage import ATTEMPT_PREFIX

                attempts = list(
                    await db.scalars(
                        select(UsageAttemptRow.attempt_id).where(
                            UsageAttemptRow.attempt_id.startswith(ATTEMPT_PREFIX),
                            UsageAttemptRow.principal_subject == OWNER,
                            UsageAttemptRow.outcome == "success",
                            UsageAttemptRow.finalized_at.is_not(None),
                        )
                    )
                )
                if str(len(set(attempts))) != completed.get("usage", {}).get("expected_requests"):
                    raise RuntimeError("Completed showcase usage facts are incomplete")
                from workspace import CREW, SCHEDULES, TASKS, conversations

                workspace = completed.get("workspace") or {}
                projects = workspace.get("projects") or {}
                seeded = workspace.get("conversations") or {}
                tasks = workspace.get("tasks") or {}
                schedules = workspace.get("schedules") or {}
                expected = (
                    {spec["key"] for spec in conversations(projects, PROJECT)}
                    if projects
                    else set()
                )
                problems = []
                if set(projects) != {"autumn", "platform", "storefront"} or set(seeded) != expected:
                    problems.append("workspace references")
                if set(tasks) != {spec["key"] for spec in TASKS}:
                    problems.append("task references")
                if set(schedules) != {spec["name"] for spec in SCHEDULES}:
                    problems.append("schedule references")
                for spec in CREW:
                    agent = await get_agent(db, spec["agent_id"])
                    if agent is None or agent.owner_email != OWNER or agent.status != "active":
                        problems.append(f"agent {spec['agent_id']}")
                for key, project_id in projects.items():
                    if await get_project(db, project_id) is None:
                        problems.append(f"project {key}")
                for key, item in seeded.items():
                    conversation = await get_conversation(db, item["conversation_id"])
                    if (
                        conversation is None
                        or conversation.user_email != OWNER
                        or conversation.agent_id != item["agent_id"]
                        or conversation.project_id != item["project_id"]
                        or conversation.active_session_id != item["session_id"]
                    ):
                        problems.append(f"conversation {key}")
                for key, item in tasks.items():
                    task = await get_task(db, item["task_id"])
                    if task is None or task.status != "completed" or task.created_by != OWNER:
                        problems.append(f"task {key}")
                for name, schedule_id in schedules.items():
                    schedule = await get_schedule(db, schedule_id)
                    if schedule is None or schedule.name != name:
                        problems.append(f"schedule {name}")
                if problems:
                    raise RuntimeError(
                        "Completed showcase workspace requires reconciliation: "
                        + ", ".join(problems)
                    )
                await db.close()
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
                figure_hashes = []
                for row in (original, revised, feedback_result):
                    for key, mime, size, expected_hash in (
                        (row.content_key, row.content_mime, row.content_size, row.content_hash),
                        (row.rich_key, "application/json", row.rich_size, row.rich_hash),
                    ):
                        # The local store does not retain MIME types; size and digest identify content.
                        data, _ = await store.async_load(
                            row.storage_namespace, row.storage_object_id, key
                        )
                        if (
                            not mime
                            or len(data) != size
                            or hashlib.sha256(data).hexdigest() != expected_hash
                        ):
                            raise RuntimeError("Completed showcase document payload conflicts")
                        if key == row.rich_key:
                            rich = json.loads(data)
                            media = [
                                rich.get("media_manifest", {}).get(
                                    block.get("media", {}).get("key"), {}
                                )
                                for block in rich["blocks"]
                                if block.get("type") == "figure"
                            ]
                            matches = [
                                item
                                for item in media
                                if item.get("artifact_ref") == figure.artifact_id
                            ]
                            if len(matches) != 1 or not matches[0].get("sha256"):
                                raise RuntimeError(
                                    "Completed showcase Rich figure reference conflicts"
                                )
                            figure_hashes.append(matches[0]["sha256"])
                for row in (html, figure):
                    data, _ = await store.async_load(row.namespace, row.object_id, row.filename)
                    if not row.mime_type or len(data) != row.size_bytes or not data:
                        raise RuntimeError("Completed showcase artifact payload conflicts")
                    if row.artifact_id == figure.artifact_id and any(
                        expected != hashlib.sha256(data).hexdigest() for expected in figure_hashes
                    ):
                        raise RuntimeError("Completed showcase figure digest conflicts")
                    if (
                        row.artifact_id == html.artifact_id
                        and data != (FIXTURE_DIR / "autumn-planner.html").read_bytes()
                    ):
                        raise RuntimeError("Completed showcase HTML content conflicts")
                payloads = Payloads(factory, store)
                await payloads.get(anchor.payload_id)
                body = await payloads.get(entry_version.payload_id)
                answer_body = await payloads.get(answer_version.payload_id)
                if body.get("body") != refs.get("comment_text") or answer_body.get(
                    "body"
                ) != refs.get("feedback_reply_text"):
                    raise RuntimeError("Completed showcase feedback payload conflicts")
                token = current_user_email.set(OWNER)
                agent_token = current_agent_id.set(AGENT)
                owner_token = current_agent_owner_email.set(OWNER)
                try:
                    events = await provider.read_events(SESSION)
                finally:
                    current_agent_owner_email.reset(owner_token)
                    current_agent_id.reset(agent_token)
                    current_user_email.reset(token)
                work = {
                    (event.get("type"), event.get("data", {}).get("call_id")): event.get("data", {})
                    for event in events.events
                    if event.get("data", {}).get("call_id") == "call_lumen_patch"
                }
                patch_call = work.get(("tool_call", "call_lumen_patch"), {})
                patch_result = work.get(("tool_result", "call_lumen_patch"), {})
                expected_diffs = [
                    {
                        "path": "launch/pricing.py",
                        "diff": "@@ -1,2 +1,4 @@\n def launch_price(cents, early_access):\n-    return cents\n+    if early_access:\n+        return cents * 9 // 10\n+    return cents\n",
                    },
                    {
                        "path": "tests/test_pricing.py",
                        "diff": "@@ -0,0 +1,5 @@\n+from launch.pricing import launch_price\n+\n+def test_early_access_price():\n+    assert launch_price(12000, True) == 10800\n+    assert launch_price(12000, False) == 12000\n",
                    },
                ]
                if not (
                    patch_call.get("name") == "apply_patch"
                    and patch_call.get("status") == "started"
                    and patch_call.get("arguments") == {"file_path": "launch/pricing.py"}
                    and patch_result.get("name") == "apply_patch"
                    and patch_result.get("status") == "complete"
                    and patch_result.get("result")
                    == "Patched launch/pricing.py and tests/test_pricing.py."
                    and patch_result.get("file_diffs") == expected_diffs
                    and all(
                        item.get("turn_id") == "turn_lumen_autumn"
                        for item in (patch_call, patch_result)
                    )
                ):
                    raise RuntimeError("Showcase Intaris history is incomplete")
                print("Validated existing showcase core.")
                await materialize_work(private["SHOWCASE_OWNER_PASSWORD"])
                await publish_manifest(factory, provider, completed)
                return
            await create_user(
                db,
                REVIEWER,
                "Eli Rowan",
                create_password_hasher().hash(private["SHOWCASE_REVIEWER_PASSWORD"]),
            )
            await create_agent(
                db,
                agent_id=AGENT,
                owner_email=OWNER,
                name="Atlas",
                display_name="Atlas",
                description="Chief of staff. Turns intent into plans, coordinates the team, and keeps every workstream moving.",
                capabilities=AgentCapabilities(
                    memory_backend="none", guardrails_backend="none"
                ).model_dump(),
                llm_config={"provider_id": "local-openai-compatible", "model": "mock-model"},
                execution={"executor_id": "local-compose-executor"},
                status="active",
            )
            await create_project(
                db,
                project_id=PROJECT,
                owner_email=OWNER,
                name="Lumen Autumn",
                description="Autumn collection launch: invitation, pricing and rollout.",
                metadata={
                    "showcase_fixture_version": FIXTURE_VERSION,
                    "fixture_input_sha256": fixture_digest(),
                    "state": "seeding",
                },
            )
            await upsert_project_agent(
                db, project_id=PROJECT, agent_id=AGENT, added_by=OWNER, is_default=False
            )
            await create_project_grant(
                db,
                project_id=PROJECT,
                granted_by=OWNER,
                grantee_user_email=REVIEWER,
                permission="member",
            )
            await create_conversation(
                db,
                OWNER,
                AGENT,
                "web",
                conversation_id=CONVERSATION,
                project_id=PROJECT,
                title="Autumn launch studio",
                title_source="manual",
                context_data={
                    "provenance": "Illustrative deterministic fixtures; no live inference"
                },
            )
            await create_session(
                db,
                CONVERSATION,
                OWNER,
                AGENT,
                session_id=SESSION,
                intaris_session_id=SESSION,
                mnemory_session_id=SESSION,
                status="idle",
            )
            await update_conversation_active_session(db, CONVERSATION, SESSION)
            await create_task(
                db,
                created_by=OWNER,
                agent_id=AGENT,
                task_id=TASK,
                project_id=PROJECT,
                title="Prepare the Autumn launch",
                description="Plan and ship the Autumn collection launch.",
                status="draft",
            )
            await create_task_comment(
                db,
                task_id=TASK,
                author_email=REVIEWER,
                body="Please make the early-access invitation clearer.",
            )
            await db.commit()
            await db.close()
            await provider.create_session(
                SESSION, "Illustrative seeded launch history.", AGENT, user_id=OWNER
            )
            events = [
                SessionEvent(
                    type="user_message",
                    data={
                        "role": "user",
                        "content": "Plan the Autumn launch. Early-access members should get a clear invitation and 10% off.",
                        "content_type": "text",
                        "turn_id": "turn_lumen_autumn",
                    },
                ),
                SessionEvent(
                    type="tool_call",
                    data={
                        "call_id": "call_lumen_patch",
                        "name": "apply_patch",
                        "turn_id": "turn_lumen_autumn",
                        "status": "started",
                        "arguments": {"file_path": "launch/pricing.py"},
                    },
                ),
                SessionEvent(
                    type="tool_result",
                    data={
                        "call_id": "call_lumen_patch",
                        "name": "apply_patch",
                        "turn_id": "turn_lumen_autumn",
                        "status": "complete",
                        "result": "Patched launch/pricing.py and tests/test_pricing.py.",
                        "file_diffs": [
                            {
                                "path": "launch/pricing.py",
                                "diff": "@@ -1,2 +1,4 @@\n def launch_price(cents, early_access):\n-    return cents\n+    if early_access:\n+        return cents * 9 // 10\n+    return cents\n",
                            },
                            {
                                "path": "tests/test_pricing.py",
                                "diff": "@@ -0,0 +1,5 @@\n+from launch.pricing import launch_price\n+\n+def test_early_access_price():\n+    assert launch_price(12000, True) == 10800\n+    assert launch_price(12000, False) == 12000\n",
                            },
                        ],
                        "duration_ms": 42,
                    },
                ),
                SessionEvent(
                    type="assistant_message",
                    data={
                        "role": "assistant",
                        "content": "Early-access members now get 10% off at launch, and the invitation says so in the first line. I updated the launch price and added a test for both cases. The launch overview is in the Library.",
                        "content_type": "text",
                        "turn_id": "turn_lumen_autumn",
                        "message_id": "msg_lumen_autumn",
                        "assistant_phase_index": 0,
                    },
                ),
            ]
            recorded = await provider.record_events(
                SESSION,
                events,
                source="showcase_seed",
                idempotency_key=f"{SESSION}:lumen-autumn-seed-v1",
                user_email=OWNER,
                agent_id=AGENT,
            )
            if not recorded.ok or recorded.count != len(events):
                raise RuntimeError("Intaris did not persist the complete showcase event batch")
            print("Seeded illustrative Lumen project, task, membership and conversation.")
        await materialize_work(private["SHOWCASE_OWNER_PASSWORD"])
        await publish_manifest(factory, provider)
    finally:
        await provider.client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
