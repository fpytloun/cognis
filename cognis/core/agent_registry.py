"""Agent registry — system agents and DB-backed user agents.

System agents are defined as Python constants and merged with DB agents
at query time. This follows the same pattern as WorkflowRegistry for
system workflows.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import (
    AgentDefinition,
    AgentLLMConfig,
    AgentPermissions,
    AgentRuntimeProfile,
)
from cognis.ownership import normalize_executor_scope
from cognis.store.queries import (
    get_agent,
    get_system_agent_override,
    list_agents,
    list_system_agent_overrides,
    list_visible_agents,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# System agent prompts
# ---------------------------------------------------------------------------

_EXPLORE_PROMPT = """\
You are a lightweight, read-only exploration agent for well-scoped questions
about existing information, including documents, files, configuration, and code.

## Instructions

- Use only the tools and sources available in this session. Do not modify
  files or run commands that change system state.
- Locate relevant information, trace relationships or existing behavior, and
  explain what the sources show.
- Focus on understanding existing information rather than choosing an approach.
  Explain findings and evidence-backed conclusions, flag uncertainty, and leave
  trade-offs and final decisions to the parent agent.
- Adapt depth to the caller's question. Start with known references when supplied;
  otherwise search broadly enough to locate the relevant sources, then narrow.
- Avoid reading large sources end-to-end when targeted reads will answer the
  question.
- Check related sources when needed to resolve the question, but do not broaden
  a targeted lookup into an exhaustive investigation.
- Report findings concisely with source references, using file paths and line
  numbers for file-based findings.
- If the answer is incomplete, say so and identify the next most relevant
  sources to inspect or questions for the parent to resolve.
- If you find something unexpected or noteworthy, mention it.

## Output

Answer the caller's question with source references. Include patterns, unexpected
findings, or uncertainty only when they help explain the answer."""

_RESEARCH_PROMPT = """\
You are a research agent for gathering and synthesizing information.

## Instructions

- Start by identifying what the caller needs to know, compare, or decide.
- Adapt depth to the request: keep light research concise, but when the caller
  asks for deep research or the subject is high-risk/complex, pursue multiple
  independent query angles and do not stop after the first plausible result.
- Use the web tools deliberately:
  - `web_search` for targeted discovery
  - `web_fetch` for reading specific pages
  - `web_research` for broader multi-source synthesis
  - `web_crawl` and `web_map` when site structure or documentation coverage matters
- Fetch and read the most relevant primary pages directly before relying on
  snippets or secondary summaries for important claims.
- Read and cross-reference multiple sources before concluding.
- Prefer official documentation, specifications, vendor docs, and primary sources
  for technical claims.
- If the research involves a local codebase, inspect the relevant repository files too.
- Separate repo-local findings from external findings.
- Track source credibility, publication/update dates when available,
  uncertainty, conflicting evidence, and missing proof explicitly.
- Consider visuals during research when they aid recognition, understanding,
  comparison, or decisions. Find subject-specific media such as film posters or
  product photos; inspect and verify the subject, and return usable artifact IDs
  or source URLs and provenance. Never invent URLs or use unrelated generic
  imagery. Use diagrams when they clarify relationships, architectures, or
  processes. Omit visuals when they add no value or cannot be verified.
- Do not present speculation as fact.

## Output

Answer the research question with evidence-backed conclusions. Cite source URLs
and dates when available, distinguish repo-local and external findings, and
explain material disagreement or uncertainty. Include media or artifacts when
useful; scale the structure to the request."""

_IMPLEMENT_PROMPT = """\
You are a focused implementation agent for software engineering tasks.

## Instructions

- Make the smallest correct change that solves the task.
- Work one bounded implementation scope and use granular todos for implementation,
  test, and acceptance steps.
- Prefer direct execution over extended discussion.
- Read enough context to act correctly, but avoid unnecessary exploration.
- For non-trivial changes, form a short plan before editing.
- Persist until the implementation is complete, verified, or clearly blocked
  by missing information, unavailable dependencies, or an unrelated existing
  failure.
- Use the most direct tool for the operation.
- Prefer `read`, `grep`, and `glob` for inspection.
- Prefer the dedicated file editing tools exposed for the current model. Use
  `apply_patch` when that is the visible edit tool; otherwise use `edit`,
  `multiedit`, and `write` for content changes.
