#!/usr/bin/env python3
"""
catpet - pixel-katten desktop pet voor Hyprland (Wayland) via gtk-layer-shell.

Installatie (CachyOS / Arch):
    sudo pacman -S gtk3 gtk-layer-shell python-gobject python-cairo
    (voor geluid: pipewire/pipewire-pulse of alsa-utils; pw-play/paplay/aplay worden automatisch gebruikt)

Starten:
    python catpet.py
    python catpet.py --cats 3                      # drie katten, elk met eigen kleur/vlekken
    python catpet.py --cats 2 --coats tuxedo,calico --floor 40 --calm 1.5

Klik op een kat: spinnen (met purr-geluid). Stoppen: pkill -f catpet.py
Eigen sprites: python catpet.py --dump-sprites sprites  (bewerk de PNG's, start opnieuw)
Eigen geluid: leg purr.wav / mew.wav in een map 'sounds' naast dit script.
"""
import argparse
import json
import math
import random
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Pixel-art engine + kat-rig (puur Python, geen GTK nodig)
# --------------------------------------------------------------------------- #

W, H = 44, 34
OY = 4          # ontwerp-y -> canvas-y (ruimte voor oren / hartjes bovenin)
GROUND = 28     # onderste pootrij (ontwerp-coordinaten)


# ---------- vachten (kleuren + patronen) ----------------------------------- #

def _rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


class Coat:
    def __init__(self, name, O, D, L, E, pattern="tabby", face=None, S=None, spots=(),
                 P="#f58a96", K="#211a24", blush=True):
        self.name, self.pattern, self.face = name, pattern, face or pattern
        self.spots, self.blush = list(spots), blush
        o = _rgb(O)
        self.pal = {"K": _rgb(K), "O": o, "D": _rgb(D), "L": _rgb(L), "S": _rgb(S or D),
                    "P": _rgb(P), "E": _rgb(E), "W": (1, 1, 1),
                    "F": tuple(v * 0.80 for v in o)}


SPOTS = [(0.15, 1.8, 2.0, "D"), (0.42, -0.9, 1.7, "D"), (0.66, 1.7, 1.9, "D"),
         (0.88, 0.1, 1.5, "D"), (0.30, -1.8, 1.1, "D")]
PATCH = [(0.18, 1.0, 3.3, "D"), (0.55, 0.4, 3.5, "S"), (0.86, 1.6, 2.7, "D")]

COATS = [
    Coat("ginger",    "#ee9438", "#bd6121", "#fce5c2", "#6ec85a", "tabby"),
    Coat("grey",      "#979cac", "#61667a", "#e4e8f0", "#f0b428", "tabby"),
    Coat("tuxedo",    "#3c3a4a", "#2c2a38", "#f6f6fa", "#cfe03c", "tux", blush=False),
    Coat("calico",    "#faf4ec", "#e88c34", "#ffffff", "#5aa0e6", "calico", S="#3e343c", spots=PATCH),
    Coat("cream",     "#f5d7a0", "#96603c", "#fff0d6", "#5aa0e6", "spots", face="spot", spots=SPOTS),
    Coat("blue",      "#70809a", "#60708a", "#aab8c8", "#e6c040", "plain"),
    Coat("chocolate", "#96644a", "#643e28", "#e6c8aa", "#f0b428", "tabby"),
    Coat("snow",      "#fafafc", "#c8cede", "#ffffff", "#5aa0e6", "plain"),
]
COAT = COATS[0]


def set_coat(c):
    global COAT
    COAT = c


def coat_variant(base, i):
    """Extra katten boven de 8: zelfde patroon, licht andere tint."""
    rnd = random.Random(i * 7919)
    k = 0.85 + rnd.random() * 0.3
    c = Coat.__new__(Coat)
    c.__dict__.update(base.__dict__)
    c.name = f"{base.name}{i}"
    c.pal = {key: (tuple(min(1, v * k) for v in val) if key in "ODLSF" else val)
             for key, val in base.pal.items()}
    return c


# ---------- canvas --------------------------------------------------------- #

class Canvas:
    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.p = [[None] * w for _ in range(h)]

    def px(self, x, y, c):
        x, y = int(round(x)), int(round(y)) + OY
        if 0 <= x < self.w and 0 <= y < self.h:
            self.p[y][x] = c

    def get(self, x, y):
        x, y = int(round(x)), int(round(y)) + OY
        if 0 <= x < self.w and 0 <= y < self.h:
            return self.p[y][x]
        return None

    def rect(self, x, y, w, h, c):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.px(xx, yy, c)

    def outline(self, c="K"):
        src = [row[:] for row in self.p]
        for y in range(self.h):
            for x in range(self.w):
                if src[y][x] is None:
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < self.w and 0 <= ny < self.h and src[ny][nx] is not None:
                            self.p[y][x] = c
                            break

    def halo(self, cells, c="K"):
        for x, y in cells:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if self.get(x + dx, y + dy) is None:
                    self.px(x + dx, y + dy, c)

    def ascii(self):
        return "\n".join("".join(c or "." for c in row) for row in self.p)


# ---------- vormen --------------------------------------------------------- #

