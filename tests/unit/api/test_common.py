from __future__ import annotations

from cognis.api.common import error_response


def test_error_response_serializes_pydantic_validation_context() -> None:
    response = error_response(
        422,
        "validation_error",
        "Request validation failed",
        details={
            "errors": [
                {
                    "type": "value_error",
                    "loc": ("body", "scope"),
                    "msg": "Value error, task_step scope requires task_id and step_run_id",
                    "ctx": {"error": ValueError("task_step scope requires task_id")},
                }
            ]
        },
    )

    assert response.status_code == 422
    assert b'"error":"task_step scope requires task_id"' in response.body
