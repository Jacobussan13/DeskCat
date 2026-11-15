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

Bediening:
    linkermuisknop op een kat   aaien (purr)       - boze katten sissen en slaan
    linkermuisknop slepen       kat optillen en gooien (je kunt andere katten omver gooien)
    rechtermuisknop op een kat  menu (snelheid, grootte, stemming, acties, wereld-instellingen)
    rechtermuisknop op de bak   popup met een zak kattenvoer: sleep hem boven de bak om bij te vullen
Muispositie wordt via Hyprland uitgelezen: de katten jagen, staren, spelen en schrikken van je muis.
Eigen sprites per kat:  python catpet.py --cats 3 --dump-sprites   -> sprites/cat1, cat2, cat3 (zie README.txt)
Stoppen: pkill -f catpet.py
"""
import argparse
import json
import math
import os
import random
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import wave
import zlib
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
                    "Y": _rgb("#b87338"), "y": _rgb("#8c5126"), "T": _rgb("#6fb4ff")}


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


BAG_W, BAG_H = 14, 18
BAG_PAL = {"K": _rgb("#211a24"), "R": _rgb("#d9453a"), "r": _rgb("#a92f2a"), "W": (1, 1, 1),
           "Y": _rgb("#b87338"), "y": _rgb("#8c5126"), "B": _rgb("#4a86d6"), "S": _rgb("#f2c46d")}


def bag_canvas():
    """Open zak kattenvoer, de opening zit bovenaan."""
    c = Canvas(BAG_W, BAG_H, oy=0)
    for x in range(3, 11):                      # brokjes die boven de rand uitsteken
        for y in range(0, 3):
            if (x + y) % 3 != 0 or y == 2:
                c.px(x, y, "y" if (x * 5 + y * 7) % 4 == 0 else "Y")
    for y in range(3, 17):                      # zak
        for x in range(1, 13):
            c.px(x, y, "R" if y > 4 else "r")
    for x in range(1, 13, 2):                   # rafelrand bovenaan
        c.px(x, 3, "r")
    for y in range(7, 14):                      # etiket
        for x in range(3, 11):
            c.px(x, y, "W")
    for x, y in ((4, 10), (5, 9), (6, 9), (7, 9), (8, 10), (7, 11), (6, 11), (5, 11), (9, 9), (9, 11)):
        c.px(x, y, "B")                         # visje
    c.outline()
    return c


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
    elif ear == 2:  # oren plat naar achter (boos / bang / verdrietig)
        left = [(1, -1), (2, -1), (3, -1)]
        right = [(5, -1), (6, -1), (7, -1)]
    for ex, ey in left:
        c.px(x + ex, y + ey, lcol)
    for ex, ey in right:
        c.px(x + ex, y + ey, rcol)
    if ear != 2:
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
        elif eyes == "angry":  # fronsende wenkbrauwen
            c.px(x + ex, y + 4, ec)
            c.px(x + (1 if ex == 2 else 7), y + 2, "K"); c.px(x + (2 if ex == 2 else 6), y + 3, "K")
        elif eyes == "x":      # duizelig
            for dx, dy in ((-1, 3), (1, 3), (0, 4), (-1, 5), (1, 5)):
                c.px(x + ex + dx, y + dy, "K")
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
    elif mouth == "frown":
        c.px(x + 7, y + 6, "K"); c.px(x + 6, y + 7, "K")
    else:
        c.px(x + 6, y + 6, "K")
    if tongue:
        c.px(x + 9, y + 5, "P"); c.px(x + 9, y + 6, "P")


def neck(c, a, b):
    paint(c, chain_mask([a, b], 2.7), "O")


def tail(c, pts, sway=0.0, dy=0.0, border=False, r=(1.35, 1.0)):
    n = max(1, len(pts) - 1)
    pts = [(x + sway * (i / n) * 2.2, y + dy) for i, (x, y) in enumerate(pts)]
    m = chain_mask(pts, r[0], r[1])
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
    "run":  [(8.5, 20), (5, 20.5), (2, 20), (0.5, 19)],
}


def stand_legs(y0, nf=(0, 0), ff=(0, 0), nb=(0, 0), fb=(0, 0)):
    return {"nf": ((22, y0), (22 + nf[0], GROUND - nf[1])),
            "ff": ((19, y0), (19 + ff[0], GROUND - ff[1])),
            "nb": ((10, y0), (10 + nb[0], GROUND - nb[1])),
            "fb": ((13, y0), (13 + fb[0], GROUND - fb[1]))}


def pose_quad(hip, sh, head_xy, legs, arch=0.0, r=3.4, tail_pts=None, sway=0.0, tail_dy=0.0,
              eyes="open", mouth="closed", ear=0, tongue=False, extra=None, tail_r=(1.35, 1.0)):
    c = Canvas()
    tail(c, tail_pts or TAILS["mid"], sway, tail_dy, r=tail_r)
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
               legs=None, tongue=False, hdy=0):
    if phase is not None:
        a, b = LEG[phase], LEG[(phase + 3) % 6]
        bob = BOB[phase]
        legs = dict(nf=a, fb=a, ff=b, nb=b)
    legs = legs or {}
    return pose_quad((12, 20 + bob), (23, 20 + bob), (25, 12 + bob + hdy), stand_legs(21 + bob, **legs),
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


def pose_sit(sway=0.0, eyes="open", ear=0, mouth="closed", hdy=0, hearts=None, head_xy=(19, 6),
             torso_top=(20.5, 13.2), tail_pts=None, extra=None):
    c = Canvas()
    sit_base(c, sway, head_xy, torso_top, eyes=eyes, ear=ear, mouth=mouth, hdy=hdy, tail_pts=tail_pts)
    if extra:
        extra(c)
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


# ---------- nieuwe poses: springen, jagen, vechten, spelen, stemmingen, optillen ---- #

STAR = [".#.", "###", ".#."]
HELD_PIVOT = (21, 10)   # waar de kat "bij de nekvel" wordt vastgehouden (ontwerp-coordinaten)


def _legs(nf, ff, nb, fb, sh=(23, 20), hip=(12, 20)):
    return {"nf": ((sh[0] - 0.5, sh[1] + 1), nf), "ff": ((sh[0] - 3.5, sh[1] + 1), ff),
            "nb": ((hip[0] - 2, hip[1] + 1), nb), "fb": ((hip[0] + 1, hip[1] + 1), fb)}


def pose_pounce(k):
    """k: 0 afzet, 1 top, 2 neerkomst, 3 landing met pootjes naar voren."""
    if k == 3:
        legs = {"nf": ((22, 25), (31, GROUND)), "ff": ((19, 25), (28, GROUND)),
                "nb": ((10, 24), (9, GROUND)), "fb": ((13, 24), (13, GROUND))}
        return pose_quad((12, 22), (23, 23.5), (26, 19), legs, tail_pts=TAILS["up"], r=3.2)
    hip, sh, hd, nf, ff, nb, fb = {
        0: ((11, 20.5), (23, 17.5), (26, 10), (31, 19), (28, 21), (5, 25), (8, 26)),
        1: ((10.5, 19.5), (23.5, 17), (27, 9), (33, 20), (30, 22), (4, 24), (7, 25)),
        2: ((11, 21), (23.5, 19), (27, 12), (32, 25), (29, 26), (6, 25), (9, 26)),
    }[k]
    return pose_quad(hip, sh, hd, _legs(nf, ff, nb, fb, sh, hip), arch=-0.6, tail_pts=TAILS["run"],
                     eyes="open", mouth="open", ear=2)


def pose_stalk(i, sway=0.0):
    a, b = LEG[i], LEG[(i + 3) % 6]
    sa = (round(a[0] * 0.6), 1 if a[1] else 0)
    sb = (round(b[0] * 0.6), 1 if b[1] else 0)
    return pose_quad((12, 23), (23, 23), (26, 17), stand_legs(24, nf=sa, ff=sb, nb=sb, fb=sa),
                     tail_pts=TAILS["low"], sway=sway, tail_dy=1, eyes="open")


def pose_wiggle(i):
    s = (-0.8, 0.8)[i % 2]
    return pose_bow((11.5 + s, 17.5), (21.5, 23.2), (25, 18.5), 25, 22, 10 + s, 13 + s, arch=-0.3,
                    tail_kind="high", sway=s * 1.8, eyes="open")


RUN = [((5, 1), (-5, 1)), ((3, 3), (-3, 3)), ((0, 3), (1, 3)), ((-3, 1), (4, 1)), ((-1, 0), (3, 0)), ((2, 0), (-1, 0))]
BOBR = [-1, -2, -2, -1, 0, 0]


def pose_run(i):
    f, h = RUN[i]
    bob = BOBR[i]
    legs = stand_legs(21 + bob, nf=f, ff=(f[0] - 1, f[1]), nb=h, fb=(h[0] - 1, h[1]))
    return pose_quad((11, 20 + bob), (24, 19.5 + bob), (27, 12 + bob), legs, arch=-0.6,
                     tail_pts=TAILS["run"], tail_dy=bob, mouth="open", ear=2)


def pose_hiss(i):
    s = (-0.6, 0.6, 0.0)[i % 3]
    legs = stand_legs(20, nf=(-1, 0), ff=(-1, 0), nb=(1, 0), fb=(1, 0))
    return pose_quad((12 + s, 18.8), (23 + s, 18.8), (24, 10.5), legs, arch=5, r=3.7,
                     tail_pts=TAILS["high"], sway=s * 2.5, tail_r=(2.0, 1.6), eyes="angry",
                     mouth="yawn", ear=2)


def pose_swat(k):
    paw = [(26, 16), (33, 19), (30, 23)][k % 3]
    legs = stand_legs(21)
    legs["nf"] = ((22, 21), paw)
    return pose_quad((12, 20), (23, 20), (26, 11), legs, tail_pts=TAILS["up"], sway=(1.5, -1.5, 1.5)[k % 3],
                     eyes="angry", mouth="yawn" if k % 3 == 1 else "closed", ear=2)


def pose_invite(i):
    s = (-1.2, 1.2)[i % 2]
    return pose_bow((11.5, 17.5), (21.5, 23.5), (25, 18), 29, 26, 10, 13, arch=-0.5, tail_kind="high",
                    sway=s, eyes="open", mouth="smile")


def pose_roll(k, eyes="happy", mouth="smile"):
    """Op de rug, pootjes in de lucht (speels / overgave)."""
    c = Canvas()
    kick = k % 2
    tail(c, [(8, 26), (5.5, 27), (3.5, 26.5), (2.5, 25)], 0.0)
    limb(c, (13, 23), (14 - 2 * kick, 14), "F")
    limb(c, (18, 23), (21 + kick, 15), "F")
    m = chain_mask([(10.5, 25.2), (22, 25.2)], 3.0)
    keys = set(m)
    for (x, y) in m:
        top = (x, y - 1) not in keys or (x, y - 2) not in keys
        c.px(x, y, "L" if top else "O")
    limb(c, (10, 23), (8 + 2 * kick, 15), "O")
    limb(c, (21, 23), (25 - kick, 16 + kick), "O")
    head(c, 23, 19, eyes, 0, mouth)
    c.outline()
    return c


def pose_catch(k):
    legs = stand_legs(21)
    legs["nf"] = ((22, 23), (31, GROUND))
    legs["ff"] = ((19, 23), (28.5, GROUND))
    return pose_quad((12, 19.5), (23, 21.8), (26, 19 + k % 2), legs, tail_pts=TAILS["up"],
                     sway=(0.8, -0.8)[k % 2], eyes="open", mouth=("open", "closed")[k % 2], ear=(0, 0, 1, 0)[k % 4])


def pose_sad(k):
    """Rustig en een beetje stil: hoofd iets omlaag, halfdichte ogen. Geen drama."""
    tp = [(8, 26.5), (5.5, 27.8), (3, 28), (1.5, 28)]
    return pose_sit(sway=(0, 0.4, 0, -0.4)[k % 4], eyes="half", ear=0, mouth="closed",
                    head_xy=(19, 8), torso_top=(20.3, 14.6), tail_pts=tp)


def pose_dizzy(k):
    def extra(c):
        ang = k * math.pi / 2
        for a in (ang, ang + math.pi):
            sx = 23 + 7 * math.cos(a)
            sy = 4 + 2.2 * math.sin(a)
            stamp(c, STAR, int(round(sx)) - 1, int(round(sy)) - 1, "W", halo=False)
    return pose_sit(eyes="x", ear=2, mouth="open", head_xy=(19, 9), torso_top=(20, 15.5), extra=extra)


def pose_held(k):
    c = Canvas()
    sw = (0.0, 1.0, 0.0, -1.0)[k % 4]
    tail(c, [(18, 21), (16, 25), (15.5 + sw, 29)], sw)
    limb(c, (17, 22), (16 + sw, GROUND), "F")
    limb(c, (19.5, 22), (19.5 - sw * 0.8, GROUND), "O")
    limb(c, (19, 15), (21 + sw, 22), "F")
    paint_fur(c, chain_mask([(20, 12), (18.5, 23)], 3.2), belly=False, bands=5)
    limb(c, (21, 15), (23 - sw, 23), "O")
    neck(c, (20.5, 11), (25, 8))
    head(c, 21, 3, ("open", "half")[k % 2], 2, "open")
    c.outline()
    return c


def pose_fly():
    legs = {"nf": ((22, 18), (30, 12)), "ff": ((19, 18), (27, 10)),
            "nb": ((10, 18), (3, 13)), "fb": ((13, 18), (6, 11))}
    return pose_quad((12, 17), (23, 17), (26, 8), legs, arch=-1, tail_pts=TAILS["run"], tail_dy=-3,
                     eyes="open", mouth="yawn", ear=2)


def pose_clatter(k):
    """Hongerig: slaat met een pootje tegen de lege bak (de bak staat rechts van de kat)."""
    paw = [(30, 21), (34, 25), (31, 22), (35, 26)][k % 4]
    legs = stand_legs(21)
    legs["nf"] = ((22, 23), paw)
    legs["ff"] = ((19, 23), (21.5, GROUND))
    return pose_quad((12, 19.5), (23, 21.8), (26, 19), legs, tail_pts=TAILS["up"], sway=(1.2, -1.2)[k % 2],
                     eyes="open", mouth=("open", "closed")[k % 2], ear=2 if k % 2 else 0)


# ---------- emotes (zwevende icoontjes boven het hoofd) -------------------------------- #

EMOTE_PAL = {"K": _rgb("#211a24"), "P": _rgb("#f58a96"), "R": _rgb("#ff4a38"), "T": _rgb("#6fb4ff"),
             "Y": _rgb("#ffd23a"), "W": (1, 1, 1)}
EMOTES = {
    "heart": ([".#.#.", "#####", ".###.", "..#.."], "P"),
    "anger": (["#...#", ".#.#.", "..#..", ".#.#.", "#...#"], "R"),
    "drop": (["..#..", ".###.", "#####", "#####", ".###."], "T"),
    "zz": (["###.....", "..#.###.", ".#....#.", "###..#..", ".....###"], "W"),
    "star": (["..#..", "..#..", "#####", "..#..", "..#.."], "Y"),
    "alert": (["#", "#", "#", ".", "#"], "R"),
    "question": (["###", "..#", ".##", "...", ".#."], "Y"),
    "note": ("..###|..#.#|..#.#|##.##|###..".split("|"), "P"),
}


def emote_canvas(name):
    glyph, col = EMOTES[name]
    c = Canvas(len(glyph[0]) + 2, len(glyph) + 2, oy=0)
    for j, row in enumerate(glyph):
        for i, ch in enumerate(row):
            if ch == "#":
                c.px(i + 1, j + 1, col)
    c.outline()
    return c


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
        fps=4, tfps=2.5, intro=2, outro=2, sfx={3: ("crunch", 0.6), 5: ("crunch", 0.6)})

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
        sfx={3: ("mew", 0.2)})

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
        sfx={4: ("mew", 0.4)})

    # spinnen (klik)
    purr = [pose_sit(sway=sw6[i] * 0.6, eyes="happy", mouth="smile", hdy=i % 2,
                     hearts=[(29, 12 - 3 * i), (33, 12 - 3 * ((i + 3) % 6))]) for i in range(6)]
    add("purr", purr, fps=8)
    # ---- springen over een andere kat / naar de muis (de boog zelf wordt in de engine bewogen)
    add("jump", [crouch("open"),
                 pose_stand(bob=-1, tail_kind="mid", legs=dict(nf=(3, 0), ff=(2, 0), nb=(-3, 0), fb=(-2, 0))),
                 pose_stand(bob=0, tail_kind="up", mouth="open", legs=dict(nf=(4, 3), ff=(3, 3), nb=(-4, 3), fb=(-3, 3))),
                 pose_stand(bob=0, tail_kind="mid", legs=dict(nf=(3, 2), ff=(2, 2), nb=(-2, 2), fb=(-1, 2))),
                 pose_stand(bob=2, tail_kind="low"), pose_stand(bob=0)],
        durs=[0.18, 0.12, 0.30, 0.20, 0.10, 0.10], sfx={4: ("thud", 0.3)})
    add("pounce", [pose_stalk(2), pose_pounce(0), pose_pounce(1), pose_pounce(2), pose_pounce(3),
                   pose_stand(bob=1, eyes="happy")],
        durs=[0.15, 0.15, 0.25, 0.20, 0.15, 0.10], sfx={1: ("swat", 0.4), 4: ("thud", 0.3)})
    add("stalk", [pose_stalk(i, [0, .6, 1, .6, 0, -.6][i]) for i in range(6)], fps=6)
    add("wiggle", [pose_wiggle(i) for i in range(4)], fps=7)
    add("reach", [pose_wall((15, 23), (26, 16.5), 1.5, p, (27, 8), r=3.1, eyes="open", mouth="smile")
                  for p in ((36, 9), (37, 4), (36, 7), (37, 3))], fps=4, sfx={1: ("swat", 0.3), 3: ("swat", 0.3)})
    add("run", [pose_run(i) for i in range(6)], fps=12)

    # ---- vechten en spelen
    add("hiss", [pose_hiss(i) for i in range(3)], fps=6, sfx={0: ("hiss", 0.5)})
    add("swat", [pose_swat(k) for k in range(3)], fps=5, sfx={1: ("swat", 0.6)})
    add("invite", [pose_invite(i) for i in range(4)], fps=4, sfx={0: ("chirp", 0.4)})
    add("roll", [crouch("open")] + [pose_roll(k) for k in range(6)] + [crouch("open")],
        fps=6, tfps=3, intro=1, outro=1, sfx={3: ("trill", 0.3)})
    add("catch", [pose_catch(k) for k in range(4)], fps=4, sfx={1: ("crunch", 0.3), 3: ("crunch", 0.3)})

    # ---- stemmingen
    add("stare", [pose_sit(sway=(0, .6, 1, .6)[i], eyes="open", head_xy=(20, 4), ear=1 if i == 2 else 0)
                  for i in range(4)], fps=2)
    add("sad", [pose_sad(k) for k in range(4)], fps=2)
    add("angry", [pose_stand(bob=0, tail_kind="up", sway=(2.2, -2.2, 2.2, -2.2)[k], eyes="angry", mouth="frown",
                             ear=2, legs=dict(nf=(1, 0), nb=(-1, 0), ff=(1, 0), fb=(-1, 0))) for k in range(4)], fps=4)
    add("happy", [pose_stand(bob=(0, -1, 0, -1)[k], tail_kind="up", sway=(1, -1, 1, -1)[k], eyes="happy",
                             mouth="smile", ear=1 if k == 2 else 0) for k in range(4)], fps=4)
    add("yawn", [pose_sit(eyes="half"), pose_sit(eyes="closed", head_xy=(20, 4), mouth="yawn"),
                 pose_sit(eyes="yawn", head_xy=(20, 3), mouth="yawn"),
                 pose_sit(eyes="closed", head_xy=(20, 4)), pose_sit(eyes="half")],
        durs=[0.4, 0.5, 1.0, 0.5, 0.4], sfx={2: ("mew", 0.25)})
    add("walk_low", [pose_stand(i, tail_kind="low", sway=sw6[i] * 0.5, eyes="half", hdy=3) for i in range(6)], fps=6)

    add("clatter", [pose_clatter(k) for k in range(4)], fps=5, sfx={1: ("clank", 1.0), 3: ("clank", 0.7)})

    # ---- optillen / gooien
    add("held", [pose_held(k) for k in range(4)], fps=4)
    add("fly", [pose_fly(), pose_fly()], fps=6)
    add("dizzy", [pose_dizzy(k) for k in range(4)], fps=4)
    for n in ("stretch_back", "stretch_wall"):
        A[n].setdefault("tfps", 3)
        A[n].setdefault("intro", 0)
        A[n].setdefault("outro", 0)
    return A


# --------------------------------------------------------------------------- #
#  Houdingen, tussenanimaties en toestanden
# --------------------------------------------------------------------------- #

STATE_POSTURE = {
    "walk": "stand", "hop": "stand", "idle": "stand", "eat": "stand", "run": "stand", "walk_low": "stand",
    "stalk": "stand", "wiggle": "stand", "clatter": "stand", "reach": "stand", "hiss": "stand", "swat": "stand", "invite": "stand",
    "roll": "stand", "catch": "stand", "angry": "stand", "happy": "stand", "jump": "stand", "pounce": "stand",
    "stretch_wall": "stand", "stretch_back": "stand", "held": "stand", "fly": "stand",
    "sit": "sit", "lick_paw": "sit", "lick_tail": "sit", "wash": "sit", "purr": "sit", "stare": "sit",
    "sad": "sit", "yawn": "sit", "dizzy": "sit",
    "sleep_loaf": "lie", "sleep_curl": "lie",
}
TRANS = {"t_stand_sit": ("stand", "sit"), "t_sit_stand": ("sit", "stand"),
         "t_sit_lie": ("sit", "lie"), "t_lie_sit": ("lie", "sit")}
REST = {"idle", "sit", "sad", "angry", "happy"}
MOVING = {"walk": 1.0, "hop": 1.05, "walk_low": 0.6, "stalk": 0.5, "run": 2.4}   # loopsnelheid t.o.v. normaal
COOL_KEY = {"lick_paw": "lick", "lick_tail": "lick", "wash": "lick",
            "sleep_loaf": "sleep", "sleep_curl": "sleep"}
SOUND_COOLDOWN = {"purr": 0.5, "mew": 2.0, "crunch": 0.4, "hiss": 2.5, "growl": 2.0, "yowl": 2.0,
                  "chirp": 1.0, "thud": 0.5, "swat": 0.5, "trill": 1.5, "clank": 0.35, "clink": 0.12}
MOODS = ["happy", "energetic", "calm", "tired", "sad", "angry"]
MOOD_NL = {"happy": "blij", "energetic": "energiek", "calm": "rustig", "tired": "moe",
           "sad": "verdrietig", "angry": "boos"}
MOOD_EMOTE = {"happy": "heart", "energetic": "star", "tired": "zz", "sad": "drop", "angry": "anger"}


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

    def air_fractions(self):
        """(begin, end) van de luchtfase als fractie van de animatie (voor springen)."""
        if self.durs and self.total and len(self.durs) >= 4:
            return self.durs[0] / self.total, sum(self.durs[:4]) / self.total
        return 0.2, 0.8


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


_EMOTE_CACHE = {}


def emote_frame(name, scale):
    key = (name, scale)
    if key not in _EMOTE_CACHE:
        _EMOTE_CACHE[key] = Frame(canvas_surface(emote_canvas(name), scale, EMOTE_PAL))
    return _EMOTE_CACHE[key]


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
  held_pivot    [x, y] punt waar de kat wordt vastgehouden als je hem optilt (standaard [21, 10])
  anims.<naam>  fps, tfps, intro, outro, durs (tijd per frame, speelt 1x), sfx ({"3": ["mew", 0.7]})

Animaties:
  lopen/staan   walk hop idle walk_low run stalk
  zitten/slapen sit lick_paw lick_tail wash sleep_loaf sleep_curl yawn stare
  tussenstappen t_stand_sit t_sit_stand t_sit_lie t_lie_sit
  rekken/eten   stretch_back stretch_wall eat clatter (rammelen aan de lege bak) purr
  stemmingen    happy angry sad
  samen         jump (over een andere kat; 'durs' = crouch, afzet, top, daling, landing, herstel;
                de boog zelf wordt door het programma bewogen)  invite roll catch hiss swat
  muis          pounce (zelfde opbouw als jump) wiggle reach stalk catch
  optillen      held (hangt aan de nekvel)  fly (gegooid, draait mee)  dizzy
Voerbak: sprites/bowl.png (4 frames: vol .. leeg) + bowl.json {"frame_width": 16}
Eigen geluid: sounds/<naam>.wav naast catpet.py, met naam: purr mew crunch hiss growl yowl chirp trill thud swat clank clink
"""


