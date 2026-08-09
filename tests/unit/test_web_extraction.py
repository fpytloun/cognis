"""Tests for structured web document extraction."""

from __future__ import annotations

from cognis.core.tool_output_presentation import (
    artifact_anchor_names,
    present_tool_output,
    safe_output_anchors,
)
from cognis.tools.executor.web.backends.formatting import build_fetch_tool_result
from cognis.tools.executor.web.extraction import _dedent_fenced_code_blocks, extract_document

ARTICLE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <title>Ignored browser title</title>
  <link rel="canonical" href="https://example.com/news/oil-market" />
  <meta property="og:title" content="Oil markets reassess risk after conflict" />
  <meta property="og:description" content="Analysts are revising oil price assumptions." />
  <meta property="og:site_name" content="Example News" />
  <meta property="og:image" content="https://cdn.example.com/hero.jpg" />
  <meta name="twitter:image" content="https://cdn.example.com/twitter.jpg" />
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "NewsArticle",
    "headline": "Oil markets reassess risk after conflict",
    "description": "Structured summary from JSON-LD.",
    "datePublished": "2026-04-27T12:00:00Z",
    "dateModified": "2026-04-27T13:30:00Z",
    "author": {"@type": "Person", "name": "Jane Analyst"},
    "publisher": {"@type": "Organization", "name": "Example Wire"},
    "image": {"@type": "ImageObject", "url": "https://cdn.example.com/jsonld-hero.jpg"}
  }
  </script>
</head>
<body>
  <nav>Subscribe Sign up Most Popular Advertisement</nav>
  <main>
    <article>
      <h1>Oil markets reassess risk after conflict</h1>
      <p>Analysts revised their oil price estimates after conflict disrupted energy markets.</p>
      <p>Several banks said supply risks could keep prices elevated while shipping routes remain uncertain.</p>
      <figure>
        <img src="/inline.jpg" alt="Oil tanker at sea" width="900" height="500" />
        <figcaption>An oil tanker crosses a busy shipping route.</figcaption>
      </figure>
    </article>
  </main>
  <footer>Related articles Cookie settings Most Popular</footer>
