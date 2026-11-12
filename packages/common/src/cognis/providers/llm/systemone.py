"""Native System One contracts and transport, independent of chat generation."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from cognis.providers.llm.hosted import PROVIDER_BASES, api_endpoint, systemone_provider_preset
from cognis.providers.llm.usage_binding import UsageBinding


class SystemOneResponseError(ValueError):
    """Content-free reason for rejecting a provider's typed decision response."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Question(BaseModel):
    """Common typed-question fields."""

    model_config = ConfigDict(extra="forbid", strict=True)
    instructions: str | dict[str, JsonValue] | list[JsonValue]


class ChoiceQuestion(Question):
    """Choose among named criteria."""

    type: Literal["choice"]
    criteria: dict[str, JsonValue] = Field(min_length=1, max_length=255)


class ScoreQuestion(Question):
    """Score against ordered criteria."""

    type: Literal["score"]
    criteria: list[str | dict[str, JsonValue] | list[JsonValue]] = Field(
        min_length=2, max_length=10
    )


class NoulQuestion(Question):
    """Evaluate the probability of a statement."""

    type: Literal["noul"]
    criteria: dict[str, JsonValue] | None = None


TypedQuestion = Annotated[
    ChoiceQuestion | ScoreQuestion | NoulQuestion, Field(discriminator="type")
]


class SystemOneRequest(BaseModel):
    """TypeSafe-compatible evaluation request."""

    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=512)
    state: str | dict[str, JsonValue] | list[JsonValue]
    questions: dict[str, TypedQuestion] = Field(min_length=1, max_length=256)


Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class ChoiceAnswer(BaseModel):
    """A selected option and its probability distribution."""

    model_config = ConfigDict(extra="allow")
    type: Literal["choice"]
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class ScoreAnswer(BaseModel):
    """An expected score and its probability distribution."""

    model_config = ConfigDict(extra="allow")
    type: Literal["score"]
    score: float = Field(ge=0, allow_inf_nan=False)
    confidence: Probability
    probabilities: dict[str, Probability]


class NoulAnswer(BaseModel):
    """A statement probability."""

    model_config = ConfigDict(extra="allow")
    type: Literal["noul"]
    noul: Probability


class SystemOneResponse(BaseModel):
    """Native typed results; retain provider metadata and usage without synthesis."""

    model_config = ConfigDict(extra="allow")
    model: str
    answers: dict[
        str, Annotated[ChoiceAnswer | ScoreAnswer | NoulAnswer, Field(discriminator="type")]
    ]
    usage: dict[str, Any] = Field(default_factory=dict)


def validate_systemone_response(data: Any, request: SystemOneRequest) -> dict[str, Any]:
    """Reject incomplete or mismatched decisions; never invent an answer."""
    try:
        response = SystemOneResponse.model_validate(data)
    except ValidationError:
        raise SystemOneResponseError("systemone_response_schema") from None
    if set(response.answers) != set(request.questions):
        raise SystemOneResponseError("systemone_answer_set")
    for name, question in request.questions.items():
        answer = response.answers[name]
        if answer.type != question.type:
            raise SystemOneResponseError("systemone_answer_type")
        # Match the deployed Jev typed-answer contract: native probabilities
        # are approximate. Preserve values/ranking; do not normalize answers.
        if (
            isinstance(answer, ChoiceAnswer | ScoreAnswer)
            and abs(sum(answer.probabilities.values()) - 1.0) > 0.02
        ):
            raise SystemOneResponseError("systemone_probability_sum")
        if (
            isinstance(question, ChoiceQuestion)
            and isinstance(answer, ChoiceAnswer)
            and (
                answer.choice not in question.criteria
                or set(answer.probabilities) != set(question.criteria)
                or answer.probabilities.get(answer.choice, -1)
                != max(answer.probabilities.values(), default=-1)
            )
        ):
            raise SystemOneResponseError("systemone_choice_criteria")
        if (
            isinstance(question, ScoreQuestion)
            and isinstance(answer, ScoreAnswer)
            and (
                answer.score > len(question.criteria) - 1
                or set(answer.probabilities) != {str(i) for i in range(len(question.criteria))}
            )
        ):
            raise SystemOneResponseError("systemone_score_criteria")
    # Preserve the original payload, including integer probabilities and extras.
    return dict(data)


async def request_systemone(
    payload: SystemOneRequest,
    *,
    preset: str,
    base_url: str = "",
    api_key: str,
    timeout: float,
    extra_headers: dict[str, str] | None = None,
    usage_binding: UsageBinding | None = None,
) -> dict[str, Any]:
    """Evaluate once without text-generation fallbacks or implicit retries."""
    transport_preset = systemone_provider_preset(preset, base_url)
    if transport_preset is None:
        raise ValueError("Provider does not support System One")
    if not api_key:
        raise ValueError("System One credentials unavailable")
    headers = {**(extra_headers or {}), "Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        attempt = (
            usage_binding.begin(
                operation="systemone",
                streaming=False,
                parameters={"model": payload.model},
                # System One reports OpenAI-shaped usage counters.
                semantic_profile="openai",
            )
            if usage_binding is not None
            else None
        )
        try:
            response = await client.post(
                api_endpoint(base_url or PROVIDER_BASES[transport_preset], "systemone"),
                headers=headers,
                json=payload.model_dump(mode="json", exclude_none=True),
            )
            response.raise_for_status()
            data = response.json()
            if attempt is not None:
                attempt.observe(data, generated=False)
            result = validate_systemone_response(data, payload)
            if usage_binding is not None:
                usage_binding.request.mark_generated()
            return result
        except BaseException as exc:
            if attempt is not None:
                attempt.finish(error=exc)
            raise
        finally:
            if attempt is not None:
                attempt.finish()
