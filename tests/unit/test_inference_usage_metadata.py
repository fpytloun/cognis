"""Authentication-derived request metadata is bounded and content-free."""

import json
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.requests import Request

from cognis.api.inference_usage import bind_authenticated_inference, observe_response
from cognis.providers.llm.usage_capture import UsageCapture


def request_for(headers):
    capture = UsageCapture(
        sink=SimpleNamespace(try_emit=lambda item: "accepted"),
        controller_owner_id="metadata-test",
        coverage_epoch_id="metadata-epoch",
    )
    app = FastAPI()
    app.state.inference_api = SimpleNamespace(llm=SimpleNamespace(usage_capture=capture))
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "scheme": "http",
            "server": ("usage.tests", 80),
            "path": "/v1/chat/completions",
            "query_string": b"private=value",
            "headers": headers,
            "state": {
                "user": SimpleNamespace(email="usage@tests.local", auth_type="api_key"),
                "inference_client_id": "metadata-key-123",
                "api_key_id": "metadata-key-123",
            },
        }
    ), capture


def test_metadata_allowlist_and_finish_reason():
    request, _ = request_for(
        [
            (b"user-agent", b"OpenAI/Python\t  1.0\r\n"),
            (b"x-cognis-inference-app-name", b"client-app"),
            (b"x-cognis-inference-app-version", b"1.0.0+build"),
            (b"x-cognis-inference-client-request-id", b"client-correlation-123"),
            (b"authorization", b"must-not-be-stored"),
            (b"x-forwarded-for", b"must-not-be-stored"),
            (b"x-private-header", b"must-not-be-stored"),
        ]
    )
    bind_authenticated_inference(request)
    bound = request.scope["state"]["cognis_usage_request"]
    assert bound.attribution.identity.traffic_origin == "api"
    assert bound.attribution.identity.key_record_id == "metadata-key-123"
    assert bound.attribution.client_labels.client_request_id == "client-correlation-123"
    assert bound.http_metadata.user_agent == "OpenAI/Python 1.0"
    assert bound.http_metadata.app_name == "client-app"
    observe_response(request, {"choices": [{"finish_reason": "length", "text": "private-content"}]})
    encoded = bound.http_metadata.model_dump_json()
    assert bound.http_metadata.finish_reason == "length"
    assert "must-not-be-stored" not in encoded
    assert "private-content" not in encoded
    assert "query_string" not in encoded
    assert set(json.loads(encoded)) <= {
        "request_path",
        "streaming",
        "user_agent",
        "app_name",
        "app_version",
        "http_status_code",
        "finish_reason",
        "error",
    }


def test_oversized_labels_reduce_metadata_without_losing_principal():
    request, capture = request_for(
        [
            (b"user-agent", b"a" * 512),
            (b"x-cognis-inference-app-name", b"a" * 64),
            (b"x-cognis-inference-app-version", b"v" * 64),
            *(
                (b"x-cognis-inference-" + name, b"x" * 160)
                for name in (b"operation-id", b"session-id", b"task-id", b"client-request-id")
            ),
        ]
    )
    bind_authenticated_inference(request)
    bound = request.scope["state"]["cognis_usage_request"]
    assert len(bound.http_metadata.user_agent) == 256
    assert bound.attribution.client_labels is None
    assert bound.attribution.identity.key_record_id == "metadata-key-123"
    assert capture.reduced_contexts == 1
