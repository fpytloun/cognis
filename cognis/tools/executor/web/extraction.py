"""Structured web document extraction.

The extractor intentionally combines several mature libraries instead of
trusting a single HTML-to-markdown pass.  Direct fetches and rendered browser
fetches both flow through this module so fallback mode benefits from the exact
same metadata, media, and body extraction logic.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

from cognis.tools.executor.web.semantic_quality import (
    SemanticQuality,
    assess_semantic_quality,
    compare_candidates,
    url_provenance,
)

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
_COMMERCE_TYPES = {
    "product",
    "vehicle",
    "car",
    "motorcycle",
    "apartment",
    "house",
    "residence",
    "hotel",
    "lodgingbusiness",
    "offer",
}
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
    url_provenance: dict[str, object] = field(default_factory=dict)
    candidate_comparison: list[dict[str, object]] = field(default_factory=list)
    semantic_quality: dict[str, object] = field(default_factory=dict)
    commerce_items: list[dict[str, Any]] = field(default_factory=list)
    page_type: str | None = None

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
            "url_provenance": self.url_provenance,
            "candidate_comparison": self.candidate_comparison,
            "semantic_quality": self.semantic_quality,
            "commerce_items": self.commerce_items,
            "page_type": self.page_type,
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
    commerce_items = _extract_commerce_items(soup, structured, url=url)
    # Metadata, structured data, and commerce state are already materialized.
    # Reuse the parsed DOM for visible-body extraction instead of reparsing
    # multi-megabyte documents.
    content_soup = soup
    _prune_hidden_elements(content_soup)
    include_media = str(options.get("include_media") or "metadata").lower()
    if include_media == "none":
        _prune_media_elements(content_soup)
    content_html = str(content_soup)
    media_limit = _coerce_int(options.get("media_limit"), default=10, lo=0, hi=50)
    candidate_comparison: list[dict[str, object]] = []

    if output_format == "html":
        content = html
        extractor = "html"
        score = float(len(_html_to_text(html)))
    else:
        candidate, candidate_comparison = _best_body_candidate(
            content_html,
            soup=content_soup,
            url=url,
            metadata=meta,
            structured=structured,
        )
        if commerce_items:
            commerce_candidate = _commerce_candidate(commerce_items)
            if commerce_candidate.score > candidate.score:
                candidate = commerce_candidate
                candidate_comparison.append(
                    {
                        "source": commerce_candidate.source,
                        "score": commerce_candidate.score,
                        "length": len(commerce_candidate.content),
                        "selected": True,
                    }
                )
        extractor = candidate.source
        score = candidate.score
        cleaned_markdown = _clean_extracted_markdown(candidate.content)
        cleaned_markdown = _dedent_fenced_code_blocks(cleaned_markdown)
        content = (
            cleaned_markdown if output_format == "markdown" else _markdown_to_text(cleaned_markdown)
        )

    images: list[ExtractedImage] = []
    if include_media != "none" and media_limit > 0:
        images = _extract_images(
            content_soup,
            structured,
            metadata=meta,
            url=url,
            limit=media_limit,
        )

    quality_content = _html_to_text(content) if output_format == "html" else content
    quality = assess_semantic_quality(quality_content, title=meta.get("title"))
    if commerce_items:
        quality = replace(
            quality,
            status="complete",
            score=max(quality.score, 75.0),
            rank=4,
            signals=[*quality.signals, "structured_listing_items"],
        )
    if (
        len(content_soup.find_all("a")) >= 8
        and not content_soup.find(["article", "p"])
        and quality.status == "complete"
    ):
        if extractor in {"html", "raw_html", "fallback_markdown"}:
            quality = SemanticQuality(
                status="navigation_only",
                score=min(quality.score, 15.0),
                rank=1,
                signals=(*quality.signals, "link_dominant_markup"),
                words=quality.words,
                characters=quality.characters,
                paragraphs=quality.paragraphs,
                noise_hits=quality.noise_hits,
                blocked=False,
            )
        else:
            candidate_comparison.append(
                {
                    "source": "whole_page_link_density",
                    "status": "ignored",
                    "reason": "selected_candidate_is_authoritative",
                }
            )
    requested_url = str(options.get("requested_url") or url)
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
        url_provenance=url_provenance(
            requested_url, fetched_url=url, canonical_url=meta.get("canonical_url")
        ),
        candidate_comparison=candidate_comparison,
        semantic_quality=quality.as_dict(),
        commerce_items=commerce_items,
        page_type=(
            "listing" if len(commerce_items) > 1 else ("detail" if commerce_items else None)
        ),
    )


_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)"
    r"(?:\s*!important)?\s*(?:;|$)",
    re.IGNORECASE,
)


def _prune_hidden_elements(soup: BeautifulSoup) -> None:
    """Remove DOM branches that a browser would not expose as page content."""
    _decompose_bottom_up(list(soup.find_all(["template", "script", "style", "noscript"])))
    _decompose_bottom_up(list(soup.find_all(attrs={"hidden": True})))
    _decompose_bottom_up(list(soup.find_all(attrs={"aria-hidden": re.compile(r"^true$", re.I)})))
    hidden_by_style = [
        tag
        for tag in soup.find_all(style=True)
        if _HIDDEN_STYLE_RE.search(str(tag.get("style") or ""))
    ]
    _decompose_bottom_up(hidden_by_style)
    _prune_interstitial_overlays(soup)


_OVERLAY_IDENTITY_RE = re.compile(
    r"(?:^|[-_ ])(?:modal|dialog|overlay|popup|pop-up|interstitial|survey|feedback)(?:$|[-_ ])",
    re.IGNORECASE,
)
_OVERLAY_TEXT_RE = re.compile(
    r"\b(?:take|begin|complete|fill in|participate in).{0,80}\b(?:survey|questionnaire)\b|"
    r"\bhelp us improve\b|\bshare your feedback\b|"
    r"\bcreate your free account\b|\bunlock .{0,80}\bfeatures\b",
    re.IGNORECASE | re.DOTALL,
)


def _prune_interstitial_overlays(soup: BeautifulSoup) -> None:
    """Remove interactive survey/feedback overlays, not ordinary survey prose."""
    candidates = list(soup.select('[role="dialog"], [aria-modal="true"]'))
    candidates.extend(
        tag
        for tag in soup.find_all(["div", "aside", "section"])
        if _OVERLAY_IDENTITY_RE.search(_tag_identity(tag))
    )
    removable = []
    for tag in candidates:
        text = _clean_text(tag.get_text(" ")) or ""
        controls = len(tag.find_all(["button", "input", "select", "textarea"]))
        links = len(tag.find_all("a"))
        if _OVERLAY_TEXT_RE.search(text) and (controls > 0 or links > 0):
            removable.append(tag)
    _decompose_bottom_up(removable)


def _decompose_bottom_up(tags: list[Any]) -> None:
    """Decompose nested BeautifulSoup tags without invalidating later entries."""
    for tag in reversed(tags):
        if getattr(tag, "parent", None) is not None:
            tag.decompose()


def _prune_media_elements(soup: BeautifulSoup) -> None:
    """Remove visual media when the caller explicitly opted out of it."""
    for picture in list(soup.find_all("picture")):
        picture.decompose()
    for tag in list(soup.find_all(["img", "source"])):
        if tag.name == "img" and tag.find_parent("td", class_="ind") is not None:
            continue
        tag.decompose()


def _extract_commerce_items(
    soup: BeautifulSoup,
    structured: dict[str, Any],
    *,
    url: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(entity: dict[str, Any], *, source: str) -> None:
        if len(items) >= 100:
            return
        raw_type = entity.get("@type") or entity.get("type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        normalized = {_type_name(value) for value in types if value}
        if "offer" in normalized and isinstance(entity.get("itemOffered"), dict):
            offered = dict(entity["itemOffered"])
            offered["offers"] = entity
            add(offered, source=source)
            return
        if not normalized & _COMMERCE_TYPES:
            nested = entity.get("item")
            if isinstance(nested, dict):
                add(nested, source=source)
            return
        name = _bounded_text(_json_value(entity, "name") or _json_value(entity, "headline"), 300)
        item_url = _safe_item_url(
            url, _bounded_text(_json_value(entity, "url") or url, 2000) or url
        )
        key = ((name or "").lower(), item_url or "")
        if not name or not item_url or key in seen:
            return
        seen.add(key)
        offer = _first_mapping(entity.get("offers"))
        aggregate = _first_mapping(entity.get("aggregateRating"))
        brand = _first_mapping(entity.get("brand"))
        seller = _first_mapping((offer or {}).get("seller"))
        price = _json_value(offer, "price") or _json_value(offer, "lowPrice")
        currency = _json_value(offer, "priceCurrency")
        availability = _json_value(offer, "availability")
        if availability:
            availability = availability.rsplit("/", 1)[-1]
        properties: dict[str, str] = {}
        raw_properties = entity.get("additionalProperty")
        if isinstance(raw_properties, list):
            for prop in raw_properties:
                if not isinstance(prop, dict):
                    continue
                prop_name = _json_value(prop, "name")
                prop_value = _json_value(prop, "value")
                if prop_name and prop_value and len(properties) < 50:
                    properties[_bounded_text(prop_name, 120) or "Property"] = (
                        _bounded_text(prop_value, 500) or ""
                    )
        items.append(
            {
                "type": sorted(normalized & _COMMERCE_TYPES)[0],
                "name": name,
                "url": item_url,
                "description": _bounded_text(_json_value(entity, "description"), 4000),
                "brand": _bounded_text(_json_value(brand, "name"), 300),
                "sku": _bounded_text(
                    _json_value(entity, "sku") or _json_value(entity, "productID"), 200
                ),
                "price": _bounded_text(price, 100),
                "currency": _bounded_text(currency, 20),
                "availability": _bounded_text(availability, 100),
                "seller": _bounded_text(_json_value(seller, "name"), 300),
                "rating": _bounded_text(_json_value(aggregate, "ratingValue"), 30),
                "review_count": _bounded_text(
                    _json_value(aggregate, "reviewCount") or _json_value(aggregate, "ratingCount"),
                    30,
                ),
                "properties": properties,
                "source": source,
            }
        )

    for entity in _iter_structured_entities(structured):
        if len(items) >= 100:
            break
        raw_type = _type_name(entity.get("@type") or entity.get("type"))
        if raw_type == "itemlist":
            elements = entity.get("itemListElement")
            if isinstance(elements, list):
                for element in elements:
                    if len(items) >= 100:
                        break
                    if isinstance(element, dict):
                        add(element, source="jsonld_itemlist")
        add(entity, source="structured_data")

    if len(items) >= 2:
        return items[:100]

    card_selector = (
        '[itemtype*="schema.org/Product"], [itemtype*="schema.org/Vehicle"], '
        '[data-testid*="product-card"], [data-testid*="listing-card"], '
        '[class*="product-card"], [class*="listing-card"], [class*="vehicle-card"], '
        '[class~="c-product"], [class*="listing-card__container"]'
    )
    cards = soup.select(card_selector)
    if len(cards) < 2:
        return _extract_embedded_state_items(soup, url=url) or items
    price_re = re.compile(
        r"(?P<price>\d[\d\s.,]{1,14})\s*(?P<currency>Kč|CZK|EUR|€|USD|\$|GBP|£)",
        re.IGNORECASE,
    )
    for card in cards[:100]:
        heading = card.find(["h2", "h3", "h4"]) or card.find("a")
        name = _clean_text(heading.get_text(" ")) if heading else None
        link = card.find("a", href=True)
        text = _clean_text(card.get_text(" ")) or ""
        price_match = price_re.search(text)
        if not name or not link:
            continue
        item_url = _safe_item_url(url, str(link.get("href") or ""))
        if not item_url:
            continue
        key = (name.lower(), item_url)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "type": "listing",
                "name": name,
                "url": item_url,
                "description": text[:1000],
                "price": price_match.group("price").strip() if price_match else None,
                "currency": price_match.group("currency") if price_match else None,
                "properties": {},
                "source": "html_card",
            }
        )
    return items or _extract_embedded_state_items(soup, url=url)


def _extract_embedded_state_items(
    soup: BeautifulSoup,
    *,
    url: str,
) -> list[dict[str, Any]]:
    """Extract common listing records from standard Next/Nuxt JSON state."""
    scripts = [
        script
        for script in soup.find_all("script")
        if script.get("id") in {"__NEXT_DATA__", "__NUXT_DATA__"}
        or script.get("type") == "application/json"
    ]
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for script in scripts:
        raw = script.string or script.get_text()
        if not raw or len(raw) > 16 * 1024 * 1024:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack and len(items) < 100:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(reversed(value))
                continue
            if not isinstance(value, dict):
                continue
            stack.extend(value.values())
            name = next(
                (
                    str(value[key]).strip()
                    for key in ("name", "title", "displayName", "modelName")
                    if isinstance(value.get(key), str) and len(str(value[key]).strip()) >= 3
                ),
                "",
            )
            price_value = next(
                (
                    value[key]
                    for key in ("price", "priceValue", "salePrice", "currentPrice")
                    if isinstance(value.get(key), (str, int, float))
                ),
                None,
            )
            raw_url = next(
                (
                    str(value[key])
                    for key in ("url", "href", "path", "detailUrl")
                    if isinstance(value.get(key), str) and str(value[key]).strip()
                ),
                "",
            )
            record_id = (
                _bounded_text(value.get("id") or value.get("sku") or value.get("slug"), 200) or ""
            )
            if not name or price_value is None or (not raw_url and not record_id):
                continue
            item_url = (
                _safe_item_url(url, _bounded_text(raw_url, 2000) or "")
                if raw_url
                else f"{url}#item-{record_id}"
            )
            if not item_url:
                continue
            key = (name.lower(), item_url)
            if key in seen:
                continue
            seen.add(key)
            currency = value.get("currency") or value.get("priceCurrency")
            price_value = _normalize_embedded_price(value, price_value)
            availability = value.get("availability")
            if isinstance(availability, dict):
                availability = availability.get("type") or availability.get("name")
            properties = {
                key: _bounded_text(value[key], 500) or ""
                for key in (
                    "year",
                    "mileage",
                    "fuel",
                    "transmission",
                    "power",
                    "engine",
                    "location",
                )
                if value.get(key) not in (None, "")
            }
            items.append(
                {
                    "type": (_bounded_text(value.get("type"), 100) or "listing").lower(),
                    "name": _bounded_text(name, 300),
                    "url": item_url,
                    "description": _bounded_text(value.get("description"), 4000),
                    "brand": _bounded_text(value.get("brandName"), 300),
                    "price": _bounded_text(price_value, 100),
                    "currency": _bounded_text(currency, 20),
                    "availability": _bounded_text(availability, 100),
                    "seller": _bounded_text(value.get("sellerName"), 300),
                    "rating": _bounded_text(value.get("rating"), 30),
                    "review_count": _bounded_text(value.get("reviewCount"), 30),
                    "properties": properties,
                    "source": "embedded_state",
                }
            )
    return items if len(items) >= 2 else []


def _normalize_embedded_price(record: dict[str, Any], price_value: Any) -> Any:
    """Prefer an exact nested major-unit price over a top-level minor-unit value."""
    if not isinstance(price_value, int | float) or isinstance(price_value, bool):
        return price_value
    candidates: list[int | float] = []
    visits = 0
    stack: list[tuple[Any, int]] = [
        (nested, 1)
        for key, nested in record.items()
        if key not in {"price", "priceValue", "salePrice", "currentPrice"}
    ]
    while stack:
        visits += 1
        if visits > 10_000:
            break
        value, depth = stack.pop()
        if depth > 4:
            continue
        if isinstance(value, dict):
            for index, (key, nested) in enumerate(value.items()):
                if index >= 100:
                    break
                if (
                    key in {"price", "priceValue", "salePrice", "currentPrice"}
                    and isinstance(nested, int | float)
                    and not isinstance(nested, bool)
                ):
                    candidates.append(nested)
                elif isinstance(nested, dict | list):
                    stack.append((nested, depth + 1))
        elif isinstance(value, list):
            stack.extend(
                (nested, depth + 1) for nested in value[:100] if isinstance(nested, dict | list)
            )
    return next(
        (
            candidate
            for candidate in candidates
            if candidate != 0 and candidate * 100 == price_value
        ),
        price_value,
    )


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _safe_item_url(base_url: str, raw_url: str) -> str | None:
    absolute = urljoin(base_url, raw_url.strip())
    return absolute if urlparse(absolute).scheme in {"http", "https"} else None


def _commerce_candidate(items: list[dict[str, Any]]) -> _Candidate:
    lines = ["# Listings"]
    for index, item in enumerate(items, start=1):
        lines.extend(["", f"## Item {index}: {item['name']}"])
        for label, key in (
            ("Type", "type"),
            ("Brand", "brand"),
            ("Price", "price"),
            ("Currency", "currency"),
            ("Availability", "availability"),
            ("Seller", "seller"),
            ("Rating", "rating"),
            ("Review count", "review_count"),
            ("SKU", "sku"),
            ("URL", "url"),
        ):
            value = item.get(key)
            if value:
                lines.append(f"- {label}: {value}")
        for name, value in (item.get("properties") or {}).items():
            lines.append(f"- {name}: {value}")
        if item.get("description"):
            lines.extend(["", str(item["description"])])
    content = "\n".join(lines)
    return _Candidate("structured_commerce", content, 1800.0 + len(items) * 100)


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


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
    structured: dict[str, Any],
) -> tuple[_Candidate, list[dict[str, object]]]:
    candidates: list[_Candidate] = []

    adapter = _site_adapter_markdown(soup, url=url)
    if adapter:
        return adapter, [
            {
                "source": adapter.source,
                "status": "complete",
                "quality": "complete",
                "score": adapter.score,
                "rank": 4,
                "signals": ["domain_adapter"],
            }
        ]

    candidates.extend(_structural_dom_candidates(soup, metadata=metadata))

    for source, kwargs in (
        ("trafilatura_precision", {"favor_precision": True}),
        ("trafilatura_recall", {"favor_recall": True}),
        ("trafilatura_default", {}),
    ):
        content = _trafilatura_markdown(html, url=url, **kwargs)
        if content:
            candidates.append(_Candidate(source, content, _score_content(content, metadata)))

    article_body = _article_body_from_structured(structured)
    if article_body:
        candidates.append(
            _Candidate(
                "schema_article_body", article_body, _score_content(article_body, metadata) + 200
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
        return _Candidate("empty", "", 0.0), []
    raw_candidates = [
        {"source": item.source, "content": item.content, "score": item.score} for item in candidates
    ]
    winner, comparison = compare_candidates(raw_candidates)
    winner_source = str(winner.get("source")) if winner else candidates[0].source
    selected = next(item for item in candidates if item.source == winner_source)
    if selected.source.startswith("dom_structural:"):
        selected = _Candidate("dom_structural", selected.content, selected.score)
    return selected, comparison


_STRUCTURAL_IDENTITY_RE = re.compile(
    r"(?:^|[-_ ])(?:article|content|description|details|documentation|guide|main|"
    r"post|sections?|story|body)(?:$|[-_ ])",
    re.IGNORECASE,
)
_HEADING_IDENTITY_RE = re.compile(
    r"(?:^|[-_ ])(?:heading|headline|section-title|subtitle|title)(?:$|[-_ ])",
    re.IGNORECASE,
)
_TABLE_IDENTITY_RE = re.compile(r"(?:^|[-_ ])(?:table|grid)(?:$|[-_ ])", re.IGNORECASE)


def _structural_dom_candidates(
    soup: BeautifulSoup,
    *,
    metadata: dict[str, str],
) -> list[_Candidate]:
    """Return bounded, high-density visible DOM roots as generic candidates."""
    ranked: list[tuple[float, Any]] = []
    seen_nodes: set[int] = set()
    page_text_length = max(len(_clean_text(soup.get_text(" ")) or ""), 1)
    roots: list[Any] = list(soup.select("article, main, [role='main']"))
    for tag in soup.find_all(["div", "section"]):
        identity = _tag_identity(tag)
        direct_children = tag.find_all(recursive=False)
        if _STRUCTURAL_IDENTITY_RE.search(identity) or len(direct_children) >= 4:
            roots.append(tag)

    for root in roots:
        if id(root) in seen_nodes:
            continue
        seen_nodes.add(id(root))
        text = _clean_text(root.get_text(" ")) or ""
        if len(text) < 300:
            continue
        link_text = sum(len(_clean_text(link.get_text(" ")) or "") for link in root.find_all("a"))
        link_ratio = link_text / max(len(text), 1)
        headings = len(root.find_all(["h1", "h2", "h3", "h4"]))
        blocks = len(root.find_all(["p", "li", "pre", "code", "table", "blockquote"]))
        repeated = len(root.find_all(recursive=False))
        identity = _tag_identity(root)
        identity_match = bool(_STRUCTURAL_IDENTITY_RE.search(identity))
        if link_ratio > 0.7 and headings == 0 and blocks < 3 and not identity_match:
            continue
        scope_ratio = min(len(text) / page_text_length, 1.0)
        structural_score = min(400.0, scope_ratio * 800)
        structural_score += headings * 80 + min(blocks, 100) * 12
        structural_score += min(repeated, 50) * 4
        structural_score -= link_ratio * (300 if identity_match else 700)
        structural_score -= min(len(root.find_all("a")), 100) * (2 if identity_match else 8)
        if root.name in {"article", "main"} or root.get("role") == "main":
            structural_score += 400
        elif identity_match:
            structural_score += 300
        if scope_ratio < 0.2:
            structural_score -= 300
        ranked.append((structural_score, root))

    candidates: list[_Candidate] = []
    seen_content: set[str] = set()
    for structural_score, root in sorted(ranked, key=lambda item: item[0], reverse=True)[:8]:
        document = BeautifulSoup(str(root), "html.parser")
        _trim_trailing_link_modules(document)
        _normalize_structural_markup(document)
        rendered = markdownify(
            str(document),
            heading_style="ATX",
            strip=["script", "style", "noscript"],
        ).strip()
        normalized_text = _markdown_to_text(rendered)
        if len(normalized_text) < 300:
            continue
        fingerprint = normalized_text[:2000]
        if fingerprint in seen_content:
            continue
        seen_content.add(fingerprint)
        leading_links = rendered[:1000].count("](")
        candidates.append(
            _Candidate(
                f"dom_structural:{len(candidates) + 1}",
                rendered,
                _score_content(rendered, metadata) + structural_score - leading_links * 100,
            )
        )
        if len(candidates) >= 4:
            break
    return candidates


_TRAILING_MODULE_RE = re.compile(
    r"(?:^|[-_ ])(?:related|recommended|most-read|popular|more-stories|"
    r"editor-picks|selected-content|suggested)(?:$|[-_ ])",
    re.IGNORECASE,
)
_PRESERVED_LINK_SECTION_RE = re.compile(
    r"\b(?:references|sources|bibliography|citations|footnotes)\b",
    re.IGNORECASE,
)


def _trim_trailing_link_modules(soup: BeautifulSoup) -> int:
    """Remove link-card tails after substantial content, preserving references."""
    removed = 0
    containers = [soup, *soup.find_all(["article", "main", "div", "section"])]
    for container in containers:
        children = [
            child for child in container.find_all(recursive=False) if getattr(child, "name", None)
        ]
        if len(children) < 3:
            continue
        substantive_chars = 0
        for index, child in enumerate(children):
            text = _clean_text(child.get_text(" ")) or ""
            if not text:
                continue
            identity = _tag_identity(child)
            heading = child.find(["h1", "h2", "h3", "h4"])
            heading_text = _clean_text(heading.get_text(" ")) if heading is not None else ""
            if _PRESERVED_LINK_SECTION_RE.search(f"{identity} {heading_text or ''}"):
                substantive_chars += len(text)
                continue
            links = child.find_all("a")
            link_text = sum(len(_clean_text(link.get_text(" ")) or "") for link in links)
            link_ratio = link_text / max(len(text), 1)
            direct_cards = [
                node for node in child.find_all(recursive=False) if getattr(node, "name", None)
            ]
            average_card_text = len(text) / max(len(direct_cards), 1)
            semantic_container = (
                child.name in {"aside", "nav"}
                or child.get("role") in {"complementary", "navigation"}
                or bool(_TRAILING_MODULE_RE.search(identity))
            )
            semantic_tail = semantic_container and len(links) >= 2 and link_ratio >= 0.25
            repeated_link_cards = (
                len(direct_cards) >= 4
                and len(links) >= 4
                and link_ratio >= 0.45
                and average_card_text <= 250
            )
            if substantive_chars >= 800 and (semantic_tail or repeated_link_cards):
                removable_tail = True
                for tail in children[index:]:
                    tail_text = _clean_text(tail.get_text(" ")) or ""
                    tail_identity = _tag_identity(tail)
                    tail_heading = tail.find(["h1", "h2", "h3", "h4"])
                    tail_heading_text = (
                        _clean_text(tail_heading.get_text(" ")) if tail_heading is not None else ""
                    )
                    if _PRESERVED_LINK_SECTION_RE.search(
                        f"{tail_identity} {tail_heading_text or ''}"
                    ):
                        removable_tail = False
                        break
                    tail_links = tail.find_all("a")
                    tail_link_text = sum(
                        len(_clean_text(link.get_text(" ")) or "") for link in tail_links
                    )
                    tail_ratio = tail_link_text / max(len(tail_text), 1)
                    tail_semantic_container = (
                        tail.name in {"aside", "nav"}
                        or tail.get("role") in {"complementary", "navigation"}
                        or bool(_TRAILING_MODULE_RE.search(tail_identity))
                    )
                    tail_semantic = (
                        tail_semantic_container and len(tail_links) >= 2 and tail_ratio >= 0.25
                    )
                    tail_cards = [
                        node
                        for node in tail.find_all(recursive=False)
                        if getattr(node, "name", None)
                    ]
                    tail_repeated = (
                        len(tail_cards) >= 4
                        and len(tail_links) >= 4
                        and tail_ratio >= 0.45
                        and len(tail_text) / max(len(tail_cards), 1) <= 250
                    )
                    if not (tail_semantic or tail_repeated):
                        removable_tail = False
                        break
                if removable_tail:
                    for tail in children[index:]:
                        tail.decompose()
                        removed += 1
                    break
            substantive_chars += len(text)
    return removed


def _tag_identity(tag: Any) -> str:
    identity = " ".join(
        [
            str(tag.get("id") or ""),
            " ".join(str(value) for value in (tag.get("class") or [])),
        ]
    )
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", identity)


def _normalize_structural_markup(soup: BeautifulSoup) -> None:
    """Normalize conservative heading and pseudo-table structures."""
    for container in reversed(list(soup.find_all(["div", "section"]))):
        _normalize_pseudo_table(container)
    for tag in soup.find_all(["div", "span"]):
        text = _clean_text(tag.get_text(" ")) or ""
        if (
            _HEADING_IDENTITY_RE.search(_tag_identity(tag))
            and 1 <= len(text) <= 160
            and not tag.find(["p", "table", "ul", "ol", "pre"])
        ):
            tag.name = "h2"
            tag.attrs = {}


def _normalize_pseudo_table(container: Any) -> bool:
    if not _TABLE_IDENTITY_RE.search(_tag_identity(container)):
        return False
    rows = [child for child in container.find_all(recursive=False) if getattr(child, "name", None)]
    if len(rows) < 3:
        return False
    cell_rows = [
        [child for child in row.find_all(recursive=False) if getattr(child, "name", None)]
        for row in rows
    ]
    widths = [len(cells) for cells in cell_rows]
    if not widths or not 2 <= widths[0] <= 8 or any(width != widths[0] for width in widths):
        return False
    if any(
        len(_clean_text(cell.get_text(" ")) or "") > 1000 for cells in cell_rows for cell in cells
    ):
        return False
    header_signal = any(
        re.search(
            r"(?:^|[-_ ])(?:th|header|heading)(?:$|[-_ ])",
            " ".join(str(value) for value in (cell.get("class") or [])),
            re.IGNORECASE,
        )
        for cell in cell_rows[0]
    )
    if not header_signal:
        return False
    container.name = "table"
    container.attrs = {}
    for row_index, (row, cells) in enumerate(zip(rows, cell_rows, strict=True)):
        row.name = "tr"
        row.attrs = {}
        for cell in cells:
            cell.name = "th" if row_index == 0 else "td"
            cell.attrs = {}
    return True


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
    """Return bounded domain adapters for stable public document structures."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("reuters.com"):
        return _reuters_candidate(soup)
    if host in {"rfc-editor.org", "www.rfc-editor.org"}:
        return _rfc_editor_candidate(soup)
    if (
        host == "news.ycombinator.com"
        and parsed.path == "/item"
        and bool(parse_qs(parsed.query).get("id"))
    ):
        return _hacker_news_candidate(soup)
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
    return _Candidate("adapter_reuters", "\n\n".join(lines), 1000.0)


