#!/usr/bin/env python3
"""
catpet - pixel-kat desktop pet voor Hyprland (Wayland) via gtk-layer-shell.

Alle sprites worden procedureel gegenereerd (pixel art in code), maar je kunt ze
vervangen door eigen PNG-sprites (zie README / --dump-sprites).

Installatie (CachyOS / Arch):
    sudo pacman -S gtk3 gtk-layer-shell python-gobject python-cairo

Starten:
    python catpet.py
    python catpet.py --scale 5 --floor 40 --monitor 0 --speed 80

Autostart in hyprland.conf:
    exec-once = python ~/catpet/catpet.py --floor 40

Stoppen:
    pkill -f catpet.py
"""
import argparse
import json
import random
import signal
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Pixel-art engine (pure Python, geen GTK nodig)
# --------------------------------------------------------------------------- #

W, H = 30, 19  # grootte van één frame in "art-pixels"

PALETTE = {
    "K": (0.13, 0.10, 0.14),  # outline / ogen
    "O": (0.93, 0.58, 0.22),  # oranje vacht
    "D": (0.76, 0.40, 0.14),  # donkere strepen / verre poten
    "L": (0.98, 0.88, 0.72),  # licht (buik, poten, snuit)
    "P": (0.95, 0.50, 0.55),  # roze neus
    "W": (1.00, 1.00, 1.00),  # wit (zzz)
}

DEFAULT_FPS = {"walk": 8, "idle": 3, "sit": 2, "lick": 4, "sleep": 1.5, "stretch": 2}
ANIM_NAMES = list(DEFAULT_FPS)


