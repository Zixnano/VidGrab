"""Companion rules. No Qt, network, downloader or filesystem access (the store owns all I/O).

Thread safety: the Qt thread, Flask request threads and the bridge worker all call into one
CompanionCore, so every public mutator takes one re-entrant lock. Listeners are called after the
lock is released, so a slow or failing listener can never stall or deadlock the pet.
"""
from __future__ import annotations

import functools
import hashlib
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from . import diag
from .animation import pose_for, speech_for

XP_DAILY_CAP = 500
BASE_DOWNLOAD_XP = 12
MIN_DOWNLOAD_BYTES = 100 * 1024
TICK_SECONDS_PER_POINT = 1500          # a full need point takes about 25 active minutes

# Care is a comfort loop, not an income source: short cooldowns and small daily ceilings.
CARE_COOLDOWN_S = {"feed": 3.0, "play": 15.0, "pet": 4.0, "nap": 45.0}
CARE_DAILY_COIN_CAP = 8
CARE_DAILY_XP_CAP = 30
GAME_DAILY_COIN_CAP = 12

_CARE_REACTION = {"feed": ("happy", 3.0), "play": ("excited", 4.0), "pet": ("happy", 3.0), "nap": ("sleeping", 5.0)}
_EVENT_REACTION = {"celebrating": 6.0, "sad": 5.0, "curious": 4.0, "happy": 4.0, "excited": 5.0, "sleeping": 5.0}

GAMES = {
    "catch_bytes": {"title": "Catch the Bytes", "max": 30, "per": 3},
    "snake": {"title": "Treat Trail", "max": 24, "per": 3},
    "copy_me": {"title": "Copy Me", "max": 12, "per": 2},
}


def stage_for_level(level: int) -> str:
    """Egg below level 3, baby below 8, kid below 16, then adult."""
    return "egg" if level < 3 else "baby" if level < 8 else "kid" if level < 16 else "adult"


def next_stage(level: int):
    """(stage name, level it arrives at) or None when fully grown."""
    for stage, first in (("baby", 3), ("kid", 8), ("adult", 16)):
        if level < first:
            return stage, first
    return None


def xp_needed(level: int) -> int:
    return max(50, int(50 * max(1, level) ** 1.5))


@dataclass
class PetState:
    enabled: bool = False
    species: str = "puppy"
    name: str = "Mochi"
    stage: str = "egg"
    level: int = 1
    xp: int = 0
    hunger: int = 82
    energy: int = 88
    affection: int = 65
    mood: str = "happy"
    coins: int = 0
    streak: int = 0
    last_seen: float = 0.0
    last_daily: str = ""
    last_open_day: str = ""
    daily_xp: int = 0
    downloads_counted: int = 0
    equipped_cosmetic: str = "none"
    games_played: int = 0
    daily_care_day: str = ""
    daily_care_coins: int = 0
    daily_care_xp: int = 0
    bubbles: bool = True
    animations: bool = True
    best_scores: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        allowed = cls.__dataclass_fields__
        clean = {k: v for k, v in (data or {}).items() if k in allowed}
        try:
            state = cls(**clean)
        except TypeError:
            state = cls()
        for key in ("hunger", "energy", "affection"):
            setattr(state, key, max(0, min(100, _to_int(getattr(state, key), 50))))
        state.level = max(1, _to_int(state.level, 1))
        state.xp = max(0, _to_int(state.xp, 0))
        state.coins = max(0, _to_int(state.coins, 0))
        state.streak = max(0, _to_int(state.streak, 0))
        state.enabled = bool(state.enabled)
        state.bubbles = bool(state.bubbles)
        state.animations = bool(state.animations)
        scores = state.best_scores if isinstance(state.best_scores, dict) else {}
        state.best_scores = {str(k): max(0, _to_int(v, 0)) for k, v in scores.items() if str(k) in GAMES}
        state.stage = stage_for_level(state.level)
        return state

    def to_dict(self):
        return asdict(self)


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def derive_mood(p: PetState) -> str:
    if p.energy < 20:
        return "sleeping"
    if p.hunger < 20:
        return "hungry"
    if p.affection < 20:
        return "lonely"
    if p.energy > 75 and p.affection > 65:
        return "happy"
    if p.hunger < 45:
        return "curious"
    return "content"


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            with self._lock:
                self._depth += 1
                try:
                    return fn(self, *args, **kwargs)
                finally:
                    self._depth -= 1
        finally:
            if self._depth == 0:
                self._flush()
    return wrapper


