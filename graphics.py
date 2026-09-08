"""Image and QR handling for labelprint.

Turns an uploaded GIF/PNG/JPG/PDF into a ZPL ^GF graphic field sized for
the loaded label stock, and builds ^BQ QR formats.
"""

import io

from PIL import Image

# ~GF/^GF pack pixels one bit each, rows padded up to whole bytes.
# Threshold rather than dither: every real input here (shipping labels,
# QR screenshots) is already high-contrast line art, and dithering makes
# barcodes and small text mushy.
THRESHOLD = 128

# Below this many dots per QR module, thermal printing plus a phone
# camera stops being reliable.
QR_MIN_MAGNIFICATION = 4


def load_image(data: bytes, filename: str = "") -> Image.Image:
    """Decode an upload to a PIL image, rendering page 1 of a PDF."""
    if data[:5] == b"%PDF-" or filename.lower().endswith(".pdf"):
        return _render_pdf(data)
    return Image.open(io.BytesIO(data))


def _render_pdf(data: bytes, dpi: int = 300) -> Image.Image:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(io.BytesIO(data))
    try:
        if len(doc) == 0:
            raise ValueError("PDF has no pages.")
        # 300 dpi then downscale beats rendering straight to 203: the
        # extra detail survives thresholding much better.
        page = doc[0]
        return page.render(scale=dpi / 72).to_pil()
    finally:
        doc.close()


