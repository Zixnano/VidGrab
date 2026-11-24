"""Pose choice, speech lines and frame sequencing. Pure Python: no Qt, no I/O.

The Qt dialog, the mini-games and (through species.json) the browser extension all use the
same rules, so the pet looks the same everywhere. Timing numbers live in species.json under
"animation" so they can be tuned without touching code.
"""
from __future__ import annotations

POSES = ("idle", "content", "happy", "curious", "sleeping", "sad", "excited", "celebrating")
EGG_POSES = ("idle", "wobble", "cracked")

DEFAULT_ANIMATION = {
    "fps": {"idle": 4, "content": 4, "happy": 4, "curious": 3, "sleeping": 2, "sad": 2,
            "excited": 6, "celebrating": 6, "wobble": 5, "cracked": 4},
    "bob": {"idle": [900, 1], "content": [900, 1], "happy": [500, 1], "curious": [700, 1],
            "sleeping": [1600, 1], "sad": [0, 0], "excited": [350, 2], "celebrating": [300, 3],
            "wobble": [0, 0], "cracked": [500, 1]},
    "blink_period_ms": 4700,
    "blink_hold_ms": 150,
}

_MOOD_POSE = {
    "sleeping": "sleeping", "hungry": "sad", "lonely": "sad",
    "curious": "curious", "happy": "idle", "content": "idle",
}

_SPEECH = {
    "sleeping": "Zzz...",
    "hungry": "Could I have a snack?",
    "lonely": "A little attention?",
    "happy": "All caught up!",
    "content": "Keeping you company!",
    "curious": "What's downloading?",
}
_REACTION_SPEECH = {
    "celebrating": "Got it! Download done!",
    "excited": "That was fun!",
    "happy": "Yay!",
    "curious": "Ooh, something is happening...",
    "sad": "Oh no, that one failed.",
    "sleeping": "Wake me when it's ready...",
}


def pose_for(mood: str, reaction: str | None = None, stage: str = "kid", xp_ratio: float = 0.0) -> str:
    """Which pose to show. A live reaction wins over mood. An egg only has three poses."""
    if stage == "egg":
        if reaction in ("celebrating", "excited", "happy"):
            return "wobble"
        return "cracked" if xp_ratio >= 0.75 else "idle"
    if reaction in POSES:
        return reaction
    return _MOOD_POSE.get(mood, "idle")


def speech_for(mood: str, reaction: str | None = None, stage: str = "kid", enabled: bool = True) -> str:
    if not enabled:
        return "I'm taking a little break. Turn me on when you're ready!"
    if stage == "egg":
        if reaction in ("celebrating", "excited", "happy"):
            return "The egg wiggles!"
        return "The egg is warm. Keep downloading!"
    if reaction in _REACTION_SPEECH:
        return _REACTION_SPEECH[reaction]
    return _SPEECH.get(mood, "Hello!")


def _cfg(registry_animation: dict | None) -> dict:
    cfg = dict(DEFAULT_ANIMATION)
    if registry_animation:
        for key, value in registry_animation.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                merged = dict(cfg[key])
                merged.update(value)
                cfg[key] = merged
            else:
                cfg[key] = value
    return cfg


class Animator:
    """Turns (pose, seconds) into (frame index, vertical offset in sprite pixels).

    `entry` is a species or egg entry from species.json ("frames", "blink").
    """

    def __init__(self, entry: dict, animation: dict | None = None):
        self.entry = entry or {}
        self.cfg = _cfg(animation)

    def frames_for(self, pose: str):
        frames = (self.entry.get("frames") or {})
        seq = frames.get(pose) or frames.get("idle") or [0]
        return [int(i) for i in seq]

    def blink_active(self, t: float) -> bool:
        period = max(1000, int(self.cfg.get("blink_period_ms", 4700)))
        hold = max(50, int(self.cfg.get("blink_hold_ms", 150)))
        phase = (t * 1000.0) % period
        return phase >= period - hold

    def frame(self, pose: str, t: float):
        seq = self.frames_for(pose)
        fps = float((self.cfg.get("fps") or {}).get(pose, 4)) or 4.0
        index = seq[int(t * fps) % len(seq)]
        blink = self.entry.get("blink")
        if blink is not None and pose in ("idle", "content") and self.blink_active(t):
            index = int(blink)
        period, amp = (self.cfg.get("bob") or {}).get(pose, [0, 0])
        dy = 0
        if period and amp:
            dy = int(amp) if int((t * 1000.0) // int(period)) % 2 else 0
        return index, dy


def sheet_cell(index: int, columns: int, frame_size: int = 64):
    """Pixel origin of a frame inside its sheet (row-major)."""
    columns = max(1, int(columns))
    return (index % columns) * frame_size, (index // columns) * frame_size