def dump_sprites(root, coats):
    import cairo
    root = Path(root)
    for i, coat in enumerate(coats):
        folder = root / f"cat{i + 1}"
        folder.mkdir(parents=True, exist_ok=True)
        set_coat(coat)
        cfg = {"coat": coat.name, "frame_width": W, "mouth_x": MOUTH_X, "held_pivot": list(HELD_PIVOT),
               "anims": {}}
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


def _glide(path, f_of_t, dur, harm, sr=22050, noise=0.0, vib=0.0, attack=0.03, power=0.7, seed=3):
    rnd = random.Random(seed)
    n, phase, out = int(sr * dur), 0.0, []
    for i in range(n):
        t = i / n
        f = f_of_t(t) + vib * math.sin(2 * math.pi * 8 * i / sr)
        phase += 2 * math.pi * f / sr
        s = sum(a * math.sin((k + 1) * phase) for k, a in enumerate(harm)) + noise * rnd.uniform(-1, 1)
        out.append(s * min(1, (i / sr) / attack) * (1 - t) ** power)
    _write_wav(path, out, sr)


def synth_mew(path, pitch=1.0, dur=0.45):
    _glide(path, lambda t: pitch * (520 + 380 * math.sin(math.pi * t) ** 0.8 - 120 * t), dur,
           [1, 0.5, 0.35, 0.2])


def synth_yowl(path, pitch=1.0, dur=0.75):
    _glide(path, lambda t: pitch * (550 + 650 * math.sin(math.pi * t) ** 0.7), dur,
           [1, 0.6, 0.45, 0.3, 0.2], noise=0.15, vib=18, attack=0.04, power=0.5)