class CompanionCore:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._depth = 0
        self._outbox = []
        self._listeners = []
        self.state = PetState.from_dict(store.load_state())
        self._last_tick = time.time()
        self._decay_accum = 0.0
        self._session_download_ids = set()
        self._cooldowns = {}
        self._reaction = (None, 0.0)
        self._unlocked = set(store.list_unlocks())
        self._game_cache = (None, 0)       # (day, treats already paid today)
        self._refresh_mood()
        self._check_achievements()

    # ---- listeners ------------------------------------------------------
    def add_listener(self, fn):
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)
        return fn

    def remove_listener(self, fn):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _emit(self, kind, data=None):
        self._outbox.append((kind, data or {}))

    def _flush(self):
        with self._lock:
            if not self._outbox:
                return
            batch, self._outbox = self._outbox, []
            listeners = tuple(self._listeners)
        for kind, data in batch:
            for fn in listeners:
                try:
                    fn(kind, data)
                except Exception as exc:
                    diag.note("core_listener", "UI listener failed (isolated)", exc)

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d")

    def _refresh_mood(self):
        self.state.mood = derive_mood(self.state)

    def _save(self):
        self._refresh_mood()
        self.state.stage = stage_for_level(self.state.level)
        self.store.save_state(self.state.to_dict())
        self._emit("changed")

    def _unlock(self, key):
        if self.store.unlock_once(key):
            self._unlocked.add(key)
            return True
        self._unlocked.add(key)
        return False

    def is_unlocked(self, key):
        if key in self._unlocked:
            return True
        if self.store.is_unlocked(key):     # unlocked by another path (tests, a restored backup)
            self._unlocked.add(key)
            return True
        return False

    @_locked
    def react(self, name, seconds=None):
        """Show a pose for a few seconds (celebrate a download, look sad after a failure)."""
        if not self.state.enabled or name not in _EVENT_REACTION:
            return
        self._reaction = (name, time.time() + float(seconds or _EVENT_REACTION[name]))
        self._emit("changed")

    def reaction(self):
        name, until = self._reaction
        return name if name and time.time() < until else None

    # ---- on / off ----------------------------------------------------------
    @_locked
    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if self.state.enabled == enabled:
            return
        self.state.enabled = enabled
        self._last_tick = time.time()
        if not enabled:
            self._reaction = (None, 0.0)
        if enabled:
            self.mark_daily_open()
        self._save()

    @_locked
    def set_option(self, name, value):
        if name not in ("bubbles", "animations"):
            return {"ok": False, "error": "Unknown option"}
        setattr(self.state, name, bool(value))
        self._save()
        return {"ok": True, "state": self.snapshot()}

    @_locked
    def mark_daily_open(self, day=None):
        """Count one enabled app visit per local day. There is no background or off-state decay."""
        if not self.state.enabled:
            return self.state.streak
        day = day or self._today()
        if self.state.last_open_day == day:
            return self.state.streak
        try:
            yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        except ValueError:
            yesterday = ""
        self.state.streak = self.state.streak + 1 if self.state.last_open_day == yesterday else 1
        self.state.last_open_day = day
        self.state.last_seen = time.time()
        self.store.add_event("daily_open", {"day": day, "streak": self.state.streak})
        self._check_achievements()
        self._save()
        return self.state.streak

    @_locked
    def tick(self, now=None):
        """Needs decay only during enabled, active app time. Returns True if a need changed."""
        now = now or time.time()
        if not self.state.enabled:
            self._last_tick = now
            return False
        elapsed = max(0.0, min(3600.0, now - self._last_tick))
        self._last_tick = now
        self._decay_accum += elapsed
        loss = int(self._decay_accum // TICK_SECONDS_PER_POINT)
        self._decay_accum %= TICK_SECONDS_PER_POINT
        if not loss:
            return False
        self.state.hunger = max(0, self.state.hunger - loss)
        self.state.energy = max(0, self.state.energy - max(0, loss - 1))
        self.state.affection = max(0, self.state.affection - max(0, loss - 2))
        self._save()
        return True

    # ---- progression ---------------------------------------------------------
    def _add_xp(self, amount):
        before_level, before_stage = self.state.level, self.state.stage
        self.state.xp += max(0, int(amount))
        while self.state.xp >= xp_needed(self.state.level):
            self.state.xp -= xp_needed(self.state.level)
            self.state.level += 1
        self.state.stage = stage_for_level(self.state.level)
        if self.state.level > before_level:
            self._emit("level_up", {"level": self.state.level})
        if self.state.stage != before_stage:
            self._emit("evolved", {"stage": self.state.stage, "from": before_stage})
            self._reaction = ("celebrating", time.time() + 8.0)

    @_locked
    def award_download(self, job):
        if not self.state.enabled or not isinstance(job, dict):
            return 0
        jid = str(job.get("id") or "")
        url = str(job.get("url") or "")
        try:
            size = int(job.get("size_total") or job.get("size_done") or 0)
        except (TypeError, ValueError):
            size = 0
        if not jid or jid in self._session_download_ids or not url or size < MIN_DOWNLOAD_BYTES:
            return 0
        if job.get("status") != "done":
            return 0
        fingerprint = hashlib.sha256(url.split("#", 1)[0].encode("utf-8", "replace")).hexdigest()
        if self.store.has_download_fingerprint(fingerprint):
            return 0
        day = self._today()
        if self.state.last_daily != day:
            self.state.last_daily, self.state.daily_xp = day, 0
        size_bonus = min(12, int(math.log2(max(1, size / (1024 * 1024))) * 2))
        bonus = 3 if self.state.streak else 0
        gain = min(BASE_DOWNLOAD_XP + size_bonus + bonus, max(0, XP_DAILY_CAP - self.state.daily_xp))
        if gain <= 0:
            return 0
        if not self.store.record_download(fingerprint, jid, gain):
            return 0
        self._session_download_ids.add(jid)
        self.state.daily_xp += gain
        self.state.downloads_counted += 1
        self.state.coins += 1 + (1 if size >= 100 * 1024 * 1024 else 0)
        self.state.affection = min(100, self.state.affection + 2)
        self._add_xp(gain)
        self.store.add_event("download", {"job_id": jid, "xp": gain})
        self._reaction = ("celebrating", time.time() + _EVENT_REACTION["celebrating"])
        self._emit("download", {"xp": gain})
        self._check_achievements()
        self._save()
        return gain

    @_locked
    def note_failure(self, job_id=""):
        """A failed download makes the pet sad for a moment. It never costs anything."""
        if not self.state.enabled:
            return
        self.store.add_event("download_failed", {"job_id": str(job_id)})
        self.react("sad")

    @_locked
    def note_queue_active(self):
        if self.state.enabled and self.reaction() is None:
            self.react("curious")

    # ---- care ------------------------------------------------------------------
    def _care_day_reset(self):
        day = self._today()
        if self.state.daily_care_day != day:
            self.state.daily_care_day = day
            self.state.daily_care_coins = 0
            self.state.daily_care_xp = 0

    @_locked
    def care(self, action):
        if not self.state.enabled:
            return {"ok": False, "error": "Companion is turned off"}
        if action not in CARE_COOLDOWN_S:
            return {"ok": False, "error": "Unknown care action"}
        p = self.state
        now = time.time()
        wait = self._cooldowns.get(action, 0.0) - now
        if wait > 0:
            return {"ok": False, "error": "Give me a moment (%d s)" % max(1, math.ceil(wait))}
        self._care_day_reset()
        if action == "feed":
            if p.hunger >= 95:
                return {"ok": False, "error": "I'm full, thank you!"}
            if p.coins < 1:
                return {"ok": False, "error": "You need a treat first"}
            p.coins -= 1
            p.hunger = min(100, p.hunger + 25)
            p.affection = min(100, p.affection + 2)
        elif action == "play":
            if p.energy < 12:
                return {"ok": False, "error": "Too sleepy to play right now"}
            p.energy = max(0, p.energy - 12)
            p.affection = min(100, p.affection + 14)
            if p.daily_care_coins < CARE_DAILY_COIN_CAP:
                p.daily_care_coins += 1
                p.coins += 1
            xp = min(2, max(0, CARE_DAILY_XP_CAP - p.daily_care_xp))
            if xp:
                p.daily_care_xp += xp
                self._add_xp(xp)
        elif action == "pet":
            p.affection = min(100, p.affection + 10)
        elif action == "nap":
            if p.energy > 70:
                return {"ok": False, "error": "Not sleepy yet"}
            p.energy = min(100, p.energy + 30)
        self._cooldowns[action] = now + CARE_COOLDOWN_S[action]
        reaction, seconds = _CARE_REACTION[action]
        self._reaction = (reaction, now + seconds)
        self.store.add_event("care", {"action": action})
        self._check_achievements()
        self._save()
        return {"ok": True, "state": p.to_dict()}

    # ---- unlocks and achievements -------------------------------------------------
    ACHIEVEMENTS = (
        ("first_download", "First Fetch", "Finish your first eligible download."),
        ("download_10", "Download Pal", "Count 10 eligible downloads."),
        ("download_100", "Century Club", "Count 100 eligible downloads."),
        ("level_5", "Growing Up", "Reach level 5."),
        ("level_10", "Big Personality", "Reach level 10."),
        ("hatched", "Hatched!", "Help your egg hatch (level 3)."),
        ("streak_3", "Regular Visitor", "Build a 3-day streak."),
        ("first_game", "Playtime", "Play any mini-game once."),
        ("all_games", "Arcade Regular", "Play all three mini-games."),
    )

    def _check_achievements(self):
        p = self.state
        rules = {
            "first_download": (p.downloads_counted >= 1, "sticker_first_fetch"),
            "download_10": (p.downloads_counted >= 10, "species_cat"),
            "download_100": (p.downloads_counted >= 100, "cosmetic_star_collar"),
            "level_5": (p.level >= 5, "cosmetic_flower_crown"),
            "level_10": (p.level >= 10, "species_axolotl"),
            "hatched": (p.level >= 3, "sticker_hatched"),
            "streak_3": (p.streak >= 3, "species_frog"),
            "first_game": (p.games_played >= 1, "sticker_arcade"),
            "all_games": (all(g in p.best_scores for g in GAMES), "cosmetic_arcade_bow"),
        }
        titles = {k: t for k, t, _ in self.ACHIEVEMENTS}
        fresh = []
        for key, (condition, reward) in rules.items():
            if condition and self._unlock(key):
                self._unlock(reward)
                self.store.add_event("achievement", {"id": key, "title": titles[key], "reward": reward})
                self._emit("achievement", {"id": key, "title": titles[key], "reward": reward})
                fresh.append(key)
        return fresh

    @_locked
    def set_species(self, species_id):
        if not self.state.enabled:
            return {"ok": False, "error": "Turn the companion on first"}
        entry = next((x for x in self.store.species_registry() if x.get("id") == species_id), None)
        if not entry:
            return {"ok": False, "error": "Unknown species"}
        if not entry.get("unlocked_by_default") and not self.is_unlocked("species_" + species_id):
            return {"ok": False, "error": "This companion is still locked"}
        self.state.species = species_id
        self.state.name = entry.get("name", entry.get("display_name", "Pal"))
        self._save()
        return {"ok": True, "state": self.snapshot()}

    @_locked
    def equip_cosmetic(self, cosmetic_id):
        if cosmetic_id != "none" and not self.is_unlocked(cosmetic_id):
            return {"ok": False, "error": "That cosmetic is still locked"}
        self.state.equipped_cosmetic = cosmetic_id
        self._save()
        return {"ok": True, "state": self.snapshot()}

    # ---- mini games -----------------------------------------------------------------
    def minigame_remaining(self, day=None):
        day = day or self._today()
        cached_day, used = self._game_cache
        if cached_day != day:
            used = self.store.daily_game_rewards(day)
            self._game_cache = (day, used)
        return max(0, GAME_DAILY_COIN_CAP - used)

    @_locked
    def award_minigame(self, score, game_id="catch_bytes"):
        if not self.state.enabled:
            return {"ok": False, "error": "Companion is turned off"}
        rules = GAMES.get(game_id)
        if rules is None:
            return {"ok": False, "error": "Unknown game"}
        score = max(0, min(rules["max"], _to_int(score, 0)))
        day = self._today()
        prior = self.store.daily_game_rewards(day)
        reward = min(max(0, score // rules["per"]), max(0, GAME_DAILY_COIN_CAP - prior))
        previous_best = self.state.best_scores.get(game_id, -1)
        new_best = score > max(0, previous_best)
        if score > previous_best:
            self.state.best_scores[game_id] = score
        self.state.games_played += 1
        self.state.coins += reward
        self._game_cache = (day, prior + reward)
        if reward:
            self.store.add_daily_game_rewards(day, reward)
            self._add_xp(min(5, reward))
        self.state.affection = min(100, self.state.affection + min(8, score // 4))
        self.store.add_event("minigame", {"game": game_id, "score": score, "coins": reward, "day": day})
        if score >= rules["max"] // 2:
            self._reaction = ("excited", time.time() + 4.0)
        self._check_achievements()
        self._save()
        return {"ok": True, "game": game_id, "score": score, "coins": reward, "new_best": new_best,
                "best": self.state.best_scores.get(game_id, score), "state": self.snapshot()}

    # ---- views ----------------------------------------------------------------------
    def achievements(self):
        unlocked = set(self.store.list_unlocks()) | self._unlocked
        return [{"id": k, "title": t, "description": d, "unlocked": k in unlocked} for k, t, d in self.ACHIEVEMENTS]

    def snapshot(self, full=False):
        """A small dict for the UI and the extension. `full=True` adds the heavy lists."""
        with self._lock:
            self._refresh_mood()
            p = self.state
            need = xp_needed(p.level)
            ratio = (p.xp / need) if need else 0.0
            progress = ((p.level - 1) + ratio) / 2.0 if p.stage == "egg" else ratio   # egg: progress toward level 3
            reaction = self.reaction()
            nxt = next_stage(p.level)
            d = p.to_dict()
            d.update({
                "xp_needed": need,
                "reaction": reaction,
                "pose": pose_for(p.mood, reaction, p.stage, progress),
                "speech": speech_for(p.mood, reaction, p.stage, p.enabled),
                "next_stage": {"stage": nxt[0], "level": nxt[1]} if nxt else None,
                "unlocked": sorted(self._unlocked),
                "games": {k: {"title": v["title"], "best": p.best_scores.get(k, 0)} for k, v in GAMES.items()},
                "games_treats_left": self.minigame_remaining(),
            })
            if full:
                d["stickers"] = self.store.list_stickers()
                d["species_catalog"] = self.store.species_registry()
                d["achievements"] = self.achievements()
            return d

    @_locked
    def reset(self):
        self.store.reset()
        self.state = PetState.from_dict({})
        self._unlocked = set()
        self._session_download_ids = set()
        self._cooldowns = {}
        self._reaction = (None, 0.0)
        self._game_cache = (None, 0)
        self._save()
