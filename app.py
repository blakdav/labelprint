#!/usr/bin/env python3
"""labelprint — web UI for a networked ZPL thermal label printer."""

import json
import os
import socket
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import graphics

PRINTER_HOST = os.environ.get("PRINTER_HOST", "172.23.24.79")
PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "9100"))
SOCKET_TIMEOUT = 5

DPI = 203

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "state.json"
SIZES_FILE = DATA_DIR / "sizes.json"

# ZPL font 0 is proportional, so a single average ratio misjudges any
# string that isn't mixed-case prose — ALL CAPS runs far wider than
# lowercase and ends up off-centre. These are per-character advances as
# a fraction of font height, close enough for wrapping and centring.
CHAR_WIDTH_RATIO = float(os.environ.get("CHAR_WIDTH_RATIO", "0.39"))
LINE_GAP = 4  # dots of extra leading between wrapped lines

_NARROW = "iljI.,:;'`|!"
_WIDE = "mwMW@"
_CAP_RATIO = 0.62
_LOWER_RATIO = 0.47
_DIGIT_RATIO = 0.55


def char_width(ch: str, font: int) -> float:
    if ch == " ":
        return font * 0.26
    if ch in _NARROW:
        return font * 0.24
    if ch in _WIDE:
        return font * 0.82
    if ch.isupper():
        return font * _CAP_RATIO
    if ch.isdigit():
        return font * _DIGIT_RATIO
    if ch.islower():
        return font * _LOWER_RATIO
    return font * 0.5


def text_width(text: str, font: int) -> float:
    return sum(char_width(c, font) for c in text)

# All dimensions in dots. inches * 203 = dots.
DEFAULT_SIZES = {
    "4x6": {
        "label": '4" x 6"', "w_in": 4, "h_in": 6,
        "pw": 812, "ll": 1218, "margin_x": 40, "margin_y": 50,
        "default_font": 70,
    },
    "4x2": {
        "label": '4" x 2"', "w_in": 4, "h_in": 2,
        "pw": 812, "ll": 406, "margin_x": 30, "margin_y": 30,
        "default_font": 56,
    },
    "2x1": {
        "label": '2" x 1"', "w_in": 2, "h_in": 1,
        "pw": 406, "ll": 203, "margin_x": 18, "margin_y": 18,
        "default_font": 36,
    },
    "1.57x0.78": {
        "label": '1.57" x 0.78"', "w_in": 1.57, "h_in": 0.78,
        "pw": 319, "ll": 158, "margin_x": 14, "margin_y": 12,
        "default_font": 28,
    },
}

# 4x6 is 1218 dots tall, so 1200 is the practical ceiling; a single
# character at that size fills the label.
FONT_MIN, FONT_MAX = 10, 1200
MAX_UPLOAD_MB = 20

