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
`sizes.json` in `DATA_DIR`. All units are **dots**: inches × 203 (the
printer is 203 dpi).

```json
{
  "4x6": {
    "label": "4\" x 6\" shipping",
    "pw": 812, "ll": 1218,
    "origin_x": 40, "origin_y": 60,
    "font": 70, "block": 730, "lines": 6
  }
}
```

- `pw` — print width. Larger than the physical label **clips silently**.
- `ll` — label length.
- `block`/`lines` — `^FB` wrap box. Overflow is silently dropped, so the
  app length-checks before sending.

The currently loaded stock is remembered in `state.json` so you don't
pick a size on every print.

## Notes

- "Set & calibrate" stores the new stock **and** sends `~JC`. Run it
  whenever you swap rolls — the gap sensor is tuned per media.
- If `~JC` does nothing, the firmware may not implement it; use the
  feed-button sequence in the printer manual instead.
- `^`, `~`, `\` are stripped from input — they're ZPL control chars.

## TODO

- Image mode: upload GIF/PNG/JPG/PDF → rotate → scale → `^GF`
  (Amazon return labels arrive as 1800×1200 two-colour GIFs, which map
  exactly onto 4x6 at 203 dpi)
