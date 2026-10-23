"""
sound_generator.py -- Procedural sound effects generator for Video Grabber.

Stop hunting for notification sounds online: this tool synthesises short
.wav files from scratch, lets you preview them instantly, and saves the
ones you like into ./sounds/ for sounds.py to play on download completion.

Only the standard library is used for synthesis / file handling
(wave, struct, math, random, pathlib, os -- plus winsound for playback,
which is also standard library on Windows) together with PySide6 for the UI.

Run it with:
    python sound_generator.py

Settings > Sounds opens SoundOptionsDialog (open_sound_options below): three
sounds from three different style families out of a library of 100 style
profiles, a length choice (1 / 3 / 5 / 10 s), Play, a name box, Save to
Library and Save as Default. Both save buttons write <home>/sounds/<name>.wav;
Save as Default also makes it the completion sound. That engine needs numpy;
the older SoundGeneratorWindow below does not.
"""

import importlib
import math
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

# winsound is Windows-only (and required for SND_ASYNC playback).
# Guard the import so this file still *runs* on macOS/Linux for testing,
# where playback falls back to PySide6's QSoundEffect instead.
try:
    import winsound
except ImportError:  # Not on Windows
    winsound = None

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# QSoundEffect is the non-Windows playback fallback. It's part of PySide6,
# but keep the import defensive in case QtMultimedia isn't installed.
try:
    from PySide6.QtMultimedia import QSoundEffect
except ImportError:  # pragma: no cover
    QSoundEffect = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100          # Hz -- CD quality, matches sounds.py playback
CHANNELS = 1                 # Mono
SAMPLE_WIDTH = 2             # Bytes per sample -> 16-bit audio
ATTACK_SECONDS = 0.004       # 4 ms attack, prevents clicks at note starts
TARGET_PEAK = 0.9            # Normalise to 90% of full scale (headroom)

BASE_DIR = Path(__file__).resolve().parent
SOUNDS_DIR = BASE_DIR / "sounds"            # ./sounds/ next to this script
# Scratch file for previews lives in the OS temp folder, NOT in the app
# folder -- otherwise an unsaved preview is left behind as junk when the
# app closes. The OS cleans its temp folder up by itself.
# Note: TEMP is the Windows variable; on systems without it we fall back
# to BASE_DIR so the tool still works.
TEMP_PATH = Path(os.environ.get("TEMP", BASE_DIR)) / "temp_preview.wav"


# ---------------------------------------------------------------------------
# Synthesis helpers
# ---------------------------------------------------------------------------

def _wave_sample(waveform: str, phase: float) -> float:
    """One oscillator sample for `waveform` at `phase` radians (Batch 4)."""
    if waveform == "square":
        return 1.0 if math.sin(phase) > 0 else -1.0
    if waveform == "sawtooth":
        return 2.0 * ((phase / (2 * math.pi)) % 1.0) - 1.0
    if waveform == "triangle":
        return 2.0 * abs(2.0 * ((phase / (2 * math.pi)) % 1.0) - 1.0) - 1.0
    if waveform == "noise":
        return random.uniform(-1.0, 1.0)
    return math.sin(phase)  # "sine" (and any unknown name) stays a sine


def _add_tone(
    buffer: list[float],
    start_seconds: float,
    duration_seconds: float,
    frequency: float,
    amplitude: float = 1.0,
    decay: float = 6.0,
    overtones: tuple[tuple[float, float], ...] = ((1.0, 1.0),),
    sweep_to: float | None = None,
    waveform: str = "sine",
) -> None:
    """Mix one tone into ``buffer`` (a list of float samples).

    Args:
        buffer:    Float sample buffer, modified in place.
        start_seconds: When the tone starts.
        duration_seconds: How long it lasts.
        frequency: Starting frequency in Hz.
        amplitude: Peak amplitude of this tone (before normalisation).
        decay:     Exponential decay rate -- higher = fades faster.
                   Use ~0 for a beep that stays level until it ends.
        overtones: (frequency_multiplier, relative_amplitude) pairs.
                   (1.0, 1.0) is a pure sine; extra pairs add harmonics.
        sweep_to:  If given, glide the frequency to this value over the
                   tone's duration (used by the laser / thump sweeps).
        waveform:  Oscillator shape for this tone (Batch 4): "sine",
                   "square", "sawtooth", "triangle" or "noise".
    """
    start = int(start_seconds * SAMPLE_RATE)
    length = int(duration_seconds * SAMPLE_RATE)
    attack = max(1, int(ATTACK_SECONDS * SAMPLE_RATE))
    release = min(length // 2, int(0.008 * SAMPLE_RATE))  # 8 ms fade-out

    phase = 0.0
    for i in range(length):
        index = start + i
        if index >= len(buffer):
            break

        t = i / SAMPLE_RATE
        progress = i / max(1, length - 1)

        # Frequency for this sample (constant, or sweeping for a blip).
        if sweep_to is None:
            freq = frequency
        else:
            freq = frequency + (sweep_to - frequency) * progress
        phase += 2.0 * math.pi * freq / SAMPLE_RATE

        # Envelope: 4 ms linear attack -> exponential decay -> short release.
        if i < attack:
            envelope = i / attack
        else:
            envelope = math.exp(-decay * max(0.0, t - ATTACK_SECONDS))
        if release and i >= length - release:
            envelope *= (length - i) / release

        # Sum the fundamental and any overtones.
        sample = 0.0
        for multiplier, relative_amp in overtones:
            sample += relative_amp * _wave_sample(waveform, phase * multiplier)

        buffer[index] += amplitude * envelope * sample


PROFILES = ("Random", "Retro 8-Bit", "16-Bit Console", "Modern UI",
            "Sci-Fi / Laser", "Chime / Marimba", "Noise / Impact")
_CONCRETE_PROFILES = PROFILES[1:]


def _render_style(profile: str) -> tuple[list[float], int]:
    """Render one Batch 4 profile into a float sample buffer.

    Returns (buffer, num_layers) -- num_layers is the number of
    _add_tone layers mixed into the buffer, which the UI reports next
    to the profile name.
    """
    if profile == "Retro 8-Bit":
        # 3 square-wave notes stepping up: root, major third (+4
        # semitones), fifth (+7) -- the classic 8-bit arpeggio.
        buffer = [0.0] * int(0.55 * SAMPLE_RATE)
        base = random.choice([523.25, 587.33, 659.25])  # C5 / D5 / E5
        for step, semitones in enumerate((0, 4, 7)):
            freq = base * (2.0 ** (semitones / 12.0))
            _add_tone(buffer, start_seconds=step * 0.09, duration_seconds=0.22,
                      frequency=freq, amplitude=0.7, decay=8.0,
                      waveform="square")
        return buffer, 3

    if profile == "16-Bit Console":
        # Sawtooth lead, a triangle harmony a fifth up (+7 semitones),
        # and a soft triangle root an octave down -- a 16-bit chord stab.
        buffer = [0.0] * int(0.80 * SAMPLE_RATE)
        base = random.choice([392.00, 440.00, 523.25])  # G4 / A4 / C5
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.50,
                  frequency=base, amplitude=0.65, decay=5.0,
                  waveform="sawtooth")
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.50,
                  frequency=base * (2.0 ** (7 / 12.0)), amplitude=0.5,
                  decay=5.0, waveform="triangle")
        _add_tone(buffer, start_seconds=0.02, duration_seconds=0.60,
                  frequency=base / 2.0, amplitude=0.55, decay=4.0,
                  waveform="triangle")
        return buffer, 3

    if profile == "Modern UI":
        # 1-2 short triangle blips, the second (when present) a fourth
        # up (+5 semitones) -- clean, soft UI confirmation chirps.
        buffer = [0.0] * int(0.40 * SAMPLE_RATE)
        base = random.choice([880.00, 1046.50])  # A5 / C6
        layers = random.choice([1, 2])
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.12,
                  frequency=base, amplitude=0.8, decay=11.0,
                  waveform="triangle")
        if layers == 2:
            _add_tone(buffer, start_seconds=0.08, duration_seconds=0.16,
                      frequency=base * (2.0 ** (5 / 12.0)), amplitude=0.8,
                      decay=10.0, waveform="triangle")
        return buffer, layers

    if profile == "Sci-Fi / Laser":
        # Sawtooth pitch sweep diving down, with a short noise burst
        # layered over it for the zap's edge.
        buffer = [0.0] * int(0.60 * SAMPLE_RATE)
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.45,
                  frequency=random.uniform(1200.0, 1600.0), sweep_to=110.0,
                  amplitude=0.75, decay=3.5, waveform="sawtooth")
        _add_tone(buffer, start_seconds=0.05, duration_seconds=0.18,
                  frequency=2000.0, amplitude=0.35, decay=16.0,
                  waveform="noise")
        return buffer, 2

    if profile == "Chime / Marimba":
        # Sine base plus two higher sine overtone layers, each decaying
        # faster than the one below it -- a struck-bar chime.
        buffer = [0.0] * int(1.20 * SAMPLE_RATE)
        base = random.choice([659.25, 783.99, 880.00, 1046.50])
        _add_tone(buffer, start_seconds=0.0, duration_seconds=1.20,
                  frequency=base, amplitude=1.0, decay=4.0, waveform="sine")
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.80,
                  frequency=base * 2.0, amplitude=0.40, decay=6.5,
                  waveform="sine")
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.50,
                  frequency=base * 2.76, amplitude=0.18, decay=9.0,
                  waveform="sine")
        return buffer, 3

    if profile == "Noise / Impact":
        # Deep sine thump gliding down, plus a white-noise burst --
        # a soft impact / thud.
        buffer = [0.0] * int(0.50 * SAMPLE_RATE)
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.35,
                  frequency=110.0, sweep_to=48.0, amplitude=1.0, decay=5.0,
                  waveform="sine")
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.22,
                  frequency=1500.0, amplitude=0.45, decay=14.0,
                  waveform="noise")
        return buffer, 2

    raise ValueError(f"Unknown profile: {profile}")


def _normalise(buffer: list[float]) -> None:
    """Scale ``buffer`` in place so its peak sits at TARGET_PEAK."""
    peak = max((abs(s) for s in buffer), default=0.0)
    if peak > 0:
        scale = TARGET_PEAK / peak
        for i, sample in enumerate(buffer):
            buffer[i] = sample * scale


def _write_wav(path: Path, buffer: list[float]) -> None:
    """Write float samples (-1.0..1.0) as a 16-bit mono .wav file."""
    frames = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
        for s in buffer
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)


def generate_sound(
    output_path: str | Path, profile: str = "Random",
) -> tuple[Path, str, int]:
    """Synthesise a notification sound for `profile` and write a .wav.

    `profile` is one of PROFILES (see the generator window's combo);
    "Random" (or an unknown name) picks a concrete profile at random.
    A 4 ms attack is applied to every note and the result is normalised.

    Args:
        output_path: Where to write the .wav file.
        profile:     Which Batch 4 profile to render.

    Returns:
        (path, profile_name, num_layers) -- so the UI can tell you what
        it generated and how many layers were mixed into it.
    """
    output_path = Path(output_path)
    if output_path.parent != Path(""):
        output_path.parent.mkdir(parents=True, exist_ok=True)

    if profile not in _CONCRETE_PROFILES:
        profile = random.choice(_CONCRETE_PROFILES)
    buffer, num_layers = _render_style(profile)
    _normalise(buffer)
    _write_wav(output_path, buffer)
    return output_path, profile, num_layers