# Chunked writes for big graphic jobs, tunable without a rebuild.
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "4096"))
CHUNK_DELAY = float(os.environ.get("CHUNK_DELAY", "0.05"))
# Refuse jobs beyond this rather than risk wedging the printer.
MAX_JOB_CHARS = int(os.environ.get("MAX_JOB_CHARS", "150000"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# --- state -----------------------------------------------------------------

def load_sizes():
    if SIZES_FILE.exists():
        try:
            return json.loads(SIZES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_SIZES


def load_state():
    sizes = load_sizes()
    state = {"stock": next(iter(sizes)), "fonts": {}, "landscape": {}}
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            if saved.get("stock") in sizes:
                state["stock"] = saved["stock"]
            if isinstance(saved.get("fonts"), dict):
                state["fonts"] = saved["fonts"]
            if isinstance(saved.get("landscape"), dict):
                state["landscape"] = saved["landscape"]
        except (json.JSONDecodeError, OSError):
            pass
    return state


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def font_for(state, sizes, stock):
    return int(state["fonts"].get(stock, sizes[stock]["default_font"]))


def landscape_for(state, stock):
    return bool(state["landscape"].get(stock, False))


# --- geometry --------------------------------------------------------------

def metrics(size, font, landscape=False):
    """Derive the wrap box and capacity for a stock, font and orientation.

    In landscape the text is rotated 90 degrees, so the wrap box runs
    along the label's long axis: block width comes from the label length
    and the line budget from the print width.
    """
    if landscape:
        block = size["ll"] - 2 * size["margin_y"]
        usable_h = size["pw"] - 2 * size["margin_x"]
    else:
        block = size["pw"] - 2 * size["margin_x"]
        usable_h = size["ll"] - 2 * size["margin_y"]
    line_h = font + LINE_GAP
    lines = max(1, usable_h // line_h)
    chars_per_line = max(1, int(block / (font * CHAR_WIDTH_RATIO)))
    return {
        "block": block,
        "lines": int(lines),
        "line_h": line_h,
        "chars_per_line": chars_per_line,
        "capacity": int(lines) * chars_per_line,
    }


def wrap_lines(text, chars_per_line, block=None, font=None):
    """Greedy word wrap.

    When `block` and `font` are given, wrapping measures each candidate
    line in dots — necessary because character widths vary enough that a
    character count misjudges ALL CAPS badly. Otherwise it falls back to
    the character budget.

    Explicit newlines are hard breaks — ^FB treats each \\& the same way,
    so a blank line costs a line of the budget just like it does here.
    """
    measured = block is not None and font is not None
    fits = (lambda s: text_width(s, font) <= block) if measured \
        else (lambda s: len(s) <= chars_per_line)

    def split_long(word):
        """Break a word too long for one line."""
        parts = []
        cur = ""
        for ch in word:
            if cur and not fits(cur + ch):
                parts.append(cur)
                cur = ch
            else:
                cur += ch
        return parts, cur

    out = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if fits(candidate):
                line = candidate
            else:
                if line:
                    out.append(line)
                parts, line = split_long(word)
                out.extend(parts)
        out.append(line)
    return out


def count_lines(text, chars_per_line, block=None, font=None):
    return len(wrap_lines(escape_zpl(text).strip(), chars_per_line, block, font))


# --- printer ---------------------------------------------------------------

def send_raw(payload: str):
    """Write a ZPL job to the printer.

    Large graphic jobs go out in chunks with a short pause between them.
    This printer's input buffer is small enough that a single large
    write can leave its parser stuck mid-format, after which every
    later job is swallowed as graphic data until it's power-cycled.
    """
    data = payload.encode("utf-8")
    try:
        with socket.create_connection(
            (PRINTER_HOST, PRINTER_PORT), timeout=SOCKET_TIMEOUT
        ) as sock:
            if len(data) <= CHUNK_SIZE:
                sock.sendall(data)
            else:
                for i in range(0, len(data), CHUNK_SIZE):
                    sock.sendall(data[i:i + CHUNK_SIZE])
                    time.sleep(CHUNK_DELAY)
        return True, "Sent to printer."
    except socket.timeout:
        return False, f"Timed out connecting to {PRINTER_HOST}:{PRINTER_PORT}."
    except OSError as exc:
        return False, f"Could not reach printer: {exc}"


def escape_zpl(text: str) -> str:
    """Strip ZPL control characters so input can't break the format.

    Backslash goes too, which means it must be stripped *before* the
    \\& line-break sequences are inserted (see zpl_body).
    """
    return text.replace("^", "").replace("~", "").replace("\\", "")


def zpl_body(text: str) -> str:
    """Escaped text with newlines turned into ^FB's line-break sequence.

    ^FD ignores a raw newline entirely — without this, paragraphs run
    together into one blob.
    """
    lines = [ln.strip() for ln in escape_zpl(text).strip().splitlines()]
    return "\\&".join(lines)


def build_text_zpl(text, size, font, landscape=False):
    m = metrics(size, font, landscape)
    lines = wrap_lines(escape_zpl(text).strip(), m["chars_per_line"],
                       m["block"], font)
    lines = lines[:m["lines"]]

    head = (
        "^XA"
        "^CI28"
        f"^PW{size['pw']}"
        f"^LL{size['ll']}"
    )

    if not landscape:
        # ^FB handles wrapping and centring fine in the normal
        # orientation, so let the printer do the work.
        text_h = len(lines) * m["line_h"]
        origin_y = max(size["margin_y"], (size["ll"] - text_h) // 2)
        return (
            head
            + f"^FO{size['margin_x']},{origin_y}"
            + f"^A0N,{font},{font}"
            + f"^FB{m['block']},{m['lines']},{LINE_GAP},C"
            + f"^FD{zpl_body(text)}^FS"
            + "^PQ1^XZ"
        )

    # Rotated: ^FB positions unpredictably on this firmware, so place
    # each line as its own field with explicit coordinates.
    #
    # Under ^A0R text reads top-to-bottom (along +y) and the glyph cell
    # extends along +x, so lines stack across the label's width and each
    # line runs down its length.
    text_block = len(lines) * m["line_h"]
    start_x = max(size["margin_x"], (size["pw"] - text_block) // 2)

    fields = []
    for i, line in enumerate(lines):
        body = escape_zpl(line)
        # Centre each line along the axis it runs down. Measured rather
        # than counted: ALL CAPS is ~60% wider than the same number of
        # lowercase characters, which visibly shifts the start point.
        y = max(size["margin_y"],
                int((size["ll"] - text_width(line, font)) / 2))
        # Successive lines stack toward -x under ^A0R: reading the label
        # with the rotated text upright, the first line sits on the far
        # side, so place them in reverse to keep reading order.
        slot = len(lines) - 1 - i
        x = start_x + slot * m["line_h"]
        fields.append(f"^FO{x},{y}^A0R,{font},{font}^FD{body}^FS")

    return head + "".join(fields) + "^PQ1^XZ"


def clamp_font(raw, fallback):
    try:
        return max(FONT_MIN, min(FONT_MAX, int(raw)))
    except (TypeError, ValueError):
        return fallback


# --- routes ----------------------------------------------------------------

@app.route("/")
def index():
    sizes = load_sizes()
    state = load_state()
    stock = state["stock"]
    return render_template(
        "index.html",
        sizes=sizes,
        current=stock,
        font=font_for(state, sizes, stock),
        fonts=state["fonts"],
        landscape=landscape_for(state, stock),
        landscapes=state["landscape"],
        font_min=FONT_MIN,
        font_max=FONT_MAX,
        char_ratio=CHAR_WIDTH_RATIO,
        cap_ratio=_CAP_RATIO,
        lower_ratio=_LOWER_RATIO,
        digit_ratio=_DIGIT_RATIO,
        narrow_chars=_NARROW,
        wide_chars=_WIDE,
        line_gap=LINE_GAP,
        printer=f"{PRINTER_HOST}:{PRINTER_PORT}",
        printer_host=PRINTER_HOST,
        config_url=f"http://{PRINTER_HOST}",
    )


@app.post("/print")
def do_print():
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, message="Nothing to print."), 400

    sizes = load_sizes()
    state = load_state()
    stock = state["stock"]
    size = sizes[stock]
    font = clamp_font(request.form.get("font"), font_for(state, sizes, stock))
    landscape = request.form.get("landscape") in ("1", "true", "on")

    m = metrics(size, font, landscape)
    used = count_lines(text, m["chars_per_line"], m["block"], font)
    if used > m["lines"]:
        return jsonify(
            ok=False,
            message=f"Needs {used} lines, only {m['lines']} fit at {font} dots. "
                    "Shrink the font, shorten the text, or rotate.",
        ), 400

    # Remember font and orientation per stock so they survive a reload.
    state["fonts"][stock] = font
    state["landscape"][stock] = landscape
    save_state(state)

    ok, message = send_raw(build_text_zpl(text, size, font, landscape))
    return jsonify(ok=ok, message=message), (200 if ok else 502)


@app.post("/stock")
def set_stock():
    """Store the loaded stock. Deliberately does NOT send ~JC.

    On this printer's firmware, ~JC over TCP takes the network stack down
    and it does not come back without a power cycle. Media calibration has
    to be done with the feed button on the printer itself.
    """
    sizes = load_sizes()
    stock = request.form.get("stock")
    if stock not in sizes:
        return jsonify(ok=False, message="Unknown stock size."), 400

    state = load_state()
    state["stock"] = stock
    save_state(state)

    return jsonify(
        ok=True,
        message=f"Stock set to {sizes[stock]['label']}.",
        label=sizes[stock]["label"],
        font=font_for(state, sizes, stock),
        landscape=landscape_for(state, stock),
    )


@app.post("/qr")
def print_qr():
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, message="Nothing to encode."), 400

    sizes = load_sizes()
    state = load_state()
    size = sizes[state["stock"]]

    ec = request.form.get("ec", "M").upper()
    if ec not in ("L", "M", "Q", "H"):
        ec = "M"

    caption = (request.form.get("caption") or "").strip()
    cap_font = clamp_font(request.form.get("caption_font"), 0) if caption else 0

    try:
        mag = int(request.form.get("magnification", 0))
    except (TypeError, ValueError):
        mag = 0
    if mag <= 0:
        mag = graphics.suggest_magnification(text, size, ec)
    mag = max(1, min(10, mag))

    modules = graphics.qr_modules(text, ec)
    qr_px = modules * mag
    if qr_px > size["pw"] or qr_px > size["ll"]:
        return jsonify(
            ok=False,
            message=f"QR is {qr_px} dots at magnification {mag}, too big for "
                    f"this label ({size['pw']}x{size['ll']}). Lower the "
                    "magnification or use bigger stock.",
        ), 400

    zpl = graphics.build_qr_zpl(text, size, mag, ec, caption, cap_font)
    ok, message = send_raw(zpl)
    if ok:
        note = "" if mag >= graphics.QR_MIN_MAGNIFICATION else \
            " Magnification is low, so it may not scan reliably."
        message = f"Printed {modules}x{modules} QR at magnification {mag}.{note}"
    return jsonify(ok=ok, message=message), (200 if ok else 502)


@app.post("/qr/info")
def qr_info():
    """Module count and suggested magnification, for the live preview."""
    text = (request.form.get("text") or "").strip()
    sizes = load_sizes()
    size = sizes[load_state()["stock"]]
    ec = request.form.get("ec", "M").upper()
    if ec not in ("L", "M", "Q", "H"):
        ec = "M"
    if not text:
        return jsonify(ok=True, modules=0, suggested=1)
    return jsonify(
        ok=True,
        modules=graphics.qr_modules(text, ec),
        suggested=graphics.suggest_magnification(text, size, ec),
    )


@app.post("/image")
def print_image():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(ok=False, message="No file uploaded."), 400

    sizes = load_sizes()
    size = sizes[load_state()["stock"]]
    rotate = request.form.get("rotate", "auto")
    if rotate not in ("auto", "none", "90", "270"):
        rotate = "auto"
    invert = request.form.get("invert") in ("1", "true", "on")

    try:
        img = graphics.load_image(upload.read(), upload.filename)
    except Exception as exc:
        return jsonify(ok=False, message=f"Could not read that file: {exc}"), 400

    try:
        fitted, rotated = graphics.fit_to_label(img, size, rotate, invert)
        zpl = graphics.build_image_zpl(fitted, size)
    except Exception as exc:
        return jsonify(ok=False, message=f"Could not convert image: {exc}"), 500

    if len(zpl) > MAX_JOB_CHARS:
        return jsonify(
            ok=False,
            message=f"Job is {len(zpl):,} characters, over the "
                    f"{MAX_JOB_CHARS:,} limit. Very detailed images on large "
                    "stock can exceed the printer's buffer. Try smaller stock "
                    "or a simpler image.",
        ), 400

    ok, message = send_raw(zpl)
    if ok:
        message = (f"Printed {upload.filename}"
                   + (" (rotated)" if rotated else "")
                   + f", {len(zpl):,} chars.")
    return jsonify(ok=ok, message=message), (200 if ok else 502)


@app.post("/image/preview")
def image_preview():
    """Render the thresholded bitmap the printer would receive."""
    import base64

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(ok=False, message="No file uploaded."), 400

    sizes = load_sizes()
    size = sizes[load_state()["stock"]]
    rotate = request.form.get("rotate", "auto")
    invert = request.form.get("invert") in ("1", "true", "on")

    try:
        img = graphics.load_image(upload.read(), upload.filename)
        fitted, rotated = graphics.fit_to_label(img, size, rotate, invert)
    except Exception as exc:
        return jsonify(ok=False, message=f"Could not read that file: {exc}"), 400

    # Downscale for transport; the preview only needs to show layout.
    import io
    thumb = fitted.convert("L")
    thumb.thumbnail((600, 900))
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    return jsonify(
        ok=True,
        rotated=rotated,
        image="data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
    )


@app.errorhandler(413)
def too_large(_):
    return jsonify(ok=False, message=f"File is over {MAX_UPLOAD_MB} MB."), 413


@app.get("/status")
def status():
    """Cheap reachability check for the status dot in the header."""
    try:
        with socket.create_connection((PRINTER_HOST, PRINTER_PORT), timeout=2):
            return jsonify(ok=True, host=PRINTER_HOST)
    except OSError as exc:
        return jsonify(ok=False, host=PRINTER_HOST, message=str(exc))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
