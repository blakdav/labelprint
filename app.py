#!/usr/bin/env python3
"""labelprint — web UI for a networked ZPL thermal label printer."""

import json
import os
import socket
from pathlib import Path

from flask import Flask, jsonify, render_template, request

PRINTER_HOST = os.environ.get("PRINTER_HOST", "172.23.24.79")
PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "9100"))
SOCKET_TIMEOUT = 5

DPI = 203

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "state.json"
SIZES_FILE = DATA_DIR / "sizes.json"

# ZPL font 0 is proportional. Average glyph advance lands near 0.55x the
# font height for mixed-case text; the preview uses the same ratio so the
# two agree. Slightly generous, so borderline text shows as overflowing.
CHAR_WIDTH_RATIO = 0.55
LINE_GAP = 4  # dots of extra leading between wrapped lines

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

FONT_MIN, FONT_MAX = 10, 200

app = Flask(__name__)


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
    state = {"stock": next(iter(sizes)), "fonts": {}}
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            if saved.get("stock") in sizes:
                state["stock"] = saved["stock"]
            if isinstance(saved.get("fonts"), dict):
                state["fonts"] = saved["fonts"]
        except (json.JSONDecodeError, OSError):
            pass
    return state


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def font_for(state, sizes, stock):
    return int(state["fonts"].get(stock, sizes[stock]["default_font"]))


# --- geometry --------------------------------------------------------------

def metrics(size, font):
    """Derive the wrap box and capacity for a given stock and font size."""
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


def wrap_lines(text, chars_per_line):
    """Greedy word wrap matching ^FB behaviour closely enough for capacity."""
    out = []
    for para in (text.splitlines() or [""]):
        line = ""
        for word in para.split():
            candidate = f"{line} {word}".strip()
            if len(candidate) <= chars_per_line:
                line = candidate
            else:
                if line:
                    out.append(line)
                # A single word longer than the line gets hard-split.
                while len(word) > chars_per_line:
                    out.append(word[:chars_per_line])
                    word = word[chars_per_line:]
                line = word
        out.append(line)
    return out


# --- printer ---------------------------------------------------------------

def send_raw(payload: str):
    try:
        with socket.create_connection(
            (PRINTER_HOST, PRINTER_PORT), timeout=SOCKET_TIMEOUT
        ) as sock:
            sock.sendall(payload.encode("utf-8"))
        return True, "Sent to printer."
    except socket.timeout:
        return False, f"Timed out connecting to {PRINTER_HOST}:{PRINTER_PORT}."
    except OSError as exc:
        return False, f"Could not reach printer: {exc}"


def escape_zpl(text: str) -> str:
    """Strip ZPL control characters so input can't break the format."""
    return text.replace("^", "").replace("~", "").replace("\\", "")


def build_text_zpl(text, size, font):
    m = metrics(size, font)
    body = escape_zpl(text).strip()
    used = len(wrap_lines(body, m["chars_per_line"]))

    # Centre the text block vertically rather than pinning it to the top.
    text_h = used * m["line_h"]
    origin_y = max(size["margin_y"], (size["ll"] - text_h) // 2)

    return (
        "^XA"
        "^CI28"
        f"^PW{size['pw']}"
        f"^LL{size['ll']}"
        f"^FO{size['margin_x']},{origin_y}"
        f"^A0N,{font},{font}"
        f"^FB{m['block']},{m['lines']},{LINE_GAP},C"
        f"^FD{body}^FS"
        "^PQ1"
        "^XZ"
    )


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
        font_min=FONT_MIN,
        font_max=FONT_MAX,
        char_ratio=CHAR_WIDTH_RATIO,
        line_gap=LINE_GAP,
        printer=f"{PRINTER_HOST}:{PRINTER_PORT}",
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

    m = metrics(size, font)
    used = len(wrap_lines(escape_zpl(text).strip(), m["chars_per_line"]))
    if used > m["lines"]:
        return jsonify(
            ok=False,
            message=f"Needs {used} lines, only {m['lines']} fit at {font} dots. "
                    "Shrink the font or shorten the text.",
        ), 400

    # Remember the font per stock so it survives a reload.
    state["fonts"][stock] = font
    save_state(state)

    ok, message = send_raw(build_text_zpl(text, size, font))
    return jsonify(ok=ok, message=message), (200 if ok else 502)


@app.post("/stock")
def set_stock():
    sizes = load_sizes()
    stock = request.form.get("stock")
    if stock not in sizes:
        return jsonify(ok=False, message="Unknown stock size."), 400

    state = load_state()
    state["stock"] = stock
    save_state(state)

    ok, message = send_raw("~JC")
    if ok:
        message = (f"Stock set to {sizes[stock]['label']}. "
                   "Calibrating — expect a few blanks.")
    return jsonify(
        ok=ok, message=message,
        label=sizes[stock]["label"],
        font=font_for(state, sizes, stock),
    ), (200 if ok else 502)


@app.post("/calibrate")
def calibrate():
    ok, message = send_raw("~JC")
    if ok:
        message = "Calibrating — expect a few blank labels."
    return jsonify(ok=ok, message=message), (200 if ok else 502)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
