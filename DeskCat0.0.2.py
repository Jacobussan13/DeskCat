

#!/usr/bin/env python3
"""
catpet - pixel-kat desktop pet voor Hyprland (Wayland) via gtk-layer-shell.

Installatie (CachyOS / Arch):
    sudo pacman -S gtk3 gtk-layer-shell python-gobject python-cairo

Starten:
    python catpet.py
    python catpet.py --scale 4 --floor 40 --calm 1.5

Klik op de kat om hem te laten spinnen. Stoppen: pkill -f catpet.py
Eigen sprites: python catpet.py --dump-sprites sprites  (bewerk de PNG's, start opnieuw)
"""
import argparse
import json
import math
import random
import signal
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Pixel-art engine + kat-rig (puur Python, geen GTK nodig)
# --------------------------------------------------------------------------- #

W, H = 40, 30
GROUND = 28  # onderste pixelrij van de poten (rij 29 = outline)

PALETTE = {
    "K": (0.13, 0.10, 0.14),
    "O": (0.93, 0.58, 0.22),
    "D": (0.74, 0.38, 0.13),
    "L": (0.99, 0.90, 0.76),
    "P": (0.96, 0.52, 0.58),
    "W": (1.00, 1.00, 1.00),
}


class Canvas:
    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.p = [[None] * w for _ in range(h)]

    def px(self, x, y, c):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.p[y][x] = c

    def get(self, x, y):
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


# ---------- vormen -------------------------------------------------------- #

def chain_mask(pts, r0, r1=None):
    """Capsule langs een polylijn. Geeft {(x,y): t} met t = positie langs de lijn (0..1)."""
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
        for i in range(n + 1):
            f = i / n
            x = a[0] + (b[0] - a[0]) * f
            y = a[1] + (b[1] - a[1]) * f
            t = (start + d * f) / total
            r = r0 + (r1 - r0) * t
            for yy in range(int(y - r) - 1, int(y + r) + 2):
                for xx in range(int(x - r) - 1, int(x + r) + 2):
                    if (xx + .5 - x) ** 2 + (yy + .5 - y) ** 2 <= r * r and (xx, yy) not in mask:
                        mask[(xx, yy)] = t
    return mask


def ellipse_mask(cx, cy, rx, ry):
    m = {}
    for yy in range(int(cy - ry) - 1, int(cy + ry) + 2):
        for xx in range(int(cx - rx) - 1, int(cx + rx) + 2):
            if ((xx + .5 - cx) / rx) ** 2 + ((yy + .5 - cy) / ry) ** 2 <= 1:
                m[(xx, yy)] = (xx + .5 - (cx - rx)) / (2 * rx)
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
    """Vacht: oranje, lichte buik onderaan, donkere tabby-strepen bovenaan."""
    keys = set(mask)
    for (x, y), t in mask.items():
        col = "O"
        if belly and ((x, y + 1) not in keys or (x, y + 2) not in keys):
            col = "L"
        elif stripes and ((x, y - 1) not in keys or (x, y - 2) not in keys) and int(t * bands) % 2 == 1:
            col = "D"
        c.px(x, y, col)


# ---------- onderdelen (overal hetzelfde -> consistente kat) -------------- #

