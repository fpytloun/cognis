"""Built-in workflow tool definitions.

These tools are controller-injected into the LLM prompt by the agent loop.
They are NOT dispatched through the executor or tool router. The definitions
here exist solely for visibility on the Tools page and tool registry.
"""

from __future__ import annotations

import copy

from cognis.models.credential import SUPPORTED_CREDENTIAL_KINDS
from cognis.models.deliverable import (
    CANONICAL_CHART_BLOCK_SCHEMA,
    PULSE_DAILY_SKELETON,
    PULSE_PRESENTATION_DESCRIPTOR,
    PULSE_WRITE_DELIVERABLE_SCHEMA,
    SUPPORTED_RICH_BLOCK_TYPES,
)
from cognis.models.tool import (
    NativeToolDefinition,
    NativeToolOperation,
    ToolMutationKind,
    ToolSource,
    declared_default_semantics,
)
from cognis.models.tool import (
    ToolDefinition as BaseToolDefinition,
)

ToolDefinition = NativeToolDefinition

_SOURCE = ToolSource(type="builtin")

STEP_COMPLETE_TOOL = ToolDefinition(
    name="step_complete",
    description=(
        "Signal that this workflow step is complete. "
        "Call this when the step objective is satisfied."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": "Brief summary of what was accomplished in this step.",
            },
            "outputs": {
                "type": "object",
                "description": "Structured outputs from this step (key-value pairs).",
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Workflow-step-specific structured metadata. When the current step defines "
                    "a metadata contract, all required fields must be present and must match "
                    "the declared JSON types."
                ),
            },
            "claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Claims about what was achieved, for evaluation.",
            },
            "outcome": {
                "type": "object",
                "description": (
                    "Optional business outcome for this step. Use status 'rejected' when the "
                    "step completed properly but the reviewed work should go back for revision, "
                    "or 'failed' when the step itself could not complete successfully."
                ),
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["success", "rejected", "failed"],
                        "description": "Outcome status for workflow routing.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Required for rejected or failed outcomes.",
                    },
                },
                "required": ["status"],
            },
            "notification": {
                "type": "object",
                "description": (
                    "Optional completion delivery choice. Use 'silent' only when silent completion "
                    "is explicitly allowed and nothing user-actionable happened. Use 'direct' for "
                    "ready-to-read outputs like daily briefs or summaries when the result should be "
                    "sent directly to the resolved target channel."
                ),
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["silent", "direct"],
                        "description": (
                            "Request silent completion with no outward notification, or direct "
                            "delivery to the resolved target channel."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Required for silent completion. Optional for direct.",
                    },
                },
                "required": ["mode"],
            },
        },
        "required": ["summary"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

_RICH_MEDIA_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Authorized saved Cognis artifact-compatible image ref. artifact_id and "
                "content_ref are accepted aliases."
            ),
        },
        "artifact_id": {"type": "string", "minLength": 1},
        "content_ref": {"type": "string", "minLength": 1},
        "alt": {"type": "string"},
        "credit": {"type": "string"},
        "source_url": {"type": "string"},
        "role": {"type": "string"},
        "aspect_ratio": {"type": "string"},
        "focal_point": {
            "oneOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "minimum": 0, "maximum": 1},
                        "y": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "anyOf": [
        {"required": ["ref"]},
        {"required": ["artifact_id"]},
        {"required": ["content_ref"]},
    ],
    "additionalProperties": False,
}

