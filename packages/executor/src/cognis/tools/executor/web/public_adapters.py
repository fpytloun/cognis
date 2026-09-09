"""Stable anonymous public-web adapters used before generic direct fetch."""

from __future__ import annotations

import asyncio
import html
import multiprocessing
import queue
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from bs4 import BeautifulSoup
from markdown import markdown  # type: ignore[import-untyped]
from markdownify import markdownify

from cognis.models.tool import ToolResult

Request = Callable[..., Awaitable[httpx.Response]]
_PUBLIC_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
_PUBLIC_ADAPTER_TIMEOUT_SECONDS = 5.0
_YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS = 4.0
_TRANSCRIPT_CHUNK_SECONDS = 5 * 60


async def dispatch_public_adapter(
    url: str,
    *,
    timeout: int,
    output_format: str,
    options: dict[str, Any] | None = None,
    request: Request | None = None,
) -> ToolResult | None:
    """Dispatch a supported public URL, returning ``None`` on adapter failure."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        adapter_timeout = min(float(timeout), _PUBLIC_ADAPTER_TIMEOUT_SECONDS)
        if host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
        } and _youtube_video_id(parsed):
            return _format_result(
                await _youtube(
                    url,
                    timeout=max(1, int(adapter_timeout)),
                    request=request,
                ),
                output_format,
                options=options,
            )
        async with asyncio.timeout(adapter_timeout):
            if host == "github.com" and _github_repo_path(parsed.path):
                result = await _github(url, parsed.path, timeout=timeout, request=request)
                return _format_result(result, output_format, options=options)
            if host == "stackoverflow.com" and (
                match := re.fullmatch(r"/questions/(\d+)(?:/.*)?", parsed.path)
            ):
                result = await _stackoverflow(
                    url,
                    match.group(1),
                    timeout=timeout,
                    request=request,
                )
                return _format_result(result, output_format, options=options)
            if host == "pubmed.ncbi.nlm.nih.gov" and (
                match := re.fullmatch(r"/(\d+)/?", parsed.path)
            ):
                result = await _pubmed(match.group(1), timeout=timeout, request=request)
                return _format_result(result, output_format, options=options)
            if host in {"arxiv.org", "www.arxiv.org"} and (
                match := re.fullmatch(r"/abs/([^/]+)", parsed.path)
            ):
                result = await _arxiv(match.group(1), timeout=timeout, request=request)
                return _format_result(result, output_format, options=options)
    except (TimeoutError, ET.ParseError, KeyError, TypeError, ValueError, httpx.HTTPError):
        return None
    return None


def _format_result(
    result: ToolResult,
    output_format: str,
    *,
    options: dict[str, Any] | None = None,
) -> ToolResult:
    metadata = dict(result.metadata or {})
    metadata["output_format"] = output_format
    source_output = result.output
    if str((options or {}).get("include_media") or "metadata").lower() == "none":
        source_output = _strip_markdown_media(source_output)
    if output_format == "markdown":
        return result.model_copy(update={"output": source_output, "metadata": metadata})
    rendered_html = markdown(source_output, extensions=["fenced_code", "tables"])
    if output_format == "html":
        output = rendered_html
    else:
        output = BeautifulSoup(rendered_html, "html.parser").get_text("\n", strip=True)
    return result.model_copy(update={"output": output, "metadata": metadata})


def _strip_markdown_media(content: str) -> str:
    """Remove image-only Markdown/HTML while preserving surrounding prose."""
    parts = _markdown_code_regions(content)
    stripped = [value if is_code else _strip_media_from_prose(value) for is_code, value in parts]
    return re.sub(r"\n{3,}", "\n\n", "".join(stripped)).strip()


def _strip_media_from_prose(content: str) -> str:
    content = re.sub(r"<picture\b[^>]*>.*?</picture\s*>", "", content, flags=re.I | re.S)
    content = re.sub(
        r"<a\b[^>]*>\s*<img\b[^>]*?/?>\s*</a\s*>",
        "",
        content,
        flags=re.I | re.S,
    )
    content = re.sub(r"<(?:img|source)\b[^>]*?/?>", "", content, flags=re.I | re.S)
    content = _strip_markdown_image_syntax(content)
    content = re.sub(r"<p\b[^>]*>\s*</p\s*>", "", content, flags=re.I)
    return content


def _markdown_code_regions(content: str) -> list[tuple[bool, str]]:
    """Partition Markdown while preserving fenced and inline code verbatim."""
    regions: list[tuple[bool, str]] = []
    prose_start = 0
    index = 0
    while index < len(content):
        line_start = index == 0 or content[index - 1] == "\n"
        if line_start:
            opening = re.match(r"[ \t]{0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)", content[index:])
            if opening is not None:
                marker = opening.group(1)[0]
                length = len(opening.group(1))
                cursor = index + opening.end()
                end = len(content)
                while cursor < len(content):
                    line_end = content.find("\n", cursor)
                    line_end = len(content) if line_end < 0 else line_end
                    line = content[cursor:line_end]
                    if re.fullmatch(rf"[ \t]{{0,3}}{re.escape(marker)}{{{length},}}[ \t]*", line):
                        end = line_end + (1 if line_end < len(content) else 0)
                        break
                    cursor = line_end + 1
                if prose_start < index:
                    regions.append((False, content[prose_start:index]))
                regions.append((True, content[index:end]))
                index = end
                prose_start = end
                continue
        if content[index] == "`":
            run_end = index
            while run_end < len(content) and content[run_end] == "`":
                run_end += 1
            marker = content[index:run_end]
            closing = content.find(marker, run_end)
            if closing >= 0:
                end = closing + len(marker)
                if prose_start < index:
                    regions.append((False, content[prose_start:index]))
                regions.append((True, content[index:end]))
                index = end
                prose_start = end
                continue
        index += 1
    if prose_start < len(content):
        regions.append((False, content[prose_start:]))
    return regions


def _strip_markdown_image_syntax(content: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(content):
        if not content.startswith("![", index):
            output.append(content[index])
            index += 1
            continue
        label_end = _balanced_end(content, index + 1, "[", "]")
        if label_end is None or label_end + 1 >= len(content) or content[label_end + 1] != "(":
            output.append(content[index])
            index += 1
            continue
        destination_end = _balanced_end(content, label_end + 1, "(", ")")
        if destination_end is None:
            output.append(content[index])
            index += 1
            continue
        index = destination_end + 1
    return "".join(output)


def _balanced_end(content: str, start: int, opening: str, closing: str) -> int | None:
    if start >= len(content) or content[start] != opening:
        return None
    depth = 0
    escaped = False
    for index in range(start, len(content)):
        char = content[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        elif char == "\n" and depth == 0:
            return None
    return None


def _github_repo_path(path: str) -> tuple[str, str] | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 or (
        len(parts) >= 5 and parts[2] == "blob" and parts[-1].lower().startswith("readme")
    ):
        return parts[0], parts[1]
    return None


async def _github(url: str, path: str, *, timeout: int, request: Request | None) -> ToolResult:
    owner, repo = _github_repo_path(path) or ("", "")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 5 and parts[2] == "blob":
        ref = quote(parts[3], safe="")
        readme_path = "/".join(quote(part, safe="") for part in parts[4:])
        endpoint = (
            f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/{ref}/{readme_path}"
        )
        headers = {**_PUBLIC_HEADERS, "Accept": "text/plain"}
    else:
        endpoint = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/readme"
        headers = {
            **_PUBLIC_HEADERS,
            "Accept": "application/vnd.github.raw+json",
        }
    response = await _get(
        endpoint,
        timeout=timeout,
        request=request,
        headers=headers,
    )
    return _result(
        response.text,
        source_url=endpoint,
        requested_url=url,
        adapter="github_readme",
        status="complete",
        semantic_type="repository_readme",
    )


async def _stackoverflow(
    requested_url: str,
    question_id: str,
    *,
    timeout: int,
    request: Request | None,
) -> ToolResult:
    base = "https://api.stackexchange.com/2.3"
    params = {"site": "stackoverflow", "filter": "withbody", "pagesize": "1"}
    question_response = await _get(
        f"{base}/questions/{question_id}", timeout=timeout, request=request, params=params
    )
    answers_response = await _get(
        f"{base}/questions/{question_id}/answers",
        timeout=timeout,
        request=request,
        params={"site": "stackoverflow", "filter": "withbody", "pagesize": "10", "sort": "votes"},
    )
    question = (question_response.json().get("items") or [None])[0]
    if not isinstance(question, dict):
        raise ValueError("Stack Exchange question missing")
    lines = [_post_markdown("Question", question)]
    answers = answers_response.json().get("items") or []
    for index, answer in enumerate(answers, start=1):
        if isinstance(answer, dict):
            lines.extend(["", _post_markdown(f"Answer {index}", answer)])
    return _result(
        "\n".join(lines),
        source_url=f"{base}/questions/{question_id}",
        requested_url=requested_url,
        adapter="stackoverflow_api",
        status="complete",
        semantic_type="question_answers",
        extra={"question_id": int(question_id), "answer_count": len(answers)},
    )


def _post_markdown(label: str, post: dict[str, Any]) -> str:
    title = str(post.get("title") or label)
    body = markdownify(
        html.unescape(str(post.get("body") or "")),
        heading_style="ATX",
        strip=["script", "style"],
    ).strip()
    owner = post.get("owner") or {}
    author = owner.get("display_name") if isinstance(owner, dict) else None
    details = [f"## {label}: {title}", f"Author: {author or 'unknown'}"]
    if label.startswith("Answer"):
        details.append(f"Accepted: {'yes' if post.get('is_accepted') else 'no'}")
    details.append(f"Votes: {post.get('score', 0)}")
    details.extend(["", body])
    return "\n".join(details).strip()


async def _pubmed(pmid: str, *, timeout: int, request: Request | None) -> ToolResult:
    endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    response = await _get(
        endpoint,
        timeout=timeout,
        request=request,
        params={"db": "pubmed", "id": pmid, "retmode": "xml"},
    )
    root = ET.fromstring(response.text)
    article = root.find(".//PubmedArticle")
    if article is None:
        raise ValueError("PubMed article missing")
    title = _xml_text(article.find(".//ArticleTitle"))
    abstract_sections: list[str] = []
    for node in article.findall(".//AbstractText"):
        text = _xml_text(node)
        if not text:
            continue
        label = str(node.attrib.get("Label") or "").strip()
        abstract_sections.append(f"**{label}:** {text}" if label else text)
    journal = _xml_text(article.find(".//Journal/Title"))
    authors: list[str] = []
    for author in article.findall(".//Author"):
        collective = _xml_text(author.find("CollectiveName"))
        given = _xml_text(author.find("ForeName"))
        family = _xml_text(author.find("LastName"))
        name = collective or " ".join(part for part in (given, family) if part)
        if name:
            authors.append(name)
    doi = next(
        (
            _xml_text(node)
            for node in article.findall(".//ArticleId")
            if str(node.attrib.get("IdType") or "").lower() == "doi"
        ),
        "",
    )
    published = " ".join(
        part
        for part in (
            _xml_text(article.find(".//PubDate/Year")),
            _xml_text(article.find(".//PubDate/Month")),
            _xml_text(article.find(".//PubDate/Day")),
        )
        if part
    )
    lines = [
        f"# {title}",
        f"PMID: {pmid}",
        f"Authors: {', '.join(authors) or 'unknown'}",
        f"Journal: {journal or 'unknown'}",
    ]
    if published:
        lines.append(f"Published: {published}")
    if doi:
        lines.append(f"DOI: {doi}")
    lines.extend(["", "## Abstract", "\n\n".join(abstract_sections)])
    return _result(
        "\n".join(lines),
        source_url=endpoint,
        requested_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        adapter="pubmed_efetch",
        status="complete",
        semantic_type="publication_abstract",
        extra={"pmid": pmid},
    )


def _xml_text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


async def _arxiv(arxiv_id: str, *, timeout: int, request: Request | None) -> ToolResult:
    endpoint = "https://export.arxiv.org/api/query"
    response = await _get(
        endpoint,
        timeout=timeout,
        request=request,
        params={"id_list": arxiv_id, "max_results": "1"},
    )
    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise ValueError("arXiv entry missing")
    title = _xml_text(entry.find("atom:title", ns))
    summary = _xml_text(entry.find("atom:summary", ns))
    published = _xml_text(entry.find("atom:published", ns))
    updated = _xml_text(entry.find("atom:updated", ns))
    authors = [
        _xml_text(author.find("atom:name", ns))
        for author in entry.findall("atom:author", ns)
        if _xml_text(author.find("atom:name", ns))
    ]
    categories = [
        str(node.attrib.get("term") or "").strip()
        for node in entry.findall("atom:category", ns)
        if str(node.attrib.get("term") or "").strip()
    ]
    doi = _xml_text(entry.find("{http://arxiv.org/schemas/atom}doi"))
    pdf_url = next(
        (
            str(node.attrib.get("href") or "")
            for node in entry.findall("atom:link", ns)
            if node.attrib.get("title") == "pdf" or node.attrib.get("type") == "application/pdf"
        ),
        f"https://arxiv.org/pdf/{arxiv_id}",
    )
    lines = [
        f"# {title}",
        f"arXiv: {arxiv_id}",
        f"Authors: {', '.join(authors) or 'unknown'}",
    ]
    if published:
        lines.append(f"Published: {published}")
    if updated:
        lines.append(f"Updated: {updated}")
    if categories:
        lines.append(f"Categories: {', '.join(categories)}")
    if doi:
        lines.append(f"DOI: {doi}")
    lines.extend([f"PDF: {pdf_url}", "", "## Abstract", summary])
    return _result(
        "\n".join(lines),
        source_url=endpoint,
        requested_url=f"https://arxiv.org/abs/{arxiv_id}",
        adapter="arxiv_atom",
        status="complete",
        semantic_type="preprint_metadata",
        extra={"arxiv_id": arxiv_id},
    )


def _youtube_video_id(parsed: Any) -> str | None:
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/") or None
    values = parse_qs(str(parsed.query)).get("v")
    if not values:
        return None
    value = values[0]
    return value if isinstance(value, str) and value else None


async def _youtube(url: str, *, timeout: int, request: Request | None) -> ToolResult:
    deadline = asyncio.get_running_loop().time() + min(
        float(timeout), _PUBLIC_ADAPTER_TIMEOUT_SECONDS
    )
    endpoint = "https://www.youtube.com/oembed"
    async with asyncio.timeout(max(0.1, deadline - asyncio.get_running_loop().time())):
        response = await _get(
            endpoint,
            timeout=timeout,
            request=request,
            params={"url": url, "format": "json"},
        )
    data = response.json()
    title = str(data.get("title") or "")
    author = str(data.get("author_name") or "")
    video_id = _youtube_video_id(urlparse(url))
    remaining = deadline - asyncio.get_running_loop().time()
    transcript = {"status": "transcript_fetch_timeout"}
    if video_id and remaining > 0.1:
        transcript = await _fetch_youtube_transcript(
            video_id,
            timeout=min(remaining, _YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS),
        )
    elif not video_id:
        transcript = {"status": "invalid_video_id"}
    transcript_status = str(transcript.get("status") or "transcript_fetch_failed")
    chunks = transcript.get("chunks")
    transcript_chunks: list[dict[str, Any]] = (
        [chunk for chunk in chunks if isinstance(chunk, dict)] if isinstance(chunks, list) else []
    )
    if not transcript_chunks and transcript_status == "available":
        transcript_status = "transcript_unavailable"
    language = str(transcript.get("language") or "")
    generated = bool(transcript.get("is_generated"))
    lines = [f"# {title}", f"Author: {author}"]
    if transcript_chunks:
        lines.extend(
            [
                f"Language: {language or 'unknown'}",
                f"Transcript: {'auto-generated' if generated else 'manually authored'}",
                "",
                "## Transcript",
                "Full timestamped transcript is available in the transcript anchors.",
            ]
        )
    else:
        lines.extend(["", f"Transcript: unavailable ({transcript_status})."])
    return _result(
        "\n".join(lines),
        source_url=endpoint,
        requested_url=url,
        adapter="youtube_transcript" if transcript_chunks else "youtube_oembed",
        status="complete" if transcript_chunks else "partial",
        semantic_type="video_transcript" if transcript_chunks else "video_metadata",
        extra={
            "video_id": video_id,
            "transcript_availability": "available" if transcript_chunks else transcript_status,
            "transcript_language": language or None,
            "transcript_language_code": transcript.get("language_code"),
            "transcript_generated": generated if transcript_chunks else None,
            "transcript_segment_count": transcript.get("segment_count", 0),
            "transcript_error_type": transcript.get("error_type"),
            "transcript_chunks": transcript_chunks,
        },
    )


def _youtube_transcript_worker(result_queue: Any, video_id: str) -> None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcripts = list(YouTubeTranscriptApi().list(video_id))
        if not transcripts:
            result_queue.put({"status": "transcript_unavailable"})
            return
        track = _select_youtube_transcript(transcripts)
        fetched = track.fetch()
        segments = [
            {
                "text": html.unescape(str(snippet.text)).strip(),
                "start": float(snippet.start),
                "duration": float(snippet.duration),
            }
            for snippet in fetched
            if str(snippet.text).strip()
        ]
        result_queue.put(
            {
                "status": "available",
                "language": str(fetched.language),
                "language_code": str(fetched.language_code),
                "is_generated": bool(fetched.is_generated),
                "segments": segments,
            }
        )
    except BaseException as exc:  # pragma: no cover - isolated provider boundary
        error_type = type(exc).__name__
        result_queue.put(
            {
                "status": _youtube_transcript_error(error_type),
                "error_type": error_type,
            }
        )


async def _fetch_youtube_transcript(video_id: str, *, timeout: float) -> dict[str, Any]:
    """Fetch captions in a process that can be terminated at the adapter deadline."""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_youtube_transcript_worker,
        args=(result_queue, video_id),
        daemon=True,
    )
    process.start()
    terminated = False
    try:
        try:
            result = await asyncio.to_thread(result_queue.get, True, timeout)
        except queue.Empty:
            process.terminate()
            terminated = True
            await asyncio.to_thread(process.join, 2.0)
            return {"status": "transcript_fetch_timeout"}
        await asyncio.to_thread(process.join, 2.0)
        if not isinstance(result, dict):
            return {"status": "transcript_fetch_failed"}
        segments = result.pop("segments", None)
        if isinstance(segments, list):
            normalized = _normalize_transcript_segments(segments)
            if normalized:
                result["segment_count"] = len(normalized)
                result["chunks"] = _chunk_transcript(normalized)
            else:
                result["status"] = "transcript_unavailable"
        return result
    except asyncio.CancelledError:
        if process.is_alive():
            process.terminate()
            terminated = True
            await asyncio.to_thread(process.join, 2.0)
        raise
    finally:
        result_queue.close()
        if not terminated and process.is_alive():
            process.terminate()
            process.join(timeout=2.0)


def _normalize_transcript_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    previous_text = ""
    previous_start = -1.0
    for item in sorted(segments, key=lambda value: float(value.get("start") or 0.0)):
        text = " ".join(str(item.get("text") or "").split())
        start = max(0.0, float(item.get("start") or 0.0))
        if not text or (text == previous_text and abs(start - previous_start) <= 1.0):
            continue
        previous_text = text
        previous_start = start
        normalized.append(
            {
                "text": text,
                "start": start,
                "duration": max(0.0, float(item.get("duration") or 0.0)),
            }
        )
    return normalized


def _select_youtube_transcript(transcripts: list[Any]) -> Any:
    """Prefer manual captions matching the video's generated/native language."""
    manual = [track for track in transcripts if not bool(track.is_generated)]
    generated_codes = {
        str(track.language_code)
        for track in transcripts
        if bool(track.is_generated) and str(track.language_code)
    }
    native_manual = [track for track in manual if str(track.language_code) in generated_codes]
    if native_manual:
        return native_manual[0]
    native_generated = [
        track
        for track in transcripts
        if bool(track.is_generated) and str(track.language_code) in generated_codes
    ]
    if native_generated:
        return native_generated[0]
    return (manual or transcripts)[0]