def head(c, x, y, eyes="open", ear=0, mouth="closed", tongue=False):
    """Kop 9x8, kijkt naar rechts. (x,y) = linkerbovenhoek."""
    for yy in range(8):
        for xx in range(9):
            if xx in (0, 8) and yy in (0, 7):
                continue
            c.px(x + xx, y + yy, "O")
    left = [(1, -1), (2, -1), (3, -1), (1, -2), (2, -2), (1, -3)]
    right = [(5, -1), (6, -1), (7, -1), (6, -2), (7, -2), (7, -3)]
    if ear == 1:  # oor-flick
        right = [(6, -1), (7, -1), (7, -2), (5, 0)]
    for ex, ey in left + right:
        c.px(x + ex, y + ey, "O")
    c.px(x + 2, y - 1, "P")
    c.px(x + 6, y - 1, "P")
    for ex, ey in ((3, 0), (4, 0), (5, 0), (4, 1), (4, 2), (0, 3), (1, 3)):  # tabby-tekening
        c.px(x + ex, y + ey, "D")
    for yy in range(4, 8):  # snuit
        for xx in range(5, 9):
            if (xx, yy) != (8, 7):
                c.px(x + xx, y + yy, "L")
    c.px(x + 8, y + 4, "P")  # neus
    for ex in (2, 6):  # ogen
        if eyes == "open":
            c.px(x + ex, y + 3, "K"); c.px(x + ex, y + 4, "K")
        elif eyes == "half":
            c.px(x + ex, y + 4, "K"); c.px(x + ex + 1, y + 4, "K")
        elif eyes == "closed":
            for k in (-1, 0, 1):
                c.px(x + ex + k, y + 4, "K")
        elif eyes == "happy":  # ^ ^
            c.px(x + ex - 1, y + 4, "K"); c.px(x + ex, y + 3, "K"); c.px(x + ex + 1, y + 4, "K")
    if mouth == "open":
        c.px(x + 7, y + 5, "K"); c.px(x + 8, y + 5, "K")
        c.px(x + 7, y + 6, "K"); c.px(x + 8, y + 6, "P")
    elif mouth == "smile":
        c.px(x + 7, y + 6, "K")
    else:
        c.px(x + 6, y + 6, "K")
    if tongue:
        c.px(x + 9, y + 5, "P"); c.px(x + 9, y + 6, "P")


def neck(c, a, b):
    paint(c, chain_mask([a, b], 2.7), "O")


def tail(c, pts, sway=0.0, dy=0.0):
    n = max(1, len(pts) - 1)
    pts = [(x + sway * (i / n) * 2.2, y + dy) for i, (x, y) in enumerate(pts)]
    m = chain_mask(pts, 1.35, 1.0)
    for (x, y), t in m.items():
        c.px(x, y, "D" if t > 0.82 else "O")


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
    leg(c, 13, y0, a[0], a[1], "D")   # verre achterpoot (A)
    leg(c, 19, y0, b[0], b[1], "D")   # verre voorpoot (B)
    body = chain_mask([(12, 20 + bob), (23, 20 + bob)], 3.4)
    paint_fur(c, body)
    leg(c, 10, y0, b[0], b[1], "O")   # nabije achterpoot (B)
    leg(c, 22, y0, a[0], a[1], "O")   # nabije voorpoot (A)
    neck(c, (22.5, 19 + bob), (28.5, 17 + bob))
    head(c, 25, 12 + bob, eyes, ear, mouth)
    c.outline()
    return c


def sit_base(c, tail_sway, head_xy, torso_top, front_leg=True, paw=None, eyes="open",
             ear=0, mouth="closed", tongue=False, hdy=0):
    tail(c, [(8, 26), (5.5, 27), (3.5, 26), (3.2, 23.5), (4, 21)], tail_sway)
    if paw is not None:
        paint(c, chain_mask([(19, 20), (19, 27.2)], 1.05), "D")  # andere voorpoot blijft staan
    haunch = ellipse_mask(13, 23.3, 5.8, 4.9)
    paint_fur(c, haunch, belly=False, bands=5)
    c.rect(14, 27, 5, 2, "L")  # achtervoet
    torso = chain_mask([(18, 22.5), torso_top], 3.4, 3.0)
    paint_fur(c, torso, belly=False, bands=5)
    chest = ellipse_mask(23.8, 19, 2.4, 4.8)
    for p in chest:
        if p in torso:
            c.px(p[0], p[1], "L")
    if paw is None and front_leg:
        paint(c, chain_mask([(21.5, 19), (21.5, 27.2)], 1.05), "O")
        c.rect(20, 27, 4, 2, "L")
    hx, hy = head_xy
    neck(c, (torso_top[0], torso_top[1] + 1), (hx + 3.5, hy + 5.5))
    if paw is not None:  # opgeheven poot: bovenarm + onderarm
        px, py = paw
        paint(c, chain_mask([(21, 18), (23.5, 22.5), (px, py)], 1.35), "O")
    head(c, hx, hy + hdy, eyes, ear, mouth, tongue)
    if paw is not None:
        px, py = paw
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                c.px(int(px) + dx, int(py) + dy, "L")
        c.px(int(px), int(py), "P")