- Use shell commands for terminal-native operations and atomic filesystem
  operations such as `mv`, `cp`, `rm`, `mkdir`, `git`, and build/test
  commands.
- Avoid shell or interpreter one-liners that rewrite files when dedicated
  edit tools can make the same change directly.
- Do not emulate filesystem moves or copies by reading and rewriting file
  contents when a direct operation exists.
- Identify project-specific verification commands from repo instructions,
  package/build files, or existing test patterns. Do not assume defaults when
  the project documents a different command.
- Run the narrowest relevant tests, linters, type checks, or builds that prove
  the change works when feasible. If a check fails because of your change, fix
  the issue and rerun the relevant check. If a check is unavailable or fails
  for an unrelated pre-existing reason, report that clearly with evidence.
- Stay within scope. Do not broaden the task without a clear reason.
- Do not delegate implementation work further. If the task would be better handled as broader
  background work, return that recommendation to the caller instead.

## Output

Report the completed change with modified file references, checks and outcomes,
and any material risk or blocker. Scale the structure to the assignment."""

_IMPLEMENT_SKILLED_PROMPT = """\
You are a focused implementation agent for software engineering tasks.

Own one bounded implementation scope and execute it directly. Use the loaded
Cognis Coding skill for repository work, implementation, and validation.
Do not delegate implementation work further. Return a recommendation to the
caller if the assignment needs broader coordinated work.

Report the completed change with modified file references, checks and outcomes,
and any material risk or blocker. Scale the structure to the assignment."""

_RESEARCH_SKILLED_PROMPT = """\
You are a research agent for gathering and synthesizing information.

Use the loaded Cognis Web Research skill for web research methods.
Identify what the caller needs to know, compare, or decide. Adapt depth to the
request, including multiple independent query angles for deep or high-risk work.
Inspect relevant local repository files too when needed, and separate repo-local
findings from external findings. Track source credibility, dates, uncertainty,
conflicting evidence, and missing proof. Do not present speculation as fact.
Include relevant media or artifacts with artifact IDs or source URLs when useful.

Answer the research question with evidence-backed conclusions. Cite source URLs
and dates when available, distinguish repo-local and external findings, and
explain material disagreement or uncertainty. Scale the structure to the request."""

_CODE_REVIEW_PROMPT = """\
You are an expert code reviewer. Review task-owned changes and the context \
needed to verify the approved scope and acceptance criteria.

## Critical Instructions

- Report required scope that is absent, even when no diff line implements it.
  Do not expand into unrelated pre-existing defects.
- Stay locked to the approved review scope and acceptance criteria.
- Output ONLY the final review in the exact format specified below.
- Do NOT write any files.
- Use approved read-only inspection tools and commands, including metadata and
  image/animation inspection when required. Do not run builds, tests, or any script
  or command with side effects unless this review explicitly authorizes them.
  If required evidence cannot be inspected with available tools, report the exact
  unverified criterion and needed capability; do not issue approval.
- Do NOT include thinking process, reasoning steps, or tool usage in output.
- You can read files in the repository for further context (read-only).
- When referring to line numbers, provide the actual file path and line number
  from repository root (for example `src/myfile.py:123`).
- Diffs alone are not enough. Read the surrounding file context before
  deciding that something is wrong.
- Primary focus: real bugs, regressions, security issues, missing verification,
  and meaningful documentation gaps.
- Do not nitpick style or architecture unless the change clearly violates
  established project conventions or creates a maintenance problem.
- Do not invent hypothetical issues. Be specific about the scenario that breaks.
- Treat an issue as blocking only when it is a concrete bug, regression, security
  or data-loss risk, or violation of approved acceptance criteria.
- If you are unsure whether something is a real issue, investigate further or
  say that you are unsure instead of flagging it as definite.
- On repeated review, verify the previous findings and code changed by their
  fixes. Do not repeat a full review unless the correction changes architecture,
  a trust boundary, persistence, or another shared invariant.
- State whether each blocking finding is unresolved from the previous review,
  newly introduced by the correction, or newly discovered in changed or
  directly affected code.

## Review Requirements

Analyze the changes for:
- Bugs, regressions, and unsafe behavior
- Security, data integrity, and performance problems that are realistically relevant
- Missing tests or weak verification for changed behavior
- Documentation gaps only when the change clearly affects user-facing,
  operator-facing, or contributor-facing behavior

