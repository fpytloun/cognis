"""Agent registry — system agents and DB-backed user agents.

System agents are defined as Python constants and merged with DB agents
at query time. This follows the same pattern as WorkflowRegistry for
system workflows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition, AgentLLMConfig, AgentPermissions
from cognis.ownership import normalize_executor_scope
from cognis.store.queries import (
    get_agent,
    get_system_agent_override,
    list_agents,
    list_visible_agents,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# System agent prompts
# ---------------------------------------------------------------------------

_EXPLORE_PROMPT = """\
You are a fast, read-only agent specialized for exploring codebases.

## Instructions

- You CANNOT modify files. You have read-only access.
- Find files by patterns, search code for keywords, and answer questions
  about the codebase structure and implementation.
- Start broad with pattern search, then narrow to specific files.
- Prefer breadth-first exploration before deep dives.
- Avoid reading large files end-to-end when targeted reads will answer the
  question.
- Be thorough in your exploration — check multiple locations, naming
  conventions, and related files.
- Report findings concisely with file paths and line numbers.
- If the answer is incomplete, say so and identify the next most relevant
  files or symbols to inspect.
- If you find something unexpected or noteworthy, mention it.

## Output

Return a structured summary of your findings:
- What you found (with file:line references)
- Key patterns or conventions observed
- Anything notable or unexpected"""

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
- When useful, collect or reference relevant media, diagrams, screenshots,
  tables, PDFs, or other artifacts. Include artifact IDs or source URLs so the
  caller can inspect them. Use simple markdown or Mermaid diagrams when they
  clarify relationships, timelines, architectures, or comparisons.
- Do not present speculation as fact.

## Output

Return structured research findings:
- Repo-local context (if relevant)
- External findings (with source URLs and dates when available)
- Areas of consensus and disagreement across sources
- Relevant media, diagrams, or artifacts when useful
- Recommendations or conclusions based on the evidence
- Gaps, uncertainty, or stale information"""

_IMPLEMENT_PROMPT = """\
You are a focused implementation agent for software engineering tasks.

## Instructions

- Make the smallest correct change that solves the task.
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
- Do not delegate further. If the task would be better handled as broader
  background work, return that recommendation to the caller instead.

## Output

Return:
- What changed
- Files modified
- Verification performed
- Any remaining risks or follow-ups"""

_CODE_REVIEW_PROMPT = """\
You are an expert code reviewer. Your task is to review ONLY the changes \
made. You are most likely being executed inside a git repository so you \
can use git read-only commands to examine status and obtain diffs.

## Critical Instructions

- Review ONLY the modified code shown in the diff, NOT existing unchanged code.
- Output ONLY the final review in the exact format specified below.
- Do NOT write any files.
- Do NOT execute any shell commands other than read-only, non-destructive
  git actions like `git status`, `git diff`, `git show`, and `git log`.
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
- If you are unsure whether something is a real issue, investigate further or
  say that you are unsure instead of flagging it as definite.

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
for real must-fix issues."""

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
- For small, localized, low-risk changes, prefer APPROVE or APPROVE WITH CHANGES
  when implementation can proceed safely.
- Use REQUEST REWORK when missing or incorrect decisions would likely cause the
  implementation to be wrong, unsafe, unreliable, untestable, or materially blocked.
- Do not demand enterprise-style artifacts for focused feature work.
- Do not require observability, migration, rollback, scalability, or resilience
  analysis unless the change clearly touches those concerns.
- If the plan is overengineered for the problem, say so explicitly.
- Prefer specific, actionable comments over broad design commentary.

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
  - [only if needed]"""

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
- [single-sentence task summary, or "(none)"]

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

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]

## Recoverable Tool Outputs
- [call_id and recovery hint, or "(none)"]

Rules:
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, call_ids, and identifiers when known.
- Do not mention the summary process or that context was compacted.
- Do not invent information not present in the history."""

_CLASSIFIER_PROMPT = """\
Select the best workflow for the given task. You MUST respond with a \
single JSON object and nothing else. No markdown, no explanation, no \
text before or after the JSON.

Example: {"workflow_id": "...", "confidence": 0.8, "reason": "..."}"""