def pose_sit(sway=0.0, eyes="open", ear=0, mouth="closed", hdy=0, hearts=None, tongue=False):
    c = Canvas()
    sit_base(c, sway, (19, 6), (20.5, 13.2), eyes=eyes, ear=ear, mouth=mouth, hdy=hdy, tongue=tongue)
    c.outline()
    for hx, hy in (hearts or []):
        if hy >= 1:
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
    body = chain_mask([(10.5, 25 - 0.5 * breath), (24, 25 - 0.5 * breath)], r)
    paint_fur(c, body, bands=6)
    c.rect(26, 26, 6, 3, "L")  # voorpoten onder de kin
    head(c, 24, 20, "closed")
    c.outline()
    for zx, zy in zs:
        if 1 <= zy:
            stamp(c, Z, zx, zy, "W")
    return c


def pose_wall(hip, sh, arch, paw, head_xy, r=3.1, eyes="open", mouth="closed", sway=0.0):
    c = Canvas()
    hx, hy = hip
    # staart
    tail(c, [(hx - 3, hy - 1), (hx - 6, hy - 3), (hx - 8, hy - 7), (hx - 8, hy - 11), (hx - 7, hy - 14)], sway)
    # achterpoten (rechtop)
    leg(c, int(hx) + 2, hy, 1, 0, "D")
    # romp met gebogen rug
    mx, my = (hip[0] + sh[0]) / 2, (hip[1] + sh[1]) / 2
    dx, dy = sh[0] - hip[0], sh[1] - hip[1]
    L = math.hypot(dx, dy)
    nx, ny = dy / L, -dx / L  # normaal naar de rugkant
    ctrl = (mx + nx * arch, my + ny * arch)
    pts = bezier(hip, ctrl, sh, 10)
    body = chain_mask(pts, r, r - 0.3)
    # verre arm
    pxx, pyy = paw
    paint(c, chain_mask([(sh[0] - 1, sh[1] + 1), (pxx - 1.5, pyy + 2)], 1.05), "D")
    paint_fur(c, body, bands=7)
    leg(c, int(hx) - 1, hy, -1, 0, "O")
    # romp-hals-kop
    neck(c, (sh[0], sh[1] - 1), (head_xy[0] + 3.5, head_xy[1] + 6))
    # nabije arm
    paint(c, chain_mask([(sh[0], sh[1] + 0.5), (pxx - 1, pyy)], 1.05), "O")
    head(c, head_xy[0], head_xy[1], eyes, 0, mouth)
    for ddx in (-1, 0, 1):
        for ddy in (-1, 0, 1):
            c.px(int(pxx) + ddx, int(pyy) + ddy, "L")
    c.px(int(pxx) + 1, int(pyy), "P")
    c.outline()
    return c