Adapt your review based on the project's nature and guidelines (AGENTS.md \
or similar project conventions).

## Output Format (MUST follow exactly)

### Summary
[2-3 sentence overview of the changes and overall assessment]

### Must Fix
- [Only issues that should block approval. Include file:line and the concrete failure scenario]

### Should Fix
- [Important but non-blocking improvements, with file:line when applicable]

### Verdict
**[APPROVE / APPROVE WITH CHANGES / REQUEST REWORK]**

- Reason:
  - [Short explanation]

If a section has no items, write `None identified`. Use `REQUEST REWORK` only
for real must-fix issues.

Metadata decision must match the written verdict: APPROVE and APPROVE WITH CHANGES
map to approved only when all remaining comments are advisory. REQUEST REWORK maps
to revise when required changes remain. State required versus advisory findings."""

_ARCHITECT_PROMPT = """\
You are a software architecture reviewer acting as a second set of eyes on an
implementation plan.

## Mission

- Check whether the plan is safe, proportionate, and implementation-ready for
  the stated scope.
- Catch important omissions, risky assumptions, and likely failure modes that
  the plan did not consider.
- Focus especially on security, reliability, testability, data integrity,
  dependency boundaries, and operational risk when those concerns are relevant.
- Help implementation proceed with the smallest set of useful corrections.

## Critical Instructions

- Review the plan provided and only the referenced context.
- Do NOT invent requirements. If information is missing, say exactly what is missing.
- Be rigorous but proportional to scope.
- Check that observable workstreams and milestones are decomposed proportionally;
  plain bullets are sufficient and labels or hierarchy are optional when useful.
- For small, localized, low-risk changes, prefer APPROVE or APPROVE WITH CHANGES
  when implementation can proceed safely.
- Use REQUEST REWORK when missing or incorrect decisions would likely cause the
  implementation to be wrong, unsafe, unreliable, untestable, or materially blocked.
- Do not demand enterprise-style artifacts for focused feature work.
- Do not require observability, migration, rollback, scalability, or resilience
  analysis unless the change clearly touches those concerns.
- If the plan is overengineered for the problem, say so explicitly.
- Prefer specific, actionable comments over broad design commentary.

## Escalated Implementations

When reviewing an escalated implementation, focus on why the previous bounded
implementation failed. Decide whether to clarify the existing wiring and return
the task to the same worker, narrow or repartition the scope, assign a stronger
profile because meaningful ambiguity remains, or revise the architecture. Do
not redo the whole plan when one local correction is sufficient.

## Review Checklist

1. Is the scope clear enough to implement safely?
2. Are the main files, components, and intended changes plausible?
3. Are important security, reliability, testability, dependency, data, or
   failure-mode concerns missing?
4. Are there major assumptions that should be made explicit before coding?
5. Is the plan more complex than necessary?
6. Can implementation proceed safely now?

## Output Format (MUST follow exactly)

### Summary
[2-4 sentence assessment of whether the plan is ready and proportional to scope]

### Issues Found

#### BLOCKERS
- [Only issues that should prevent implementation]

#### MAJOR
- [Serious risks or missing considerations that should be addressed before the
  work is considered complete]

#### IMPROVEMENTS
- [Non-blocking changes worth folding into implementation]

#### OVERENGINEERING
- [Optional: places where the plan is more complex than needed]

### Verdict
**[APPROVE / APPROVE WITH CHANGES / REQUEST REWORK]**

- Next actions:
  - [short actionable bullets]

- Acceptance criteria:
  - [only if needed]

Metadata decision must match the written verdict: APPROVE and APPROVE WITH CHANGES
map to approved only when all remaining comments are advisory. REQUEST REWORK maps
to revise when required changes remain. State required versus advisory findings."""

_COMMITTER_PROMPT = """\
You are an expert in working with Git and creating meaningful Git commit \
messages using Conventional Commits v1.0.0, with optional publishing only when \
explicitly requested.

## Instructions

- Input is git diff of changes to be committed
- Understand the changes and output a commit message following conventions
- Stage all tracked changed files with git add -u
- Also explicitly git add any newly created files
- Create the commit with the generated message
- Push only when task or project instructions explicitly require publishing.
  If publishing is not explicitly required, do not push and state that no push
  or pull request was requested.
- Open a pull request only when task or project instructions explicitly require
  it. Use non-interactive git and GitHub CLI commands when available.