</body>
</html>
"""


def test_long_fetch_promotes_media_ref_without_reordering_tool_output() -> None:
    result = build_fetch_tool_result(
        url="https://example.com/article",
        content="article body " * 2_000,
        metadata={
            "extracted_document": {
                "url": "https://example.com/article",
                "images": [
                    {
                        "url": "https://cdn.example.com/article.jpg",
                        "alt": "Source photograph",
                    }
                ],
            }
        },
    )
    anchors = safe_output_anchors((result.metadata or {}).get("output_anchors"))
    names = [anchor["anchor"] for anchor in anchors]
    presentation = present_tool_output(
        result.output,
        900,
        recovery_call_id="call_web_media",
        has_full_output=True,
        anchors=names,
        lazy_artifact_anchors=artifact_anchor_names(anchors),
    )

    assert result.output.index("[[page:1]]") < result.output.index("[[media:1]]")
    assert presentation.lazy_artifact_refs == ("tool_artifact:call_web_media:media:1",)


def test_extract_document_merges_jsonld_opengraph_and_media() -> None:
    document = extract_document(
        ARTICLE_HTML,
        url="https://example.com/news/oil-market?utm=1",
        output_format="markdown",
    )

    assert document.title == "Oil markets reassess risk after conflict"
    assert document.description in {
        "Structured summary from JSON-LD.",
        "Analysts are revising oil price assumptions.",
    }
    assert document.author == "Jane Analyst"
    assert document.published_at == "2026-04-27T12:00:00Z"
    assert document.canonical_url == "https://example.com/news/oil-market"
    assert "Analysts revised their oil price estimates" in document.content
    assert "Several banks said supply risks" in document.content
    assert "Cookie settings" not in document.content
    assert any(image.role == "hero" for image in document.images)
    assert any(
        image.caption == "An oil tanker crosses a busy shipping route." for image in document.images
    )


def test_extract_document_omits_hidden_authorization_and_browser_warnings() -> None:
    html = """
    <html><head>
      <title>Public API reference</title>
      <meta name="description" content="Public API documentation." />
    </head><body>
      <div id="unsupported-browser" hidden>
        This browser is no longer supported. Upgrade to another browser.
      </div>
      <div unauthorized-private-section hidden>
        Access to this page requires authorization. Try signing in.
      </div>
      <div aria-hidden="true">Login required.</div>
      <div style="display: none !important">Verify you are human.</div>
      <template><p>Subscription required.</p></template>
      <main>
        <h1>Public API reference</h1>
        <p>This public property returns the configured display root for the drive.</p>
        <p>Its value is null when the drive does not target a network location.</p>
      </main>
    </body></html>
    """

    document = extract_document(
        html,
        url="https://learn.example/api/public-property",
        output_format="markdown",
    )

    assert "This public property returns" in document.content
    assert "requires authorization" not in document.content
    assert "browser is no longer supported" not in document.content
    assert "Login required" not in document.content
    assert "Verify you are human" not in document.content
    assert "Subscription required" not in document.content
    assert document.semantic_quality["status"] == "complete"


def test_extract_document_prunes_nested_hidden_styles_without_stale_tags() -> None:
    html = """
    <html><head><title>Public issue report</title></head><body>
      <div style="display: none">
        Parent placeholder.
        <div style="visibility: hidden">
          Nested placeholder.
          <span style="display: none !important">Deep placeholder.</span>
        </div>
      </div>
      <main>
        <h1>Public issue report</h1>
        <p>This public issue describes a reproducible metadata display defect.</p>
        <p>The report includes enough detail for maintainers to investigate it.</p>
      </main>
    </body></html>
    """

    document = extract_document(
        html,
        url="https://github.example/project/issues/20221",
        output_format="markdown",
        options={"include_media": "none"},
    )

    assert "This public issue describes" in document.content
    assert "Parent placeholder" not in document.content
    assert "Nested placeholder" not in document.content
    assert "Deep placeholder" not in document.content
    assert document.semantic_quality["status"] == "complete"


def test_hidden_pruning_preserves_jsonld_only_article_body() -> None:
    html = """
    <html><head><title>Structured public article</title>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Article",
       "headline":"Structured public article",
       "articleBody":"The structured article body remains available to public readers."}
      </script>
    </head><body>
      <div hidden>Access to this page requires authorization. Try signing in.</div>
    </body></html>
    """

    document = extract_document(
        html,
        url="https://learn.example/structured-article",
        output_format="markdown",
    )

    assert document.extractor == "schema_article_body"
    assert "structured article body remains available" in document.content
    assert "requires authorization" not in document.content


def test_rfc_editor_adapter_preserves_early_and_late_sections() -> None:
    repeated = "Protocol requirements and implementation considerations. " * 220
    html = f"""
    <html><head><title>RFC 9999: Test Protocol</title></head><body>
      <table class="ears"><tr><td>Repeated running header</td></tr></table>
      <h1 id="rfcnum">RFC 9999</h1>
      <h1 id="title">Test Protocol</h1>
      <section id="section-abstract"><h2>Abstract</h2><p>{repeated}</p></section>
      <div id="toc"><h2>Table of Contents</h2><p>Navigation only</p></div>
      <div id="introduction"><h2>1. Introduction</h2><p>{repeated}</p></div>
      <div id="security"><h2>9. Security Considerations</h2><p>{repeated}</p></div>
      <section id="section-10"><h2>10. References</h2><p>{repeated}</p></section>
      <section id="appendix-D"><h2>Appendix D. Deployment Notes</h2>
        <p>{repeated}</p></section>
      <section id="appendix-Z"><h2>Index</h2><p>Keyword navigation</p></section>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://www.rfc-editor.org/rfc/rfc9999.html",
        output_format="markdown",
        options={"include_media": "none"},
    )

    assert document.extractor == "adapter_rfc_editor"
    assert "# RFC 9999" in document.content
    assert "1. Introduction" in document.content
    assert "9. Security Considerations" in document.content
    assert "10. References" in document.content
    assert "Appendix D. Deployment Notes" in document.content
    assert "Repeated running header" not in document.content
    assert "Navigation only" not in document.content
    assert "Keyword navigation" not in document.content


