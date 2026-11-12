"""Typed classification requests shared by tool metadata and skill activation."""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

from cognis.providers.llm.systemone import (
    ChoiceQuestion,
    NoulQuestion,
    SystemOneRequest,
    TypedQuestion,
    validate_systemone_response,
)

_GROUPS: dict[str, JsonValue] = {
    "filesystem": "Read or modify files and directories.",
    "shell": "Execute arbitrary commands or manage processes.",
    "web": "Search or retrieve public web content.",
    "browser": "Automate a browser.",
    "development": "Develop, build, test, or manage source code and development systems.",
    "office": "Read or edit office documents and spreadsheets.",
    "personal": "Manage personal services, shopping, calendar, or personal productivity.",
    "communication": "Read or send messages through communication services.",
    "conversations": "Manage Cognis conversations and their histories.",
}
_CAPABILITIES = {
    "read": "The tool can read information without modifying state.",
    "write": "The tool can modify state or cause external side effects.",
    "privileged": "The tool offers administrative or unrestricted execution capabilities.",
    "destructive": "The tool can delete data or perform destructive operations.",
}


async def classify_tool_metadata(
    llm: Any, tools: list[dict[str, Any]], *, acting_user_email: str | None
) -> dict[str, Any]:
    """Classify bounded batches without granting tool permissions."""
    results: list[dict[str, Any]] = []
    # Bound request size independently of caller batching.
    for start in range(0, len(tools), 50):
        batch = tools[start : start + 50]
        questions: dict[str, TypedQuestion] = {}
        for index, _tool in enumerate(batch):
            questions[f"{index}:group"] = ChoiceQuestion(
                type="choice",
                instructions=f"Choose the profile group for tool at index {index}.",
                criteria=_GROUPS,
            )
            for capability, description in _CAPABILITIES.items():
                questions[f"{index}:{capability}"] = NoulQuestion(
                    type="noul",
                    instructions=f"For tool at index {index}: {description}",
                )
        request = SystemOneRequest.model_validate(
            {"model": "routes/classifier", "state": {"tools": batch}, "questions": questions}
        )
        response = validate_systemone_response(
            await llm.classify(request, acting_user_email=acting_user_email, max_retries=1),
            request,
        )
        answers = response["answers"]
        for index, tool in enumerate(batch):
            group = answers[f"{index}:group"]
            results.append(
                {
                    "tool_id": tool["tool_id"],
                    "profile_group": group["choice"],
                    "confidence": group["confidence"],
                    "capabilities": [
                        capability
                        for capability in _CAPABILITIES
                        if answers[f"{index}:{capability}"]["noul"] >= 0.5
                    ],
                }
            )
    return {"tools": results}


async def select_skill_tools(
    llm: Any,
    *,
    state: str,
    tool_ids: list[str],
    instructions: str,
    acting_user_email: str | None,
    cognis_session_id: str,
) -> set[str]:
    """Select only clearly required candidates; malformed answers fail closed."""
    request = SystemOneRequest(
        model="routes/classifier",
        state=state,
        questions={
            tool_id: NoulQuestion(
                type="noul",
                instructions=f"{instructions}\nIs tool {tool_id} clearly required by this skill?",
            )
            for tool_id in tool_ids
        },
    )
    response = validate_systemone_response(
        await llm.classify(
            request,
            acting_user_email=acting_user_email,
            cognis_session_id=cognis_session_id,
        ),
        request,
    )
    return {tool_id for tool_id in tool_ids if response["answers"][tool_id]["noul"] >= 0.8}