def chain_mask(pts, r0, r1=None):
    """Capsule langs een polylijn -> {(x,y): (t, v)}; t = 0..1 langs de lijn, v = afstand naar de rugkant."""
    r1 = r0 if r1 is None else r1
    segs, total = [], 0.0
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        segs.append((a, b, d, total))
        total += d
    total = total or 1.0
    mask = {}
    for a, b, d, start in segs:
        n = max(1, int(d * 3))
        ux, uy = ((b[0] - a[0]) / d, (b[1] - a[1]) / d) if d else (1, 0)
        nx, ny = uy, -ux
        for i in range(n + 1):
            f = i / n
            x = a[0] + (b[0] - a[0]) * f
            y = a[1] + (b[1] - a[1]) * f
            t = (start + d * f) / total
            r = r0 + (r1 - r0) * t
            for yy in range(int(y - r) - 1, int(y + r) + 2):
                for xx in range(int(x - r) - 1, int(x + r) + 2):
                    if (xx + .5 - x) ** 2 + (yy + .5 - y) ** 2 <= r * r and (xx, yy) not in mask:
                        mask[(xx, yy)] = (t, (xx + .5 - x) * nx + (yy + .5 - y) * ny)
    return mask


def ellipse_mask(cx, cy, rx, ry):
    m = {}
    for yy in range(int(cy - ry) - 1, int(cy + ry) + 2):
        for xx in range(int(cx - rx) - 1, int(cx + rx) + 2):
            if ((xx + .5 - cx) / rx) ** 2 + ((yy + .5 - cy) / ry) ** 2 <= 1:
                m[(xx, yy)] = ((xx + .5 - (cx - rx)) / (2 * rx), cy - (yy + .5))
    return m


def bezier(p0, c, p1, n=8):
    out = []
    for i in range(n + 1):
        u = i / n
        out.append(((1 - u) ** 2 * p0[0] + 2 * (1 - u) * u * c[0] + u * u * p1[0],
                    (1 - u) ** 2 * p0[1] + 2 * (1 - u) * u * c[1] + u * u * p1[1]))
    return out


def paint(c, mask, col):
    for (x, y) in mask:
        c.px(x, y, col)


def paint_fur(c, mask, belly=True, stripes=True, bands=6):
    """Vacht volgens het patroon van de huidige kat (tabby / vlekken / vlakken / tuxedo)."""
    coat, keys = COAT, set(mask)
    for (x, y), (t, v) in mask.items():
        col = "O"
        if coat.pattern in ("spots", "calico"):
            for t0, v0, r, sc in coat.spots:
                if ((t - t0) * 11) ** 2 + (v - v0) ** 2 <= r * r:
                    col = sc
                    break
        top = (x, y - 1) not in keys or (x, y - 2) not in keys
        if coat.pattern == "tabby" and stripes and top and int(t * bands) % 2 == 1:
            col = "D"
        if coat.pattern == "tux" and t > 0.84:
            col = "L"
        rows = 3 if coat.pattern == "tux" else 2
        if belly and any((x, y + k) not in keys for k in range(1, rows + 1)):
            col = "L"
        c.px(x, y, col)


# ---------- het gezicht ---------------------------------------------------- #

HEAD_ROWS = [(2, 12), (1, 13), (0, 14), (0, 14), (0, 14), (0, 14),
             (0, 14), (0, 14), (0, 14), (1, 13), (2, 12), (4, 10)]


def eye(c, x, y, kind):
    """Oog van 3x4 pixels (x,y = linkerbovenhoek)."""
    if kind == "open":
        for dx in range(3):
            c.px(x + dx, y, "K")
        c.px(x, y + 1, "W"); c.px(x + 1, y + 1, "E"); c.px(x + 2, y + 1, "E")
        c.px(x, y + 2, "E"); c.px(x + 1, y + 2, "K"); c.px(x + 2, y + 2, "E")
        c.px(x + 1, y + 3, "E")
    elif kind == "half":
        for dx in range(3):
            c.px(x + dx, y + 2, "K")
        c.px(x, y + 3, "E"); c.px(x + 1, y + 3, "K"); c.px(x + 2, y + 3, "E")
    elif kind == "closed":
        for dx in range(3):
            c.px(x + dx, y + 3, "K")
    elif kind == "happy":
        c.px(x + 1, y + 2, "K"); c.px(x, y + 3, "K"); c.px(x + 2, y + 3, "K")
    elif kind == "left":   # > (geeuw)
        c.px(x, y + 1, "K"); c.px(x + 1, y + 2, "K"); c.px(x, y + 3, "K")
    elif kind == "right":  # <
        c.px(x + 2, y + 1, "K"); c.px(x + 1, y + 2, "K"); c.px(x + 2, y + 3, "K")


