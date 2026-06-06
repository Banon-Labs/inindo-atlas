"""Inindo atlas pipeline — pure-Python, ROM-bytes-in, image-out. Designed to run
both in plain CPython (testing) and in Pyodide (browser, client-side on an uploaded
ROM). No file IO, no third-party deps. All offsets/algorithms are the validated
cold-ROM facts from the reverse-engineering work.

Public entry points (all take `rom: bytes`):
  render_cave_structural(rom) -> (w, h, rgba_bytes)   # per-quadrant 16x16 metatile render
  walkable_overlay(rom)       -> (w, h, rgba_bytes)   # passability tint over the render
  cave_walkable_cells(rom)    -> set[(hx,hy)]         # reachable walkable half-tiles

This is the first vertical slice (Password cave). Floor 96BC-CHR fidelity, A* routes,
and the map-directory selector for other maps are layered on next.
"""

# --- validated ROM offsets (Password cave, headerless LoROM == file offset) ---
CAVE_GRID = 0x06829B          # $0D:829B  RLE terrain grid
CAVE_DIMS = (50, 21)          # cells (w, h)
LUT = 0x068055                # $0D:8055  metatile/attr table, 4 bytes/tile-id
CHR_ROM = 0x09CD80            # $13:CD80  4bpp CHR, chr index 0x20 origin, 32B/tile
CHR_FIRST = 0x20
PAL_ROM = 0x0159E0            # $02:D9E0  pal 2 (CGRAM colours 0x20..0x2F), BGR555
WALK_THRESH = 0x19            # walkable iff metatile attr byte >= 0x19
OBJ_RES = 0x068205            # $0D:8205  per-map object/event resource (cave)
# Item table: id 0 ("Sword") at 0x01DBBA, 17B entries [5 data][12B null-padded name].
# A searchable field object's avail-row `id` is a DIRECT index here -> the item it
# grants/references. Derived purely from data and confirmed against three independent
# ground-truth points: both id-0x5F chests (26,2)+(94,35) -> item[0x5F]="Medicine";
# the id-0x6F sign -> item[0x6F]="Password" (matches its "This way to Password" text).
# `flags` is the per-object opened/save bit (0x11 vs 0x12 distinguish the two chests),
# NOT the contents. The 0x01DBA9 "s?" entry one stride earlier is a pre-table sentinel.
ITEM_TABLE = 0x01DBBA
ITEM_ENTRY = 17
ITEM_NAME_OFF = 5
ITEM_NAME_LEN = 12

# --- Field OBJECT graphics (the things the player actually sees drawn on the map) ---
# The visible field objects (searchable shrines, blocking braziers) are NOT OAM sprites:
# the ONLY OAM sprite on the field is the player (confirmed across a 359-frame cave-walk
# OAM capture). The objects are 16x16 BG-layer tiles drawn over the terrain by the game's
# "redraw object cell" path. A selector cell's ACTION code maps to the object's BG char
# via the native field renderer (bank $04):
#   char = 0x1A0 + ((a-0x29)>>3)*0x20 + ((a-0x29)&7)*2  ->  0x2B->0x1A4, 0x2C->0x1A6
# The object CHR is a CONTIGUOUS ROM block (NOT the terrain CHR page): each object = 4
# tiles stored TL,TR,BL,BR consecutively (0x80 bytes); char 0x1A4's block is at 0x0AE9D8
# and the block advances 0x80 per +2 chars. Drawn with BG palette 5 (cold ROM 0x015AE0).
# CHR + palette were verified pixel-identical to a real cave-walk keyframe capture
# (chest -> a glowing shrine, blocker -> a flaming brazier).
OBJ_CHR_BLOCK = 0x0AE9D8      # object CHR block: char 0x1A4 -> here; +0x80 per +2 chars
OBJ_CHR_REF = 0x1A4           # char whose block sits at OBJ_CHR_BLOCK
OBJ_PAL_ROM = 0x015AE0        # BG palette 5: 16 BGR555 colours (index 0 = transparent)
OBJ_ACTION_BASE = 0x1A0

# --- Player sprite (used to draw A* routes as the character, hue-cycled per route) ---
# The player is the only OAM sprite on the field (verified across the cave-walk OAM).
# 16x16, natural OBJ palette 5 (the blue ninja). CHR is stored LINEARLY at 0x0D2D00
# (char 0x0E0 origin; char N at +((N-0xE0)*0x20)). Facing chars: up/back 0x0E0,
# down/front 0x0E4, side 0x0E8 (h-flipped for the opposite side). All verified
# pixel-identical to live cave-walk keyframes. Each route hue-shifts the palette so
# overlapping routes read as distinct colours (replacing the old coloured route dots).
PLAYER_CHR = 0x0D2D00
PLAYER_CHR_REF = 0x0E0
PLAYER_PAL_ROM = 0x015900     # player OBJ palette 5
PLAYER_FACE = {"up": 0x0E0, "down": 0x0E4, "side": 0x0E8}


def item_name(rom, item_id):
    """Resolve a searchable object's `id` to its item name via the item table. Returns
    None if the id is out of range or the entry isn't printable (not an item)."""
    e = ITEM_TABLE + item_id * ITEM_ENTRY
    if e < 0 or e + ITEM_NAME_OFF + ITEM_NAME_LEN > len(rom):
        return None
    s = rom[e + ITEM_NAME_OFF: e + ITEM_NAME_OFF + ITEM_NAME_LEN].split(b"\x00")[0]
    return s.decode("latin1") if s and all(32 <= b < 127 for b in s) else None


