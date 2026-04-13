"""Agent registry — system agents and DB-backed user agents.

System agents are defined as Python constants and merged with DB agents
at query time. This follows the same pattern as WorkflowRegistry for
system workflows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.store.queries import get_agent, list_agents

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

- Search the web for current information on the given topic.
- Read and analyze multiple sources to build a comprehensive picture.
- Cross-reference findings across sources for accuracy.
- When researching technical topics, prefer official documentation and
  authoritative sources.
- If the research involves code, also search the local codebase for
  relevant context.
- Separate repo-local findings from external findings.
- Call out freshness, uncertainty, and missing evidence explicitly.

## Output

Return structured research findings:
- Key facts and findings (with source URLs)
- Areas of consensus and disagreement across sources
- Recommendations or conclusions based on the evidence
- Gaps in available information"""

_IMPLEMENT_PROMPT = """\
You are a focused implementation agent for software engineering tasks.

## Instructions

- Make the smallest correct change that solves the task.
- Prefer direct execution over extended discussion.
- Read enough context to act correctly, but avoid unnecessary exploration.
- Use the most direct tool for the operation.
- Use file and patch tools for content changes.
- Use shell commands for terminal-native operations and atomic filesystem
  operations such as `mv`, `cp`, `rm`, `mkdir`, `git`, and build/test
  commands.
- Do not emulate filesystem moves or copies by reading and rewriting file
  contents when a direct operation exists.
- When verification is feasible, run targeted checks relevant to the
  change.
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

- Review ONLY the modified code shown in the diff, NOT existing unchanged code
- Output ONLY the final review in the exact format specified below
- Do NOT write any files
- Do NOT execute any shell commands other than read-only, non-destructive
  git actions like git status, git diff, etc.
- Do NOT include thinking process, reasoning steps, or tool usage in output
- You can read files in the repository for further context (read-only)
- When referring to line numbers, provide actual file name and line number
  using full path from repository root (e.g., `src/myfile.py:123`)

## Review Requirements

Analyze the changes for:
- Code quality, readability, and maintainability
- Potential bugs, security issues, or performance problems
- Best practices and design patterns
- Test coverage and edge cases
- Documentation completeness

Adapt your review based on the project's nature and guidelines (AGENTS.md \
or similar project conventions).

## Output Format (MUST follow exactly)

### Summary
[2-3 sentence overview of the changes and overall assessment]

---

### Strengths
- [Specific positive aspect with file/line reference if applicable]

---

### Issues Found

#### CRITICAL
- [Security vulnerabilities, data loss risks, breaking changes]

#### MAJOR
- [Bugs, logic errors, significant performance issues]

#### MINOR
- [Style issues, minor optimizations, suggestions]

---

### Recommendations
- [Actionable improvement with specific guidance]

---

## SCORE: [number]/100

## Scoring Guidelines

- 90-100: Excellent quality, minimal issues
- 80-89: Good quality, some minor improvements needed
- 70-79: Acceptable quality, several issues to address
- 60-69: Below standard, significant improvements required
- 0-59: Poor quality, major problems present

If no issues exist in a severity category, write "None identified".
Include file names and line numbers when referencing issues.
The SCORE line MUST always be present."""

