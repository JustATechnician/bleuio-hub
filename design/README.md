# BleuIO Hub — static UI mockups

HTML/CSS only. No backend, no `app.js`. Use as a layout reference while you build functionality yourself.

## Files

| File | Purpose |
| --- | --- |
| `styles.css` | Full theme (colors, cards, tables, GATT, macros, modal) |
| `index.html` | Lobby — dongle cards (available / yours / busy) |
| `station.html` | Workstation — Scan, GATT, Macros, Log tabs with sample data |

## Preview

Open in a browser (double-click or):

```bash
# from repo root
python -m http.server 8765 --directory design
# http://127.0.0.1:8765/
```

Tabs on `station.html` switch via CSS only (radio + label).

## Class map (matches live app)

- `top`, `brand`, `mark` — header
- `grid`, `card`, `badge` — lobby
- `station-bar`, `tabs` / `tab-nav` — station chrome
- `toolbar`, `split`, `drawer` — scan layout
- `svc`, `char`, `props` — GATT tree
- `macro-list`, `macro-item`, `step` — macros
- `logbox` — event log
- `modal`, `dialog` — passkey (hidden by default in mockup)

The production UI lives in `app/static/` (`index.html`, `styles.css`, `app.js`).