def test_hacker_news_adapter_frames_submission_and_discussion() -> None:
    html = """
    <html><head><title>Launch story | Hacker News</title></head><body>
      <table class="itemlist">
        <tr class="athing" id="42"><td class="title">
          <span class="titleline"><a href="https://example.com/launch">Launch story</a></span>
        </td></tr>
        <tr><td class="subtext"><span class="score">123 points</span>
          <a class="hnuser">alice</a><span class="age">2 hours ago</span></td></tr>
        <tr><td class="toptext"><p>Ask HN submission details.</p></td></tr>
        <tr class="comtr"><td><table><tr>
          <td class="ind"><img width="40"></td>
          <td><span class="comhead"><a class="hnuser">bob</a>
            <span class="age">1 hour ago</span></span>
            <div class="commtext"><p>Substantive first comment.</p></div></td>
        </tr></table></td></tr>
      </table>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://news.ycombinator.com/item?id=42",
        output_format="markdown",
    )

    assert document.extractor == "adapter_hacker_news"
    assert document.content.startswith("# Launch story")
    assert "Original submission: https://example.com/launch" in document.content
    assert "123 points · by alice · 2 hours ago" in document.content
    assert "Ask HN submission details." in document.content
    assert "## Discussion" in document.content
    assert "bob · 1 hour ago · depth 1" in document.content
    assert "Substantive first comment." in document.content


def test_hacker_news_adapter_does_not_collapse_listing_pages() -> None:
    html = """
    <html><head><title>Hacker News</title></head><body>
      <table class="itemlist">
        <tr class="athing" id="1"><td class="title"><span class="titleline">
          <a href="https://example.com/one">First story</a></span></td></tr>
        <tr><td class="subtext"><span class="score">10 points</span></td></tr>
        <tr class="athing" id="2"><td class="title"><span class="titleline">
          <a href="https://example.com/two">Second story</a></span></td></tr>
        <tr><td class="subtext"><span class="score">20 points</span></td></tr>
      </table>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://news.ycombinator.com/news",
        output_format="markdown",
    )

    assert document.extractor != "adapter_hacker_news"
    assert "First story" in document.content
    assert "Second story" in document.content


def test_generic_structural_candidate_extracts_deep_guide_and_pseudo_tables() -> None:
    rows = "".join(
        f"""
        <div class="bb_table_tr">
          <div class="bb_table_td"><img src="icon-{index}.png"></div>
          <div class="bb_table_td">Weapon {index}</div>
          <div class="bb_table_td">{100 + index} damage</div>
        </div>
        """
        for index in range(12)
    )
    html = f"""
    <html><head>
      <title>Steam Community :: Guide :: Complete weapons</title>
      <meta property="og:description" content="A public weapon guide." />
    </head><body>
      <nav>{"<a href='/'>Navigation</a>" * 12}</nav>
      <div class="workshopItemTitle">Complete weapons</div>
      <div class="guide subSections">
        <div class="subSection">
          <div class="subSectionTitle">One-Handed Swords</div>
          <div class="subSectionDesc">
            <div class="bb_table">
              <div class="bb_table_tr">
                <div class="bb_table_th">Icon</div>
                <div class="bb_table_th">Item</div>
                <div class="bb_table_th">Damage</div>
              </div>
              {rows}
            </div>
          </div>
        </div>
        <div class="subSection">
          <div class="subSectionTitle">Frequently Asked Questions</div>
          <div class="subSectionDesc">
            This public guide explains where each weapon can be obtained.
          </div>
        </div>
      </div>
    </body></html>
    """

    document = extract_document(
        html,
        url="https://steamcommunity.com/sharedfiles/filedetails/?id=3739156180",
        output_format="markdown",
        options={"include_media": "none"},
    )

    assert document.extractor == "dom_structural"
    assert "## One-Handed Swords" in document.content
    assert "| Icon | Item | Damage |" in document.content
    assert "| --- | --- | --- |" in document.content
    assert "|  | Weapon 11 | 111 damage |" in document.content
    assert "Weapon 11" in document.content
    assert "111 damage" in document.content
    assert "Frequently Asked Questions" in document.content
    assert "Navigation" not in document.content
    assert "icon-1.png" not in document.content
    assert document.semantic_quality["status"] == "complete"


