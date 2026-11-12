"""Populate the fictional Lumen workspace around the core showcase project.

Adds the agent crew with their approved portraits, two more projects, seeded
conversation history, schedules, and tasks that complete through the real
task lifecycle against the isolated local mock model.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from cognis.models.session import SessionEvent
from cognis.store.models import Agent, Conversation
from cognis.store.queries import (
    create_conversation,
    create_session,
    update_conversation_active_session,
)

MOCK = "http://mock-llm:8090"
API = "http://cognis:8080"
MODEL = {"provider_id": "local-openai-compatible", "model": "mock-model"}
EXECUTION = {"executor_id": "local-compose-executor"}
CAPABILITIES = {"memory_backend": "none", "guardrails_backend": "none"}
LOCAL_AGENT = "local-implement-test-agent"
LOCAL_CONVERSATION = "conv_local_compose_welcome"

CREW = (
    {
        "agent_id": "atlas",
        "name": "Atlas",
        "description": "Chief of staff. Turns intent into plans, coordinates the team, and keeps every workstream moving.",
        "purpose": "coordinate work across agents and keep priorities clear",
        "tone": "calm, direct, structured",
    },
    {
        "agent_id": "forge",
        "name": "Forge",
        "description": "Software engineer. Implements features, fixes bugs, writes tests, and ships reviewable diffs.",
        "purpose": "deliver reliable software changes with tests",
        "tone": "precise, technical, concise",
    },
    {
        "agent_id": "iris",
        "name": "Iris",
        "description": "Editor and writer. Turns noisy material into clear briefs, reports, and polished documents.",
        "purpose": "write clear, well-structured documents",
        "tone": "warm, readable, exact",
    },
    {
        "agent_id": "vela",
        "name": "Vela",
        "description": "Operations engineer. Watches systems, diagnoses incidents, and keeps infrastructure healthy.",
        "purpose": "keep services healthy and explain what changed",
        "tone": "clear, evidence-first, calm",
    },
)

PROJECTS = (
    {
        "key": "platform",
        "name": "Lumen Platform",
        "description": "Executors, staging and production health.",
        "agents": ("vela", "forge", "atlas"),
    },
    {
        "key": "storefront",
        "name": "Lumen Storefront",
        "description": "Checkout, catalog and the new collection pages.",
        "agents": ("forge", "iris", "atlas"),
    },
)
AUTUMN_AGENTS = ("atlas", "iris", "forge")


def _user(text: str, turn: str) -> SessionEvent:
    return SessionEvent(
        type="user_message",
        data={"role": "user", "content": text, "content_type": "text", "turn_id": turn},
    )


def _assistant(text: str, turn: str) -> SessionEvent:
    return SessionEvent(
        type="assistant_message",
        data={
            "role": "assistant",
            "content": text,
            "content_type": "text",
            "turn_id": turn,
            "message_id": f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, turn + text).hex}",
            "assistant_phase_index": 0,
        },
    )


def _tool(
    turn: str, name: str, arguments: dict[str, Any], result: str, **extra: Any
) -> list[SessionEvent]:
    call_id = f"call_{uuid.uuid5(uuid.NAMESPACE_URL, turn + name + str(arguments)).hex}"
    return [
        SessionEvent(
            type="tool_call",
            data={
                "call_id": call_id,
                "name": name,
                "turn_id": turn,
                "status": "started",
                "arguments": arguments,
            },
        ),
        SessionEvent(
            type="tool_result",
            data={
                "call_id": call_id,
                "name": name,
                "turn_id": turn,
                "status": "complete",
                "result": result,
                "duration_ms": extra.pop("duration_ms", 840),
                **extra,
            },
        ),
    ]


PODS = """NAME                              READY   STATUS    RESTARTS   AGE
storefront-web-7c9d5b8f6-2xk4q    1/1     Running   0          9h
storefront-web-7c9d5b8f6-r8m2c    1/1     Running   0          9h
checkout-api-5f6b7d9c4-hq7lp      1/1     Running   0          9h
checkout-api-5f6b7d9c4-wz3nd      1/1     Running   0          9h
catalog-worker-6d8f9b7c5-k2j9s    1/1     Running   0          9h
search-indexer-0                  1/1     Running   0          2d
postgres-0                        1/1     Running   0          6d"""

CART_DIFF = (
    "@@ -12,9 +12,17 @@ class CartStore:\n"
    "     def load(self, session):\n"
    "-        return self._sessions.get(session.id, Cart.empty())\n"
    "+        if session.account_id:\n"
    "+            saved = self._accounts.get(session.account_id)\n"
    "+            if saved is not None:\n"
    "+                return saved.merge(self._sessions.pop(session.id, None))\n"
    "+        return self._sessions.get(session.id, Cart.empty())\n"
    " \n"
    "     def save(self, session, cart):\n"
    "-        self._sessions[session.id] = cart\n"
    "+        if session.account_id:\n"
    "+            self._accounts[session.account_id] = cart\n"
    "+        else:\n"
    "+            self._sessions[session.id] = cart\n"
)
CART_TEST_DIFF = (
    "@@ -0,0 +1,14 @@\n"
    "+from storefront.cart import Cart, CartStore\n"
    "+\n"
    "+def test_cart_follows_the_account(store, guest, member):\n"
    "+    store.save(member, Cart.of('lamp-amber'))\n"
    "+    assert store.load(member.on_new_device()).items == ['lamp-amber']\n"
    "+\n"
    "+def test_guest_cart_merges_on_sign_in(store, guest, member):\n"
    "+    store.save(guest, Cart.of('shade-linen'))\n"
    "+    signed_in = guest.sign_in(member)\n"
    "+    assert store.load(signed_in).items == ['shade-linen']\n"
)


DIFF_STAT = (
    " storefront/cart.py | 11 +++++++++--\n"
    " tests/test_cart.py | 10 ++++++++++\n"
    " 2 files changed, 19 insertions(+), 2 deletions(-)"
)

# Fictional sources on reserved example domains.
SEARCH_RESULTS = (
    "1. Lighting retailers try conversational shopping - retail-notes.example\n"
    "2. What customers ask a lamp shop assistant - storefront-lab.example\n"
    "3. Guided selling for home decor - homegoods-weekly.example"
)
FETCHED = (
    "Retailers that added a guided assistant saw customers ask about room size, "
    "bulb warmth and dimming first. Product comparisons and stock questions followed."
)


def conversations(projects: dict[str, str], autumn: str) -> list[dict[str, Any]]:
    """Seeded history; ``hours`` is how long ago the last message was sent."""
    return [
        {
            "key": "weekly-plan",
            "agent": "atlas",
            "project": autumn,
            "title": "Weekly plan",
            "hours": 2,
            "read": False,
            "events": lambda t: [
                _user("What should the team focus on this week?", t),
                _assistant(
                    "Three priorities this week:\n\n"
                    "1. **Early access opens Friday.** Iris finalises the invitation by Wednesday; "
                    "Forge ships the early-access price on Thursday.\n"
                    "2. **Staging upgrade rehearsal.** Vela runs it Tuesday night with a tested rollback.\n"
                    "3. **Partner newsletter.** Draft ready for your review on Thursday.\n\n"
                    "I'll check progress every morning and flag anything that slips.",
                    t,
                ),
            ],
        },
        {
            "key": "staging-health",
            "agent": "vela",
            "project": projects["platform"],
            "title": "Staging health check",
            "hours": 5,
            "read": False,
            "events": lambda t: [
                _user("How is staging after last night's deploy?", t),
                *_tool(
                    t, "bash", {"command": "kubectl get pods -n staging"}, PODS, duration_ms=1210
                ),
                _assistant(
                    "Staging is healthy: all 7 workloads are running with no restarts since the deploy. "
                    "Checkout p95 latency is back to 180 ms.\n\n"
                    "One thing to watch: the image cache on the edge executor is at 82%. "
                    "I'll prune images older than 14 days tonight unless you object.",
                    t,
                ),
            ],
        },
        {
            "key": "partner-newsletter",
            "agent": "iris",
            "project": autumn,
            "title": "Partner newsletter",
            "hours": 7,
            "read": False,
            "events": lambda t: [
                _user(
                    "Draft the partner newsletter for the Autumn collection. Warm, short, one clear call to action.",
                    t,
                ),
                _assistant(
                    "**Subject:** Autumn light, a week early\n\n"
                    "The evenings are drawing in, and our Autumn collection is ready to meet them. "
                    "Amber glass, linen shades and a dimmer that remembers your favourite glow.\n\n"
                    "As a partner, you see it first: early access opens **Friday at 9:00**, "
                    "a week before the public launch.\n\n"
                    "**Preview the collection →**\n\n"
                    "Want a shorter version for the shop window displays as well?",
                    t,
                ),
            ],
        },
        {
            "key": "saved-carts",
            "agent": "forge",
            "project": projects["storefront"],
            "title": "Checkout · saved carts",
            "hours": 26,
            "read": True,
            "events": lambda t: [
                _user("Customers lose their cart when they switch devices. Can we keep it?", t),
                *_tool(
                    t,
                    "apply_patch",
                    {"file_path": "storefront/cart.py"},
                    "Patched storefront/cart.py and tests/test_cart.py.",
                    file_diffs=[
                        {"path": "storefront/cart.py", "diff": CART_DIFF},
                        {"path": "tests/test_cart.py", "diff": CART_TEST_DIFF},
                    ],
                    duration_ms=64,
                ),
                *_tool(t, "bash", {"command": "git diff --stat"}, DIFF_STAT, duration_ms=180),
                _assistant(
                    "Carts now follow the account. A signed-in customer gets the same cart on every device, "
                    "and a guest cart merges into the account on sign-in. Two tests cover both paths. "
                    "Ready for review.",
                    t,
                ),
            ],
        },
        {
            "key": "assistant-research",
            "agent": "iris",
            "project": projects["storefront"],
            "title": "Brief: AI shopping assistants",
            "hours": 28,
            "read": True,
            "events": lambda t: [
                _user(
                    "How do other home-lighting shops use AI shopping assistants? "
                    "Short brief with sources, please.",
                    t,
                ),
                *_tool(
                    t,
                    "web_search",
                    {"query": "AI shopping assistant home lighting retail"},
                    SEARCH_RESULTS,
                    duration_ms=1640,
                ),
                *_tool(
                    t,
                    "web_fetch",
                    {"url": "https://retail-notes.example/conversational-shopping"},
                    FETCHED,
                    duration_ms=920,
                ),
                _assistant(
                    "**Brief: AI shopping assistants in home lighting**\n\n"
                    "- Customers start with the room, not the product: size, mood and bulb warmth.\n"
                    "- The assistants that work answer three things fast: which lamp fits, "
                    "is it in stock, and how it compares.\n"
                    "- Guided selling pays off most for gifts and first-time buyers.\n\n"
                    "For Lumen, a room-first assistant on the Autumn collection pages is the smallest useful start.",
                    t,
                ),
            ],
        },
        {
            "key": "six-week-review",
            "agent": "iris",
            "project": autumn,
            "title": "Launch results · six-week review",
            "hours": 30,
            "read": True,
            "events": lambda t: [
                _user("Summarise how the spring launch went, so we can plan Autumn better.", t),
                _assistant(
                    "The spring launch reached 6,100 people and converted 4.2% in the first six weeks. "
                    "Early-access customers converted at almost twice that rate.\n\n"
                    "Two lessons for Autumn: announce the early-access date in the first line, "
                    "and send the reminder 48 hours before opening, not on the day.",
                    t,
                ),
            ],
        },
        {
            "key": "executor-capacity",
            "agent": "vela",
            "project": projects["platform"],
            "title": "Executor capacity for launch week",
            "hours": 50,
            "read": True,
            "events": lambda t: [
                _user("Do we have enough executor capacity for launch week?", t),
                _assistant(
                    "Yes, with one change. The cloud executor pool handles the expected load with 40% headroom. "
                    "I recommend moving the image-render jobs to the studio executor during launch week, "
                    "so the cloud pool keeps that headroom for checkout tasks.",
                    t,
                ),
            ],
        },
    ]


SCHEDULES = (
    {
        "agent": "atlas",
        "project": "autumn",
        "name": "Monday team plan",
        "cron": "0 8 * * 1",
        "title": "Monday team plan",
        "description": "Prepare the Monday team plan for the Lumen crew.",
    },
    {
        "agent": "vela",
        "project": "platform",
        "name": "Nightly staging check",
        "cron": "30 2 * * *",
        "title": "Nightly staging check",
        "description": "Run the nightly staging health check and report anything unusual.",
    },
    {
        "agent": "iris",
        "project": "autumn",
        "name": "Friday launch digest",
        "cron": "0 16 * * 5",
        "title": "Friday launch digest",
        "description": "Write the Friday launch digest for the Autumn collection.",
    },
)

TASKS = (
    {
        "key": "readiness-brief",
        "agent": "iris",
        "project": "autumn",
        "conversation": "partner-newsletter",
        "title": "Launch readiness brief",
        "description": "Prepare the launch readiness brief for the Autumn collection.",
        "deliverable": (
            "# Launch readiness brief\n\n"
            "**Status: on track for Friday early access.**\n\n"
            "| Area | Owner | Status |\n|---|---|---|\n"
            "| Invitation and newsletter | Iris | Ready for review |\n"
            "| Early-access pricing | Forge | Shipped to staging |\n"
            "| Staging upgrade | Vela | Rehearsed, rollback tested |\n"
            "| Support playbook | Atlas | Draft |\n\n"
            "## Open risk\n\nCourier capacity for the first weekend is not confirmed yet."
        ),
        "summary": "Launch readiness brief ready: invitation, pricing and staging are on track; courier capacity is the open risk.",
    },
    {
        "key": "upgrade-rehearsal",
        "agent": "vela",
        "project": "platform",
        "conversation": "staging-health",
        "title": "Staging upgrade rehearsal",
        "description": "Rehearse the staging cluster upgrade with a tested rollback.",
        "deliverable": (
            "# Staging upgrade rehearsal\n\n"
            "- Upgraded the staging cluster and all 7 workloads.\n"
            "- Rolled back and forward again; rollback took 6 minutes.\n"
            "- No customer-facing errors during the window.\n\n"
            "Production upgrade can follow the same runbook next Tuesday."
        ),
        "summary": "Staging upgrade rehearsed: 7/7 workloads healthy, rollback tested in 6 minutes.",
    },
    {
        "key": "rounding-fix",
        "agent": "forge",
        "project": "storefront",
        "conversation": "saved-carts",
        "title": "Fix rounding in early-access prices",
        "description": "Fix the half-cent rounding in early-access prices and add regression tests.",
        "deliverable": (
            "# Early-access price rounding\n\n"
            "Prices now round half-up to the cent before the early-access discount is shown. "
            "Six regression tests cover the catalog prices that were off by one cent."
        ),
        "summary": "Fixed half-cent rounding in early-access prices and added six regression tests.",
    },
    {
        "key": "status-report",
        "agent": "atlas",
        "project": "autumn",
        "conversation": "weekly-plan",
        "title": "Weekly status report",
        "description": "Write the weekly status report for the Autumn launch.",
        "deliverable": (
            "# Weekly status report\n\n"
            "**Overall: green.** Early access opens Friday.\n\n"
            "- Invitation and partner newsletter drafted.\n"
            "- Early-access pricing shipped to staging.\n"
            "- Staging upgrade rehearsed with a tested rollback.\n\n"
            "Next week: production upgrade and launch-day support rota."
        ),
        "summary": "Weekly status: green. Early access opens Friday; production upgrade is next.",
    },
)


def task_scenario(spec: dict[str, Any]) -> dict[str, Any]:
    """Deterministic mock turns that publish a deliverable and complete the step."""
    write = {"title": spec["title"], "format": "markdown", "content": spec["deliverable"]}
    return {
        "id": f"lumen-task-{spec['key']}",
        "trigger": spec["description"],
        "description": "Fictional Lumen task result produced by the local mock model.",
        "turns": [
            {
                "role": "assistant",
                "steps": [
                    {
                        "type": "tool_call",
                        "call_id": f"call_validate_{spec['key']}",
                        "name": "validate_tool_call",
                        "arguments": {"tool": "builtin:write_deliverable", "arguments": write},
                        "delay_ms": 400,
                    },
                    {
                        "type": "tool_call",
                        "call_id": f"call_write_{spec['key']}",
                        "name": "write_deliverable",
                        "arguments": write,
                        "delay_ms": 400,
                    },
                ],
            },
            {
                "role": "assistant",
                "steps": [
                    {
                        "type": "tool_call",
                        "call_id": f"call_complete_{spec['key']}",
                        "name": "step_complete",
                        "arguments": {
                            "summary": spec["summary"],
                            "metadata": {
                                "result_status": "completed",
                                # Required by the general-task contract; states the mock provenance.
                                "verification": {
                                    "checks_run": ["Inspected the synthetic task result."],
                                    "results": ["Deliverable complete; no external changes."],
                                    "checks_not_run": [
                                        "Live operational checks: synthetic showcase content."
                                    ],
                                    "synthetic_content": True,
                                    "external_changes": False,
                                },
                            },
                        },
                        "delay_ms": 200,
                    }
                ],
            },
        ],
    }


def schedule_scenario(spec: dict[str, Any]) -> dict[str, Any]:
    """Let a scheduled run that fires during the runtime's lifetime finish cleanly."""
    return task_scenario(
        {
            "key": "schedule-" + spec["name"].lower().replace(" ", "-"),
            "title": spec["title"],
            "description": spec["description"],
            "deliverable": f"# {spec['title']}\n\nEverything is on track. No action is needed this time.",
            "summary": f"{spec['title']}: everything is on track.",
        }
    )


