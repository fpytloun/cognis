from cognis.channels.matrix_formatting import markdown_to_matrix_html


def test_matrix_markdown_renders_headings_and_lists() -> None:
    html = markdown_to_matrix_html(
        "# Status\n\n## Checks\n\n- tests passed\n- lint passed\n\n1. deploy\n2. verify"
    )

    assert "<h1>Status</h1>" in html
    assert "<h2>Checks</h2>" in html
    assert "<ul>" in html
    assert "<li>tests passed</li>" in html
    assert "<ol>" in html
    assert "<li>verify</li>" in html


def test_matrix_markdown_linearizes_tables_for_client_portability() -> None:
    html = markdown_to_matrix_html(
        "| Check | Result |\n| --- | --- |\n| tests | ✅ |\n| lint | ✅ |"
    )

    assert "<table>" not in html
    assert "<strong>Check:</strong> tests" in html
    assert "<strong>Result:</strong> ✅" in html


def test_matrix_markdown_preserves_code_and_links() -> None:
    html = markdown_to_matrix_html(
        "Use `pytest` and [docs](https://example.com).\n\n```python\nprint('hi')\n```"
    )

    assert "<code>pytest</code>" in html
    assert '<a href="https://example.com">docs</a>' in html
    assert "<pre><code>print('hi')\n</code></pre>" in html


def test_matrix_markdown_normalizes_task_lists() -> None:
    html = markdown_to_matrix_html("- [x] done\n- [ ] pending")

    assert "<li>☑ done</li>" in html
    assert "<li>☐ pending</li>" in html


def test_matrix_markdown_supports_gfm_strikethrough_and_two_space_nested_lists() -> None:
    html = markdown_to_matrix_html("~~obsolete~~\n\n- parent\n  - nested")

    assert "<del>obsolete</del>" in html
    assert "<li>nested</li>" in html
    assert html.count("<ul>") == 2


def test_matrix_compact_rich_markdown_avoids_margin_heavy_paragraphs_and_lists() -> None:
    html = markdown_to_matrix_html(
        "# Brief\n\nFirst paragraph.\n\nSecond paragraph.\n\n- one\n- two",
        compact=True,
    )

    assert "<h1>Brief</h1>" in html
    assert "<p>" not in html
    assert "<ul>" not in html
    assert "• one<br/>" in html


def test_matrix_markdown_sanitizes_unsafe_html_and_links() -> None:
    html = markdown_to_matrix_html(
        '<script>alert("x")</script>\n\n'
        "[bad](javascript:alert(1))\n\n"
        '<span style="color:red">plain</span>'
    )

    assert "<script>" not in html
    assert "javascript:" not in html
    assert "<span" not in html
    assert "plain" in html


def test_matrix_markdown_sanitizes_nested_unsafe_html() -> None:
    html = markdown_to_matrix_html(
        '<span><a href="javascript:alert(1)">bad</a></span>\n\n'
        '<div><strong onclick="alert(1)">strong</strong></div>'
    )

    assert "javascript:" not in html
    assert "onclick" not in html
    assert "<span" not in html
    assert "<div" not in html
    assert "<a " not in html
    assert "<strong>strong</strong>" in html
