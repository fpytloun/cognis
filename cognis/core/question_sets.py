"""Canonical workflow question-set validation and rendering helpers."""

from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    """Return a plain dict for dict-like/Pydantic question-set records."""

    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    raise ValueError("question-set item must be an object")


def normalize_questions(value: Any) -> list[dict[str, Any]]:
    """Return canonical question-set payload or raise ``ValueError``."""

    if not isinstance(value, list) or not value:
        raise ValueError("questions must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    seen_question_ids: set[str] = set()
    for raw in value:
        raw = _as_dict(raw)
        question_id = str(raw.get("id") or "").strip()
        if not question_id:
            raise ValueError("question id is required")
        if question_id in seen_question_ids:
            raise ValueError(f"duplicate question id: {question_id}")
        seen_question_ids.add(question_id)
        question_text = str(raw.get("question") or "").strip()
        if not question_text:
            raise ValueError(f"question {question_id} must include question text")
        options = normalize_options(raw.get("options", []), question_id=question_id)
        normalized.append(
            {
                "id": question_id,
                "header": str(raw["header"]).strip()
                if raw.get("header") is not None and str(raw.get("header")).strip()
                else None,
                "question": question_text,
                "options": options,
                "multiple": bool(raw.get("multiple", False)),
                "allow_custom": bool(raw.get("allow_custom", True)),
                "required": bool(raw.get("required", True)),
            }
        )
    return normalized


def normalize_options(value: Any, *, question_id: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"question {question_id} options must be an array")
    normalized: list[dict[str, Any]] = []
    seen_option_ids: set[str] = set()
    for raw in value:
        raw = _as_dict(raw)
        option_id = str(raw.get("id") or "").strip()
        if not option_id:
            raise ValueError(f"question {question_id} option id is required")
        label = str(raw.get("label") or "").strip()
        description = (
            str(raw["description"]).strip()
            if raw.get("description") is not None and str(raw.get("description")).strip()
            else None
        )
        if not label:
            raise ValueError(f"question {question_id} option {option_id} must include a label")
        if option_id in seen_option_ids:
            raise ValueError(f"duplicate option id in question {question_id}: {option_id}")
        seen_option_ids.add(option_id)
        normalized.append({"id": option_id, "label": label, "description": description})
    return normalized


def normalize_context(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {"note": value.strip()}
    return None


def normalize_reply(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("reply must be an object")
    mode = str(value.get("mode") or "structured")
    if mode not in {"structured", "plain_text"}:
        raise ValueError("mode must be structured or plain_text")
    answers = value.get("answers")
    if not isinstance(answers, list):
        raise ValueError("answers must be an array")
    normalized_answers: list[dict[str, Any]] = []
    for raw in answers:
        if not isinstance(raw, dict):
            raise ValueError("each answer must be an object")
        question_id = str(raw.get("question_id") or "").strip()
        if not question_id:
            raise ValueError("answer question_id is required")
        selected = raw.get("selected_option_ids", [])
        if selected is None:
            selected = []
        if not isinstance(selected, list):
            raise ValueError("selected_option_ids must be an array")
        custom = raw.get("custom_answer")
        normalized_answers.append(
            {
                "question_id": question_id,
                "selected_option_ids": [str(item) for item in selected],
                "custom_answer": str(custom) if custom is not None else None,
            }
        )
    return {"answers": normalized_answers, "mode": mode}


def validate_reply_for_questions(
    reply: dict[str, Any], questions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Validate structured rich-client reply against stored question set."""

    normalized = normalize_reply(reply)
    if normalized["mode"] == "plain_text":
        return normalized
    normalized_questions = [_as_dict(question) for question in questions]
    by_id = {str(question.get("id")): question for question in normalized_questions}
    answered_ids: set[str] = set()
    for answer in normalized["answers"]:
        qid = answer["question_id"]
        question = by_id.get(qid)
        if question is None:
            raise ValueError(f"unknown question id: {qid}")
        if qid in answered_ids:
            raise ValueError(f"duplicate answer for question id: {qid}")
        answered_ids.add(qid)
        selected = answer["selected_option_ids"]
        if selected and not bool(question.get("multiple", False)) and len(selected) > 1:
            raise ValueError(f"question {qid} does not allow multiple selections")
        allowed_options = {str(option.get("id")) for option in question.get("options") or []}
        invalid_options = [option_id for option_id in selected if option_id not in allowed_options]
        if invalid_options:
            raise ValueError(f"question {qid} has invalid option ids: {', '.join(invalid_options)}")
        custom = answer.get("custom_answer")
        if custom and not bool(question.get("allow_custom", True)):
            raise ValueError(f"question {qid} does not allow custom answers")
        if bool(question.get("required", True)) and not selected and not custom:
            raise ValueError(f"question {qid} requires an answer")
    missing_required = [
        str(question.get("id"))
        for question in normalized_questions
        if bool(question.get("required", True)) and str(question.get("id")) not in answered_ids
    ]
    if missing_required:
        raise ValueError(f"missing required answers: {', '.join(missing_required)}")
    return normalized


def plain_text_reply_for_questions(content: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    if not questions:
        raise ValueError("pending question set is empty")
    first_question = _as_dict(questions[0])
    return {
        "mode": "plain_text",
        "answers": [
            {
                "question_id": str(first_question.get("id") or "q1"),
                "selected_option_ids": [],
                "custom_answer": content,
            }
        ],
    }