def test_generic_structural_candidate_declines_thin_guide() -> None:
    html = """
    <html><head><title>Steam Community :: Guide :: Short guide</title></head><body>
      <div class="workshopItemTitle">Short guide</div>
      <div class="guide subSections">
        <div class="subSectionTitle">Short section</div>
        <div class="subSectionDesc"><p>Short public note.</p></div>
      </div>
      <main><p>Generic extraction remains available for this public page.</p></main>
    </body></html>
    """

    document = extract_document(
        html,
        url="https://steamcommunity.com/sharedfiles/filedetails/?id=42",
        output_format="markdown",
    )

    assert document.extractor != "dom_structural"
    assert "Generic extraction remains available" in document.content


def test_link_heavy_shell_does_not_downgrade_selected_candidate() -> None:
    html = f"""
    <html><head><title>Complete field guide</title></head><body>
      <nav>{"<a href='/nav'>Navigation</a>" * 30}</nav>
      <div class="guide sections">
        <div class="section-title">Complete field guide</div>
        <div class="description">
          {"This guide preserves substantive instructions and reference details. " * 30}
        </div>
        <div class="section-title">Late reference section</div>
        <div class="description">
          {"Late content remains available after generic candidate selection. " * 30}
        </div>
      </div>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://portal.example/guides/complete",
        output_format="markdown",
    )

    assert document.extractor == "dom_structural"
    assert document.semantic_quality["status"] == "complete"
    assert "Late reference section" in document.content
    assert "Navigation" not in document.content


def test_short_semantic_root_does_not_beat_fuller_content_candidate() -> None:
    html = f"""
    <html><head><title>Complete maintenance manual</title></head><body>
      <main><h1>Summary</h1><p>{"Short overview. " * 24}</p></main>
      <div class="article-content">
        <h2>Complete maintenance manual</h2>
        <p>{"Detailed procedure, constraints, warnings, and reference data. " * 100}</p>
        <h2>Late troubleshooting section</h2>
        <p>{"Diagnostic steps and recovery details remain available. " * 80}</p>
      </div>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://docs.example/maintenance",
        output_format="markdown",
    )

    assert "Late troubleshooting section" in document.content
    assert len(document.content) > 4_000


def test_structural_candidate_trims_recommendation_tail_but_keeps_references() -> None:
    cards = "".join(
        f"<div class='card'><a href='/story-{index}'>Recommended story {index}</a></div>"
        for index in range(8)
    )
    html = f"""
    <html><head><title>Substantive report</title></head><body>
      <article>
        <h1>Substantive report</h1>
        <p>{"Verified reporting and detailed context for the main article. " * 50}</p>
        <section class="references">
          <h2>References</h2>
          <a href="/source-a">Primary source A</a>
          <a href="/source-b">Primary source B</a>
          <p>References support the claims in the article.</p>
        </section>
        <aside class="recommended selected-content">
          <h2>Recommended</h2>
          {cards}
        </aside>
      </article>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://news.example/report",
        output_format="markdown",
    )

    assert "Verified reporting and detailed context" in document.content
    assert "Primary source A" in document.content
    assert "References support the claims" in document.content
    assert "Recommended story" not in document.content


def test_inline_recommendation_module_does_not_delete_later_article_content() -> None:
    cards = "".join(
        f"<div><a href='/related-{index}'>Related item {index}</a></div>" for index in range(6)
    )
    html = f"""
    <html><head><title>Long report</title></head><body>
      <article>
        <h1>Long report</h1>
        <p>{"Opening analysis and verified evidence. " * 45}</p>
        <aside class="recommended">{cards}</aside>
        <h2>Later substantive findings</h2>
        <p>{"These findings must remain after the inline module. " * 35}</p>
        <section class="references">
          <h2>References</h2>
          <a href="/source">Primary source</a>
        </section>
      </article>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://news.example/long-report",
        output_format="markdown",
    )

    assert "Later substantive findings" in document.content
    assert "These findings must remain" in document.content
    assert "Primary source" in document.content