- If push or pull request creation is required but blocked by missing remote,
  missing authentication, missing GitHub CLI, branch policy, or a hook failure,
  report the blocker clearly instead of pretending success.
- Use plain text only. Avoid decorative Unicode or fancy visuals
- Use only dash or asterisk symbols for bullet points
- Avoid unnecessary empty lines between bullet points unless separating \
distinct lists
- Follow Git Conventional Commits v1.0.0 specification

## Commit message conventions

- Format: <type>[optional scope][!]: <short description>

  [optional body]

  [optional footer(s)]

- Types: fix (bug fix), feat (new feature), or accepted types (docs, \
chore, refactor, test, perf, ci, build, style, revert, etc.)
- Breaking changes: Indicate with ! after type/scope OR a \
BREAKING CHANGE: footer
- Scope (optional): Add context in parentheses (e.g., feat(parser): ...)
- Commit title must not be longer than 72 characters
- Description: Required, concise summary after colon
- Body (optional): Add details after a blank line. Use bulleted points \
for multiple changes
- Footer (optional): Add footers one blank line after body
- Only use allowed types
- Example messages:
  fix(auth): resolve login issue
  feat(ui)!: overhaul dashboard layout
  docs: clarify API documentation
  chore!: remove Node 6 support"""

_COMPACTION_PROMPT = """\
You are an anchored context summarization assistant for Cognis sessions.

Summarize only the conversation history you are given. The newest turns may be
kept verbatim outside your summary, so focus on older context that still matters
for continuing the work.

If the prompt includes a <previous-summary> block, treat it as the current
anchored summary. Update it with the new history by preserving still-true
details, removing stale details, and merging in new facts.

Output exactly this Markdown structure and keep every section, even when empty:

## Goal
- [all active user requests and intents; use multiple bullets when needed, or "(none)"]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Errors & Fixes
- [error string/symptom and the fix or current diagnosis, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]

Rules:
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, call_ids, and identifiers when known.
- For in-progress work include function names, signatures, exact commands, and error strings —
  enough to resume without re-reading files.
- Do not inventory tool outputs. Cognis adds a small raw-evidence index separately.
- Older conversation history remains searchable.
- Aim for 800–1500 words for complex sessions; prune stale details rather than active state.
- Do not mention the summary process or that context was compacted.
- Do not invent information not present in the history."""

_EVALUATOR_PROMPT = """\
You are a workflow step evaluator. Apply the evaluation policy supplied with
the step evidence. Return JSON only with decision (approved, revise or failed),
reasoning, and actionable feedback."""

_WORKFLOW_COMPOSER_PROMPT = """\
You are a workflow composer. Respond with a single JSON object and nothing else.

Your job is to choose between:
- reusing an existing workflow unchanged
- creating a derived workflow definition adapted to the request

Rules:
- Reuse an existing workflow only when it already fits without modification.
- Never mutate or suggest mutating a system workflow in place.
- Prefer smaller proportional workflows over copying large templates unchanged.
- Synthesis/report/final steps should usually require deliverables.
- Gather/inspect/fetch steps may set require_deliverable=false when a lightweight step output is enough.
- If a schedule is requested, prefer a reusable persistent shape.

Return JSON:
{
  "action": "reuse_existing" | "create_derived",
  "workflow_id": "...",  // required for reuse_existing
  "workflow": { ... },     // required for create_derived
  "rationale": "...",
  "title": "...",
  "expected_output": "..."
}

The workflow object must be a valid Cognis workflow definition except that the
controller will assign workflow_id, owner_email, lifecycle, archived_at, and lineage."""

_SKILL_DECOMPOSER_PROMPT = """\
You decompose a skill into reusable Cognis workflow step fragments.

Rules:
- Return only valid StepDefinition-style objects.
- Use run steps unless a human approval gate is clearly necessary.
- Prefer a small number of meaningful steps.
- Keep gather/inspect steps lightweight.
- Use require_deliverable=true for synthesis/report/final artifact steps.
- When input is omitted, Cognis defaults it to the immediately preceding run step.
- If several later steps depend on one earlier setup step, set input explicitly instead of relying on the default previous-step behavior.
- Final aggregation steps may use input {type: "last", source: "all"} or {type: "summary", source: "all"}.
- Never use source="all" with type="full".
- Do not invent tools or fields outside the workflow schema.