def read_wav_samples(path: str | Path) -> list[float]:
    """Read a 16-bit .wav back into float mono samples (for the waveform).

    Our own files are mono, but a file dropped into ./sounds/ by hand may
    be stereo -- in that case the channels are averaged per frame rather
    than interleaved (which would draw a garbled waveform). Files that
    aren't 16-bit return an empty list instead of mis-decoding.
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != SAMPLE_WIDTH:
            return []
        channels = max(1, wav.getnchannels())
        raw = wav.readframes(wav.getnframes())
    count = len(raw) // SAMPLE_WIDTH
    ints = struct.unpack(f"<{count}h", raw[: count * SAMPLE_WIDTH])
    if channels == 1:
        return [s / 32768.0 for s in ints]
    # De-interleave: average each frame's channels into one mono sample.
    return [
        sum(ints[i:i + channels]) / channels / 32768.0
        for i in range(0, count - channels + 1, channels)
    ]


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def stop_playback(fallback_player=None) -> None:
    """Stop any async preview that is currently playing.

    This matters before overwriting, moving or deleting a .wav: on
    Windows a file that winsound is playing can be locked, and a new
    PlaySound on top of an old one is exactly the confusion the
    Generate-button debounce is trying to avoid.
    """
    if winsound is not None:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except RuntimeError:
            pass
    elif fallback_player is not None:
        fallback_player.stop()


def play_wav_async(path: str | Path, fallback_player=None) -> None:
    """Play a .wav without blocking the UI.

    On Windows this uses winsound with SND_ASYNC, exactly as sounds.py
    expects. Elsewhere it falls back to a QSoundEffect if one is supplied.
    A corrupt / unreadable file must never crash the UI, so playback
    errors are swallowed here -- generation and saving still work.
    """
    path = Path(path)
    if winsound is not None:
        try:
            winsound.PlaySound(
                str(path),
                winsound.SND_FILENAME | winsound.SND_ASYNC,
            )
        except RuntimeError:
            pass
    elif fallback_player is not None:
        fallback_player.setSource(QUrl.fromLocalFile(str(path)))
        fallback_player.play()
    # If neither is available there's simply no preview -- generation
    # and saving still work, so don't crash the tool over audio output.


# ---------------------------------------------------------------------------
# Waveform widget
# ---------------------------------------------------------------------------

class WaveformWidget(QWidget):
    """Simple QWidget that draws the amplitude waveform with QPainter."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._samples: list[float] = []
        self.setMinimumHeight(130)
        self.setMinimumWidth(280)

    def set_samples(self, samples: list[float]) -> None:
        """Replace the displayed waveform and repaint."""
        self._samples = samples
        self.update()

    def load_wav(self, path: str | Path) -> None:
        """Load a .wav file and display its waveform."""
        try:
            self.set_samples(read_wav_samples(path))
        except (OSError, EOFError, wave.Error):
            # Truncated / non-wav file in the folder: show "no waveform"
            # rather than taking the whole window down with it.
            self.set_samples([])

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background + centre (zero) line.
        painter.fillRect(self.rect(), QColor(30, 30, 36))
        mid_y = self.height() / 2
        painter.setPen(QPen(QColor(90, 90, 100), 1))
        painter.drawLine(0, int(mid_y), self.width(), int(mid_y))

        if not self._samples:
            painter.setPen(QColor(150, 150, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Generate a sound to see its waveform")
            return

        # Min/max per horizontal pixel: accurate even for long files,
        # and cheap -- one vertical line per pixel of width. Buckets are
        # mapped proportionally across the whole file, so the tail of
        # the sound isn't cut off when len(samples) % width != 0.
        painter.setPen(QPen(QColor(80, 220, 160), 1))
        width = max(1, self.width())
        total = len(self._samples)
        half_height = (self.height() / 2) - 6

        for x in range(width):
            start = x * total // width
            end = max(start + 1, (x + 1) * total // width)
            chunk = self._samples[start:end]
            if not chunk:
                break
            y_top = mid_y - (max(chunk) * half_height)
            y_bottom = mid_y - (min(chunk) * half_height)
            # Ensure a silent chunk still shows as a 1px dot on the line.
            painter.drawLine(x, int(y_top), x, int(max(y_bottom, y_top + 1)))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SoundGeneratorWindow(QMainWindow):
    """Preview, name, save and manage procedurally generated sounds.

    This works two ways:
      * Standalone: run this file and it is the whole app (see main()).
      * Embedded in Video Grabber: pass the app's main window as
        ``parent`` and it opens as a child window of the app -- see
        open_sound_generator() below, which is what a menu item /
        toolbar button in Video Grabber should call.

    Args:
        parent:     Optional parent widget (Video Grabber's main window).
        sounds_dir: Folder to save to / list from. Defaults to ./sounds/
                    next to this file. Pass Video Grabber's real sounds
                    folder here if sounds.py keeps sounds somewhere else.
    """

    def __init__(self, parent=None, sounds_dir: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Video Grabber -- Sound Generator")
        self.resize(760, 360)

        self.sounds_dir = Path(sounds_dir) if sounds_dir else SOUNDS_DIR
        self.temp_path: Path | None = None

        # Fallback player (only actually used when winsound is missing).
        self._fallback_player = QSoundEffect(self) if QSoundEffect else None

        self.sounds_dir.mkdir(parents=True, exist_ok=True)

        # --- Left side: generation + preview + saving -------------------
        # Batch 4: profile picker above Generate.
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(PROFILES)
        self.profile_combo.setToolTip(
            "Sound profile for the next generated sound")

        self.generate_button = QPushButton("Generate New Sound")
        self.generate_button.setMinimumHeight(56)
        self.generate_button.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.generate_button.clicked.connect(self.on_generate)

        self.waveform = WaveformWidget()

        self.style_label = QLabel("No sound generated yet.")
        self.style_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Sound Name:"))
        self.name_input = QLineEdit(f"custom_{random.randint(1000, 9999)}")
        name_row.addWidget(self.name_input)

        self.save_button = QPushButton("Save to Sounds Folder")
        self.save_button.setEnabled(False)  # Nothing to save until generated
        self.save_button.clicked.connect(self.on_save)

        left_layout = QVBoxLayout()
        left_layout.addWidget(self.profile_combo)
        left_layout.addWidget(self.generate_button)
        left_layout.addWidget(self.waveform)
        # Batch 4: preview transport for the unsaved sound, below the
        # waveform. Disabled until a sound has been generated.
        preview_row = QHBoxLayout()
        self.preview_play_btn = QPushButton("▶ Play Preview")
        self.preview_play_btn.setEnabled(False)
        self.preview_play_btn.clicked.connect(self._preview_play)
        self.preview_stop_btn = QPushButton("⏹ Stop")
        self.preview_stop_btn.setEnabled(False)
        self.preview_stop_btn.clicked.connect(self._preview_stop)
        preview_row.addWidget(self.preview_play_btn)
        preview_row.addWidget(self.preview_stop_btn)
        left_layout.addLayout(preview_row)
        left_layout.addWidget(self.style_label)
        left_layout.addLayout(name_row)
        left_layout.addWidget(self.save_button)
        left_layout.addStretch()

        # --- Right side: existing sounds in ./sounds/ ---------------------
        self.sounds_list = QListWidget()
        self.sounds_list.itemDoubleClicked.connect(lambda _item: self.on_play())
        self.sounds_list.currentRowChanged.connect(self._on_selection_changed)

        # Kept as attributes so they can be disabled when nothing is
        # selected -- otherwise clicking them silently does nothing,
        # which looks like the app has hung.
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.on_play)
        self.play_button.setEnabled(False)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.on_delete)
        self.delete_button.setEnabled(False)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.play_button)
        list_buttons.addWidget(self.delete_button)

        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("Saved sounds (./sounds/)"))
        right_layout.addWidget(self.sounds_list)
        right_layout.addLayout(list_buttons)

        # --- Assemble ------------------------------------------------------
        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.addLayout(left_layout, stretch=3)
        main_layout.addLayout(right_layout, stretch=2)
        self.setCentralWidget(central)

        self.refresh_list()

    # -- Generation ---------------------------------------------------------

    def on_generate(self) -> None:
        """Generate a temp sound, play it immediately, show its waveform."""
        # Debounce: disable the button for 0.5s so a double-click can't
        # fire two generations / two async plays at the same instant --
        # winsound handles that badly (often playing neither sound).
        self.generate_button.setEnabled(False)
        QTimer.singleShot(500, self._re_enable_generate)

        # Stop the previous preview before overwriting the temp file it
        # may still be playing from (a playing file can be locked).
        stop_playback(self._fallback_player)

        try:
            self.temp_path, profile, num_layers = generate_sound(
                TEMP_PATH, self.profile_combo.currentText())
        except OSError as exc:
            QMessageBox.warning(self, "Generation failed",
                                f"Could not write the preview file:\n{exc}")
            return

        self.waveform.load_wav(self.temp_path)
        self.style_label.setText(
            f"Profile: {profile} (Layers: {num_layers}) -- press Save to keep it")
        self.save_button.setEnabled(True)
        self.preview_play_btn.setEnabled(True)
        self.preview_stop_btn.setEnabled(True)
        play_wav_async(self.temp_path, self._fallback_player)

    def _re_enable_generate(self) -> None:
        try:
            self.generate_button.setEnabled(True)
        except RuntimeError:
            pass  # window closed before the debounce fired

    def _preview_play(self) -> None:
        """Replay the current unsaved preview (Batch 4)."""
        if self.temp_path is not None:
            play_wav_async(self.temp_path, self._fallback_player)

    def _preview_stop(self) -> None:
        """Stop the preview playback (Batch 4)."""
        stop_playback(self._fallback_player)

    # -- Saving -------------------------------------------------------------

    # Names Windows reserves as devices -- a file literally called
    # "con.wav" or "nul.wav" can't be created normally on Windows.
    _RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))}

    def _clean_name(self, raw: str) -> str:
        """Turn user input into a safe file stem (no folders / extension).

        This is a security boundary, not just tidiness: input such as
        "../../hack" must resolve to a plain name *inside* ./sounds/,
        never to a path outside it. Path(...).name drops the folders,
        and stripping "." / "/" / "\\" removes anything left over.
        """
        name = raw.strip()
        if name.lower().endswith(".wav"):
            name = name[:-4]
        # Keep it to a single file name: strip any path parts, then
        # remove dots and separators explicitly (defence in depth --
        # Path().name behaves differently for "\\" on POSIX vs Windows).
        name = (
            Path(name).name.replace(" ", "_").replace(".", "")
            .replace("/", "").replace("\\", "")
        )
        # Characters Windows forbids in file names outright. Without
        # this, a name like "a<b" passes the checks above, then saving
        # fails with an unhandled OSError.
        for char in '<>:"|?*':
            name = name.replace(char, "_")
        # Windows also rejects trailing dots/spaces and very long names.
        name = name.strip(" ._")[:60]
        if name.upper() in self._RESERVED_NAMES:
            name = f"_{name}"
        return name

    def on_save(self) -> None:
        """Move the temp preview file to ./sounds/<name>.wav."""
        if self.temp_path is None or not self.temp_path.exists():
            QMessageBox.warning(self, "Nothing to save",
                                "Generate a sound first, then save it.")
            return

        name = self._clean_name(self.name_input.text())
        if not name:
            QMessageBox.warning(self, "Invalid name",
                                "Please enter a name for the sound.")
            return

        destination = self.sounds_dir / f"{name}.wav"
        # Final safeguard, independent of _clean_name: the file we are
        # about to write really must live directly inside the sounds folder.
        if destination.parent.resolve() != self.sounds_dir.resolve():
            QMessageBox.warning(self, "Invalid name",
                                "That name would save outside the sounds folder.")
            return

        if destination.exists():
            answer = QMessageBox.question(
                self, "Overwrite?",
                f"'{destination.name}' already exists. Overwrite it?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        # The preview may still be playing -- stop it before moving the
        # file out from under winsound.
        stop_playback(self._fallback_player)

        try:
            # Path.replace() = move (and it overwrites on all platforms),
            # BUT it only works within one filesystem. TEMP may be on
            # C: while the app lives on D:, which raises OSError
            # (WinError 17) -- so fall back to copy-then-delete.
            try:
                self.temp_path.replace(destination)
            except OSError:
                destination.write_bytes(self.temp_path.read_bytes())
                self.temp_path.unlink()
        except OSError as exc:
            # Leave self.temp_path alone on failure so the user can
            # fix the problem and click Save again instead of losing
            # a sound they liked.
            QMessageBox.warning(self, "Save failed",
                                f"Could not save '{destination.name}':\n{exc}")
            return

        self.temp_path = None
        self.save_button.setEnabled(False)
        self.preview_play_btn.setEnabled(False)
        self.preview_stop_btn.setEnabled(False)
        self.style_label.setText(f"Saved as {destination.name}")

        # Fresh default name for the next sound, then refresh + select it.
        self.name_input.setText(f"custom_{random.randint(1000, 9999)}")
        self.refresh_list(select_name=destination.name)

    # -- Saved-sounds list ----------------------------------------------------

    def refresh_list(self, select_name: str | None = None) -> None:
        """Reload the list widget from ./sounds/."""
        self.sounds_list.clear()
        for wav_path in sorted(self.sounds_dir.glob("*.wav")):
            self.sounds_list.addItem(wav_path.name)
            if wav_path.name == select_name:
                self.sounds_list.setCurrentRow(self.sounds_list.count() - 1)

    def _on_selection_changed(self, row: int) -> None:
        """Enable Play/Delete only when a sound is actually selected."""
        has_selection = row >= 0
        self.play_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def _selected_path(self) -> Path | None:
        item = self.sounds_list.currentItem()
        return self.sounds_dir / item.text() if item else None

    def on_play(self) -> None:
        """Play the selected saved sound (async) and preview its waveform."""
        path = self._selected_path()
        if path is None or not path.exists():
            return
        self.waveform.load_wav(path)
        self.style_label.setText(f"Playing: {path.name}")
        play_wav_async(path, self._fallback_player)

    def on_delete(self) -> None:
        """Delete the selected saved sound after confirmation."""
        path = self._selected_path()
        if path is None or not path.exists():
            return
        answer = QMessageBox.question(
            self, "Delete sound?", f"Delete '{path.name}' permanently?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            # Deleting the file winsound is currently playing can fail
            # on Windows, so stop playback first.
            stop_playback(self._fallback_player)
            try:
                path.unlink()
            except OSError as exc:
                QMessageBox.warning(self, "Delete failed",
                                    f"Could not delete '{path.name}':\n{exc}")
                return
            self.refresh_list()

    # -- Window lifecycle ---------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Stop playback and remove any unsaved preview on close.

        The temp file now lives in the OS temp folder, so even without
        this it wouldn't pollute the app folder -- but deleting it
        ourselves means no junk is left behind at all.
        """
        stop_playback(self._fallback_player)
        if self.temp_path is not None:
            try:
                self.temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Procedural engine: 100 style profiles, 3 options at a time (numpy)
# ---------------------------------------------------------------------------
# Every sound is built from scratch in code (sine / square / pulse / saw /
# triangle / FM / Karplus-Strong string / filtered noise), shaped by an
# envelope and an effects chain. There is no sample playback anywhere.
#
# A style is plain data (see S() and L() below). The renderer turns one style
# plus a length in seconds into a float32 mono buffer:
#   fit "tail": the notes play once; for a longer length the voices ring out
#               longer (slower decay) and the reverb tail grows with it.
#   fit "loop": the pattern repeats, fading, until the length is filled.
#   fit "hold": one evolving sustained voice that spans the whole length.
# A length shorter than the pattern compresses the pattern instead.
try:
    import numpy as np
except ImportError:  # the Sounds tab says so and keeps Generate disabled
    np = None

LENGTHS = ((1, "1 second"), (3, "3 seconds"), (5, "5 seconds"), (10, "10 seconds"))
DEFAULT_LENGTH = 3
LEVEL_PEAK = 0.85       # final peak
LEVEL_RMS = 0.12        # quiet styles (hiss, wind) are lifted toward this


def numpy_ok() -> bool:
    """True when numpy is importable. Re-checks, so an install made while the
    app is running (the dialog's Install numpy button) takes effect at once."""
    global np
    if np is None:
        try:
            importlib.invalidate_caches()
            np = importlib.import_module("numpy")
        except Exception:
            return False
    return True


def numpy_install_command() -> list:
    """pip for the exact Python that is running this app."""
    return [sys.executable, "-m", "pip", "install", "numpy>=1.26"]


def _hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def L(w="sin", **kw):
    """One synth layer. w: sin sq saw tri fm ks noise. Keys: o (octaves),
    st (semitones), len, d (decay /s), a (attack s), r (release s), amp,
    sw (end/start pitch ratio), vib (rate, depth), det (cents), pw (pulse
    width), harm ((mult, amp), ...), fm (ratio, index, index decay), band
    (lo, hi Hz) for noise, ev (only these event numbers), bed (one layer
    spanning the whole length), shape (hump riser fall flat), af / rf
    (attack / release as a fraction of the length, for hold and bed)."""
    d = dict(w=w, o=0, st=0, len=0.4, d=6.0, a=0.003, r=0.01, amp=1.0)
    d.update(kw)
    return d


def S(cat, name, layers, notes=(0,), step=0.0, root=72, fit="tail", fx=(), **kw):
    """One style profile. step 0 = chord (tiny strum); times = explicit event
    times; rand = {'rate', 'n0'} scatters events at random; stretch False
    keeps voices short and lets the reverb fill a longer length."""
    s = dict(cat=cat, name=name, layers=list(layers), notes=notes, step=step,
             root=root, fit=fit, fx=list(fx))
    s.update(kw)
    return s


_CH, _SF, _UI, _AC, _PC = "Chiptune / Retro", "Sci-Fi / Space", "Modern UI", "Acoustic / Bell", "Percussive"
_NA, _CI, _LF, _GL, _IN = "Nature", "Cinematic / Orchestral", "Lo-Fi / Ambient", "FX / Glitch", "Industrial / Mechanical"
CATEGORIES = (_CH, _SF, _UI, _AC, _PC, _NA, _CI, _LF, _GL, _IN)

_BELL = ((1, 1.0), (2.4, 0.45), (3.0, 0.25), (4.2, 0.12))
_GLASS = ((1, 1.0), (2.76, 0.4), (5.4, 0.15))
_PIANO = ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.2), (5, 0.12), (6, 0.08))
_METAL = ((1, 1.0), (2.76, 0.7), (5.4, 0.5), (8.93, 0.35), (13.3, 0.2))
_PENT = (0, 2, 4, 7, 9, 12)

STYLES = [
    # ---- Chiptune / Retro ------------------------------------------------
    S(_CH, "8-bit coin", [L("sq", len=.26, d=7)], (0, 5), .085, 83, "loop", gap=.45),
    S(_CH, "8-bit jump", [L("sq", len=.28, d=3.5, sw=2.2, pw=.25)], (0,), 0, 60, "loop", gap=.5),
    S(_CH, "8-bit level up", [L("sq", len=.16, d=8, pw=.25)], (0, 4, 7, 12, 16, 19, 24), .07, 72, "loop", gap=.5),
    S(_CH, "NES startup", [L("sq", len=.3, d=5, pw=.125), L("tri", o=-1, len=.4, d=4, amp=.8)],
      (0, 7, 12, 19), .12, 67, "tail", [("echo", .18, .4, .35)]),
    S(_CH, "Gameboy chime", [L("sq", len=.4, d=5, pw=.25)], (0, 7), .15, 76, "tail", [("lp", 5000), ("echo", .22, .35, .3)]),
    S(_CH, "Sega arpeggio", [L("saw", len=.09, d=14, amp=.8), L("saw", len=.09, d=14, det=9, amp=.6)],
      (0, 4, 7, 12, 7, 4, 0, 4, 7, 12), .05, 64, "loop", [("lp", 6000)], gap=.2),
    S(_CH, "Atari blip", [L("sq", len=.12, d=18, pw=.5)], (0,), 0, 57, "loop", [("crush", 6, 2)], gap=.35),
    S(_CH, "Commodore 64 sweep", [L("saw", len=.7, d=2, sw=.25, vib=(7, .02)), L("sq", len=.7, d=2, sw=.5, pw=.3, amp=.5)],
      (0,), 0, 69, "tail", [("crush", 8, 2), ("lp", 4500)]),
    S(_CH, "pixel victory", [L("sq", len=.18, d=6, pw=.25), L("tri", o=-1, len=.2, d=6, amp=.8)],
      (0, 0, 0, 5, 0, 5, 12), 0, 67, "tail", [("echo", .15, .3, .25)], times=(0, .1, .2, .3, .55, .65, .8)),
    S(_CH, "arcade insert coin", [L("sq", len=.18, d=8), L("noise", len=.03, d=90, band=(3000, 9000), amp=.5, ev=(0,))],
      (0, 7), .06, 81, "loop", [("crush", 7, 1)], gap=.6),
    # ---- Sci-Fi / Space ---------------------------------------------------
    S(_SF, "UFO landing", [L("sin", len=1.4, d=1.6, sw=.45, vib=(6, .06)), L("fm", len=1.4, d=1.6, sw=.45, fm=(2.01, 3, 2), amp=.5)],
      (0,), 0, 74, "tail", [("reverb", 1.2, .3)]),
    S(_SF, "laser blast", [L("saw", len=.35, d=4, sw=.08), L("noise", len=.12, d=25, band=(2000, 9000), amp=.4)],
      (0,), 0, 100, "loop", [("drive", 2.5), ("echo", .12, .3, .3)], gap=.55),
    S(_SF, "warp drive", [L("saw", len=1.2, d=0, sw=6, a=.7, amp=.6, shape="riser", bed=True),
                          L("noise", len=1.2, band=(300, 3000), amp=.5, shape="riser", bed=True)],
      (0,), 0, 45, "hold", [("lp", 6000), ("reverb", 1.5, .25)]),
    S(_SF, "teleport", [L("fm", len=.5, d=4, sw=3.5, fm=(1.5, 4, 5)), L("sin", len=.35, d=9, o=1, amp=.4)],
      (0, 7, 12, 19), .07, 72, "tail", [("trem", 14, .4), ("reverb", 1.0, .3)]),
    S(_SF, "alien radio", [L("fm", len=.14, d=7, fm=(3.7, 5, 3), vib=(11, .04)), L("noise", len=.2, d=10, band=(600, 3500), amp=.25)],
      (0, 5, -3, 9, 2, 7), .11, 66, "loop", [("gate", 9, .6), ("crush", 9, 2)], gap=.4),
    S(_SF, "spaceship alarm", [L("sq", len=.22, d=1.5, amp=.8)], (0, -5), .25, 69, "loop", [("lp", 3000), ("echo", .18, .35, .3)], gap=.1),
    S(_SF, "stargate hum", [L("saw", len=1, d=0, amp=.7, af=.2, rf=.3, bed=True), L("sin", o=-1, len=1, amp=.9, af=.2, rf=.3, bed=True, vib=(.6, .02))],
      (0, 7), 0, 40, "hold", [("lp", 900), ("trem", 5, .35), ("reverb", 1.8, .3)]),
    S(_SF, "cyberpunk neon", [L("saw", len=.9, d=2.5, a=.02), L("saw", len=.9, d=2.5, a=.02, det=11), L("saw", len=.9, d=2.5, a=.02, det=-11)],
      (0, 3, 7, 10), 0, 58, "tail", [("lp", 2200), ("echo", .3, .4, .35), ("reverb", 1.4, .3)]),
    S(_SF, "matrix glitch", [L("sq", len=.05, d=30, pw=.25)], (0, 12, 7, 19, 5, 24, 3, 14), .06, 72, "loop",
      [("crush", 6, 3), ("gate", 18, .5), ("echo", .1, .4, .3)], gap=.3),
    S(_SF, "hologram boot", [L("tri", len=.7, d=3.5, a=.01), L("fm", len=.7, d=4, fm=(2, 2, 3), amp=.35, o=1)],
      (0, 7, 12, 16, 19, 24), .09, 64, "tail", [("reverb", 1.8, .45), ("echo", .27, .35, .3)]),
    # ---- Modern UI --------------------------------------------------------
    S(_UI, "Apple glass", [L("sin", len=.8, d=6, harm=_GLASS)], (0,), 0, 88, "tail", [("reverb", .8, .25)]),
    S(_UI, "Windows fluent", [L("tri", len=.5, d=6, a=.006)], (0, 7), .12, 79, "tail", [("lp", 4000), ("reverb", .9, .3)]),
    S(_UI, "Material design", [L("sin", len=.25, d=12, a=.01), L("sin", o=1, len=.2, d=14, amp=.3)], (0, 12), .08, 79, "tail", [("reverb", .6, .2)]),
    S(_UI, "minimal click", [L("noise", len=.02, d=150, band=(2000, 7000)), L("sin", len=.03, d=100, amp=.6)], (0,), 0, 100, "tail",
      [("reverb", .4, .15)], stretch=False),
    S(_UI, "soft pop", [L("sin", len=.14, d=22, sw=.5, a=.004)], (0,), 0, 81, "tail", [("reverb", .6, .2)], stretch=False),
    S(_UI, "digital bubble", [L("sin", len=.14, d=16, sw=2.2, a=.004)], (0, 5, 9), .075, 76, "tail",
      [("lp", 5000), ("echo", .16, .3, .25), ("reverb", .6, .2)], stretch=False),
    S(_UI, "clean ping", [L("sin", len=.9, d=5, harm=((1, 1), (2, .15)))], (0,), 0, 91, "tail", [("echo", .25, .35, .25), ("reverb", .8, .2)]),
    S(_UI, "interface swoosh", [L("noise", len=.5, d=0, band=(600, 7000), shape="hump", bed=True)], (0,), 0, 72, "hold", [("lp", 6000)]),
    S(_UI, "notification bell", [L("sin", len=.9, d=4.5, harm=_BELL)], (0, -4), .2, 84, "tail", [("reverb", 1.1, .3)]),
    S(_UI, "subtle tap", [L("sin", len=.06, d=60), L("noise", len=.02, d=150, band=(1500, 6000), amp=.3)], (0,), 0, 55, "tail",
      [("reverb", .3, .15)], stretch=False),
    # ---- Acoustic / Bell ----------------------------------------------------
    S(_AC, "grand piano chord", [L("sin", len=2, d=2.5, a=.004), L("sin", len=1, d=7, harm=_PIANO, amp=.6),
                                  L("noise", len=.02, d=150, band=(1000, 4000), amp=.15)],
      (0, 4, 7, 12), 0, 60, "tail", [("reverb", 1.6, .3)]),
    S(_AC, "music box", [L("sin", len=.55, d=7, harm=((1, 1), (6.27, .2)))], (0, 4, 7, 12, 7, 4, 2, 7), .17, 84, "loop",
      [("reverb", .7, .2)], gap=.5),
    S(_AC, "orchestral harp", [L("ks", len=.9, d=0, damp=.997)], (0, 2, 4, 7, 9, 12, 14, 16, 19), .06, 55, "tail", [("reverb", 1.6, .3)]),
    S(_AC, "marimba mallet", [L("sin", len=.4, d=9, harm=((1, 1), (4, .4)))], (0, 7, 4), .1, 67, "tail", [("reverb", .8, .2)]),
    S(_AC, "crystal glass", [L("sin", len=2, d=2, harm=_GLASS, vib=(5, .003))], (0,), 0, 96, "tail", [("reverb", 2.2, .4)]),
    S(_AC, "wind chime", [L("sin", len=1, d=3, harm=_BELL, amp=.8)], _PENT, 0, 84, "tail", [("reverb", 1.5, .35)],
      rand=dict(rate=2.5, n0=5), stretch=False),
    S(_AC, "tubular bell", [L("sin", len=3, d=1.2, harm=((1, 1), (2.4, .7), (3.5, .5), (5.2, .3), (7.9, .15)))], (0,), 0, 60, "tail",
      [("reverb", 2.0, .35)]),
    S(_AC, "celesta", [L("tri", len=.6, d=6, a=.004, harm=((1, 1), (4, .3)))], (0, 4, 7, 12), .1, 79, "tail", [("reverb", 1.2, .3)]),
    S(_AC, "kalimba", [L("sin", len=.5, d=8, harm=((1, 1), (5.4, .25)))], (0, 4, 2, 7, 9, 4), .14, 72, "loop", [("reverb", .6, .2)], gap=.6),
    S(_AC, "plucked string", [L("ks", len=1.2, d=0, damp=.995)], (0, 7), .18, 57, "tail", [("reverb", 1.0, .25)]),
    # ---- Percussive --------------------------------------------------------
    S(_PC, "snare rimshot", [L("noise", len=.25, d=40, band=(1500, 6500)), L("sin", len=.2, d=50, amp=.7)], (0,), 0, 52, "tail",
      [("reverb", .6, .2)], stretch=False),
    S(_PC, "woodblock", [L("sin", len=.12, d=35, harm=((1, 1), (2.5, .4)))], (0, 0), .22, 79, "loop", [("reverb", .4, .15)], gap=.5),
    S(_PC, "synth tom", [L("sin", len=.45, d=7, sw=.35), L("noise", len=.02, d=100, band=(1500, 5000), amp=.3)], (0,), 0, 57, "tail",
      [("reverb", .7, .2)], stretch=False),
    S(_PC, "kick drum thud", [L("sin", len=.4, d=9, sw=.3), L("noise", len=.015, d=150, band=(1000, 4000), amp=.4)], (0,), 0, 50, "tail",
      [("drive", 2), ("reverb", .5, .15)], stretch=False),
    S(_PC, "digital clap", [L("noise", len=.14, d=28, band=(900, 4000))], (0, 0, 0, 0), 0, 60, "tail", [("reverb", .7, .25)],
      times=(0, .012, .026, .042), stretch=False),
    S(_PC, "rim click", [L("tri", len=.05, d=90), L("noise", len=.02, d=120, band=(3000, 9000), amp=.5)], (0,), 0, 93, "loop", [("reverb", .3, .15)], gap=.4),
    S(_PC, "tabla tap", [L("sin", len=.3, d=12, sw=.7, harm=((1, 1), (2, .3)))], (0, 7, 0), .15, 55, "tail", [("reverb", .6, .2)], stretch=False),
    S(_PC, "cymbal splash", [L("noise", len=1.6, d=3, band=(5000, 14000)), L("sq", len=1.2, d=4, harm=_METAL, amp=.25)], (0,), 0, 90, "tail",
      [("reverb", 1.0, .25)]),
    S(_PC, "tribal drum", [L("sin", len=.35, d=8, sw=.6), L("noise", len=.06, d=40, band=(200, 900), amp=.4)], (0, 0, -5, 0, -5), 0, 48, "loop",
      [("reverb", .7, .2)], times=(0, .16, .32, .48, .64), gap=.5),
    S(_PC, "hollow log", [L("sin", len=.3, d=10, sw=.85), L("noise", len=.1, d=25, band=(400, 900), amp=.5)], (0, -3), .2, 55, "tail",
      [("reverb", .9, .3)], stretch=False),
    # ---- Nature -------------------------------------------------------------
    S(_NA, "water drop", [L("sin", len=.14, d=24, sw=2.6, a=.004)], (0, -5), .5, 80, "tail", [("reverb", 1.6, .45)], stretch=False),
    S(_NA, "bird chirp", [L("sin", len=.09, d=12, sw=1.6, vib=(30, .04)), L("sin", len=.09, d=12, sw=1.6, amp=.3, o=1)],
      (0, 2, 5, 7), 0, 96, "tail", [("reverb", .6, .2)], rand=dict(rate=3, n0=3), stretch=False),
    S(_NA, "wind gust", [L("noise", len=1, band=(200, 1500), shape="hump", bed=True), L("noise", len=1, band=(1200, 4000), amp=.3, shape="hump", bed=True)],
      (0,), 0, 60, "hold", [("trem", 3, .2)]),
    S(_NA, "ocean wave", [L("noise", len=1, band=(60, 900), shape="hump", bed=True), L("noise", len=1, band=(1500, 6000), amp=.25, shape="riser", bed=True)],
      (0,), 0, 60, "hold", [("reverb", .8, .15)]),
    S(_NA, "crackling fire", [L("noise", len=.02, d=180, band=(800, 6000)), L("noise", len=1, band=(60, 350), amp=.35, shape="hump", bed=True)],
      (0,), 0, 60, "tail", [], rand=dict(rate=22, n0=15), stretch=False),
    S(_NA, "thunder rumble", [L("noise", len=1, band=(30, 220), shape="fall", bed=True, amp=1.2), L("sin", o=-3, len=1, shape="fall", bed=True, amp=.7),
                              L("noise", len=.1, d=35, band=(300, 4000), amp=.6)],
      (0,), 0, 50, "tail", [("drive", 1.8), ("reverb", 2.0, .35)], stretch=False),
    S(_NA, "cricket chirp", [L("sin", len=.045, d=5, a=.004)], (0, 0, 0), .06, 112, "loop", [("hp", 2500), ("reverb", .3, .1)], gap=.38),
    S(_NA, "bubbling brook", [L("sin", len=.08, d=30, sw=1.9, a=.003), L("noise", len=1, band=(800, 5000), amp=.12, shape="hump", bed=True)],
      (0, 3, 7, 12, -2), 0, 79, "tail", [("reverb", .5, .15)], rand=dict(rate=14, n0=10), stretch=False),
    S(_NA, "leaf crunch", [L("noise", len=.03, d=120, band=(2000, 9000))], (0,), 0, 60, "tail", [], rand=dict(rate=16, n0=10), stretch=False),
    S(_NA, "whale song", [L("sin", len=1, d=0, sw=2.0, vib=(.5, .04), af=.25, rf=.35, bed=True), L("sin", len=1, d=0, sw=.6, amp=.5, af=.3, rf=.35, bed=True, o=1)],
      (0,), 0, 52, "hold", [("reverb", 2.0, .4)]),
    # ---- Cinematic / Orchestral ----------------------------------------------
    S(_CI, "movie trailer hit", [L("sin", len=2.5, d=1.6, sw=.4, a=.004), L("noise", len=.8, d=6, band=(30, 900), amp=.8)], (0,), 0, 38, "tail",
      [("drive", 2), ("reverb", 2.2, .4)], stretch=False),
    S(_CI, "horror sting", [L("saw", len=1.4, d=2, a=.03, vib=(6, .01)), L("saw", len=1.4, d=2, a=.03, o=1, sw=.97, amp=.5)], (0, 6, 13), 0, 50, "tail",
      [("drive", 1.8), ("lp", 5000), ("reverb", 1.6, .3)]),
    S(_CI, "mystery reveal", [L("tri", len=.9, d=2.5, a=.01), L("sin", len=.9, d=2.5, o=1, amp=.4)], (0, 3, 7, 10, 15), .22, 64, "tail",
      [("reverb", 1.8, .4)]),
    S(_CI, "triumphant brass", [L("saw", len=1.4, d=1.2, a=.06), L("saw", len=1.4, d=1.2, a=.06, det=7)], (0, 4, 7, 12), 0, 55, "tail",
      [("lp", 2600), ("reverb", 1.5, .3)]),
    S(_CI, "suspense riser", [L("saw", len=1, d=0, sw=3, amp=.5, shape="riser", bed=True), L("noise", len=1, band=(400, 6000), amp=.6, shape="riser", bed=True)],
      (0,), 0, 45, "hold", [("lp", 5000), ("reverb", 1.0, .2)]),
    S(_CI, "dramatic swell", [L("saw", len=1, d=0, af=.55, rf=.35, bed=True), L("saw", len=1, d=0, af=.55, rf=.35, det=8, bed=True, amp=.7)],
      (0, 7, 12, 16), 0, 48, "hold", [("lp", 2400), ("reverb", 1.8, .35)]),
    S(_CI, "villain theme", [L("saw", len=.7, d=1.8, a=.03), L("saw", len=.7, d=1.8, a=.03, det=-9, amp=.7)], (0, 0, 3, 2, 0), 0, 43, "loop",
      [("lp", 1800), ("drive", 1.5), ("reverb", 1.4, .3)], times=(0, .4, .8, 1.2, 1.6), gap=.6),
    S(_CI, "hero entrance", [L("saw", len=.6, d=2, a=.03), L("saw", len=.6, d=2, a=.03, st=7, amp=.5)], (0, 7, 12, 16, 19), 0, 55, "tail",
      [("lp", 3000), ("reverb", 1.6, .35)], times=(0, .16, .32, .55, .8)),
    S(_CI, "epic choir", [L("saw", len=1, d=0, af=.3, rf=.4, vib=(5, .008), bed=True), L("saw", len=1, d=0, af=.3, rf=.4, det=10, bed=True, amp=.7)],
      (0, 7, 12, 16), 0, 50, "hold", [("formant", (800, 120, 1.0), (1150, 150, .7), (2900, 250, .3)), ("reverb", 2.2, .4)]),
    S(_CI, "soft strings", [L("saw", len=1, d=0, af=.3, rf=.4, vib=(5.5, .006), bed=True), L("saw", len=1, d=0, af=.3, rf=.4, det=-9, bed=True, amp=.8)],
      (0, 4, 7), 0, 52, "hold", [("lp", 2600), ("reverb", 1.8, .35)]),
    # ---- Lo-Fi / Ambient ------------------------------------------------------
    S(_LF, "tape hiss", [L("noise", len=1, band=(4000, 12000), amp=.4, shape="hump", bed=True), L("sin", len=.8, d=4, amp=.9, ev=(0,))], (0,), 0, 76, "tail",
      [("wobble", 1.2, .01), ("reverb", .8, .2)]),
    S(_LF, "vinyl crackle", [L("noise", len=.012, d=250, band=(1500, 8000)), L("noise", len=1, band=(100, 1500), amp=.08, shape="hump", bed=True)],
      (0,), 0, 60, "tail", [], rand=dict(rate=18, n0=12), stretch=False),
    S(_LF, "lo-fi piano", [L("fm", len=1.5, d=2.6, fm=(1, 1.6, 4)), L("sin", len=.1, d=40, o=1, amp=.2)], (0, 4, 7, 11), .03, 60, "tail",
      [("wobble", .8, .006), ("lp", 3200), ("crush", 12, 1), ("reverb", 1.2, .25)]),
    S(_LF, "warm pad", [L("tri", len=1, d=0, af=.35, rf=.4, vib=(4.5, .004), bed=True), L("saw", len=1, d=0, af=.35, rf=.4, det=6, bed=True, amp=.4)],
      (0, 7, 12, 16), 0, 55, "hold", [("lp", 1500), ("reverb", 1.8, .35)]),
    S(_LF, "analog drift", [L("saw", len=1, d=0, af=.3, rf=.4, vib=(.35, .012), bed=True), L("saw", len=1, d=0, af=.3, rf=.4, det=14, vib=(.27, .01), bed=True, amp=.8)],
      (0, 7), 0, 50, "hold", [("lp", 1300), ("wobble", .5, .01), ("reverb", 1.2, .25)]),
    S(_LF, "cassette stop", [L("saw", len=1.2, d=1.5, a=.01), L("saw", len=1.2, d=1.5, a=.01, det=7)], (0, 4, 7), 0, 55, "tail",
      [("lp", 3000), ("warp", 1.0, .03)]),
    S(_LF, "tape rewind", [L("tri", len=.4, d=5)], (0, 4, 7, 12, 9, 5), .1, 67, "tail",
      [("reverse",), ("warp", 3.0, 1.8), ("hp", 300), ("reverb", .4, .1)]),
    S(_LF, "chill wave", [L("saw", len=1.6, d=1.2, a=.02), L("saw", len=1.6, d=1.2, a=.02, det=10)], (0, 4, 7, 11, 14), 0, 53, "tail",
      [("lp", 1700), ("trem", 2, .55), ("echo", .33, .45, .4), ("reverb", 1.6, .3)]),
    S(_LF, "dream sequence", [L("sin", len=1.2, d=2, harm=((1, 1), (2, .3), (3, .1)))], (0, 7, 12, 16, 19, 24), .25, 64, "tail",
      [("echo", .38, .5, .45), ("reverb", 2.6, .55)]),
    S(_LF, "soft static", [L("noise", len=1, band=(200, 3500), amp=.5, shape="hump", bed=True), L("sin", len=1, o=-1, amp=.15, shape="hump", bed=True)],
      (0,), 0, 57, "hold", [("lp", 3000)]),
    # ---- FX / Glitch ---------------------------------------------------------
    S(_GL, "digital stutter", [L("tri", len=.4, d=3)], (0, 4, 7, 12), .1, 72, "loop", [("gate", 16, .45), ("crush", 8, 2)], gap=.2),
    S(_GL, "bitcrush", [L("saw", len=.8, d=3), L("saw", len=.8, d=3, det=9, amp=.7)], (0, 4, 7), 0, 60, "tail", [("crush", 4, 8), ("lp", 5000)]),
    S(_GL, "data stream", [L("sq", len=.03, d=40, pw=.5)], (0, 12, 7, 19, 4, 24, 9, 16), 0, 84, "tail", [("crush", 6, 2), ("hp", 800)],
      rand=dict(rate=30, n0=20), stretch=False),
    S(_GL, "static burst", [L("noise", len=.3, d=14, band=(1500, 12000))], (0,), 0, 60, "tail", [("gate", 22, .6), ("reverb", .5, .2)], stretch=False),
    S(_GL, "glitch hop", [L("sin", len=.25, d=12, sw=.3, ev=(0, 3)), L("noise", len=.05, d=50, band=(6000, 14000), amp=.5, ev=(1, 2, 4, 5)),
                          L("sq", len=.12, d=14, pw=.25, amp=.5, o=1, ev=(2, 5))],
      (0, 0, 7, 0, 0, 12), 0, 50, "loop", [("crush", 8, 2)], times=(0, .125, .25, .375, .5, .625), gap=.4),
    S(_GL, "broken radio", [L("noise", len=1, band=(400, 3200), amp=.5, shape="hump", bed=True), L("sin", len=.15, d=8, sw=1.2, vib=(20, .03), amp=.5)],
      (0, 5, 2, 9), 0, 79, "tail", [("wobble", 3, .04), ("gate", 7, .7), ("crush", 9, 2)], rand=dict(rate=3, n0=4), stretch=False),
    S(_GL, "circuit bend", [L("sq", len=.15, d=8, sw=2.5, pw=.3), L("sq", len=.15, d=8, sw=.4, pw=.15, amp=.5)], (0, 5, -2, 7), .13, 60, "loop",
      [("crush", 7, 3), ("trem", 18, .5)], gap=.3),
    S(_GL, "modem dial-up", [L("sin", len=.07, d=4, st=0), L("sin", len=.07, d=4, st=7.6, amp=.9),
                             L("noise", len=.5, d=2, band=(900, 3400), amp=.6, ev=(7,))],
      (0, 3, 1, 5, 2, 6, 4, 0), .08, 79, "tail", [("crush", 10, 1), ("lp", 4500)], stretch=False),
    S(_GL, "error buzz", [L("sq", len=.22, d=1.2, pw=.5), L("sq", len=.22, d=1.2, det=15, amp=.7)], (0, -1), .3, 45, "loop", [("lp", 1800), ("drive", 2)], gap=.4),
    S(_GL, "system crash", [L("saw", len=.9, d=2, sw=.06), L("noise", len=.5, d=5, band=(200, 8000), amp=.6)], (0,), 0, 72, "tail",
      [("crush", 5, 12), ("gate", 14, .55), ("drive", 2)], stretch=False),
    # ---- Industrial / Mechanical ----------------------------------------------
    S(_IN, "air horn", [L("saw", len=1.1, d=.9, a=.02), L("sq", len=1.1, d=.9, a=.02, st=4, amp=.6), L("saw", len=1.1, d=.9, a=.02, det=14, amp=.6)], (0, 5), 0, 58, "tail",
      [("drive", 2.2), ("lp", 3500), ("reverb", 1.0, .25)]),
    S(_IN, "steam release", [L("noise", len=1, band=(2500, 12000), shape="fall", bed=True), L("noise", len=1, band=(400, 2500), amp=.4, shape="fall", bed=True)],
      (0,), 0, 60, "hold", [("reverb", .6, .15)]),
    S(_IN, "metal clank", [L("sin", len=1.4, d=7, harm=_METAL), L("noise", len=.03, d=100, band=(1500, 8000), amp=.5)], (0,), 0, 70, "tail",
      [("reverb", .8, .25)], stretch=False),
    S(_IN, "factory siren", [L("sin", len=1, d=0, vib=(.45, .22), af=.15, rf=.3, bed=True), L("saw", len=1, d=0, vib=(.45, .22), af=.15, rf=.3, amp=.35, bed=True)],
      (0,), 0, 70, "hold", [("lp", 3200), ("reverb", 1.2, .25)]),
    S(_IN, "hydraulic press", [L("noise", len=1, band=(100, 900), shape="riser", bed=True), L("saw", o=-2, len=1, sw=.6, amp=.9, shape="riser", bed=True),
                               L("sin", len=.4, d=10, sw=.5, o=-2, amp=.5, ev=(0,))],
      (0,), 0, 52, "hold", [("trem", 11, .4), ("drive", 1.6)]),
    S(_IN, "chainsaw rev", [L("saw", len=1, d=0, sw=3, vib=(38, .05), shape="hump", bed=True), L("noise", len=1, band=(500, 4500), amp=.4, shape="hump", bed=True)],
      (0,), 0, 38, "hold", [("drive", 3), ("lp", 4500)]),
    S(_IN, "train whistle", [L("sin", len=1.2, d=.6, a=.06, vib=(6, .008), harm=((1, 1), (2, .2))), L("noise", len=1.2, d=.6, a=.06, band=(2500, 6000), amp=.12)],
      (0, 4), 0, 79, "tail", [("reverb", 1.5, .35)]),
    S(_IN, "engine start", [L("saw", len=1, d=0, sw=3, vib=(14, .05), bed=True, shape="riser", amp=.9), L("noise", len=1, band=(60, 500), amp=.6, bed=True, shape="hump")],
      (0,), 0, 33, "hold", [("trem", 16, .55), ("drive", 2), ("lp", 2500)]),
    S(_IN, "drill spin", [L("sq", len=1, d=0, sw=3, pw=.3, vib=(30, .04), bed=True, shape="hump"), L("noise", len=1, band=(2000, 9000), amp=.3, bed=True, shape="hump")],
      (0,), 0, 57, "hold", [("drive", 2.4), ("trem", 24, .35), ("lp", 5000)]),
    S(_IN, "robotic servo", [L("saw", len=.15, d=6, sw=1.8), L("sq", len=.15, d=6, sw=1.8, pw=.2, amp=.4)], (0, 5, 2, 7), .12, 60, "loop",
      [("lp", 3500), ("crush", 10, 1)], gap=.3),
]
assert len(STYLES) == 100, len(STYLES)
STYLE_BY_NAME = {s["name"]: s for s in STYLES}


# ---- renderer ---------------------------------------------------------------
def _band(x, lo, hi):
    """Smooth band-pass / low-pass / high-pass through the FFT (lo or hi may be 0)."""
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    m = np.ones_like(f)
    if lo and lo > 0:
        m = m / (1.0 + (lo / np.maximum(f, 1e-3)) ** 4)
    if hi and hi > 0:
        m = m / (1.0 + (f / hi) ** 4)
    return np.fft.irfft(X * m, n)


def _karplus(freq, n, damp, rng):
    """Plucked string: a noise burst circulating through a damped delay line,
    computed one delay-length block at a time."""
    period = max(2, int(SAMPLE_RATE / max(freq, 20.0)))
    y = np.zeros(n + 2 * period + 4)
    burst = rng.uniform(-1, 1, period)
    y[1:period + 1] = 0.5 * (burst + np.roll(burst, 1))
    for s in range(period + 1, n + period + 2, period):
        y[s:s + period] = damp * 0.5 * (y[s - period:s] + y[s - period - 1:s - 1])
    return y[1:n + 1]


def _osc(w, ph, o):
    if w == "sin":
        return np.sin(ph)
    frac = (ph / (2 * np.pi)) % 1.0
    if w == "sq":
        return np.where(frac < o.get("pw", 0.5), 1.0, -1.0)
    if w == "saw":
        return 2 * frac - 1
    if w == "tri":
        return 2 * np.abs(2 * frac - 1) - 1
    return np.sin(ph)


def _shape_curve(shape, n):
    u = np.linspace(0.0, 1.0, n, endpoint=False)
    if shape == "riser":
        return u ** 1.6
    if shape == "fall":
        return (1 - u) ** 2.0 * np.minimum(1.0, u * 200)
    if shape == "flat":
        return np.ones(n)
    return np.sin(np.pi * u) ** 1.4          # "hump"


def _envelope(o, n, d, a, r, s, bed_or_hold):
    t = np.arange(n) / SAMPLE_RATE
    if bed_or_hold and o.get("shape"):
        env = _shape_curve(o["shape"], n)
        if o.get("d"):
            env = env * np.exp(-o["d"] * t / s)
        return env
    env = np.exp(-d * t)
    na = min(n, max(8, int(a * SAMPLE_RATE)))
    env[:na] *= np.linspace(0.0, 1.0, na, endpoint=False)
    nr = min(n, max(8, int(r * SAMPLE_RATE)))
    env[n - nr:] *= np.linspace(1.0, 0.0, nr)
    return env


def _voice(o, n, freq, rng, s, scale_t, bed_or_hold):
    """One layer for one note: n samples, base pitch freq, time scale s."""
    t = np.arange(n) / SAMPLE_RATE
    dur = n / SAMPLE_RATE
    w = o["w"]
    if w == "noise":
        x = rng.uniform(-1, 1, n)
        lo, hi = o.get("band", (0, 0))
        if lo or hi:
            x = _band(x, lo, hi)
        x = x / (np.max(np.abs(x)) + 1e-9)
    elif w == "ks":
        x = _karplus(freq, n, o.get("damp", 0.996) ** (1.0 / s), rng)   # longer length = slower decay
        x = x / (np.max(np.abs(x)) + 1e-9)
    else:
        f = np.full(n, freq)
        if o.get("sw"):
            f = f * o["sw"] ** (t / max(dur, 1e-3))
        if o.get("vib"):
            f = f * (1 + o["vib"][1] * np.sin(2 * np.pi * o["vib"][0] * t))
        if o.get("det"):
            f = f * 2 ** (o["det"] / 1200.0)
        ph = 2 * np.pi * np.cumsum(f) / SAMPLE_RATE
        if w == "fm":
            ratio, idx, fd = o["fm"]
            x = np.sin(ph + idx * np.exp(-fd * t / s) * np.sin(ph * ratio))
        elif o.get("harm"):
            x = sum(a * _osc(w, ph * h, o) for h, a in o["harm"])
            x = x / sum(a for _h, a in o["harm"])
        else:
            x = _osc(w, ph, o)
    return o["amp"] * x * _envelope(o, n, o["d"] / s, o["a"] * scale_t, o["r"], s, bed_or_hold)


# ---- effects ------------------------------------------------------------------
def _norm1(x):
    return x / (np.max(np.abs(x)) + 1e-9)


def _fx_reverb(x, t, mix, length, rng):
    t = min(4.5, max(0.15, t * max(0.7, length / 3.0)))
    m = int(t * SAMPLE_RATE)
    ir = rng.standard_normal(m) * np.exp(-np.arange(m) / SAMPLE_RATE * (6.9 / t))
    ir = _band(ir, 0, 5500)
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    size = 1 << int(np.ceil(np.log2(len(x) + m)))
    wet = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:len(x)]
    return x * (1 - mix) + wet * mix * 1.6


