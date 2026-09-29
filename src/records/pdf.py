"""A small PDF writer for certificates. Plain Python, no dependencies.

It uses three of the fonts every PDF reader carries (Times, Times Bold and
Courier), so nothing is embedded and the file is a few kilobytes. Text is
measured with the fonts' published glyph widths, so lines can be centred and
wrapped. Characters outside Windows-1252 are simplified (é stays é, other
scripts fall back to their closest Latin letters); the HTML certificate always
shows names exactly.
"""

import unicodedata
import zlib

# Widths per 1000 units for printable ASCII 32..126, from the Adobe metrics.
_TIMES = [250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333, 250, 278, 500, 500, 500, 500,
          500, 500, 500, 500, 500, 500, 278, 278, 564, 564, 564, 444, 921, 722, 667, 667, 722, 611, 556, 722, 722,
          333, 389, 722, 611, 889, 722, 722, 556, 722, 667, 556, 611, 722, 722, 944, 722, 722, 611, 333, 278, 333,
          469, 500, 333, 444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500, 500, 500, 333,
          389, 278, 500, 500, 722, 500, 500, 444, 480, 200, 480, 541]
_TIMES_BOLD = [250, 333, 555, 500, 500, 1000, 833, 278, 333, 333, 500, 570, 250, 333, 250, 278, 500, 500, 500, 500,
               500, 500, 500, 500, 500, 500, 333, 333, 570, 570, 570, 500, 930, 722, 667, 722, 722, 667, 611, 778,
               778, 389, 500, 778, 667, 944, 722, 778, 611, 778, 722, 556, 667, 722, 722, 1000, 722, 722, 667, 333,
               278, 333, 581, 500, 333, 500, 556, 444, 556, 444, 333, 500, 556, 278, 333, 556, 278, 833, 556, 500,
               556, 556, 444, 389, 333, 556, 500, 722, 500, 500, 444, 394, 220, 394, 520]
FONTS = {"serif": ("Times-Roman", _TIMES), "serif-bold": ("Times-Bold", _TIMES_BOLD), "mono": ("Courier", None)}
EXTRA = {"‘": 333, "’": 333, "“": 444, "”": 444, "•": 350, "·": 250, "…": 1000}


def _char_width(ch, table):
    if table is None:
        return 600
    if ch in EXTRA:
        return EXTRA[ch]
    code = ord(ch)
    if 32 <= code <= 126:
        return table[code - 32]
    base = unicodedata.normalize("NFKD", ch)[:1]
    if base and 32 <= ord(base) <= 126:
        return table[ord(base) - 32]
    return 500


def width(text, font, size):
    table = FONTS[font][1]
    return sum(_char_width(ch, table) for ch in text) * size / 1000


def wrap(text, font, size, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and width(trial, font, size) > max_width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _encode(text):
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            simple = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore")
            out += simple or b"?"
    return bytes(out).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Page:
    """Draw on one A4 landscape page (842 x 595 points, origin bottom left)."""

    WIDTH, HEIGHT = 842, 595

    def __init__(self):
        self.ops = []

    def colour(self, rgb, stroke=False):
        self.ops.append(("%.3f %.3f %.3f " % rgb + ("RG" if stroke else "rg")).encode())

    def rect(self, x, y, w, h, line=1.0, fill=False):
        self.ops.append(f"{line:.2f} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re {'f' if fill else 'S'}".encode())

    def line(self, x1, y1, x2, y2, width_=1.0):
        self.ops.append(f"{width_:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S".encode())

    def text(self, x, y, text, font="serif", size=12, align="left", spacing=0.0):
        tag = {"serif": "F1", "serif-bold": "F2", "mono": "F3"}[font]
        w = width(text, font, size) + spacing * max(0, len(text) - 1)
        if align == "center":
            x -= w / 2
        elif align == "right":
            x -= w
        self.ops.append(b"BT /" + tag.encode() + f" {size:.2f} Tf {spacing:.2f} Tc {x:.2f} {y:.2f} Td (".encode()
                        + _encode(text) + b") Tj ET")

    def stream(self):
        return b"\n".join(self.ops)


def document(page, title=""):
    """Bytes of a one-page PDF."""
    content = zlib.compress(page.stream())
    fonts = [FONTS["serif"][0], FONTS["serif-bold"][0], FONTS["mono"][0]]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {Page.WIDTH} {Page.HEIGHT}] "
         f"/Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R >> >> /Contents 4 0 R >>").encode(),
        b"<< /Length " + str(len(content)).encode() + b" /Filter /FlateDecode >>\nstream\n" + content + b"\nendstream",
    ] + [f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode() for name in fonts]
    objects.append(b"<< /Title (" + _encode(title) + b") /Producer (Shipshape) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)
