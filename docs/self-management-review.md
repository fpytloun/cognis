# Self-management review

An authorized primary agent can inspect its own definition. Existing ownership and tool permissions still apply.

Self mutations require Intaris evaluation with `minimum_outcome="escalate"`, including when ordinary guardrails are disabled.
The existing approval handler passes the original Intaris `approval_call_id` on continuation.
Intaris checks the owner, session, tool, arguments, and human approval. A new call does not inherit a previous approval.

The approval reference exists only in controller runtime metadata for the retry.
The existing notification service owns cancellation and user resolution. Intaris owns the audit and decision.
Database writes keep their existing transactions. This change adds no revision checks or single-use grants.
Runtime profile selection does not change.

Deploy Intaris support before Cognis. Cognis rejects responses that do not acknowledge the requested outcome floor.
