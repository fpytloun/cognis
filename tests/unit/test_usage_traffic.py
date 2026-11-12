"""Traffic classification uses trusted attribution, never caller labels."""

from cognis.models.usage import UsageAttribution, UsageClientLabels, UsageIdentity
from cognis.models.usage_traffic import traffic_dimensions


def test_legacy_key_attribution_and_unknown_are_conservative():
    identity = UsageIdentity(
        principal_type="user",
        principal_subject="reader@tests.local",
        usage_subject_kind="user",
        usage_subject_id="reader@tests.local",
        client_id="key-record-123",
        auth_type="api_key",
    )
    attribution = UsageAttribution(identity=identity, purpose="chat")
    dimensions = traffic_dimensions(attribution)
    assert dimensions["traffic_origin"] == "api"
    assert dimensions["traffic_key_id"] == "key-record-123"
    assert attribution.identity.traffic_origin == "unknown"
    unknown = attribution.model_copy(
        update={
            "identity": identity.model_copy(update={"auth_type": None}),
            "client_labels": UsageClientLabels(session_id="claimed-session"),
        }
    )
    assert traffic_dimensions(unknown)["traffic_origin"] == "unknown"
