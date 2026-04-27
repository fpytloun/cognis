"""Tests for structured web document extraction."""

from __future__ import annotations

from cognis.tools.executor.web.backends.formatting import build_fetch_tool_result
from cognis.tools.executor.web.extraction import extract_document

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
    anchors = result.metadata.get("output_anchors") if result.metadata else None
    assert isinstance(anchors, list)
    assert {anchor["anchor"] for anchor in anchors} >= {"metadata", "page:1", "media:1"}
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