def fit_to_label(img, size, rotate="auto", invert=False):
    """Scale an image to fit the label, rotating when it helps.

    Returns (1-bit image, was_rotated). Aspect ratio is preserved and the
    result is letterboxed on white — cropping to fill risks cutting a
    barcode, which is the one thing that must survive intact.
    """
    img = img.convert("L")
    if invert:
        img = Image.eval(img, lambda p: 255 - p)

    target_w, target_h = size["pw"], size["ll"]

    rotated = False
    if rotate == "90":
        img, rotated = img.rotate(-90, expand=True), True
    elif rotate == "270":
        img, rotated = img.rotate(90, expand=True), True
    elif rotate == "auto":
        # Rotate when the image and the label disagree about which way
        # is long — an 1800x1200 shipping label onto portrait 4x6.
        # PIL rotates counter-clockwise, so -90 is the clockwise turn
        # that lands a landscape label upright.
        img_landscape = img.width > img.height
        label_landscape = target_w > target_h
        if img_landscape != label_landscape:
            img, rotated = img.rotate(-90, expand=True), True

    scale = min(target_w / img.width, target_h / img.height)
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    # Nearest-neighbour on exact integer downscales keeps QR modules and
    # barcode bars crisp; LANCZOS grey-edges them and thresholding then
    # shifts bar widths unpredictably.
    resample = Image.NEAREST if scale <= 1 and abs(scale - round(scale)) < 1e-9 \
        else Image.LANCZOS
    img = img.resize((new_w, new_h), resample)

    canvas = Image.new("L", (target_w, target_h), 255)
    canvas.paste(img, ((target_w - new_w) // 2, (target_h - new_h) // 2))

    return canvas.point(lambda p: 0 if p < THRESHOLD else 255, "1"), rotated


def to_grf(img) -> tuple:
    """Pack a 1-bit image into ^GF hex. Returns (hex, total, row_bytes)."""
    if img.mode != "1":
        img = img.convert("1")
    w, h = img.size
    row_bytes = (w + 7) // 8
    px = img.load()

    out = bytearray()
    for y in range(h):
        row = bytearray(row_bytes)
        for x in range(w):
            # Mode "1": 0 is black, and in ZPL a set bit prints.
            if px[x, y] == 0:
                row[x >> 3] |= 0x80 >> (x & 7)
        out += row

    return out.hex().upper(), len(out), row_bytes


# ZPL's ASCII run-length codes: G-Y are counts 1-19, g-z are 20-400 in
# steps of 20. A count is built from at most one of each, high first.
_HIGH = {i: chr(ord("g") + i - 1) for i in range(1, 21)}      # 20..400
_LOW = {i: chr(ord("G") + i - 1) for i in range(1, 20)}       # 1..19


def _rle(count: int, char: str) -> str:
    """Encode `count` repeats of a hex nibble using ZPL run-length codes."""
    out = []
    while count > 0:
        if count >= 20:
            n = min(20, count // 20)
            out.append(_HIGH[n])
            count -= n * 20
        else:
            out.append(_LOW[count])
            count = 0
    return "".join(out) + char


def compress_grf(hex_data: str, row_bytes: int) -> str:
    """Compress ^GF hex with ZPL's own scheme.

    Shipping labels are mostly white, so this typically cuts the payload
    by 10-30x. Without it a 4x6 at 203 dpi is ~248,000 characters, which
    overruns this printer's buffer and wedges its parser mid-format.

    Codes used: run-length for repeated nibbles, `,` to fill the rest of
    a row with zeros, `!` to fill with ones, and `:` to repeat the row
    above verbatim.
    """
    row_chars = row_bytes * 2
    rows = [hex_data[i:i + row_chars] for i in range(0, len(hex_data), row_chars)]

    out = []
    prev = None
    for row in rows:
        if row == prev:
            out.append(":")
            continue
        prev = row

        # Trailing runs of one nibble collapse to a single , or !
        stripped, tail = row, ""
        if row.endswith("0") and len(set(row[len(row.rstrip("0")):])) <= 1:
            stripped, tail = row.rstrip("0"), ","
        elif row.endswith("F") and len(set(row[len(row.rstrip("F")):])) <= 1:
            stripped, tail = row.rstrip("F"), "!"

        buf = []
        i = 0
        while i < len(stripped):
            ch = stripped[i]
            run = 1
            while i + run < len(stripped) and stripped[i + run] == ch:
                run += 1
            buf.append(_rle(run, ch) if run > 2 else ch * run)
            i += run

        out.append("".join(buf) + tail)

    return "".join(out)


def build_image_zpl(img, size, compress=True) -> str:
    data, total, row_bytes = to_grf(img)
    payload = compress_grf(data, row_bytes) if compress else data
    return (
        "^XA"
        f"^PW{size['pw']}"
        f"^LL{size['ll']}"
        "^FO0,0"
        f"^GFA,{total},{total},{row_bytes},{payload}"
        "^FS"
        "^PQ1"
        "^XZ"
    )


def qr_modules(text: str, ec: str = "M") -> int:
    """Module count per side for the QR this data will produce."""
    try:
        import segno
        return segno.make(text, error=ec).symbol_size()[0]
    except Exception:
        # Rough fallback: version grows ~4 modules per step, and a
        # version-N code is 4N+17 modules across.
        n = len(text)
        version = 1
        while version < 40 and _qr_capacity(version, ec) < n:
            version += 1
        return 4 * version + 17


def _qr_capacity(version: int, ec: str) -> int:
    # Byte-mode capacity, approximated; only used when segno is absent.
    base = {"L": 17, "M": 14, "Q": 11, "H": 7}.get(ec, 14)
    return int(base * (version ** 1.85))


def suggest_magnification(text, size, ec="M"):
    """Largest magnification whose QR still fits the label."""
    modules = qr_modules(text, ec)
    room = min(size["pw"] - 2 * size["margin_x"],
               size["ll"] - 2 * size["margin_y"])
    return max(1, min(10, room // max(1, modules)))


def build_qr_zpl(text, size, magnification, ec="M", caption="", font=0):
    """QR centred on the label, with an optional caption underneath."""
    modules = qr_modules(text, ec)
    qr_px = modules * magnification

    caption = caption.strip()
    cap_h = (font + 8) if (caption and font) else 0

    total_h = qr_px + cap_h
    top = max(size["margin_y"], (size["ll"] - total_h) // 2)
    left = max(0, (size["pw"] - qr_px) // 2)

    parts = [
        "^XA",
        "^CI28",
        f"^PW{size['pw']}",
        f"^LL{size['ll']}",
        f"^FO{left},{top}",
        f"^BQN,2,{magnification}",
        f"^FDQA,{_escape(text)}^FS",
    ]

    if cap_h:
        block = size["pw"] - 2 * size["margin_x"]
        parts += [
            f"^FO{size['margin_x']},{top + qr_px + 8}",
            f"^A0N,{font},{font}",
            f"^FB{block},1,0,C",
            f"^FD{_escape(caption)}^FS",
        ]

    parts += ["^PQ1", "^XZ"]
    return "".join(parts)


def _escape(text: str) -> str:
    return text.replace("^", "").replace("~", "").replace("\\", "")