def _rfc_editor_candidate(soup: BeautifulSoup) -> _Candidate | None:
    body = soup.body
    if body is None or body.find("h1", id="rfcnum") is None:
        return None
    document = BeautifulSoup(str(body), "html.parser")
    for selector in (
        "script",
        "style",
        ".ears",
        ".document-information",
        "#status-of-memo",
        "#copyright",
        "#toc",
    ):
        for node in list(document.select(selector)):
            node.decompose()
    for node in list(document.find_all(["section", "div"])):
        heading = node.find(["h1", "h2"], recursive=False)
        heading_text = _clean_text(heading.get_text(" ")) if heading is not None else None
        if heading_text and re.fullmatch(r"(?:appendix\s+[a-z0-9.]+\s*)?index", heading_text, re.I):
            node.decompose()
    content = markdownify(
        str(document.body or document),
        heading_style="ATX",
        strip=["script", "style"],
    ).strip()
    if len(_markdown_to_text(content)) < 10_000:
        return None
    return _Candidate("adapter_rfc_editor", content, 2000.0)


def _hacker_news_candidate(soup: BeautifulSoup) -> _Candidate | None:
    story = soup.select_one("tr.athing")
    title_link = story.select_one(".titleline > a") if story is not None else None
    if story is None or title_link is None:
        return None
    title = _clean_text(title_link.get_text(" "))
    href = _tag_attr(title_link, "href")
    if not title:
        return None

    lines = [f"# {title}"]
    if href:
        lines.extend(["", f"Original submission: {href}"])
    subtext = story.find_next_sibling("tr")
    if subtext is not None:
        points = subtext.select_one(".score")
        author = subtext.select_one(".hnuser")
        age = subtext.select_one(".age")
        details = [
            _clean_text(points.get_text(" ")) if points else None,
            f"by {_clean_text(author.get_text(' '))}" if author else None,
            _clean_text(age.get_text(" ")) if age else None,
        ]
        details = [detail for detail in details if detail]
        if details:
            lines.extend(["", " · ".join(details)])

    story_body = None
    for sibling in story.find_next_siblings("tr", limit=6):
        if "comtr" in (sibling.get("class") or []):
            break
        story_body = sibling.select_one(".toptext")
        if story_body is not None:
            break
    if story_body is not None:
        rendered = markdownify(str(story_body), heading_style="ATX").strip()
        if rendered:
            lines.extend(["", rendered])

    comments: list[str] = []
    for row in soup.select("tr.comtr"):
        comment = row.select_one(".commtext")
        if comment is None:
            continue
        rendered = markdownify(str(comment), heading_style="ATX").strip()
        if not rendered:
            continue
        author = row.select_one(".hnuser")
        age = row.select_one(".age")
        byline = _clean_text(author.get_text(" ")) if author else "unknown"
        if age is not None:
            age_text = _clean_text(age.get_text(" "))
            if age_text:
                byline = f"{byline} · {age_text}"
        indent = row.select_one("td.ind img[width]")
        depth = _coerce_optional_int(_tag_attr(indent, "width")) or 0
        comments.extend([f"### {byline} · depth {depth // 40}", "", rendered])
    if comments:
        lines.extend(["", "## Discussion", "", *comments])
    if len(lines) < 4:
        return None
    return _Candidate("adapter_hacker_news", "\n".join(lines), 1800.0)


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
    if not body:
        return None
    body = body.strip()
    if re.search(r"<[A-Za-z][^>]*>", body):
        return markdownify(body, heading_style="ATX", strip=["script", "style"]).strip()
    return body