def head(c, x, y, eyes="open", mouth="closed", tongue=False, ear=0):
    """Gezicht van voren, 15x12 pixels + oren. (x,y) = linkerbovenhoek van het gezicht."""
    coat = COAT
    x, y = int(round(x)), int(round(y))
    inside = set()
    for r, (x0, x1) in enumerate(HEAD_ROWS):
        for xx in range(x0, x1 + 1):
            inside.add((xx, r))
            c.px(x + xx, y + r, "O")
    # vlekken / patronen op het gezicht
    face = coat.face
    if face == "calico":
        for (xx, r) in inside:
            if r <= 4 and xx <= 5:
                c.px(x + xx, y + r, "S")
            elif r <= 4 and xx >= 9:
                c.px(x + xx, y + r, "D")
    elif face == "spot":
        for (xx, r) in inside:
            if (xx - 11) ** 2 + (r - 3) ** 2 <= 5.5:
                c.px(x + xx, y + r, "D")
    elif face == "tux":
        for (xx, r) in inside:
            if (6 <= xx <= 8 and r <= 6) or (r >= 6 and 2 <= xx <= 12) or (r >= 8):
                c.px(x + xx, y + r, "L")
    # oren
    wl, wr = [5, 4, 3, 2], [5, 4, 3, 2]
    if ear == 1:
        wr = [5, 3]
    lcol = "S" if face == "calico" else "O"
    rcol = "D" if face in ("calico", "spot") else "O"
    for k, w in enumerate(wl):
        for xx in range(1, 1 + w):
            c.px(x + xx, y - 1 - k, lcol)
        if k < 3:
            for xx in range(2, w):
                c.px(x + xx, y - 1 - k, "P")
    for k, w in enumerate(wr):
        for xx in range(14 - w, 14):
            c.px(x + xx, y - 1 - k, rcol)
        if k < 3 and w >= 3:
            for xx in range(14 - w + 1, 13):
                c.px(x + xx, y - 1 - k, "P")
    # tabby-tekening
    if face == "tabby":
        for dx, dy in ((7, 0), (7, 1), (7, 2), (5, 0), (5, 1), (9, 0), (9, 1),
                       (0, 5), (1, 5), (0, 7), (1, 7), (13, 5), (14, 5), (13, 7), (14, 7)):
            c.px(x + dx, y + dy, "D")
    # snuit
    for r, (a, b) in ((8, (5, 9)), (9, (4, 10)), (10, (4, 10)), (11, (5, 9))):
        for xx in range(a, b + 1):
            if (xx, r) in inside:
                c.px(x + xx, y + r, "L")
    if coat.blush:
        for dx in (1, 2, 12, 13):
            c.px(x + dx, y + 8, "P")
    # ogen
    if eyes in ("left", "right", "yawn"):
        eye(c, x + 3, y + 4, "left")
        eye(c, x + 9, y + 4, "right")
    else:
        eye(c, x + 3, y + 4, eyes)
        eye(c, x + 9, y + 4, eyes)
    # neus + mond
    for dx in (6, 7, 8):
        c.px(x + dx, y + 8, "P")
    if mouth == "open":
        for dx in (5, 6, 7, 8, 9):
            c.px(x + dx, y + 9, "K")
        c.px(x + 6, y + 10, "K"); c.px(x + 7, y + 10, "P"); c.px(x + 8, y + 10, "K")
    elif mouth == "yawn":
        for dx in (5, 6, 7, 8, 9):
            c.px(x + dx, y + 9, "K")
        for dx in (5, 6, 8, 9):
            c.px(x + dx, y + 10, "K")
        c.px(x + 7, y + 10, "P"); c.px(x + 6, y + 11, "K"); c.px(x + 7, y + 11, "K"); c.px(x + 8, y + 11, "K")
        c.px(x + 6, y + 10, "P"); c.px(x + 8, y + 10, "P")
    else:
        c.px(x + 7, y + 9, "K")
        c.px(x + 5, y + 9, "K"); c.px(x + 6, y + 10, "K"); c.px(x + 8, y + 10, "K"); c.px(x + 9, y + 9, "K")
    if tongue:
        c.px(x + 7, y + 11, "P"); c.px(x + 7, y + 12, "P")


# ---------- lichaamsdelen -------------------------------------------------- #

def neck(c, a, b):
    paint(c, chain_mask([a, b], 2.7), "O")


def tail(c, pts, sway=0.0, dy=0.0):
    n = max(1, len(pts) - 1)
    pts = [(x + sway * (i / n) * 2.2, y + dy) for i, (x, y) in enumerate(pts)]
    for (x, y), (t, _v) in chain_mask(pts, 1.35, 1.0).items():
        c.px(x, y, "F" if t > 0.82 else "O")


def leg(c, x, y0, dx, lift, col, ground=GROUND):
    fx, fy = x + dx, ground - lift - 0.3
    paint(c, chain_mask([(x, y0), (fx, fy)], 1.05), col)
    bx, row = int(round(fx)), ground - lift
    for xx in range(bx - 1, bx + 2):
        c.px(xx, row, "L")
    c.px(bx, row - 1, "L"); c.px(bx + 1, row - 1, "L")


Z = ["###", "..#", ".#.", "###"]
HEART = [".#.#.", "#####", ".###.", "..#.."]


def stamp(c, glyph, x, y, col, halo=True):
    cells = [(x + i, y + j) for j, row in enumerate(glyph) for i, ch in enumerate(row) if ch == "#"]
    if halo:
        c.halo(cells)
    for cx, cy in cells:
        c.px(cx, cy, col)


# ---------- poses ---------------------------------------------------------- #

LEG = [(3, 0), (1, 0), (-1, 0), (-3, 0), (-1, 2), (1, 2)]
BOB = [0, -1, 0, 0, -1, 0]
TAILS = {
    "mid": [(8.5, 19), (6, 18.5), (4, 17), (3, 14.5), (3.5, 12)],
    "up":  [(8.5, 19), (6.5, 17.5), (5.5, 15), (5.5, 12), (6.5, 9.5)],
    "low": [(8.5, 20.5), (6, 22), (4, 24), (3, 26)],
}


def pose_stand(phase=None, bob=0, tail_kind="mid", sway=0.0, eyes="open", ear=0, mouth="closed"):
    c = Canvas()
    walking = phase is not None
    a = LEG[phase] if walking else (0, 0)
    b = LEG[(phase + 3) % 6] if walking else (0, 0)
    if walking:
        bob = BOB[phase]
    tail(c, TAILS[tail_kind], sway, bob)
    y0 = 21 + bob
    leg(c, 13, y0, a[0], a[1], "F")
    leg(c, 19, y0, b[0], b[1], "F")
    paint_fur(c, chain_mask([(12, 20 + bob), (23, 20 + bob)], 3.4))
    leg(c, 10, y0, b[0], b[1], "O")
    leg(c, 22, y0, a[0], a[1], "O")
    neck(c, (22.5, 19 + bob), (27, 17 + bob))
    head(c, 22, 10 + bob, eyes, mouth, False, ear)
    c.outline()
    return c