def build_generated():
    sw6 = [0.0, 0.7, 1.0, 0.7, 0.0, -0.7]
    crouch = lambda eyes="half": pose_stand(bob=3, tail_kind="low", eyes=eyes)

    walk = [pose_stand(i, tail_kind="mid", sway=sw6[i]) for i in range(6)]
    idle = [pose_stand(sway=sw6[i], eyes="closed" if i == 4 else "open", ear=1 if i == 2 else 0)
            for i in range(6)]
    sit = [pose_sit(sway=sw6[i], eyes="closed" if i == 3 else "open", ear=1 if i == 1 else 0)
           for i in range(6)]
    lick = [
        pose_lick((25.5, 21), (20, 8), eyes="half"),                            # intro: poot omhoog
        pose_lick((28, 18.5), (21, 9), tongue=True),                              # lik A
        pose_lick((27.5, 17.5), (21.5, 9.5), lean=0.5),                           # lik B
        pose_lick((28, 18.5), (21, 10), tongue=True, lean=0.5),                   # lik A
        pose_lick((27.5, 17.5), (21, 9)),                                         # lik B
        pose_lick((25.5, 21), (20, 8), eyes="half"),                            # outro
    ]
    zpos = lambda i, k: (30 + 1.1 * ((i + 2 * k) % 6), 14 - 2.3 * ((i + 2 * k) % 6))
    breath = [0, 0, 1, 1, 1, 0]
    sleep = [crouch()] + [
        pose_sleep(breath[i], [(round(zpos(i, k)[0]), round(zpos(i, k)[1])) for k in range(3)])
        for i in range(6)] + [crouch()]
    stretch = [
        pose_wall((13, 21), (24.5, 17.5), 1.0, (36.5, 15), (26, 10), r=3.2),                              # reach
        pose_wall((14, 22), (25.5, 16.5), 2.8, (37.5, 9), (27, 9), r=3.1, eyes="half"),                   # omhoog, rug bolt
        pose_wall((15, 23), (26.5, 16), -1.6, (38, 5), (28, 8), r=2.8, eyes="happy", sway=0.5),           # uitrekken, rug hol
        pose_wall((15, 23), (27, 15.5), -2.4, (38.2, 3.5), (28, 8), r=2.7, eyes="closed", mouth="open", sway=-0.5),  # geeuw
        pose_wall((14, 22.5), (25.5, 17), 3.2, (37, 10), (27, 10), r=3.3, eyes="closed"),                 # rug-bolling
        pose_wall((13, 21.5), (24.5, 18), 1.5, (36.5, 16), (26, 11), r=3.3, eyes="half"),                 # zakt terug
    ]
    purr = []
    for i in range(6):
        h1 = 11 - 2 * i
        h2 = 11 - 2 * ((i + 3) % 6)
        purr.append(pose_sit(sway=sw6[i] * 0.6, eyes="happy", mouth="smile", hdy=i % 2,
                             hearts=[(29, h1), (33, h2)]))
    return {
        "walk": (walk, 0, 0), "idle": (idle, 0, 0), "sit": ([crouch()] + sit + [crouch()], 1, 1),
        "lick": (lick, 1, 1), "sleep": (sleep, 1, 1), "stretch": (stretch, 2, 2), "purr": (purr, 0, 0),
    }


# --------------------------------------------------------------------------- #
#  Animaties -> cairo (generated of geimporteerd)
# --------------------------------------------------------------------------- #

# fps = lus-snelheid, tfps = snelheid van intro/outro-frames
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


class Anim:
    def __init__(self, frames, fps, tfps, intro=0, outro=0):
        self.frames, self.fps, self.tfps = frames, fps, tfps
        self.intro, self.outro = intro, outro


def canvas_surface(canvas, scale):
    import cairo
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas.w * scale, canvas.h * scale)
    ctx = cairo.Context(s)
    for y, row in enumerate(canvas.p):
        for x, col in enumerate(row):
            if col:
                ctx.set_source_rgb(*PALETTE[col])
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
        ctx.get_source().set_filter(cairo.FILTER_NEAREST)  # scherpe pixels
        ctx.rectangle(0, 0, fw, fh)
        ctx.fill()
        frames.append(Frame(s))
    return frames


