"""Treat Trail: a pet snake game. Steer with the D-pad, eat treats, never touch a wall or yourself.

The speed rises slowly. Start pauses.
"""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor

from .engine import BG2, GOLD, INK, PANEL, PINK, W, Game, draw_pet, text

COLS, ROWS, CELL = 22, 12, 10
OX, OY = 10, 30
VEC = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


class TreatTrail(Game):
    id = "snake"
    title = "Treat Trail"
    blurb = "Eat treats. Do not bump."
    controls = "D-pad: steer   Start: pause"

    def reset(self):
        super().reset()
        cx, cy = COLS // 2, ROWS // 2
        self.body = [(cx, cy), (cx - 1, cy), (cx - 2, cy)]
        self.dir = "right"
        self.queue = []
        self.acc = 0.0
        self.grow = 0
        self.food = None
        self._place_food()

    def press(self, key):
        if key in VEC and not self.done and not self.paused:
            last = self.queue[-1] if self.queue else self.dir
            if key != last and key != OPPOSITE[last] and len(self.queue) < 2:
                self.queue.append(key)

    def _place_food(self):
        free = [(x, y) for x in range(COLS) for y in range(ROWS) if (x, y) not in self.body]
        self.food = self.rng.choice(free) if free else None
        if self.food is None:
            self.done = True

    def _interval(self):
        return max(0.075, 0.18 - 0.004 * self.score)

    def update(self, dt):
        if self.paused or self.done:
            return
        self.acc += dt
        while self.acc >= self._interval() and not self.done:
            self.acc -= self._interval()
            self._step()

    def _step(self):
        if self.queue:
            self.dir = self.queue.pop(0)
        dx, dy = VEC[self.dir]
        hx, hy = self.body[0]
        head = (hx + dx, hy + dy)
        tail_leaves = self.grow == 0
        blocked = self.body[:-1] if tail_leaves else self.body
        if not (0 <= head[0] < COLS and 0 <= head[1] < ROWS) or head in blocked:
            self.done = True
            return
        self.body.insert(0, head)
        if head == self.food:
            self.score += 1
            self.grow += 1
            self._place_food()
        if self.grow > 0:
            self.grow -= 1
        else:
            self.body.pop()

    def draw(self, p):
        p.fillRect(QRectF(OX - 2, OY - 2, COLS * CELL + 4, ROWS * CELL + 4), PANEL)
        p.fillRect(QRectF(OX, OY, COLS * CELL, ROWS * CELL), BG2)
        for y in range(ROWS):                                         # faint checker
            for x in range(COLS):
                if (x + y) % 2 == 0:
                    p.fillRect(QRectF(OX + x * CELL, OY + y * CELL, CELL, CELL), QColor(255, 255, 255, 6))
        if self.food:
            fx, fy = OX + self.food[0] * CELL, OY + self.food[1] * CELL
            p.fillRect(QRectF(fx + 1, fy + 1, CELL - 2, CELL - 2), PINK)
            p.fillRect(QRectF(fx + 2, fy + 2, 3, 2), INK)
            p.fillRect(QRectF(fx + 6, fy + 5, 2, 2), QColor("#b84a6e"))
        n = len(self.body)
        for i, (x, y) in enumerate(self.body):
            shade = 1.0 - 0.45 * (i / max(1, n))
            col = QColor(int(139 * shade + 20), int(226 * shade), int(139 * shade + 20))
            p.fillRect(QRectF(OX + x * CELL + 1, OY + y * CELL + 1, CELL - 2, CELL - 2), col)
        hx, hy = self.body[0]
        px, py = OX + hx * CELL, OY + hy * CELL
        p.fillRect(QRectF(px, py, CELL, CELL), QColor("#b6f5b6"))
        vx, vy = VEC[self.dir]
        for sx, sy in ((2, 2), (6, 2)) if vy else ((2, 2), (2, 6)) if vx else ((2, 2), (6, 2)):
            p.fillRect(QRectF(px + sx + vx, py + sy + vy, 2, 2), INK)
        text(p, "TREATS %d" % self.score, 6, 8, 10, INK)
        if self.paused:
            text(p, "PAUSED", W / 2, 10, 10, GOLD, "center", 80)