def sit_base(c, tail_sway, head_xy, torso_top, paw=None, eyes="open", ear=0, mouth="closed",
             tongue=False, hdy=0):
    tail(c, [(8, 26), (5.5, 27), (3.5, 26), (3.2, 23.5), (4, 21)], tail_sway)
    if paw is not None:
        paint(c, chain_mask([(19, 20), (19, 27.2)], 1.05), "F")
    paint_fur(c, ellipse_mask(13, 23.3, 5.8, 4.9), belly=False, bands=5)
    c.rect(14, 27, 5, 2, "L")
    torso = chain_mask([(18, 22.5), torso_top], 3.4, 3.0)
    paint_fur(c, torso, belly=False, bands=5)
    for p in ellipse_mask(23.8, 19, 2.4, 4.8):
        if p in torso:
            c.px(p[0], p[1], "L")
    if paw is None:
        paint(c, chain_mask([(21.5, 19), (21.5, 27.2)], 1.05), "O")
        c.rect(20, 27, 4, 2, "L")
    hx, hy = head_xy
    neck(c, (torso_top[0], torso_top[1] + 1), (hx + 7, hy + 7))
    if paw is not None:
        px, py = paw
        paint(c, chain_mask([(21, 18), (23.5, 22.5), (px, py)], 1.35), "O")
    head(c, hx, hy + hdy, eyes, mouth, tongue, ear)
    if paw is not None:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                c.px(int(px) + dx, int(py) + dy, "L")
        c.px(int(px), int(py), "P")


def pose_sit(sway=0.0, eyes="open", ear=0, mouth="closed", hdy=0, hearts=None):
    c = Canvas()
    sit_base(c, sway, (15, 2), (20.5, 13.2), eyes=eyes, ear=ear, mouth=mouth, hdy=hdy)
    c.outline()
    for hx, hy in (hearts or []):
        stamp(c, HEART, hx, hy, "P")
    return c


def pose_lick(paw, head_xy, eyes="closed", tongue=False, sway=0.0, lean=0.0):
    c = Canvas()
    sit_base(c, sway, head_xy, (20.5 + lean, 13.4), paw=paw, eyes=eyes, tongue=tongue)
    c.outline()
    return c


def pose_sleep(breath=0, zs=()):
    c = Canvas()
    tail(c, [(8.5, 26), (5.5, 27), (3.5, 25.5), (3.5, 23)], 0.0)
    r = 3.2 + 0.5 * breath
    paint_fur(c, chain_mask([(10.5, 25 - 0.5 * breath), (24, 25 - 0.5 * breath)], r), bands=6)
    head(c, 21, 17, "closed")
    c.rect(23, 27, 3, 2, "L")
    c.rect(31, 27, 3, 2, "L")
    c.outline()
    for zx, zy in zs:
        stamp(c, Z, zx, zy, "W")
    return c


def pose_wall(hip, sh, arch, paw, head_xy, r=3.1, eyes="open", mouth="closed", sway=0.0):
    c = Canvas()
    hx, hy = hip
    tail(c, [(hx - 3, hy - 1), (hx - 6, hy - 3), (hx - 8, hy - 7), (hx - 8, hy - 11), (hx - 7, hy - 14)], sway)
    leg(c, int(hx) + 2, hy, 1, 0, "F")
    mx, my = (hip[0] + sh[0]) / 2, (hip[1] + sh[1]) / 2
    dx, dy = sh[0] - hip[0], sh[1] - hip[1]
    L = math.hypot(dx, dy)
    ctrl = (mx + dy / L * arch, my - dx / L * arch)
    body = chain_mask(bezier(hip, ctrl, sh, 10), r, r - 0.3)
    pxx, pyy = paw
    paint(c, chain_mask([(sh[0] - 1, sh[1] + 1), (pxx - 1.5, pyy + 2)], 1.05), "F")
    paint_fur(c, body, bands=7)
    leg(c, int(hx) - 1, hy, -1, 0, "O")
    neck(c, (sh[0], sh[1] - 1), (head_xy[0] + 7, head_xy[1] + 7))
    paint(c, chain_mask([(sh[0], sh[1] + 0.5), (pxx - 1, pyy)], 1.05), "O")
    head(c, head_xy[0], head_xy[1], eyes, mouth)
    for ddx in (-1, 0, 1):
        for ddy in (-1, 0, 1):
            c.px(int(pxx) + ddx, int(pyy) + ddy, "L")
    c.px(int(pxx) + 1, int(pyy), "P")
    c.outline()
    return c