class Canvas:
    def __init__(self, w=W, h=H):
        self.w, self.h = w, h
        self.p = [[None] * w for _ in range(h)]

    def px(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.p[y][x] = c

    def rect(self, x, y, w, h, c, rounded=False):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if (rounded and w > 2 and h > 2
                        and xx in (x, x + w - 1) and yy in (y, y + h - 1)):
                    continue
                self.px(xx, yy, c)

    def line(self, x0, y0, x1, y1, t, c):
        n = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(n + 1):
            x = round(x0 + (x1 - x0) * i / n)
            y = round(y0 + (y1 - y0) * i / n)
            self.rect(x - t // 2, y - t // 2, t, t, c)

    def outline(self, c="K"):
        src = [row[:] for row in self.p]
        for y in range(self.h):
            for x in range(self.w):
                if src[y][x] is not None:
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < self.w and 0 <= ny < self.h and src[ny][nx] is not None:
                        self.p[y][x] = c
                        break

    def ascii(self):
        return "\n".join("".join(c or "." for c in row) for row in self.p)


Z_GLYPH = ["###", "..#", ".#.", "#..", "###"]


def draw_z(c, x, y):
    cells = [(x + i, y + j) for j, row in enumerate(Z_GLYPH)
             for i, ch in enumerate(row) if ch == "#"]
    for cx, cy in cells:  # zwarte rand
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (0 <= cx + dx < c.w and 0 <= cy + dy < c.h
                        and c.p[cy + dy][cx + dx] is None):
                    c.p[cy + dy][cx + dx] = "K"
    for cx, cy in cells:
        c.px(cx, cy, "W")


def head_side(c, x, y, eyes="open"):
    """Kop van opzij, kijkend naar rechts. (x, y) = linkerbovenhoek."""
    c.rect(x, y, 7, 7, "O", True)
    for ex, ey in ((0, -1), (0, -2), (1, -1), (6, -1), (6, -2), (5, -1)):  # oren
        c.px(x + ex, y + ey, "O")
    c.px(x + 3, y, "D")
    c.px(x + 3, y + 1, "D")
    c.rect(x + 3, y + 4, 4, 3, "L")  # snuit
    c.px(x + 6, y + 4, "P")  # neus
    for ex in (x + 2, x + 5):  # ogen
        if eyes == "open":
            c.px(ex, y + 2, "K")
            c.px(ex, y + 3, "K")
        else:
            c.px(ex, y + 3, "K")


TAILS = {
    "up":    [(5, 9), (4, 8), (4, 6), (4, 4), (5, 3)],
    "wag_a": [(5, 9), (4, 8), (3, 6), (2, 4)],
    "wag_b": [(5, 9), (5, 7), (6, 5), (7, 3)],
}


def draw_tail(c, kind):
    pts = TAILS[kind]
    for i, (x, y) in enumerate(pts):
        c.rect(x, y, 2, 2, "D" if i == len(pts) - 1 else "O")


def leg(c, x, dx, lift, col):
    h = 4 - lift
    c.rect(x + dx, 14, 2, h, col)
    c.rect(x + dx, 14 + h - 1, 2, 1, "L")


# gait = (dx A, lift A, dx B, lift B); A = near-front + far-back, B = far-front + near-back
WALK_GAIT = [(1, 0, -1, 0), (0, 0, 0, 2), (-1, 0, 1, 0), (0, 2, 0, 0)]


def pose_stand(gait=(0, 0, 0, 0), tail="up", eyes="open"):
    c = Canvas()
    adx, alift, bdx, blift = gait
    leg(c, 14, bdx, blift, "D")  # verre voorpoot
    leg(c, 11, adx, alift, "D")  # verre achterpoot
    draw_tail(c, tail)
    c.rect(7, 8, 13, 6, "O", True)  # lichaam
    c.rect(9, 13, 9, 1, "L")  # buik
    for x in (10, 13, 16):
        c.rect(x, 8, 1, 2, "D")  # strepen
    leg(c, 17, adx, alift, "O")  # nabije voorpoot
    leg(c, 8, bdx, blift, "O")  # nabije achterpoot
    head_side(c, 20, 5, eyes)
    c.outline()
    return c


def pose_sit(eyes="open", tail_tip=2, paw=None, hy=0):
    c = Canvas()
    c.rect(1, 16, 6, 2, "O")  # staart op de grond
    c.rect(1, 16 - tail_tip, 2, tail_tip, "O")
    c.px(1, 16 - tail_tip, "D")
    c.rect(6, 11, 8, 7, "O", True)  # achterpoot/bil
    c.rect(11, 9, 8, 9, "O", True)  # romp
    c.rect(16, 11, 3, 6, "L")  # borst
    c.rect(11, 17, 5, 1, "L")  # achtervoet
    for y in range(12, 16):
        c.px(11, y, "D")
    if paw is None:
        c.rect(17, 13, 2, 5, "L")  # voorpoten
    head_side(c, 13, 3 + hy, eyes)
    if paw is not None:  # opgeheven poot bij de mond
        c.rect(19, 9 + hy + paw, 2, 4, "L")
    c.outline()
    return c


def pose_sleep(breath=0, z=0):
    c = Canvas()
    c.rect(2, 16, 5, 2, "O")
    c.px(2, 16, "D")
    c.rect(6, 10 - breath, 15, 8 + breath, "O", True)
    for x in (9, 12, 15):
        c.rect(x, 10 - breath, 1, 2, "D")
    c.rect(14, 16, 5, 2, "L")
    head_side(c, 19, 11, "closed")
    c.outline()
    if z >= 1:
        draw_z(c, 20, 3)
    if z >= 2:
        draw_z(c, 24, 1)
    return c


def pose_wall(reach=0):
    """Kat rekt zich tegen de muur aan de RECHTERkant (rechterrand van het frame)."""
    c = Canvas()
    c.line(7, 15, 2, 12, 2, "O")  # staart
    c.line(2, 12, 2, 8, 2, "O")
    c.rect(1, 7, 2, 1, "D")
    c.rect(11, 15, 2, 3, "D")  # verre achterpoot
    c.rect(8, 14, 3, 4, "O")  # nabije achterpoot
    c.rect(7, 17, 6, 1, "L")  # voeten
    c.line(10, 14, 19, 9, 5, "O")  # opgerekte romp
    py = [11, 8, 5][reach]
    c.line(18, 11, 27, py, 2, "L")  # armen
    head_side(c, 20, 3, "closed")
    c.rect(27, py - 1, 2, 3, "L")  # pootjes tegen de muur
    c.outline()
    return c


def build_generated():
    walk = [pose_stand(g, "wag_a" if i % 2 == 0 else "wag_b")
            for i, g in enumerate(WALK_GAIT)]
    idle = [pose_stand(tail="up"), pose_stand(tail="wag_a"),
            pose_stand(tail="up", eyes="closed"), pose_stand(tail="wag_b")]
    sit = [pose_sit(), pose_sit(tail_tip=3), pose_sit(), pose_sit(eyes="closed")]
    lick = [pose_sit("closed", 2, paw=0, hy=0), pose_sit("closed", 2, paw=1, hy=1),
            pose_sit("closed", 2, paw=0, hy=1), pose_sit("closed", 2, paw=1, hy=0)]
    sleep = [pose_sleep(0, 0), pose_sleep(1, 1), pose_sleep(1, 2), pose_sleep(0, 1)]
    stretch = [pose_wall(0), pose_wall(1), pose_wall(2), pose_wall(1)]
    return {"walk": walk, "idle": idle, "sit": sit, "lick": lick,
            "sleep": sleep, "stretch": stretch}


# --------------------------------------------------------------------------- #
#  Sprites -> cairo surfaces (generated of geimporteerd)
# --------------------------------------------------------------------------- #

class Anim:
    def __init__(self, frames, fps):
        self.frames, self.fps = frames, fps


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
        frames.append(s)
    return frames


def load_anims(sprite_dir, scale):
    gen = build_generated()
    anims = {n: Anim([canvas_surface(c, scale) for c in gen[n]], DEFAULT_FPS[n])
             for n in ANIM_NAMES}
    sprite_dir = Path(sprite_dir)
    if not sprite_dir.is_dir():
        return anims, scale
    cfg = {}
    cfg_path = sprite_dir / "config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
    scale = cfg.get("scale", scale)
    # regenereer ingebouwde sprites als de schaal uit de config komt
    anims = {n: Anim([canvas_surface(c, scale) for c in gen[n]], DEFAULT_FPS[n])
             for n in ANIM_NAMES}
    for name in ANIM_NAMES:
        png = sprite_dir / f"{name}.png"
        if not png.exists():
            continue
        per = cfg.get("anims", {}).get(name, {})
        import cairo
        height = cairo.ImageSurface.create_from_png(str(png)).get_height()
        fw = per.get("frame_width", cfg.get("frame_width", height))
        fps = per.get("fps", cfg.get("fps", {}).get(name, DEFAULT_FPS[name]))
        anims[name] = Anim(load_strip(png, fw, scale), fps)
        print(f"[catpet] eigen sprite geladen: {png.name} ({len(anims[name].frames)} frames)")
    return anims, scale


def dump_sprites(out_dir):
    """Schrijf de ingebouwde sprites als PNG-strips + config.json (om te bewerken)."""
    import cairo
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, canvases in build_generated().items():
        strip = cairo.ImageSurface(cairo.FORMAT_ARGB32, W * len(canvases), H)
        ctx = cairo.Context(strip)
        for i, cv in enumerate(canvases):
            ctx.set_source_surface(canvas_surface(cv, 1), i * W, 0)
            ctx.paint()
        strip.write_to_png(str(out / f"{name}.png"))
    cfg = {"scale": 4, "frame_width": W, "fps": DEFAULT_FPS}
    (out / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"[catpet] sprites geschreven naar {out}/")


# --------------------------------------------------------------------------- #
#  Gedrag van de kat
# --------------------------------------------------------------------------- #

class Pet:
    def __init__(self, anims, screen_w, screen_h, floor, speed):
        self.anims = anims
        self.sw, self.sh, self.floor, self.speed = screen_w, screen_h, floor, speed
        self.dir = random.choice((-1, 1))
        self.state, self.t, self.dur = "idle", 0.0, 2.0
        self.target, self.goal = 0.0, "wander"
        self.x = random.uniform(0, max(1, self.sw - self.width("walk")))

    # -- helpers
    def width(self, anim):
        return self.anims[anim].frames[0].get_width()

    def frame(self):
        a = self.anims[self.state]
        i = int(self.t * a.fps) % len(a.frames)
        return a.frames[i], i

    def rect(self):
        s, _ = self.frame()
        w, h = s.get_width(), s.get_height()
        return int(self.x), self.sh - self.floor - h, w, h

    def signature(self):
        return (self.state, self.frame()[1], int(self.x), self.dir)

    # -- state machine
    def set_state(self, state, dur):
        self.state, self.t, self.dur = state, 0.0, dur

    def go(self, target, goal="wander"):
        self.target, self.goal = target, goal
        self.set_state("walk", 0)
        if abs(target - self.x) > 1:
            self.dir = 1 if target > self.x else -1

    def choose_next(self):
        prev = self.state
        if prev == "sit" and random.random() < 0.45:
            return self.set_state("lick", random.uniform(4, 8))
        if prev == "lick" and random.random() < 0.5:
            return self.set_state("sit", random.uniform(2, 4))
        options = [("walk", 40), ("sit", 20), ("wall", 18), ("sleep", 10), ("idle", 12)]
        if prev == "stretch":
            options = [o for o in options if o[0] != "wall"]
        choice = random.choices([o[0] for o in options], [o[1] for o in options])[0]
        ww = self.width("walk")
        if choice == "walk":
            for _ in range(10):
                tx = random.uniform(0, max(1, self.sw - ww))
                if abs(tx - self.x) > 200:
                    break
            self.go(tx)
        elif choice == "wall":
            self.go(random.choice((0.0, float(self.sw - ww))), "wall")
        elif choice == "sleep":
            self.set_state("sleep", random.uniform(15, 40))
        elif choice == "sit":
            self.set_state("sit", random.uniform(4, 10))
        else:
            self.set_state("idle", random.uniform(2, 5))

    def begin_stretch(self):
        sw_w = self.width("stretch")
        if self.target <= 0:
            self.dir, self.x = -1, 0.0
        else:
            self.dir, self.x = 1, float(self.sw - sw_w)
        self.set_state("stretch", random.uniform(5, 9))

    def update(self, dt):
        self.t += dt
        if self.state == "walk":
            step = self.speed * dt
            d = self.target - self.x
            if abs(d) <= step:
                self.x = self.target
                if self.goal == "wall":
                    self.begin_stretch()
                else:
                    self.choose_next()
            else:
                self.dir = 1 if d > 0 else -1
                self.x += step * self.dir
        elif self.t >= self.dur:
            self.choose_next()

    def draw(self, ctx):
        s, _ = self.frame()
        x, y, w, _h = self.rect()
        ctx.save()
        if self.dir < 0:
            ctx.translate(x + w, y)
            ctx.scale(-1, 1)
        else:
            ctx.translate(x, y)
        ctx.set_source_surface(s, 0, 0)
        ctx.paint()
        ctx.restore()


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
    n = display.get_n_monitors()
    monitor = display.get_monitor(min(args.monitor, n - 1))
    geo = monitor.get_geometry()

    anims, scale = load_anims(args.sprites, args.scale or 4)
    pet = Pet(anims, geo.width, geo.height, args.floor, args.speed)

    win = Gtk.Window()
    win.set_title("catpet")
    win.set_app_paintable(True)
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)

    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_namespace(win, "catpet")
    GtkLayerShell.set_monitor(win, monitor)
    GtkLayerShell.set_layer(
        win, GtkLayerShell.Layer.OVERLAY if args.layer == "overlay" else GtkLayerShell.Layer.TOP)
    for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                 GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
        GtkLayerShell.set_anchor(win, edge, True)
    GtkLayerShell.set_exclusive_zone(win, -1)  # negeer balken (waybar e.d.)
    GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)

    win.input_shape_combine_region(cairo.Region())  # klikken vallen erdoorheen

    def on_draw(_w, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        pet.draw(ctx)
        return False

    win.connect("draw", on_draw)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()

    state = {"last": time.monotonic(), "sig": None, "rect": pet.rect()}

    def tick():
        now = time.monotonic()
        dt = min(now - state["last"], 0.1)
        state["last"] = now
        pet.update(dt)
        sig = pet.signature()
        if sig != state["sig"]:
            state["sig"] = sig
            ox, oy, ow, oh = state["rect"]
            nx, ny, nw, nh = pet.rect()
            x0, y0 = min(ox, nx) - 2, min(oy, ny) - 2
            x1, y1 = max(ox + ow, nx + nw) + 2, max(oy + oh, ny + nh) + 2
            win.queue_draw_area(x0, y0, x1 - x0, y1 - y0)
            state["rect"] = (nx, ny, nw, nh)
        return True

    GLib.timeout_add(33, tick)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
    Gtk.main()


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Pixel-kat desktop pet voor Hyprland")
    ap.add_argument("--scale", type=int, default=0, help="pixelgrootte (standaard 4)")
    ap.add_argument("--floor", type=int, default=0, help="px boven de onderrand (bv. hoogte van je waybar)")
    ap.add_argument("--monitor", type=int, default=0, help="monitor-index")
    ap.add_argument("--speed", type=float, default=70, help="loopsnelheid in px/s")
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