_GENERIC_RICH_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": sorted(SUPPORTED_RICH_BLOCK_TYPES)},
        "variant": {"type": "string"},
        "dek": {"type": "string"},
        "summary": {"type": "string"},
        "href": {"type": "string"},
        "content": {"type": "string"},
        "sources": {
            "oneOf": [
                {"type": "string", "minLength": 1},
                {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string", "minLength": 1},
                            {"type": "object"},
                        ]
                    },
                },
            ],
            "description": (
                "Optional source records or document-level source IDs. For source_list, "
                "omit this field to render every document-level source."
            ),
        },
        "source_ids": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string", "minLength": 1}},
            ]
        },
        "citations": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string", "minLength": 1}},
            ]
        },
        "icon": {"type": ["string", "number", "integer", "boolean", "null"]},
        "tone": {"type": "string"},
        "media": _RICH_MEDIA_INPUT_SCHEMA,
        "blocks": {
            "type": "array",
            "items": {"$ref": "#/definitions/genericRichBlock"},
        },
        "children": {
            "type": "array",
            "items": {"$ref": "#/definitions/genericRichBlock"},
        },
    },
    "required": ["type"],
    "if": {"properties": {"type": {"const": "chart"}}, "required": ["type"]},
    "then": CANONICAL_CHART_BLOCK_SCHEMA,
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "markdown"}}, "required": ["type"]},
            "then": {
                "properties": {"content": {"type": "string", "minLength": 1, "pattern": "\\S"}},
                "required": ["content"],
            },
        },
        {
            "if": {"properties": {"type": {"const": "mermaid"}}, "required": ["type"]},
            "then": {
                "properties": {
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "\\S",
                        "description": "Canonical Mermaid diagram source.",
                    },
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "\\S",
                        "description": "Accepted alias for Mermaid source; prefer source.",
                    },
                    "content": {"type": "string", "minLength": 1, "pattern": "\\S"},
                },
                "anyOf": [
                    {"required": ["source"]},
                    {"required": ["code"]},
                    {"required": ["content"]},
                ],
            },
        },
        {
            "if": {"properties": {"type": {"enum": ["accordion", "gallery", "modal", "tabs"]}}},
            "then": {
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"$ref": "#/definitions/genericRichBlock"},
                                {
                                    "type": "object",
                                    "not": {"required": ["type"]},
                                },
                            ]
                        },
                    }
                }
            },
        },
    ],
    "additionalProperties": True,
}

_WRITE_DELIVERABLE_SCHEMA = {
    "type": "object",
    "definitions": {"genericRichBlock": _GENERIC_RICH_BLOCK_SCHEMA},
    "properties": {
        "action": {
            "type": "string",
            "const": "write_deliverable",
            "description": "Select the generic write_deliverable operation.",
        },
        "content": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Required fallback deliverable content. In workflow/task steps "
                "this is the durable step output fallback; in direct chat it is "
                "the fallback for a shareable artifact. Do not use for "
                "intermediate progress or unvalidated drafts."
            ),
        },
        "format": {
            "type": "string",
            "enum": ["markdown", "plain", "html", "rich"],
            "description": "How the deliverable should be rendered.",
            "default": "markdown",
        },
        "rich": {
            "type": "object",
            "description": (
                "Renderer-neutral Rich Deliverables v2 payload for format='rich'. "
                "Canonical shape is block-composed with blocks, assets, sources, "
                "datasets, exports, and metadata. content remains required fallback."
            ),
            "properties": {
                "blocks": {
                    "type": "array",
                    "items": _GENERIC_RICH_BLOCK_SCHEMA,
                    "description": (
                        "Canonical blocks. card/metric/status/action and all existing blocks "
                        "accept optional variant, dek/summary, href, source_ids/citations, "
                        "scalar icon, tone, and authorized media."
                    ),
                },
                "metadata": {
                    "type": "object",
                    "properties": {
                        "presentation": {
                            "not": {"const": "pulse"},
                            "description": (
                                "Pulse payloads must use action='rich:pulse'; omit this "
                                "field for explicit generic rich fallback."
                            ),
                        }
                    },
                },
            },
            "required": ["blocks"],
        },
        "title": {
            "type": "string",
            "description": "Optional title for the deliverable.",
        },
        "target": {
            "type": "string",
            "enum": ["channel", "none"],
            "description": (
                "Optional delivery hint. Final workflow policy decides what actually "
                "gets delivered."
            ),
        },
        "outputs": {
            "type": "object",
            "description": "Optional structured sidecar data for evaluators or later steps.",
        },
    },
    "required": ["action", "content"],
}

_PULSE_WRITE_DELIVERABLE_SCHEMA = copy.deepcopy(PULSE_WRITE_DELIVERABLE_SCHEMA)
_PULSE_WRITE_DELIVERABLE_SCHEMA.setdefault("properties", {})["action"] = {
    "type": "string",
    "const": "rich:pulse",
    "description": "Select the validated Pulse authoring operation.",
}
_PULSE_WRITE_DELIVERABLE_SCHEMA["required"] = [
    "action",
    *[field for field in _PULSE_WRITE_DELIVERABLE_SCHEMA.get("required", []) if field != "action"],
]

