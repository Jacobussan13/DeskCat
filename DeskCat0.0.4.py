#!/usr/bin/env python3
"""
catpet - pixel-katten desktop pet voor Hyprland (Wayland) via gtk-layer-shell.

Installatie (CachyOS / Arch):
    sudo pacman -S gtk3 gtk-layer-shell python-gobject python-cairo
    (voor geluid: pipewire of alsa-utils; pw-play / paplay / aplay worden automatisch gebruikt)

Starten:
    python catpet.py
    python catpet.py --cats 3 --size 4          # drie katten; --size = pixelgrootte van katten EN voerbak
    python catpet.py --cats 2 --coats tuxedo,calico --floor 40 --calm 1.5

Klik op een kat: hij gaat spinnen en er opent een menu (snelheid, grootte, rust, volume, voerbak, acties).
Eigen sprites per kat:  python catpet.py --cats 3 --dump-sprites   -> sprites/cat1, cat2, cat3 (zie README.txt)
Stoppen: pkill -f catpet.py
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

W, H = 40, 33   # framegrootte in art-pixels
OY = 3          # ontwerp-y -> canvas-y (ruimte boven voor oren / sprongen / hartjes)
GROUND = 28     # onderste pootrij (ontwerp-coordinaten)
BOWL_W, BOWL_H = 16, 9
MOUTH_X = 34    # x (ontwerp) van de mond als de kat eet; bepaalt waar de kat bij de bak staat


# ---------- vachten (kleuren + patronen) ----------------------------------- #

def _rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


class Coat:
    def __init__(self, name, O, D, L, pattern="tabby", face=None, S=None, spots=(),
                 P="#f58a96", K="#211a24", eye="K", E="#cfe03c"):
        self.name, self.pattern, self.face, self.eyecol = name, pattern, face or pattern, eye
        self.spots = list(spots)
        o = _rgb(O)
        self.pal = {"K": _rgb(K), "O": o, "D": _rgb(D), "L": _rgb(L), "S": _rgb(S or D),
                    "P": _rgb(P), "E": _rgb(E), "W": (1, 1, 1), "F": tuple(v * 0.80 for v in o),
                    "Y": _rgb("#b87338"), "y": _rgb("#8c5126")}


SPOTS = [(0.15, 1.8, 2.0, "D"), (0.42, -0.9, 1.7, "D"), (0.66, 1.7, 1.9, "D"),
         (0.88, 0.1, 1.5, "D"), (0.30, -1.8, 1.1, "D")]
PATCH = [(0.18, 1.0, 3.3, "D"), (0.55, 0.4, 3.5, "S"), (0.86, 1.6, 2.7, "D")]

COATS = [
    Coat("ginger",    "#ee9438", "#bd6121", "#fce5c2", "tabby"),
    Coat("grey",      "#979cac", "#61667a", "#e4e8f0", "tabby"),
    Coat("tuxedo",    "#3c3a4a", "#2c2a38", "#f6f6fa", "tux", eye="E"),
    Coat("calico",    "#faf4ec", "#e88c34", "#ffffff", "calico", S="#3e343c", spots=PATCH),
    Coat("cream",     "#f5d7a0", "#96603c", "#fff0d6", "spots", face="spot", spots=SPOTS),
    Coat("blue",      "#70809a", "#60708a", "#aab8c8", "plain"),
    Coat("chocolate", "#96644a", "#643e28", "#e6c8aa", "tabby"),
    Coat("snow",      "#fafafc", "#c8cede", "#ffffff", "plain"),
]
COAT = COATS[0]
BOWL_PAL = {"K": _rgb("#211a24"), "B": _rgb("#5c94d9"), "b": _rgb("#456fb3"), "R": _rgb("#d9ebff"),
            "W": (1, 1, 1), "Y": _rgb("#b87338"), "y": _rgb("#8c5126")}


def set_coat(c):
    global COAT
    COAT = c


def coat_variant(base, i):
    """Extra katten boven de 8: zelfde patroon, licht andere tint."""
    k = 0.85 + random.Random(i * 7919).random() * 0.3
    c = Coat.__new__(Coat)
    c.__dict__.update(base.__dict__)
    c.name = f"{base.name}{i}"
    c.pal = {key: (tuple(min(1, v * k) for v in val) if key in "ODLSF" else val)
             for key, val in base.pal.items()}
    return c


# ---------- canvas --------------------------------------------------------- #

class Canvas:
    def __init__(self, w=W, h=H, oy=None):
        self.w, self.h = w, h
        self.oy = OY if oy is None else oy
        self.p = [[None] * w for _ in range(h)]

    def px(self, x, y, c):
        x, y = int(round(x)), int(round(y)) + self.oy
        if 0 <= x < self.w and 0 <= y < self.h:
            self.p[y][x] = c

    def get(self, x, y):
        x, y = int(round(x)), int(round(y)) + self.oy
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


def body_pts(hip, sh, arch):
    """Ruglijn van heup naar schouder; arch>0 = bolle rug, arch<0 = holle rug."""
    if not arch:
        return [hip, sh]
    mx, my = (hip[0] + sh[0]) / 2, (hip[1] + sh[1]) / 2
    dx, dy = sh[0] - hip[0], sh[1] - hip[1]
    L = math.hypot(dx, dy)
    return bezier(hip, (mx + dy / L * arch, my - dx / L * arch), sh, 10)


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


# ---------- onderdelen (overal hetzelfde -> consistente kat) -------------- #

def head(c, x, y, eyes="open", ear=0, mouth="closed", tongue=False):
    """Kop 9x8, kijkt naar rechts. (x,y) = linkerbovenhoek."""
    coat, face = COAT, COAT.face
    x, y = int(round(x)), int(round(y))
    for yy in range(8):
        for xx in range(9):
            if xx in (0, 8) and yy in (0, 7):
                continue
            col = "O"
            if face == "calico":
                col = "S" if (yy <= 3 and xx <= 3) else "D" if (yy <= 3 and xx >= 5) else "O"
            elif face == "spot" and (xx - 6) ** 2 + (yy - 2) ** 2 <= 3.5:
                col = "D"
            c.px(x + xx, y + yy, col)
    lcol = "S" if face == "calico" else "O"
    rcol = "D" if face in ("calico", "spot") else "O"
    left = [(1, -1), (2, -1), (3, -1), (1, -2), (2, -2), (1, -3)]
    right = [(5, -1), (6, -1), (7, -1), (6, -2), (7, -2), (7, -3)]
    if ear == 1:  # oor-flick
        right = [(6, -1), (7, -1), (7, -2), (5, 0)]
    for ex, ey in left:
        c.px(x + ex, y + ey, lcol)
    for ex, ey in right:
        c.px(x + ex, y + ey, rcol)
    c.px(x + 2, y - 1, "P")
    c.px(x + 6, y - 1, "P")
    if face == "tabby":
        for ex, ey in ((3, 0), (4, 0), (5, 0), (4, 1), (4, 2), (0, 3), (1, 3)):
            c.px(x + ex, y + ey, "D")
    for yy in range(4, 8):  # snuit
        for xx in range(5, 9):
            if (xx, yy) != (8, 7):
                c.px(x + xx, y + yy, "L")
    if face == "tux":  # witte bef + blaze
        for yy in range(3, 8):
            for xx in range(4, 9):
                if (xx, yy) != (8, 7):
                    c.px(x + xx, y + yy, "L")
        for yy in range(0, 3):
            c.px(x + 4, y + yy, "L")
    c.px(x + 8, y + 4, "P")  # neus
    ec = coat.eyecol
    for ex in (2, 6):  # ogen
        if eyes == "open":
            c.px(x + ex, y + 3, ec); c.px(x + ex, y + 4, ec)
        elif eyes == "half":
            c.px(x + ex, y + 4, ec); c.px(x + ex + 1, y + 4, ec)
        elif eyes == "closed":
            for k in (-1, 0, 1):
                c.px(x + ex + k, y + 4, "K" if ec == "K" else ec)
        elif eyes == "happy":  # ^ ^
            c.px(x + ex - 1, y + 4, ec); c.px(x + ex, y + 3, ec); c.px(x + ex + 1, y + 4, ec)
        elif eyes == "yawn":   # > <
            c.px(x + ex - 1 + (2 if ex == 6 else 0) * 0, y + 3, ec)
            c.px(x + ex, y + 4, ec)
            c.px(x + ex - 1, y + 5, ec)
    if mouth == "open":
        c.px(x + 7, y + 5, "K"); c.px(x + 8, y + 5, "K")
        c.px(x + 7, y + 6, "K"); c.px(x + 8, y + 6, "P")
    elif mouth == "yawn":
        for yy in (5, 6):
            for xx in (6, 7, 8):
                c.px(x + xx, y + yy, "K")
        c.px(x + 7, y + 6, "P"); c.px(x + 8, y + 7, "K"); c.px(x + 7, y + 7, "K")
    elif mouth == "smile":
        c.px(x + 7, y + 6, "K")
    else:
        c.px(x + 6, y + 6, "K")
    if tongue:
        c.px(x + 9, y + 5, "P"); c.px(x + 9, y + 6, "P")


def neck(c, a, b):
    paint(c, chain_mask([a, b], 2.7), "O")


def tail(c, pts, sway=0.0, dy=0.0, border=False):
    n = max(1, len(pts) - 1)
    pts = [(x + sway * (i / n) * 2.2, y + dy) for i, (x, y) in enumerate(pts)]
    m = chain_mask(pts, 1.35, 1.0)
    for (x, y), (t, _v) in m.items():
        col = "O"
        if t > 0.82:
            col = "F"
        elif COAT.pattern == "tabby" and int(t * 7) % 2 == 1:
            col = "D"
        c.px(x, y, col)
    if border:  # donkere rand zodat de staart los van het lijf leesbaar blijft
        keys = set(m)
        for (x, y) in m:
            for dx, dy2 in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (x + dx, y + dy2) not in keys and c.get(x + dx, y + dy2) is not None:
                    c.px(x + dx, y + dy2, "K")


def limb(c, top, foot, col):
    """Poot van `top` naar `foot` = (x, onderste pootrij); met lichte pootjes."""
    fx, row = foot
    paint(c, chain_mask([top, (fx, row - 0.3)], 1.05), col)
    bx = int(round(fx))
    for xx in range(bx - 1, bx + 2):
        c.px(xx, row, "L")
    c.px(bx, row - 1, "L"); c.px(bx + 1, row - 1, "L")


def leg(c, x, y0, dx, lift, col):
    limb(c, (x, y0), (x + dx, GROUND - lift), col)


def paw_blob(c, px, py):
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            c.px(int(px) + dx, int(py) + dy, "L")
    c.px(int(px), int(py), "P")


Z = ["###", "..#", ".#.", "###"]
HEART = [".#.#.", "#####", ".###.", "..#.."]


def stamp(c, glyph, x, y, col, halo=True):
    cells = [(x + i, y + j) for j, row in enumerate(glyph) for i, ch in enumerate(row) if ch == "#"]
    if halo:
        c.halo(cells)
    for cx, cy in cells:
        c.px(cx, cy, col)


# ---------- generieke viervoeter (stand, bow, eten, boog, ...) ------------- #

LEG = [(3, 0), (1, 0), (-1, 0), (-3, 0), (-1, 2), (1, 2)]
BOB = [0, -1, 0, 0, -1, 0]
TAILS = {
    "mid":  [(8.5, 19), (6, 18.5), (4, 17), (3, 14.5), (3.5, 12)],
    "up":   [(8.5, 19), (6.5, 17.5), (5.5, 15), (5.5, 12), (6.5, 9.5)],
    "high": [(8.5, 18), (6.5, 14.5), (6, 11), (6.5, 7.5), (7.5, 4.5)],
    "low":  [(8.5, 20.5), (6, 22), (4, 24), (3, 26)],
}


def stand_legs(y0, nf=(0, 0), ff=(0, 0), nb=(0, 0), fb=(0, 0)):
    return {"nf": ((22, y0), (22 + nf[0], GROUND - nf[1])),
            "ff": ((19, y0), (19 + ff[0], GROUND - ff[1])),
            "nb": ((10, y0), (10 + nb[0], GROUND - nb[1])),
            "fb": ((13, y0), (13 + fb[0], GROUND - fb[1]))}


def pose_quad(hip, sh, head_xy, legs, arch=0.0, r=3.4, tail_pts=None, sway=0.0, tail_dy=0.0,
              eyes="open", mouth="closed", ear=0, tongue=False, extra=None):
    c = Canvas()
    tail(c, tail_pts or TAILS["mid"], sway, tail_dy)
    for k in ("ff", "fb"):
        limb(c, *legs[k], "F")
    paint_fur(c, chain_mask(body_pts(hip, sh, arch), r, r - 0.2))
    for k in ("nb", "nf"):
        limb(c, *legs[k], "O")
    neck(c, (sh[0] - 0.5, sh[1] - 1), (head_xy[0] + 4, head_xy[1] + 5))
    head(c, head_xy[0], head_xy[1], eyes, ear, mouth, tongue)
    if extra:
        extra(c)
    c.outline()
    return c


def pose_stand(phase=None, bob=0, tail_kind="mid", sway=0.0, eyes="open", ear=0, mouth="closed",
               legs=None, tongue=False):
    if phase is not None:
        a, b = LEG[phase], LEG[(phase + 3) % 6]
        bob = BOB[phase]
        legs = dict(nf=a, fb=a, ff=b, nb=b)
    legs = legs or {}
    return pose_quad((12, 20 + bob), (23, 20 + bob), (25, 12 + bob), stand_legs(21 + bob, **legs),
                     tail_pts=TAILS[tail_kind], sway=sway, tail_dy=bob, eyes=eyes, ear=ear,
                     mouth=mouth, tongue=tongue)


# ---------- zitten / likken / wassen --------------------------------------- #

def sit_base(c, tail_sway, head_xy, torso_top, paw=None, eyes="open", ear=0, mouth="closed",
             tongue=False, hdy=0, tail_pts=None, tail_late=False, arm_from=(21, 18)):
    tp = tail_pts or [(8, 26), (5.5, 27), (3.5, 26), (3.2, 23.5), (4, 21)]
    if not tail_late:
        tail(c, tp, tail_sway)
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
    if tail_late:
        tail(c, tp, tail_sway, border=True)
    hx, hy = head_xy
    neck(c, (torso_top[0], torso_top[1] + 1), (hx + 3.5, hy + 5.5))
    if paw is not None:
        px, py = paw
        paint(c, chain_mask([arm_from, (23.5, 22.5), (px, py)], 1.35), "O")
    head(c, hx, hy + hdy, eyes, ear, mouth, tongue)
    if paw is not None:
        paw_blob(c, *paw)


def pose_sit(sway=0.0, eyes="open", ear=0, mouth="closed", hdy=0, hearts=None):
    c = Canvas()
    sit_base(c, sway, (19, 6), (20.5, 13.2), eyes=eyes, ear=ear, mouth=mouth, hdy=hdy)
    c.outline()
    for hx, hy in (hearts or []):
        stamp(c, HEART, hx, hy, "P")
    return c


def pose_lick(paw, head_xy, eyes="closed", tongue=False, sway=0.0, lean=0.0):
    c = Canvas()
    sit_base(c, sway, head_xy, (20.5 + lean, 13.4), paw=paw, eyes=eyes, tongue=tongue)
    c.outline()
    return c


def pose_wash(paw, eyes="closed", sway=0.0):
    """Gezicht wassen: poot veegt over wang en oor."""
    c = Canvas()
    sit_base(c, sway, (19, 6), (20.5, 13.2), paw=paw, eyes=eyes, arm_from=(21, 18))
    c.outline()
    return c


def pose_lick_tail(head_xy, tip, tongue=False, eyes="closed", sway=0.0):
    """Zittend, staart om de voorkant heen naar de mond; hij likt aan de staartpunt."""
    c = Canvas()
    tp = [(8, 26), (5.5, 28), (10, 28.6), (17, 28.4), (23, 27.4), tip]
    sit_base(c, sway, head_xy, (19.2, 15.8), eyes=eyes, tongue=tongue, tail_pts=tp, tail_late=True)
    c.outline()
    return c


# ---------- liggen / slapen -------------------------------------------------- #

def pose_loaf(breath=0, zs=(), eyes="closed"):
    c = Canvas()
    tail(c, [(8.5, 26), (5.5, 27), (3.5, 25.5), (3.5, 23)], 0.0)
    r = 3.2 + 0.5 * breath
    paint_fur(c, chain_mask([(10.5, 25 - 0.5 * breath), (24, 25 - 0.5 * breath)], r), bands=6)
    c.rect(26, 26, 6, 3, "L")
    head(c, 24, 20, eyes)
    c.outline()
    for zx, zy in zs:
        stamp(c, Z, zx, zy, "W")
    return c


def pose_curl(k, breath=0, zs=(), eyes="closed"):
    """k = 0 (liggend) .. 1 (helemaal opgerold, staart om het lichaam)."""
    c = Canvas()
    lerp = lambda a, b: a + (b - a) * k
    loaf = [(8.5, 26), (6, 27.2), (4.5, 27), (3.6, 25.5), (3.5, 23)]
    curl = [(8, 25.5), (10, 28), (16, 28.6), (23, 28.2), (27.5, 26.5)]
    ry = 3.2 + 1.5 * k + 0.5 * breath
    rx = 8.3 + 0.7 * k
    body = ellipse_mask(17.3, 28.2 - ry, rx, ry)
    paint_fur(c, body, bands=6)
    if k < 0.5:
        c.rect(26, 26, 6, 3, "L")
    pts = [(lerp(a[0], b[0]), lerp(a[1], b[1])) for a, b in zip(loaf, curl)]
    tail(c, pts, 0.0, border=k > 0.55)
    head(c, lerp(24, 20), lerp(20, 20.5), eyes)
    c.outline()
    for zx, zy in zs:
        stamp(c, Z, zx, zy, "W")
    return c


def pose_sit_lean():
    """Tussenstap zitten -> liggen: borst zakt, voorpoten naar voren."""
    c = Canvas()
    tail(c, [(8, 26.5), (5.5, 27.5), (3.5, 26.5), (3.2, 24.5)], 0.0)
    paint_fur(c, ellipse_mask(13, 24, 5.8, 4.3), belly=False, bands=5)
    paint_fur(c, chain_mask([(17, 23), (22.5, 19)], 3.3, 3.0), belly=False, bands=5)
    limb(c, (19, 24), (26, GROUND), "F")
    limb(c, (22, 23), (29, GROUND), "O")
    neck(c, (22, 19), (26, 17))
    head(c, 22, 13, "half")
    c.outline()
    return c


# ---------- eten ------------------------------------------------------------- #

def pose_eat(head_xy, eyes="open", mouth="closed", ear=0, sway=0.0, crumbs=(), tongue=False, dip=0.0):
    hx, hy = head_xy
    lower = 1.0 + dip
    legs = stand_legs(21, nf=(2.5, 0), ff=(2, 0))
    legs["nf"] = ((22, 23), (24, GROUND))
    legs["ff"] = ((19, 23), (21.5, GROUND))

    def extra(c):
        for cx, cy in crumbs:
            c.px(cx, cy, "Y")
    return pose_quad((12, 19.3), (23, 21.5 + lower * 0.3), head_xy, legs, tail_pts=TAILS["up"],
                     sway=sway, tail_dy=-0.5, eyes=eyes, mouth=mouth, ear=ear, tongue=tongue, extra=extra)


# ---------- rekken ----------------------------------------------------------- #

def pose_wall(hip, sh, arch, paw, head_xy, r=3.1, eyes="open", mouth="closed", sway=0.0):
    c = Canvas()
    hx, hy = hip
    tail(c, [(hx - 3, hy - 1), (hx - 6, hy - 3), (hx - 8, hy - 7), (hx - 8, hy - 11), (hx - 7, hy - 14)], sway)
    leg(c, int(hx) + 2, hy, 1, 0, "F")
    body = chain_mask(body_pts(hip, sh, arch), r, r - 0.3)
    pxx, pyy = paw
    paint(c, chain_mask([(sh[0] - 1, sh[1] + 1), (pxx - 1.5, pyy + 2)], 1.05), "F")
    paint_fur(c, body, bands=7)
    leg(c, int(hx) - 1, hy, -1, 0, "O")
    neck(c, (sh[0], sh[1] - 1), (head_xy[0] + 3.5, head_xy[1] + 6))
    paint(c, chain_mask([(sh[0], sh[1] + 0.5), (pxx - 1, pyy)], 1.05), "O")
    head(c, head_xy[0], head_xy[1], eyes, 0, mouth)
    for ddx in (-1, 0, 1):
        for ddy in (-1, 0, 1):
            c.px(int(pxx) + ddx, int(pyy) + ddy, "L")
    c.px(int(pxx) + 1, int(pyy), "P")
    c.outline()
    return c


def pose_bow(hip, sh, head_xy, nf, ff, nb, fb, arch=0.0, tail_kind="high", eyes="closed",
             mouth="closed", sway=0.0, r=3.3):
    legs = {"nf": ((sh[0] - 1, sh[1] + 1), (nf, GROUND)), "ff": ((sh[0] - 3.5, sh[1] + 1), (ff, GROUND)),
            "nb": ((hip[0] - 2, hip[1] + 2), (nb, GROUND)), "fb": ((hip[0] + 1, hip[1] + 2), (fb, GROUND))}
    return pose_quad(hip, sh, head_xy, legs, arch=arch, r=r, tail_pts=TAILS[tail_kind], sway=sway,
                     tail_dy=hip[1] - 20, eyes=eyes, mouth=mouth)


# ---------- voerbak ---------------------------------------------------------- #

def bowl_canvas(level):
    """level 0 = vol, 1, 2, 3 = leeg."""
    c = Canvas(BOWL_W, BOWL_H, oy=0)
    h = 3 - level
    for r in range(h):
        row = 2 - r
        wd = [12, 8, 4][r] if h == 3 else [10, 6, 0][r] if h == 2 else [8, 0, 0][r]
        x0 = (BOWL_W - wd) // 2
        for x in range(x0, x0 + wd):
            c.px(x, row, "y" if (x * 7 + row * 13) % 5 == 0 else "Y")
    for x in range(0, BOWL_W):
        c.px(x, 3, "R")
    for r, (a, b) in zip(range(4, 9), ((1, 14), (1, 14), (2, 13), (3, 12), (4, 11))):
        for x in range(a, b + 1):
            c.px(x, r, "B" if r < 6 else "b")
    for x in range(4, 12):
        c.px(x, 5, "W")
    c.outline()
    return c


# ---------- alle animaties --------------------------------------------------- #

def build_generated():
    """{naam: dict(frames, fps, tfps, intro, outro, durs, sfx)} voor de huidige vacht (set_coat)."""
    sw6 = [0.0, 0.7, 1.0, 0.7, 0.0, -0.7]
    crouch = lambda eyes="half": pose_stand(bob=2, tail_kind="low", eyes=eyes)
    A = {}

    def add(name, frames, fps=3, tfps=3, intro=0, outro=0, durs=None, sfx=None):
        A[name] = dict(frames=frames, fps=fps, tfps=tfps, intro=intro, outro=outro,
                       durs=durs, sfx=sfx or {})

    # lopen / huppelen / staan / zitten
    add("walk", [pose_stand(i, tail_kind="mid", sway=sw6[i]) for i in range(6)], fps=9)
    hop = [
        pose_stand(bob=2, tail_kind="low", legs={}),
        pose_stand(bob=-1, tail_kind="mid", legs=dict(nf=(3, 1), ff=(2, 1), nb=(-3, 1), fb=(-2, 1))),
        pose_stand(bob=-4, tail_kind="up", mouth="open", legs=dict(nf=(4, 4), ff=(3, 4), nb=(-4, 4), fb=(-3, 4))),
        pose_stand(bob=-4, tail_kind="mid", legs=dict(nf=(1, 4), ff=(0, 4), nb=(-1, 4), fb=(0, 4))),
        pose_stand(bob=0, tail_kind="up", legs=dict(nf=(2, 0), ff=(1, 0), nb=(-2, 0), fb=(-1, 0))),
        pose_stand(bob=1, tail_kind="mid", legs={}),
    ]
    add("hop", hop, fps=7)
    add("idle", [pose_stand(sway=sw6[i], eyes="half" if i == 4 else "open", ear=1 if i == 2 else 0)
                 for i in range(6)], fps=2)
    add("sit", [pose_sit(sway=sw6[i], eyes="half" if i == 3 else "open", ear=1 if i == 1 else 0)
                for i in range(6)], fps=1.5)

    # tussenanimaties (posities: stand <-> sit <-> lie)
    mid_sit = pose_quad((12, 23.5), (22.5, 19.5), (22, 9),
                        {"nf": ((22, 21), (22, GROUND)), "ff": ((19, 21), (19.5, GROUND)),
                         "nb": ((11, 24), (15, GROUND)), "fb": ((13, 24), (17, GROUND))},
                        tail_pts=TAILS["low"], eyes="half")
    t_ss = [crouch(), mid_sit, pose_sit()]
    add("t_stand_sit", t_ss, durs=[0.25, 0.25, 0.3])
    add("t_sit_stand", t_ss[::-1], durs=[0.3, 0.25, 0.25])
    t_sl = [pose_sit(eyes="half"), pose_sit_lean(), pose_loaf(0, eyes="half")]
    add("t_sit_lie", t_sl, durs=[0.45, 0.5, 0.55])
    add("t_lie_sit", t_sl[::-1], durs=[0.55, 0.5, 0.45])

    # likken: poot, staart, gezicht wassen
    add("lick_paw", [pose_lick((24, 22), (17, 5), eyes="half"),
                     pose_lick((26.5, 19.5), (19, 7), tongue=True) if False else
                     pose_lick((27, 18.5), (20, 7), tongue=True),
                     pose_lick((26.5, 17.5), (20.5, 7.5), lean=0.5),
                     pose_lick((27, 18.5), (20, 8), tongue=True, lean=0.5),
                     pose_lick((26.5, 17.5), (20, 7)),
                     pose_lick((24, 22), (17, 5), eyes="half")], fps=3, tfps=2.5, intro=1, outro=1)
    add("lick_tail", [pose_lick_tail((17, 9), (24, 27.5), eyes="half"),
                      pose_lick_tail((19, 16), (26, 25), tongue=True),
                      pose_lick_tail((19.5, 16.5), (26.5, 24.5)),
                      pose_lick_tail((19, 17), (26, 25.5), tongue=True, sway=0.3),
                      pose_lick_tail((19.5, 16.5), (26.5, 24.8)),
                      pose_lick_tail((17, 9), (24, 27.5), eyes="half")], fps=3, tfps=2.5, intro=1, outro=1)
    add("wash", [pose_wash((24.5, 17), eyes="half"),
                 pose_wash((26.5, 12), eyes="closed"), pose_wash((26, 8.5), eyes="closed"),
                 pose_wash((25, 10), eyes="closed"), pose_wash((26, 7), eyes="closed"),
                 pose_wash((24.5, 17), eyes="half")], fps=5, tfps=3, intro=1, outro=1)

    # slapen: opgerold of als brood
    zl = lambda i, k, x0, y0: (round(x0 + 0.7 * ((i + 2 * k) % 6)), round(y0 - 2.4 * ((i + 2 * k) % 6)))
    breath = [0, 0, 1, 1, 1, 0]
    add("sleep_loaf", [pose_loaf(breath[i], [zl(i, k, 33, 16) for k in range(3)]) for i in range(6)], fps=1.5)
    curl_in = [pose_curl(0.4, eyes="half"), pose_curl(0.8)]
    add("sleep_curl", curl_in + [pose_curl(1.0, breath[i], [zl(i, k, 29, 17) for k in range(3)]) for i in range(4)]
        + curl_in[::-1], fps=1.5, tfps=1.5, intro=2, outro=2)

    # eten
    add("eat", [pose_eat((27, 14), eyes="open", crumbs=[]), pose_eat((27, 17), eyes="open", mouth="open"),
                pose_eat((26, 20), eyes="open", mouth="open", sway=0.5),
                pose_eat((26, 21), eyes="half", mouth="closed", sway=-0.5, crumbs=[(37, 22), (38, 19)]),
                pose_eat((26, 20), eyes="half", mouth="open", ear=1, sway=0.5),
                pose_eat((26, 21), eyes="half", mouth="closed", sway=-0.5, crumbs=[(36, 20), (38, 23)]),
                pose_eat((26, 13), eyes="happy", tongue=True),
                pose_stand(eyes="happy", mouth="smile", tail_kind="up")],
        fps=4, tfps=2.5, intro=2, outro=2, sfx={3: ("crunch", 1.0), 5: ("crunch", 1.0)})

    # rug-stretch op de vloer (kort, met opbouw: voorover -> boog -> bolle rug)
    A["stretch_back"] = dict(frames=[
        pose_stand(eyes="half", tail_kind="up"),
        pose_bow((12, 19), (22.5, 21.5), (26, 15), 26, 23.5, 10, 13, arch=0.0, eyes="half"),
        pose_bow((11.5, 17.5), (21.5, 23.5), (24.5, 18), 29.5, 26.5, 10, 13, arch=-0.8, eyes="closed"),
        pose_bow((11.5, 17), (21, 24.3), (24, 19.5), 30, 27, 10, 13, arch=-1.2, eyes="yawn",
                 mouth="yawn", sway=0.4),
        pose_bow((12, 19.5), (22, 20.3), (24.5, 15), 23, 21, 11.5, 14, arch=3.4, tail_kind="low", eyes="closed"),
        pose_bow((12, 20.3), (22.5, 20.5), (25, 16), 22.5, 20.5, 12, 14.5, arch=5.0, tail_kind="low",
                 eyes="closed", sway=0.3, r=3.3),
        pose_stand(eyes="half", tail_kind="mid"),
        pose_stand(eyes="open", tail_kind="up"),
    ], fps=3, tfps=3, intro=0, outro=0, durs=[0.3, 0.4, 0.55, 0.7, 0.35, 0.7, 0.3, 0.25],
        sfx={3: ("mew", 0.35)})

    # rekken tegen de muur (de muur = rechterrand van het frame)
    A["stretch_wall"] = dict(frames=[
        pose_wall((13, 21), (24.5, 17.5), 1.0, (36.5, 15), (26, 10), r=3.2),
        pose_wall((14, 22), (25.5, 16.5), 2.8, (37.5, 9), (27, 9), r=3.1, eyes="half"),
        pose_wall((14.5, 22.5), (26, 16.2), 4.4, (37.8, 7), (27.5, 8), r=3.0, eyes="closed"),
        pose_wall((15, 23), (26.5, 16), -1.6, (38, 5), (28, 8), r=2.8, eyes="happy", sway=0.5),
        pose_wall((15, 23), (27, 15.5), -2.4, (38.2, 3.5), (28, 8), r=2.7, eyes="yawn", mouth="yawn", sway=-0.5),
        pose_wall((14.5, 22.5), (26, 17), 3.4, (37, 10), (27, 10), r=3.3, eyes="closed"),
        pose_wall((14, 22), (25, 17.5), 2.0, (36.5, 14), (26, 10.5), r=3.3, eyes="half"),
        pose_wall((13, 21.5), (24.5, 18), 1.0, (36.5, 17), (26, 11), r=3.3, eyes="half"),
    ], fps=1, tfps=1, intro=0, outro=0, durs=[0.45, 0.45, 0.4, 0.5, 1.0, 0.5, 0.4, 0.35],
        sfx={4: ("mew", 0.7)})

    # spinnen (klik)
    purr = [pose_sit(sway=sw6[i] * 0.6, eyes="happy", mouth="smile", hdy=i % 2,
                     hearts=[(29, 12 - 3 * i), (33, 12 - 3 * ((i + 3) % 6))]) for i in range(6)]
    add("purr", purr, fps=8)
    for n in ("stretch_back", "stretch_wall"):
        A[n].setdefault("tfps", 3)
        A[n].setdefault("intro", 0)
        A[n].setdefault("outro", 0)
    return A


# --------------------------------------------------------------------------- #
#  Houdingen, tussenanimaties en toestanden
# --------------------------------------------------------------------------- #

STATE_POSTURE = {
    "walk": "stand", "hop": "stand", "idle": "stand", "eat": "stand",
    "stretch_wall": "stand", "stretch_back": "stand",
    "sit": "sit", "lick_paw": "sit", "lick_tail": "sit", "wash": "sit", "purr": "sit",
    "sleep_loaf": "lie", "sleep_curl": "lie",
}
TRANS = {"t_stand_sit": ("stand", "sit"), "t_sit_stand": ("sit", "stand"),
         "t_sit_lie": ("sit", "lie"), "t_lie_sit": ("lie", "sit")}
REST = {"idle", "sit"}
COOL_KEY = {"lick_paw": "lick", "lick_tail": "lick", "wash": "lick",
            "sleep_loaf": "sleep", "sleep_curl": "sleep"}
SOUND_COOLDOWN = {"purr": 0.4, "mew": 0.4, "crunch": 0.12}


def route(a, b):
    """Kortste rij tussenanimaties om van houding a naar b te komen (geen teleporten)."""
    if a == b:
        return []
    seen, queue = {a: []}, [a]
    while queue:
        cur = queue.pop(0)
        for name, (s, e) in TRANS.items():
            if s == cur and e not in seen:
                seen[e] = seen[cur] + [name]
                queue.append(e)
    return seen.get(b, [])


# --------------------------------------------------------------------------- #
#  Animaties -> cairo (ingebouwd of eigen PNG's)
# --------------------------------------------------------------------------- #

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


def norm_sfx(d):
    out = {}
    for k, v in (d or {}).items():
        out[int(k)] = (v, 1.0) if isinstance(v, str) else (v[0], float(v[1]))
    return out


class Anim:
    def __init__(self, maker, scale, fps, tfps, intro, outro, durs, sfx):
        self.maker = maker
        self.frames = maker(scale)
        self.fps, self.tfps, self.intro, self.outro = fps, tfps, intro, outro
        self.durs, self.sfx = durs, norm_sfx(sfx)
        self.total = sum(durs) if durs else 0.0

    def set_scale(self, scale):
        self.frames = self.maker(scale)


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


def make_coats(n, names):
    pool = list(COATS)
    if names:
        pool = []
        for nm in names:
            m = [c for c in COATS if c.name == nm.strip().lower()]
            if not m:
                sys.exit(f"onbekende vacht '{nm}'. Kies uit: {', '.join(c.name for c in COATS)}")
            pool.append(m[0])
    return [pool[i % len(pool)] if i < len(pool) else coat_variant(pool[i % len(pool)], i)
            for i in range(n)]


def find_folder(root, i, coat):
    """Eigen sprites per kat: sprites/cat1, sprites/cat2, ... (of sprites/<vachtnaam>, sprites/default)."""
    for cand in (root / f"cat{i + 1}", root / coat.name, root / "default"):
        if cand.is_dir():
            return cand
    return None


def build_cat(coat, scale, folder):
    set_coat(coat)
    anims = {}
    for name, s in build_generated().items():
        canv, pal = s["frames"], coat.pal
        anims[name] = Anim(lambda sc, canv=canv, pal=pal: [Frame(canvas_surface(c, sc, pal)) for c in canv],
                           scale, s["fps"], s["tfps"], s["intro"], s["outro"], s["durs"], s["sfx"])
    cfg = {}
    if folder:
        if (folder / "config.json").exists():
            cfg = json.loads((folder / "config.json").read_text())
        import cairo
        for name, a in list(anims.items()):
            png = folder / f"{name}.png"
            if not png.exists():
                continue
            per = cfg.get("anims", {}).get(name, {})
            strip = cairo.ImageSurface.create_from_png(str(png))
            fw = per.get("frame_width", cfg.get("frame_width", strip.get_height()))
            n = max(1, strip.get_width() // fw)
            durs = per.get("durs", a.durs if (a.durs and len(a.durs) == n) else
                           ([a.total / n] * n if a.durs else None))
            sfx = per.get("sfx", {k: v for k, v in a.sfx.items()} if n == len(a.frames) else {})
            anims[name] = Anim(lambda sc, p=png, f=fw: load_strip(p, f, sc), scale,
                               per.get("fps", a.fps), per.get("tfps", a.tfps),
                               per.get("intro", a.intro if n == len(a.frames) else 0),
                               per.get("outro", a.outro if n == len(a.frames) else 0), durs, sfx)
        print(f"[catpet] eigen sprites uit {folder}")
    return anims, cfg


README = """EIGEN SPRITES PER KAT
=====================
Elke kat leest zijn sprites uit een eigen map naast catpet.py:

    sprites/cat1/   -> eerste kat
    sprites/cat2/   -> tweede kat
    sprites/cat3/   -> ...
    sprites/default/-> wordt gebruikt door katten zonder eigen map