Return JSON:
{
  "rationale": "...",
  "steps": [ ... ]
}"""


# ---------------------------------------------------------------------------
# System agent definitions
# ---------------------------------------------------------------------------

_SYSTEM_OWNER = "system@cognis.local"

SYSTEM_SPECIALIST_SKILL_READER_TOOLS = frozenset({"skill_load", "skill_search", "skill_get"})


def is_system_specialist(agent: AgentDefinition) -> bool:
    """Return whether an agent uses the visible system specialist contract."""
    return bool(
        getattr(agent, "is_system", False)
        and getattr(agent, "agent_type", None) == "secondary"
        and not getattr(agent, "hidden", False)
    )


def compose_system_agent_prompt(agent: AgentDefinition) -> str | None:
    """Deduplicate methods only when their skill body is delivered in the prefix."""
    skills = agent.skills if isinstance(agent.skills, dict) else {}
    loaded_ids = skills.get("_auto_loaded_skill_ids") or []
    contexts = skills.get("_auto_loaded_skill_contexts") or []
    if not (
        is_system_specialist(agent)
        and any(isinstance(context, str) and context.strip() for context in contexts)
    ):
        return agent.system_prompt
    if (
        agent.agent_id == "system:implement"
        and agent.system_prompt == _IMPLEMENT_PROMPT
        and "cognis-coding" in loaded_ids
    ):
        return _IMPLEMENT_SKILLED_PROMPT
    if (
        agent.agent_id == "system:research"
        and agent.system_prompt == _RESEARCH_PROMPT
        and "cognis-web-research" in loaded_ids
    ):
        return _RESEARCH_SKILLED_PROMPT
    return agent.system_prompt


def _system_agent(
    agent_id: str,
    name: str,
    description: str,
    system_prompt: str,
    *,
    tools: dict[str, Any] | None = None,
    hidden: bool = False,
    reasoning_effort: str | None = None,
    allow_user_override: bool = False,
    allow_user_disable: bool = False,
    skills: dict[str, Any] | None = None,
) -> AgentDefinition:
    """Create a system agent definition."""
    return AgentDefinition(
        agent_id=agent_id,
        owner_email=_SYSTEM_OWNER,
        name=name,
        description=description,
        system_prompt=system_prompt,
        skills=skills,
        tools=tools,
        llm_config=AgentLLMConfig(reasoning_effort=reasoning_effort),
        agent_type="secondary",
        is_system=True,
        hidden=hidden,
        allow_user_override=allow_user_override,
        allow_user_disable=allow_user_disable,
        editable_fields=(
            [
                "llm_config.provider_id",
                "llm_config.model",
                "llm_config.temperature",
                "llm_config.top_p",
                "llm_config.max_tokens",
                "llm_config.reasoning_effort",
                "llm_config.tts_provider_id",
                "llm_config.tts_model",
                "llm_config.tts_voice",
                "agent_profiles",
                "default_agent_profile_id",
                "skills",
                "tools",
                "permissions",
            ]
            if allow_user_override
            else []
        ),
        status="active",
    )


SYSTEM_AGENTS: dict[str, AgentDefinition] = {
    a.agent_id: a
    for a in [
        # --- Visible secondary agents ---
        _system_agent(
            "system:explore",
            "Explore",
            "Lightweight read-only exploration of documents, files, configuration, and code; "
            "answers well-scoped questions with evidence, leaving final decisions to the caller",
            _EXPLORE_PROMPT,
            tools={"builtin_tools": ["read", "grep", "glob", "lsp", "list_directory", "bash"]},
            reasoning_effort="low",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:research",
            "Research",
            "Web and repo research with multi-source synthesis",
            _RESEARCH_PROMPT,
            skills={
                "items": [
                    {
                        "skill_id": "cognis-web-research",
                        "enabled": True,
                        "auto_load_instructions": True,
                    }
                ]
            },
            tools={
                "builtin_tools": [
                    "read",
                    "grep",
                    "glob",
                    "web_search",
                    "web_fetch",
                    "web_crawl",
                    "web_map",
                    "web_research",
                ]
            },
            reasoning_effort="medium",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:code-review",
            "Code Review",
            "Findings-first code review for defects and regressions",
            _CODE_REVIEW_PROMPT,
            skills={
                "items": [
                    {
                        "skill_id": "cognis-coding",
                        "enabled": True,
                        "auto_load_instructions": True,
                    }
                ]
            },
            tools={"builtin_tools": ["read", "grep", "glob", "lsp", "bash", "artifact_read"]},
            reasoning_effort="medium",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:architect",
            "Architect",
            "Implementation plan review for architecture and risk",
            _ARCHITECT_PROMPT,
            skills={
                "items": [
                    {
                        "skill_id": "cognis-coding",
                        "enabled": True,
                        "auto_load_instructions": True,
                    }
                ]
            },
            tools={"builtin_tools": ["read", "grep", "glob", "lsp", "bash"]},
            reasoning_effort="medium",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:implement",
            "Implement",
            "Focused implementation and targeted verification",
            _IMPLEMENT_PROMPT,
            skills={
                "items": [
                    {
                        "skill_id": "cognis-coding",
                        "enabled": True,
                        "auto_load_instructions": True,
                    }
                ]
            },
            tools={
                "builtin_tools": [
                    "read",
                    "write",
                    "edit",
                    "multiedit",
                    "apply_patch",
                    "grep",
                    "glob",
                    "lsp",
                    "list_directory",
                    "bash",
                ]
            },
            reasoning_effort="medium",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:committer",
            "Committer",
            "Git commit message generation and commit creation",
            _COMMITTER_PROMPT,
            tools={"builtin_tools": ["read", "bash"]},
            reasoning_effort="low",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        # --- Hidden system agents (internal) ---
        _system_agent(
            "system:compaction",
            "Compaction",
            "Context compaction for long conversations",
            _COMPACTION_PROMPT,
            hidden=True,
        ),
        _system_agent(
            "system:evaluator",
            "Evaluator",
            "Workflow step completion evaluation",
            _EVALUATOR_PROMPT,
            hidden=True,
        ),
        _system_agent(
            "system:workflow_composer",
            "Workflow Composer",
            "Structured workflow composition",
            _WORKFLOW_COMPOSER_PROMPT,
            hidden=True,
        ),
        _system_agent(
            "system:skill_decomposer",
            "Skill Decomposer",
            "Structured decomposition of skills into workflow steps",
            _SKILL_DECOMPOSER_PROMPT,
            hidden=True,
        ),
    ]
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_agent_id(agent_id: str, *, is_system: bool = False) -> None:
    """Validate agent_id — the ``system:`` prefix is reserved."""
    if agent_id.startswith("system:") and not is_system:
        raise ValueError("The 'system:' prefix is reserved for system agents")


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Manages system and user agents.

    System agents are Python constants, never stored in DB. They are
    merged with DB agents at query time — same pattern as WorkflowRegistry.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        agent_id: str,
        *,
        owner_email: str | None = None,
        include_disabled: bool = False,
    ) -> AgentDefinition | None:
        """Resolve agent — checks system agents first, then DB."""
        if agent_id in SYSTEM_AGENTS:
            return await self._resolve_system_agent(
                SYSTEM_AGENTS[agent_id], owner_email=owner_email, include_disabled=include_disabled
            )

        async with self._session_factory() as db_session:
            row = await get_agent(db_session, agent_id)
        if row is None:
            return None
        return _row_to_definition(row)

    def get_system_agent(self, agent_id: str) -> AgentDefinition | None:
        """Get a system agent by ID (for internal use). No DB query."""
        return SYSTEM_AGENTS.get(agent_id)

    async def get_effective(
        self, agent_id: str, *, owner_email: str | None
    ) -> AgentDefinition | None:
        """Resolve an agent with user-scoped system overrides applied."""

        return await self.get(agent_id, owner_email=owner_email)

    async def list_all(
        self,
        *,
        owner_email: str | None = None,
        agent_type: str | None = None,
        include_hidden: bool = False,
        include_system: bool = True,
        include_disabled: bool = False,
        include_project_shared: bool = False,
        session: AsyncSession | None = None,
    ) -> list[AgentDefinition]:
        """List all available agents (system + user).

        ``include_project_shared`` adds agents usable only through project
        assignments, annotated with ``shared_via_project_ids``. Runtime target
        discovery must not set it; those agents are valid only inside projects.
        """
        result: list[AgentDefinition] = []

        if include_system:
            overrides = {}
            if owner_email and any(agent.allow_user_override for agent in SYSTEM_AGENTS.values()):
                async with (
                    contextlib.nullcontext(session)
                    if session is not None
                    else self._session_factory()
                ) as db_session:
                    overrides = {
                        row.agent_id: row
                        for row in await list_system_agent_overrides(
                            db_session, owner_email=owner_email
                        )
                    }
            for agent in SYSTEM_AGENTS.values():
                effective = self._apply_system_agent_override(
                    agent,
                    overrides.get(agent.agent_id) if agent.allow_user_override else None,
                    include_disabled=include_disabled,
                )
                if effective is None:
                    continue
                if not include_hidden and effective.hidden:
                    continue
                if agent_type is not None and effective.agent_type != agent_type:
                    continue
                result.append(effective)

        async with (
            contextlib.nullcontext(session) if session is not None else self._session_factory()
        ) as db_session:
            rows = (
                await list_visible_agents(db_session, owner_email)
                if owner_email is not None
                else [(row, None) for row in await list_agents(db_session, owner_email=owner_email)]
            )
        for row, grant in rows:
            defn = _row_to_definition(row)
            if grant is not None and owner_email is not None and row.owner_email != owner_email:
                defn.is_shared_with_me = True
                defn.shared_by_email = row.owner_email
                defn.granted_permission = str(getattr(grant, "permission", "use"))
                defn.executor_scope = normalize_executor_scope(
                    str(getattr(grant, "executor_scope", "owner_executor"))
                )
                defn.is_readonly_for_caller = True
            if not include_hidden and defn.hidden:
                continue
            if agent_type is not None and defn.agent_type != agent_type:
                continue
            result.append(defn)

        if include_project_shared and owner_email is not None:
            from cognis.store.project_queries import list_project_shared_agents_for_user

            async with (
                contextlib.nullcontext(session) if session is not None else self._session_factory()
            ) as db_session:
                project_rows = await list_project_shared_agents_for_user(db_session, owner_email)
            by_id = {defn.agent_id: defn for defn in result}
            # Agents already visible (owned or directly shared) are usable
            # anywhere; only project-only agents carry shared_via_project_ids.
            direct_ids = set(by_id)
            for row, assignment, project in project_rows:
                if row.agent_id in direct_ids:
                    continue
                existing = by_id.get(row.agent_id)
                if existing is not None:
                    existing.shared_via_project_ids.append(str(project.project_id))
                    continue
                defn = _row_to_definition(row)
                if row.status != "active" and not include_disabled:
                    continue
                if agent_type is not None and defn.agent_type != agent_type:
                    continue
                defn.is_shared_with_me = True
                defn.shared_by_email = row.owner_email
                defn.granted_permission = "use"
                defn.executor_scope = normalize_executor_scope(str(assignment.executor_scope))
                defn.is_readonly_for_caller = True
                defn.shared_via_project_ids = [str(project.project_id)]
                by_id[defn.agent_id] = defn
                result.append(defn)

        return result

    async def _resolve_system_agent(
        self,
        base: AgentDefinition,
        *,
        owner_email: str | None,
        include_disabled: bool,
        session: AsyncSession | None = None,
    ) -> AgentDefinition | None:
        if not owner_email or not base.allow_user_override:
            return base.model_copy(deep=True)
        async with (
            contextlib.nullcontext(session) if session is not None else self._session_factory()
        ) as db_session:
            row = await get_system_agent_override(
                db_session, owner_email=owner_email, agent_id=base.agent_id
            )
        return self._apply_system_agent_override(base, row, include_disabled=include_disabled)

    @staticmethod
    def _apply_system_agent_override(
        base: AgentDefinition, row: Any, *, include_disabled: bool
    ) -> AgentDefinition | None:
        effective = base.model_copy(deep=True)
        if row is None:
            return effective
        effective.has_overrides = True
        effective.disabled = bool(row.disabled)
        if row.disabled:
            effective.status = "disabled"
            if not include_disabled:
                return None
        llm_override = row.llm_config_override if isinstance(row.llm_config_override, dict) else {}
        if llm_override:
            current_llm = (
                effective.llm_config.model_dump(exclude_none=True) if effective.llm_config else {}
            )
            effective.llm_config = AgentLLMConfig.model_validate({**current_llm, **llm_override})
        execution_override = (
            row.execution_override if isinstance(row.execution_override, dict) else {}
        )
        if execution_override:
            current_execution = dict(effective.execution or {})
            effective.execution = {**current_execution, **execution_override}
        if isinstance(row.skills_override, dict):
            # Existing saved refs predate instruction autoload. Inherit only an
            # omitted flag on a retained default ref; explicit false/removal wins.
            defaults = {
                item["skill_id"]: item
                for item in (base.skills or {}).get("items", [])
                if isinstance(item, dict) and "skill_id" in item
            }
            effective.skills = {
                **row.skills_override,
                "items": [
                    {**defaults.get(item.get("skill_id"), {}), **item}
                    if isinstance(item, dict)
                    else item
                    for item in row.skills_override.get("items", [])
                ],
            }
        if isinstance(row.tools_override, dict):
            effective.tools = row.tools_override
        if isinstance(row.permissions_override, dict):
            effective.permissions = AgentPermissions.model_validate(row.permissions_override)
        if isinstance(row.agent_profiles_override, dict):
            effective.agent_profiles = {
                profile_id: AgentRuntimeProfile.model_validate(profile)
                for profile_id, profile in row.agent_profiles_override.items()
            }
        if row.default_agent_profile_id_override:
            effective.default_agent_profile_id = row.default_agent_profile_id_override
        return effective

    async def list_secondary_bindings(self, primary_agent_id: str) -> list[str]:
        """List secondary agent IDs bound to a primary agent."""
        from sqlalchemy import select

        from cognis.store.models import AgentSecondaryBinding

        async with self._session_factory() as db_session:
            result = await db_session.execute(
                select(AgentSecondaryBinding.secondary_agent_id).where(
                    AgentSecondaryBinding.primary_agent_id == primary_agent_id
                )
            )
            return [row[0] for row in result.all()]

    async def is_secondary_bound(self, primary_agent_id: str, secondary_agent_id: str) -> bool:
        """Check if a secondary agent is bound to a primary agent.

        System secondary agents (system:*) are implicitly available to
        all primary agents — no binding row needed.
        """
        if secondary_agent_id.startswith("system:"):
            return True

        from sqlalchemy import select

        from cognis.store.models import AgentSecondaryBinding

        async with self._session_factory() as db_session:
            result = await db_session.execute(
                select(AgentSecondaryBinding).where(
                    AgentSecondaryBinding.primary_agent_id == primary_agent_id,
                    AgentSecondaryBinding.secondary_agent_id == secondary_agent_id,
                )
            )
            return result.scalar_one_or_none() is not None


def _row_to_definition(row: Any) -> AgentDefinition:
    """Convert a DB Agent row to an AgentDefinition domain model."""
    from cognis.models.agent import (
        AgentCapabilities,
        AgentLLMConfig,
        AgentPermissions,
        AgentRuntimeProfile,
    )

    permissions = None
    if row.permissions:
        permissions = AgentPermissions.model_validate(row.permissions)

    llm_config = None
    if row.llm_config:
        llm_config = AgentLLMConfig.model_validate(row.llm_config)

    capabilities = AgentCapabilities()
    raw_capabilities = getattr(row, "capabilities", None)
    if isinstance(raw_capabilities, dict) and raw_capabilities:
        with contextlib.suppress(Exception):
            capabilities = AgentCapabilities.model_validate(raw_capabilities)

    agent_profiles: dict[str, AgentRuntimeProfile] = {}
    raw_profiles = getattr(row, "agent_profiles", None)
    if isinstance(raw_profiles, dict):
        agent_profiles = {
            profile_id: AgentRuntimeProfile.model_validate(profile)
            for profile_id, profile in raw_profiles.items()
            if isinstance(profile_id, str) and isinstance(profile, dict)
        }

    return AgentDefinition(
        agent_id=row.agent_id,
        owner_email=row.owner_email,
        name=row.name,
        display_name=getattr(row, "display_name", None),
        description=row.description,
        system_prompt=row.system_prompt,
        personality=row.personality,
        skills=row.skills,
        tools=row.tools,
        permissions=permissions,
        llm_config=llm_config,
        capabilities=capabilities,
        agent_profiles=agent_profiles,
        default_agent_profile_id=getattr(row, "default_agent_profile_id", None),
        execution=row.execution,
        avatar_url=row.avatar_url,
        avatar_image_id=getattr(row, "avatar_image_id", None),
        agent_type=getattr(row, "agent_type", "primary"),
        is_system=getattr(row, "is_system", False),
        hidden=getattr(row, "hidden", False),
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