_WRITE_DELIVERABLE_DESCRIPTION = (
    "Write a durable deliverable after the work is complete. In workflow/task "
    "steps, preserve the workflow contract: the deliverable is the step's "
    "durable output boundary and may be evaluator/reviewer input, downstream "
    "step input, a handoff artifact, or final user delivery. If the step "
    "expects a deliverable, write it after validation/review and before "
    "step_complete. Workflow/task deliverables remain scoped to that task. "
    "In direct chat, the deliverable is published for the owner, so other "
    "conversations owned by the same user can search and use it. Use this only "
    "when the turn should produce "
    "a durable/rendered/shareable artifact such as a report, spec, dashboard, "
    "or rich document; do not use it for normal answers, intermediate progress, "
    "drafts, notes, or status updates. The content argument is always the "
    "required fallback artifact for model-visible summaries, channels, "
    "compaction, notifications, and accessibility.\n\n"
    "Decision tree before authoring: (1) Is this a normal conversational answer, "
    "a status update, or intermediate progress? Answer inline in the assistant "
    "message; do not call this tool. (2) Does the reader only need prose "
    "(an explanation, a short writeup, a plain list)? Use format='markdown'; do "
    "not force it into blocks. (3) Does the reader benefit from structure a "
    "human designer would choose deliberately — comparison, dashboard, "
    "narrative-with-evidence, timeline, reference doc, or visual monitoring? "
    "Use format='rich' and compose from the generic block vocabulary (see the "
    "block type enum and, for detailed composition guidance and archetype "
    "recipes, call describe_tool for this tool or load the "
    "cognis-rich-deliverable skill). (4) Is the artifact specifically a daily "
    "Pulse presentation? Use the registered rich:pulse operation instead of "
    "generic rich.\n\n"
    "Compose for a reader, not a form: pick blocks the way a human editor would "
    "— use dashboard/metric/card_grid for at-a-glance status, comparison_matrix "
    "or decision_matrix for options being weighed, research_answer or "
    "evidence_report/claim_cards for narrative claims backed by sources, "
    "timeline/steps for sequences, chart only for genuinely multi-point "
    "quantitative series (never a 1-2 point or purely categorical fact), and "
    "callout/quote sparingly for one true highlight, not for every fact. Do not "
    "wrap plain prose in card/status/metric blocks just to make the payload "
    "look 'rich'; a markdown or section block with real paragraphs is often the "
    "correct choice for a reflective or narrative body. Avoid nesting cards "
    "inside cards, avoid a wall of same-weight tiles with no hierarchy, and "
    "give every deliverable one clear focal point."
)

