# labelprint

Web UI for a networked ZPL thermal label printer (Westinghouse WHTP203e).
Type text, hit print. No CUPS, no drivers — raw ZPL over TCP 9100.

## Run

```sh
pip install -r requirements.txt
PRINTER_HOST=172.23.24.79 python app.py
```

Then open http://localhost:8080

## Docker

Images build on push to `main` and publish to
`ghcr.io/blakdav/labelprint:latest`.

```sh
docker compose up -d
```

Pull a specific build with the short-SHA or `vX.Y.Z` tag instead of
`latest`.

## Config

| Env | Default | Notes |
| --- | --- | --- |
| `PRINTER_HOST` | `172.23.24.79` | Printer IP (set a DHCP reservation) |
| `PRINTER_PORT` | `9100` | Raw ZPL socket |
| `DATA_DIR` | `./data` | Holds `state.json`, optional `sizes.json` |
| `PORT` | `8080` | HTTP listen port |

## Label stocks

Sizes live in `DEFAULT_SIZES` in `app.py`, overridable by dropping a
`sizes.json` in `DATA_DIR`. All dimensions are **dots**: inches × 203.

| Key | Label | Dots |
| --- | --- | --- |
| `4x6` | 4" × 6" | 812 × 1218 |
| `4x2` | 4" × 2" | 812 × 406 |
| `2x1` | 2" × 1" | 406 × 203 |
| `1.57x0.78` | 1.57" × 0.78" | 319 × 158 |

Each entry stores `pw`, `ll`, `margin_x`, `margin_y` and `default_font`.
Line count and characters-per-line are **derived** from the font size at
print time, not stored — so a smaller font yields more lines
automatically.

- `pw` — print width. Larger than the physical label **clips silently**.
- `margin_*` — whitespace on each edge; the wrap box is what's left.
- `default_font` — starting font height in dots for that stock.

## Font

Font height is picked per print: S/M/L presets (0.6× / 1× / 1.5× the
stock default), a free number input, or **Fit**, which finds the largest
size at which the text still fits. The chosen font is remembered per
stock in `state.json`.

## Orientation

Portrait or landscape, per print, remembered per stock. On 4x6 that's
40 chars x 9 lines landscape versus 26 x 15 portrait at font 70.

The two orientations emit different ZPL:

- **Portrait** uses `^FB`, letting the printer wrap and centre.
- **Landscape** does *not*. This firmware positions `^FB` unpredictably
  when combined with a rotated font (`^A0R`), so each wrapped line is
  emitted as its own `^FO`/`^FD` field at explicit coordinates. Wrapping
  is done in Python — the same `wrap_lines` the preview and capacity
  check already use.

Landscape assumes `^A0R` text reads top-to-bottom along +y with the
glyph body extending along +x, so lines stack across the label width and
each line runs down its length. If output is mirrored or offset, those
two assumptions in `build_text_zpl` are the knobs.

## Preview

The canvas preview renders locally — no external service. It mirrors the
server's wrap logic and shows the wrap box, so overflow is visible before
printing. Character widths are estimated (`CHAR_WIDTH_RATIO`), so it's a
fit check rather than a proof; the server rejects genuine overflow
anyway, since `^FB` drops extra lines silently.

## Printer help

A collapsed "Printer help" section in the UI covers the manual
recalibration sequence (power off 3s → hold feed → power on → release on
beep/blink), the feed-button LED codes, and a link to the printer's own
config page at `http://$PRINTER_HOST`.

That link is derived from `PRINTER_HOST`, so pointing the app at a
different address updates it too — there's one place to change. The app
can't discover the printer on its own; set a DHCP reservation so the
address doesn't move.

A dot next to the address in the header polls `/status` every 30s and
turns green or red depending on whether port 9100 accepts a connection.

## Notes

- **Calibration must be done at the printer.** Sending `~JC` over TCP
  takes this printer's network stack down and it does not recover
  without a power cycle — reproducible, so the app never sends it. After
  swapping rolls, use the feed-button sequence in the printer manual.
- The firmware handles ZPL *drawing* commands (`^FO`, `^FB`, `^A0`,
  `^GF`) reliably; it's the configuration and control commands that are
  unsafe. Test any new `~` command before wiring it to a button.
- `^`, `~`, `\` are stripped from input — they're ZPL control chars.
  Stripping happens *before* newlines become `\&`, so typing `\&`
  yourself can't inject a line break.
- `^FD` ignores raw newlines. Blank lines and paragraph breaks are
  converted to `\&`, which `^FB` treats as a hard break.
- `CHAR_WIDTH_RATIO` (default 0.39) is measured from real output: a
  732-dot block at font 70 fits ~27 characters. Override it with the
  env var if your text runs consistently wider or narrower than the
  preview predicts.
- `^FB` drops overflow lines silently, so the server rejects text that
  needs more lines than fit rather than printing a truncated label.

## Modes

Three tabs in the print panel.

**Text** — wrapped, centred, portrait or landscape. See Orientation above.

**QR** — native `^BQ`, so the printer renders the code at full
resolution. Magnification defaults to the largest that fits the loaded
stock; a caption can be printed underneath. Below magnification 4 the
UI warns, since thermal printing plus a phone camera gets unreliable
at 3 dots per module. A 33-module code (short URL) needs ~130 dots at
magnification 4, so 4x6 and 4x2 are comfortable, 2x1 works, and
1.57x0.78 only suits short data.

**Image** — GIF/PNG/JPG/BMP/WEBP/PDF, converted to `^GF`. PDFs render
page 1 at 300 dpi then downscale, which survives thresholding better
than rendering straight to 203. Images are letterboxed rather than
cropped: a cropped barcode is worse than a smaller one. Auto-rotate
turns the image when its long axis disagrees with the label's, which is
what Amazon's 1800x1200 return-label GIFs need. Preview shows the actual
thresholded bitmap the printer will receive.

Thresholding is a plain cut at 128 with no dithering — every real input
here is high-contrast line art, and dithering makes barcodes mushy.

Uploads are capped at 20 MB.