def test_substantive_trailing_aside_without_link_cards_is_preserved() -> None:
    html = f"""
    <html><head><title>Safety report</title></head><body>
      <article>
        <h1>Safety report</h1>
        <p>{"Detailed verified findings and operating context. " * 45}</p>
        <aside class="author-note" role="complementary">
          <h2>Important correction</h2>
          <p>This correction changes the interpretation of the final measurement.</p>
        </aside>
      </article>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://news.example/safety-report",
        output_format="markdown",
    )

    assert "Important correction" in document.content
    assert "changes the interpretation" in document.content


def test_survey_dialog_is_pruned_but_article_survey_prose_is_preserved() -> None:
    html = f"""
    <html><head><title>Survey methodology</title></head><body>
      <div class="modal survey-overlay" role="dialog" aria-modal="true">
        <p>Please participate in our site improvement survey.</p>
        <button>Begin Survey</button>
      </div>
      <main>
        <h1>Survey methodology</h1>
        <p>{"This article explains how a scientific survey measures public opinion. " * 20}</p>
      </main>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://research.example/survey-methodology",
        output_format="markdown",
    )

    assert "scientific survey measures public opinion" in document.content
    assert "Begin Survey" not in document.content
    assert "site improvement survey" not in document.content


def test_cleanup_removes_survey_cta() -> None:
    html = """
    <html><head><title>Developer guide</title></head><body><main>
      <h1>Developer guide</h1>
      <p>Substantive public documentation for implementing the integration safely.</p>
      <p>To help us improve this service, we'd like to know more about your visit today.
      Please fill in this survey.</p>
    </main></body></html>
    """
    document = extract_document(
        html,
        url="https://docs.example/developer-guide",
        output_format="markdown",
    )

    assert "fill in this survey" not in document.content


def test_dedent_fenced_code_blocks_preserves_internal_indentation() -> None:
    markdown = """* Example:

  ```python
  import asyncio

  async def main():
      await asyncio.sleep(1)
  ```
"""
    cleaned = _dedent_fenced_code_blocks(markdown)
    assert "\n```python\nimport asyncio\n" in cleaned
    assert "\nasync def main():\n    await asyncio.sleep(1)\n" in cleaned


def test_dedent_fenced_code_blocks_handles_nested_and_mixed_fences() -> None:
    markdown = """  ````markdown
  Outer example
  ```python
  print("nested")
  ```
  ~~~
  still content
  ~~~
  ````
"""
    cleaned = _dedent_fenced_code_blocks(markdown)
    assert cleaned.startswith("````markdown\nOuter example")
    assert '\n```python\nprint("nested")\n```\n' in cleaned
    assert "\n~~~\nstill content\n~~~\n````" in cleaned


def test_schema_article_body_is_cleaned_as_markdown_and_cta_removed() -> None:
    html = """
    <html><head><title>Government guidance</title>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Article","headline":"Government guidance",
     "articleBody":"<div class='govspeak'><h2>Details</h2><p>Public guidance body.</p></div>"}
    </script></head><body>
    <article><h1>Government guidance</h1><p>Public guidance body.</p>
    <p>Sign up for our newsletter</p></article></body></html>
    """
    document = extract_document(
        html,
        url="https://www.gov.uk/example",
        output_format="markdown",
    )
    assert "Public guidance body." in document.content
    assert "<div" not in document.content
    assert "Sign up for our newsletter" not in document.content