def _fx_echo(x, delay, fb, mix):
    d = int(delay * SAMPLE_RATE)
    y = x.copy()
    for k in range(1, 6):
        if k * d >= len(x):
            break
        y[k * d:] += x[:-k * d] * (fb ** k) * mix * 1.6
    return y


def _fx_crush(x, bits, down):
    x = _norm1(x)
    if down > 1:
        x = np.repeat(x[::down], down)[:len(x)]
    levels = 2.0 ** (bits - 1)
    return np.round(x * levels) / levels


def _fx_warp(x, s0, s1):
    n = len(x)
    speed = np.linspace(s0, s1, n)
    pos = np.cumsum(speed)
    return np.interp(pos, np.arange(n), x, right=0.0)


def _apply_fx(x, fx, length, rng):
    n = len(x)
    t = np.arange(n) / SAMPLE_RATE
    for item in fx:
        name, args = item[0], item[1:]
        if name == "reverb":
            x = _fx_reverb(x, args[0], args[1], length, rng)
        elif name == "echo":
            x = _fx_echo(x, *args)
        elif name == "crush":
            x = _fx_crush(x, *args)
        elif name == "drive":
            x = np.tanh(_norm1(x) * args[0] * 1.5)
        elif name == "lp":
            x = _band(x, 0, args[0])
        elif name == "hp":
            x = _band(x, args[0], 0)
        elif name == "trem":
            x = x * (1 - args[1] * (0.5 + 0.5 * np.sin(2 * np.pi * args[0] * t)))
        elif name == "gate":
            g = ((t * args[0]) % 1.0 < args[1]).astype(np.float64)
            x = x * np.convolve(g, np.ones(48) / 48, "same")
        elif name == "warp":
            x = _fx_warp(x, args[0], args[1])
        elif name == "wobble":
            speed = 1 + args[1] * np.sin(2 * np.pi * args[0] * t)
            x = np.interp(np.cumsum(speed), np.arange(n), x, right=0.0)
        elif name == "reverse":
            x = x[::-1].copy()
        elif name == "formant":
            f = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
            m = np.full_like(f, 0.04)
            for fc, bw, g in args:
                m += g * np.exp(-0.5 * ((f - fc) / bw) ** 2)
            x = np.fft.irfft(np.fft.rfft(x) * m, n)
    return x