def synth_chirp(path, pitch=1.0, dur=0.3, sr=22050):
    out = []
    for start, f0, f1 in ((0.0, 700, 1100), (0.14, 800, 1300)):
        for i in range(int(sr * 0.1)):
            t = i / (sr * 0.1)
            ph = 2 * math.pi * pitch * (f0 * t + (f1 - f0) * t * t / 2) * 0.1
            out.append((start, i, math.sin(ph) * math.sin(math.pi * t)))
    buf = [0.0] * int(sr * dur)
    for start, i, v in out:
        idx = int(start * sr) + i
        if idx < len(buf):
            buf[idx] += v
    _write_wav(path, buf, sr)


def synth_trill(path, pitch=1.0, dur=0.32):
    _glide(path, lambda t: pitch * (420 + 220 * t), dur, [1, 0.4, 0.2], vib=40, attack=0.02, power=0.4)


def synth_hiss(path, pitch=1.0, dur=0.8, sr=22050):
    rnd = random.Random(7)
    lp, out = 0.0, []
    for i in range(int(sr * dur)):
        t = i / sr
        n = rnd.uniform(-1, 1)
        lp += 0.12 * (n - lp)
        env = min(1, t / 0.06) * min(1, (dur - t) / 0.3) * (0.8 + 0.2 * math.sin(2 * math.pi * 11 * t))
        out.append((n - lp) * env)
    _write_wav(path, out, sr)


def synth_growl(path, pitch=1.0, dur=1.0, sr=22050):
    rnd = random.Random(9)
    lp, out, f = 0.0, [], 70 * pitch
    for i in range(int(sr * dur)):
        t = i / sr
        n = rnd.uniform(-1, 1)
        lp += 0.1 * (n - lp)
        saw = 2 * ((f * t) % 1) - 1
        trem = 0.6 + 0.4 * math.sin(2 * math.pi * 28 * t)
        out.append((saw * 0.7 + lp * 2.0) * trem * min(1, t / 0.1) * min(1, (dur - t) / 0.3))
    _write_wav(path, out, sr)


def synth_thud(path, pitch=1.0, dur=0.18, sr=22050):
    rnd = random.Random(4)
    out = []
    for i in range(int(sr * dur)):
        t = i / sr
        out.append((math.sin(2 * math.pi * (90 - 50 * t / dur) * pitch * t) * 1.0 + rnd.uniform(-1, 1) * 0.25)
                   * math.exp(-t * 28))
    _write_wav(path, out, sr)


def synth_swat(path, pitch=1.0, dur=0.14, sr=22050):
    rnd = random.Random(2)
    lp, out = 0.0, []
    for i in range(int(sr * dur)):
        t = i / sr
        n = rnd.uniform(-1, 1)
        lp += 0.3 * (n - lp)
        out.append((n - lp) * math.sin(math.pi * t / dur) ** 2)
    _write_wav(path, out, sr)


def synth_clank(path, pitch=1.0, dur=0.4, sr=22050):
    """Metalen bakje dat rinkelt."""
    rnd = random.Random(8)
    out = []
    for i in range(int(sr * dur)):
        t = i / sr
        ring = sum(a * math.sin(2 * math.pi * f * pitch * t) * math.exp(-t * d)
                   for f, a, d in ((830, 1.0, 14), (1330, 0.7, 18), (2150, 0.5, 24), (3170, 0.3, 32)))
        out.append(ring + rnd.uniform(-1, 1) * 0.5 * math.exp(-t * 120))
    _write_wav(path, out, sr)


def synth_clink(path, pitch=1.0, dur=0.09, sr=22050):
    out = []
    for i in range(int(sr * dur)):
        t = i / sr
        out.append((math.sin(2 * math.pi * 2400 * pitch * t) + 0.6 * math.sin(2 * math.pi * 3300 * pitch * t))
                   * math.exp(-t * 70))
    _write_wav(path, out, sr)


def synth_crunch(path, pitch=1.0, dur=0.16, sr=22050):
    rnd = random.Random(5)
    lp, out = 0.0, []
    for i in range(int(sr * dur)):
        t = i / sr
        n = rnd.uniform(-1, 1)
        lp += (0.45 / pitch) * (n - lp)
        env = math.exp(-t * 60) + 0.7 * (math.exp(-(t - 0.07) * 80) if t > 0.07 else 0)
        out.append((n - lp) * env)
    _write_wav(path, out, sr)


SYNTH = {"purr": synth_purr, "mew": synth_mew, "crunch": synth_crunch, "hiss": synth_hiss,
         "growl": synth_growl, "yowl": synth_yowl, "chirp": synth_chirp, "trill": synth_trill,
         "thud": synth_thud, "swat": synth_swat, "clank": synth_clank, "clink": synth_clink}


class Sfx:
    def __init__(self, enabled, volume, pitches, custom_dir, rate=0.5):
        self.enabled, self.volume, self.rate = enabled, volume, rate
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
            for name, fn in SYNTH.items():
                f = self.cache / f"{name}_{int(p * 100)}.wav"
                if not f.exists():
                    fn(f, p)
                self.files[(name, i)] = f

    def _scaled(self, path):
        """Pas het volume toe op een kopie van de wav (gecachet)."""
        if path.suffix != ".wav" or self.volume >= 0.99:
            return path
        out = self.cache / f"{path.stem}_v{int(self.volume * 100)}_{zlib.crc32(str(path).encode()) % 10000}.wav"
        if not out.exists():
            with wave.open(str(path)) as f:
                params, raw = f.getparams(), f.readframes(f.getnframes())
            n = len(raw) // 2
            vals = struct.unpack(f"<{n}h", raw)
            with wave.open(str(out), "wb") as g:
                g.setparams(params)
                g.writeframes(struct.pack(f"<{n}h", *[int(v * self.volume) for v in vals]))
        return out

    def play(self, name, idx, forced=False):
        """forced = direct gevolg van jouw actie (aaien, zak legen); de rest wordt door `rate` uitgedund."""
        if not self.enabled or self.volume <= 0:
            return
        if not forced and random.random() > self.rate:
            return
        now = time.monotonic()
        if now - self.last.get(name, 0) < SOUND_COOLDOWN.get(name, 0.3):
            return
        p = self.proc.get(name)
        if name in ("purr", "mew", "growl") and p is not None and p.poll() is None:
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
#  Muis volgen (Hyprland), instellingen, voerbak
# --------------------------------------------------------------------------- #

class Pointer:
    """Leest de muispositie via Hyprland (socket of hyprctl). x/y zijn relatief aan de monitor."""

    def __init__(self, geo, enabled=True):
        self.ox, self.oy = (geo.x, geo.y) if geo else (0, 0)
        self.x = self.y = None
        self.speed, self.last_move, self.last_t, self.fail = 0.0, 0.0, 0.0, 0
        self.enabled = enabled
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.paths = [f"{rt}/hypr/{sig}/.socket.sock", f"/tmp/hypr/{sig}/.socket.sock"] if sig else []
        if enabled and not self.paths and not shutil.which("hyprctl"):
            print("[catpet] geen Hyprland gevonden: de katten kunnen de muis niet zien")
            self.enabled = False

    def _query(self):
        for path in self.paths:
            for cmd in (b"/cursorpos", b"cursorpos"):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(0.05)
                    s.connect(path)
                    s.sendall(cmd)
                    data = b""
                    while True:
                        chunk = s.recv(256)
                        if not chunk:
                            break
                        data += chunk
                    s.close()
                    if b"," in data:
                        return data.decode()
                except OSError:
                    continue
        if shutil.which("hyprctl"):
            try:
                return subprocess.run(["hyprctl", "cursorpos"], capture_output=True, text=True, timeout=0.2).stdout
            except Exception:
                return None
        return None

    def poll(self, now):
        if not self.enabled or now - self.last_t < 0.1:
            return
        txt = self._query()
        dt, self.last_t = now - self.last_t, now
        try:
            gx, gy = [float(v) for v in txt.replace("\n", "").split(",")]
        except Exception:
            self.fail += 1
            if self.fail > 30:
                print("[catpet] muispositie niet beschikbaar - muisgedrag uitgeschakeld")
                self.enabled = False
            return
        self.fail = 0
        nx, ny = gx - self.ox, gy - self.oy
        if self.x is not None and dt > 0:
            sp = math.hypot(nx - self.x, ny - self.y) / dt
            self.speed = 0.6 * self.speed + 0.4 * sp
            if sp > 20:
                self.last_move = now
        else:
            self.last_move = now
        self.x, self.y = nx, ny

    def active(self):
        return self.enabled and self.x is not None and time.monotonic() - self.last_move < 25


class Settings:
    def __init__(self, args):
        self.jump_height, self.jump_speed = 1.0, 1.0
        self.fight, self.play, self.mouse, self.throw = 1.0, 1.0, 1.0, 1.0
        self.track_mouse = not args.no_mouse


class Bowl:
    """Voerbak met een vulstand (0..1). Vullen doe je zelf met de zak (rechtermuisknop op de bak)."""

    def __init__(self, sw, sh, floor, scale, frac, root, auto=False):
        self.sw, self.sh, self.floor = sw, sh, floor
        self.cx = frac * sw
        self.fill, self.timer, self.refill_after, self.occupied = 1.0, 0.0, random.uniform(60, 120), None
        self.auto, self.filled_at, self.shake_until = auto, -1e9, 0.0
        self.root = Path(root)
        self.set_scale(scale)

    @property
    def level(self):
        f = self.fill
        return 0 if f > 0.7 else 1 if f > 0.45 else 2 if f > 0.2 else 3

    @property
    def empty(self):
        return self.level >= 3

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
        self.fill = max(0.0, self.fill - 0.27)

    def add(self, amount):
        self.fill = min(1.0, self.fill + amount)
        self.filled_at = time.monotonic()

    def refill(self):
        self.fill, self.timer = 1.0, 0.0
        self.filled_at = time.monotonic()

    def shake(self, dur=0.4):
        self.shake_until = time.monotonic() + dur

    def shaking(self):
        return time.monotonic() < self.shake_until

    def update(self, dt):
        if self.auto and self.empty and self.occupied is None:
            self.timer += dt
            if self.timer > self.refill_after:
                self.refill()

    def draw(self, ctx):
        x, y, _w, _h = self.rect()
        if self.shaking():  # kletteren: het bakje rinkelt heen en weer
            x += math.sin(time.monotonic() * 70) * 1.6 * self.scale
        ctx.set_source_surface(self.frames[self.level].surf, x, y)
        ctx.paint()


# --------------------------------------------------------------------------- #
#  De kat: toestanden, stemming, muis, optillen/gooien
# --------------------------------------------------------------------------- #

