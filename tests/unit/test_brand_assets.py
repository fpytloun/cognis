"""Check the canonical vector and its generated distribution assets."""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "ui" / "static"
SVG = "{http://www.w3.org/2000/svg}"


def _geometry(path: Path) -> list[tuple[str, dict[str, str]]]:
    return [
        (
            item.tag,
            {key: value for key, value in item.attrib.items() if key != "fill"},
        )
        for item in ET.parse(path).getroot().iter()
        if item.tag in {f"{SVG}path", f"{SVG}circle"}
    ]


def test_brand_master_is_real_transparent_current_color_vector() -> None:
    root = ET.parse(STATIC / "brand" / "cognis-mark.svg").getroot()
    assert root.attrib["fill"] == "currentColor"
    assert [element.tag for element in root] == [
        f"{SVG}title",
        f"{SVG}path",
        f"{SVG}path",
        f"{SVG}path",
        f"{SVG}path",
        f"{SVG}circle",
    ]
    assert "data:" not in ET.tostring(root, encoding="unicode")


def test_generated_svg_variants_preserve_master_geometry() -> None:
    geometry = _geometry(STATIC / "brand" / "cognis-mark.svg")
    for path in [
        STATIC / "brand" / "cognis-mark-color.svg",
        STATIC / "favicon.svg",
        STATIC / "pwa" / "icon.svg",
    ]:
        assert _geometry(path) == geometry
        assert not any(
            item.tag in {f"{SVG}image", f"{SVG}script", f"{SVG}foreignObject"}
            for item in ET.parse(path).getroot().iter()
        )


def test_generated_png_dimensions_and_alpha_format() -> None:
    for name, expected in [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
        ("badge-96.png", 96),
    ]:
        payload = (STATIC / "pwa" / name).read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", payload[16:24]) == (expected, expected)
        if name in {"icon-192.png", "icon-512.png", "badge-96.png"}:
            assert payload[25] == 6  # PNG RGBA.


def test_generated_favicon_contains_multiple_sizes() -> None:
    reserved, kind, count = struct.unpack("<HHH", (STATIC / "favicon.ico").read_bytes()[:6])
    assert (reserved, kind) == (0, 1)
    assert count == 6


def test_apple_splash_dimensions_match_filenames() -> None:
    for pattern in ("apple-splash-[0-9]*.png", "apple-splash-light-*.png"):
        splashes = list((STATIC / "pwa").glob(pattern))
        assert len(splashes) == 11
        for path in splashes:
            expected = tuple(int(value) for value in path.stem.split("-")[-2:])
            assert struct.unpack(">II", path.read_bytes()[16:24]) == expected