def build_generated():
    """Geeft {naam: (frames, intro, outro)} voor de huidige vacht (set_coat)."""
    sw6 = [0.0, 0.7, 1.0, 0.7, 0.0, -0.7]
    crouch = lambda eyes="half": pose_stand(bob=3, tail_kind="low", eyes=eyes)
    walk = [pose_stand(i, tail_kind="mid", sway=sw6[i]) for i in range(6)]
    idle = [pose_stand(sway=sw6[i], eyes="half" if i == 4 else "open", ear=1 if i == 2 else 0)
            for i in range(6)]
    sit = [pose_sit(sway=sw6[i], eyes="half" if i == 3 else "open", ear=1 if i == 1 else 0)
           for i in range(6)]
    lick = [
        pose_lick((24, 22), (17, 5), eyes="half"),
        pose_lick((26.5, 19.5), (19, 7), tongue=True),
        pose_lick((26, 18.5), (19.5, 7.5), lean=0.5),
        pose_lick((26.5, 19.5), (19, 8), tongue=True, lean=0.5),
        pose_lick((26, 18.5), (19, 7)),
        pose_lick((24, 22), (17, 5), eyes="half"),
    ]
    zpos = lambda i, k: (36 + 0.8 * ((i + 2 * k) % 6), 11 - 2.4 * ((i + 2 * k) % 6))
    breath = [0, 0, 1, 1, 1, 0]
    sleep = [crouch()] + [
        pose_sleep(breath[i], [(round(zpos(i, k)[0]), round(zpos(i, k)[1])) for k in range(3)])
        for i in range(6)] + [crouch()]
    stretch = [
        pose_wall((14, 21.5), (25, 17.5), 1.0, (40.5, 16), (23, 5), r=3.2),
        pose_wall((14.5, 22), (26, 16.5), 2.8, (41, 11), (24, 4), r=3.1, eyes="half"),
        pose_wall((15, 23), (27, 16), -1.6, (41, 6), (25, 3), r=2.8, eyes="happy", sway=0.5),
        pose_wall((15, 23), (27.5, 15.5), -2.4, (41, 4), (25, 2), r=2.7, eyes="yawn", mouth="yawn", sway=-0.5),
        pose_wall((14.5, 22.5), (26, 17), 3.2, (40.5, 11), (24, 5), r=3.3, eyes="closed"),
        pose_wall((13.5, 21.5), (25, 18), 1.5, (40, 17), (23, 6), r=3.3, eyes="half"),
    ]
    purr = []
    for i in range(6):
        purr.append(pose_sit(sway=sw6[i] * 0.6, eyes="happy", hdy=i % 2,
                             hearts=[(31, 12 - 3 * i), (36, 12 - 3 * ((i + 3) % 6))]))
    return {
        "walk": (walk, 0, 0), "idle": (idle, 0, 0), "sit": ([crouch()] + sit + [crouch()], 1, 1),
        "lick": (lick, 1, 1), "sleep": (sleep, 1, 1), "stretch": (stretch, 2, 2), "purr": (purr, 0, 0),
    }


# --------------------------------------------------------------------------- #
#  Animaties -> cairo (generated of geimporteerd)
# --------------------------------------------------------------------------- #

SPEC = {
    "walk":    dict(fps=9,   tfps=3),
    "idle":    dict(fps=2,   tfps=3),
    "sit":     dict(fps=1.5, tfps=3),
    "lick":    dict(fps=3,   tfps=2.5),
    "sleep":   dict(fps=1.5, tfps=2),
    "stretch": dict(fps=1.0, tfps=1.6),
    "purr":    dict(fps=8,   tfps=3),
}
ANIM_NAMES = list(SPEC)
ACTIVITIES = {"walk", "stretch", "lick", "sleep", "purr"}
GEN_SFX = {"stretch": {3: "mew"}}  # frame-index -> geluid (de geeuw)


class Frame:
    def __init__(self, surf):
        self.surf = surf
        self._flip = None
        self._regions = {}

    @property
    def w(self):
        return self.surf.get_width()

    @property
    def h(self):
        return self.surf.get_height()

    def flipped(self):
        if self._flip is None:
            import cairo
            s = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.w, self.h)
            ctx = cairo.Context(s)
            ctx.translate(self.w, 0)
            ctx.scale(-1, 1)
            ctx.set_source_surface(self.surf, 0, 0)
            ctx.paint()
            self._flip = s
        return self._flip

    def region(self, flip):
        """Klik-gebied = niet-transparante pixels (None als dat niet lukt)."""
        if flip not in self._regions:
            try:
                from gi.repository import Gdk
                self._regions[flip] = Gdk.cairo_region_create_from_surface(
                    self.flipped() if flip else self.surf)
            except Exception:
                self._regions[flip] = None
        return self._regions[flip]

    def alpha(self, x, y, flip):
        s = self.flipped() if flip else self.surf
        if not (0 <= x < s.get_width() and 0 <= y < s.get_height()):
            return 0
        return s.get_data()[y * s.get_stride() + x * 4 + 3]


class Anim:
    def __init__(self, frames, fps, tfps, intro=0, outro=0, sfx=None):
        self.frames, self.fps, self.tfps = frames, fps, tfps
        self.intro, self.outro, self.sfx = intro, outro, sfx or {}


def canvas_surface(canvas, scale, pal):
    import cairo
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas.w * scale, canvas.h * scale)
    ctx = cairo.Context(s)
    for y, row in enumerate(canvas.p):
        for x, col in enumerate(row):
            if col:
                ctx.set_source_rgb(*pal[col])
                ctx.rectangle(x * scale, y * scale, scale, scale)
                ctx.fill()
    return s


