from pathlib import Path

from alembic.script import ScriptDirectory


def test_migration_graph_has_one_head() -> None:
    migrations = Path(__file__).resolve().parents[2] / "cognis/store/migrations"
    script = ScriptDirectory(str(migrations))
    heads = script.get_heads()
    assert len(heads) == 1, f"Deployment requires exactly one Alembic head: {heads}"
    assert heads == ["190_document_write_access"]
    ancestors = {item.revision for item in script.walk_revisions()}
    assert {"157_artifact_agent_access", "157_workflow_creation_origin"} <= ancestors
    assert {
        "180_document_sharing",
        "180_conversation_title_retry",
        "181_merge_document_sharing",
    } <= ancestors
    merge = script.get_revision("181_merge_document_sharing")
    assert merge is not None
    assert set(merge.down_revision) == {
        "180_document_sharing",
        "180_conversation_title_retry",
    }
    usage_merge = script.get_revision("185_merge_usage_heads")
    assert usage_merge is not None
    assert set(usage_merge.down_revision) == {
        "184_usage_session_day_index",
        "181_merge_document_sharing",
    }
    assert "180_usage_accounting" in ancestors
    deployment_merge = script.get_revision("186_merge_usage_read_states")
    assert deployment_merge is not None
    assert set(deployment_merge.down_revision) == {
        "185_merge_usage_heads",
        "182_conversation_read_states",
    }
