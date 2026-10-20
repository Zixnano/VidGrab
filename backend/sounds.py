"""sounds.py: completion sound for Video Grabber (v5.5).

The old behavior was winsound.MessageBeep, which plays a Windows *system*
sound and is silent when the Windows sound scheme is "No Sounds". This module
plays real audio files instead:

  1. a bundled preset (sounds\\<id>.wav next to the app code; if the file is
     missing it is synthesized into <data folder>\\sounds\\ on first use, so a
     preset always exists), or the user's own .wav / .mp3 file;
  2. if that fails or the file is missing: winsound.MessageBeep;
  3. if that fails too: silent.

Every step is logged, so a silent failure shows up in the log.
"""
import ctypes
import itertools
import math
import struct
import sys
import threading
import wave
from pathlib import Path

try:
    from logging_setup import log
except Exception:                      # standalone use (tests)
    def log(msg):
        print(msg)

try:
    import settings as _settings
    STATE = _settings.STATE
except Exception:                      # standalone use (tests)
    _settings, STATE = None, {}


def _home():
    # Read at call time: settings.set_paths() can move HOME (portable mode).
    return Path(_settings.HOME) if _settings is not None else Path(".")

DEFAULT_PRESET = "chime"
CUSTOM = "custom"
# id -> label shown in Settings
PRESETS = {
    "chime": "Chime",
    "ding": "Ding",
    "pop": "Pop",
    "double": "Double beep",
}
_RATE = 44100
_ALIAS = itertools.count(1)


# ---------------------------------------------------------------- synthesis
def _tone(buf, freq, start, dur, amp, decay, harmonics=((1, 1.0),)):
    """Add a decaying sine (plus optional overtones) into the sample buffer."""
    n0 = int(start * _RATE)
    n = int(dur * _RATE)
    for i in range(n):
        idx = n0 + i
        if idx >= len(buf):
            break
        t = i / _RATE
        env = math.exp(-t * decay) * min(1.0, i / (0.004 * _RATE))   # 4 ms attack: no click
        s = sum(a * math.sin(2 * math.pi * freq * h * t) for h, a in harmonics)
        buf[idx] += amp * env * s


def _synth(preset):
    """Return 16-bit mono samples (list of ints) for a preset id."""
    if preset == "ding":
        buf = [0.0] * int(0.9 * _RATE)
        _tone(buf, 880.0, 0.0, 0.9, 0.55, 5.5, ((1, 1.0), (2, 0.25), (3, 0.08)))
    elif preset == "pop":
        buf = [0.0] * int(0.25 * _RATE)
        _tone(buf, 520.0, 0.0, 0.2, 0.6, 22.0, ((1, 1.0), (2, 0.2)))
    elif preset == "double":
        buf = [0.0] * int(0.5 * _RATE)
        _tone(buf, 880.0, 0.0, 0.14, 0.5, 14.0)
        _tone(buf, 880.0, 0.2, 0.14, 0.5, 14.0)
    else:                                  # "chime": three rising notes
        buf = [0.0] * int(1.3 * _RATE)
        for k, f in enumerate((1046.5, 1318.5, 1568.0)):
            _tone(buf, f, 0.0 + 0.1 * k, 1.0, 0.32, 4.5, ((1, 1.0), (2, 0.2)))
    peak = max(1e-9, max(abs(x) for x in buf))
    scale = min(1.0, 0.85 / peak)
    return [int(max(-1.0, min(1.0, x * scale)) * 32767) for x in buf]


def write_preset(preset, path):
    """Write a preset to a 16-bit mono 44.1 kHz .wav file."""
    samples = _synth(preset)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return path


# ---------------------------------------------------------------- resolving
def bundled_dir():
    return Path(__file__).resolve().parent / "sounds"


def preset_path(preset):
    """Path to a preset's .wav: the bundled file if it shipped, else one
    synthesized into <data folder>\\sounds\\ (created once)."""
    if preset not in PRESETS:
        preset = DEFAULT_PRESET
    p = bundled_dir() / f"{preset}.wav"
    if p.is_file():
        return p
    user = _home() / "sounds" / f"{preset}.wav"
    if not user.is_file():
        log(f"sound: bundled {preset}.wav not found, generating {user}")
        write_preset(preset, user)
    return user


def resolve(preset=None, custom_path=None):
    """(path, description). path is None when nothing usable is selected."""
    preset = preset if preset is not None else STATE.get("completion_sound", DEFAULT_PRESET)
    custom_path = custom_path if custom_path is not None else STATE.get("completion_sound_path", "")
    if preset == CUSTOM:
        p = Path(str(custom_path or ""))
        if str(custom_path or "").strip() and p.is_file():
            return p, f"custom file {p}"
        return None, f"custom file missing ({custom_path or 'no file chosen'})"
    return preset_path(preset), f"preset {preset}"


# ---------------------------------------------------------------- playback
def _mci(cmd):
    """One MCI command (winmm). Raises RuntimeError with Windows' own text."""
    winmm = ctypes.windll.winmm
    buf = ctypes.create_unicode_buffer(256)
    rc = winmm.mciSendStringW(cmd, buf, 255, None)
    if rc:
        err = ctypes.create_unicode_buffer(256)
        winmm.mciGetErrorStringW(rc, err, 255)
        raise RuntimeError(f"MCI error {rc}: {err.value} ({cmd})")
    return buf.value


def _play_mci(path):
    """mp3 (and any non-wav) through MCI. Opens synchronously so a bad file
    raises here; the playback itself runs in a daemon thread."""
    alias = f"vgsnd{next(_ALIAS)}"
    kind = " type mpegvideo" if path.suffix.lower() == ".mp3" else ""
    _mci(f'open "{path}"{kind} alias {alias}')

    def run():
        try:
            _mci(f"play {alias} wait")
        except Exception as e:
            log(f"sound: MCI playback failed: {e}")
        finally:
            try:
                _mci(f"close {alias}")
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name="vg-sound").start()


def _play_file(path):
    if sys.platform != "win32":
        raise RuntimeError("sound playback is Windows-only")
    if path.suffix.lower() == ".wav":
        import winsound
        # SND_FILENAME plays the file itself, so the Windows sound scheme
        # ("No Sounds") doesn't matter. SND_NODEFAULT: no fallback beep.
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
    else:
        _play_mci(path)


def _beep():
    import winsound
    winsound.MessageBeep(winsound.MB_ICONASTERISK)


def play_completion_sound(preset=None, custom_path=None, why="completion"):
    """Play the selected sound. Selection defaults to STATE; Settings'
    Test button passes the unsaved choice. Returns True when the file was
    started, False when it fell back to MessageBeep or stayed silent."""
    path, desc = None, ""
    try:
        path, desc = resolve(preset, custom_path)
        if path is not None:
            log(f"sound ({why}): playing {desc} -> {path}")
            _play_file(path)
            return True
        log(f"sound ({why}): {desc}; falling back to MessageBeep")
    except Exception as e:
        log(f"sound ({why}): couldn't play {desc or 'selected sound'}: {e}; "
            "falling back to MessageBeep")
    try:
        _beep()
        log(f"sound ({why}): MessageBeep played (silent if the Windows sound "
            "scheme is set to No Sounds)")
    except Exception as e:
        log(f"sound ({why}): MessageBeep failed too ({e}); staying silent")
    return False