_GENERIC_RICH_COMPOSITION_GUIDE: dict[str, object] = {
    "principle": (
        "Generic rich deliverables are use-case-neutral: there is no preset for "
        "most archetypes. Compose the block vocabulary the way a human editor "
        "or designer would for that specific reader and content, not by "
        "reflexively wrapping every fact in a card. One clear focal point per "
        "deliverable; hierarchy over uniform tiles; prose stays prose."
    ),
    "block_families": {
        "status_at_a_glance": {
            "blocks": ["dashboard", "metric", "status", "status_grid", "card_grid"],
            "use_for": "Numeric or state summaries meant to be scanned in seconds.",
            "avoid": "Do not use for narrative content or a single number with no comparison.",
        },
        "narrative_with_evidence": {
            "blocks": ["research_answer", "evidence_report", "claim_cards", "quote"],
            "use_for": (
                "Answers, findings, or claims that must show supporting sources; "
                "research_answer for a direct answer with key points and "
                "citations, evidence_report/claim_cards for multiple weighed "
                "claims each with its own evidence and confidence."
            ),
        },
        "comparison_and_decision": {
            "blocks": ["comparison_matrix", "decision_matrix", "table"],
            "use_for": "Options being weighed against each other on shared criteria.",
        },
        "sequence_and_process": {
            "blocks": ["timeline", "steps", "day_agenda", "incident_timeline", "checklist"],
            "use_for": "Anything with an inherent order: events, procedures, agendas, incident chronology.",
        },
        "prose_and_structure": {
            "blocks": ["markdown", "section", "stack", "columns", "grid", "hero"],
            "use_for": (
                "Real paragraphs, long-form reading, and layout grouping. Prefer "
                "markdown/section over card for reflective or explanatory prose. "
                "hero accepts an optional media reference (the same media object "
                "shape as figure/card, authorized via ref/artifact_id/content_ref) "
                "as a full-bleed lead banner behind the title/subtitle -- use this "
                "when the deliverable already has a generated banner/cover image "
                "(e.g. for a published article or newsroom piece) so the report "
                "opens with that image instead of a plain gradient hero."
            ),
        },
        "visual_evidence": {
            "blocks": ["chart", "figure", "gallery", "mermaid"],
            "use_for": (
                "chart only for genuinely multi-point quantitative series with a "
                "source and timestamp; figure/gallery for images with alt text "
                "and provenance; mermaid for diagrams."
            ),
        },
        "reference_and_code": {
            "blocks": ["code", "kv", "key_value", "source_list", "link", "link_preview"],
            "use_for": "Code samples, key-value reference data, and source attribution.",
        },
        "emphasis_sparingly": {
            "blocks": ["callout", "divider", "action"],
            "use_for": (
                "callout for exactly one true highlight per deliverable, not "
                "every fact; action for a single explicit next step, not a menu "
                "of buttons; divider to separate real sections, not decoration."
            ),
        },
        "containers": {
            "blocks": ["tabs", "accordion", "modal"],
            "use_for": "Progressive disclosure when there is genuinely more than one story/detail to browse.",
        },
    },
    "archetype_recipes": {
        "rca_or_incident_dashboard": (
            "hero/title -> dashboard or status_grid for current impact -> "
            "incident_timeline for chronology -> evidence_report or "
            "research_answer for root cause -> table for affected systems -> "
            "checklist for remediation actions."
        ),
        "research_answer_or_deep_dive": (
            "research_answer for the direct answer with key_points and "
            "citations -> evidence_report/claim_cards for supporting claims -> "
            "comparison_matrix if alternatives were weighed -> source_list."
        ),
        "newsletter_or_digest": (
            "hero -> card_grid or accordion of story cards (each cited) -> "
            "closing callout -> source_list. Use progressive disclosure "
            "(accordion/tabs) once there is more than a few stories."
        ),
        "product_or_option_comparison": (
            "hero/markdown framing the decision -> a cited comparison_matrix "
            "or decision_matrix as the centerpiece, with exactly one "
            "recommended: true row when recommending an option -> callout "
            "for the recommendation -> research_answer for reasoning. When "
            "individual product imagery or detail is useful, follow the "
            "matrix immediately with a card_grid containing one card per "
            "compared product; repeat the exact product name from its matrix "
            "row, include verified media and source links, and summarize only "
            "product-specific trade-offs. Do not emit a detached image-only "
            "gallery that forces the reader to map pictures back to rows."
        ),
        "scientific_or_technical_report": (
            "markdown/section for abstract and prose -> figure for diagrams "
            "with captions -> table for data -> evidence_report for claims -> "
            "source_list for references. Favor real paragraphs over cards."
        ),
        "architecture_or_design_deck": (
            "hero -> section per concern with markdown prose -> mermaid or "
            "figure per diagram -> table for tradeoffs -> decision_matrix if "
            "choosing between designs."
        ),
        "notes_or_freeform_visualization": (
            "Let the content shape the layout: markdown/section for prose, "
            "timeline/steps only if there is a real sequence, metric/dashboard "
            "only if there are real numbers to scan. Do not force structure "
            "that is not in the content."
        ),
        "daily_pulse_or_briefing": (
            "Use the registered rich:pulse operation instead of generic rich "
            "for this specific archetype; it is an optional preset, not a "
            "quality requirement for anything else."
        ),
    },
    "anti_patterns": [
        "widget_salad: many small unrelated card/metric/status tiles with no "
        "hierarchy or grouping, forcing the reader to scan everything equally.",
        "nested_cards: a card block containing another card block for no "
        "structural reason; prefer a single card or a section/grid of siblings.",
        "two_point_chart: a chart block with only one or two data points or a "
        "single category; use metric, status, or a sentence instead.",
        "thesis_as_status_pill: compressing a substantive claim or finding "
        "into a status/metric label instead of a paragraph or research_answer.",
        "everything_is_a_card: wrapping plain narrative prose in card/callout "
        "blocks purely to look 'rich'; use markdown/section for prose.",
        "chart_without_provenance: a chart with no source or observed_at.",
    ],
}