# ---- the render --------------------------------------------------------------------
def render_style(style, length, seed=None):
    """Render one style profile to a float32 mono buffer of `length` seconds."""
    if np is None:
        raise RuntimeError("numpy is not installed")
    rng = np.random.default_rng(seed)
    n_tot = int(length * SAMPLE_RATE)
    out = np.zeros(n_tot)
    fit = style["fit"]
    root = style["root"]
    if isinstance(root, (list, tuple)):
        root = root[int(rng.integers(len(root)))]
    notes = list(style["notes"])
    layers = style["layers"]
    step = style["step"]
    rand = style.get("rand")
    strum = step if step else 0.012
    if "times" in style:
        starts = list(style["times"])
    else:
        starts = [i * strum for i in range(len(notes))]
    base_len = max([o["len"] for o in layers if not o.get("bed")] or [0.4])
    cycle = (max(starts) if starts else 0.0) + base_len

    # events: (start seconds, semitone, index, gain, time scale, length scale)
    events = []
    ts, kx = 1.0, 1.0
    if fit == "hold":
        events = [(i * 0.012, nt, i, 1.0) for i, nt in enumerate(notes)]
    elif rand:
        k = max(rand.get("n0", 4), int(rand["rate"] * length * 0.8))
        times = np.sort(rng.uniform(0, length * 0.85, k))
        for i, tm in enumerate(times):
            g = float(rng.uniform(0.4, 1.0)) * (1 - 0.7 * tm / length)
            events.append((float(tm), notes[int(rng.integers(len(notes)))], i, g))
    else:
        ts = min(1.0, length / cycle)
        if fit == "tail" and style.get("stretch", True):
            kx = min(14.0, max(1.0, length / cycle))
        # Short "hit" styles (stretch False) repeat with fading echoes once
        # the length is much longer than one hit, so a 10 s sound isn't mostly silence.
        loopish = fit == "loop" or (fit == "tail" and not style.get("stretch", True)
                                    and length > 2 * cycle)
        if loopish:
            period = cycle * ts + style.get("gap", 0.5) * ts
            t0 = 0.0
            while t0 < length - 0.05:
                g = max(0.05, 1 - 0.85 * t0 / length)
                for i, nt in enumerate(notes):
                    events.append((t0 + starts[i] * ts, nt, i, g))
                t0 += period
        else:
            events = [(starts[i] * ts, nt, i, 1.0) for i, nt in enumerate(notes)]
    s = ts * kx

    bed_done = False
    for (st, nt, idx, g) in events:
        i0 = int(st * SAMPLE_RATE)
        if i0 >= n_tot:
            continue
        for o in layers:
            if "ev" in o and idx not in o["ev"]:
                continue
            freq = _hz(root + nt + o["st"] + 12 * o["o"])
            if o.get("bed"):
                if fit != "hold":
                    if bed_done:
                        continue
                    freq = _hz(root + notes[0] + o["st"] + 12 * o["o"])
                n = n_tot - i0
                o2 = dict(o)
                if not o.get("shape"):
                    o2["shape"] = "hump"
                v = _voice(o2, n, freq, rng, length, 1.0, True)
                out[i0:i0 + n] += g * v
                continue
            n = min(n_tot - i0, int(o["len"] * s * SAMPLE_RATE))
            if n < 16:
                continue
            v = _voice(o, n, freq, rng, s, ts, False)
            out[i0:i0 + n] += g * v
        if fit != "hold":
            bed_done = True

    out = _apply_fx(out, style["fx"], length, rng)
    out = out[:n_tot] if len(out) >= n_tot else np.pad(out, (0, n_tot - len(out)))
    out = np.nan_to_num(out)
    out -= out.mean()
    peak = float(np.max(np.abs(out)))
    if peak < 1e-9:
        return out.astype(np.float32)
    out *= LEVEL_PEAK / peak
    rms = float(np.sqrt(np.mean(out ** 2)))
    if rms < LEVEL_RMS:                     # lift quiet styles, soft-limited
        g = min(3.0, LEVEL_RMS / max(rms, 1e-6))
        out = np.tanh(out * g) / np.tanh(g)
        out *= LEVEL_PEAK / (float(np.max(np.abs(out))) + 1e-9)
    nf = min(int(0.06 * SAMPLE_RATE), n_tot // 4)
    na = min(int(0.002 * SAMPLE_RATE), n_tot // 8)
    if nf:
        out[-nf:] *= np.linspace(1.0, 0.0, nf)
    if na:
        out[:na] *= np.linspace(0.0, 1.0, na)
    return out.astype(np.float32)


def pick_three(rng=None):
    """Three styles from three different categories, as (style, seed) pairs."""
    rng = rng or random.Random()
    cats = rng.sample(CATEGORIES, 3)
    picks = []
    for c in cats:
        style = rng.choice([s for s in STYLES if s["cat"] == c])
        picks.append((style, rng.randrange(1 << 30)))
    return picks


def write_wav_array(path, samples):
    """Write a float32 mono buffer as a 16-bit 44.1 kHz .wav."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return path


# ---------------------------------------------------------------------------
# "Generate Sounds" dialog: three very different options, Play, Save as Default
# ---------------------------------------------------------------------------
try:
    from logging_setup import log as _vg_log
except Exception:                       # standalone use
    def _vg_log(msg):
        print(msg)


class _MiniWave(QWidget):
    """Small waveform thumbnail (min/max per column) for a numpy buffer."""

    BINS = 240

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bars: list[tuple[float, float]] = []
        self.setMinimumHeight(64)

    def set_samples(self, samples) -> None:
        self._bars = []
        if samples is not None and len(samples) >= self.BINS:
            k = len(samples) // self.BINS
            grid = samples[: k * self.BINS].reshape(self.BINS, k)
            self._bars = list(zip(grid.min(axis=1).tolist(), grid.max(axis=1).tolist()))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 36))
        mid = self.height() / 2
        p.setPen(QPen(QColor(90, 90, 100), 1))
        p.drawLine(0, int(mid), self.width(), int(mid))
        if not self._bars:
            return
        p.setPen(QPen(QColor(80, 220, 160), 1))
        half = mid - 4
        w = max(1, self.width())
        for x in range(w):
            lo, hi = self._bars[min(len(self._bars) - 1, x * len(self._bars) // w)]
            p.drawLine(x, int(mid - hi * half), x, int(max(mid - lo * half, mid - hi * half + 1)))


class _InstallThread(QThread):
    """Runs pip for numpy off the UI thread."""
    finished_ok = Signal(bool, str)

    def run(self) -> None:
        try:
            r = subprocess.run(numpy_install_command(), capture_output=True, text=True,
                               timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
            self.finished_ok.emit(r.returncode == 0, tail[0])
        except Exception as exc:
            self.finished_ok.emit(False, f"{type(exc).__name__}: {exc}")


class _RenderThread(QThread):
    """Renders the three options off the UI thread (a 10 s sound can take a second)."""
    done = Signal(list)       # [(style name, float32 array), ...]
    failed = Signal(str)

    def __init__(self, picks, length, parent=None) -> None:
        super().__init__(parent)
        self._picks, self._length = picks, length

    def run(self) -> None:
        try:
            self.done.emit([(st["name"], render_style(st, self._length, seed))
                            for st, seed in self._picks])
        except Exception as exc:          # never let a synthesis bug kill the app
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "sound"


_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}


def _safe_stem(text: str) -> str:
    """File-name stem for a sound name: letters, digits, space, - and _ only,
    spaces become underscores, no Windows reserved names. Never empty."""
    stem = re.sub(r"[^\w\- ]+", " ", text or "")
    stem = re.sub(r"\s+", "_", stem.strip()).strip("._-")[:60]
    if not stem:
        stem = "sound"
    if stem.lower() in _RESERVED_NAMES:
        stem += "_sound"
    return stem


class SoundOptionsDialog(QDialog):
    """Generate 3 procedural completion sounds from 3 different style
    categories, audition them, and save one as the default.

    Each option has a name box. Save to Library writes <sounds>/<name>.wav;
    Save as Default writes the same file and also makes it the completion sound.

    on_saved(path, name) is called after Save as Default so the host
    (Settings > Sounds) can point the completion sound at the file.
    on_library_changed() is called after any save, so the host can refresh its
    sound list without a restart."""

    def __init__(self, parent=None, on_saved=None, sounds_dir: str | Path | None = None,
                 on_library_changed=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate Sounds")
        self.setMinimumWidth(560)
        self._on_saved = on_saved
        self._on_library_changed = on_library_changed
        self._sounds_dir = Path(sounds_dir) if sounds_dir else None
        self._picks: list = []
        self._bufs: list = [None] * 3
        self._paths: list = [None] * 3
        self._names: list[str] = [""] * 3
        self._thread: _RenderThread | None = None
        self._rerun = False
        self._tmp = Path(tempfile.gettempdir()) / "vg_sound_options"

        root = QVBoxLayout(self)
        hint = QLabel("Three sounds from three different style families, built from "
                      "scratch. Play them, name the ones you like and save them to "
                      "your library, or save one as your completion sound.")
        hint.setObjectName("CardHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        top = QHBoxLayout()
        top.addWidget(QLabel("Length"))
        self.length_combo = QComboBox()
        for secs, label in LENGTHS:
            self.length_combo.addItem(label, userData=secs)
        self.length_combo.setCurrentIndex(
            max(0, self.length_combo.findData(DEFAULT_LENGTH)))
        self.length_combo.currentIndexChanged.connect(self._on_length_changed)
        top.addWidget(self.length_combo)
        top.addStretch(1)
        self.gen_btn = QPushButton("Generate 3 new options")
        self.gen_btn.clicked.connect(self.generate)
        top.addWidget(self.gen_btn)
        root.addLayout(top)

        self._cards = []
        for i in range(3):
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            fl = QVBoxLayout(frame)
            title = QLabel("")
            f = title.font()
            f.setBold(True)
            title.setFont(f)
            cat = QLabel("")
            cat.setObjectName("CardHint")
            wave_w = _MiniWave()
            row = QHBoxLayout()
            play = QPushButton("\u25b6 Play")
            play.clicked.connect(lambda _=False, k=i: self._play(k))
            stop = QPushButton("\u23f9 Stop")
            stop.clicked.connect(lambda _=False: stop_playback())
            row.addWidget(play)
            row.addWidget(stop)
            row.addStretch(1)
            save_row = QHBoxLayout()
            name_edit = QLineEdit()
            name_edit.setPlaceholderText("Sound name")
            name_edit.setMaxLength(60)
            name_edit.setClearButtonEnabled(True)
            name_edit.returnPressed.connect(lambda k=i: self._save_to_library(k))
            lib = QPushButton("Save to Library")
            lib.setToolTip("Keep this sound in your sounds folder under the name above.")
            lib.clicked.connect(lambda _=False, k=i: self._save_to_library(k))
            save = QPushButton("Save as Default")
            save.setToolTip("Save to the library and use it as the completion sound.")
            save.clicked.connect(lambda _=False, k=i: self._save(k))
            for b in (play, stop, lib, save, name_edit):
                b.setEnabled(False)
            save_row.addWidget(name_edit, 1)
            save_row.addWidget(lib)
            save_row.addWidget(save)
            fl.addWidget(title)
            fl.addWidget(cat)
            fl.addWidget(wave_w)
            fl.addLayout(row)
            fl.addLayout(save_row)
            root.addWidget(frame)
            self._cards.append(dict(title=title, cat=cat, wave=wave_w, name=name_edit,
                                    play=play, stop=stop, lib=lib, save=save))

        self.status = QLabel("")
        self.status.setObjectName("CardHint")
        self.status.setWordWrap(True)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        self.install_btn = QPushButton("Install numpy")
        self.install_btn.clicked.connect(self._install_numpy)
        self.install_btn.setVisible(False)
        bottom.insertWidget(1, self.install_btn)
        self._installer = None
        # Enter in a name box saves; it must not also press Generate or Close.
        for btn in self.findChildren(QPushButton):
            btn.setAutoDefault(False)
        if not numpy_ok():
            self._show_numpy_missing()
        else:
            QTimer.singleShot(0, self.generate)   # show 3 options straight away

    def _show_numpy_missing(self) -> None:
        self.gen_btn.setEnabled(False)
        self.length_combo.setEnabled(False)
        frozen = bool(getattr(sys, "frozen", False))
        cmd = " ".join(f'"{c}"' if " " in c else c for c in numpy_install_command())
        if frozen:
            self.status.setText("The sound engine needs numpy, which is not bundled in this "
                                "build. Rebuild with numpy installed (it is in requirements.txt).")
        else:
            self.install_btn.setVisible(True)
            self.status.setText("The sound engine needs numpy, which this Python does not have "
                                f"({sys.executable}). Press Install numpy, or run: {cmd}")
        _vg_log(f"sound generator: numpy is missing for {sys.executable}; generation is disabled")

    def _install_numpy(self) -> None:
        self.install_btn.setEnabled(False)
        self.status.setText("Installing numpy into this Python\u2026 this can take a minute.")
        t = _InstallThread(self)
        t.finished_ok.connect(self._on_numpy_installed)
        self._installer = t
        t.start()

    def _on_numpy_installed(self, ok: bool, tail: str) -> None:
        self._installer = None
        _vg_log(f"sound generator: numpy install {'ok' if ok else 'failed'}: {tail}")
        if ok and numpy_ok():
            self.install_btn.setVisible(False)
            self.length_combo.setEnabled(True)
            self.generate()
        else:
            self.install_btn.setEnabled(True)
            self.status.setText(f"Couldn't install numpy ({tail}). Run this in a terminal "
                                f"instead: {' '.join(numpy_install_command())}")

    # ---- generating ----------------------------------------------------------
    def _length(self) -> int:
        return int(self.length_combo.currentData() or DEFAULT_LENGTH)

    def generate(self) -> None:
        """Roll three new styles (three different categories) and render them."""
        if not numpy_ok():
            return
        self._picks = pick_three()
        self._start_render()

    def _on_length_changed(self) -> None:
        if self._picks:                      # same three styles, new length
            self._start_render()

    def _start_render(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._rerun = True               # finish the current render, then redo
            return
        stop_playback()
        self._set_busy(True)
        self.status.setText(f"Generating {self._length()} s sounds\u2026")
        t = _RenderThread(self._picks, self._length(), self)
        t.done.connect(self._on_done)
        t.failed.connect(self._on_failed)
        t.finished.connect(self._on_thread_finished)
        self._thread = t
        t.start()

    def _on_thread_finished(self) -> None:
        self._thread = None
        if self._rerun:
            self._rerun = False
            self._start_render()

    def _set_busy(self, busy: bool) -> None:
        self.gen_btn.setEnabled(not busy and numpy_ok())
        for c in self._cards:
            for k in ("play", "stop", "lib", "save", "name"):
                c[k].setEnabled(False if busy else c[k].isEnabled())

    def _on_failed(self, msg: str) -> None:
        _vg_log(f"sound generator: render failed: {msg}")
        self.status.setText(f"Couldn't generate sounds: {msg}")
        self.gen_btn.setEnabled(numpy_ok())

    def _on_done(self, results) -> None:
        if self._rerun:                      # a newer request is waiting; skip the stale one
            return
        try:
            self._tmp.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._on_failed(f"temp folder: {exc}")
            return
        for i, (name, buf) in enumerate(results):
            c = self._cards[i]
            style = self._picks[i][0]
            self._bufs[i], self._names[i] = buf, name
            c["title"].setText(name)
            c["name"].setText(name)               # default library name; edit freely
            c["cat"].setText(style["cat"])
            c["wave"].set_samples(buf)
            try:
                self._paths[i] = write_wav_array(self._tmp / f"opt{i}_{os.getpid()}.wav", buf)
            except OSError as exc:
                self._paths[i] = None
                _vg_log(f"sound generator: couldn't write preview {i}: {exc}")
            ok = self._paths[i] is not None
            for k in ("play", "stop", "lib", "save", "name"):
                c[k].setEnabled(ok)
        self.gen_btn.setEnabled(True)
        self.status.setText(f"{self._length()} s sounds ready. Press Play to hear each one.")
        _vg_log("sound generator: options " + ", ".join(n for n, _ in results)
                + f" at {self._length()} s")

    # ---- playing and saving ----------------------------------------------------
    def _play(self, i: int) -> None:
        path = self._paths[i]
        if path is None:
            return
        stop_playback()
        try:
            import sounds as _sounds
            ok = _sounds.play_completion_sound(_sounds.CUSTOM, str(path),
                                               why="generator preview")
            if not ok:
                self.status.setText("That couldn't be played, so the Windows beep was "
                                    "used instead. The log says why.")
        except Exception as exc:
            _vg_log(f"sound generator: preview failed ({exc}); trying winsound directly")
            try:
                play_wav_async(path)
            except Exception as exc2:
                _vg_log(f"sound generator: preview failed again ({exc2}); staying silent")
                self.status.setText("Audio output failed. The log says why.")

    def _target_dir(self) -> Path:
        if self._sounds_dir is not None:
            return self._sounds_dir
        try:
            import sounds as _sounds
            return _sounds._home() / "sounds"      # the app's data folder
        except Exception:
            return SOUNDS_DIR

    def _write_to_library(self, i: int):
        """Copy option i's temp wav to <sounds>/<name>.wav. Returns (dest, name)
        or None when nothing was written (no sound yet, a declined overwrite or
        a write error). Asks before replacing an existing sound."""
        src = self._paths[i]
        if src is None or not Path(src).exists():
            return None
        name = self._cards[i]["name"].text().strip() or self._names[i]
        dest = self._target_dir() / f"{_safe_stem(name)}.wav"
        if dest.exists() and QMessageBox.question(
                self, "Replace sound",
                f"{dest.name} is already in your library. Replace it?") != QMessageBox.Yes:
            return None
        stop_playback()                         # a playing file can be locked on Windows
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(Path(src).read_bytes())
        except OSError as exc:
            _vg_log(f"sound generator: save failed: {exc}")
            QMessageBox.warning(self, "Save failed", f"Could not save the sound:\n{exc}")
            return None
        self._cards[i]["name"].setText(name)
        if self._on_library_changed is not None:
            try:
                self._on_library_changed()
            except Exception as exc:
                _vg_log(f"sound generator: on_library_changed callback failed: {exc}")
        return dest, name

    def _save_to_library(self, i: int) -> None:
        res = self._write_to_library(i)
        if res is None:
            return
        dest, name = res
        _vg_log(f"sound generator: saved {self._names[i]} to the library as {dest}")
        self.status.setText(f"Saved \u201c{name}\u201d to your library ({dest.name}).")

    def _save(self, i: int) -> None:
        """Save as Default: write to the library, then make it the completion sound."""
        res = self._write_to_library(i)
        if res is None:
            return
        dest, name = res
        _vg_log(f"sound generator: saved {self._names[i]} as the completion sound -> {dest}")
        if self._on_saved is not None:
            try:
                self._on_saved(str(dest), name)
            except Exception as exc:
                _vg_log(f"sound generator: on_saved callback failed: {exc}")
        self.status.setText(f"Saved \u201c{name}\u201d to your library and set it as your "
                            f"completion sound ({dest.name}).")

    # ---- lifecycle ------------------------------------------------------------------
    def done(self, r: int) -> None:
        stop_playback()
        if self._installer is not None:
            self._installer.wait(15000)
        if self._thread is not None:
            self._rerun = False
            self._thread.wait(15000)
        for p in self._paths:
            if p is not None:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        super().done(r)


def open_sound_options(parent=None, on_saved=None, sounds_dir: str | Path | None = None,
                       on_library_changed=None) -> int:
    """Open the Generate Sounds dialog (modal). Called from Settings > Sounds."""
    dlg = SoundOptionsDialog(parent, on_saved=on_saved, sounds_dir=sounds_dir,
                             on_library_changed=on_library_changed)
    return dlg.exec()


# ---------------------------------------------------------------------------
# Integration: open this from inside Video Grabber
# ---------------------------------------------------------------------------

def open_sound_generator(
    parent=None, sounds_dir: str | Path | None = None
) -> SoundGeneratorWindow:
    """Open the sound generator from inside Video Grabber. Call this
    from a menu item or button in the app's main window, e.g.:

        from sound_generator import open_sound_generator

        # Tools menu (Video Grabber already has one in its menu bar):
        action = QAction("Generate Sounds...", self)
        action.triggered.connect(lambda: open_sound_generator(self))
        tools_menu.addAction(action)

    Behaviour notes:
      * Non-modal -- the user can keep using Video Grabber while it is
        open, and closing it returns them to the app, nothing restarts.
      * Calling it again while it is already open just brings the
        existing window to the front instead of opening a second copy
        (which would fight over the same temp preview file).
      * A reference is stored on the parent (``parent._sound_generator``).
        Keep it: without a reference Qt can garbage-collect the window
        and it will vanish as soon as the click handler returns.
    """
    existing = getattr(parent, "_sound_generator", None) if parent else None
    if existing is not None:
        try:
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return existing
        except RuntimeError:
            # The underlying C++ object was already deleted (the window
            # was closed with WA_DeleteOnClose somewhere) -- fall through
            # and create a fresh one.
            pass

    window = SoundGeneratorWindow(parent, sounds_dir=sounds_dir)
    if parent is not None:
        parent._sound_generator = window
    window.show()
    window.raise_()
    window.activateWindow()
    return window


def install_sound_generator(
    parent, sounds_dir: str | Path | None = None, toolbar=None
) -> QAction:
    """LEGACY (no longer called by the app): the entry point moved to
    Settings > Sounds (open_sound_options). Kept so old callers still work.

    One-line integration for Video Grabber's main window.

    Add this at the END of the main window's __init__ (after its menu
    bar exists):

        from sound_generator import install_sound_generator
        install_sound_generator(self)

    It finds the existing "Tools" menu by title -- no need to know the
    variable name it was stored in -- creates one if there isn't one,
    and adds a "Generate Sounds..." item to it. Optionally also pass a
    QToolBar to get the same action as a toolbar button:

        install_sound_generator(self, toolbar=self.toolbar)

    Pass ``sounds_dir`` if sounds.py keeps its sounds anywhere other
    than ./sounds/ next to this file.
    """
    action = QAction("Generate Sounds...", parent)
    action.triggered.connect(
        lambda _checked=False: open_sound_generator(parent, sounds_dir=sounds_dir)
    )

    tools_menu = None
    menu_bar = parent.menuBar()
    for menu_action in menu_bar.actions():
        menu = menu_action.menu()
        if menu and menu.title().replace("&", "").strip().lower() == "tools":
            tools_menu = menu
            break
    if tools_menu is None:
        tools_menu = menu_bar.addMenu("Tools")
    tools_menu.addAction(action)

    if toolbar is not None:
        toolbar.addAction(action)

    # Keep a reference, same reason as in open_sound_generator().
    parent._sound_generator_action = action
    return action


# ---------------------------------------------------------------------------
# Entry point (standalone use only -- not used when embedded)
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication([])
    window = SoundGeneratorWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