class Pet:
    def __init__(self, index, coat, anims, cfg, world, scale, speed, calm):
        self.index, self.coat, self.anims, self.cfg, self.world = index, coat, anims, cfg, world
        self.sw, self.sh, self.floor, self.bowl = world.sw, world.sh, world.floor, world.bowl
        self.scale, self.calm = scale, calm * random.uniform(0.85, 1.2)
        self.mouth_x = cfg.get("mouth_x", MOUTH_X)
        self.pivot = tuple(cfg.get("held_pivot", HELD_PIVOT))
        self.speed_mult = speed * random.uniform(0.9, 1.1)
        self.px_speed = 18 * scale * self.speed_mult
        self.dir = random.choice((-1, 1))
        self.posture, self.queue, self.script = "stand", [], []
        self.state, self.t, self.dur, self.tscale = "idle", 0.0, random.uniform(1, 5), 1.0
        self.target, self.goal, self.back = 0.0, "wander", False
        self.track, self.track_until, self.track_variant = None, 0.0, "walk"
        self.eat_dir, self.eat_acc, self.arc, self.resume, self.enc = 1, 0.0, None, None, None
        self.yoff, self.vx, self.vy, self.omega, self.spin, self.impact = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        self.held, self.hold, self.swing, self.swing_target = False, None, 0.0, 0.0
        self.last, self.events, self._idx, self.emote = {}, [], -1, None
        self._react_t, self.eat_call = 0.0, 0.0
        # stemming en behoeften
        self.energy, self.hunger, self.social = random.uniform(0.5, 1), random.uniform(0, 0.4), random.uniform(0, 0.4)
        self.mood, self.mood_until, self.mood_lock, self._mood_t = "calm", 0.0, None, 0.0
        self.x = random.uniform(0, max(1, self.sw - self.fw))

    # -- maten
    @property
    def fw(self):
        return self.anims["walk"].frames[0].w

    def core(self):
        return 22 * self.scale

    def cx(self):
        return self.x + self.fw / 2

    def cat_h(self):
        return 22 * self.scale

    def grounded(self):
        return self.yoff < 3 and self.state not in ("jump", "pounce", "fly", "held")

    def px_for_center(self, cx):
        return max(0.0, min(cx - self.fw / 2, self.sw - self.fw))

    # -- instellingen (menu)
    def set_speed(self, v):
        self.speed_mult = v
        self.px_speed = 18 * self.scale * v

    def set_scale(self, scale):
        self.scale = scale
        for a in self.anims.values():
            a.set_scale(scale)
        self.px_speed = 18 * scale * self.speed_mult
        self.x = min(self.x, max(0, self.sw - self.fw))

    # -- frame-keuze
    def frame(self):
        a = self.anims[self.state]
        n = len(a.frames)
        if a.durs:
            tt, acc = self.t * self.tscale, 0.0
            for i, d in enumerate(a.durs[:n]):
                acc += d
                if tt < acc:
                    return a.frames[i], i
            return a.frames[n - 1], n - 1
        fps = a.fps * (self.speed_mult if self.state in MOVING else 1)
        loop_n = max(1, n - a.intro - a.outro)
        intro_t, outro_t = a.intro / a.tfps, a.outro / a.tfps
        if a.intro and self.t < intro_t:
            i = int(self.t * a.tfps)
        elif a.outro and self.state not in MOVING and self.t >= self.dur - outro_t:
            i = n - a.outro + min(int((self.t - (self.dur - outro_t)) * a.tfps), a.outro - 1)
        else:
            i = a.intro + int((self.t - intro_t) * fps) % loop_n
        return a.frames[min(i, n - 1)], min(i, n - 1)

    def rect(self):
        f, _ = self.frame()
        return int(self.x), int(self.sh - self.floor - f.h - self.yoff), f.w, f.h

    def signature(self):
        return (self.state, self.frame()[1], int(self.x), int(self.yoff), self.dir, self.scale,
                round(self.spin, 1), round(self.swing, 1), self.emote[0] if self.emote else None,
                int(time.monotonic() * 8) if self.emote else 0)

    def hit(self, px, py):
        f, _ = self.frame()
        x, y, w, h = self.rect()
        if not (x <= px < x + w and y <= py < y + h):
            return False
        if not self.grounded():
            return True
        return f.alpha(int(px) - x, int(py) - y, self.dir < 0) > 0

    # -- kleine helpers
    def say(self, name, prob=1.0):
        if random.random() < prob:
            self.events.append((name, 1.0))

    def show_emote(self, name, dur=1.8):
        now = time.monotonic()
        self.emote = (name, now + dur, now)

    def face(self, other):
        self.dir = 1 if other.cx() > self.cx() else -1

    # -- toestanden en tussenanimaties
    def set_state(self, name, loop_time=0.0):
        a = self.anims[name]
        self.state, self.t, self._idx, self.tscale, self.arc = name, 0.0, -1, 1.0, None
        self.dur = a.total if a.durs else loop_time + (a.intro + a.outro) / a.tfps
        if name not in REST and name not in TRANS and name not in MOVING:
            self.last[COOL_KEY.get(name, name)] = time.monotonic()
        if name != "eat":
            self.eat_acc = 0.0

    def cancel(self):
        """Onderbreek alles wat de kat aan het doen/plannen was."""
        self.queue, self.script, self.track, self.resume, self.back = [], [], None, None, False
        self.goal, self.arc = "wander", None
        if self.bowl and self.bowl.occupied is self:
            self.bowl.occupied = None

    def begin(self, state, loop_time=0.0):
        """Start `state`; past de houding niet, dan eerst via tussenanimaties (zitten, liggen, ...)."""
        if self.bowl and state != "eat" and self.bowl.occupied is self and self.goal != "bowl":
            self.bowl.occupied = None
        if state not in MOVING:
            self.goal, self.back = "wander", False
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
        if cur in ("jump", "pounce"):
            self.arc, self.yoff = None, 0.0
        if cur in ("eat", "clatter") and self.bowl and self.bowl.occupied is self:
            self.bowl.occupied = None
        if self.queue:
            return self._advance()
        if cur == "jump" and self.resume:
            tgt, goal, variant = self.resume
            self.resume = None
            return self.go(tgt, goal, variant)
        if self.track and cur in ("pounce", "jump") and time.monotonic() < self.track_until:
            return self.go(self.track(), variant=self.track_variant)   # na een sprong gewoon verder achter de muis aan
        if self.enc:
            return self.enc.pet_done(self)
        if self.script:
            return self.script.pop(0)(self)
        if cur.startswith("sleep"):
            return self.after_sleep()
        self.choose_next()

    def go(self, target, goal="wander", variant=None):
        self.target, self.goal = target, goal
        if abs(target - self.x) > 1:
            self.dir = 1 if target > self.x else -1
        self.begin(variant or ("hop" if random.random() < 0.25 else "walk"))
        self.goal, self.back = goal, False

    def backoff(self, dist):
        """Loop achteruit (kijkt naar de prooi) - aanloop voor de sprong."""
        d = self.dir
        self.target = max(0.0, min(self.x - d * dist, self.sw - self.fw))
        self.begin("stalk")
        self.dir, self.back = d, True

    def ready(self, name, cooldown):
        return time.monotonic() - self.last.get(name, -1e9) > cooldown * self.calm

    # -- stemming en behoeften
    def set_mood(self, m, dur=40.0, emote=True):
        if self.mood_lock or m == self.mood and time.monotonic() < self.mood_until:
            return
        if m != self.mood and emote and m in MOOD_EMOTE and random.random() < 0.4:
            self.show_emote(MOOD_EMOTE[m])
        self.mood, self.mood_until = m, time.monotonic() + dur

    def update_mood(self, dt):
        s = self.state
        if s.startswith("sleep"):
            self.energy = min(1.0, self.energy + dt / 60)
        elif s in ("run", "pounce", "jump", "hop", "swat", "hiss", "roll", "stalk", "wiggle"):
            self.energy = max(0.0, self.energy - dt / 300)
        else:
            self.energy = max(0.0, self.energy - dt / 1000)
        self.hunger = min(1.0, self.hunger + dt / 1500)
        near = self.world.nearest_other(self)
        if near is not None and abs(near.cx() - self.cx()) < 600:
            self.social = max(0.0, self.social - dt / 400)   # gezelschap helpt tegen eenzaamheid
        else:
            self.social = min(1.0, self.social + dt / 1500)
        now = time.monotonic()
        if self.mood_lock:
            self.mood = self.mood_lock
            return
        if now < self.mood_until or now - self._mood_t < 6:
            return
        self._mood_t = now
        if self.energy < 0.2:
            new = "tired"
        elif self.hunger > 0.85:
            new = "angry"
        elif self.social > 0.95 and random.random() < 0.35:
            new = "sad"
        elif self.energy > 0.8:
            new = "energetic"
        elif self.energy > 0.5 and self.hunger < 0.5 and self.social < 0.5:
            new = "happy" if random.random() < 0.6 else "calm"
        else:
            new = "calm"
        if new != self.mood:
            self.mood = new
            if new in MOOD_EMOTE and random.random() < 0.3:
                self.show_emote(MOOD_EMOTE[new])

    # -- plannen: wat doet de kat nu? (afhankelijk van stemming en behoeften, niet puur random)
    def rest_after(self):
        kind, lo, hi = {"tired": ("sit", 8, 16), "sad": ("sad", 8, 18), "angry": ("angry", 5, 10),
                        "happy": ("happy", 3, 7), "energetic": ("idle", 1.5, 4),
                        "calm": (random.choice(("sit", "idle")), 5, 12)}[self.mood]
        self.begin(kind, random.uniform(lo, hi) * self.calm * 1.5)

    def weights(self):
        m, r, st = self.mood, self.ready, self.world.settings
        w = dict({"calm": dict(walk=34, hop=6, sit=12, idle=8),
                  "happy": dict(walk=24, hop=26, sit=6, idle=4, happy=8),
                  "energetic": dict(walk=10, hop=24, run=34, idle=2),
                  "tired": dict(walk_low=12, sit=18, idle=4),
                  "sad": dict(walk_low=8, sad=4, sit=10, walk=12, idle=6),
                  "angry": dict(walk=16, run=8, angry=6)}[m])
        if r("stretch_back", 18):
            w["stretch_back"] = 40 if m == "tired" else 26
        if r("stretch_wall", 75):
            w["wall"] = 4 if m == "tired" else 10
        if r("lick", 28):
            w["lick"] = 30 if m in ("calm", "happy", "tired") else 8
        if r("sleep", 60 if m == "tired" else 160):
            w["sleep"] = {"tired": 70, "calm": 6, "sad": 8}.get(m, 0) + (25 if self.energy < 0.3 else 0)
        b = self.bowl
        if b and b.occupied is None:
            fresh = time.monotonic() - b.filled_at < 25   # net bijgevuld: dat hoor/ruik je meteen
            if not b.empty and (r("eat", 100) or self.hunger > 0.6 or (fresh and self.hunger > 0.25)):
                w["eat"] = 12 + 90 * max(0.0, self.hunger - 0.4) + (70 if fresh and self.hunger > 0.25 else 0)
            elif b.empty and self.hunger > 0.45 and r("clatter", 45):
                w["clatter"] = 10 + 70 * (self.hunger - 0.45)
        mi = self.mouse_interest()
        if mi > 0 and r("mouse", 40) and r("reaction", 20):
            w["mouse"] = 40 * mi
        return {k: v for k, v in w.items() if v > 0}

    def choose_next(self):
        if self.state not in REST:  # na een activiteit eerst even rust
            return self.rest_after()
        w = self.weights()
        choice = random.choices(list(w), list(w.values()))[0]
        fw = self.fw
        if choice in ("walk", "hop", "run", "walk_low"):
            tx = self.x
            for _ in range(12):
                tx = random.uniform(0, max(1, self.sw - fw))
                if abs(tx - self.x) > 250:
                    break
            self.go(tx, "wander", choice)
        elif choice == "wall":
            self.go(random.choice((0.0, float(self.sw - fw))), "wall")
        elif choice == "stretch_back":
            self.begin("stretch_back")
        elif choice == "lick":
            kind = random.choice(("lick_paw", "lick_tail", "wash"))
            self.begin(kind, random.uniform(3, 5) if kind == "wash" else random.uniform(6, 10))
        elif choice == "sleep":  # ritueel: geeuwen -> rekken -> slapen (-> rekken bij het wakker worden)
            kind, dur = random.choice(("sleep_loaf", "sleep_curl")), random.uniform(20, 50) * self.calm
            self.script = [lambda p: p.begin("stretch_back"), lambda p, k=kind, d=dur: p.begin(k, d)]
            self.begin("yawn")
        elif choice in ("eat", "clatter"):
            self.go_to_bowl()
        elif choice == "mouse":
            self.start_mouse_play()
        else:
            self.begin(choice, random.uniform(3, 8) * self.calm)

    def after_sleep(self):
        self.last["sleep"] = time.monotonic()
        self.script = [lambda p: p.begin("stretch_back")]
        if random.random() < 0.5:
            self.script.append(lambda p: p.begin("yawn"))
        self.set_mood("calm" if self.energy > 0.5 else self.mood, 20, emote=False)
        self.script.pop(0)(self)

    # -- eten
    def eat_spot(self):
        s, cx = self.scale, self.bowl.cx
        xa = cx - self.mouth_x * s                       # links van de bak, kijkt naar rechts
        xb = cx - (self.fw / s - self.mouth_x) * s       # rechts van de bak, gespiegeld
        _d, x, d = min((abs(xa - self.x), xa, 1), (abs(xb - self.x), xb, -1))
        return max(0.0, min(x, self.sw - self.fw)), d

    def go_to_bowl(self):
        if not self.bowl:
            return
        x, d = self.eat_spot()
        self.eat_dir = d
        self.go(x, "clatter" if self.bowl.empty else "bowl")   # lege bak: kletteren in plaats van eten
        self.bowl.occupied = self

    # -- springen (boog)
    def start_arc(self, anim, x1, height, duration):
        a = self.anims[anim]
        self.set_state(anim)
        a0, a1 = a.air_fractions()
        self.arc = dict(x0=self.x, x1=x1, H=height, T=duration, a0=a0, a1=a1)
        self.dur = duration
        self.tscale = (a.total / duration) if a.durs else 1.0
        self.dir = 1 if x1 > self.x else -1

    def jump_over(self, other):
        st, sc = self.world.settings, self.scale
        d = 1 if other.cx() > self.cx() else -1
        land_cx = other.cx() + d * ((other.core() + self.core()) / 2 + 8 * sc)
        land_x = land_cx - self.fw / 2
        if land_x < 0 or land_x > self.sw - self.fw:  # geen ruimte aan de overkant: andere kant op
            self.cancel()
            return self.rest_after()
        if self.state in MOVING and not self.back:
            self.resume = (self.target, self.goal, self.state)
        self.dir = d
        self.start_arc("jump", land_x, 26 * sc * st.jump_height, 0.9 / st.jump_speed)

    def update_arc(self):
        a = self.arc
        s = min(1.0, self.t / a["T"])
        u = 0.0 if s < a["a0"] else 1.0 if s >= a["a1"] else (s - a["a0"]) / (a["a1"] - a["a0"])
        self.x = a["x0"] + (a["x1"] - a["x0"]) * u
        self.yoff = a["H"] * 4 * u * (1 - u)
        if s >= 1.0:
            self.x, self.yoff = a["x1"], 0.0
            self.end_state()

    # -- bewegen
    def arrive(self):
        self.x, goal, self.goal, self.back = self.target, self.goal, "wander", False
        self.posture = "stand"
        if self.track:
            return self.begin("stare", 9999)
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
        elif goal == "clatter" and self.bowl:
            self.dir = self.eat_dir
            self.begin("clatter", random.uniform(3, 5))
            self.bowl.occupied = self
            self.say("mew", 0.5)
        else:
            self.end_state()

    def update_move(self, dt):
        step = self.px_speed * MOVING[self.state] * dt
        d = self.target - self.x
        if abs(d) <= step:
            return self.arrive()
        mdir = 1 if d > 0 else -1
        if not self.back:
            self.dir = mdir
        nx = self.x + step * mdir
        if not self.back:
            o = self.world.blocking(self, nx)
            if o:
                return self.world.on_block(self, o)
        self.x = nx

    # -- muis
    def pointer_height(self):
        p = self.world.pointer
        return (self.sh - self.floor) - p.y

    def mouse_interest(self):
        w = self.world
        if not (w.settings.track_mouse and w.pointer.active() and self.grounded() and not self.enc):
            return 0.0
        f = {"energetic": 1.7, "happy": 1.2, "calm": 0.6, "angry": 0.4, "sad": 0.15, "tired": 0.1}[self.mood]
        return w.settings.mouse * f * (0.4 + self.energy)

    def start_mouse_play(self, kind=None):
        p = self.world.pointer
        if not p.active():
            return self.rest_after()
        self.last["mouse"] = time.monotonic()
        low = self.pointer_height() < 2.0 * self.cat_h()
        opts = {"hunt": 4 if low else 2, "chase": 2.5, "follow": 1.5 if self.mood in ("happy", "energetic") else 0.4,
                "stare": 0.6 if low else 2, "bat": 2.5 if self.pointer_height() < 1.9 * self.cat_h() else 0}
        opts = {k: v for k, v in opts.items() if v > 0}
        kind = kind or random.choices(list(opts), list(opts.values()))[0]
        getattr(self, "mouse_" + kind)()

    def _run_script(self, steps):
        self.script = list(steps)
        self.script.pop(0)(self)

    def mouse_hunt(self):
        sc = self.scale
        side = 1 if self.cx() < self.world.pointer.x else -1  # kat links van de muis = 1

        def at(dist):
            return self.px_for_center(self.world.pointer.x - side * dist)

        def alive(f):
            def step(p):
                if not p.world.pointer.active():
                    return p.rest_after()
                f(p)
            return step

        self.show_emote("alert", 1.6)
        self._run_script([
            alive(lambda p: p.go(at(70 * sc), variant="run")),
            alive(lambda p: p.go(at(36 * sc), variant="stalk")),
            alive(lambda p: (setattr(p, "dir", side), p.backoff(16 * sc))),       # aanloop: naar achteren lopen
            alive(lambda p: (setattr(p, "dir", side), p.begin("wiggle", random.uniform(0.9, 1.5)))),  # kont omhoog
            alive(lambda p: p.pounce_at_pointer()),
            lambda p: p.rebound(0),
        ])

    def pounce_at_pointer(self):
        p, st, sc = self.world.pointer, self.world.settings, self.scale
        if not p.active():
            return self.rest_after()
        face = 1 if p.x > self.cx() else -1
        self.dir = face
        ch, hy = self.cat_h(), self.pointer_height()
        maxh = 3.2 * ch * st.jump_height
        if hy > 1.1 * ch:   # hoog: de top van de sprong ligt bij de muis - hij springt er echt naartoe
            x1 = self.px_for_center(self.cx() + 2 * (p.x - self.cx()))
            height = max(0.5 * ch, min(hy - 0.5 * ch, maxh))
        else:               # laag: neerkomen op de muis
            x1 = self.px_for_center(p.x - face * 14 * sc)
            height = max(0.35 * ch, hy * 0.9)
        dur = (0.55 + 0.14 * height / ch + min(0.5, abs(x1 - self.x) / (900 * sc))) / st.jump_speed
        self.start_arc("pounce", x1, height, dur)

    def rebound(self, n):
        """Na de sprong: vaak meteen nog een sprong (de muis bewoog), anders vastpakken."""
        p = self.world.pointer
        if p.active() and n < 2 and random.random() < 0.55 and abs(p.x - self.cx()) < 12 * self.core():
            self.script = [
                lambda q: (setattr(q, "dir", 1 if q.world.pointer.x > q.cx() else -1), q.begin("wiggle", 0.5)),
                lambda q: q.pounce_at_pointer(),
                lambda q, n=n: q.rebound(n + 1)]
            return self.script.pop(0)(self)
        if self.near_pointer():
            self.begin("catch", random.uniform(2, 3.5))
        else:
            self.begin("sit", 2)

    def near_pointer(self):
        p = self.world.pointer
        return (p.active() and abs(p.x - (self.cx() + self.dir * 14 * self.scale)) < 16 * self.scale
                and self.pointer_height() < 1.2 * self.cat_h())

    def _track(self, variant, dur, offset=0.0):
        sc = self.scale
        self.track_variant, self.track_until = variant, time.monotonic() + dur
        self.track = lambda: self.px_for_center(self.world.pointer.x + offset * sc)
        self.go(self.track(), variant=variant)

    def mouse_chase(self):
        self.show_emote("alert", 1.4)
        self._track("run", random.uniform(4, 8))

    def mouse_follow(self):
        self._track("hop" if self.mood == "happy" else "walk", random.uniform(5, 9), offset=random.choice((-40, 40)))

    def mouse_stare(self):
        self.show_emote("question", 2.0)
        self._run_script([lambda p: p.go(p.px_for_center(p.world.pointer.x - p.dir * 20 * p.scale), variant="walk"),
                          lambda p: p.begin("stare", random.uniform(4, 8))])

    def mouse_bat(self):
        sc = self.scale
        side = 1 if self.cx() < self.world.pointer.x else -1
        self._run_script([
            lambda p: p.go(p.px_for_center(p.world.pointer.x - side * 24 * sc), variant="walk"),
            lambda p: (setattr(p, "dir", side), p.begin("reach", random.uniform(2.5, 4))),
        ])

    def startle(self):
        away = -1 if self.world.pointer.x > self.cx() else 1
        self.cancel()
        self.dir = -away
        self.show_emote("alert", 1.2)
        self.say("hiss", 0.6)
        tx = self.px_for_center(self.cx() + away * random.uniform(200, 500))
        self.script = [lambda p: p.go(tx, variant="run")]
        self.begin("hiss", 0.8)

    def react_pointer(self, now):
        if now - self._react_t < 0.3:
            return
        self._react_t = now
        w, p = self.world, self.world.pointer
        if (w.settings.track_mouse and p.active() and p.speed > 1800 and abs(p.x - self.cx()) < 450 * self.scale / 3
                and self.grounded() and not self.enc and not self.script
                and self.mood not in ("tired", "angry") and (self.state in REST or self.state in MOVING)
                and now - self.last.get("startle", -1e9) > 25 and random.random() < 0.1 * w.settings.mouse):
            self.last["startle"] = now
            self.startle()

    def update_track(self, now):
        if not self.track:
            return
        p = self.world.pointer
        if now > self.track_until or not p.active():
            self.track = None
            if self.state in MOVING or self.state == "stare":
                self.end_state()
            return
        tx = self.track()
        if self.state in MOVING:
            self.target = tx
            dist = abs(p.x - self.cx())
            if (self.state == "run" and 24 * self.scale < dist < 85 * self.scale
                    and self.pointer_height() < 3 * self.cat_h() and now - self.last.get("chase_pounce", -1e9) > 2.2):
                self.last["chase_pounce"] = now       # tijdens het rennen ineens naar de muis springen
                return self.pounce_at_pointer()
            if dist < 24 * self.scale:
                self.arrive()
        elif self.state == "stare" and abs(p.x - self.cx()) > 90 * self.scale:
            self.go(tx, variant=self.track_variant)

    # -- optillen / gooien
    def grab(self, px, py):
        if self.enc:
            self.enc.abort()
        self.cancel()
        self.held, self.hold, self.swing, self.swing_target = True, (px, py), 0.0, 0.0
        self.set_state("held")
        self.dur = 1e9
        self.posture = "stand"
        self.yoff = 0.0
        self.show_emote("anger" if self.mood == "angry" else "alert", 1.5)
        self.say("yowl", 0.7)
        self.set_mood("angry", 40, emote=False)
        self.hold_to(px, py, 0)

    def hold_to(self, px, py, vx):
        self.hold = (px, py)
        f, _ = self.frame()
        sc, pvx, pvy = self.scale, self.pivot[0], self.pivot[1]
        art_w = f.w / sc
        self.x = max(-f.w * 0.3, min(px - (pvx if self.dir > 0 else art_w - pvx) * sc, self.sw - f.w * 0.7))
        self.yoff = max(0.0, (self.sh - self.floor) - (py - pvy * sc + f.h))
        self.swing_target = max(-0.7, min(0.7, -vx * 0.0006))

    def release(self, vx, vy):
        k = self.world.settings.throw
        self.held = False
        self.vx, self.vy = vx * k, vy * k
        self.omega = max(-14.0, min(14.0, self.vx / 350))
        self.impact = 0.0
        self.set_state("fly")
        self.dur = 1e9

    def knock(self, vx, vy):
        """Door een andere kat geraakt (of gegooid): vliegt weg."""
        if self.enc:
            self.enc.abort()
        self.cancel()
        self.dir = -1 if vx > 0 else 1
        self.held = False
        self.vx, self.vy, self.omega, self.impact = vx, vy, vx / 350, 0.0
        self.set_state("fly")
        self.dur, self.posture = 1e9, "stand"
        self.show_emote("alert", 1.5)
        self.say("yowl", 0.8)
        self.set_mood("angry", 45, emote=False)

    def update_air(self, dt):
        self.vy -= 2800.0 * dt
        self.yoff += self.vy * dt
        self.x += self.vx * dt
        self.spin += self.omega * dt
        maxx = self.sw - self.fw
        if self.x < 0 or self.x > maxx:
            self.x = max(0.0, min(self.x, maxx))
            self.vx, self.omega = -self.vx * 0.6, -self.omega * 0.6
            self.say("thud", 0.7)
        top = (self.sh - self.floor) - self.frame()[0].h
        if self.yoff > top:
            self.yoff, self.vy = top, -abs(self.vy) * 0.4
        if self.yoff <= 0:
            self.yoff = 0.0
            self.impact = max(self.impact, -self.vy)
            if self.vy < -450:
                self.vy, self.vx, self.omega = -self.vy * 0.38, self.vx * 0.7, self.omega * 0.5
                self.say("thud")
            elif abs(self.vx) > 140:   # glijden/tuimelen over de vloer tot hij stilstaat
                self.vy = 0.0
                self.vx *= max(0.0, 1 - 3.0 * dt)
                self.omega *= max(0.0, 1 - 4.0 * dt)
            else:
                self.land()

    def land(self):
        self.vx = self.vy = self.omega = self.spin = 0.0
        self.say("thud")
        if self.impact > 1100 or random.random() < 0.5:
            self.posture = "sit"
            self.set_state("dizzy", random.uniform(2.5, 4))
        else:
            self.posture = "stand"
            self.set_state("angry", random.uniform(2, 4))
        self.set_mood("angry", 45)

    def update_held(self, dt):
        self.swing += (self.swing_target - self.swing) * min(1.0, 10 * dt)
        self.swing_target *= 0.9

    # -- klikken
    def on_click(self):
        """Korte linkerklik = aaien (een boze kat sist en slaat juist)."""
        if self.mood == "angry" and random.random() < 0.8:
            self.cancel()
            self.script = [lambda p: p.begin("angry", 3)]
            self.begin("swat", 1.2)
            self.say("hiss")
            self.show_emote("anger")
            return
        self.social = max(0.0, self.social - 0.4)
        if self.mood in ("sad", "calm", "tired"):
            self.set_mood("happy", 45)
        self.events.append(("purr", 2.0))   # >1.5 = direct gevolg van jouw klik: altijd horen
        self.cancel()
        if self.state == "purr":
            self.dur += 2.5
        else:
            self.begin("purr", random.uniform(5, 8))

    # -- per beeld
    def update(self, dt):
        now = time.monotonic()
        self.t += dt
        self.update_mood(dt)
        if self.emote and now > self.emote[1]:
            self.emote = None
        if self.held:
            self.update_held(dt)
        elif self.state == "fly":
            self.update_air(dt)
        elif self.arc:
            self.update_arc()
        elif self.state in MOVING:
            self.update_move(dt)
        elif self.t >= self.dur:
            self.end_state()
        if self.eat_call and now > self.eat_call:      # kibble gehoord: zodra hij even rust heeft naar de bak
            if not self.bowl or self.bowl.empty or self.bowl.occupied is not None or now > self.eat_call + 30:
                self.eat_call = 0.0
            elif self.state in REST and self.grounded() and not self.enc:
                self.eat_call = 0.0
                self.go_to_bowl()
        if self.grounded() and not self.held:
            self.update_track(now)
            self.react_pointer(now)
            if self.state == "eat" and self.bowl:  # de bak raakt langzaam leeg
                a = self.anims["eat"]
                outro_t = a.outro / a.tfps
                if a.intro / a.tfps < self.t < self.dur - outro_t:
                    self.eat_acc += dt
                    if self.eat_acc > 3.0:
                        self.eat_acc = 0.0
                        self.bowl.consume()
                        self.hunger = max(0.0, self.hunger - 0.3)
                        if self.hunger < 0.3:
                            self.set_mood("happy", 50)
                    if self.bowl.empty:
                        self.dur = min(self.dur, self.t + outro_t)
        _f, idx = self.frame()
        if idx != self._idx:
            self._idx = idx
            snd = self.anims[self.state].sfx.get(idx)
            if snd and random.random() < snd[1]:
                self.events.append(snd)
            if self.state == "clatter" and self.bowl and idx in (1, 3):
                self.bowl.shake(0.35)

    # -- tekenen
    def draw(self, ctx):
        import cairo
        f, _ = self.frame()
        x, y, w, h = self.rect()
        surf = f.flipped() if self.dir < 0 else f.surf
        pivot, ang = None, 0.0
        if self.held and self.hold:
            pivot, ang = self.hold, self.swing
        elif self.state == "fly":
            pivot, ang = (x + w / 2, y + h / 2), self.spin
        if pivot and abs(ang) > 0.001:
            ctx.save()
            ctx.translate(*pivot)
            ctx.rotate(ang)
            ctx.set_source_surface(surf, x - pivot[0], y - pivot[1])
            ctx.get_source().set_filter(cairo.FILTER_NEAREST)
            ctx.paint()
            ctx.restore()
        else:
            ctx.set_source_surface(surf, x, y)
            ctx.paint()

    def draw_emote(self, ctx):
        """Klein, doorzichtig icoontje achter/boven het hoofd - een sfeertje op de achtergrond."""
        if not self.emote:
            return
        name, end, start = self.emote
        now = time.monotonic()
        a = min(1.0, (now - start) / 0.3, (end - now) / 0.6) * 0.5
        if a <= 0:
            return
        e = emote_frame(name, max(1, self.scale - 1))
        x, y, w, h = self.rect()
        ex = x + w * (0.42 if self.dir > 0 else 0.58) - e.w / 2
        ey = y - e.h + 6 * self.scale - (now - start) * 7
        ctx.set_source_surface(e.surf, ex, ey)
        ctx.paint_with_alpha(a)


