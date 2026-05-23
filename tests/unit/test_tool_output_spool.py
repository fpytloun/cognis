from cognis.core.tool_output_spool import ToolOutputSpool


def test_live_spool_stores_and_pages_chunks() -> None:
    spool = ToolOutputSpool(max_bytes_per_call=1000, max_chunks_per_call=10)

    assert spool.append(
        conversation_id="conv_1",
        session_id="sess_1",
        call_id="call_1",
        tool_name="bash",
        text="head\n",
        stream="stdout",
    ) == (0, 0)
    assert spool.append(
        conversation_id="conv_1",
        session_id="sess_1",
        call_id="call_1",
        tool_name="bash",
        text="tail\n",
        stream="stderr",
    ) == (1, 5)

    page = spool.page(
        conversation_id="conv_1",
        session_id="sess_1",
        call_id="call_1",
        offset=0,
        limit=1,
    )

    assert page is not None
    assert page.content == "head\n"
    assert page.has_more_after
    assert page.chunks[0].stream == "stdout"


def test_live_spool_enforces_chunk_cap() -> None:
    spool = ToolOutputSpool(max_bytes_per_call=1000, max_chunks_per_call=2)
    for text in ["one", "two", "three"]:
        spool.append(
            conversation_id="conv_1",
            session_id="sess_1",
            call_id="call_1",
            tool_name="bash",
            text=text,
            stream="stdout",
        )

    page = spool.page(
        conversation_id="conv_1",
        session_id="sess_1",
        call_id="call_1",
        latest=True,
        limit=10,
    )

    assert page is not None
    assert page.content == "twothree"
    assert page.truncated
    assert page.has_more_before