WRITE_DELIVERABLE_TOOL = BaseToolDefinition(
    name="write_deliverable",
    description=_WRITE_DELIVERABLE_DESCRIPTION,
    parameters={},
    source=_SOURCE,
    category="deliverable",
    read_only=False,
    descriptor_extensions={
        "presentation_contracts": {"rich:pulse": PULSE_PRESENTATION_DESCRIPTOR},
        "rich_media_contract": {
            "input": _RICH_MEDIA_INPUT_SCHEMA,
            "persisted_reference": {"type": "object", "required": ["key"]},
            "serving": (
                "Media is authorized and validated before persistence, stored by "
                "deliverable-local key without signed URLs, and served only through "
                "authenticated or same-token deliverable media endpoints."
            ),
        },
        "composition_guide": _GENERIC_RICH_COMPOSITION_GUIDE,
    },
    native_operations=[
        NativeToolOperation(
            operation="write_deliverable",
            summary="Write a generic durable deliverable.",
            mutation_kind=ToolMutationKind.CREATE,
            input_schema=_WRITE_DELIVERABLE_SCHEMA,
            semantics=declared_default_semantics(ToolMutationKind.CREATE),
            examples=[
                {
                    "action": "write_deliverable",
                    "content": "## Summary\nFallback text",
                    "format": "rich",
                    "rich": {
                        "blocks": [
                            {
                                "type": "chart",
                                "title": "Request trend",
                                "description": "Recent request volume.",
                                "spec_version": "cognis.chart.v1",
                                "chart_type": "line",
                                "series": [
                                    {
                                        "id": "requests",
                                        "label": "Requests",
                                        "stack": "traffic",
                                        "points": [
                                            {"x": "T-1", "y": 12},
                                            {"x": "Now", "y": 18},
                                        ],
                                    }
                                ],
                                "x_axis": {"type": "category", "label": "Window"},
                                "y_axis": {
                                    "type": "linear",
                                    "label": "Requests",
                                    "unit": "req/s",
                                },
                                "stack": False,
                                "legend_position": "bottom",
                                "palette_token": "cool",
                                "source_ids": ["metrics"],
                                "source": "Metrics",
                                "source_url": "https://metrics.example.org/requests",
                                "observed_at": "2026-01-01T08:00:00+00:00",
                            }
                        ],
                        "assets": [],
                        "sources": [],
                        "datasets": [],
                        "exports": [],
                        "metadata": {},
                    },
                },
                {
                    "action": "write_deliverable",
                    "content": (
                        "## Should we migrate to arm64 Bottlerocket?\nYes, based on cost "
                        "and operational fit. See attached comparison."
                    ),
                    "format": "rich",
                    "rich": {
                        "blocks": [
                            {
                                "type": "research_answer",
                                "title": "Should we migrate to arm64 Bottlerocket?",
                                "answer": "Yes: it lowers node cost and matches our container images.",
                                "key_points": [
                                    "~20% lower per-node cost at equivalent throughput",
                                    "Bottlerocket's minimal OS reduces patch surface",
                                ],
                                "source_ids": ["cost-model"],
                            },
                            {
                                "type": "comparison_matrix",
                                "title": "Node option comparison",
                                "columns": [
                                    {"key": "option", "label": "Option"},
                                    {"key": "cost", "label": "Relative cost"},
                                    {"key": "ops_effort", "label": "Ops effort"},
                                ],
                                "rows": [
                                    {
                                        "option": "x86_64 Amazon Linux 2",
                                        "cost": "Baseline",
                                        "ops_effort": "Low (status quo)",
                                    },
                                    {
                                        "option": "arm64 Bottlerocket",
                                        "cost": "-20%",
                                        "ops_effort": "Medium (one-time migration)",
                                    },
                                ],
                            },
                            {
                                "type": "callout",
                                "tone": "positive",
                                "content": "Recommendation: migrate non-latency-critical workloads first.",
                            },
                        ],
                        "assets": [],
                        "sources": [{"id": "cost-model", "title": "Internal cost model"}],
                        "datasets": [],
                        "exports": [],
                        "metadata": {},
                    },
                },
            ],
            side_effects=["Persists a durable deliverable after validation succeeds."],
            validator_ids=["write_deliverable.rich"],
        ),
        NativeToolOperation(
            operation="rich:pulse",
            summary=(
                "Author a validated, visual-first Pulse Rich Deliverable using the registered "
                "composition grammar. Use meaningful icons for every metric and renderer-safe "
                "images/visual editorial cards when they clarify a decision; describe this "
                "operation before authoring."
            ),
            mutation_kind=ToolMutationKind.CREATE,
            input_schema=_PULSE_WRITE_DELIVERABLE_SCHEMA,
            semantics=declared_default_semantics(ToolMutationKind.CREATE),
            examples=[
                {
                    "action": "rich:pulse",
                    "content": "Daily pulse fallback.",
                    "format": "rich",
                    "rich": PULSE_DAILY_SKELETON,
                }
            ],
            side_effects=[
                "Persists a deliverable only after the Pulse presentation validator succeeds."
            ],
            validator_ids=["write_deliverable.rich"],
        ),
    ],
)