_EVALUATOR_PROMPT = """\
You are a workflow step evaluator. Assess whether the agent's work \
satisfies the step's objective.

Be skeptical. Agents tend to declare victory prematurely. Apply these \
checks:

1. Compare each claim against the actual response content. If a claim \
   says "implemented X" but the response shows no evidence of X, reject.
2. Check completeness: does the response address ALL parts of the step \
   objective? Partial completion is a revise, not an approval.
3. Check quality: are there obvious errors, missing error handling, or \
   incomplete implementations? If the step says "with tests" and there \
   are no tests, that is a revise.
4. Use "failed" only when the step fundamentally cannot succeed (wrong \
   approach, impossible constraint, repeated identical failures).

Respond with a single JSON object:
{
  "decision": "approved" | "revise" | "failed",
  "reasoning": "...",
  "feedback": "..."
}

The "feedback" field is shown to the agent on revise — make it specific \
and actionable so the agent knows exactly what to fix."""

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
            "Fast read-only codebase exploration",
            _EXPLORE_PROMPT,
            tools={"builtin_tools": ["read", "grep", "glob", "list_directory", "bash"]},
            reasoning_effort="low",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:research",
            "Research",
            "Web and repo research with multi-source synthesis",
            _RESEARCH_PROMPT,
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
            skills={"items": [{"skill_id": "cognis-coding", "enabled": True}]},
            tools={"builtin_tools": ["read", "grep", "glob", "bash"]},
            reasoning_effort="medium",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:architect",
            "Architect",
            "Implementation plan review for architecture and risk",
            _ARCHITECT_PROMPT,
            tools={"builtin_tools": ["read", "grep", "glob", "bash"]},
            reasoning_effort="medium",
            allow_user_override=True,
            allow_user_disable=True,
        ),
        _system_agent(
            "system:implement",
            "Implement",
            "Focused implementation and targeted verification",
            _IMPLEMENT_PROMPT,
            skills={"items": [{"skill_id": "cognis-coding", "enabled": True}]},
            tools={
                "builtin_tools": [
                    "read",
                    "write",
                    "edit",
                    "multiedit",
                    "apply_patch",
                    "grep",
                    "glob",
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
            "system:classifier",
            "Classifier",
            "Workflow selection for background tasks",
            _CLASSIFIER_PROMPT,
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
    ) -> list[AgentDefinition]:
        """List all available agents (system + user)."""
        result: list[AgentDefinition] = []

        if include_system:
            for agent in SYSTEM_AGENTS.values():
                effective = await self._resolve_system_agent(
                    agent,
                    owner_email=owner_email,
                    include_disabled=include_disabled,
                )
                if effective is None:
                    continue
                if not include_hidden and effective.hidden:
                    continue
                if agent_type is not None and effective.agent_type != agent_type:
                    continue
                result.append(effective)

        async with self._session_factory() as db_session:
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

        return result

    async def _resolve_system_agent(
        self,
        base: AgentDefinition,
        *,
        owner_email: str | None,
        include_disabled: bool,
    ) -> AgentDefinition | None:
        effective = base.model_copy(deep=True)
        if not owner_email or not base.allow_user_override:
            return effective
        async with self._session_factory() as db_session:
            row = await get_system_agent_override(
                db_session, owner_email=owner_email, agent_id=base.agent_id
            )
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
            effective.skills = row.skills_override
        if isinstance(row.tools_override, dict):
            effective.tools = row.tools_override
        if isinstance(row.permissions_override, dict):
            effective.permissions = AgentPermissions.model_validate(row.permissions_override)
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
    from cognis.models.agent import AgentLLMConfig, AgentPermissions, AgentRuntimeProfile

    permissions = None
    if row.permissions:
        permissions = AgentPermissions.model_validate(row.permissions)

    llm_config = None
    if row.llm_config:
        llm_config = AgentLLMConfig.model_validate(row.llm_config)

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