def _clean_extracted_markdown(content: str) -> str:
    """Remove bounded promotional/CTA blocks without rewriting article prose."""
    blocks = re.split(r"\n\s*\n", content)
    cleaned: list[str] = []
    cta = re.compile(
        r"\b(?:sign up for|subscribe to|join (?:our|my) newsletter|"
        r"get (?:our|my) newsletter|follow me on|my free tools|"
        r"fill in this survey|participate in .{0,80}\bsurvey|"
        r"help us improve .{0,80}\bvisit today)\b",
        re.IGNORECASE,
    )
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if len(stripped) <= 400 and cta.search(stripped):
            continue
        cleaned.append(stripped)
    return "\n\n".join(cleaned)


def _dedent_fenced_code_blocks(content: str) -> str:
    """Make fenced code copyable when HTML conversion nested it under a list."""
    lines = content.splitlines()
    output: list[str] = []
    fence_indent: int | None = None
    fence_marker: str | None = None
    fence_length = 0
    for line in lines:
        stripped = line.lstrip(" ")
        opening = re.match(r"^(`{3,}|~{3,})", stripped) if fence_indent is None else None
        if opening is not None:
            fence_indent = len(line) - len(stripped)
            fence_marker = opening.group(1)[0]
            fence_length = len(opening.group(1))
            output.append(stripped)
            continue
        if fence_indent is not None:
            closing = re.fullmatch(
                rf"{re.escape(fence_marker or '')}{{{fence_length},}}\s*",
                stripped,
            )
            if closing is not None:
                output.append(stripped)
                fence_indent = None
                fence_marker = None
                fence_length = 0
                continue
            remove = min(fence_indent, len(line) - len(line.lstrip(" ")))
            output.append(line[remove:])
            continue
        output.append(line)
    return "\n".join(output)


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
