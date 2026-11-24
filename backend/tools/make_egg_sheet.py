#!/usr/bin/env python3
"""Regenerate backend/companion/assets/egg.png (4 frames of 64x64, one row).

Frames: 0 still, 1 tilt left, 2 tilt right, 3 cracked.
The egg is authored at 32x32 and doubled with nearest-neighbour, so every art pixel is a
clean 2x2 block. Needs only Pillow. Run:  python tools/make_egg_sheet.py
"""
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent.parent / "backend" / "companion" / "assets" / "egg.png"

INK, INK_LIT = (31, 26, 54), (61, 54, 104)
SHELL = {"shadow": (216, 201, 173), "base": (246, 239, 224), "light": (255, 250, 240)}
SPOT_A = {"shadow": (122, 100, 214), "base": (185, 167, 255), "light": (214, 202, 255)}
SPOT_B = {"shadow": (95, 78, 190), "base": (138, 118, 232), "light": (170, 154, 250)}
GROUND = (43, 37, 72)
GLOW = (255, 143, 179)

N = 32
CX, CY, RX, RY = 15.5, 17.0, 9.2, 11.2


def inside(x, y):
    t = (y - CY) / RY
    rx = RX * (1.0 + 0.13 * t)          # wider near the bottom: an egg, not an ellipse
    return ((x - CX) / rx) ** 2 + t * t <= 1.0


def build(dx=0, dy=0, cracked=False):
    grid = [[None] * N for _ in range(N)]

    def put(x, y, c):
        x, y = x + dx, y + dy
        if 0 <= x < N and 0 <= y < N:
            grid[y][x] = c

    # ground shadow stays put while the egg moves
    for y in range(29, 31):
        for x in range(8, 24):
            if ((x - 15.5) / 8.0) ** 2 + ((y - 29.5) / 1.4) ** 2 <= 1:
                if 0 <= x < N:
                    grid[y][x] = GROUND

    body = {}
    for y in range(N):
        for x in range(N):
            if inside(x, y):
                body[(x, y)] = SHELL["base"]
    for (x, y) in list(body):
        d = (x - CX) * 0.7 + (y - CY) * 0.7
        if d > 5.5 or not inside(x + 1, y) or not inside(x, y + 1):
            body[(x, y)] = SHELL["shadow"]
        elif d < -5.5 or not inside(x - 1, y) or not inside(x, y - 1):
            body[(x, y)] = SHELL["light"]

    def spot(cx, cy, rx, ry, pal):
        for (x, y) in list(body):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1:
                d = (x - cx) * 0.7 + (y - cy) * 0.7
                body[(x, y)] = pal["shadow"] if d > 1.2 else (pal["light"] if d < -1.4 else pal["base"])

    spot(11.5, 14.5, 3.0, 2.6, SPOT_A)
    spot(20.5, 21.0, 3.6, 3.0, SPOT_A)
    spot(13.0, 25.0, 2.4, 1.9, SPOT_B)
    spot(20.0, 12.0, 2.0, 1.7, SPOT_B)

    if cracked:
        crack = [(16, 7), (16, 8), (15, 9), (15, 10), (16, 11), (16, 12), (17, 13), (17, 14), (16, 15), (18, 15)]
        for (x, y) in crack:
            if (x, y) in body:
                body[(x, y)] = INK
        for (x, y) in [(17, 9), (17, 10), (17, 11), (18, 12), (18, 13), (18, 14)]:
            if (x, y) in body:
                body[(x, y)] = GLOW

    for (x, y), c in body.items():
        put(x, y, c)

    # selective outline: dark on the shadow side, lighter on the lit side
    solid = {(x, y) for y in range(N) for x in range(N) if grid[y][x] and grid[y][x] != GROUND}
    for y in range(N):
        for x in range(N):
            if (x, y) in solid or grid[y][x] == GROUND:
                continue
            if ((x - 1, y) in solid) or ((x, y - 1) in solid):
                grid[y][x] = INK
            elif ((x + 1, y) in solid) or ((x, y + 1) in solid):
                grid[y][x] = INK_LIT
    return grid


def to_image(grid):
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    for y in range(N):
        for x in range(N):
            if grid[y][x]:
                im.putpixel((x, y), tuple(grid[y][x]) + (255,))
    return im.resize((N * 2, N * 2), Image.NEAREST)


def main():
    frames = [build(), build(dx=-1), build(dx=1), build(cracked=True)]
    sheet = Image.new("RGBA", (64 * len(frames), 64), (0, 0, 0, 0))
    for i, g in enumerate(frames):
        sheet.alpha_composite(to_image(g), (i * 64, 0))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT)
    print("wrote", OUT, sheet.size)


if __name__ == "__main__":
    main()