async def _json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


async def seed_workspace(
    factory,
    provider,
    *,
    owner: str,
    password: str,
    autumn_project: str,
    fixture_dir: Path,
) -> dict[str, Any]:
    """Create crew, projects, history, schedules and completed tasks; return their IDs."""
    async with (
        httpx.AsyncClient(base_url=API, timeout=30) as client,
        httpx.AsyncClient(base_url=MOCK, timeout=30) as mock,
    ):
        login = await _json(
            await client.post(
                "/api/auth/login", json={"email": owner, "password": password, "mode": "native"}
            )
        )
        client.headers["Authorization"] = f"Bearer {login['token']}"

        # Crew with the approved portraits used in the product film.
        existing = {
            item["agent_id"] for item in (await _json(await client.get("/api/v1/agents")))["items"]
        }
        for spec in CREW:
            avatar = (fixture_dir / "avatars" / f"{spec['agent_id']}.webp").read_bytes()
            upload = await _json(
                await client.post(
                    "/api/v1/images/upload",
                    files={"file": (f"{spec['agent_id']}.webp", avatar, "image/webp")},
                )
            )
            payload = {
                "name": spec["name"],
                "display_name": spec["name"],
                "description": spec["description"],
                "system_prompt": (
                    f"You are {spec['name']}, part of the Lumen agent crew. "
                    f"{spec['description']} Be concise, concrete and outcome-oriented."
                ),
                "personality": {
                    "purpose": spec["purpose"],
                    "tone": spec["tone"],
                    "temperament": "steady",
                    "behavioral_rules": [],
                },
                "llm_config": MODEL,
                "capabilities": CAPABILITIES,
                "execution": EXECUTION,
                "avatar_image_id": upload["image_id"],
                "status": "active",
            }
            if spec["agent_id"] in existing:
                await _json(await client.put(f"/api/v1/agents/{spec['agent_id']}", json=payload))
            else:
                await _json(
                    await client.post(
                        "/api/v1/agents",
                        json={**payload, "agent_id": spec["agent_id"], "agent_type": "primary"},
                    )
                )

        # The local-compose test agent and its welcome chat are not part of the story.
        if LOCAL_AGENT in existing:
            await _json(await client.delete(f"/api/v1/agents/{LOCAL_AGENT}"))
        welcome = await client.get(f"/api/v1/conversations/{LOCAL_CONVERSATION}")
        if welcome.status_code == 200:
            await _json(await client.delete(f"/api/v1/conversations/{LOCAL_CONVERSATION}"))
        # The crew is configured, so first-run setup is done for this workspace.
        await _json(await client.put("/api/v1/onboarding", json={"status": "completed"}))

        listed = await _json(await client.get("/api/v1/projects"))
        by_name = {item["name"]: item["project_id"] for item in listed}
        projects: dict[str, str] = {"autumn": autumn_project}
        for spec in PROJECTS:
            if spec["name"] not in by_name:
                created = await _json(
                    await client.post(
                        "/api/v1/projects",
                        json={"name": spec["name"], "description": spec["description"]},
                    )
                )
                by_name[spec["name"]] = created["project_id"]
            projects[spec["key"]] = by_name[spec["name"]]
        assignments = [(autumn_project, agent) for agent in AUTUMN_AGENTS] + [
            (projects[spec["key"]], agent) for spec in PROJECTS for agent in spec["agents"]
        ]
        for project_id, agent in assignments:
            await _json(
                await client.put(
                    f"/api/v1/projects/{project_id}/agents/{agent}",
                    json={
                        "is_default": agent == ("atlas" if project_id == autumn_project else None)
                    },
                )
            )

    # Archived agents still appear in the dashboard strip; hide the local-compose test agent.
    async with factory.begin() as db:
        local = await db.get(Agent, LOCAL_AGENT)
        if local is not None:
            local.hidden = True

    # Seeded history goes to the canonical owners: metadata in Cognis, content in Intaris.
    now = datetime.now(UTC)
    seeded: dict[str, dict[str, str]] = {}
    for spec in conversations(projects, autumn_project):
        async with factory.begin() as db:
            conversation = await create_conversation(
                db,
                owner,
                spec["agent"],
                "web",
                project_id=spec["project"],
                title=spec["title"],
                title_source="manual",
                context_data={"showcase": spec["key"]},
            )
            session_id = f"sess_{uuid.uuid4().hex}"
            await create_session(
                db,
                conversation.conversation_id,
                owner,
                spec["agent"],
                session_id=session_id,
                intaris_session_id=session_id,
                mnemory_session_id=session_id,
                status="idle",
            )
            await update_conversation_active_session(db, conversation.conversation_id, session_id)
            conversation_id = conversation.conversation_id
        await provider.create_session(session_id, spec["title"], spec["agent"], user_id=owner)
        turn = f"turn_{uuid.uuid5(uuid.NAMESPACE_URL, session_id).hex}"
        events = spec["events"](turn)
        recorded = await provider.record_events(
            session_id,
            events,
            source="showcase_seed",
            idempotency_key=f"{session_id}:workspace-v1",
            user_email=owner,
            agent_id=spec["agent"],
        )
        if not recorded.ok or recorded.count != len(events):
            raise RuntimeError(f"Workspace history for {spec['key']} requires reconciliation")
        last = now - timedelta(hours=spec["hours"])
        async with factory.begin() as db:
            row = await db.get(Conversation, conversation_id)
            assert row is not None
            row.created_at = last - timedelta(minutes=12)
            row.last_message_at = last
            row.last_read_at = last if spec["read"] else None
        seeded[spec["key"]] = {
            "conversation_id": conversation_id,
            "session_id": session_id,
            "agent_id": spec["agent"],
            "project_id": spec["project"],
        }

    async with (
        httpx.AsyncClient(base_url=API, timeout=30) as client,
        httpx.AsyncClient(base_url=MOCK, timeout=30) as mock,
    ):
        client.headers["Authorization"] = f"Bearer {login['token']}"
        # Project seeded tool events into Work so files and commands are browsable.
        for item in seeded.values():
            conversation_id = item["conversation_id"]
            await _json(
                await client.post(
                    "/api/v1/work/refresh",
                    json={
                        "scope": {
                            "kind": "conversation",
                            "key": f"conversation:{conversation_id}",
                            "conversation_id": conversation_id,
                        }
                    },
                )
            )
        carts = seeded["saved-carts"]["conversation_id"]
        async with asyncio.timeout(60):
            while True:
                files = await _json(
                    await client.get(
                        f"/api/v1/chat/v2/conversations/{carts}/work", params={"category": "files"}
                    )
                )
                if any(
                    stat.get("path", "").endswith("storefront/cart.py")
                    and stat.get("path_generation_id")
                    for mutation in files.get("mutations", [])
                    for stat in mutation.get("file_stats", [])
                ):
                    break
                await asyncio.sleep(0.5)
        workflows = (await _json(await client.get("/api/v1/workflows")))["items"]
        [workflow] = [
            item["workflow_id"]
            for item in workflows
            if item["workflow_id"].endswith("general-task")
        ]

        schedules: dict[str, str] = {}
        for spec in SCHEDULES:
            (await mock.post("/__mock/scenario", json=schedule_scenario(spec))).raise_for_status()
            created = await _json(
                await client.post(
                    "/api/v1/schedules",
                    json={
                        "name": spec["name"],
                        "schedule_type": "cron",
                        "cron_expr": spec["cron"],
                        "timezone": "Europe/Prague",
                        "agent_id": spec["agent"],
                        "workflow_id": workflow,
                        "project_id": projects[spec["project"]],
                        "task_template": {
                            "title": spec["title"],
                            "description": spec["description"],
                        },
                    },
                )
            )
            schedules[spec["name"]] = created["schedule_id"]

        tasks: dict[str, str] = {}
        for spec in TASKS:
            (await mock.post("/__mock/scenario", json=task_scenario(spec))).raise_for_status()
            created = await _json(
                await client.post(
                    "/api/v1/tasks",
                    json={
                        "agent_id": spec["agent"],
                        "project_id": projects[spec["project"]],
                        "title": spec["title"],
                        "description": spec["description"],
                        "workflow_id": workflow,
                        "delivery_mode": "specific_conversation",
                        "delivery_target": seeded[spec["conversation"]]["conversation_id"],
                        "interaction_mode_override": "none",
                        "status": "queued",
                    },
                )
            )
            tasks[spec["key"]] = created["task_id"]
        steps: dict[str, dict[str, str]] = {}
        async with asyncio.timeout(180):
            pending = set(tasks.values())
            while pending:
                for task_id in list(pending):
                    detail = await _json(await client.get(f"/api/v1/tasks/{task_id}"))
                    if detail["status"] in {"failed", "cancelled"}:
                        raise RuntimeError(f"Workspace task {task_id} ended {detail['status']}")
                    if detail["status"] == "completed":
                        pending.discard(task_id)
                        # Usage for task work is attributed to its step-run session, as at runtime.
                        [step] = detail["step_runs"]
                        steps[task_id] = {
                            "conversation_id": step["conversation_id"],
                            "session_id": step["session_id"],
                        }
                await asyncio.sleep(1)

    return {
        "projects": projects,
        "conversations": seeded,
        "schedules": schedules,
        "tasks": {
            key: {
                "task_id": task_id,
                **steps[task_id],
                **next(
                    {"agent_id": spec["agent"], "project_id": projects[spec["project"]]}
                    for spec in TASKS
                    if spec["key"] == key
                ),
            }
            for key, task_id in tasks.items()
        },
    }
