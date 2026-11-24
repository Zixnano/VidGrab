"""Tiny engine for the shell-controlled mini-games.

Games draw on a fixed 240x160 canvas (the Game Boy Advance screen size). The overlay scales that
canvas to whatever space the main window gives it, with no smoothing, so pixels stay crisp.
Input is discrete (one press at a time) because the on-screen shell buttons are clicks, not held
keys. Every game is also playable from the keyboard.
"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont

W, H = 240, 160

BG = QColor("#1c1830")
BG2 = QColor("#241f3d")
PANEL = QColor("#2e2850")
INK = QColor("#eee9ff")
DIM = QColor("#9b93d6")
ACCENT = QColor("#a99cff")
PINK = QColor("#ff8fb3")
GOLD = QColor("#ffd34e")
GREEN = QColor("#8be28b")
RED = QColor("#ff6b7d")

DIRS = ("up", "down", "left", "right")


def text(p, s, x, y, size=10, color=INK, align="left", width=None):
    """Draw text with its top-left (or centre) at x, y in logical pixels."""
    font = QFont("Consolas")
    font.setPixelSize(int(size))
    font.setBold(True)
    p.setFont(font)
    p.setPen(color)
    w = width if width is not None else W
    flags = {"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}[align] | Qt.AlignTop
    if align == "center":
        rect = QRectF(x - w / 2.0, y, w, size * 2)
    elif align == "right":
        rect = QRectF(x - w, y, w, size * 2)
    else:
        rect = QRectF(x, y, w, size * 2)
    p.drawText(rect, int(flags), s)


def draw_pet(p, ctx, pose, x, y, size):
    pm = ctx.pet(pose)
    if pm is not None and not pm.isNull():
        p.drawPixmap(QRectF(x, y, size, size), pm, QRectF(0, 0, pm.width(), pm.height()))


def hearts(p, lives, x, y, total=3):
    for i in range(total):
        p.fillRect(QRectF(x + i * 11, y, 8, 7), PINK if i < lives else PANEL)
        p.fillRect(QRectF(x + i * 11 + 1, y - 1, 2, 2), PINK if i < lives else PANEL)
        p.fillRect(QRectF(x + i * 11 + 5, y - 1, 2, 2), PINK if i < lives else PANEL)


class Game:
    id = "game"
    title = "Game"
    blurb = ""
    controls = "D-pad: move   A: ok   B: back"

    def __init__(self, ctx):
        self.ctx = ctx
        self.rng = ctx.rng
        self.done = False
        self.score = 0
        self.paused = False
        self.reset()

    def reset(self):
        self.done = False
        self.score = 0
        self.paused = False

    def press(self, key):
        pass

    def update(self, dt):
        pass

    def draw(self, p):
        pass