_ARCHITECT_PROMPT = """\
You are an expert Software Architect acting as an Architecture Review \
Board (ARB) reviewer.

## Mission

1. Verify the architecture correlates with the stated intentions (goals, \
constraints, non-goals).
2. Identify weaknesses, missing requirements, risky assumptions, and \
likely failure modes.
3. Propose pragmatic improvements and alternatives with clear trade-offs.
4. Produce an actionable review that can be used to revise the plan.

## Critical Instructions

- Review the architectural plan provided (and only the referenced context).
- Do NOT invent requirements; if information is missing, explicitly call \
it out.
- Be constructive but rigorous. Do not rubber-stamp.
- Prefer specific, testable statements over vague advice.
- If a decision depends on unknowns: provide conditional guidance \
("If X, do Y; otherwise do Z").
- If you propose patterns, justify them against the stated goals and list \
operational costs.

## Review Checklist (follow in order)

A) Extract Intent & Constraints
   - Goals, Non-Goals, Constraints, Assumptions
   - NFRs: availability/SLO, latency, throughput, consistency, RPO/RTO, \
compliance, data retention, privacy
   - If key items are missing, list "Blocking Questions" (max 5)

B) Architecture Summary (neutral)
   - Components & responsibilities, interfaces/APIs, data stores, key flows
   - Deployment/runtime topology, scaling model, integration points

C) Intent <-> Decision Traceability
   - Table: Intent/Constraint -> Decision -> Evidence -> Impact -> Gap/Risk

D) Quality Attributes Review
   - Reliability/Resilience, Scalability/Performance, Security/Privacy, \
Maintainability/Evolvability, Operability/Observability, Cost Efficiency, \
Data Integrity

E) Risk Register
   - Prefer >=8 risks when possible.
   - Each: Risk, Likelihood, Impact, Detection signal, Mitigation, \
Residual risk

F) Recommendations
   - (1) Minimal-change improvements
   - (2) Bolder alternative architecture
   - For each: benefit, downside, migration approach, what to measure

G) Verdict
   - APPROVE / APPROVE WITH CHANGES / REQUEST REWORK
   - Concrete next actions + acceptance criteria

## Output Format (MUST follow exactly)

### Summary
[2-4 sentence overview of the architecture and overall assessment, \
explicitly stating whether it matches the intentions]

---

### Strengths
- [Specific strength tied to a goal/NFR]

---

### Issues Found

#### CRITICAL
- [High-impact misalignments, security/compliance gaps, data loss risks, \
unrecoverable failure modes]

#### MAJOR
- [Significant scalability/reliability/operability issues, unclear \
boundaries, weak data model, risky coupling]

#### MINOR
- [Clarity, documentation, naming, small optimizations, optional \
enhancements]

---

### Intent <-> Decision Traceability

| Intent / Constraint | Architectural Decision | Evidence in Plan | Impact | Gap / Risk |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

---

### Quality Attributes (1-5)
- Reliability/Resilience: [1-5] - [one-line justification]
- Scalability/Performance: [1-5] - [...]
- Security/Privacy: [1-5] - [...]
- Maintainability/Evolvability: [1-5] - [...]
- Operability/Observability: [1-5] - [...]
- Cost Efficiency: [1-5] - [...]
- Data Integrity: [1-5] - [...]

---

### Recommendations

#### Minimal-change
- [Actionable change + why + trade-off + measurement]

#### Bolder alternative
- [Alternative + when to choose it + migration notes]

---

### Verdict
**[APPROVE / APPROVE WITH CHANGES / REQUEST REWORK]**
- Next actions:
  - [ ]
- Acceptance criteria:
  - [ ]

---

## SCORE: [number]/100

## Scoring Rubric

Start from 100 and subtract penalties. Do not exceed 100.

1. Intent correlation (0-30)
   - Missing/unclear goals/non-goals/constraints: -5 to -15
   - Key decisions not traceable to intent: -5 to -20

2. Quality attributes & NFRs (0-30)
   - Missing explicit SLO/latency/throughput targets: -5 to -15
   - Missing RPO/RTO, backup/restore, DR: -5 to -15
   - Weak security/privacy posture: -5 to -20

3. Operability & delivery feasibility (0-20)
   - Missing observability: -5 to -15
   - Migration/rollout plan missing: -5 to -15
   - Unrealistic complexity: -5 to -15

4. Data & integration correctness (0-20)
   - Unclear data ownership, consistency, schema evolution: -5 to -15
   - Risky coupling, no failure handling: -5 to -15

Automatic floor rules:
- Any CRITICAL issues unmitigated: score <= 69
- Security/compliance critical gap: score <= 59
- Blocking Questions prevent validation: score <= 79"""

