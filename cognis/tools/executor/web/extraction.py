"""Structured web document extraction.

The extractor intentionally combines several mature libraries instead of
trusting a single HTML-to-markdown pass.  Direct fetches and rendered browser
fetches both flow through this module so fallback mode benefits from the exact
same metadata, media, and body extraction logic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

logger = logging.getLogger(__name__)

_NOISE_TOKENS = (
    "subscribe",
    "sign up",
    "most popular",
    "advertisement",
    "cookie",
    "newsletter",
    "related articles",
    "share this",
)
_IMAGE_NOISE_TOKENS = (
    "logo",
    "icon",
    "sprite",
    "avatar",
    "tracking",
    "pixel",
    "badge",
    "spinner",
)
_ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "reportagenewsarticle"}
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class ExtractedImage:
    """Image/media candidate extracted from document metadata or body."""

    url: str
    role: str = "inline"
    alt: str | None = None
    caption: str | None = None
    width: int | None = None
    height: int | None = None
    source: str = "html"
    position: int | None = None

    def as_dict(self) -> dict[str, object | None]:
        return {
            "url": self.url,
            "role": self.role,
            "alt": self.alt,
            "caption": self.caption,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "position": self.position,
        }


@dataclass(slots=True)
class ExtractedDocument:
    """Structured result of extracting one web page."""

    url: str
    content: str
    output_format: str = "markdown"
    canonical_url: str | None = None
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    author: str | None = None
    published_at: str | None = None
    modified_at: str | None = None
    language: str | None = None
    extractor: str = "unknown"
    extraction_score: float = 0.0
    images: list[ExtractedImage] = field(default_factory=list)
    structured_data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "description": self.description,
            "site_name": self.site_name,
            "author": self.author,
            "published_at": self.published_at,
            "modified_at": self.modified_at,
            "language": self.language,
            "output_format": self.output_format,
            "extractor": self.extractor,
            "extraction_score": self.extraction_score,
            "images": [image.as_dict() for image in self.images],
            "structured_data": _structured_data_summary(self.structured_data),
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class _Candidate:
    source: str
    content: str
    score: float


def extract_document(
    html: str,
    *,
    url: str,
    output_format: str = "markdown",
    options: dict[str, Any] | None = None,
) -> ExtractedDocument:
    """Extract structured text, metadata, and media candidates from HTML."""

    options = options or {}
    output_format = output_format if output_format in {"markdown", "text", "html"} else "markdown"
    soup = BeautifulSoup(html, "html.parser")
    structured = _extract_structured_data(html, url=url)
    meta = _merge_metadata(soup, structured, url=url)
    include_media = str(options.get("include_media") or "metadata").lower()
    media_limit = _coerce_int(options.get("media_limit"), default=10, lo=0, hi=50)

    if output_format == "html":
        content = html
        extractor = "html"
        score = float(len(_html_to_text(html)))
    else:
        candidate = _best_body_candidate(html, soup=soup, url=url, metadata=meta)
        extractor = candidate.source
        score = candidate.score
        content = (
            candidate.content
            if output_format == "markdown"
            else _markdown_to_text(candidate.content)
        )

    images: list[ExtractedImage] = []
    if include_media != "none" and media_limit > 0:
        images = _extract_images(soup, structured, metadata=meta, url=url, limit=media_limit)

    return ExtractedDocument(
        url=url,
        content=content,
        output_format=output_format,
        canonical_url=meta.get("canonical_url"),
        title=meta.get("title"),
        description=meta.get("description"),
        site_name=meta.get("site_name"),
        author=meta.get("author"),
        published_at=meta.get("published_at"),
        modified_at=meta.get("modified_at"),
        language=meta.get("language"),
        extractor=extractor,
        extraction_score=score,
        images=images,
        structured_data=structured,
        warnings=[] if content.strip() else ["no_content_extracted"],
    )


def _extract_structured_data(html: str, *, url: str) -> dict[str, Any]:
    try:
        import extruct  # type: ignore[import-untyped]
        from w3lib.html import get_base_url

        base_url = get_base_url(html, url)
        extracted = extruct.extract(
            html,
            base_url=base_url,
            syntaxes=["json-ld", "microdata", "opengraph", "rdfa"],
            uniform=True,
        )
        return extracted if isinstance(extracted, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive around third-party parser
        logger.debug("web: structured metadata extraction failed: %s", type(exc).__name__)
        return {}


def _structured_data_summary(structured: dict[str, Any]) -> dict[str, object]:
    """Return small metadata about structured data without duplicating payloads."""

    summary: dict[str, object] = {}
    for key in ("json-ld", "microdata", "opengraph", "rdfa"):
        value = structured.get(key)
        if isinstance(value, list) and value:
            summary[key] = len(value)
    article = _find_article_entity(structured)
    article_type = _json_value_raw(article, "@type") or _json_value_raw(article, "type")
    if article_type:
        summary["article_type"] = article_type
    return summary


def _merge_metadata(
    soup: BeautifulSoup,
    structured: dict[str, Any],
    *,
    url: str,
) -> dict[str, str]:
    article = _find_article_entity(structured)
    metadata: dict[str, str] = {}

    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical is not None:
        href = _tag_attr(canonical, "href")
        if href:
            metadata["canonical_url"] = urljoin(url, href)

    html_tag = soup.find("html")
    lang = _tag_attr(html_tag, "lang") if html_tag is not None else None
    if lang:
        metadata["language"] = lang

    _set_first(metadata, "title", _json_value(article, "headline"), _json_value(article, "name"))
    _set_first(metadata, "description", _json_value(article, "description"))
    _set_first(metadata, "author", _author_value(_json_value_raw(article, "author")))
    _set_first(metadata, "published_at", _json_value(article, "datePublished"))
    _set_first(metadata, "modified_at", _json_value(article, "dateModified"))
    _set_first(metadata, "site_name", _publisher_value(_json_value_raw(article, "publisher")))

    _set_first(metadata, "title", _meta_value(soup, "property", "og:title"))
    _set_first(metadata, "description", _meta_value(soup, "property", "og:description"))
    _set_first(metadata, "site_name", _meta_value(soup, "property", "og:site_name"))
    _set_first(metadata, "published_at", _meta_value(soup, "property", "article:published_time"))
    _set_first(metadata, "modified_at", _meta_value(soup, "property", "article:modified_time"))
    _set_first(metadata, "author", _meta_value(soup, "property", "article:author"))

    _set_first(metadata, "title", _meta_value(soup, "name", "twitter:title"))
    _set_first(metadata, "description", _meta_value(soup, "name", "twitter:description"))
    _set_first(metadata, "description", _meta_value(soup, "name", "description"))
    _set_first(metadata, "author", _meta_value(soup, "name", "author"))

    if "title" not in metadata and soup.title and soup.title.string:
        _set_first(metadata, "title", soup.title.string)

    # Trafilatura's metadata extractor is a useful final fallback for dates/authors.
    try:
        import trafilatura

        bare: Any = trafilatura.bare_extraction(
            str(soup),
            url=url,
            with_metadata=True,
            include_comments=False,
        )
        bare_dict = bare.as_dict() if hasattr(bare, "as_dict") else bare
        if isinstance(bare_dict, dict):
            _set_first(metadata, "title", _coerce_str(bare_dict.get("title")))
            _set_first(metadata, "author", _coerce_str(bare_dict.get("author")))
            _set_first(metadata, "published_at", _coerce_str(bare_dict.get("date")))
            _set_first(metadata, "description", _coerce_str(bare_dict.get("description")))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("web: trafilatura metadata fallback failed: %s", type(exc).__name__)

    return metadata


def _best_body_candidate(
    html: str,
    *,
    soup: BeautifulSoup,
    url: str,
    metadata: dict[str, str],
) -> _Candidate:
    candidates: list[_Candidate] = []

    for source, kwargs in (
        ("trafilatura_precision", {"favor_precision": True}),
        ("trafilatura_recall", {"favor_recall": True}),
        ("trafilatura_default", {}),
    ):
        content = _trafilatura_markdown(html, url=url, **kwargs)
        if content:
            candidates.append(_Candidate(source, content, _score_content(content, metadata)))

    article_body = _article_body_from_structured(_extract_structured_data(html, url=url))
    if article_body:
        candidates.append(
            _Candidate(
                "schema_article_body", article_body, _score_content(article_body, metadata) + 200
            )
        )

    adapter = _site_adapter_markdown(soup, url=url)
    if adapter:
        candidates.append(
            _Candidate(
                adapter.source,
                adapter.content,
                _score_content(adapter.content, metadata) + adapter.score,
            )
        )

    readability = _readability_markdown(html)
    if readability:
        candidates.append(
            _Candidate("readability", readability, _score_content(readability, metadata))
        )

    fallback = _fallback_markdown(soup)
    if fallback:
        candidates.append(
            _Candidate("markdownify_body", fallback, _score_content(fallback, metadata) - 100)
        )

    if not candidates:
        return _Candidate("empty", "", 0.0)
    return max(candidates, key=lambda item: item.score)


def _trafilatura_markdown(html: str, *, url: str, **kwargs: Any) -> str | None:
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_comments=False,
            include_images=False,
            url=url,
            **kwargs,
        )
        return str(extracted).strip() if extracted and str(extracted).strip() else None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("web: trafilatura body extraction failed: %s", type(exc).__name__)
        return None


def _readability_markdown(html: str) -> str | None:
    try:
        from readability import Document  # type: ignore[import-untyped]

        summary = Document(html).summary(html_partial=True)
        converted = markdownify(summary, heading_style="ATX", strip=["script", "style"])
        return converted.strip() or None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("web: readability extraction failed: %s", type(exc).__name__)
        return None


def _site_adapter_markdown(soup: BeautifulSoup, *, url: str) -> _Candidate | None:
    """Return a high-confidence domain adapter candidate when available.

    Adapters are deliberately narrow and additive: they compete with the
    generic extractors instead of replacing them outright.
    """

    host = (urlparse(url).hostname or "").lower()
    if host.endswith("reuters.com"):
        return _reuters_candidate(soup)
    return None


def _reuters_candidate(soup: BeautifulSoup) -> _Candidate | None:
    selectors = (
        '[data-testid^="paragraph"]',
        '[data-testid="Body"] p',
        'article [data-testid*="paragraph"]',
        "article p",
    )
    paragraphs: list[str] = []
    for selector in selectors:
        paragraphs = [_clean_text(node.get_text(" ")) or "" for node in soup.select(selector)]
        paragraphs = [p for p in paragraphs if len(p) > 40]
        if len(paragraphs) >= 2:
            break
    if not paragraphs:
        return None
    title = soup.find("h1")
    lines: list[str] = []
    if title is not None:
        title_text = _clean_text(title.get_text(" "))
        if title_text:
            lines.append(f"# {title_text}")
            lines.append("")
    lines.extend(paragraphs)
    return _Candidate("adapter_reuters", "\n\n".join(lines), 300.0)


def _fallback_markdown(soup: BeautifulSoup) -> str | None:
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body = soup.body or soup
    converted = markdownify(str(body), heading_style="ATX", strip=["script", "style"])
    return converted.strip() or None


def _score_content(content: str, metadata: dict[str, str]) -> float:
    text = _markdown_to_text(content)
    normalized = text.lower()
    length = len(text)
    score = length * 0.2 if length < 120 else min(length, 20_000) * 0.08
    paragraphs = [p for p in re.split(r"\n\s*\n", content) if len(_markdown_to_text(p)) > 80]
    score += len(paragraphs) * 30
    noise_count = sum(normalized.count(token) for token in _NOISE_TOKENS)
    score -= noise_count * 45
    link_count = content.count("](")
    if length:
        score -= max(0, link_count - 20) * 8
    title = metadata.get("title")
    if title and _title_overlap(title, text) >= 0.35:
        score += 120
    return score


def _extract_images(
    soup: BeautifulSoup,
    structured: dict[str, Any],
    *,
    metadata: dict[str, str],
    url: str,
    limit: int,
) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    seen: set[str] = set()

    def add(raw_url: str | None, **kwargs: Any) -> None:
        if not raw_url:
            return
        absolute = urljoin(url, raw_url.strip())
        if not _image_url_allowed(absolute) or absolute in seen:
            return
        seen.add(absolute)
        images.append(ExtractedImage(url=absolute, **kwargs))

    article = _find_article_entity(structured)
    for raw in _image_values(_json_value_raw(article, "image")):
        add(raw, role="hero", source="jsonld")

    add(_meta_value(soup, "property", "og:image"), role="hero", source="opengraph")
    add(_meta_value(soup, "name", "twitter:image"), role="hero", source="twitter")

    for index, img in enumerate(soup.find_all("img"), start=1):
        src = _best_img_src(img)
        if not src:
            continue
        alt = _tag_attr(img, "alt")
        width = _coerce_optional_int(_tag_attr(img, "width"))
        height = _coerce_optional_int(_tag_attr(img, "height"))
        if width is not None and height is not None and (width < 80 or height < 80):
            continue
        figure = img.find_parent("figure")
        caption = None
        if figure is not None:
            figcaption = figure.find("figcaption")
            caption = _clean_text(figcaption.get_text(" ")) if figcaption is not None else None
        add(
            src,
            role="inline",
            alt=alt,
            caption=caption,
            width=width,
            height=height,
            source="html",
            position=index,
        )
        if len(images) >= limit:
            break

    # Promote the first media candidate to hero if metadata did not provide one.
    if images and not any(image.role == "hero" for image in images):
        images[0].role = "hero"
    return images[:limit]


def _find_article_entity(structured: dict[str, Any]) -> dict[str, Any]:
    for item in _iter_structured_entities(structured):
        raw_type = item.get("@type") or item.get("type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        normalized = {_type_name(t) for t in types if t}
        if normalized & _ARTICLE_TYPES:
            return item
    return {}


def _type_name(value: Any) -> str:
    raw = str(value).strip().lower()
    return raw.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _iter_structured_entities(structured: dict[str, Any]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for key in ("json-ld", "microdata", "rdfa"):
        raw_items = structured.get(key)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if isinstance(item, dict):
                entities.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    entities.extend([entry for entry in graph if isinstance(entry, dict)])
    return entities


def _article_body_from_structured(structured: dict[str, Any]) -> str | None:
    article = _find_article_entity(structured)
    body = _json_value(article, "articleBody")
    return body.strip() if body else None


def _meta_value(soup: BeautifulSoup, attr: str, name: str) -> str | None:
    tag = soup.find("meta", attrs={attr: name})
    if tag is None:
        return None
    return _tag_attr(tag, "content")


def _tag_attr(tag: Any, attr: str) -> str | None:
    value = tag.get(attr) if tag is not None and hasattr(tag, "get") else None
    return _clean_text(value) if isinstance(value, str) and value.strip() else None


def _json_value(obj: dict[str, Any], key: str) -> str | None:
    return _coerce_str(_json_value_raw(obj, key))


def _json_value_raw(obj: dict[str, Any], key: str) -> Any:
    if not isinstance(obj, dict):
        return None
    if key in obj:
        return obj[key]
    lower = key.lower()
    for candidate_key, value in obj.items():
        if str(candidate_key).lower() == lower:
            return value
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "headline", "text", "url"):
            result = _coerce_str(value.get(key))
            if result:
                return result
    if isinstance(value, list):
        for item in value:
            result = _coerce_str(item)
            if result:
                return result
    return None


def _author_value(value: Any) -> str | None:
    if isinstance(value, list):
        names = [_author_value(item) for item in value]
        return ", ".join(name for name in names if name) or None
    if isinstance(value, dict):
        return _coerce_str(value.get("name"))
    return _coerce_str(value)


def _publisher_value(value: Any) -> str | None:
    if isinstance(value, dict):
        return _coerce_str(value.get("name"))
    return _coerce_str(value)


def _image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result = _coerce_str(value.get("url") or value.get("contentUrl"))
        return [result] if result else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_image_values(item))
        return out
    return []


def _set_first(metadata: dict[str, str], key: str, *values: str | None) -> None:
    if key in metadata:
        return
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            metadata[key] = cleaned
            return


def _best_img_src(tag: Any) -> str | None:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = _tag_attr(tag, attr)
        if value:
            return value
    srcset = _tag_attr(tag, "srcset")
    if srcset:
        candidates = [part.strip().split(" ")[0] for part in srcset.split(",") if part.strip()]
        if candidates:
            return candidates[-1]
    return None


def _image_url_allowed(image_url: str) -> bool:
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    lowered = image_url.lower()
    return not any(token in lowered for token in _IMAGE_NOISE_TOKENS)


def _markdown_to_text(markdown: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_>#-]+", " ", text)
    return _clean_text(text) or ""


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return _clean_text(soup.get_text(" ")) or ""


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _WHITESPACE_RE.sub(" ", value).strip()
    return cleaned or None


def _title_overlap(title: str, text: str) -> float:
    title_words = {word for word in re.findall(r"[a-z0-9]+", title.lower()) if len(word) > 3}
    if not title_words:
        return 0.0
    text_words = set(re.findall(r"[a-z0-9]+", text.lower()[:3000]))
    return len(title_words & text_words) / len(title_words)


def _coerce_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
