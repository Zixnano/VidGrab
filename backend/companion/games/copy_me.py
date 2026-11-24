"""Copy Me: watch the pet's pattern, then repeat it on the D-pad. One step longer every round.

Calm, no timer. Twelve rounds is a perfect run.
"""
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPolygonF

from .engine import ACCENT, DIM, DIRS, GOLD, GREEN, INK, PANEL, RED, W, Game, draw_pet, text

MAX_ROUNDS = 12
SHOW_ON, SHOW_GAP = 0.45, 0.16
CENTER = (120.0, 84.0)
ARROW_AT = {"up": (120.0, 50.0), "down": (120.0, 118.0), "left": (74.0, 84.0), "right": (166.0, 84.0)}


class CopyMe(Game):
    id = "copy_me"
    title = "Copy Me"
    blurb = "Watch, then repeat the moves."
    controls = "D-pad: repeat the pattern"

    def reset(self):
        super().reset()
        self.round = 1
        self.seq = []
        self.state = "intro"
        self.t = 0.9
        self.show_i = 0
        self.in_i = 0
        self.lit = None
        self.lit_left = 0.0
        self.phase = "gap"
        self.pose = "curious"
        self._extend()

    def _extend(self):
        self.seq = [self.rng.choice(DIRS) for _ in range(self.round)] if not self.seq else self.seq + [self.rng.choice(DIRS)]

    def press(self, key):
        if self.paused or self.done or self.state != "input" or key not in DIRS:
            return
        self.lit, self.lit_left = key, 0.16
        if key == self.seq[self.in_i]:
            self.in_i += 1
            self.pose = "excited"
            if self.in_i == len(self.seq):
                self.score = self.round
                self.state, self.t, self.pose = "good", 0.8, "celebrating"
        else:
            self.state, self.t, self.pose = "wrong", 1.0, "sad"

    def update(self, dt):
        if self.paused or self.done:
            return
        if self.lit_left > 0:
            self.lit_left -= dt
            if self.lit_left <= 0:
                self.lit = None
        self.t -= dt
        if self.state == "intro" and self.t <= 0:
            self._begin_show()
        elif self.state == "show" and self.t <= 0:
            if self.phase == "on":                              # arrow finished lighting up
                self.lit, self.phase, self.t = None, "gap", SHOW_GAP
                self.show_i += 1
            elif self.show_i >= len(self.seq):                  # whole pattern shown
                self.state, self.in_i, self.pose, self.t = "input", 0, "curious", 0.0
            else:
                self.lit, self.phase, self.t = self.seq[self.show_i], "on", SHOW_ON
                self.pose = "excited"
        elif self.state == "good" and self.t <= 0:
            if self.round >= MAX_ROUNDS:
                self.done = True
            else:
                self.round += 1
                self._extend()
                self._begin_show()
        elif self.state == "wrong" and self.t <= 0:
            self.done = True

    def _begin_show(self):
        self.state, self.show_i, self.in_i, self.t, self.lit = "show", 0, 0, 0.35, None
        self.phase = "gap"
        self.pose = "curious"

    def draw(self, p):
        draw_pet(p, self.ctx, self.pose, CENTER[0] - 22, CENTER[1] - 24, 44)
        for key, (x, y) in ARROW_AT.items():
            on = self.lit == key
            col = GOLD if on else PANEL
            edge = INK if on else DIM
            p.setBrush(col)
            p.setPen(edge)
            p.drawPolygon(QPolygonF(self._arrow(key, x, y)))
        text(p, "ROUND %d" % self.round, 6, 5, 10, INK)
        label = {"intro": "Get ready", "show": "Watch...", "input": "Your turn!", "good": "Nice!", "wrong": "Oops!"}[self.state]
        color = {"input": GREEN, "good": GREEN, "wrong": RED}.get(self.state, ACCENT)
        text(p, label, W / 2, 142, 11, color, "center", 120)

    @staticmethod
    def _arrow(key, x, y):
        s = 13.0
        if key == "up":
            return [QPointF(x, y - s), QPointF(x + s, y + s * 0.6), QPointF(x - s, y + s * 0.6)]
        if key == "down":
            return [QPointF(x, y + s), QPointF(x + s, y - s * 0.6), QPointF(x - s, y - s * 0.6)]
        if key == "left":
            return [QPointF(x - s, y), QPointF(x + s * 0.6, y - s), QPointF(x + s * 0.6, y + s)]
        return [QPointF(x + s, y), QPointF(x - s * 0.6, y - s), QPointF(x - s * 0.6, y + s)]