# --------------------------------------------------------------------------- #
#  Contact tussen katten: over elkaar springen, spelen, vechten
# --------------------------------------------------------------------------- #

class Encounter:
    """Een scene met twee katten (spelen of vechten), stap voor stap."""

    def __init__(self, world, kind, a, b):
        self.w, self.kind, self.a, self.b = world, kind, a, b
        self.phase, self.t, self.dur, self.data, self.age = None, 0.0, None, {}, 0.0
        for p in (a, b):
            if p.enc:
                p.enc.abort()
            p.cancel()
            p.enc = self
        world.encs.append(self)
        world.cooldown[frozenset((a.index, b.index))] = time.monotonic() + (90 if kind == "fight" else 0)
        self.enter("approach")

    # -- hulpjes
    def wait(self, p):
        p.begin("idle", 9999)

    def meet_x(self):
        a, b, sc = self.a, self.b, self.a.scale
        side = 1 if a.cx() < b.cx() else -1
        return a.px_for_center(b.cx() - side * 30 * sc)

    def enter(self, phase, dur=None):
        self.phase, self.t, self.dur = phase, 0.0, dur
        getattr(self, "p_" + phase)()

    def update(self, dt):
        self.t += dt
        self.age += dt
        if self.phase == "chase":
            self.b.target = self.a.x
        if self.phase == "swat" and random.random() < dt * 0.8:
            random.choice((self.a, self.b)).say("yowl")
        if self.age > 45 or (self.phase == "approach" and self.t > 14):
            return self.finish()
        if self.dur is not None and self.t >= self.dur:
            getattr(self, "n_" + self.phase)()

    def abort(self):
        self.finish(happy=False)

    def finish(self, happy=True):
        if self not in self.w.encs:
            return
        self.w.encs.remove(self)
        for p in (self.a, self.b):
            p.enc = None
            if happy and self.kind == "play":
                p.set_mood("happy", 50)
                p.social = max(0.0, p.social - 0.5)
                p.show_emote("heart")
        for p in (self.a, self.b):
            if p.state in ("idle", "stare", "angry") and p.dur > 1000 and not p.held:
                p.end_state()

    def pet_done(self, pet):
        ph = self.phase
        if ph == "approach" and pet is self.a:
            return self.enter("invite" if self.kind == "play" else "hiss", 2.2 if self.kind == "play" else 2.4)
        if ph == "leap1b" and pet is self.a:
            return self.enter("leap2", 1.5)
        if ph == "leap2b" and pet is self.b:
            return self.finish()
        if ph == "outcome" and pet is self.data.get("loser"):
            return self.finish()
        self.wait(pet)

    # -- spelen
    def p_approach(self):
        a, b = self.a, self.b
        if self.kind == "play":
            self.wait(b)
            b.face(a)
            a.go(self.meet_x(), "meet", "walk" if a.mood != "energetic" else "run")
        else:  # vechten: de aanvaller rent op de ander af
            b.begin("angry" if b.mood == "angry" else "idle", 9999)
            b.face(a)
            a.show_emote("anger")
            b.show_emote("alert")
            a.go(self.meet_x(), "meet", "run" if a.energy > 0.35 else "walk")

    def p_invite(self):
        a, b = self.a, self.b
        a.face(b)
        b.face(a)
        a.begin("invite", 2.2)
        self.data["declined"] = b.energy < 0.3 or b.mood in ("tired", "sad", "angry")
        b.begin("stare" if self.data["declined"] else "invite", 2.2)
        a.show_emote("note")

    def n_invite(self):
        if self.data["declined"]:
            self.a.set_mood("calm", 20, emote=False)
            return self.finish(happy=False)
        game = random.choice(("wrestle", "chase", "leap"))
        self.data["step"] = 0
        self.enter(game, {"wrestle": 3.6, "chase": 6.0, "leap": 1.5}[game])

    def p_wrestle(self):
        down, up = (self.a, self.b) if self.data["step"] == 0 else (self.b, self.a)
        down.face(up)
        up.face(down)
        down.begin("roll", 3.6)
        up.begin("catch", 3.6)
        self.w.settings and down.say("trill", 0.6)

    def n_wrestle(self):
        self.data["step"] += 1
        if self.data["step"] >= 2:
            return self.finish()
        self.enter("wrestle", 3.6)

    def p_chase(self):
        a, b, sc = self.a, self.b, self.a.scale
        away = -1 if b.cx() > a.cx() else 1
        far = a.px_for_center(a.cx() + away * 9 * a.fw)
        a.go(far, "wander", "run")
        b.face(a)
        b.go(a.x, "wander", "run")
        a.say("chirp")

    def n_chase(self):
        self.finish()

    def p_leap(self):
        self.a.face(self.b)
        self.b.face(self.a)
        self.a.begin("wiggle", 1.5)
        self.b.begin("stare", 5)

    def n_leap(self):
        a, b, sc, st = self.a, self.b, self.a.scale, self.w.settings
        d = 1 if b.cx() > a.cx() else -1
        land = a.px_for_center(b.cx() + d * ((a.core() + b.core()) / 2 + 8 * sc) - 0)
        a.start_arc("pounce", land, 28 * sc * st.jump_height, 0.9 / st.jump_speed)
        self.phase, self.dur = "leap1b", None

    def p_leap1b(self):
        pass

    def p_leap2(self):
        a, b = self.a, self.b
        b.face(a)
        a.face(b)
        b.begin("wiggle", 1.4)
        a.begin("stare", 5)

    def n_leap2(self):
        a, b, sc, st = self.a, self.b, self.a.scale, self.w.settings
        d = 1 if a.cx() > b.cx() else -1
        land = b.px_for_center(a.cx() + d * ((a.core() + b.core()) / 2 + 8 * sc))
        b.start_arc("pounce", land, 28 * sc * st.jump_height, 0.9 / st.jump_speed)
        self.phase, self.dur = "leap2b", None

    def p_leap2b(self):
        pass

    # -- vechten
    def p_hiss(self):
        a, b = self.a, self.b
        a.face(b)
        b.face(a)
        a.begin("hiss", 2.4)
        b.begin("hiss", 2.4)
        a.say("growl")
        b.set_mood("angry", 40, emote=False)

    def n_hiss(self):
        self.enter("swat", random.uniform(4, 6) * max(0.5, self.w.settings.fight))

    def p_swat(self):
        self.a.begin("swat", self.dur)
        self.b.begin("swat", self.dur)

    def n_swat(self):
        a, b = self.a, self.b
        wa = a.energy * 0.6 + (0.4 if a.mood == "angry" else 0.0) + random.random() * 0.5
        wb = b.energy * 0.6 + (0.4 if b.mood == "angry" else 0.0) + random.random() * 0.5
        win, lose = (a, b) if wa >= wb else (b, a)
        self.data["loser"] = lose
        win.face(lose)
        win.begin("hiss", 1.4)
        win.set_mood(random.choice(("happy", "angry")), 50)
        lose.set_mood("sad" if random.random() < 0.4 else "calm", 25)
        lose.say("yowl")
        away = -1 if lose.cx() > win.cx() else 1
        lose.dir = away
        lose.go(lose.px_for_center(lose.cx() + away * 10 * lose.fw), "wander", "run")
        self.phase, self.dur = "outcome", 10.0

    def p_outcome(self):
        pass

    def n_outcome(self):
        self.finish()