ATTACH_ARTIFACT_TOOL = ToolDefinition(
    name="attach_artifact",
    description=(
        "Queue an existing persisted Cognis artifact or deliverable for presentation with the "
        "final response of this turn. Accepts only an `art_…`, `att_…`, `doc_…`, `img_…`, or `dlv_…` content "
        "reference. "
        "Local paths, URLs, filenames, and bytes must first be published as an artifact. "
        "The same content reference cannot be attached twice in one turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content_ref": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Persisted Cognis content reference to present: `art_…`, `att_…`, `doc_…`, or `img_…` for "
                    "an artifact, or `dlv_…` for a deliverable."
                ),
            }
        },
        "required": ["content_ref"],
        "additionalProperties": False,
    },
    source=_SOURCE,
    category="deliverable",
    read_only=False,
)

STEP_REQUEST_QUESTIONS_TOOL = ToolDefinition(
    name="step_request_questions",
    description=(
        "Request a structured set of questions from the caller while staying in the same step. "
        "Use when clarification, design choices, or planning decisions are needed before proceeding."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "description": "Structured question set to ask the caller.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Stable question identifier."},
                        "header": {
                            "type": "string",
                            "description": "Optional short section label.",
                        },
                        "question": {"type": "string", "description": "Question text."},
                        "options": {
                            "type": "array",
                            "description": "Optional selectable answers.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["id", "label"],
                            },
                        },
                        "multiple": {"type": "boolean", "description": "Allow multiple options."},
                        "allow_custom": {
                            "type": "boolean",
                            "description": "Allow a free-form custom answer.",
                        },
                        "required": {"type": "boolean", "description": "Require an answer."},
                    },
                    "required": ["id", "question"],
                },
            },
            "context": {
                "type": "object",
                "description": "Optional background context for the question set.",
            },
        },
        "required": ["questions"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

REQUEST_CREDENTIAL_TOOL = ToolDefinition(
    name="request_credential",
    description=(
        "Request a durable credential from the user without exposing its secret value to the "
        "LLM. This call suspends the current turn until the user approves, denies, cancels, or "
        "the request times out. On approval, the credential is stored and granted to the current "
        "agent before this call returns. Resume the original operation using the returned "
        "credential ID; do not ask the user for the same credential again."
    ),
    parameters={
        "type": "object",
        "properties": {
            "credential_id": {"type": "string", "description": "Credential ID to create/update"},
            "kind": {
                "type": "string",
                "enum": list(SUPPORTED_CREDENTIAL_KINDS),
                "description": ("Credential kind. Use 'username_password' for login credentials."),
            },
            "label": {"type": "string", "description": "Human-readable credential label"},
            "description": {"type": "string", "description": "Why this credential is needed"},
            "metadata": {
                "type": "object",
                "description": "Non-secret metadata such as login_url or domain",
            },
            "required_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expected payload fields such as username/password/token",
            },
            "agent_id": {"type": "string", "description": "Optional agent scope override"},
            "scope": {"type": "string", "description": "Credential scope (default: user)"},
        },
        "required": ["credential_id", "kind", "label"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

REQUEST_AUTH_CHALLENGE_TOOL = ToolDefinition(
    name="request_auth_challenge",
    description=(
        "Request a live auth or MFA challenge response from the user without exposing the value to the LLM."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Challenge kind such as otp_code or push_approval",
            },
            "label": {"type": "string", "description": "Short title shown to the user"},
            "message": {"type": "string", "description": "What the user should do"},
            "metadata": {
                "type": "object",
                "description": "Safe non-secret context such as origin/domain/login_url",
            },
            "required_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expected response fields, e.g. code",
            },
            "timeout_seconds": {"type": "integer", "description": "Challenge timeout in seconds"},
        },
        "required": ["kind", "label", "message"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

LIST_CREDENTIALS_TOOL = ToolDefinition(
    name="list_credentials",
    description=(
        "List credential metadata that the current agent is allowed to use. "
        "Use this to discover available credential IDs before requesting or using one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "Optional credential kind filter"},
            "domain": {"type": "string", "description": "Optional domain filter"},
            "origin": {"type": "string", "description": "Optional origin filter"},
            "label_contains": {
                "type": "string",
                "description": "Optional case-insensitive label filter",
            },
        },
    },
    source=_SOURCE,
    category="workflow",
    read_only=True,
)

STEP_TODO_WRITE_TOOL = ToolDefinition(
    name="step_todo_write",
    description=(
        "Track required progress for genuine multistep work within this step. Do not create a "
        "todo list for work that can be completed in a single response, including straightforward "
        "questions, short answers, or simple clarification. Keep created todos current across "
        "turns, and mark every item completed or cancelled before terminal completion. Multiple "
        "in_progress items are allowed for genuinely parallel workstreams. Architect todos should "
        "track durable workstreams or milestones; developer todos should track granular "
        "implementation, test, and acceptance steps. Stable labels or hierarchy are optional when "
        "useful. The todos array replaces the entire current list."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Brief description of the task.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status of the task.",
                        },
                    },
                    "required": ["content", "status"],
                },
                "description": "The updated todo list.",
            },
        },
        "required": ["todos"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

STEP_TODO_LIST_TOOL = ToolDefinition(
    name="step_todo_list",
    description="Read the durable todo list for the current step.",
    parameters={"type": "object", "properties": {}},
    source=_SOURCE,
    category="workflow",
    read_only=True,
)


# Stage 36: switch_executor — controller-handled tool that changes the
# conversation's active executor for subsequent executor-routed tool calls.
# The active executor binding persists across turns and steps until the
# next switch (by the agent or by the user via /executor). The controller
# never auto-changes it; this tool is the agent's only mutator.
SWITCH_EXECUTOR_TOOL = ToolDefinition(
    name="switch_executor",
    description=(
        "Change the active executor for subsequent tool calls in this conversation. "
        "Use when you want to keep working on a different assigned executor without "
        "specifying target_executor on every call. The target executor must be one "
        "of the executors assigned to you (primary or additional) and currently "
        "usable. Use primary executors for normal work. Additional executors are "
        "special-purpose targets and must not be used merely as fallback capacity "
        "when a primary executor is down; switch to one only when the task requires "
        "that specific machine or the user asks for it. Switch back to a primary "
        "executor after that specific work is done, and before unrelated or generic "
        "follow-up work. Switching to a non-primary (additional) executor will be "
        "flagged in your context until you switch back to a primary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "executor_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Assigned executor id to make active. Must be one of the agent's "
                    "primary or additional executors. Additional executors are "
                    "special-purpose targets, not general fallback capacity."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Brief, optional reason for the switch.",
            },
        },
        "required": ["executor_id"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

SWITCH_AGENT_PROFILE_TOOL = ToolDefinition(
    name="switch_agent_profile",
    description=(
        "Switch to another runtime profile explicitly listed as agent-switch-eligible in the "
        "current system context. Use this to match the remaining task's complexity, risk, and "
        "cost needs. Call this tool alone. The current LLM cycle ends after a successful switch "
        "and the same logical turn continues under the selected profile."
    ),
    parameters={
        "type": "object",
        "properties": {
            "profile_id": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
                "description": "Switch-eligible runtime profile ID.",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "pattern": "\\S",
                "description": (
                    "Concise operational reason why the remaining task needs this profile. "
                    "State decision drivers, not private chain-of-thought."
                ),
            },
        },
        "required": ["profile_id", "reason"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)


def workflow_tools() -> list[BaseToolDefinition]:
    """Return built-in workflow tool definitions.

    These are display-only definitions for the tool registry.
    Actual handling is done by the agent loop, not the tool router.
    """
    return [
        WRITE_DELIVERABLE_TOOL,
        ATTACH_ARTIFACT_TOOL,
        STEP_COMPLETE_TOOL,
        STEP_REQUEST_QUESTIONS_TOOL,
        REQUEST_CREDENTIAL_TOOL,
        REQUEST_AUTH_CHALLENGE_TOOL,
        LIST_CREDENTIALS_TOOL,
        STEP_TODO_WRITE_TOOL,
        STEP_TODO_LIST_TOOL,
        SWITCH_EXECUTOR_TOOL,
        SWITCH_AGENT_PROFILE_TOOL,
    ]