# ---------- RLE grid ($7E:70B9) ----------
def decode_grid(rom, off, w, h):
    total = w * h; out = []; p = off
    while len(out) < total:
        ctrl = rom[p]
        if ctrl & 0x80:
            out.append(ctrl & 0x7F); p += 1
        else:
            out.extend([ctrl] * rom[p + 1]); p += 2
    return [out[r * w:(r + 1) * w] for r in range(h)]


# ---------- SNES 4bpp + palette ----------
def _bgr555(lo, hi):
    v = lo | (hi << 8)
    return ((v & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, ((v >> 10) & 31) * 255 // 31)


def _decode_4bpp(rom, off):
    """32-byte 4bpp tile -> 8x8 palette indices."""
    px = [[0] * 8 for _ in range(8)]
    for y in range(8):
        p0, p1 = rom[off + y * 2], rom[off + y * 2 + 1]
        p2, p3 = rom[off + 16 + y * 2], rom[off + 16 + y * 2 + 1]
        for x in range(8):
            b = 7 - x
            px[y][x] = ((p0 >> b) & 1) | (((p1 >> b) & 1) << 1) | (((p2 >> b) & 1) << 2) | (((p3 >> b) & 1) << 3)
    return px


def _palette(rom):
    # pal 2 occupies CGRAM colours 0x20..0x2F; render uses palnum 2 -> base 0x20
    pal = [(0, 0, 0)] * 0x30
    for c in range(0x10):
        lo, hi = rom[PAL_ROM + c * 2], rom[PAL_ROM + c * 2 + 1]
        pal[0x20 + c] = _bgr555(lo, hi)
    return pal


def _bg_word(chr_idx):
    return 0x0820 + (((chr_idx << 2) & 0xFFE0) + ((chr_idx & 7) << 1))


def _chr_tile(rom, chr_slot, cache):
    """chr_slot is a synthetic-VRAM slot index; map back to ROM CHR and decode."""
    t = cache.get(chr_slot)
    if t is None:
        ro = CHR_ROM + (chr_slot - CHR_FIRST) * 32
        t = _decode_4bpp(rom, ro) if 0 <= ro and ro + 32 <= len(rom) else [[0] * 8 for _ in range(8)]
        cache[chr_slot] = t
    return t


# The cave is rendered at 2x native (each half-tile = 16 px, each metatile = 32 px) so that
# the field objects — placed at half-tile coords — sit at their true in-game spacing
# (one tile apart, matching real screenshots). HALF_PX is the pixels-per-half-tile used by
# every cell->pixel conversion below; the base terrain render is upscaled 2x to match.
HALF_PX = 16


def _upscale2x(pw, ph, rgba):
    pw2, ph2 = pw * 2, ph * 2
    out = bytearray(pw2 * ph2 * 4)
    for y in range(ph2):
        srow = (y // 2) * pw
        for x in range(pw2):
            s = (srow + (x // 2)) * 4
            d = (y * pw2 + x) * 4
            out[d:d + 4] = rgba[s:s + 4]
    return pw2, ph2, bytes(out)


# ---------- structural map render (per-quadrant 16x16 metatile, upscaled 2x) ----------
def render_cave_structural(rom):
    w, h = CAVE_DIMS
    grid = decode_grid(rom, CAVE_GRID, w, h)
    pal = _palette(rom)
    pw, ph = w * 16, h * 16
    rgba = bytearray(pw * ph * 4)
    cache = {}
    for r in range(h):
        for c in range(w):
            tid = grid[r][c]
            for q in range(4):
                chr_byte = rom[LUT + tid * 4 + q]
                slot = _bg_word(chr_byte) & 0x3FF
                tile = _chr_tile(rom, slot, cache)
                ox = c * 16 + (q & 1) * 8
                oy = r * 16 + ((q >> 1) & 1) * 8
                for yy in range(8):
                    for xx in range(8):
                        cr, cg, cb = pal[0x20 + tile[yy][xx]]
                        i = ((oy + yy) * pw + (ox + xx)) * 4
                        rgba[i] = cr; rgba[i + 1] = cg; rgba[i + 2] = cb; rgba[i + 3] = 255
    return _upscale2x(pw, ph, bytes(rgba))


# ---------- high-fidelity floor render (full CHR page) ----------
# Investigation note (atlas/floor_autotile_findings.md): the cave floor is NOT
# neighbour-autotiled. The live BG2 tilemap equals the metatile-table prediction
# byte-for-byte (medicine_005C_0023 capture: 0 substitutions). "Decorated floor"
# char 0x100 is just a per-sub-area floor metatile (chr index 0x38). Its pixels are
# uncompressed in ROM at 0x09E980, which is the natural linear continuation of the
# cave CHR page: CHR_ROM(0x09CD80) + (0x100 - 0x20)*32 = 0x09E980. So a faithful
# render only needs to source CHR over the full linear page (no special case, no
# autotile pass). Cold-ROM CHR == live VRAM CHR over all 88 used chars = 100% exact.
CHR_HIGH_MAX = 0x200          # render chr slots up to char 0x1FF (covers the 0x100 page)


def _chr_tile_full(rom, chr_slot, cache):
    """Like _chr_tile but valid across the whole linear cave CHR page, including the
    char>=0x100 region (e.g. char 0x100 = chr index 0x38 -> ROM 0x09E980). Out-of-ROM
    slots decode to a blank tile."""
    t = cache.get(chr_slot)
    if t is None:
        ro = CHR_ROM + (chr_slot - CHR_FIRST) * 32
        t = (_decode_4bpp(rom, ro) if 0 <= ro and ro + 32 <= len(rom)
             else [[0] * 8 for _ in range(8)])
        cache[chr_slot] = t
    return t


def render_cave_high_fidelity(rom):
    """Pixel-faithful cave render: identical to render_cave_structural but sources
    CHR over the full linear page so any metatile chr index (incl. chr 0x38 -> char
    0x100 decorated floor at ROM 0x09E980) resolves to its real cold-ROM pixels.
    For the password-cave grid this is pixel-identical to render_cave_structural
    (that grid never references the 0x100 page); the function generalises the floor
    CHR so maps whose metatile table uses the 0x100 page render real floor, not garbage.
    Returns (w, h, rgba_bytes)."""
    w, h = CAVE_DIMS
    grid = decode_grid(rom, CAVE_GRID, w, h)
    pal = _palette(rom)
    pw, ph = w * 16, h * 16
    rgba = bytearray(pw * ph * 4)
    cache = {}
    for r in range(h):
        for c in range(w):
            tid = grid[r][c]
            for q in range(4):
                chr_byte = rom[LUT + tid * 4 + q]
                slot = _bg_word(chr_byte) & 0x3FF
                tile = _chr_tile_full(rom, slot, cache)
                ox = c * 16 + (q & 1) * 8
                oy = r * 16 + ((q >> 1) & 1) * 8
                for yy in range(8):
                    for xx in range(8):
                        cr, cg, cb = pal[0x20 + tile[yy][xx]]
                        i = ((oy + yy) * pw + (ox + xx)) * 4
                        rgba[i] = cr; rgba[i + 1] = cg; rgba[i + 2] = cb; rgba[i + 3] = 255
    return pw, ph, bytes(rgba)


# ---------- manifest-driven multi-map render ----------
# Inindo has NO static map directory (docs/reverse-engineering/map-generalization-negative-result.md),
# so each area's ROM resource offsets are derived once from a runtime capture (savestate) via
# work/derive_map_manifest.py and recorded here. The pipeline then renders any listed map straight
# from the uploaded ROM. New towns/dungeons are added by capturing them and appending an entry.
MANIFEST = {
    "cave":      {"name": "Password Cave", "grid": 0x06829B, "dims": (50, 21),
                  "lut": 0x068055, "chr": 0x09CD80, "chr_first": 0x20, "pal": 0x0159E0},
    "iga-field": {"name": "Iga Village",   "grid": 0x0DD4B2, "dims": (26, 27),
                  "lut": 0x030612, "chr": 0x099F00, "chr_first": 0x20, "pal": 0x0159A0},
}


def _map_palette(rom, pal_off):
    return [_bgr555(rom[pal_off + c * 2], rom[pal_off + c * 2 + 1]) for c in range(0x10)]


def render_map_struct(rom, key):
    """Generalized terrain render for any MANIFEST area (cave-equivalent for 'cave').
    Returns (w, h, rgba) at 2x scale (HALF_PX per half-tile)."""
    m = MANIFEST[key]; w, h = m["dims"]
    grid = decode_grid(rom, m["grid"], w, h)
    pal = _map_palette(rom, m["pal"])
    pw, ph = w * 16, h * 16
    rgba = bytearray(pw * ph * 4); cache = {}
    for r in range(h):
        for c in range(w):
            tid = grid[r][c]
            for q in range(4):
                chr_byte = rom[m["lut"] + tid * 4 + q]
                slot = _bg_word(chr_byte) & 0x3FF
                tile = cache.get(slot)
                if tile is None:
                    ro = m["chr"] + (slot - m["chr_first"]) * 32
                    tile = (_decode_4bpp(rom, ro) if 0 <= ro and ro + 32 <= len(rom)
                            else [[0] * 8 for _ in range(8)])
                    cache[slot] = tile
                ox, oy = c * 16 + (q & 1) * 8, r * 16 + ((q >> 1) & 1) * 8
                for yy in range(8):
                    for xx in range(8):
                        cr, cg, cb = pal[tile[yy][xx]]
                        i = ((oy + yy) * pw + (ox + xx)) * 4
                        rgba[i] = cr; rgba[i + 1] = cg; rgba[i + 2] = cb; rgba[i + 3] = 255
    return _upscale2x(pw, ph, bytes(rgba))


def map_list():
    """Selector metadata for the UI: which maps the manifest can render from this ROM."""
    return [{"key": k, "name": v["name"], "w": v["dims"][0], "h": v["dims"][1]}
            for k, v in MANIFEST.items()]


# ---------- walkability ----------
def _attr(rom, grid, hx, hy):
    tid = grid[hy // 2][hx // 2]
    return rom[LUT + tid * 4 + (hy & 1) * 2 + (hx & 1)]


def _object_cells(rom):
    """Parse the cave object/event resource ($0D:8205) straight from ROM. Header:
    [n5, nText, nAvail, nSel, ...]. Availability rows (x,y,id,flags, 4B) start at
    OBJ_RES+0x4E (after header + the nText text section); selector rows (x,y,action,
    3B) follow immediately. (Pinned by content-match to the live WRAM anchors;
    OBJ_AVAIL_OFF should be derived from nText/n5 for other maps.)"""
    OBJ_AVAIL_OFF = 0x4E
    n_avail = rom[OBJ_RES + 2]; n_sel = rom[OBJ_RES + 3]
    avail = OBJ_RES + OBJ_AVAIL_OFF
    sel = avail + n_avail * 4
    cells = set()
    for i in range(n_avail):
        cells.add((rom[avail + i * 4], rom[avail + i * 4 + 1]))
    for i in range(n_sel):
        cells.add((rom[sel + i * 3], rom[sel + i * 3 + 1]))
    return {c for c in cells if c[0] < 100 and c[1] < 42}


def cave_walkable_cells(rom):
    w, h = CAVE_DIMS
    grid = decode_grid(rom, CAVE_GRID, w, h)
    objs = _object_cells(rom)
    base = set()
    for hy in range(h * 2):
        for hx in range(w * 2):
            if _attr(rom, grid, hx, hy) >= WALK_THRESH and (hx, hy) not in objs:
                base.add((hx, hy))
    # reachability flood-fill from a known walkable seed (player spawn area ~ (96,3))
    seed = None
    for s in [(96, 3), (95, 3), (96, 4)]:
        if s in base:
            seed = s; break
    if seed is None:
        return base
    seen = {seed}; stack = [seed]
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (x + dx, y + dy)
            if n in base and n not in seen:
                seen.add(n); stack.append(n)
    return seen


def walkable_overlay(rom):
    pw, ph, rgba = render_cave_structural(rom)
    rgba = bytearray(rgba)
    walk = cave_walkable_cells(rom)
    w, h = CAVE_DIMS
    for hy in range(h * 2):
        for hx in range(w * 2):
            if (hx, hy) not in walk:           # tint blocked half-tiles red (2x scale)
                for yy in range(HALF_PX):
                    for xx in range(HALF_PX):
                        i = ((hy * HALF_PX + yy) * pw + (hx * HALF_PX + xx)) * 4
                        rgba[i] = min(255, rgba[i] // 2 + 110)
                        rgba[i + 1] //= 2; rgba[i + 2] //= 2
    return pw, ph, bytes(rgba)


# ---------- A* routing + overlay ----------
import heapq  # noqa: E402

SPAWN = (96, 3)  # player spawn (first walked half-tile); cold-ROM TODO: derive per map
ROUTE_COLORS = [(255, 60, 60), (255, 150, 40), (60, 220, 220), (220, 220, 50), (230, 60, 230),
                (120, 200, 120), (140, 160, 255)]

# Corner-slip routing is GATED OFF: the diagonal slip is direction/sub-tile-dependent (a
# slip's validity depends on how you ENTER the cell, not just its neighbors), so a static
# per-cell A* edge over-generates — it created false connectivity (routes weaving through /
# into walls; a user-flagged segment had the same 8-neighbour config as a replay-confirmed
# valid slip). Until the directional mechanic is reverse-engineered from the field-movement
# routine, route with plain 4-connectivity (always valid). Flip to True only with a
# direction-aware A* (state = cell + entry direction). The slip CHR/predicate/dual-count
# code is kept for that re-enable.
ENABLE_CORNER_SLIP = False


def _wall_open_cells(rom):
    """Half-tiles whose TERRAIN is passable (attr >= WALK_THRESH), IGNORING objects and
    reachability. Field objects (braziers/shrines/guards) sit on passable floor, so they
    ARE in this set — i.e. a cell blocked only by an object is still 'wall-open'. Used by
    the corner-slip predicate, which fires on WALLS only, not on object/sprite collision."""
    w, h = CAVE_DIMS
    grid = decode_grid(rom, CAVE_GRID, w, h)
    return {(hx, hy) for hy in range(h * 2) for hx in range(w * 2)
            if _attr(rom, grid, hx, hy) >= WALK_THRESH}


def _diag_slip_neighbors(walk, wall_open, x, y):
    """Diagonal corner-slip edges from (x,y). WALLS-ONLY (user-confirmed + replay-validated):
    the slip fires only when the pressed cardinal is blocked by a WALL (terrain), NOT by an
    object/sprite — pressing into a brazier/guard does not slip. Conditions: pressed cardinal
    is wall-blocked (not in wall_open), the perpendicular cardinal is wall-open, the diagonal
    target is a walkable node (in walk), AND that pressed cardinal admits EXACTLY ONE flanking
    diagonal. Both-flanks-eligible is ambiguous — the engine refuses it (replay: (88,11) Down
    did not move) and its tiebreak isn't RE'd yet, so emit nothing. Returns [(diag, press)]."""
    out = []
    for dy in (-1, 1):                                   # press VERTICAL (0,dy), slip sideways
        if (x, y + dy) in wall_open:                     # not wall-blocked -> no slip
            continue
        flanks = [dx for dx in (-1, 1)
                  if (x + dx, y) in wall_open and (x + dx, y + dy) in walk]
        if len(flanks) == 1:
            out.append(((x + flanks[0], y + dy), (0, dy)))
    for dx in (-1, 1):                                   # press HORIZONTAL (dx,0), slip vertically
        if (x + dx, y) in wall_open:
            continue
        flanks = [dy for dy in (-1, 1)
                  if (x, y + dy) in wall_open and (x + dx, y + dy) in walk]
        if len(flanks) == 1:
            out.append(((x + dx, y + flanks[0]), (dx, 0)))
    return out


def _astar(walk, start, goal, diagonal=False, wall_open=None):
    """4-connectivity A* over walkable half-tiles. With diagonal=True (and wall_open given),
    also use walls-only corner-slip diagonal edges (same per-step cost as a cardinal step),
    making a slip strictly cheaper than the two cardinal steps it replaces."""
    if start not in walk or goal not in walk:
        return None
    if diagonal and wall_open is None:
        wall_open = walk                                 # fallback (treats objects as walls)
    def hh(a):
        dx, dy = abs(a[0] - goal[0]), abs(a[1] - goal[1])
        return max(dx, dy) if diagonal else dx + dy     # Chebyshev (diag) vs Manhattan
    openh = [(hh(start), 0, start)]
    came = {start: None}; g = {start: 0}
    while openh:
        _, gc, cur = heapq.heappop(openh)
        if cur == goal:
            path = []
            while cur is not None:
                path.append(cur); cur = came[cur]
            return path[::-1]
        if gc > g.get(cur, 1e9):
            continue
        nbrs = [(cur[0] + dx, cur[1] + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
        if diagonal:
            nbrs += [d for d, _press in _diag_slip_neighbors(walk, wall_open, cur[0], cur[1])]
        for n in nbrs:
            if n in walk and gc + 1 < g.get(n, 1e9):
                g[n] = gc + 1; came[n] = cur
                heapq.heappush(openh, (gc + 1 + hh(n), gc + 1, n))
    return None


def interactable_objects(rom):
    """INTERACTABLE objects = availability rows (x,y,id,flags) — searchable TREASURE
    CHESTS. In the selector table they carry action 0x2C (vs the default 0x2B of the
    blocking-but-unsearchable decorations). Searching one runs the global chest-open
    dialog (chest_dialog): "<name> opened the treasure chest." then "<item> is inside."

    The avail-row `id` is a DIRECT index into the item table (see item_name) = the item
    in the chest. Cave: (7,2) id 0x6F -> "Password" chest, (26,2)+(94,35) id 0x5F ->
    "Medicine" chests. CODE-CONFIRMED (VM handler $7E:968F): the id is pushed straight to
    the inventory-add routine $7E:4204 with NO RNG anywhere in the handler -> chest
    contents are DETERMINISTIC (item == object.id), not random. `flags` is the per-chest
    opened/seen bit: it indexes the packed event-flag region $F089 as byte=$F089+(flags>>3),
    bit=1<<(flags&7); set on grant, checked on map load (sets id=0xFF -> "nothing here").
    NOTE: the per-map message "This way to Password" is NOT a chest's text — it belongs
    to the floor TRIGGER tile (movement_triggers), a one-time hint cutscene that points
    toward this chest. These are deliberately kept separate.
    Returns [(x, y, id, flags, label, item), ...]."""
    n_avail = rom[OBJ_RES + 2]
    avail = OBJ_RES + 0x4E
    out = []
    for i in range(n_avail):
        x, y, oid, flags = rom[avail + i * 4: avail + i * 4 + 4]
        if x < 100 and y < 42:
            item = item_name(rom, oid)
            label = f"chest: {item}" if item else f"obj 0x{oid:02X}"
            out.append((x, y, oid, flags, label, item))
    return out


def _ascii_run_at(rom, anchor):
    """Expand a found byte offset to its full printable-ASCII run."""
    lo = anchor
    while lo > 0 and 0x20 <= rom[lo - 1] < 0x7F:
        lo -= 1
    hi = anchor
    while hi < len(rom) and 0x20 <= rom[hi] < 0x7F:
        hi += 1
    return rom[lo:hi].decode("latin1")


def chest_dialog(rom, item):
    """Build a chest's search dialog from the GLOBAL ROM templates (sourced from data,
    not hardcoded English): "%s opened the treasure chest." (%s = player name) then
    "%s is inside." (%s = item name). Returns (open_line, reveal_line) with the player
    name shown as a "(hero)" placeholder (cold ROM has no save-file name) and the item
    substituted. Avoids angle brackets so the string is safe in HTML. Falls back to None
    templates if the ROM strings aren't present."""
    op = rom.find(b"opened the")
    ins = rom.find(b"is inside.")
    open_line = None
    if op != -1:
        run = _ascii_run_at(rom, op)              # "%s opened the" (treasure chest. may be a
        nxt = rom.find(b"treasure chest", op)     # separate run after a control byte)
        chest = _ascii_run_at(rom, nxt) if nxt != -1 and nxt - op < 0x20 else ""
        tmpl = (run + " " + chest).strip() if chest else run
        open_line = tmpl.replace("%s", "(hero)", 1)
    reveal_line = None
    if ins != -1:
        reveal_line = _ascii_run_at(rom, ins).replace("%s", item or "?", 1)
    return open_line, reveal_line


def movement_triggers(rom):
    """One-time floor-tile cutscenes: stepping on the trigger tile fires a dialog once
    (a 'seen' save flag then suppresses it). These are NOT A* targets — they are passed
    along the route.

    Parsed straight from the resource's nText records (0x4A bytes each, located after
    header(4) + n5*5). Each record begins [trigger_x][trigger_y][b2] then a null-terminated
    message, then the event VM bytecode. Cave record 0 = (6,17) "This way to Password" — a
    tile in the column-6 corridor leading into the Password-chest room. (Coordinate read
    directly from the record header; the per-step trigger check + the b2 byte's meaning are
    being confirmed against the field-event interpreter.) Returns
    [{cell_x, cell_y, px, py, text, b2, kind, note}, ...]."""
    n5, n_text = rom[OBJ_RES], rom[OBJ_RES + 1]
    base = OBJ_RES + 4 + n5 * 5
    REC = 0x4A
    out = []
    for i in range(n_text):
        rec = base + i * REC
        x, y, b2 = rom[rec], rom[rec + 1], rom[rec + 2]
        text = rom[rec + 3: rec + REC].split(b"\x00")[0]
        text = text.decode("latin1") if all(32 <= b < 127 for b in text) else ""
        on_map = x < 100 and y < 42
        out.append({
            "cell_x": x if on_map else None, "cell_y": y if on_map else None,
            "px": x * HALF_PX + HALF_PX // 2 if on_map else None,
            "py": y * HALF_PX + HALF_PX // 2 if on_map else None,
            "text": text, "b2": b2, "kind": "floor-trigger",
            "note": "one-time hint cutscene (fires once, then a 'seen' flag suppresses it); "
                    "stepped on along the way — NOT the search target",
        })
    return out


def interactable_info(rom):
    """Per searchable CHEST: position, id, item, flags (opened/save bit), the two-step
    search dialog (from chest_dialog), and the A* route length from spawn. These are the
    POI targets of the routes. JSON-serializable for the atlas UI click panel. The floor
    trigger tiles are returned separately by movement_triggers()."""
    walk = cave_walkable_cells(rom)
    wall_open = _wall_open_cells(rom)
    spawn = SPAWN if SPAWN in walk else next(iter(walk))
    out = []
    for (x, y, oid, flags, label, item) in interactable_objects(rom):
        goal = None
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            if (x + dx, y + dy) in walk:
                goal = (x + dx, y + dy); break
        path = _astar(walk, spawn, goal, diagonal=ENABLE_CORNER_SLIP, wall_open=wall_open) if goal else None
        path_plain = _astar(walk, spawn, goal, diagonal=False) if goal else None  # 4-conn baseline
        open_line, reveal_line = chest_dialog(rom, item)
        dialog = [s for s in (open_line, reveal_line) if s]
        search = "Search → " + " → ".join(f"“{s}”" for s in dialog) if dialog else \
                 f"Search → opens a chest holding {item}"
        out.append({
            "cell_x": x, "cell_y": y, "px": x * HALF_PX + HALF_PX // 2, "py": y * HALF_PX + HALF_PX // 2,
            "id": oid, "type": label, "item": item, "flags": flags, "action": 0x2C,
            "kind": "chest", "dialog": dialog, "search": search,
            "route_steps": (len(path) - 1 if path else None),                 # with corner-slip
            "route_steps_plain": (len(path_plain) - 1 if path_plain else None),  # 4-connectivity
        })
    return out


def _obj_palette(rom):
    """Object BG palette (palette 5), 16 BGR555 colours from cold ROM. Index 0 = transparent."""
    return [_bgr555(rom[OBJ_PAL_ROM + c * 2], rom[OBJ_PAL_ROM + c * 2 + 1]) for c in range(16)]


def _action_sprite_char(action):
    """Selector action code -> object BG char (native bank-$04 converter)."""
    d = action - 0x29
    return OBJ_ACTION_BASE + ((d >> 3) * 0x20) + ((d & 7) * 2)


def _obj_block_base(char):
    """ROM offset of an object's CHR block (4 consecutive tiles TL,TR,BL,BR; 0x80 bytes)."""
    return OBJ_CHR_BLOCK + ((char - OBJ_CHR_REF) // 2) * 0x80


def _obj_sprite_rgba(rom, char, pal):
    """16x16 RGBA bytes for the object whose BG char is `char`. The object's four 8x8
    quarters are stored consecutively in ROM as TL,TR,BL,BR. Palette index 0 -> alpha 0
    (transparent, so the map shows through). Returns a flat list."""
    out = bytearray(16 * 16 * 4)
    b = _obj_block_base(char)
    for qi in range(4):
        t = _decode_4bpp(rom, b + qi * 0x20)
        ox, oy = (qi & 1) * 8, (qi >> 1) * 8
        for y in range(8):
            for x in range(8):
                idx = t[y][x]
                o = ((oy + y) * 16 + (ox + x)) * 4
                if idx:
                    r, g, bl = pal[idx]
                    out[o], out[o + 1], out[o + 2], out[o + 3] = r, g, bl, 255
    return list(out)


def _downscale_rgba_2x(rgba, w, h):
    """Average-downscale a w x h RGBA flat list to (w//2) x (h//2). Transparent (alpha 0)
    pixels are ignored in the average; output alpha is 0 only if all 4 were transparent."""
    ow, oh = w // 2, h // 2
    out = bytearray(ow * oh * 4)
    for oy in range(oh):
        for ox in range(ow):
            r = g = b = a = n = 0
            for dy in range(2):
                for dx in range(2):
                    i = (((oy * 2 + dy) * w) + (ox * 2 + dx)) * 4
                    if rgba[i + 3]:
                        r += rgba[i]; g += rgba[i + 1]; b += rgba[i + 2]; n += 1
            o = (oy * ow + ox) * 4
            if n:
                out[o], out[o + 1], out[o + 2], out[o + 3] = r // n, g // n, b // n, 255
    return list(out)


# Experiment: draw objects at this fraction of their native 16x16 (1.0 = full tile).
OBJ_DRAW_SCALE = 1.0


def object_sprites(rom):
    """The real in-game OBJECT layer (topmost): every visible field object the player
    sees — searchable shrines (action 0x2C) and blocking braziers (action 0x2B). These
    are 16x16 BG-layer tiles (NOT OAM sprites; only the player is a sprite). Parses the
    selector table (x,y,action); each cell -> a 16x16 object drawn at its half-tile cell
    (HALF_PX per half-tile). Searchable cells are merged with their item /
    dialog / route info so a click shows what's inside. Returns a JSON-serializable dict:
    {w, h, sprites:{char:rgba}, objects:[...]}. The entrance stairs are NOT here: those
    are baked into the BG terrain, not an object tile."""
    B = OBJ_RES
    n_avail, n_sel = rom[B + 2], rom[B + 3]
    sel = B + 0x4E + n_avail * 4
    pal = _obj_palette(rom)
    sprites = {}
    info = {(o["cell_x"], o["cell_y"]): o for o in interactable_info(rom)}
    # Object selector coords are HALF-tile (8 px/unit); the map renders 8 px per half-tile.
    # Draw each 16x16 object at its half-tile cell (px = x*8, y*8) — true position, nothing
    # merged or dropped. (Player/camera coords are a separate full-tile unit, 16 px/unit.)
    objects = []
    for i in range(n_sel):
        x, y, act = rom[sel + i * 3: sel + i * 3 + 3]
        if x >= 100 or y >= 42:
            continue
        kind = "searchable" if act == 0x2C else "blocker"
        tl = _action_sprite_char(act)
        if tl not in sprites:
            spr = _obj_sprite_rgba(rom, tl, pal)
            if OBJ_DRAW_SCALE == 0.5:
                spr = _downscale_rgba_2x(spr, 16, 16)
            sprites[tl] = spr
        sz = int(round(16 * OBJ_DRAW_SCALE))
        off = (16 - sz) // 2
        d = {"cell_x": x, "cell_y": y, "px": x * HALF_PX + off, "py": y * HALF_PX + off,
             "action": act, "char": tl, "kind": kind}
        if kind == "searchable" and (x, y) in info:
            ci = info[(x, y)]
            d.update({"item": ci.get("item"), "dialog": ci.get("dialog"),
                      "id": ci.get("id"), "flags": ci.get("flags"),
                      "route_steps": ci.get("route_steps"),
                      "route_steps_plain": ci.get("route_steps_plain"),
                      "search": ci.get("search")})
        objects.append(d)
    sz = int(round(16 * OBJ_DRAW_SCALE))
    return {"w": sz, "h": sz, "sprites": {str(k): v for k, v in sprites.items()}, "objects": objects}


def _player_palette(rom):
    return [_bgr555(rom[PLAYER_PAL_ROM + c * 2], rom[PLAYER_PAL_ROM + c * 2 + 1]) for c in range(16)]


def _hue_shift(r, g, b, deg):
    """Rotate an RGB colour's hue by `deg` degrees (keeps saturation/value). No trig."""
    mx = max(r, g, b); mn = min(r, g, b); d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - b) / d) + 360) % 360
    elif mx == g:
        h = 60 * ((b - r) / d) + 120
    else:
        h = 60 * ((r - g) / d) + 240
    s = 0.0 if mx == 0 else d / mx
    v = mx / 255.0
    h = (h + deg) % 360
    c = v * s; hp = h / 60.0; x = c * (1 - abs(hp % 2 - 1)); m = v - c
    if hp < 1:   rp, gp, bp = c, x, 0
    elif hp < 2: rp, gp, bp = x, c, 0
    elif hp < 3: rp, gp, bp = 0, c, x
    elif hp < 4: rp, gp, bp = 0, x, c
    elif hp < 5: rp, gp, bp = x, 0, c
    else:        rp, gp, bp = c, 0, x
    return (round((rp + m) * 255), round((gp + m) * 255), round((bp + m) * 255))


def _player_rgba(rom, face, hue, hflip):
    """16x16 RGBA for the player facing `face`, palette hue-shifted by `hue` degrees.
    Name-table layout TL,TL+1,TL+0x10,TL+0x11. OBJ index 0 -> transparent."""
    pal = [(_hue_shift(*c, hue) if i else c) for i, c in enumerate(_player_palette(rom))]
    tl = PLAYER_FACE[face]
    out = bytearray(16 * 16 * 4)
    for ch, ox, oy in ((tl, 0, 0), (tl + 1, 8, 0), (tl + 0x10, 0, 8), (tl + 0x11, 8, 8)):
        t = _decode_4bpp(rom, PLAYER_CHR + (ch - PLAYER_CHR_REF) * 0x20)
        for y in range(8):
            for x in range(8):
                idx = t[y][x]
                if not idx:
                    continue
                r, g, b = pal[idx]
                o = ((oy + y) * 16 + (ox + x)) * 4
                out[o], out[o + 1], out[o + 2], out[o + 3] = r, g, b, 255
    if hflip:
        for y in range(16):
            for x in range(8):
                a = (y * 16 + x) * 4; bb = (y * 16 + 15 - x) * 4
                out[a:a + 4], out[bb:bb + 4] = bytes(out[bb:bb + 4]), bytes(out[a:a + 4])
    return list(out)


def _dir_face(dx, dy):
    """Movement delta -> (face, hflip). Side sprite faces left as stored; h-flip for right."""
    if abs(dx) >= abs(dy):
        return ("side", dx > 0) if dx else ("down", False)
    return ("down" if dy > 0 else "up"), False


def route_player_sprites(rom, spacing=12):
    """A* routes drawn as the player character instead of coloured dots. For each POI
    route: sample points along the path roughly every `spacing` px, each with the facing
    direction (and h-flip), and hue-shift the player palette per route so routes read as
    distinct colours (replacing the custom route-colour dots). The spawn/start stays a
    coloured square (warp marker), drawn by the caller. Returns a JSON-serializable dict:
    {w,h, routes:[{idx,hue,anchor,steps,points:[{px,py,face,hflip}]}], sprites:{key:rgba}}.
    `key` = "<hue>:<face>:<hflip>"; JS draws sprites[key] at each point."""
    walk = cave_walkable_cells(rom)
    wall_open = _wall_open_cells(rom)
    spawn = SPAWN if SPAWN in walk else next(iter(walk))
    goals = _poi_goals(rom, walk)
    n = max(1, len(goals))
    routes = []
    sprites = {}
    for i, (goal, anchor) in enumerate(goals):
        path = _astar(walk, spawn, goal, diagonal=ENABLE_CORNER_SLIP, wall_open=wall_open)
        if not path:
            continue
        hue = round(i * 360.0 / n)
        pts = []
        acc = spacing  # emit on the first cell
        for j in range(len(path)):
            x, y = path[j]
            if j + 1 < len(path):
                nx, ny = path[j + 1]; dx, dy = nx - x, ny - y
            elif j > 0:
                ax, ay = path[j - 1]; dx, dy = x - ax, y - ay
            else:
                dx, dy = 0, 1
            if dx and dy:   # diagonal slip: the player faces the PRESSED (wall-blocked) cardinal
                if (x, y + dy) not in wall_open:
                    face, hflip = ("down" if dy > 0 else "up"), False
                elif (x + dx, y) not in wall_open:
                    face, hflip = "side", (dx > 0)
                else:
                    face, hflip = _dir_face(dx, dy)
            else:
                face, hflip = _dir_face(dx, dy)
            acc += HALF_PX  # each path step = one half-tile
            if acc >= spacing:
                acc = 0
                key = f"{hue}:{face}:{int(hflip)}"
                if key not in sprites:
                    sprites[key] = _player_rgba(rom, face, hue, hflip)
                pts.append({"px": x * HALF_PX, "py": y * HALF_PX, "key": key})
        routes.append({"idx": i, "hue": hue,
                       "anchor": anchor[2] if len(anchor) > 2 else None,
                       "steps": len(path) - 1, "points": pts})
    return {"w": 16, "h": 16, "spawn_px": [spawn[0] * HALF_PX, spawn[1] * HALF_PX],
            "routes": routes, "sprites": sprites}


def _poi_goals(rom, walk):
    """Route targets = INTERACTABLE objects only (not all 16 blocking cells); each ->
    the nearest reachable walkable half-tile beside it (you search from adjacent)."""
    goals = []
    for (ax, ay, _oid, _flags, label, _item) in interactable_objects(rom):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            c = (ax + dx, ay + dy)
            if c in walk:
                goals.append((c, (ax, ay, label))); break
    return goals


def _plot(rgba, pw, ph, x, y, col, rad=1):
    for yy in range(-rad, rad + 1):
        for xx in range(-rad, rad + 1):
            px, py = x + xx, y + yy
            if 0 <= px < pw and 0 <= py < ph:
                i = (py * pw + px) * 4
                rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3] = col[0], col[1], col[2], 255


def route_overlay(rom):
    pw, ph, rgba = render_cave_structural(rom)
    rgba = bytearray(rgba)
    walk = cave_walkable_cells(rom)
    spawn = SPAWN if SPAWN in walk else next(iter(walk))
    # half-tile -> pixel: HALF_PX per half-tile, centered
    def px(c):
        return c[0] * HALF_PX + HALF_PX // 2, c[1] * HALF_PX + HALF_PX // 2
    routes = []
    wall_open = _wall_open_cells(rom)
    for i, (goal, anchor) in enumerate(_poi_goals(rom, walk)):
        path = _astar(walk, spawn, goal, diagonal=ENABLE_CORNER_SLIP, wall_open=wall_open)
        if not path:
            continue
        col = ROUTE_COLORS[i % len(ROUTE_COLORS)]
        for c in path:
            x, y = px(c); _plot(rgba, pw, ph, x, y, col, 2)
        routes.append((anchor, len(path)))
    # spawn marker only; chest/object endpoints are drawn by the (topmost) Objects layer
    # in the UI, so the z-order is map -> A* route -> objects.
    sx, sy = px(spawn); _plot(rgba, pw, ph, sx, sy, (60, 230, 60), 3)
    return pw, ph, bytes(rgba)
