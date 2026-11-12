"""Shared pure token semantics for analytics and valuation, never provider replay.

Raw counts remain unchanged in NormalizedUsage. Analytics exposes inclusive
input/output totals; billing exposes disjoint units. Native modality extras are
not text tokens and are deliberately absent from both computations.
"""

from __future__ import annotations

from cognis.models.usage import NormalizedUsage, UsageCount, UsageModel


class UsageBillableTokens(UsageModel):
    """Disjoint observed token units; unknown units must never be priced as zero."""

    input: UsageCount
    output: UsageCount
    cache_read: UsageCount
    cache_write: UsageCount
    cache_write_5m: UsageCount
    cache_write_1h: UsageCount
    reasoning: UsageCount


class UsageTokenNormalization(UsageModel):
    """Versioned derived evidence; source semantic profile/version stays canonical."""

    version: int = 1
    input: UsageCount
    output: UsageCount
    total: UsageCount
    cache_read: UsageCount
    cache_write: UsageCount
    reasoning: UsageCount
    billable: UsageBillableTokens


def _sum_counts(*counts: UsageCount) -> UsageCount:
    applicable = [count for count in counts if count.status != "not_applicable"]
    if not applicable:
        return UsageCount(status="not_applicable")
    known = [count for count in applicable if count.value is not None]
    if not known:
        return UsageCount()
    sources = {count.source for count in known}
    return UsageCount(
        value=sum(count.value for count in known if count.value is not None),
        status="reported" if all(count.status == "reported" for count in applicable) else "partial",
        source=known[0].source if len(sources) == 1 else "adapter",
    )


def _inclusive(base: UsageCount, subset: UsageCount, semantics: str, additional: str) -> UsageCount:
    if semantics == additional:
        return _sum_counts(base, subset)
    if (
        semantics in {"included_in_input", "included_in_output"}
        and base.value is not None
        and subset.value is not None
        and subset.value > base.value
    ):
        return UsageCount(value=base.value, status="partial", source=base.source)
    if semantics == "unknown" and subset.status != "not_applicable" and subset.value != 0:
        if base.value is not None:
            return UsageCount(value=base.value, status="partial", source=base.source)
        return UsageCount()
    return base


def _exclusive(base: UsageCount, subset: UsageCount, semantics: str) -> UsageCount:
    if semantics == "included_in_input" and subset.status != "not_applicable":
        if base.value is None or subset.status != "reported" or subset.value is None:
            return UsageCount()
        if subset.value > base.value:
            return UsageCount()
        return UsageCount(value=base.value - subset.value, status=base.status, source=base.source)
    if semantics == "unknown" and subset.status != "not_applicable" and subset.value != 0:
        return UsageCount()
    return base


def normalize_usage_tokens(usage: NormalizedUsage) -> UsageTokenNormalization:
    """Derive inclusive analytics and disjoint billing without changing raw facts."""
    write = usage.cache_write_tokens
    ttl_observed = (
        usage.cache_write_5m_tokens.value is not None
        or usage.cache_write_1h_tokens.value is not None
    )
    if write.value is None and ttl_observed:
        write = _sum_counts(usage.cache_write_5m_tokens, usage.cache_write_1h_tokens)
    inclusive_input = _inclusive(
        usage.input_tokens,
        usage.cache_read_tokens,
        usage.cache_read_semantics,
        "additional_to_input",
    )
    inclusive_input = _inclusive(
        inclusive_input, write, usage.cache_write_semantics, "additional_to_input"
    )
    inclusive_output = _inclusive(
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.reasoning_semantics,
        "additional_to_output",
    )
    billable_input = _exclusive(
        usage.input_tokens, usage.cache_read_tokens, usage.cache_read_semantics
    )
    billable_input = _exclusive(billable_input, write, usage.cache_write_semantics)
    billable_output = usage.output_tokens
    if (
        usage.reasoning_semantics == "unknown"
        and usage.reasoning_tokens.status != "not_applicable"
        and usage.reasoning_tokens.value != 0
    ):
        billable_output = UsageCount()
    na = UsageCount(status="not_applicable")
    return UsageTokenNormalization(
        input=inclusive_input,
        output=inclusive_output,
        total=_sum_counts(inclusive_input, inclusive_output),
        cache_read=usage.cache_read_tokens,
        cache_write=write,
        reasoning=usage.reasoning_tokens,
        billable=UsageBillableTokens(
            input=billable_input,
            output=billable_output,
            cache_read=usage.cache_read_tokens,
            cache_write=na if ttl_observed else write,
            cache_write_5m=usage.cache_write_5m_tokens if ttl_observed else na,
            cache_write_1h=usage.cache_write_1h_tokens if ttl_observed else na,
            reasoning=usage.reasoning_tokens
            if usage.reasoning_semantics == "additional_to_output"
            else na,
        ),
    )
