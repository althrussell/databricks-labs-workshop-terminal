"""Brag certificate: a branded, landscape A4 PDF of what the attendee built.

Pure reportlab vector drawing — no image assets needed. The attendee supplies
their display name (lab identities are generic), the event branding comes from
env, and the stats come from server/stats.py.
"""

from __future__ import annotations

import io
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from . import config

RED = HexColor("#FF3621")
INK = HexColor("#131A26")
SLATE = HexColor("#5A6473")
PAPER = HexColor("#FAF8F5")
LINE = HexColor("#E3DED6")


def _databricks_mark(c: canvas.Canvas, x: float, y: float, s: float) -> None:
    """Draw a stacked-chevron Databricks-style mark at (x, y), height ~s."""
    c.saveState()
    c.setFillColor(RED)
    c.setStrokeColor(RED)
    for i in range(3):
        oy = y + i * s * 0.28
        path = c.beginPath()
        path.moveTo(x, oy + s * 0.18)
        path.lineTo(x + s * 0.5, oy + s * 0.42)
        path.lineTo(x + s, oy + s * 0.18)
        path.lineTo(x + s * 0.5, oy - s * 0.06)
        path.close()
        c.drawPath(path, fill=1, stroke=0)
    c.restoreState()


def _stat_value(value: int | str) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def build_pdf(name: str, stats: dict) -> bytes:
    branding = config.branding()
    event = branding["event_name"] or "Databricks Workshop"
    brand = branding["brand_name"]

    buf = io.BytesIO()
    width, height = landscape(A4)
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setTitle(f"{event} — Certificate of Achievement")

    # Canvas + frame
    c.setFillColor(PAPER)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setStrokeColor(LINE)
    c.setLineWidth(1)
    c.rect(28, 28, width - 56, height - 56, fill=0, stroke=1)
    c.setFillColor(RED)
    c.rect(28, height - 36, width - 56, 8, fill=1, stroke=0)

    # Header: mark + brand
    _databricks_mark(c, 56, height - 96, 34)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(104, height - 78, brand)
    c.setFillColor(SLATE)
    c.setFont("Helvetica", 10.5)
    c.drawString(104, height - 93, event)
    c.setFont("Helvetica", 10.5)
    c.drawRightString(width - 56, height - 78, date.today().strftime("%B %d, %Y"))

    # Title block
    c.setFillColor(SLATE)
    c.setFont("Helvetica", 13)
    c.drawCentredString(width / 2, height - 170, "CERTIFICATE OF ACHIEVEMENT")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 40)
    c.drawCentredString(width / 2, height - 218, name)
    c.setStrokeColor(RED)
    c.setLineWidth(2)
    name_w = max(c.stringWidth(name, "Helvetica-Bold", 40), 200)
    c.line(width / 2 - name_w / 2, height - 230, width / 2 + name_w / 2, height - 230)
    c.setFillColor(SLATE)
    c.setFont("Helvetica", 13)
    c.drawCentredString(
        width / 2, height - 254,
        "built with AI coding agents on the Databricks Data + AI Platform",
    )

    # Stats grid
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
    grid_top = height - 300
    cell_w = (width - 144) / 3
    cell_h = 78
    for i, (value, label) in enumerate(cells):
        col, row = i % 3, i // 3
        cx = 72 + col * cell_w
        cy = grid_top - row * (cell_h + 14) - cell_h
        c.setFillColor(HexColor("#FFFFFF"))
        c.setStrokeColor(LINE)
        c.roundRect(cx, cy, cell_w - 16, cell_h, 8, fill=1, stroke=1)
        c.setFillColor(RED)
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(cx + (cell_w - 16) / 2, cy + 38, value)
        c.setFillColor(SLATE)
        c.setFont("Helvetica", 10)
        c.drawCentredString(cx + (cell_w - 16) / 2, cy + 18, label.upper())

    # Detail lines
    detail_y = grid_top - 2 * (cell_h + 14) - 26
    details = []
    if resources:
        parts = [
            f"{v} {k.rstrip('s') if v == 1 else k}" for k, v in resources.items() if v
        ]
        if parts:
            details.append("In the workshop workspace: " + ", ".join(parts))
    topics = stats.get("topics", [])
    if topics:
        details.append("Explored: " + " · ".join(t.replace("-", " ").title() for t in topics))
    if code.get("projects"):
        details.append(
            f"{code['projects']} project(s), {code.get('files', 0)} files — "
            "synced to your Databricks Workspace to keep building"
        )
    c.setFont("Helvetica", 10.5)
    c.setFillColor(SLATE)
    for line in details[:3]:
        c.drawCentredString(width / 2, detail_y, line)
        detail_y -= 16

    # Footer
    c.setFillColor(SLATE)
    c.setFont("Helvetica-Oblique", 9.5)
    c.drawCentredString(
        width / 2, 36, "Powered by the Databricks Workshop Terminal — databricks.com"
    )

    c.showPage()
    c.save()
    return buf.getvalue()