class World:
    """Alles waar katten elkaar en de muis tegenkomen."""

    def __init__(self, sw, sh, floor, settings, pointer, bowl):
        self.sw, self.sh, self.floor, self.settings, self.pointer, self.bowl = sw, sh, floor, settings, pointer, bowl
        self.pets, self.encs, self.cooldown, self._social_t, self._was_empty = [], [], {}, 0.0, False

    def blocking(self, pet, newx):
        """Staat er een andere kat in de weg?"""
        cx = newx + pet.fw / 2
        for o in self.pets:
            if o is pet or not o.grounded():
                continue
            lim = (pet.core() + o.core()) / 2 * 0.9
            if abs(cx - o.cx()) < lim and abs(cx - o.cx()) < abs(pet.cx() - o.cx()):
                return o
        return None

    def on_block(self, pet, other):
        if pet.enc is not None:
            if other in (pet.enc.a, pet.enc.b) and pet.enc.phase != "chase":
                return                     # de partner zelf: gewoon blijven staan
            return pet.jump_over(other)   # een derde kat in de weg (of jagen): erover heen
        if other.enc is None and self.pair_ready(pet, other):
            angry = pet if pet.mood == "angry" else other if other.mood == "angry" else None
            fight_p = self.settings.fight * (0.5 if angry else 0.05)
            fun = {"energetic": 1.0, "happy": 0.7, "calm": 0.12}
            play_p = (self.settings.play * fun.get(pet.mood, 0.0) * (0 if other.mood in ("angry", "sad", "tired") else 1)
                      * (0 if other.state.startswith("sleep") else 1))
            roll = random.random()
            if roll < fight_p:
                att = angry or pet
                return Encounter(self, "fight", att, other if att is pet else pet)
            if roll < fight_p + play_p:
                return Encounter(self, "play", pet, other)
        pet.jump_over(other)

    def pair_ready(self, a, b):
        return time.monotonic() - self.cooldown.get(frozenset((a.index, b.index)), -1e9) > 90

    def can_social(self, p):
        return p.grounded() and not p.enc and not p.script and not p.queue and not p.held and p.state != "eat"

    def start_encounter(self, kind, a, b):
        if a is not b:
            Encounter(self, kind, a, b)

    def social_tick(self):
        for i, a in enumerate(self.pets):
            for b in self.pets[i + 1:]:
                if not (self.can_social(a) and self.can_social(b)) or not self.pair_ready(a, b):
                    continue
                if abs(a.cx() - b.cx()) > 1200:
                    continue
                angry = a if a.mood == "angry" else b if b.mood == "angry" else None
                if angry and random.random() < 0.15 * self.settings.fight:
                    other = b if angry is a else a
                    return self.start_encounter("fight", angry, other)
                asleep = a.state.startswith("sleep") or b.state.startswith("sleep")
                if (not asleep and a.mood in ("happy", "energetic") and b.mood in ("happy", "energetic", "calm")
                        and random.random() < 0.15 * self.settings.play):
                    return self.start_encounter("play", a, b)

    def nearest_other(self, pet):
        others = [p for p in self.pets if p is not pet]
        return min(others, key=lambda o: abs(o.cx() - pet.cx())) if others else None

    def update(self, dt):
        now = time.monotonic()
        self.pointer.poll(now)
        if self.bowl:
            self.bowl.update(dt)
            if self.bowl.empty:
                self._was_empty = True
            elif self._was_empty and self.bowl.fill >= 0.7:   # net gevuld: hongerige katten komen kijken
                self._was_empty = False
                for p in self.pets:
                    if p.hunger > 0.35 and random.random() < 0.85:
                        p.eat_call = now + random.uniform(1.0, 7.0)
        for p in self.pets:
            p.update(dt)
        for e in list(self.encs):
            e.update(dt)
        # gegooide katten raken andere katten ("uit de weg gooien")
        for a in self.pets:
            if a.state != "fly" or math.hypot(a.vx, a.vy) < 450:
                continue
            ar = a.rect()
            for b in self.pets:
                if b is a or b.state == "fly" or b.held:
                    continue
                br = b.rect()
                if abs((ar[0] + ar[2] / 2) - (br[0] + br[2] / 2)) < (a.core() + b.core()) / 2 and \
                        ar[1] + ar[3] > br[1] + br[3] * 0.25 and ar[1] < br[1] + br[3]:
                    sgn = 1 if a.vx >= 0 else -1
                    b.knock(sgn * (abs(a.vx) * 0.8 + 250), 450 + abs(a.vx) * 0.15)
                    a.vx *= -0.35
                    a.say("thud")
        # katten die door elkaar staan schuiven vanzelf uit elkaar
        for i, a in enumerate(self.pets):
            for b in self.pets[i + 1:]:
                if not (a.grounded() and b.grounded()) or a.state in MOVING or b.state in MOVING:
                    continue
                dx = b.cx() - a.cx()
                over = (a.core() + b.core()) / 2 - abs(dx)
                if over > 0.3 * a.core():
                    push = 160 * dt * (1 if dx >= 0 else -1)
                    a.x = max(0.0, min(a.x - push, self.sw - a.fw))
                    b.x = max(0.0, min(b.x + push, self.sw - b.fw))
        if now - self._social_t > 4:
            self._social_t = now
            self.social_tick()