_COMMITTER_PROMPT = """\
You are an expert in working with Git and creating meaningful Git commit \
messages using Conventional Commits v1.0.0.

## Instructions

- Input is git diff of changes to be committed
- Understand the changes and output a commit message following conventions
- Stage all tracked changed files with git add -u
- Also explicitly git add any newly created files
- Create the commit with the generated message
- NEVER push. Do not run git push under any circumstances
- Use only plaintext and ASCII characters, no fancy visuals
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
Summarize the older conversation history into a structured handoff \
document for the same assistant to continue from. Use exactly these \
sections (omit empty sections):

## Goal - What the user is trying to accomplish.
## Key Instructions - Constraints, preferences, and rules the user stated.
## Discoveries - Important findings, decisions, or conclusions reached.
## Relevant Files - Files read, created, or modified (with paths).
## Accomplished - Completed actions and their outcomes.
## Current Work - What was in progress when this history ended.

Be concise. Do not invent information not present in the history. \
Prefer bullet points over prose."""

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
) -> AgentDefinition:
    """Create a system agent definition."""
    return AgentDefinition(
        agent_id=agent_id,
        owner_email=_SYSTEM_OWNER,
        name=name,
        description=description,
        system_prompt=system_prompt,
        tools=tools,
        agent_type="secondary",
        is_system=True,
        hidden=hidden,
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
            tools={"builtin_tools": ["read", "grep", "glob", "list", "bash"]},
        ),
        _system_agent(
            "system:research",
            "Research",
            "Web research and information gathering",
            _RESEARCH_PROMPT,
            tools={"builtin_tools": ["read", "grep", "glob", "web_search", "web_fetch"]},
        ),
        _system_agent(
            "system:code-review",
            "Code Review",
            "Code review with structured scoring",
            _CODE_REVIEW_PROMPT,
            tools={"builtin_tools": ["read", "grep", "glob", "bash"]},
        ),
        _system_agent(
            "system:architect",
            "Architect",
            "Architecture Review Board (ARB) reviewer",
            _ARCHITECT_PROMPT,
            tools={"builtin_tools": ["read", "grep", "glob", "bash"]},
        ),
        _system_agent(
            "system:implement",
            "Implement",
            "Focused implementation and targeted verification",
            _IMPLEMENT_PROMPT,
            tools={
                "builtin_tools": [
                    "read",
                    "write",
                    "edit",
                    "multiedit",
                    "patch",
                    "grep",
                    "glob",
                    "list",
                    "bash",
                ]
            },
        ),
        _system_agent(
            "system:committer",
            "Committer",
            "Git commit message generation and commit creation",
            _COMMITTER_PROMPT,
            tools={"builtin_tools": ["read", "bash"]},
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

    async def get(self, agent_id: str) -> AgentDefinition | None:
        """Resolve agent — checks system agents first, then DB."""
        if agent_id in SYSTEM_AGENTS:
            return SYSTEM_AGENTS[agent_id]

        async with self._session_factory() as db_session:
            row = await get_agent(db_session, agent_id)
        if row is None:
            return None
        return _row_to_definition(row)

    def get_system_agent(self, agent_id: str) -> AgentDefinition | None:
        """Get a system agent by ID (for internal use). No DB query."""
        return SYSTEM_AGENTS.get(agent_id)

    async def list_all(
        self,
        *,
        owner_email: str | None = None,
        agent_type: str | None = None,
        include_hidden: bool = False,
        include_system: bool = True,
    ) -> list[AgentDefinition]:
        """List all available agents (system + user)."""
        result: list[AgentDefinition] = []

        if include_system:
            for agent in SYSTEM_AGENTS.values():
                if not include_hidden and agent.hidden:
                    continue
                if agent_type is not None and agent.agent_type != agent_type:
                    continue
                result.append(agent)

        async with self._session_factory() as db_session:
            rows = await list_agents(db_session, owner_email=owner_email)
        for row in rows:
            defn = _row_to_definition(row)
            if not include_hidden and defn.hidden:
                continue
            if agent_type is not None and defn.agent_type != agent_type:
                continue
            result.append(defn)

        return result

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
    from cognis.models.agent import AgentLLMConfig, AgentPermissions

    permissions = None
    if row.permissions:
        permissions = AgentPermissions.model_validate(row.permissions)

    llm_config = None
    if row.llm_config:
        llm_config = AgentLLMConfig.model_validate(row.llm_config)

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
