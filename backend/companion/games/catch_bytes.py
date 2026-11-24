"""Catch the Bytes: slide your pet left and right to catch falling files and dodge the 404s.

30 seconds, three hearts. Bytes are +1, golden stars +3, a 404 costs a heart.
"""
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPolygonF

from .engine import (BG2, GOLD, GREEN, INK, PANEL, PINK, RED, W, H, Game, draw_pet, hearts, text)

LANES = 5
LANE_W = W / LANES
PLAYER = 34
PLAYER_TOP = 112
DURATION = 30.0


class CatchBytes(Game):
    id = "catch_bytes"
    title = "Catch the Bytes"
    blurb = "Catch files, dodge the 404s."
    controls = "Left / Right: move"

    def reset(self):
        super().reset()
        self.lane = LANES // 2
        self.time_left = DURATION
        self.lives = 3
        self.items = []
        self.spawn_in = 0.6
        self.elapsed = 0.0
        self.pose = "idle"
        self.pose_left = 0.0

    def press(self, key):
        if self.paused or self.done:
            return
        if key == "left":
            self.lane = max(0, self.lane - 1)
        elif key == "right":
            self.lane = min(LANES - 1, self.lane + 1)

    def _spawn(self):
        roll = self.rng.random()
        kind = "star" if roll < 0.1 else "bug" if roll < 0.32 else "byte"
        speed = min(135.0, 58.0 + self.elapsed * 1.6)
        self.items.append({"lane": self.rng.randrange(LANES), "y": -12.0, "kind": kind, "speed": speed})

    def update(self, dt):
        if self.paused or self.done:
            return
        self.elapsed += dt
        self.time_left -= dt
        self.pose_left = max(0.0, self.pose_left - dt)
        if self.pose_left == 0.0:
            self.pose = "idle"
        self.spawn_in -= dt
        if self.spawn_in <= 0:
            self._spawn()
            self.spawn_in = max(0.34, 0.78 - self.elapsed * 0.012)
        keep = []
        for it in self.items:
            it["y"] += it["speed"] * dt
            if it["lane"] == self.lane and PLAYER_TOP + 4 <= it["y"] + 12 and it["y"] <= PLAYER_TOP + 26:
                if it["kind"] == "bug":
                    self.lives -= 1
                    self.pose, self.pose_left = "sad", 0.7
                else:
                    self.score += 3 if it["kind"] == "star" else 1
                    self.pose, self.pose_left = "excited", 0.35
                continue
            if it["y"] < H:
                keep.append(it)
        self.items = keep
        if self.time_left <= 0 or self.lives <= 0:
            self.time_left = max(0.0, self.time_left)
            self.done = True

    def draw(self, p):
        for lane in range(LANES):                                  # soft lane stripes
            if lane % 2 == 0:
                p.fillRect(QRectF(lane * LANE_W, 0, LANE_W, H), BG2)
        p.fillRect(QRectF(0, 148, W, 12), PANEL)
        for it in self.items:
            cx = it["lane"] * LANE_W + LANE_W / 2
            self._item(p, it["kind"], cx, it["y"])
        cx = self.lane * LANE_W + LANE_W / 2
        draw_pet(p, self.ctx, self.pose, cx - PLAYER / 2, PLAYER_TOP, PLAYER)
        text(p, "BYTES %d" % self.score, 6, 5, 10, INK)
        text(p, "%d" % max(0, int(self.time_left + 0.99)), W / 2, 5, 10, GOLD, "center", 40)
        hearts(p, self.lives, W - 38, 7)

    @staticmethod
    def _item(p, kind, cx, y):
        if kind == "byte":
            p.fillRect(QRectF(cx - 6, y, 12, 12), GREEN)
            p.fillRect(QRectF(cx - 4, y + 2, 4, 3), INK)
            p.fillRect(QRectF(cx - 6, y + 9, 12, 3), PANEL)
        elif kind == "star":
            p.setBrush(GOLD)
            p.setPen(GOLD)
            pts = [QPointF(cx, y - 2), QPointF(cx + 3, y + 4), QPointF(cx + 8, y + 5), QPointF(cx + 4, y + 9),
                   QPointF(cx + 5, y + 14), QPointF(cx, y + 11), QPointF(cx - 5, y + 14), QPointF(cx - 4, y + 9),
                   QPointF(cx - 8, y + 5), QPointF(cx - 3, y + 4)]
            p.drawPolygon(QPolygonF(pts))
        else:
            p.fillRect(QRectF(cx - 9, y, 18, 12), RED)
            text(p, "404", cx, y + 1, 9, INK, "center", 24)
