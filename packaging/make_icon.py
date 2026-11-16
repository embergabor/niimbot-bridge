"""Draws the app and printer icon (a small label printer with a label coming out) and writes an .icns.
    python packaging/make_icon.py OUT_DIR        → OUT_DIR/NiimbotProxy.icns and OUT_DIR/NiimbotProxy.png
Needs Pillow and macOS's iconutil. Original artwork; no Niimbot logo."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 4                      # draw at 4x and scale down, for smooth edges
N = 1024 * S


def px(*v):
    return [round(x * S) for x in v]


def vgradient(size, top, bottom):
    w, h = size
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        g.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return g.resize((w, h))


def rounded(box, r):
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).rounded_rectangle(px(*box), radius=r * S, fill=255)
    return m


def paint(img, box, r, top, bottom=None):
    """Fill a rounded rectangle with a flat colour or a vertical gradient."""
    x0, y0, x1, y1 = px(*box)
    fill = vgradient((x1 - x0, y1 - y0), top, bottom or top)
    layer = Image.new("RGB", (N, N))
    layer.paste(fill, (x0, y0))
    img.paste(layer, (0, 0), rounded(box, r))


def draw() -> Image.Image:
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # soft shadow under the printer
    sh = Image.new("L", (N, N), 0)
    ImageDraw.Draw(sh).rounded_rectangle(px(150, 470, 874, 930), radius=130 * S, fill=110)
    sh = sh.filter(ImageFilter.GaussianBlur(28 * S))
    img.paste((0, 0, 0, 255), (0, 0), sh)

    base = Image.new("RGB", (N, N))
    mask = Image.new("L", (N, N), 0)

    # black top cover, behind the paper
    paint(base, (196, 300, 828, 520), 90, (52, 56, 62), (24, 26, 30))
    mask.paste(255, (0, 0), rounded((196, 300, 828, 520), 90))

    # label backing coming out of the slot, with one white label and a barcode on it
    paint(base, (290, 96, 734, 470), 18, (236, 238, 240), (214, 217, 220))
    mask.paste(255, (0, 0), rounded((290, 96, 734, 470), 18))
    paint(base, (314, 124, 710, 400), 30, (255, 255, 255))
    d = ImageDraw.Draw(base)
    ink = (40, 44, 50)
    x = 352
    for i, w in enumerate([10, 4, 6, 12, 4, 8, 4, 10, 6, 4, 12, 4, 8, 6, 4, 10, 4, 6, 12, 4, 8, 4, 10, 6, 4, 8]):
        if i % 2 == 0:
            d.rectangle(px(x, 162, x + w, 290), fill=ink)
        x += w + 2
    d.rounded_rectangle(px(352, 318, 600, 338), radius=10 * S, fill=ink)
    d.rounded_rectangle(px(352, 354, 520, 370), radius=8 * S, fill=(120, 126, 134))

    # slate front body, overlapping the bottom of the paper
    paint(base, (150, 440, 874, 900), 120, (86, 102, 116), (50, 60, 70))
    mask.paste(255, (0, 0), rounded((150, 440, 874, 900), 120))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle(px(270, 444, 754, 462), radius=9 * S, fill=(20, 22, 26))        # paper slot
    d.rounded_rectangle(px(190, 474, 834, 482), radius=4 * S, fill=(104, 120, 134))     # top-edge highlight

    # blue status light, with a glow
    glow = Image.new("L", (N, N), 0)
    ImageDraw.Draw(glow).rounded_rectangle(px(262, 652, 392, 690), radius=19 * S, fill=200)
    glow = glow.filter(ImageFilter.GaussianBlur(16 * S))
    base.paste((70, 130, 255), (0, 0), glow)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle(px(272, 660, 382, 682), radius=11 * S, fill=(90, 150, 255))

    # round power button
    cx, cy, r = 690, 690, 70
    d.ellipse(px(cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8), fill=(38, 46, 54))
    d.ellipse(px(cx - r, cy - r, cx + r, cy + r), fill=(64, 78, 90))
    sym = (160, 196, 214)
    d.arc(px(cx - 34, cy - 30, cx + 34, cy + 38), start=-50, end=230, fill=sym, width=10 * S)
    d.rounded_rectangle(px(cx - 5, cy - 46, cx + 5, cy + 2), radius=5 * S, fill=sym)

    body = base.convert("RGBA")
    body.putalpha(mask)
    img.alpha_composite(body)
    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    icon = draw()
    icon.save(out / "NiimbotProxy.png")
    iconset = out / "NiimbotProxy.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "NiimbotProxy.icns")], check=True)
    shutil.rmtree(iconset)
    print(out / "NiimbotProxy.icns")


if __name__ == "__main__":
    main()