def test_extracts_schema_product_itemlist_as_bound_comparison_records() -> None:
    html = """
    <html><head><title>Phones</title>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
      {"@type":"ListItem","item":{"@type":"Product","name":"Phone Alpha",
       "url":"/alpha","brand":{"@type":"Brand","name":"Acme"},
       "offers":{"@type":"Offer","price":"31990","priceCurrency":"CZK",
                 "availability":"https://schema.org/InStock"},
       "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.8","reviewCount":"42"}}},
      {"@type":"Product","name":"Phone Beta","url":"/beta",
       "offers":{"@type":"Offer","lowPrice":"28990","priceCurrency":"CZK"}}
    ]}</script></head><body><main><p>Financing and legal boilerplate.</p></main></body></html>
    """
    document = extract_document(
        html,
        url="https://shop.example/phones",
        output_format="markdown",
    )
    assert document.extractor == "structured_commerce"
    assert document.page_type == "listing"
    assert len(document.commerce_items) == 2
    assert document.commerce_items[0]["price"] == "31990"
    assert document.commerce_items[0]["availability"] == "InStock"
    assert document.commerce_items[0]["rating"] == "4.8"
    assert document.commerce_items[0]["url"] == "https://shop.example/alpha"


def test_extracts_repeated_html_cards_without_detaching_price_and_link() -> None:
    html = """
    <html><head><title>Used cars</title></head><body>
      <div class="vehicle-card"><h2>Škoda Octavia</h2>
        <a href="/cars/octavia">Detail</a><p>2021 · 45 000 km · 499 000 Kč</p></div>
      <div class="vehicle-card"><h2>Volkswagen Golf</h2>
        <a href="/cars/golf">Detail</a><p>2020 · 61 000 km · 429 000 Kč</p></div>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://cars.example/listing",
        output_format="markdown",
    )
    assert document.page_type == "listing"
    assert [item["name"] for item in document.commerce_items] == [
        "Škoda Octavia",
        "Volkswagen Golf",
    ]
    assert document.commerce_items[0]["price"] == "499 000"
    assert document.commerce_items[0]["currency"] == "Kč"
    assert document.commerce_items[0]["url"] == "https://cars.example/cars/octavia"


def test_extracts_repeated_records_from_standard_next_state() -> None:
    html = """
    <html><head><title>Cars</title></head><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"cars":[
      {"title":"Volvo XC60","price":799000,"currency":"CZK","url":"/car/volvo-xc60"},
      {"title":"BMW X3","price":899000,"currency":"CZK","url":"/car/bmw-x3"}
    ]}}}
    </script></body></html>
    """
    document = extract_document(
        html,
        url="https://cars.example/inventory",
        output_format="markdown",
    )
    assert document.extractor == "structured_commerce"
    assert [item["name"] for item in document.commerce_items] == ["Volvo XC60", "BMW X3"]
    assert {item["price"] for item in document.commerce_items} == {"799000", "899000"}


def test_embedded_state_normalizes_exact_nested_major_unit_price() -> None:
    html = """
    <html><head><title>Offers</title></head><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"offers":[
      {"name":"Phone One","price":2649000,"url":"/one",
       "clickLogData":{"analytics":{"price":26490}}},
      {"name":"Phone Two","price":2859000,"url":"/two",
       "tracking":{"price":28590}}
    ]}}
    </script></body></html>
    """
    document = extract_document(
        html,
        url="https://shop.example/offers",
        output_format="markdown",
    )
    assert [item["price"] for item in document.commerce_items] == ["26490", "28590"]


def test_embedded_state_keeps_price_without_exact_nested_major_unit_match() -> None:
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {"offers":[
      {"name":"Phone One","price":2649000,"url":"/one",
       "tracking":{"price":26491}},
      {"name":"Phone Two","price":2859000,"url":"/two",
       "shipping":{"price":100}}
    ]}
    </script>
    """
    document = extract_document(
        html,
        url="https://shop.example/offers",
        output_format="markdown",
    )
    assert [item["price"] for item in document.commerce_items] == ["2649000", "2859000"]


def test_ignores_single_generic_pricing_object_and_unsafe_card_url() -> None:
    html = """
    <html><head><title>Article</title></head><body>
      <article><h1>Article</h1><p>This is ordinary substantive article prose.</p></article>
      <script type="application/json">
      {"plan":{"name":"Premium","price":20,"url":"/subscribe"}}
      </script>
      <div class="product-card"><h2>Unsafe One</h2><a href="javascript:alert(1)">x</a></div>
      <div class="product-card"><h2>Unsafe Two</h2><a href="data:text/plain,x">x</a></div>
    </body></html>
    """
    document = extract_document(
        html,
        url="https://example.com/article",
        output_format="markdown",
    )
    assert document.commerce_items == []
    assert document.extractor != "structured_commerce"


