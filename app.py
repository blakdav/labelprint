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

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_FILE = DATA_DIR / "state.json"
SIZES_FILE = DATA_DIR / "sizes.json"

# Measured on the WHTP203e at 203 dpi. inches * 203 = dots.
DEFAULT_SIZES = {
    "4x6": {
        "label": '4" x 6" shipping',
        "pw": 812, "ll": 1218,
        "origin_x": 40, "origin_y": 60,
        "font": 70, "block": 730, "lines": 6,
    },
    "1.57x0.78": {
        "label": '1.57" x 0.78" small',
        "pw": 319, "ll": 158,
        "origin_x": 15, "origin_y": 45,
        "font": 28, "block": 290, "lines": 2,
    },
}

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
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("stock") in sizes:
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"stock": next(iter(sizes))}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --- printer ---------------------------------------------------------------

def send_raw(payload: str):
    """Open a socket to the printer and write bytes. Returns (ok, message)."""
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
    """Strip ZPL control characters so user input can't break the format."""
    return text.replace("^", "").replace("~", "").replace("\\", "")


def build_text_zpl(text: str, size: dict) -> str:
    body = escape_zpl(text).strip()
    return (
        "^XA"
        "^CI28"
        f"^PW{size['pw']}"
        f"^LL{size['ll']}"
        f"^FO{size['origin_x']},{size['origin_y']}"
        f"^A0N,{size['font']},{size['font']}"
        f"^FB{size['block']},{size['lines']},4,C"
        f"^FD{body}^FS"
        "^PQ1"
        "^XZ"
    )


# --- routes ----------------------------------------------------------------

@app.route("/")
def index():
    sizes = load_sizes()
    state = load_state()
    return render_template(
        "index.html",
        sizes=sizes,
        current=state["stock"],
        current_label=sizes[state["stock"]]["label"],
        printer=f"{PRINTER_HOST}:{PRINTER_PORT}",
    )


@app.post("/print")
def do_print():
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, message="Nothing to print."), 400

    sizes = load_sizes()
    size = sizes[load_state()["stock"]]

    # ^FB silently drops overflow, so warn rather than print a truncated label.
    capacity = size["lines"] * max(1, size["block"] // max(1, size["font"] // 2))
    if len(text) > capacity:
        return jsonify(
            ok=False,
            message=f"Too long for this stock (~{capacity} chars max). "
                    "Shorten it or switch to a larger label.",
        ), 400

    ok, message = send_raw(build_text_zpl(text, size))
    return jsonify(ok=ok, message=message), (200 if ok else 502)


@app.post("/stock")
def set_stock():
    sizes = load_sizes()
    stock = request.form.get("stock")
    if stock not in sizes:
        return jsonify(ok=False, message="Unknown stock size."), 400

    save_state({"stock": stock})
    ok, message = send_raw("~JC")
    label = sizes[stock]["label"]
    if ok:
        message = f"Stock set to {label}. Calibrating — expect a few blanks."
    return jsonify(ok=ok, message=message, label=label), (200 if ok else 502)


@app.post("/calibrate")
def calibrate():
    ok, message = send_raw("~JC")
    if ok:
        message = "Calibrating — expect a few blank labels."
    return jsonify(ok=ok, message=message), (200 if ok else 502)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