def load_strip(path, fw, scale):
    """Horizontale spritesheet (PNG): frames naast elkaar, gelijke breedte."""
    import cairo
    strip = cairo.ImageSurface.create_from_png(str(path))
    fh = strip.get_height()
    frames = []
    for i in range(max(1, strip.get_width() // fw)):
        s = cairo.ImageSurface(cairo.FORMAT_ARGB32, fw * scale, fh * scale)
        ctx = cairo.Context(s)
        ctx.scale(scale, scale)
        ctx.set_source_surface(strip, -i * fw, 0)
        ctx.get_source().set_filter(cairo.FILTER_NEAREST)
        ctx.rectangle(0, 0, fw, fh)
        ctx.fill()
        frames.append(Frame(s))
    return frames


def make_coats(n, names, seed):
    pool = list(COATS)
    if names:
        chosen = []
        for nm in names:
            m = [c for c in COATS if c.name == nm.strip().lower()]
            if not m:
                sys.exit(f"onbekende vacht '{nm}'. Kies uit: {', '.join(c.name for c in COATS)}")
            chosen.append(m[0])
        pool = chosen
    coats = []
    for i in range(n):
        base = pool[i % len(pool)]
        coats.append(base if i < len(pool) else coat_variant(base, i))
    return coats


def build_cat_anims(coat, scale):
    set_coat(coat)
    anims = {}
    for name, (canvases, intro, outro) in build_generated().items():
        anims[name] = Anim([Frame(canvas_surface(c, scale, coat.pal)) for c in canvases],
                           SPEC[name]["fps"], SPEC[name]["tfps"], intro, outro,
                           GEN_SFX.get(name))
    return anims


def load_custom(anims, sprite_dir, cfg, scale):
    for name in ANIM_NAMES:
        png = sprite_dir / f"{name}.png"
        if not png.exists():
            continue
        import cairo
        height = cairo.ImageSurface.create_from_png(str(png)).get_height()
        per = cfg.get("anims", {}).get(name, {})
        fw = per.get("frame_width", cfg.get("frame_width", height))
        anims[name] = Anim(load_strip(png, fw, scale),
                           per.get("fps", SPEC[name]["fps"]), per.get("tfps", SPEC[name]["tfps"]),
                           per.get("intro", 0), per.get("outro", 0),
                           {int(k): v for k, v in per.get("sfx", {}).items()})


def dump_sprites(out_dir):
    """Schrijf de ingebouwde sprites (ginger) als PNG-strips + config.json om te bewerken."""
    import cairo
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    set_coat(COATS[0])
    cfg = {"scale": 3, "frame_width": W, "anims": {}}
    for name, (canvases, intro, outro) in build_generated().items():
        strip = cairo.ImageSurface(cairo.FORMAT_ARGB32, W * len(canvases), H)
        ctx = cairo.Context(strip)
        for i, cv in enumerate(canvases):
            ctx.set_source_surface(canvas_surface(cv, 1, COATS[0].pal), i * W, 0)
            ctx.paint()
        strip.write_to_png(str(out / f"{name}.png"))
        cfg["anims"][name] = {**SPEC[name], "intro": intro, "outro": outro,
                              "sfx": {str(k): v for k, v in GEN_SFX.get(name, {}).items()}}
    (out / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"[catpet] sprites geschreven naar {out}/")


# --------------------------------------------------------------------------- #
#  Geluid (procedureel gegenereerd, of eigen .wav in sounds/)
# --------------------------------------------------------------------------- #

def _write_wav(path, samples, sr=22050):
    peak = max(1e-9, max(abs(s) for s in samples))
    data = b"".join(struct.pack("<h", int(max(-1, min(1, s / peak)) * 32000)) for s in samples)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(data)


def synth_purr(path, pitch=1.0, vol=0.5, dur=1.8, sr=22050):
    rnd = random.Random(11)
    lp1 = lp2 = 0.0
    out = []
    for i in range(int(sr * dur)):
        t = i / sr
        n = rnd.uniform(-1, 1)
        lp1 += 0.06 * (n - lp1)
        lp2 += 0.22 * (n - lp2)
        pulse = max(0.0, math.sin(2 * math.pi * 24 * pitch * t)) ** 2
        body = 0.5 * math.sin(2 * math.pi * 55 * pitch * t) + 0.25 * math.sin(2 * math.pi * 110 * pitch * t)
        s = (lp1 * 3.0 + lp2 * 1.2 + body * 0.5) * (0.25 + 0.75 * pulse)
        env = min(1, t / 0.15) * min(1, (dur - t) / 0.45) * (0.85 + 0.15 * math.sin(2 * math.pi * 1.1 * t))
        out.append(s * env * vol)
    _write_wav(path, [s / 1.0 for s in out], sr)


def synth_mew(path, pitch=1.0, vol=0.5, dur=0.45, sr=22050):
    n, phase, out = int(sr * dur), 0.0, []
    for i in range(n):
        t = i / n
        f = pitch * (520 + 380 * math.sin(math.pi * t) ** 0.8 - 120 * t)
        phase += 2 * math.pi * f / sr
        s = (math.sin(phase) + 0.5 * math.sin(2 * phase) + 0.35 * math.sin(3 * phase)
             + 0.2 * math.sin(4 * phase)) * (0.6 + 0.4 * math.sin(math.pi * t))
        env = min(1, (i / sr) / 0.03) * (1 - t) ** 0.7
        out.append(s * env * vol)
    _write_wav(path, out, sr)


class Sfx:
    def __init__(self, enabled, volume, pitches, custom_dir):
        self.enabled = enabled
        self.custom = Path(custom_dir)
        self.cache = Path.home() / ".cache" / "catpet"
        self.proc, self.last = {}, {}
        self.player = None
        if not enabled:
            return
        for cmd in (["pw-play"], ["paplay"], ["aplay", "-q"],
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
            if shutil.which(cmd[0]):
                self.player = cmd
                break
        if not self.player:
            print("[catpet] geen audiospeler gevonden (pw-play/paplay/aplay) - geluid staat uit")
            self.enabled = False
            return
        self.cache.mkdir(parents=True, exist_ok=True)
        self.files = {}
        for i, p in enumerate(pitches):
            for name, fn in (("purr", synth_purr), ("mew", synth_mew)):
                f = self.cache / f"{name}_{int(p * 100)}_{int(volume * 100)}.wav"
                if not f.exists():
                    fn(f, p, volume)
                self.files[(name, i)] = f

    def play(self, name, idx):
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self.last.get(name, 0) < 0.3:
            return
        p = self.proc.get(name)
        if p is not None and p.poll() is None:
            return  # speelt nog
        f = None
        for ext in ("wav", "ogg", "mp3"):
            if (self.custom / f"{name}.{ext}").exists():
                f = self.custom / f"{name}.{ext}"
                break
        f = f or self.files.get((name, idx))
        if f:
            self.last[name] = now
            self.proc[name] = subprocess.Popen(
                self.player + [str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- #
#  Gedrag (per kat)
# --------------------------------------------------------------------------- #

class Pet:
    def __init__(self, index, anims, sw, sh, floor, scale, speed, calm):
        self.index, self.anims, self.sw, self.sh, self.floor = index, anims, sw, sh, floor
        self.speed_mult = speed * random.uniform(0.88, 1.12)
        self.calm = calm * random.uniform(0.85, 1.2)
        self.px_speed = 18 * scale * self.speed_mult
        self.dir = random.choice((-1, 1))
        self.state, self.t, self.dur = "idle", 0.0, random.uniform(1, 5)
        self.target, self.goal = 0.0, "wander"
        self.last, self.events, self._idx = {}, [], -1
        self.x = random.uniform(0, max(1, sw - anims["walk"].frames[0].w))

    def frame(self):
        a = self.anims[self.state]
        fps = a.fps * (self.speed_mult if self.state == "walk" else 1)
        n = len(a.frames)
        loop_n = max(1, n - a.intro - a.outro)
        intro_t, outro_t = a.intro / a.tfps, a.outro / a.tfps
        if a.intro and self.t < intro_t:
            i = int(self.t * a.tfps)
        elif a.outro and self.state != "walk" and self.t >= self.dur - outro_t:
            i = n - a.outro + min(int((self.t - (self.dur - outro_t)) * a.tfps), a.outro - 1)
        else:
            i = a.intro + int((self.t - intro_t) * fps) % loop_n
        return a.frames[min(i, n - 1)], min(i, n - 1)

    def rect(self):
        f, _ = self.frame()
        return int(self.x), self.sh - self.floor - f.h, f.w, f.h

    def signature(self):
        return (self.state, self.frame()[1], int(self.x), self.dir)

    def hit(self, px, py):
        f, _ = self.frame()
        x, y, w, h = self.rect()
        return x <= px < x + w and y <= py < y + h and f.alpha(int(px) - x, int(py) - y, self.dir < 0) > 0

    # -- toestanden
    def set_state(self, state, loop_time):
        a = self.anims[state]
        self.state, self.t, self._idx = state, 0.0, -1
        self.dur = loop_time + (a.intro + a.outro) / a.tfps
        if state in ACTIVITIES:
            self.last[state] = time.monotonic()

    def ready(self, name, cooldown):
        return time.monotonic() - self.last.get(name, -1e9) > cooldown * self.calm

    def go(self, target, goal="wander"):
        self.target, self.goal = target, goal
        self.set_state("walk", 0)
        if abs(target - self.x) > 1:
            self.dir = 1 if target > self.x else -1

    def rest(self):
        kind = random.choices(("sit", "idle"), (60, 40))[0]
        self.set_state(kind, random.uniform(5, 12) * self.calm)

    def choose_next(self):
        if self.state in ACTIVITIES:
            return self.rest()
        fw = self.anims["walk"].frames[0].w
        opts = {"walk": 50, "sit": 15, "idle": 8}
        if self.ready("stretch", 90):
            opts["wall"] = 15
        if self.ready("sleep", 150):
            opts["sleep"] = 12
        if self.state == "sit" and self.ready("lick", 30) and random.random() < 0.5:
            return self.set_state("lick", random.uniform(7, 12))
        choice = random.choices(list(opts), list(opts.values()))[0]
        if choice == "walk":
            tx = self.x
            for _ in range(12):
                tx = random.uniform(0, max(1, self.sw - fw))
                if abs(tx - self.x) > 250:
                    break
            self.go(tx)
        elif choice == "wall":
            self.go(random.choice((0.0, float(self.sw - fw))), "wall")
        elif choice == "sleep":
            self.set_state("sleep", random.uniform(25, 60) * self.calm)
        elif choice == "sit":
            self.set_state("sit", random.uniform(6, 14) * self.calm)
        else:
            self.set_state("idle", random.uniform(4, 9) * self.calm)

    def begin_stretch(self):
        w = self.anims["stretch"].frames[0].w
        if self.target <= 0:
            self.dir, self.x = -1, 0.0
        else:
            self.dir, self.x = 1, float(self.sw - w)
        self.set_state("stretch", random.uniform(6, 10))

    def on_click(self):
        self.events.append("purr")
        if self.state == "purr":
            self.dur += 2.5
        else:
            self.set_state("purr", random.uniform(5, 8))

    def update(self, dt):
        self.t += dt
        if self.state == "walk":
            step = self.px_speed * dt
            d = self.target - self.x
            if abs(d) <= step:
                self.x = self.target
                self.begin_stretch() if self.goal == "wall" else self.choose_next()
            else:
                self.dir = 1 if d > 0 else -1
                self.x += step * self.dir
        elif self.t >= self.dur:
            self.choose_next()
        # geluid bij bepaalde frames (bv. de geeuw)
        _f, idx = self.frame()
        if idx != self._idx:
            self._idx = idx
            snd = self.anims[self.state].sfx.get(idx)
            if snd and random.random() < 0.7:
                self.events.append(snd)

    def draw(self, ctx):
        f, _ = self.frame()
        x, y, _w, _h = self.rect()
        ctx.set_source_surface(f.flipped() if self.dir < 0 else f.surf, x, y)
        ctx.paint()


# --------------------------------------------------------------------------- #
#  GTK / layer-shell
# --------------------------------------------------------------------------- #

def run(args):
    import gi
    import cairo
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    try:
        gi.require_version("GtkLayerShell", "0.1")
    except ValueError:
        sys.exit("gtk-layer-shell ontbreekt:  sudo pacman -S gtk-layer-shell")
    from gi.repository import Gtk, Gdk, GLib, GtkLayerShell

    random.seed(args.seed)
    display = Gdk.Display.get_default()
    monitor = display.get_monitor(min(args.monitor, display.get_n_monitors() - 1))
    geo = monitor.get_geometry()

    sprite_dir = Path(args.sprites)
    cfg = {}
    if (sprite_dir / "config.json").exists():
        cfg = json.loads((sprite_dir / "config.json").read_text())
    scale = args.scale or cfg.get("scale", 3)

    coats = make_coats(args.cats, args.coats.split(",") if args.coats else None, args.seed)
    pets = []
    for i, coat in enumerate(coats):
        anims = build_cat_anims(coat, scale)
        if sprite_dir.is_dir():
            load_custom(anims, sprite_dir, cfg, scale)
        pets.append(Pet(i, anims, geo.width, geo.height, args.floor, scale, args.speed, args.calm))
    print("[catpet] katten: " + ", ".join(c.name for c in coats))
    pitches = [1.0] + [round(random.uniform(0.8, 1.3), 2) for _ in coats[1:]]
    sfx = Sfx(not args.no_sfx, args.volume, pitches, Path(args.sounds))

    win = Gtk.Window()
    win.set_title("catpet")
    win.set_app_paintable(True)
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)
    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)

    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_namespace(win, "catpet")
    GtkLayerShell.set_monitor(win, monitor)
    GtkLayerShell.set_layer(
        win, GtkLayerShell.Layer.OVERLAY if args.layer == "overlay" else GtkLayerShell.Layer.TOP)
    for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                 GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
        GtkLayerShell.set_anchor(win, edge, True)
    GtkLayerShell.set_exclusive_zone(win, -1)
    GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
    win.input_shape_combine_region(cairo.Region())

    def on_draw(_w, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        for p in sorted(pets, key=lambda p: p.state == "purr"):  # spinnende kat bovenop
            p.draw(ctx)
        return False

    def on_press(_w, event):
        if event.button == 1:
            for p in reversed(pets):
                if p.hit(event.x, event.y):
                    p.on_click()
                    break
        return True

    win.connect("draw", on_draw)
    win.connect("button-press-event", on_press)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()

    st = {"last": time.monotonic(), "sig": None, "rects": [p.rect() for p in pets]}

    def update_input_region():
        total = cairo.Region()
        for p in pets:
            f, _ = p.frame()
            x, y, w, h = p.rect()
            reg = f.region(p.dir < 0)
            if reg is None:
                reg = cairo.Region(cairo.RectangleInt(x, y, w, h))
            else:
                reg = reg.copy()
                reg.translate(x, y)
            total.union(reg)
        win.input_shape_combine_region(total)

    def tick():
        now = time.monotonic()
        dt = min(now - st["last"], 0.1)
        st["last"] = now
        for p in pets:
            p.update(dt)
            while p.events:
                sfx.play(p.events.pop(0), p.index)
        sig = tuple(p.signature() for p in pets)
        if sig != st["sig"]:
            st["sig"] = sig
            new = [p.rect() for p in pets]
            for (ox, oy, ow, oh), (nx, ny, nw, nh) in zip(st["rects"], new):
                x0, y0 = min(ox, nx) - 2, min(oy, ny) - 2
                win.queue_draw_area(x0, y0, max(ox + ow, nx + nw) + 2 - x0, max(oy + oh, ny + nh) + 2 - y0)
            st["rects"] = new
            update_input_region()
        return True

    GLib.timeout_add(33, tick)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    Gtk.main()


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Pixel-katten desktop pet voor Hyprland")
    ap.add_argument("--cats", type=int, default=1, help="aantal katten (elk met eigen kleur/vlekken)")
    ap.add_argument("--coats", help="kies vachten, bv. ginger,tuxedo,calico (zie lijst hieronder)")
    ap.add_argument("--seed", type=int, default=None, help="vaste random-seed")
    ap.add_argument("--scale", type=int, default=0, help="pixelgrootte (standaard 3)")
    ap.add_argument("--floor", type=int, default=0, help="px boven de onderrand (bv. hoogte van je waybar)")
    ap.add_argument("--monitor", type=int, default=0, help="monitor-index")
    ap.add_argument("--speed", type=float, default=1.0, help="loopsnelheid (1.0 = normaal)")
    ap.add_argument("--calm", type=float, default=1.0,
                    help="rust tussen activiteiten: 1.0 = normaal, 2.0 = rustiger, 0.5 = drukker")
    ap.add_argument("--layer", choices=("top", "overlay"), default="top",
                    help="top = onder fullscreen-apps, overlay = altijd erboven")
    ap.add_argument("--no-sfx", action="store_true", help="geen geluid")
    ap.add_argument("--volume", type=float, default=0.4, help="geluidsvolume 0..1 (standaard 0.4)")
    ap.add_argument("--sounds", default=str(here / "sounds"), help="map met eigen purr.wav / mew.wav")
    ap.add_argument("--sprites", default=str(here / "sprites"), help="map met eigen sprite-PNG's")
    ap.add_argument("--dump-sprites", metavar="MAP",
                    help="schrijf de ingebouwde sprites als PNG-strips naar MAP en stop")
    ap.epilog = "vachten: " + ", ".join(c.name for c in COATS)
    args = ap.parse_args()
    if args.dump_sprites:
        dump_sprites(args.dump_sprites)
        return
    args.cats = max(1, args.cats)
    run(args)


if __name__ == "__main__":
    main()
