"""PWA asset parity with the opencode fork's mobile web UI.

The opencode fork installs cleanly on phones because it ships real PNG
manifest icons marked ``purpose: maskable``, an ``apple-touch-icon`` link,
and a ``mobile-web-app-capable`` meta tag. agy-remote shipped only an SVG
data-URI icon, which iOS ignores entirely: the installed app lands on the
home screen as a browser-screenshot tile. These tests pin the parity set.
"""

import json
import re
import struct
from pathlib import Path

STATIC_DIR = Path(__file__).parents[1] / "src" / "agy_remote" / "static"


def png_dimensions(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    assert raw[12:16] == b"IHDR", f"{path.name} missing IHDR"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def test_manifest_has_png_maskable_icons():
    manifest = json.loads((STATIC_DIR / "manifest.json").read_text())
    pngs = [i for i in manifest["icons"] if i["type"] == "image/png"]
    sizes = {(i["sizes"], i["purpose"]) for i in pngs}
    assert ("192x192", "maskable") in sizes
    assert ("512x512", "maskable") in sizes


def test_manifest_has_id_and_scope():
    manifest = json.loads((STATIC_DIR / "manifest.json").read_text())
    assert manifest["id"] == "/"
    assert manifest["scope"] == "/"


def test_png_icon_files_exist_with_correct_dimensions():
    for name, size in [
        ("icons/icon-192.png", 192),
        ("icons/icon-512.png", 512),
        ("icons/apple-touch-icon.png", 180),
        ("icons/favicon-96x96.png", 96),
    ]:
        path = STATIC_DIR / name
        assert path.exists(), f"missing {name}"
        assert png_dimensions(path) == (size, size), f"{name} wrong dimensions"


def test_manifest_icon_paths_resolve_to_real_files():
    manifest = json.loads((STATIC_DIR / "manifest.json").read_text())
    for icon in manifest["icons"]:
        if icon["type"] != "image/png":
            continue
        src = icon["src"]
        assert src.startswith("/static/icons/"), src
        assert (STATIC_DIR / src.removeprefix("/static/")).exists(), src


def test_index_html_has_ios_and_android_install_metas():
    html = (STATIC_DIR / "index.html").read_text()
    assert re.search(r'<link[^>]+rel="apple-touch-icon"', html), (
        "iOS needs an apple-touch-icon PNG link or the installed app shows a screenshot tile"
    )
    assert re.search(r'<meta[^>]+name="mobile-web-app-capable"[^>]+content="yes"', html)


def test_sw_push_notification_uses_png_icon():
    sw = (STATIC_DIR / "sw.js").read_text()
    assert "icons/icon-192.png" in sw
