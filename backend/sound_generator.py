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
"""

import math
import os
import random
import struct
import wave
from pathlib import Path

# winsound is Windows-only (and required for SND_ASYNC playback).
# Guard the import so this file still *runs* on macOS/Linux for testing,
# where playback falls back to PySide6's QSoundEffect instead.
try:
    import winsound
except ImportError:  # Not on Windows
    winsound = None

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
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

def _add_tone(
    buffer: list[float],
    start_seconds: float,
    duration_seconds: float,
    frequency: float,
    amplitude: float = 1.0,
    decay: float = 6.0,
    overtones: tuple[tuple[float, float], ...] = ((1.0, 1.0),),
    sweep_to: float | None = None,
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
                   tone's duration (used for the "Pop" blip).
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
            sample += relative_amp * math.sin(phase * multiplier)

        buffer[index] += amplitude * envelope * sample


def _render_style(style: str) -> list[float]:
    """Render one of the four styles into a float sample buffer."""
    if style == "Arpeggio":
        # 3 quick rising notes: root, major third (+4 semitones), fifth (+7).
        buffer = [0.0] * int(0.55 * SAMPLE_RATE)
        base = random.choice([523.25, 587.33, 659.25])  # C5 / D5 / E5
        for step, semitones in enumerate((0, 4, 7)):
            freq = base * (2.0 ** (semitones / 12.0))
            _add_tone(buffer, start_seconds=step * 0.09, duration_seconds=0.22,
                      frequency=freq, amplitude=0.8, decay=7.0)

    elif style == "Bell":
        # 1 note, long decay. Inharmonic partials (1, 2.76, 5.4) are what
        # make a sine stack sound like a bell rather than an organ.
        buffer = [0.0] * int(1.20 * SAMPLE_RATE)
        base = random.choice([659.25, 783.99, 880.00, 1046.50])
        _add_tone(buffer, start_seconds=0.0, duration_seconds=1.20,
                  frequency=base, amplitude=1.0, decay=4.0,
                  overtones=((1.0, 1.0), (2.76, 0.45), (5.40, 0.18)))

    elif style == "Pop":
        # 1 very short, low-frequency blip with a downward pitch sweep --
        # the sweep is what gives it that "bloop" character.
        buffer = [0.0] * int(0.14 * SAMPLE_RATE)
        _add_tone(buffer, start_seconds=0.0, duration_seconds=0.10,
                  frequency=random.uniform(280.0, 360.0), sweep_to=90.0,
                  amplitude=1.0, decay=10.0)

    elif style == "Double Beep":
        # 2 quick identical notes, clean sine with a hint of 2nd harmonic.
        buffer = [0.0] * int(0.42 * SAMPLE_RATE)
        freq = random.choice([880.00, 987.77, 1046.50])
        for step in range(2):
            _add_tone(buffer, start_seconds=step * 0.17, duration_seconds=0.11,
                      frequency=freq, amplitude=0.85, decay=1.5,
                      overtones=((1.0, 1.0), (2.0, 0.12)))

    else:  # pragma: no cover - styles are only chosen from the list below
        raise ValueError(f"Unknown style: {style}")

    return buffer


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


def generate_sound(output_path: str | Path) -> tuple[Path, str]:
    """Synthesise a random notification sound and write it as a .wav.

    Randomly picks one of: "Arpeggio", "Bell", "Pop", "Double Beep".
    A 4 ms attack is applied to every note and the result is normalised.

    Args:
        output_path: Where to write the .wav file.

    Returns:
        (path, style_name) -- so the UI can tell you what it generated.
    """
    output_path = Path(output_path)
    if output_path.parent != Path(""):
        output_path.parent.mkdir(parents=True, exist_ok=True)

    style = random.choice(["Arpeggio", "Bell", "Pop", "Double Beep"])
    buffer = _render_style(style)
    _normalise(buffer)
    _write_wav(output_path, buffer)
    return output_path, style


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
    """Preview, name, save and manage procedurally generated sounds."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Video Grabber -- Sound Generator")
        self.resize(760, 360)

        self.temp_path: Path | None = None

        # Fallback player (only actually used when winsound is missing).
        self._fallback_player = QSoundEffect(self) if QSoundEffect else None

        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)

        # --- Left side: generation + preview + saving -------------------
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
        left_layout.addWidget(self.generate_button)
        left_layout.addWidget(self.waveform)
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
        QTimer.singleShot(500, lambda: self.generate_button.setEnabled(True))

        # Stop the previous preview before overwriting the temp file it
        # may still be playing from (a playing file can be locked).
        stop_playback(self._fallback_player)

        try:
            self.temp_path, style = generate_sound(TEMP_PATH)
        except OSError as exc:
            QMessageBox.warning(self, "Generation failed",
                                f"Could not write the preview file:\n{exc}")
            return

        self.waveform.load_wav(self.temp_path)
        self.style_label.setText(f"Style: {style}  --  press Save to keep it")
        self.save_button.setEnabled(True)
        play_wav_async(self.temp_path, self._fallback_player)

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

        destination = SOUNDS_DIR / f"{name}.wav"
        # Final safeguard, independent of _clean_name: the file we are
        # about to write really must live directly inside ./sounds/.
        if destination.parent.resolve() != SOUNDS_DIR.resolve():
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
        self.style_label.setText(f"Saved as {destination.name}")

        # Fresh default name for the next sound, then refresh + select it.
        self.name_input.setText(f"custom_{random.randint(1000, 9999)}")
        self.refresh_list(select_name=destination.name)

    # -- Saved-sounds list ----------------------------------------------------

    def refresh_list(self, select_name: str | None = None) -> None:
        """Reload the list widget from ./sounds/."""
        self.sounds_list.clear()
        for wav_path in sorted(SOUNDS_DIR.glob("*.wav")):
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
        return SOUNDS_DIR / item.text() if item else None

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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication([])
    window = SoundGeneratorWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
