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

# ZPL font 0 is proportional. Measured against real 203 dpi output: a
# 732-dot block at font 70 fits ~27 mixed-case characters, so the average
# glyph advance is ~0.39x the font height. Tunable without a rebuild if a
# different font or character mix shifts it.
CHAR_WIDTH_RATIO = float(os.environ.get("CHAR_WIDTH_RATIO", "0.39"))
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


def wrap_lines(text, chars_per_line):
    """Greedy word wrap matching ^FB behaviour closely enough for capacity.

    Explicit newlines are hard breaks — ^FB treats each \\& the same way,
    so a blank line costs a line of the budget just like it does here.
    """
    out = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        line = ""
        for word in words:
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


def count_lines(text, chars_per_line):
    return len(wrap_lines(escape_zpl(text).strip(), chars_per_line))


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
    body = zpl_body(text)
    used = count_lines(text, m["chars_per_line"])
    text_h = used * m["line_h"]

    if landscape:
        # ^A0R rotates 90 degrees clockwise: the block runs down the
        # label from the origin, and successive lines advance leftward.
        # So the origin sits on the right and moves left to centre.
        rot = "R"
        origin_x = min(size["pw"] - size["margin_x"],
                       (size["pw"] + text_h) // 2)
        origin_y = size["margin_y"]
    else:
        rot = "N"
        origin_x = size["margin_x"]
        origin_y = max(size["margin_y"], (size["ll"] - text_h) // 2)

    return (
        "^XA"
        "^CI28"
        f"^PW{size['pw']}"
        f"^LL{size['ll']}"
        f"^FO{origin_x},{origin_y}"
        f"^A0{rot},{font},{font}"
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
        landscape=landscape_for(state, stock),
        landscapes=state["landscape"],
        font_min=FONT_MIN,
        font_max=FONT_MAX,
        char_ratio=CHAR_WIDTH_RATIO,
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
    used = count_lines(text, m["chars_per_line"])
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
