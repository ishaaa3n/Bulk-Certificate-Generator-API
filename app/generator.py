"""Certificate rendering (single predefined template) using ReportLab."""
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = landscape(A4)
NAME_FONT = "Helvetica-BoldOblique"


@dataclass
class CertificateData:
    certificate_id: str
    name: str
    course_name: str
    issue_date: date
    issuer: str | None = None


def _fit_font_size(text: str, font: str, max_width: float, start: float, minimum: float = 12) -> float:
    """Shrink the font until long names/titles still fit inside the border."""
    size = start
    while size > minimum and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _centered(c: canvas.Canvas, text: str, y: float, font: str, size: float, max_width: float | None = None):
    max_width = max_width or PAGE_W - 140
    size = _fit_font_size(text, font, max_width, size)
    c.setFont(font, size)
    c.drawCentredString(PAGE_W / 2, y, text)


def generate_certificate(data: CertificateData, dest: Path) -> None:
    """Render the PDF to `dest`.

    Written to a temp file then atomically renamed, so a crash mid-render can
    never leave a truncated PDF at the final path.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")  # unique: concurrent writers never share a temp file
    try:
        c = canvas.Canvas(str(tmp), pagesize=(PAGE_W, PAGE_H), invariant=1)
        c.setTitle(f"Certificate - {data.name}")
        c.setAuthor(data.issuer or "Certificate Generator")

        c.setStrokeColor(colors.HexColor("#1f3a5f"))
        c.setLineWidth(6)
        c.rect(25, 25, PAGE_W - 50, PAGE_H - 50)
        c.setLineWidth(1.5)
        c.rect(38, 38, PAGE_W - 76, PAGE_H - 76)

        c.setFillColor(colors.HexColor("#1f3a5f"))
        _centered(c, "CERTIFICATE OF COMPLETION", PAGE_H - 140, "Helvetica-Bold", 38)

        c.setFillColor(colors.HexColor("#444444"))
        _centered(c, "This is to certify that", PAGE_H - 200, "Helvetica", 18)

        c.setFillColor(colors.black)
        _centered(c, data.name, PAGE_H - 265, NAME_FONT, 44)
        c.setStrokeColor(colors.HexColor("#999999"))
        c.setLineWidth(1)
        c.line(PAGE_W / 2 - 220, PAGE_H - 280, PAGE_W / 2 + 220, PAGE_H - 280)

        c.setFillColor(colors.HexColor("#444444"))
        _centered(c, "has successfully completed", PAGE_H - 320, "Helvetica", 18)

        c.setFillColor(colors.HexColor("#1f3a5f"))
        _centered(c, data.course_name, PAGE_H - 365, "Helvetica-Bold", 28)

        c.setFillColor(colors.HexColor("#444444"))
        _centered(c, f"Issued on {data.issue_date.strftime('%d %B %Y')}", PAGE_H - 420, "Helvetica", 14)
        if data.issuer:
            _centered(c, f"Issued by {data.issuer}", PAGE_H - 445, "Helvetica", 14)

        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#777777"))
        c.drawCentredString(PAGE_W / 2, 55, f"Certificate ID: {data.certificate_id}")
        c.showPage()
        c.save()
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