def test_preserves_lone_schema_product_detail() -> None:
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"One Product",
     "url":"/one","offers":{"@type":"Offer","price":"99","priceCurrency":"EUR"}}
    </script>
    """
    document = extract_document(
        html,
        url="https://shop.example/detail",
        output_format="markdown",
    )
    assert document.page_type == "detail"
    assert len(document.commerce_items) == 1
    assert document.commerce_items[0]["name"] == "One Product"


def test_fetch_formatter_exposes_metadata_and_media_anchors() -> None:
    document = extract_document(
        ARTICLE_HTML,
        url="https://example.com/news/oil-market",
        output_format="markdown",
    )

    result = build_fetch_tool_result(
        url=document.url,
        content=document.content,
        metadata={"extracted_document": document.as_dict()},
    )

    assert "[[metadata]]" in result.output
    assert "[[page:1]]" in result.output
    assert "[[media:1]]" in result.output
    assert "Published: 2026-04-27T12:00:00Z" in result.output
    assert 'artifact_read with artifact_id="tool_artifact:<tool_call_id>:media:1"' in result.output
    anchors = result.metadata.get("output_anchors") if result.metadata else None
    assert isinstance(anchors, list)
    assert {anchor["anchor"] for anchor in anchors} >= {"metadata", "page:1", "media:1"}
    media_anchor = next(anchor for anchor in anchors if anchor["anchor"] == "media:1")
    assert media_anchor["artifact_candidate"]["source_type"] == "remote_url"
    assert media_anchor["artifact_candidate"]["url"] == "https://cdn.example.com/jsonld-hero.jpg"
    stored = result.metadata.get("stored_output") if result.metadata else None
    assert isinstance(stored, str)
    assert "An oil tanker crosses a busy shipping route." in stored


def test_fetch_formatter_caps_noisy_metadata_and_uses_canonical_url() -> None:
    long_description = "noisy description " * 200
    long_caption = "long caption " * 200
    document = extract_document(
        ARTICLE_HTML,
        url="https://example.com/requested",
        output_format="markdown",
    )
    data = document.as_dict()
    data["canonical_url"] = "https://canonical.example/article"
    data["description"] = long_description
    data["images"] = [
        {
            "url": "https://cdn.example.com/very-long-image.jpg",
            "role": "hero",
            "alt": "very long alt text " * 200,
            "caption": long_caption,
            "source": "opengraph",
        }
    ]

    result = build_fetch_tool_result(
        url="https://example.com/requested",
        content=document.content,
        metadata={"extracted_document": data},
    )

    assert "URL: https://canonical.example/article" in result.output
    assert "Requested URL: https://example.com/requested" in result.output
    assert "[snippet truncated]" in result.output
    assert len(result.output) < 15_000
    assert (result.metadata or {}).get("source_url") == "https://canonical.example/article"
    assert (result.metadata or {}).get("requested_url") == "https://example.com/requested"


def test_extract_document_can_disable_media_metadata() -> None:
    document = extract_document(
        ARTICLE_HTML,
        url="https://example.com/news/oil-market",
        output_format="markdown",
        options={"include_media": "none"},
    )

    assert document.images == []
    assert document.title == "Oil markets reassess risk after conflict"


def test_reuters_adapter_competes_as_high_confidence_candidate() -> None:
    html = """
    <html><body>
      <nav>Most Popular Subscribe Advertisement Noise</nav>
      <article>
        <h1>Analysts reassess oil estimates</h1>
        <div data-testid="paragraph-0">Analysts revised oil price estimates after conflict disrupted markets.</div>
        <div data-testid="paragraph-1">The disruption raised concerns about shipping routes and supply risks.</div>
      </article>
    </body></html>
    """

    document = extract_document(
        html,
        url="https://www.reuters.com/business/energy/example-2026-04-27/",
        output_format="markdown",
    )

    assert document.extractor == "adapter_reuters"
    assert "Analysts revised oil price estimates" in document.content
    assert "Most Popular Subscribe" not in document.content