def _chunk_transcript(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[str]] = {}
    for item in segments:
        start = int(float(item["start"]))
        bucket = start // _TRANSCRIPT_CHUNK_SECONDS * _TRANSCRIPT_CHUNK_SECONDS
        buckets.setdefault(bucket, []).append(f"[{_timestamp(start)}] {item['text']}")
    chunks: list[dict[str, Any]] = []
    for start, lines in sorted(buckets.items()):
        end = start + _TRANSCRIPT_CHUNK_SECONDS
        chunks.append(
            {
                "anchor": f"transcript:{_timestamp(start)}",
                "label": f"Transcript {_timestamp(start)}–{_timestamp(end)}",
                "start_seconds": start,
                "end_seconds": end,
                "lines": lines,
            }
        )
    return chunks


def _timestamp(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _youtube_transcript_error(error_name: str) -> str:
    mapping = {
        "TranscriptsDisabled": "captions_disabled",
        "NoTranscriptFound": "transcript_unavailable",
        "VideoUnavailable": "video_unavailable",
        "VideoUnplayable": "video_unavailable",
        "AgeRestricted": "age_restricted",
        "RequestBlocked": "provider_blocked",
        "IpBlocked": "provider_blocked",
        "PoTokenRequired": "provider_blocked",
        "InvalidVideoId": "invalid_video_id",
    }
    return mapping.get(error_name, "transcript_fetch_failed")


async def _get(
    url: str,
    *,
    timeout: int,
    request: Request | None,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if request is not None:
        response = await request(url, params=params, headers=headers, timeout=timeout)
    else:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers)
    response.raise_for_status()
    return response


def _result(
    content: str,
    *,
    source_url: str,
    requested_url: str,
    adapter: str,
    status: str,
    semantic_type: str,
    extra: dict[str, Any] | None = None,
) -> ToolResult:
    metadata: dict[str, Any] = {
        "adapter": adapter,
        "adapter_status": status,
        "semantic_type": semantic_type,
        "requested_url": requested_url,
        "source_url": source_url,
        "provenance": {"requested_url": requested_url, "source_url": source_url},
        "url_provenance": {
            "requested_url": requested_url,
            "fetched_url": source_url,
            "canonical_url": requested_url,
            "redirected": source_url != requested_url,
            "canonicalized": False,
        },
        "extracted_document": {
            "url": source_url,
            "canonical_url": requested_url,
            "extractor": f"adapter_{adapter}",
            "semantic_quality": {
                "status": status,
                "label": status,
                "score": 100.0 if status == "complete" else 40.0,
                "rank": 4 if status == "complete" else 3,
                "signals": ["public_adapter", semantic_type],
            },
            "url_provenance": {
                "requested_url": requested_url,
                "fetched_url": source_url,
                "canonical_url": requested_url,
            },
        },
    }
    metadata.update(extra or {})
    return ToolResult(output=content, metadata=metadata)
