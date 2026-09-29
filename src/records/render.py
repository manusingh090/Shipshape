"""Drawing a record as a PDF certificate. The HTML certificate
(templates/records/detail.html) shows the same payload; both come from the
signed record, so what's printed is exactly what's signed."""

from . import pdf

INK = (0.110, 0.102, 0.086)
SOFT = (0.294, 0.275, 0.239)
SIGNAL = (0.788, 0.275, 0.110)
RULE = (0.827, 0.792, 0.710)

HEADINGS = {
    "participant": "Certificate of participation",
    "award": "Winner's certificate",
    "judge": "Judge's record",
    "organizer": "Certificate of organizing",
}


def certificate_pdf(payload, verify_url, key_id):
    page = pdf.Page()
    w, h = pdf.Page.WIDTH, pdf.Page.HEIGHT
    cx = w / 2

    # A printed programme's frame: a heavy rule, a hairline inside it.
    page.colour(INK, stroke=True)
    page.rect(28, 28, w - 56, h - 56, line=2.2)
    page.colour(RULE, stroke=True)
    page.rect(38, 38, w - 76, h - 76, line=0.6)

    page.colour(SIGNAL)
    page.text(cx, h - 92, "SHIPSHAPE  \u00b7  " + payload["event"]["name"].upper(), font="mono", size=9,
              align="center", spacing=1.2)
    page.colour(SIGNAL, stroke=True)
    page.line(cx - 60, h - 104, cx + 60, h - 104, width_=1.4)

    page.colour(INK)
    page.text(cx, h - 150, HEADINGS.get(payload["kind"], payload["title"]), font="serif", size=24, align="center")

    page.colour(SOFT)
    page.text(cx, h - 196, "This is to certify that", font="serif", size=13, align="center")
    name_size = 40
    while pdf.width(payload["recipient"], "serif-bold", name_size) > w - 160 and name_size > 20:
        name_size -= 2
    page.colour(INK)
    page.text(cx, h - 248, payload["recipient"], font="serif-bold", size=name_size, align="center")

    statement = payload["statement"]
    if statement.startswith(payload["recipient"] + " "):
        statement = statement[len(payload["recipient"]) + 1:]
    y = h - 290
    page.colour(SOFT)
    for line in pdf.wrap(statement, "serif", 15, w - 220)[:4]:
        page.text(cx, y, line, font="serif", size=15, align="center")
        y -= 21

    details = payload.get("details") or {}
    extra = []
    if payload["kind"] == "judge":
        extra.append(f"Scores fingerprint (SHA-256): {details.get('scores_sha256', '')[:32]}\u2026")
    elif details.get("note"):
        extra.append(details["note"])
    for line in extra:
        page.text(cx, y - 6, line, font="mono" if payload["kind"] == "judge" else "serif", size=9 if payload["kind"] == "judge" else 12,
                  align="center")
        y -= 18

    page.colour(INK)
    page.text(cx, 132, payload["event"]["dates"] + (f"  \u00b7  {payload['event']['location']}" if payload["event"].get("location") else ""),
              font="serif", size=12, align="center")

    page.colour(RULE, stroke=True)
    page.line(70, 104, w - 70, 104, width_=0.6)
    page.colour(SOFT)
    page.text(70, 86, f"Record {payload['code']}", font="mono", size=9)
    page.text(70, 72, f"Issued {payload['issued_at']}  \u00b7  Ed25519 key {key_id}", font="mono", size=8)
    page.text(w - 70, 86, "Check it at", font="mono", size=9, align="right")
    page.text(w - 70, 72, verify_url, font="mono", size=8, align="right")
    return pdf.document(page, title=f"{HEADINGS.get(payload['kind'], 'Record')}: {payload['recipient']}")