# --------------------------------------------------------------------------- #
#  Menu (zelf getekend, werkt overal op Wayland) - rechtermuisknop op een kat
# --------------------------------------------------------------------------- #

class Menu:
    PAD, WIDTH = 14, 310

    def __init__(self, app):
        self.app, self.open, self.pet, self.drag, self.tab = app, False, None, None, 0
        self.items, self.rect, self.last, self.anchor = [], (0, 0, 0, 0), 0.0, None
        self.pend_size = self.pend_bowl = None

    def show(self, pet):
        self.pet, self.open, self.last, self.tab = pet, True, time.monotonic(), 0
        self.pend_size = self.pend_bowl = None
        self.layout()

    # -- inhoud per tab
    def _cat_actions(self):
        pet, app = self.pet, self.app
        w = app.world

        def with_other(kind):
            o = w.nearest_other(pet)
            if o is None:
                pet.show_emote("question")
            else:
                w.start_encounter(kind, pet, o)

        def cycle_mood():
            order = [None] + MOODS
            cur = order.index(pet.mood_lock) if pet.mood_lock in order else 0
            pet.mood_lock = order[(cur + 1) % len(order)]
            if pet.mood_lock:
                pet.mood = pet.mood_lock
                pet.show_emote(MOOD_EMOTE.get(pet.mood_lock, "heart"))

        return [
            ("Aaien", lambda: pet.on_click()),
            ("Eten", lambda: pet.go_to_bowl() if app.bowl else None),
            ("Rekken", lambda: (pet.cancel(), pet.begin("stretch_back"))),
            ("Slapen", lambda: (pet.cancel(), pet.begin("yawn"), pet.script.extend(
                [lambda p: p.begin("stretch_back"),
                 lambda p: p.begin(random.choice(("sleep_loaf", "sleep_curl")), 25 * p.calm)]))),
            ("Likken", lambda: (pet.cancel(), pet.begin(random.choice(("lick_paw", "lick_tail", "wash")), 7))),
            ("Hupsen", lambda: (pet.cancel(), pet.go(random.uniform(0, max(1, pet.sw - 200)), variant="hop"))),
            ("Jagen", lambda: (pet.cancel(), pet.start_mouse_play("hunt" if pet.world.pointer.active() else None))),
            ("Spelen", lambda: with_other("play")),
            ("Vechten", lambda: with_other("fight")),
        ], cycle_mood

    def build(self):
        app, pet, ui = self.app, self.pet, self.app.ui
        pw, pad = self.WIDTH * ui, self.PAD * ui
        S = app.world.settings
        items, y = [], 0.0
        items.append(dict(kind="title", rect=(0, y, pw, 34 * ui)))
        y += 34 * ui
        tw = (pw - 2 * pad - 6 * ui) / 2
        for i, label in enumerate(("Kat", "Wereld")):
            items.append(dict(kind="tab", idx=i, label=label, rect=(pad + i * (tw + 6 * ui), y, tw, 26 * ui)))
        y += 32 * ui

        def slider(label, get, set_, lo, hi, step, fmt, done=None):
            nonlocal y
            items.append(dict(kind="slider", label=label, get=get, set=set_, done=done, lo=lo, hi=hi, step=step,
                              fmt=fmt, rect=(0, y, pw, 44 * ui)))
            y += 44 * ui

        def buttons(defs, per=3):
            nonlocal y
            bw = (pw - 2 * pad - (per - 1) * 6 * ui) / per
            for i, (label, fn) in enumerate(defs):
                row, col = divmod(i, per)
                items.append(dict(kind="btn", label=label, fn=fn,
                                  rect=(pad + col * (bw + 6 * ui), y + row * 36 * ui, bw, 30 * ui)))
            y += ((len(defs) + per - 1) // per) * 36 * ui + 4 * ui

        if self.tab == 0:
            slider("Snelheid", lambda: pet.speed_mult, lambda v: pet.set_speed(v), 0.3, 2.5, 0.05, "{:.2f}x")
            slider("Grootte", lambda: self.pend_size or pet.scale,
                   lambda v: setattr(self, "pend_size", int(round(v))), 1, 8, 1, "{:.0f}", self._apply_size)
            slider("Rust", lambda: pet.calm, lambda v: setattr(pet, "calm", v), 0.3, 3.0, 0.1, "{:.1f}")
            y += 4 * ui
            acts, cycle = self._cat_actions()
            lock = pet.mood_lock
            items.append(dict(kind="btn", label=("Stemming: vast " + MOOD_NL[lock]) if lock else "Stemming: automatisch",
                              fn=cycle, rect=(pad, y, pw - 2 * pad, 30 * ui)))
            y += 38 * ui
            buttons(acts)
        else:
            slider("Volume", lambda: app.sfx.volume, lambda v: setattr(app.sfx, "volume", v), 0.0, 1.0, 0.05,
                   "{:.0%}", lambda: app.sfx.play("purr", pet.index, forced=True))
            if app.bowl:
                slider("Voerbak", lambda: self.pend_bowl or app.bowl.scale,
                       lambda v: setattr(self, "pend_bowl", int(round(v))), 1, 8, 1, "{:.0f}", self._apply_bowl)
            slider("Springhoogte", lambda: S.jump_height, lambda v: setattr(S, "jump_height", v), 0.5, 2.0, 0.05, "{:.2f}x")
            slider("Springtempo", lambda: S.jump_speed, lambda v: setattr(S, "jump_speed", v), 0.5, 2.0, 0.05, "{:.2f}x")
            slider("Vechtlust", lambda: S.fight, lambda v: setattr(S, "fight", v), 0.0, 2.0, 0.05, "{:.2f}x")
            slider("Speelsheid", lambda: S.play, lambda v: setattr(S, "play", v), 0.0, 2.0, 0.05, "{:.2f}x")
            slider("Muis-interesse", lambda: S.mouse, lambda v: setattr(S, "mouse", v), 0.0, 2.0, 0.05, "{:.2f}x")
            slider("Gooikracht", lambda: S.throw, lambda v: setattr(S, "throw", v), 0.5, 2.0, 0.05, "{:.2f}x")
            slider("Geluidsdrukte", lambda: app.sfx.rate, lambda v: setattr(app.sfx, "rate", v), 0.0, 1.0, 0.05, "{:.0%}")
            y += 4 * ui
            buttons([("Geluid aan/uit", self._toggle_sound),
                     ("Muis aan/uit", lambda: setattr(S, "track_mouse", not S.track_mouse))])
        return items, y + 6 * ui

    def layout(self):
        pet, ui = self.pet, self.app.ui
        self.items, ph = self.build()
        pw = self.WIDTH * ui
        if self.anchor is None or self.tab == 0 and self.anchor[2] != pet:
            c = pet.rect()
            self.anchor = (c[0] + c[2] / 2, c, pet)
        cx, c, _ = self.anchor
        px = max(8, min(cx - pw / 2, pet.sw - pw - 8))
        py = c[1] - ph - 10
        if py < 8:
            py = min(max(8, pet.sh - ph - 8), c[1] + c[3] + 10)
        self.rect = (px, py, pw, ph)

    # -- acties
    def _apply_size(self):
        if self.pend_size and self.pet and self.pend_size != self.pet.scale:
            self.pet.set_scale(self.pend_size)
        self.pend_size = None

    def _apply_bowl(self):
        if self.pend_bowl and self.app.bowl and self.pend_bowl != self.app.bowl.scale:
            self.app.bowl.set_scale(self.pend_bowl)
        self.pend_bowl = None

    def _toggle_sound(self):
        sfx = self.app.sfx
        if sfx.player:
            sfx.enabled = not sfx.enabled

    def close(self):
        self._apply_size()
        self._apply_bowl()
        self.open, self.drag, self.pet, self.anchor = False, None, None, None

    # -- muis
    def _abs(self, r):
        return (self.rect[0] + r[0], self.rect[1] + r[1], r[2], r[3])

    def _set_from_x(self, it, px):
        pad = self.PAD * self.app.ui
        frac = max(0.0, min(1.0, (px - (self.rect[0] + pad)) / (self.rect[2] - 2 * pad)))
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
                elif it["kind"] == "tab":
                    self.tab = it["idx"]
                    self.layout()
                elif it["kind"] == "slider":
                    self.drag = it
                    self._set_from_x(it, px)
                elif it["kind"] == "btn":
                    it["fn"]()
                    self.layout()
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
            ctx.move_to(tx - (ext.width if align == "r" else ext.width / 2 if align == "c" else 0), ty)
            ctx.show_text(s)

        for it in self.items:
            ix, iy, iw, ih = self._abs(it["rect"])
            if it["kind"] == "title":
                m = MOOD_NL[self.pet.mood] + (" (vast)" if self.pet.mood_lock else "")
                text(f"{self.pet.coat.name.capitalize()} - {m}", x + pad, iy + 22 * ui, (0.93, 0.58, 0.22))
                text("x", x + w - pad - 6 * ui, iy + 22 * ui, align="c")
            elif it["kind"] == "tab":
                on = it["idx"] == self.tab
                ctx.set_source_rgb(*((0.93, 0.58, 0.22) if on else (0.22, 0.20, 0.28)))
                ctx.rectangle(ix, iy, iw, ih)
                ctx.fill()
                text(it["label"], ix + iw / 2, iy + ih / 2 + 4 * ui, (0.1, 0.09, 0.12) if on else (0.96, 0.93, 0.88), "c")
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
                elif label.startswith("Muis aan"):
                    label = "Muis: " + ("aan" if self.app.world.settings.track_mouse else "uit")
                ctx.set_font_size(12 * ui)
                text(label, ix + iw / 2, iy + ih / 2 + 4 * ui, align="c")
                ctx.set_font_size(13 * ui)


# --------------------------------------------------------------------------- #
#  Applicatie (GTK / layer-shell)
# --------------------------------------------------------------------------- #

class BowlMenu:
    """Popup bij de voerbak (rechtermuisknop): haal de zak kattenvoer eruit en strooi hem in de bak."""
    WIDTH = 260

    def __init__(self, app):
        self.app, self.open, self.items, self.rect, self.last = app, False, [], (0, 0, 0, 0), 0.0

    def show(self):
        app, ui = self.app, self.app.ui
        b = app.bowl
        pw, pad = self.WIDTH * ui, 14 * ui
        self.items = [dict(kind="title", rect=(0, 0, pw, 34 * ui)),
                      dict(kind="fill", rect=(pad, 40 * ui, pw - 2 * pad, 14 * ui)),
                      dict(kind="bag", rect=(pad, 62 * ui, pw - 2 * pad, 112 * ui)),
                      dict(kind="auto", rect=(pad, 182 * ui, pw - 2 * pad, 30 * ui))]
        ph = 222 * ui
        bx, by, bw, bh = b.rect()
        px = max(8, min(bx + bw / 2 - pw / 2, b.sw - pw - 8))
        py = max(8, by - ph - 12)
        self.rect, self.open, self.last = (px, py, pw, ph), True, time.monotonic()

    def close(self):
        self.open = False

    def _abs(self, r):
        return (self.rect[0] + r[0], self.rect[1] + r[1], r[2], r[3])

    def press(self, px, py):
        """'bag' = zak gepakt, 'inside' = klik in de popup, 'outside' = erbuiten (popup sluit)."""
        if not self.open:
            return "outside"
        x, y, w, h = self.rect
        if not (x <= px <= x + w and y <= py <= y + h):
            self.close()
            return "outside"
        self.last = time.monotonic()
        for it in self.items:
            ix, iy, iw, ih = self._abs(it["rect"])
            if ix <= px <= ix + iw and iy <= py <= iy + ih:
                if it["kind"] == "title" and px > ix + iw - 40 * self.app.ui:
                    self.close()
                elif it["kind"] == "bag":
                    return "bag"
                elif it["kind"] == "auto":
                    self.app.bowl.auto = not self.app.bowl.auto
                break
        return "inside"

    def draw(self, ctx):
        if not self.open:
            return
        import cairo
        app, ui = self.app, self.app.ui
        x, y, w, h = self.rect
        pad, b = 14 * ui, app.bowl
        r = 10 * ui
        ctx.new_sub_path()
        for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
            ctx.arc(cx, cy, r, math.radians(a0), math.radians(a0 + 90))
        ctx.close_path()
        ctx.set_source_rgba(0.11, 0.10, 0.14, 0.97)
        ctx.fill_preserve()
        ctx.set_source_rgb(0.36, 0.58, 0.85)
        ctx.set_line_width(2 * ui)
        ctx.stroke()
        ctx.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(13 * ui)

        def text(s_, tx, ty, col=(0.96, 0.93, 0.88), align="l"):
            ext = ctx.text_extents(s_)
            ctx.set_source_rgb(*col)
            ctx.move_to(tx - (ext.width if align == "r" else ext.width / 2 if align == "c" else 0), ty)
            ctx.show_text(s_)

        for it in self.items:
            ix, iy, iw, ih = self._abs(it["rect"])
            if it["kind"] == "title":
                text("Voerbak", x + pad, iy + 22 * ui, (0.36, 0.58, 0.85))
                text("x", x + w - pad - 6 * ui, iy + 22 * ui, align="c")
            elif it["kind"] == "fill":
                ctx.set_source_rgb(0.26, 0.25, 0.32)
                ctx.rectangle(ix, iy, iw, ih)
                ctx.fill()
                ctx.set_source_rgb(0.72, 0.45, 0.22)
                ctx.rectangle(ix, iy, iw * b.fill, ih)
                ctx.fill()
                ctx.set_font_size(11 * ui)
                text("leeg" if b.empty else f"{int(b.fill * 100)}%", ix + iw / 2, iy + ih - 3 * ui, align="c")
                ctx.set_font_size(13 * ui)
            elif it["kind"] == "bag":
                f = app.bag_frame(max(2, int(4 * ui)))
                ctx.set_source_surface(f.surf, ix + iw / 2 - f.w / 2, iy + 2 * ui)
                ctx.paint()
                ctx.set_font_size(11 * ui)
                text("Pak de zak en strooi hem", ix + iw / 2, iy + ih - 14 * ui, align="c")
                text("boven de bak leeg", ix + iw / 2, iy + ih - 1 * ui, align="c")
                ctx.set_font_size(13 * ui)
            elif it["kind"] == "auto":
                ctx.set_source_rgb(0.22, 0.20, 0.28)
                ctx.rectangle(ix, iy, iw, ih)
                ctx.fill()
                ctx.set_font_size(12 * ui)
                text("Automatisch bijvullen: " + ("aan" if b.auto else "uit"), ix + iw / 2, iy + ih / 2 + 4 * ui, align="c")
                ctx.set_font_size(13 * ui)


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
    settings = Settings(args)
    app.bowl = None if args.no_bowl else Bowl(
        sw, sh, args.floor, scale, args.bowl_x or random.uniform(0.25, 0.75), root, args.auto_refill)
    world = World(sw, sh, args.floor, settings, Pointer(geo, settings.track_mouse), app.bowl)
    app.world = world

    coats = make_coats(args.cats, args.coats.split(",") if args.coats else None)
    for i, coat in enumerate(coats):
        anims, cfg = build_cat(coat, scale, find_folder(root, i, coat))
        world.pets.append(Pet(i, coat, anims, cfg, world, args.scale or cfg.get("scale", 3), args.speed, args.calm))
    pets = world.pets
    print("[catpet] katten: " + ", ".join(c.name for c in coats))
    pitches = [1.0] + [round(random.uniform(0.8, 1.3), 2) for _ in coats[1:]]
    app.sfx = Sfx(not args.no_sfx, args.volume, pitches, Path(args.sounds), args.sfx_rate)
    menu = Menu(app)
    bowl_menu = BowlMenu(app)
    carry, app.parts, bag_cache = {"c": None}, [], {}

    def bag_frame(bs):
        if bs not in bag_cache:
            bag_cache[bs] = Frame(canvas_surface(bag_canvas(), bs, BAG_PAL))
        return bag_cache[bs]
    app.bag_frame = bag_frame

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

    drag = {"d": None}

    def on_draw(_w, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        for p in pets:            # emoties: kleine doorzichtige laag achter alles
            p.draw_emote(ctx)
        eaters = [p for p in pets if p.state in ("eat", "clatter")]
        for p in eaters:          # de etende kat eerst: de bak komt dan voor zijn snuit
            p.draw(ctx)
        if app.bowl:
            app.bowl.draw(ctx)
        order = sorted((p for p in pets if p not in eaters),
                       key=lambda p: (p.held or p.state == "fly", p.state == "purr"))
        for p in order:
            p.draw(ctx)
        if app.parts and app.bowl:
            size = max(2, app.bowl.scale)
            ctx.set_source_rgb(*BOWL_PAL["Y"])
            for pt in app.parts:
                ctx.rectangle(pt[0] - size / 2, pt[1] - size / 2, size, size)
                ctx.fill()
        c = carry["c"]
        if c:
            import cairo as _c
            f = bag_frame(c["bs"])
            ctx.save()
            ctx.translate(c["x"], c["y"])
            ctx.rotate(c["angle"])
            ctx.set_source_surface(f.surf, -f.w / 2, -f.h / 2)
            ctx.get_source().set_filter(_c.FILTER_NEAREST)
            ctx.paint()
            ctx.restore()
        menu.draw(ctx)
        bowl_menu.draw(ctx)
        return False

    def full_region():
        return cairo.Region(cairo.RectangleInt(0, 0, sw, sh))

    def update_input_region():
        if menu.open or bowl_menu.open or carry["c"] or drag["d"]:  # volledig scherm: klik ernaast sluit, slepen overal
            win.input_shape_combine_region(full_region())
            return
        total = cairo.Region()
        for p in pets:
            f, _ = p.frame()
            x, y, w, h = p.rect()
            reg = f.region(p.dir < 0) if p.grounded() else None
            if reg is None:
                reg = cairo.Region(cairo.RectangleInt(x, y, w, h))
            else:
                reg = reg.copy()
                reg.translate(x, y)
            total.union(reg)
        if app.bowl:
            bx, by, bw, bh = app.bowl.rect()
            total.union(cairo.Region(cairo.RectangleInt(bx, by, bw, bh)))   # rechtermuisknop op de bak
        win.input_shape_combine_region(total)

    def pet_at(x, y):
        for p in reversed(pets):
            if p.hit(x, y):
                return p
        return None

    def bowl_hit(x, y):
        if not app.bowl:
            return False
        bx, by, bw, bh = app.bowl.rect()
        return bx <= x < bx + bw and by <= y < by + bh

    def on_press(_w, event):
        if carry["c"]:                                  # tweede klik: zak neerzetten
            carry["c"] = None
            update_input_region()
            win.queue_draw()
            return True
        if bowl_menu.open:
            res = bowl_menu.press(event.x, event.y)
            if res == "bag":
                carry["c"] = dict(x=event.x, y=event.y, angle=0.0, emit=0.0, pickup=True, moved=0.0,
                                  last=(event.x, event.y), t0=time.monotonic(), bs=max(3, app.bowl.scale + 1),
                                  hits=0)
                bowl_menu.close()
            if res != "outside":
                update_input_region()
                win.queue_draw()
                return True
        if menu.open and menu.press(event.x, event.y):
            win.queue_draw()
            return True
        p = pet_at(event.x, event.y)
        if p and event.button == 3:                 # rechtermuisknop op een kat = menu
            bowl_menu.close()
            menu.show(p)
        elif p and event.button == 1:               # linkermuisknop: klik = aaien, slepen = optillen
            drag["d"] = dict(pet=p, x0=event.x, y0=event.y, moved=False, s=[(time.monotonic(), event.x, event.y)])
        elif event.button == 3 and bowl_hit(event.x, event.y):   # rechtermuisknop op de voerbak = zak pakken
            menu.close()
            bowl_menu.show()
        update_input_region()
        win.queue_draw()
        return True

    def on_motion(_w, event):
        c = carry["c"]
        d = drag["d"]
        if c:
            c["moved"] += math.hypot(event.x - c["last"][0], event.y - c["last"][1])
            c["last"], c["x"], c["y"] = (event.x, event.y), event.x, event.y
        elif d:
            now = time.monotonic()
            d["s"] = [s_ for s_ in d["s"] if now - s_[0] < 0.25] + [(now, event.x, event.y)]
            if not d["moved"] and math.hypot(event.x - d["x0"], event.y - d["y0"]) > 8:
                d["moved"] = True
                d["pet"].grab(d["x0"], d["y0"])
            if d["moved"]:
                t0, x0, _y0 = d["s"][0]
                vx = (event.x - x0) / max(0.03, now - t0)
                d["pet"].hold_to(event.x, event.y, vx)
        elif menu.drag:
            menu.motion(event.x, event.y)
        win.queue_draw()
        return True

    def on_release(_w, event):
        c = carry["c"]
        d = drag["d"]
        if c:
            if c["pickup"]:                      # losknop van het oppakken: slepen = loslaten, klikken = blijft hangen
                c["pickup"] = False
                if c["moved"] > 40:
                    carry["c"] = None
        elif d:
            drag["d"] = None
            if d["moved"]:
                now = time.monotonic()
                t0, x0, y0 = d["s"][0]
                dt = max(0.04, now - t0)
                vx, vy = (event.x - x0) / dt, -(event.y - y0) / dt
                d["pet"].release(max(-4000, min(4000, vx)), max(-4000, min(4000, vy)))
            else:
                d["pet"].on_click()
        elif menu.drag:
            menu.release()
        update_input_region()
        win.queue_draw()
        return True

    def update_carry(dt):
        """De zak: kantelt boven de bak en laat korreltjes vallen die de bak vullen."""
        now = time.monotonic()
        b = app.bowl
        c = carry["c"]
        floor_y = sh - args.floor
        if c and b:
            bx, by, bw, bh = b.rect()
            bs = c["bs"]
            side = 1 if c["x"] < b.cx else -1
            over = abs(c["x"] - b.cx) < bw * 0.9 and by - 22 * bs < c["y"] < by + bh * 0.3 and b.fill < 1.0
            target = side * 1.75 if over else 0.12 * math.sin(now * 3)
            c["angle"] += (target - c["angle"]) * min(1.0, 7 * dt)
            if over and abs(c["angle"]) > 1.2:
                c["emit"] += dt
                while c["emit"] >= 0.035:
                    c["emit"] -= 0.035
                    th = c["angle"]
                    mx = c["x"] + (18 * bs / 2) * math.sin(th)
                    my = c["y"] - (18 * bs / 2) * math.cos(th)
                    app.parts.append([mx + random.uniform(-bs, bs), my, side * random.uniform(40, 110),
                                      random.uniform(0, 60)])
            if now - c["t0"] > 45:
                carry["c"] = None
        if app.parts and b:
            bx, by, bw, bh = b.rect()
            keep = []
            for pt in app.parts:
                pt[3] += 1800 * dt
                pt[0] += pt[2] * dt
                pt[1] += pt[3] * dt
                if bx + 2 * b.scale <= pt[0] <= bx + bw - 2 * b.scale and pt[1] >= by + 2 * b.scale:
                    b.add(0.03)
                    if int(b.fill * 100) % 4 == 0:
                        app.sfx.play("clink", 0, forced=True)
                    continue
                if pt[1] < floor_y:
                    keep.append(pt)
            app.parts = keep

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
        world.update(dt)
        update_carry(dt)
        for p in pets:
            while p.events:
                name, _prob = p.events.pop(0)
                app.sfx.play(name, p.index, forced=_prob > 1.5)
        if menu.open and now - menu.last > 25:
            menu.close()
        if bowl_menu.open and now - bowl_menu.last > 25:
            bowl_menu.close()
        sig = (tuple(p.signature() for p in pets), app.bowl.level if app.bowl else 0,
               app.bowl.scale if app.bowl else 0, menu.open, bowl_menu.open, bool(carry["c"]), len(app.parts) > 0,
               int(now * 30) if (carry["c"] or app.parts or (app.bowl and app.bowl.shaking())) else 0)
        if sig != st["sig"]:
            st["sig"] = sig
            new = [p.rect() for p in pets]
            moving = any(p.held or p.state == "fly" or p.emote for p in pets) or carry["c"] or app.parts \
                or bowl_menu.open
            if menu.open or st["menu"] or moving:
                win.queue_draw()
            else:
                for (ox, oy, ow, oh), (nx, ny, nw, nh) in zip(st["rects"], new):
                    x0, y0 = min(ox, nx) - 2, min(oy, ny) - 2
                    win.queue_draw_area(x0, y0, max(ox + ow, nx + nw) + 2 - x0, max(oy + oh, ny + nh) + 2 - y0)
            if app.bowl:
                bx, by, bw, bh = st["bowl"]
                nbx, nby, nbw, nbh = app.bowl.rect()
                m = 2 + 4 * app.bowl.scale
                x0, y0 = min(bx, nbx) - m, min(by, nby) - m
                win.queue_draw_area(x0, y0, max(bx + bw, nbx + nbw) + m - x0, max(by + bh, nby + nbh) + m - y0)
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
    ap.add_argument("--no-mouse", action="store_true", help="de katten letten niet op de muis")
    ap.add_argument("--no-sfx", action="store_true", help="geen geluid")
    ap.add_argument("--sfx-rate", type=float, default=0.5,
                    help="hoeveel van de kattengeluiden je hoort 0..1 (standaard 0.5; aaien klinkt altijd)")
    ap.add_argument("--auto-refill", action="store_true", help="voerbak vult zichzelf weer bij (standaard: alleen jij met de zak)")
    ap.add_argument("--volume", type=float, default=0.4, help="geluidsvolume 0..1 (standaard 0.4)")
    ap.add_argument("--sounds", default=str(here / "sounds"), help="map met eigen geluiden (purr.wav, hiss.wav, ...)")
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
