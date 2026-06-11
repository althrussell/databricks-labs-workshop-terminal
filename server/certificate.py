"""Brag certificate: a branded, landscape A4 PDF of what the attendee built.

Design follows the Databricks v2 editorial system (html-slides skill): oat
ground, navy ink, a single lava accent, DM Sans / DM Mono typography, the real
lockup in the header, and the diamond symbol as a faint background watermark.
The brand marks are pre-rasterized transparent PNGs vendored under
assets/brand/ (the SVG route needed svglib→lxml, which the Apps platform's
package install rejected). No network at render time.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from . import config

logger = logging.getLogger(__name__)

_BRAND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets", "brand"))

LAVA = HexColor("#FF3621")
NAVY = HexColor("#1B3139")
NAVY_500 = HexColor("#5B7079")
OAT = HexColor("#F9F7F4")
OAT_DARK = HexColor("#DCD9D3")

_FONTS = {
    "DMSans": "dm-sans-regular.ttf",
    "DMSans-Medium": "dm-sans-medium.ttf",
    "DMSans-Bold": "dm-sans-bold.ttf",
    "DMSans-Italic": "dm-sans-italic.ttf",
    "DMMono": "dm-mono-regular.ttf",
    "DMMono-Medium": "dm-mono-medium.ttf",
}
_fonts_ready = False


def _ensure_fonts() -> dict[str, str]:
    """Register DM Sans/Mono; fall back to Helvetica when assets are missing."""
    global _fonts_ready
    mapping = {
        "sans": "Helvetica", "medium": "Helvetica", "bold": "Helvetica-Bold",
        "italic": "Helvetica-Oblique", "mono": "Courier", "mono-medium": "Courier-Bold",
    }
    try:
        if not _fonts_ready:
            for name, filename in _FONTS.items():
                pdfmetrics.registerFont(TTFont(name, os.path.join(_BRAND, filename)))
            _fonts_ready = True
    except Exception as e:  # noqa: BLE001 — certificate must render regardless
        logger.warning("brand fonts unavailable (%s) — falling back to Helvetica", e)
        return mapping
    return {
        "sans": "DMSans", "medium": "DMSans-Medium", "bold": "DMSans-Bold",
        "italic": "DMSans-Italic", "mono": "DMMono", "mono-medium": "DMMono-Medium",
    }


def _brand_image(filename: str) -> ImageReader | None:
    try:
        return ImageReader(os.path.join(_BRAND, filename))
    except Exception as e:  # noqa: BLE001
        logger.warning("brand image %s unavailable: %s", filename, e)
        return None


def _draw_image(c: canvas.Canvas, image: ImageReader | None,
                x: float, y: float, height: float) -> float:
    """Draw a transparent PNG scaled to `height`; returns rendered width."""
    if image is None:
        return 0.0
    iw, ih = image.getSize()
    width = height * iw / ih
    c.drawImage(image, x, y, width=width, height=height, mask="auto")
    return width


def _stat_value(value: int | str) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def build_pdf(name: str, stats: dict) -> bytes:
    branding = config.branding()
    event = branding["event_name"] or "Databricks Workshop"
    f = _ensure_fonts()

    buf = io.BytesIO()
    width, height = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setTitle(f"{event} — Certificate of Achievement")

    # Ground
    c.setFillColor(OAT)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    # Watermark: the diamond symbol, huge and faint (pre-tinted PNG, 5% navy),
    # bleeding off bottom-right.
    _draw_image(c, _brand_image("watermark-symbol.png"), width - 330, -130, 460)

    # Lava hairline top — the single accent rule (v2 eyebrow grammar).
    c.setFillColor(LAVA)
    c.rect(0, height - 6, width, 6, fill=1, stroke=0)

    # Header: real lockup + date
    _draw_image(c, _brand_image("lockup-navy.png"), 64, height - 92, 30)
    c.setFillColor(NAVY_500)
    c.setFont(f["mono"], 10)
    c.drawRightString(width - 64, height - 82, date.today().strftime("%B %d, %Y").upper())

    # Eyebrow + name + lede
    c.setFillColor(LAVA)
    c.setFont(f["mono-medium"], 11)
    c.drawCentredString(width / 2, height - 168, "C E R T I F I C A T E   O F   A C H I E V E M E N T")
    c.setFillColor(NAVY)
    name_size = 54 if c.stringWidth(name, f["bold"], 54) < width - 200 else 40
    c.setFont(f["bold"], name_size)
    c.drawCentredString(width / 2, height - 224, name)
    c.setFillColor(NAVY_500)
    c.setFont(f["italic"], 14.5)
    c.drawCentredString(
        width / 2, height - 252,
        f"built with AI coding agents on the Databricks Data + AI Platform — {event}",
    )

    # Stats row: editorial columns separated by hairlines, not boxes.
    code = stats.get("code", {})
    resources = stats.get("resources", {})
    resource_total = sum(resources.values()) if resources else 0
    cells = [
        (_stat_value(stats.get("agent_sessions", 0)), "AI agent sessions"),
        (_stat_value(stats.get("minutes_building", 0)), "minutes building"),
        (_stat_value(code.get("lines", 0)), "lines of code"),
        (_stat_value(code.get("commits", 0)), "commits"),
        (_stat_value(resource_total), "Databricks resources"),
        (_stat_value(len(stats.get("topics", []))), "topics explored"),
    ]
    row_top, row_h = height - 296, 96
    cell_w = (width - 128) / 6
    c.setStrokeColor(OAT_DARK)
    c.setLineWidth(1)
    c.line(64, row_top, width - 64, row_top)
    c.line(64, row_top - row_h, width - 64, row_top - row_h)
    for i, (value, label) in enumerate(cells):
        cx = 64 + i * cell_w + cell_w / 2
        if i:
            c.line(64 + i * cell_w, row_top - 14, 64 + i * cell_w, row_top - row_h + 14)
        size = 30 if c.stringWidth(value, f["bold"], 30) < cell_w - 18 else 22
        c.setFillColor(NAVY)
        c.setFont(f["bold"], size)
        c.drawCentredString(cx, row_top - 48, value)
        c.setFillColor(NAVY_500)
        c.setFont(f["mono"], 8.5)
        c.drawCentredString(cx, row_top - 72, label.upper())

    # Detail lines
    detail_y = row_top - row_h - 30
    details = []
    if resources:
        parts = [f"{v} {k.rstrip('s') if v == 1 else k}" for k, v in resources.items() if v]
        if parts:
            details.append("In the workshop workspace: " + ", ".join(parts))
    topics = stats.get("topics", [])
    if topics:
        details.append("Explored " + " · ".join(t.replace("-", " ").title() for t in topics))
    if code.get("projects"):
        details.append(
            f"{code['projects']} project{'s' if code['projects'] != 1 else ''}, "
            f"{code.get('files', 0)} files — synced to your Databricks Workspace to keep building"
        )
    c.setFillColor(NAVY_500)
    c.setFont(f["sans"], 11)
    for line in details[:3]:
        c.drawCentredString(width / 2, detail_y, line)
        detail_y -= 17

    # Footer
    c.setStrokeColor(OAT_DARK)
    c.line(64, 58, width - 64, 58)
    c.setFillColor(NAVY_500)
    c.setFont(f["mono"], 8.5)
    c.drawString(64, 42, "POWERED BY THE DATABRICKS WORKSHOP TERMINAL")
    c.drawRightString(width - 64, 42, "DATABRICKS.COM")

    c.showPage()
    c.save()
    return buf.getvalue()