def load_anims(sprite_dir, scale):
    cfg = {}
    sprite_dir = Path(sprite_dir)
    if (sprite_dir / "config.json").exists():
        cfg = json.loads((sprite_dir / "config.json").read_text())
    scale = scale or cfg.get("scale", 3)

    anims = {}
    for name, (canvases, intro, outro) in build_generated().items():
        anims[name] = Anim([Frame(canvas_surface(c, scale)) for c in canvases],
                           SPEC[name]["fps"], SPEC[name]["tfps"], intro, outro)

    for name in ANIM_NAMES:
        png = sprite_dir / f"{name}.png"
        if not png.exists():
            continue
        import cairo
        height = cairo.ImageSurface.create_from_png(str(png)).get_height()
        per = cfg.get("anims", {}).get(name, {})
        fw = per.get("frame_width", cfg.get("frame_width", height))
        anims[name] = Anim(load_strip(png, fw, scale),
                           per.get("fps", SPEC[name]["fps"]),
                           per.get("tfps", SPEC[name]["tfps"]),
                           per.get("intro", 0), per.get("outro", 0))
        print(f"[catpet] eigen sprite geladen: {png.name} ({len(anims[name].frames)} frames)")
    return anims, scale


def dump_sprites(out_dir):
    """Schrijf de ingebouwde sprites als PNG-strips + config.json (om te bewerken)."""
    import cairo
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = {"scale": 3, "frame_width": W, "anims": {}}
    for name, (canvases, intro, outro) in build_generated().items():
        strip = cairo.ImageSurface(cairo.FORMAT_ARGB32, W * len(canvases), H)
        ctx = cairo.Context(strip)
        for i, cv in enumerate(canvases):
            ctx.set_source_surface(canvas_surface(cv, 1), i * W, 0)
            ctx.paint()
        strip.write_to_png(str(out / f"{name}.png"))
        cfg["anims"][name] = {**SPEC[name], "intro": intro, "outro": outro}
    (out / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"[catpet] sprites geschreven naar {out}/")


# --------------------------------------------------------------------------- #
#  Gedrag
# --------------------------------------------------------------------------- #

