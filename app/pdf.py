"""Certificate rendering: one fixed landscape-A4 template drawn with ReportLab.
The renderer is a pure function (data dict -> PDF bytes), which keeps it easy to
test and to swap out."""
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

NAVY = colors.HexColor("#1f3a5f")
GOLD = colors.HexColor("#b08d3c")
GREY = colors.HexColor("#555555")


def _fit(text, font, max_size, max_width, min_size=14):
    """Largest font size <= max_size so that text fits max_width."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _wrap(text, font, size, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if stringWidth(trial, font, size) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render_certificate(d: dict) -> bytes:
    """d keys: title, event_name, issuer, issue_date, recipient_name, code,
    achievement (optional)."""
    w, h = landscape(A4)
    cx = w / 2
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setTitle(f"{d['title']} - {d['recipient_name']}")
    c.setAuthor(d["issuer"])

    # Double border
    c.setStrokeColor(NAVY); c.setLineWidth(6); c.rect(25, 25, w - 50, h - 50)
    c.setStrokeColor(GOLD); c.setLineWidth(1.5); c.rect(38, 38, w - 76, h - 76)

    inner = w - 200  # usable text width

    # Title
    c.setFillColor(NAVY)
    size = _fit(d["title"].upper(), "Helvetica-Bold", 38, inner)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, h - 125, d["title"].upper())
    c.setStrokeColor(GOLD); c.setLineWidth(2)
    c.line(cx - 110, h - 140, cx + 110, h - 140)

    c.setFillColor(GREY); c.setFont("Helvetica", 15)
    c.drawCentredString(cx, h - 185, "This certificate is proudly presented to")

    # Recipient name (auto-shrinks for long names)
    c.setFillColor(NAVY)
    size = _fit(d["recipient_name"], "Times-BoldItalic", 46, inner, min_size=18)
    c.setFont("Times-BoldItalic", size)
    c.drawCentredString(cx, h - 245, d["recipient_name"])
    nw = min(stringWidth(d["recipient_name"], "Times-BoldItalic", size) + 40, inner)
    c.setStrokeColor(GREY); c.setLineWidth(0.8)
    c.line(cx - nw / 2, h - 255, cx + nw / 2, h - 255)

    # Body
    y = h - 295
    c.setFillColor(GREY); c.setFont("Helvetica", 15)
    c.drawCentredString(cx, y, "in recognition of their participation in")
    y -= 36
    c.setFillColor(NAVY)
    size = _fit(d["event_name"], "Helvetica-Bold", 24, inner, min_size=14)
    c.setFont("Helvetica-Bold", size)
    for line in _wrap(d["event_name"], "Helvetica-Bold", size, inner)[:2]:
        c.drawCentredString(cx, y, line)
        y -= size + 4

    if d.get("achievement"):
        y -= 6
        c.setFillColor(GOLD); c.setFont("Helvetica-Oblique", 15)
        for line in _wrap(d["achievement"], "Helvetica-Oblique", 15, inner)[:2]:
            c.drawCentredString(cx, y, line)
            y -= 20

    # Footer: date / issuer
    base = 110
    issued = date.fromisoformat(d["issue_date"]).strftime("%d %B %Y")
    c.setStrokeColor(GREY); c.setLineWidth(0.8)
    c.line(110, base, 290, base); c.line(w - 290, base, w - 110, base)
    c.setFillColor(NAVY); c.setFont("Helvetica", 13)
    c.drawCentredString(200, base + 8, issued)
    isize = _fit(d["issuer"], "Helvetica", 13, 180, min_size=8)
    c.setFont("Helvetica", isize)
    c.drawCentredString(w - 200, base + 8, d["issuer"])
    c.setFillColor(GREY); c.setFont("Helvetica", 10)
    c.drawCentredString(200, base - 15, "Date of issue")
    c.drawCentredString(w - 200, base - 15, "Issued by")

    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, 55, f"Certificate No. {d['code']}")
    c.showPage()
    c.save()
    return buf.getvalue()
