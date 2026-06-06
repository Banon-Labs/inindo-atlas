# Inindo Atlas

Generate maps for **Inindo — Way of the Ninja** (SNES) directly in your browser from your own ROM.

**Live site:** https://banon-labs.github.io/inindo-atlas/

Upload your `.sfc` ROM and the atlas reconstructs the map **client-side** (via [Pyodide](https://pyodide.org/)) — terrain and tile graphics straight from the ROM, walkability, the real in-game objects, and A\* routes to each searchable object.

- **The ROM never leaves your machine.** Everything runs in your browser; nothing is uploaded to any server. No ROM is included in this repo.
- **Map (1:1 render)** — cold-ROM terrain + tile graphics.
- **Walkability** — passable vs blocked half-tiles.
- **A\* routes** — drawn as the player character, hue-cycled per route. Routes model Inindo's *diagonal corner-slip* movement (you slip diagonally when you press into a wall with an open side), and the step count shows both the corner-slip-aware count and the naive 4-connectivity baseline, e.g. `149 steps (165 without corner-slip)`.
- **Objects + Sprites** — the real in-game object graphics (searchable shrines, blocking braziers), decoded from the ROM; click one for its contents/dialog and route.
- **Markers** — analytical POIs (a separate toggle).

First slice: the Password cave. More maps are layered on next.

## How it works

`index.html` loads Pyodide and runs `atlas_pipeline.py` (pure-Python, no third-party deps) over the uploaded ROM bytes, rendering to a canvas. All offsets/algorithms are validated cold-ROM facts from the reverse-engineering work.

## Files

- `index.html` — the upload UI + Pyodide glue.
- `atlas_pipeline.py` — the ROM→image pipeline (render, walkability, A\*, objects).