class Pet:
    def __init__(self, anims, sw, sh, floor, scale, speed, calm):
        self.anims, self.sw, self.sh, self.floor = anims, sw, sh, floor
        self.speed_mult, self.calm = speed, calm
        self.px_speed = 18 * scale * speed  # px/s (voeten glijden dan niet)
        self.dir = random.choice((-1, 1))
        self.state, self.t, self.dur = "idle", 0.0, 3.0
        self.target, self.goal = 0.0, "wander"
        self.last = {}  # cooldowns per activiteit
        self.x = random.uniform(0, max(1, sw - anims["walk"].frames[0].w))

    # -- frame-keuze (intro -> lus -> outro)
    def anim(self):
        return self.anims[self.state]

    def frame(self):
        a = self.anim()
        fps = a.fps * (self.speed_mult if self.state == "walk" else 1)
        n = len(a.frames)
        loop_n = max(1, n - a.intro - a.outro)
        intro_t = a.intro / a.tfps
        outro_t = a.outro / a.tfps
        if a.intro and self.t < intro_t:
            i = int(self.t * a.tfps)
        elif a.outro and self.state != "walk" and self.t >= self.dur - outro_t:
            i = n - a.outro + min(int((self.t - (self.dur - outro_t)) * a.tfps), a.outro - 1)
        else:
            i = a.intro + int((self.t - intro_t) * fps) % loop_n
        return a.frames[min(i, n - 1)], i

    def rect(self):
        f, _ = self.frame()
        return int(self.x), self.sh - self.floor - f.h, f.w, f.h

    def signature(self):
        return (self.state, self.frame()[1], int(self.x), self.dir)

    # -- toestanden
    def set_state(self, state, loop_time):
        a = self.anims[state]
        self.state, self.t = state, 0.0
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
        """Na elke activiteit eerst even rust - dat houdt het rustig."""
        kind = random.choices(("sit", "idle"), (60, 40))[0]
        self.set_state(kind, random.uniform(5, 12) * self.calm)

    def choose_next(self):
        prev = self.state
        if prev in ACTIVITIES:
            return self.rest()
        fw = self.anims["walk"].frames[0].w
        opts = {"walk": 50, "sit": 15, "idle": 8}
        if self.ready("stretch", 90):
            opts["wall"] = 15
        if self.ready("sleep", 150):
            opts["sleep"] = 12
        if prev == "sit" and self.ready("lick", 30) and random.random() < 0.5:
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
        if self.state == "purr":
            self.dur += 2.5  # nog even doorspinnen
            return
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

    display = Gdk.Display.get_default()
    monitor = display.get_monitor(min(args.monitor, display.get_n_monitors() - 1))
    geo = monitor.get_geometry()

    anims, scale = load_anims(args.sprites, args.scale)
    pet = Pet(anims, geo.width, geo.height, args.floor, scale, args.speed, args.calm)

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

    win.input_shape_combine_region(cairo.Region())  # begin: alles klik-doorlatend

    def on_draw(_w, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        pet.draw(ctx)
        return False

    def on_press(_w, event):
        if event.button == 1:
            pet.on_click()
        return True

    win.connect("draw", on_draw)
    win.connect("button-press-event", on_press)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()

    st = {"last": time.monotonic(), "sig": None, "rect": pet.rect()}

    def update_input_region():
        """Alleen de pixels van de kat vangen klikken; de rest van het scherm blijft vrij."""
        f, _ = pet.frame()
        x, y, w, h = pet.rect()
        reg = f.region(pet.dir < 0)
        if reg is None:
            reg = cairo.Region(cairo.RectangleInt(x, y, w, h))
        else:
            reg = reg.copy()
            reg.translate(x, y)
        win.input_shape_combine_region(reg)

    def tick():
        now = time.monotonic()
        dt = min(now - st["last"], 0.1)
        st["last"] = now
        pet.update(dt)
        sig = pet.signature()
        if sig != st["sig"]:
            st["sig"] = sig
            ox, oy, ow, oh = st["rect"]
            nx, ny, nw, nh = pet.rect()
            x0, y0 = min(ox, nx) - 2, min(oy, ny) - 2
            x1, y1 = max(ox + ow, nx + nw) + 2, max(oy + oh, ny + nh) + 2
            win.queue_draw_area(x0, y0, x1 - x0, y1 - y0)
            st["rect"] = (nx, ny, nw, nh)
            update_input_region()
        return True

    GLib.timeout_add(33, tick)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    Gtk.main()


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Pixel-kat desktop pet voor Hyprland")
    ap.add_argument("--scale", type=int, default=0, help="pixelgrootte (standaard 3)")
    ap.add_argument("--floor", type=int, default=0, help="px boven de onderrand (bv. hoogte van je waybar)")
    ap.add_argument("--monitor", type=int, default=0, help="monitor-index")
    ap.add_argument("--speed", type=float, default=1.0, help="loopsnelheid (1.0 = normaal)")
    ap.add_argument("--calm", type=float, default=1.0,
                    help="rust tussen activiteiten: 1.0 = normaal, 2.0 = nog rustiger, 0.5 = drukker")
    ap.add_argument("--layer", choices=("top", "overlay"), default="top",
                    help="top = onder fullscreen-apps, overlay = altijd erboven")
    ap.add_argument("--sprites", default=str(here / "sprites"), help="map met eigen sprite-PNG's")
    ap.add_argument("--dump-sprites", metavar="MAP",
                    help="schrijf de ingebouwde sprites als PNG-strips naar MAP en stop")
    args = ap.parse_args()
    if args.dump_sprites:
        dump_sprites(args.dump_sprites)
        return
    run(args)


if __name__ == "__main__":
    main()