Maak de mappen met:   python catpet.py --cats 3 --dump-sprites
Dat schrijft per kat alle animaties als PNG-strips (frames naast elkaar, 40x33 px per frame)
plus een config.json. Bewerk de PNG's in Aseprite / Piskel / GIMP en start catpet opnieuw.
Een PNG die ontbreekt valt terug op de ingebouwde animatie, dus je kunt ook maar een deel vervangen.
Sprites moeten naar RECHTS kijken (links wordt automatisch gespiegeld); bij 'stretch_wall' zit
de muur aan de rechterkant van het frame.

config.json per map:
  frame_width   breedte van een frame (standaard 40)
  mouth_x       x-positie van de mond in 'eat' (bepaalt waar de kat bij de bak staat)
  anims.<naam>  fps, tfps, intro, outro, durs (tijd per frame, speelt 1x), sfx ({"3": ["mew", 0.7]})

Animaties: walk hop idle sit lick_paw lick_tail wash sleep_loaf sleep_curl eat
           stretch_back stretch_wall purr  +  t_stand_sit t_sit_stand t_sit_lie t_lie_sit
Voerbak: sprites/bowl.png (4 frames: vol .. leeg) + bowl.json {"frame_width": 16}
Eigen geluid: sounds/purr.wav, mew.wav, crunch.wav naast catpet.py.
"""


def dump_sprites(root, coats):
    import cairo
    root = Path(root)
    for i, coat in enumerate(coats):
        folder = root / f"cat{i + 1}"
        folder.mkdir(parents=True, exist_ok=True)
        set_coat(coat)
        cfg = {"coat": coat.name, "frame_width": W, "mouth_x": MOUTH_X, "anims": {}}
        for name, s in build_generated().items():
            strip = cairo.ImageSurface(cairo.FORMAT_ARGB32, W * len(s["frames"]), H)
            ctx = cairo.Context(strip)
            for k, cv in enumerate(s["frames"]):
                ctx.set_source_surface(canvas_surface(cv, 1, coat.pal), k * W, 0)
                ctx.paint()
            strip.write_to_png(str(folder / f"{name}.png"))
            cfg["anims"][name] = {"fps": s["fps"], "tfps": s["tfps"], "intro": s["intro"],
                                  "outro": s["outro"], "durs": s["durs"],
                                  "sfx": {str(k): list(v) if isinstance(v, tuple) else v
                                          for k, v in s["sfx"].items()}}
        (folder / "config.json").write_text(json.dumps(cfg, indent=2))
    strip = cairo.ImageSurface(cairo.FORMAT_ARGB32, BOWL_W * 4, BOWL_H)
    ctx = cairo.Context(strip)
    for lv in range(4):
        ctx.set_source_surface(canvas_surface(bowl_canvas(lv), 1, BOWL_PAL), lv * BOWL_W, 0)
        ctx.paint()
    strip.write_to_png(str(root / "bowl.png"))
    (root / "bowl.json").write_text(json.dumps({"frame_width": BOWL_W}, indent=2))
    (root / "README.txt").write_text(README)
    print(f"[catpet] sprite-mappen geschreven naar {root}/ (cat1 .. cat{len(coats)}), zie README.txt")


# --------------------------------------------------------------------------- #
#  Geluid (procedureel gegenereerd, of eigen .wav in sounds/)
# --------------------------------------------------------------------------- #

def _write_wav(path, samples, sr=22050):
    peak = max(1e-9, max(abs(s) for s in samples))
    data = b"".join(struct.pack("<h", int(max(-1, min(1, 0.9 * s / peak)) * 32767)) for s in samples)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(data)


def synth_purr(path, pitch=1.0, dur=1.8, sr=22050):
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
        out.append(s * env)
    _write_wav(path, out, sr)


def synth_mew(path, pitch=1.0, dur=0.45, sr=22050):
    n, phase, out = int(sr * dur), 0.0, []
    for i in range(n):
        t = i / n
        f = pitch * (520 + 380 * math.sin(math.pi * t) ** 0.8 - 120 * t)
        phase += 2 * math.pi * f / sr
        s = (math.sin(phase) + 0.5 * math.sin(2 * phase) + 0.35 * math.sin(3 * phase)
             + 0.2 * math.sin(4 * phase)) * (0.6 + 0.4 * math.sin(math.pi * t))
        out.append(s * min(1, (i / sr) / 0.03) * (1 - t) ** 0.7)
    _write_wav(path, out, sr)


def synth_crunch(path, pitch=1.0, dur=0.16, sr=22050):
    rnd = random.Random(5)
    lp, out = 0.0, []
    for i in range(int(sr * dur)):
        t = i / sr
        n = rnd.uniform(-1, 1)
        lp += (0.45 / pitch) * (n - lp)
        crack = n - lp
        env = math.exp(-t * 60) + 0.7 * (math.exp(-(t - 0.07) * 80) if t > 0.07 else 0)
        out.append(crack * env)
    _write_wav(path, out, sr)


class Sfx:
    def __init__(self, enabled, volume, pitches, custom_dir):
        self.enabled, self.volume = enabled, volume
        self.custom = Path(custom_dir)
        self.cache = Path.home() / ".cache" / "catpet"
        self.proc, self.last, self.files, self.player = {}, {}, {}, None
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
        for i, p in enumerate(pitches):
            for name, fn in (("purr", synth_purr), ("mew", synth_mew), ("crunch", synth_crunch)):
                f = self.cache / f"{name}_{int(p * 100)}.wav"
                if not f.exists():
                    fn(f, p)
                self.files[(name, i)] = f

    def _scaled(self, path):
        """Pas het volume toe op een kopie van de wav (gecachet)."""
        if path.suffix != ".wav" or self.volume >= 0.99:
            return path
        out = self.cache / f"{path.stem}_v{int(self.volume * 100)}_{abs(hash(str(path))) % 10000}.wav"
        if not out.exists():
            with wave.open(str(path)) as f:
                params, raw = f.getparams(), f.readframes(f.getnframes())
            n = len(raw) // 2
            vals = struct.unpack(f"<{n}h", raw)
            with wave.open(str(out), "wb") as g:
                g.setparams(params)
                g.writeframes(struct.pack(f"<{n}h", *[int(v * self.volume) for v in vals]))
        return out

    def play(self, name, idx):
        if not self.enabled or self.volume <= 0:
            return
        now = time.monotonic()
        if now - self.last.get(name, 0) < SOUND_COOLDOWN.get(name, 0.3):
            return
        p = self.proc.get(name)
        if name != "crunch" and p is not None and p.poll() is None:
            return
        f = None
        for ext in ("wav", "ogg", "mp3"):
            if (self.custom / f"{name}.{ext}").exists():
                f = self.custom / f"{name}.{ext}"
                break
        f = f or self.files.get((name, idx))
        if f:
            self.last[name] = now
            self.proc[name] = subprocess.Popen(
                self.player + [str(self._scaled(f))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- #
#  Voerbak
# --------------------------------------------------------------------------- #

class Bowl:
    def __init__(self, sw, sh, floor, scale, frac, root):
        self.sw, self.sh, self.floor = sw, sh, floor
        self.cx = frac * sw
        self.level, self.timer, self.refill_after, self.occupied = 0, 0.0, random.uniform(60, 120), None
        self.root = Path(root)
        self.set_scale(scale)

    def set_scale(self, scale):
        self.scale = scale
        png = self.root / "bowl.png"
        if png.exists():
            fw = BOWL_W
            if (self.root / "bowl.json").exists():
                fw = json.loads((self.root / "bowl.json").read_text()).get("frame_width", BOWL_W)
            self.frames = load_strip(png, fw, scale)
            while len(self.frames) < 4:
                self.frames.append(self.frames[-1])
        else:
            self.frames = [Frame(canvas_surface(bowl_canvas(lv), scale, BOWL_PAL)) for lv in range(4)]

    def rect(self):
        f = self.frames[self.level]
        return int(self.cx - f.w / 2), self.sh - self.floor - f.h, f.w, f.h

    def consume(self):
        self.level = min(3, self.level + 1)

    def refill(self):
        self.level, self.timer = 0, 0.0
        self.refill_after = random.uniform(60, 120)

    def update(self, dt):
        if self.level >= 3 and self.occupied is None:
            self.timer += dt
            if self.timer > self.refill_after:
                self.refill()

    def draw(self, ctx):
        x, y, _w, _h = self.rect()
        ctx.set_source_surface(self.frames[self.level].surf, x, y)
        ctx.paint()


# --------------------------------------------------------------------------- #
#  Gedrag (per kat)
# --------------------------------------------------------------------------- #

class Pet:
    def __init__(self, index, coat, anims, cfg, sw, sh, floor, scale, speed, calm, bowl):
        self.index, self.coat, self.anims, self.cfg = index, coat, anims, cfg
        self.sw, self.sh, self.floor, self.bowl = sw, sh, floor, bowl
        self.scale, self.calm = scale, calm * random.uniform(0.85, 1.2)
        self.mouth_x = cfg.get("mouth_x", MOUTH_X)
        self.speed_mult = speed * random.uniform(0.9, 1.1)
        self.px_speed = 18 * scale * self.speed_mult
        self.dir = random.choice((-1, 1))
        self.posture, self.queue = "stand", []
        self.state, self.t, self.dur = "idle", 0.0, random.uniform(1, 5)
        self.target, self.goal, self.eat_dir, self.eat_acc = 0.0, "wander", 1, 0.0
        self.last, self.events, self._idx = {}, [], -1
        self.x = random.uniform(0, max(1, sw - anims["walk"].frames[0].w))

    # -- instellingen (menu)
    def set_speed(self, v):
        self.speed_mult = v
        self.px_speed = 18 * self.scale * v

    def set_scale(self, scale):
        self.scale = scale
        for a in self.anims.values():
            a.set_scale(scale)
        self.px_speed = 18 * scale * self.speed_mult
        self.x = min(self.x, max(0, self.sw - self.anims["walk"].frames[0].w))

    # -- frame-keuze
    def frame(self):
        a = self.anims[self.state]
        n = len(a.frames)
        if a.durs:
            acc = 0.0
            for i, d in enumerate(a.durs[:n]):
                acc += d
                if self.t < acc:
                    return a.frames[i], i
            return a.frames[n - 1], n - 1
        fps = a.fps * (self.speed_mult if self.state in ("walk", "hop") else 1)
        loop_n = max(1, n - a.intro - a.outro)
        intro_t, outro_t = a.intro / a.tfps, a.outro / a.tfps
        if a.intro and self.t < intro_t:
            i = int(self.t * a.tfps)
        elif a.outro and self.state not in ("walk", "hop") and self.t >= self.dur - outro_t:
            i = n - a.outro + min(int((self.t - (self.dur - outro_t)) * a.tfps), a.outro - 1)
        else:
            i = a.intro + int((self.t - intro_t) * fps) % loop_n
        return a.frames[min(i, n - 1)], min(i, n - 1)

    def rect(self):
        f, _ = self.frame()
        return int(self.x), self.sh - self.floor - f.h, f.w, f.h

    def signature(self):
        return (self.state, self.frame()[1], int(self.x), self.dir, self.scale)

    def hit(self, px, py):
        f, _ = self.frame()
        x, y, w, h = self.rect()
        return x <= px < x + w and y <= py < y + h and f.alpha(int(px) - x, int(py) - y, self.dir < 0) > 0

    # -- toestanden en tussenanimaties
    def set_state(self, name, loop_time=0.0):
        a = self.anims[name]
        self.state, self.t, self._idx = name, 0.0, -1
        self.dur = a.total if a.durs else loop_time + (a.intro + a.outro) / a.tfps
        if name not in REST and name not in TRANS and name not in ("walk", "hop"):
            self.last[COOL_KEY.get(name, name)] = time.monotonic()
        if name != "eat":
            self.eat_acc = 0.0

    def begin(self, state, loop_time=0.0):
        """Start `state`; als de houding niet past eerst via tussenanimaties (zitten, liggen, ...)."""
        if self.bowl and state != "eat" and self.bowl.occupied is self and self.goal != "bowl":
            self.bowl.occupied = None
        if state not in ("walk", "hop"):
            self.goal = "wander"
        start = TRANS[self.state][1] if self.state in TRANS else self.posture
        self.queue = [(t, 0.0) for t in route(start, STATE_POSTURE[state])] + [(state, loop_time)]
        if self.state not in TRANS:
            self._advance()

    def _advance(self):
        name, lt = self.queue.pop(0)
        self.set_state(name, lt)

    def end_state(self):
        cur = self.state
        self.posture = TRANS[cur][1] if cur in TRANS else STATE_POSTURE.get(cur, self.posture)
        if cur == "eat" and self.bowl and self.bowl.occupied is self:
            self.bowl.occupied = None
        if self.queue:
            self._advance()
        else:
            self.choose_next()

    def ready(self, name, cooldown):
        return time.monotonic() - self.last.get(name, -1e9) > cooldown * self.calm

    def go(self, target, goal="wander", variant=None):
        self.target, self.goal = target, goal
        if abs(target - self.x) > 1:
            self.dir = 1 if target > self.x else -1
        self.begin(variant or ("hop" if random.random() < 0.25 else "walk"))
        self.goal = goal

    def eat_spot(self):
        wa = self.anims["walk"].frames[0].w
        s = self.scale
        cx = self.bowl.cx
        xa = cx - self.mouth_x * s                       # links van de bak, kijkt naar rechts
        xb = cx - (wa / s - self.mouth_x) * s            # rechts van de bak, gespiegeld
        cand = [(abs(xa - self.x), xa, 1), (abs(xb - self.x), xb, -1)]
        _d, x, d = min(cand)
        return max(0.0, min(x, self.sw - wa)), d

    def go_to_bowl(self):
        if not self.bowl:
            return
        if self.bowl.level >= 3:
            self.bowl.refill()
        x, d = self.eat_spot()
        self.eat_dir = d
        self.go(x, "bowl")
        self.bowl.occupied = self

    def choose_next(self):
        if self.state not in REST:  # na een activiteit eerst even rust
            kind = random.choices(("sit", "idle"), (60, 40))[0]
            return self.begin(kind, random.uniform(5, 12) * self.calm)
        fw = self.anims["walk"].frames[0].w
        opts = {"walk": 40, "sit": 10, "idle": 6}
        if self.ready("stretch_back", 18):
            opts["stretch_back"] = 26
        if self.ready("stretch_wall", 75):
            opts["wall"] = 9
        if self.ready("lick", 28):
            opts["lick"] = 35 if self.state == "sit" else 14
        if self.ready("sleep", 150):
            opts["sleep"] = 10
        b = self.bowl
        if b and b.level < 3 and b.occupied is None and self.ready("eat", 100):
            opts["eat"] = 20
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
        elif choice == "stretch_back":
            self.begin("stretch_back")
        elif choice == "lick":
            kind = random.choice(("lick_paw", "lick_tail", "wash"))
            self.begin(kind, random.uniform(3, 5) if kind == "wash" else random.uniform(6, 10))
        elif choice == "sleep":
            self.begin(random.choice(("sleep_loaf", "sleep_curl")), random.uniform(20, 50) * self.calm)
        elif choice == "eat":
            self.go_to_bowl()
        elif choice == "sit":
            self.begin("sit", random.uniform(6, 14) * self.calm)
        else:
            self.begin("idle", random.uniform(4, 9) * self.calm)

    def arrive(self):
        self.x = self.target
        goal, self.goal = self.goal, "wander"
        self.posture = "stand"
        if goal == "wall":
            w = self.anims["stretch_wall"].frames[0].w
            if self.target <= 0:
                self.dir, self.x = -1, 0.0
            else:
                self.dir, self.x = 1, float(self.sw - w)
            self.begin("stretch_wall")
        elif goal == "bowl" and self.bowl:
            self.dir = self.eat_dir
            self.begin("eat", random.uniform(7, 11))
            self.bowl.occupied = self
        else:
            self.end_state()

    def on_click(self):
        self.events.append(("purr", 1.0))
        if self.state == "purr":
            self.dur += 2.5
        else:
            self.begin("purr", random.uniform(5, 8))

    def update(self, dt):
        self.t += dt
        if self.state in ("walk", "hop"):
            step = self.px_speed * dt
            d = self.target - self.x
            if abs(d) <= step:
                self.arrive()
            else:
                self.dir = 1 if d > 0 else -1
                self.x += step * self.dir
        elif self.t >= self.dur:
            self.end_state()
        if self.state == "eat" and self.bowl:  # de bak raakt langzaam leeg
            a = self.anims["eat"]
            outro_t = a.outro / a.tfps
            if a.intro / a.tfps < self.t < self.dur - outro_t:
                self.eat_acc += dt
                if self.eat_acc > 3.0:
                    self.eat_acc = 0.0
                    self.bowl.consume()
                if self.bowl.level >= 3:
                    self.dur = min(self.dur, self.t + outro_t)
        _f, idx = self.frame()
        if idx != self._idx:
            self._idx = idx
            snd = self.anims[self.state].sfx.get(idx)
            if snd and random.random() < snd[1]:
                self.events.append(snd)

    def draw(self, ctx):
        f, _ = self.frame()
        x, y, _w, _h = self.rect()
        ctx.set_source_surface(f.flipped() if self.dir < 0 else f.surf, x, y)
        ctx.paint()


# --------------------------------------------------------------------------- #
#  Menu (zelf getekend, werkt overal op Wayland)
# --------------------------------------------------------------------------- #

class Menu:
    PAD, WIDTH = 14, 300

    def __init__(self, app):
        self.app, self.open, self.pet, self.drag = app, False, None, None
        self.items, self.rect, self.last, self.pend_size, self.pend_bowl = [], (0, 0, 0, 0), 0.0, None, None

    def show(self, pet):
        app, ui = self.app, self.app.ui
        self.pet, self.open, self.last = pet, True, time.monotonic()
        self.pend_size = self.pend_bowl = None
        pw, pad = self.WIDTH * ui, self.PAD * ui
        items, y = [], 0.0
        items.append(dict(kind="title", rect=(0, y, pw, 38 * ui)))
        y += 38 * ui
        sliders = [
            ("Snelheid", lambda: pet.speed_mult, lambda v: pet.set_speed(v), None, 0.3, 2.5, 0.05, "{:.2f}x"),
            ("Grootte", lambda: self.pend_size or pet.scale, lambda v: setattr(self, "pend_size", int(round(v))),
             self._apply_size, 1, 8, 1, "{:.0f}"),
            ("Rust", lambda: pet.calm, lambda v: setattr(pet, "calm", v), None, 0.3, 3.0, 0.1, "{:.1f}"),
            ("Volume", lambda: app.sfx.volume, lambda v: setattr(app.sfx, "volume", v), self._preview_sound,
             0.0, 1.0, 0.05, "{:.0%}"),
        ]
        if app.bowl:
            sliders.append(("Voerbak", lambda: self.pend_bowl or app.bowl.scale,
                            lambda v: setattr(self, "pend_bowl", int(round(v))), self._apply_bowl,
                            1, 8, 1, "{:.0f}"))
        for label, get, set_, done, lo, hi, step, fmt in sliders:
            items.append(dict(kind="slider", label=label, get=get, set=set_, done=done, lo=lo, hi=hi,
                              step=step, fmt=fmt, rect=(0, y, pw, 44 * ui)))
            y += 44 * ui
        buttons = [("Aaien", lambda: pet.on_click()),
                   ("Eten", lambda: pet.go_to_bowl() if app.bowl else None),
                   ("Rekken", lambda: pet.begin("stretch_back")),
                   ("Slapen", lambda: pet.begin(random.choice(("sleep_loaf", "sleep_curl")), 25 * pet.calm)),
                   ("Likken", lambda: pet.begin(random.choice(("lick_paw", "lick_tail", "wash")), 7)),
                   ("Hupsen", lambda: pet.go(random.uniform(0, max(1, pet.sw - 200)), variant="hop")),
                   ("Bak vullen", lambda: app.bowl and app.bowl.refill()),
                   ("Geluid aan/uit", self._toggle_sound)]
        bw = (pw - 2 * pad - 2 * 6 * ui) / 3
        for i, (label, fn) in enumerate(buttons):
            row, col = divmod(i, 3)
            if i == 6:
                col, wide = 0, 1
            bx = pad + col * (bw + 6 * ui)
            by = y + 6 * ui + row * 36 * ui
            width = bw * (1.5 if i >= 6 else 1) + (3 * ui if i >= 6 else 0)
            if i == 7:
                bx = pad + bw * 1.5 + 9 * ui
            items.append(dict(kind="btn", label=label, fn=fn, rect=(bx, by, width, 30 * ui)))
        y += 6 * ui + 3 * 36 * ui + 6 * ui
        ph = y
        c = pet.rect()
        px = max(8, min(c[0] + c[2] / 2 - pw / 2, pet.sw - pw - 8))
        py = c[1] - ph - 10
        if py < 8:
            py = min(pet.sh - ph - 8, c[1] + c[3] + 10)
        self.rect = (px, py, pw, ph)
        self.items = items

    # -- acties
    def _apply_size(self):
        if self.pend_size and self.pet and self.pend_size != self.pet.scale:
            self.pet.set_scale(self.pend_size)
        self.pend_size = None

    def _apply_bowl(self):
        if self.pend_bowl and self.app.bowl and self.pend_bowl != self.app.bowl.scale:
            self.app.bowl.set_scale(self.pend_bowl)
        self.pend_bowl = None

    def _preview_sound(self):
        self.app.sfx.play("purr", self.pet.index)

    def _toggle_sound(self):
        sfx = self.app.sfx
        if sfx.player:
            sfx.enabled = not sfx.enabled

    def close(self):
        self._apply_size()
        self._apply_bowl()
        self.open, self.drag, self.pet = False, None, None

    # -- muis
    def _abs(self, r):
        return (self.rect[0] + r[0], self.rect[1] + r[1], r[2], r[3])

    def _set_from_x(self, it, px):
        ui, pad = self.app.ui, self.PAD * self.app.ui
        x0 = self.rect[0] + pad
        frac = max(0.0, min(1.0, (px - x0) / (self.rect[2] - 2 * pad)))
        v = it["lo"] + frac * (it["hi"] - it["lo"])
        v = round((v - it["lo"]) / it["step"]) * it["step"] + it["lo"]
        it["set"](max(it["lo"], min(it["hi"], v)))

    def press(self, px, py):
        """True = klik viel in het menu (verwerkt). False = erbuiten (menu sluit)."""
        if not self.open:
            return False
        self.last = time.monotonic()
        x, y, w, h = self.rect
        if not (x <= px <= x + w and y <= py <= y + h):
            self.close()
            return False
        for it in self.items:
            ix, iy, iw, ih = self._abs(it["rect"])
            if ix <= px <= ix + iw and iy <= py <= iy + ih:
                if it["kind"] == "title":
                    if px > ix + iw - 40 * self.app.ui:
                        self.close()
                elif it["kind"] == "slider":
                    self.drag = it
                    self._set_from_x(it, px)
                elif it["kind"] == "btn":
                    it["fn"]()
                break
        return True

    def motion(self, px, py):
        if self.drag:
            self.last = time.monotonic()
            self._set_from_x(self.drag, px)

    def release(self):
        if self.drag:
            if self.drag["done"]:
                self.drag["done"]()
            self.drag = None

    # -- tekenen
    def draw(self, ctx):
        if not self.open:
            return
        import cairo
        ui, pad = self.app.ui, self.PAD * self.app.ui
        x, y, w, h = self.rect
        r = 10 * ui
        ctx.new_sub_path()
        for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
            ctx.arc(cx, cy, r, math.radians(a0), math.radians(a0 + 90))
        ctx.close_path()
        ctx.set_source_rgba(0.11, 0.10, 0.14, 0.97)
        ctx.fill_preserve()
        ctx.set_source_rgb(0.93, 0.58, 0.22)
        ctx.set_line_width(2 * ui)
        ctx.stroke()
        ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(13 * ui)

        def text(s, tx, ty, col=(0.96, 0.93, 0.88), align="l"):
            ext = ctx.text_extents(s)
            ctx.set_source_rgb(*col)
            ctx.move_to(tx - (ext.width if align == "r" else ext.width / 2 if align == "c" else 0) - ext.x_bearing * 0, ty)
            ctx.show_text(s)

        for it in self.items:
            ix, iy, iw, ih = self._abs(it["rect"])
            if it["kind"] == "title":
                name = self.pet.coat.name.capitalize() if self.pet else ""
                text(f"{name}  (kat {self.pet.index + 1})", x + pad, iy + 24 * ui, (0.93, 0.58, 0.22))
                text("x", x + w - pad - 6 * ui, iy + 24 * ui, align="c")
            elif it["kind"] == "slider":
                v = it["get"]()
                text(it["label"], x + pad, iy + 15 * ui)
                text(it["fmt"].format(v), x + w - pad, iy + 15 * ui, (0.93, 0.58, 0.22), "r")
                tx, ty, tw = x + pad, iy + 26 * ui, w - 2 * pad
                ctx.set_source_rgb(0.26, 0.25, 0.32)
                ctx.rectangle(tx, ty, tw, 6 * ui)
                ctx.fill()
                frac = (v - it["lo"]) / (it["hi"] - it["lo"])
                ctx.set_source_rgb(0.93, 0.58, 0.22)
                ctx.rectangle(tx, ty, tw * frac, 6 * ui)
                ctx.fill()
                ctx.set_source_rgb(0.98, 0.9, 0.76)
                ctx.arc(tx + tw * frac, ty + 3 * ui, 7 * ui, 0, 2 * math.pi)
                ctx.fill()
            elif it["kind"] == "btn":
                ctx.set_source_rgb(0.22, 0.20, 0.28)
                ctx.rectangle(ix, iy, iw, ih)
                ctx.fill()
                label = it["label"]
                if label.startswith("Geluid"):
                    label = "Geluid: " + ("aan" if self.app.sfx.enabled else "uit")
                ctx.set_font_size(12 * ui)
                text(label, ix + iw / 2, iy + ih / 2 + 4 * ui, align="c")
                ctx.set_font_size(13 * ui)


# --------------------------------------------------------------------------- #
#  Applicatie (GTK / layer-shell)
# --------------------------------------------------------------------------- #

class App:
    pass


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
    sw, sh = geo.width, geo.height
    root = Path(args.sprites)

    app = App()
    app.ui = 1.0 if sh <= 1200 else 1.5 if sh <= 1800 else 2.0
    scale = args.scale or 3
    app.bowl = None if args.no_bowl else Bowl(
        sw, sh, args.floor, scale, args.bowl_x or random.uniform(0.25, 0.75), root)

    coats = make_coats(args.cats, args.coats.split(",") if args.coats else None)
    pets = []
    for i, coat in enumerate(coats):
        anims, cfg = build_cat(coat, scale, find_folder(root, i, coat))
        pets.append(Pet(i, coat, anims, cfg, sw, sh, args.floor, args.scale or cfg.get("scale", 3),
                        args.speed, args.calm, app.bowl))
    print("[catpet] katten: " + ", ".join(c.name for c in coats))
    pitches = [1.0] + [round(random.uniform(0.8, 1.3), 2) for _ in coats[1:]]
    app.sfx = Sfx(not args.no_sfx, args.volume, pitches, Path(args.sounds))
    app.pets = pets
    menu = Menu(app)

    win = Gtk.Window()
    win.set_title("catpet")
    win.set_app_paintable(True)
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)
    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                   | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_MOTION_MASK)

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
        eaters = [p for p in pets if p.state == "eat"]
        for p in eaters:          # de etende kat eerst: de bak komt dan voor zijn snuit
            p.draw(ctx)
        if app.bowl:
            app.bowl.draw(ctx)
        for p in sorted((p for p in pets if p not in eaters), key=lambda p: p.state == "purr"):
            p.draw(ctx)
        menu.draw(ctx)
        return False

    def update_input_region():
        if menu.open:  # volledig scherm, zodat een klik ernaast het menu sluit
            win.input_shape_combine_region(cairo.Region(cairo.RectangleInt(0, 0, sw, sh)))
            return
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

    def on_press(_w, event):
        if event.button not in (1, 3):
            return True

        # Menu staat open: beide muisknoppen kunnen het sluiten
        # of een menu-optie aanklikken.
        if menu.open:
            if menu.press(event.x, event.y):
                win.queue_draw()
                return True

            menu.close()
            update_input_region()
            win.queue_draw()
            return True

        # Alleen rechtermuisknop opent het menu.
        if event.button == 3:
            for p in reversed(pets):
                if p.hit(event.x, event.y):
                    menu.show(p)
                    break

        # Linkermuisknop blijft de kat aaien (purr).
        elif event.button == 1:
            for p in reversed(pets):
                if p.hit(event.x, event.y):
                    p.on_click()
                    break

        update_input_region()
        win.queue_draw()
        return True

    def on_motion(_w, event):
        if menu.drag:
            menu.motion(event.x, event.y)
            win.queue_draw()
        return True

    def on_release(_w, event):
        if menu.drag:
            menu.release()
            win.queue_draw()
        return True

    win.connect("draw", on_draw)
    win.connect("button-press-event", on_press)
    win.connect("motion-notify-event", on_motion)
    win.connect("button-release-event", on_release)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()

    st = {"last": time.monotonic(), "sig": None, "menu": False,
          "rects": [p.rect() for p in pets], "bowl": app.bowl.rect() if app.bowl else None}

    def tick():
        now = time.monotonic()
        dt = min(now - st["last"], 0.1)
        st["last"] = now
        if app.bowl:
            app.bowl.update(dt)
        for p in pets:
            p.update(dt)
            while p.events:
                name, _prob = p.events.pop(0)
                app.sfx.play(name, p.index)
        if menu.open and now - menu.last > 25:
            menu.close()
        sig = (tuple(p.signature() for p in pets), app.bowl.level if app.bowl else 0,
               app.bowl.scale if app.bowl else 0, menu.open)
        if sig != st["sig"]:
            st["sig"] = sig
            new = [p.rect() for p in pets]
            if menu.open or st["menu"]:
                win.queue_draw()
            else:
                for (ox, oy, ow, oh), (nx, ny, nw, nh) in zip(st["rects"], new):
                    x0, y0 = min(ox, nx) - 2, min(oy, ny) - 2
                    win.queue_draw_area(x0, y0, max(ox + ow, nx + nw) + 2 - x0, max(oy + oh, ny + nh) + 2 - y0)
            if app.bowl:
                bx, by, bw, bh = st["bowl"]
                nbx, nby, nbw, nbh = app.bowl.rect()
                x0, y0 = min(bx, nbx) - 2, min(by, nby) - 2
                win.queue_draw_area(x0, y0, max(bx + bw, nbx + nbw) + 2 - x0, max(by + bh, nby + nbh) + 2 - y0)
                st["bowl"] = (nbx, nby, nbw, nbh)
            st["rects"], st["menu"] = new, menu.open
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
    ap.add_argument("--coats", help="kies vachten, bv. ginger,tuxedo,calico")
    ap.add_argument("--seed", type=int, default=None, help="vaste random-seed")
    ap.add_argument("--scale", "--size", dest="scale", type=int, default=0,
                    help="grootte van katten EN voerbak (pixelgrootte, standaard 3)")
    ap.add_argument("--floor", type=int, default=0, help="px boven de onderrand (bv. hoogte van je waybar)")
    ap.add_argument("--monitor", type=int, default=0, help="monitor-index")
    ap.add_argument("--speed", type=float, default=1.0, help="loopsnelheid (1.0 = normaal)")
    ap.add_argument("--calm", type=float, default=1.0,
                    help="rust tussen activiteiten: 1.0 = normaal, 2.0 = rustiger, 0.5 = drukker")
    ap.add_argument("--layer", choices=("top", "overlay"), default="top",
                    help="top = onder fullscreen-apps, overlay = altijd erboven")
    ap.add_argument("--no-bowl", action="store_true", help="geen voerbak")
    ap.add_argument("--bowl-x", type=float, default=0, help="positie van de voerbak 0..1 (standaard willekeurig)")
    ap.add_argument("--no-sfx", action="store_true", help="geen geluid")
    ap.add_argument("--volume", type=float, default=0.4, help="geluidsvolume 0..1 (standaard 0.4)")
    ap.add_argument("--sounds", default=str(here / "sounds"), help="map met eigen purr.wav / mew.wav / crunch.wav")
    ap.add_argument("--sprites", default=str(here / "sprites"), help="map met sprite-mappen cat1, cat2, ...")
    ap.add_argument("--dump-sprites", nargs="?", const="", metavar="MAP",
                    help="schrijf per kat een map met sprite-PNG's (cat1..catN) en stop")
    ap.epilog = "vachten: " + ", ".join(c.name for c in COATS)
    args = ap.parse_args()
    args.cats = max(1, args.cats)
    if args.dump_sprites is not None:
        dump_sprites(args.dump_sprites or args.sprites,
                     make_coats(args.cats, args.coats.split(",") if args.coats else None))
        return
    run(args)


if __name__ == "__main__":
    main()
