"""What's New, Pair, Help and Update dialogs, plus dialog fade helpers."""

import re

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QProgressBar, QPushButton, QStackedWidget, QTextBrowser, QVBoxLayout,
)

import gui_palette
from gui_api import _ApiFetchWorker
from gui_fit import fade_veil, install_fit
from gui_settings_dialog import _COMPONENT_WORKERS


def _fade_widget(widget, enabled, duration=170):
    """Soft fade-in of a widget (the table viewport when rows change). Uses a
    painted veil, not a graphics effect: an effect on a scroll-area viewport
    could be replaced mid-animation by the next fade."""
    fade_veil(widget, enabled, gui_palette.PANEL, duration)


def _fade_dialog(dialog, enabled, duration=140):
    """Opacity fade for dialogs. Runs for every dialog (it used to run only for
    the first one of the session); no-op when animations are off."""
    if not enabled:
        return
    try:
        dialog.setWindowOpacity(0.0)
        anim = QPropertyAnimation(dialog, b"windowOpacity", dialog)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda d=dialog: d.setWindowOpacity(1.0))
        anim.start(QAbstractAnimation.DeleteWhenStopped)
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# v5.5 Batch 3: Help, Pair Extension and What's New dialogs
# ---------------------------------------------------------------------------
def _md_to_html(md):
    """Small markdown subset (headers, bullets, bold, code, links) for the
    What's New dialog. Everything is HTML-escaped first."""
    import html as _html

    def inline(t):
        t = _html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        t = re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
        return t

    out, in_ul = [], False
    for raw in str(md or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^\s*[-*+]\s+(.*)", line)
        if m:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if not line.strip():
            continue
        h = re.match(r"^(#{1,6})\s*(.*)", line)
        if h:
            out.append(f"<h{3 if len(h.group(1)) <= 2 else 4}>{inline(h.group(2))}</h{3 if len(h.group(1)) <= 2 else 4}>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "".join(out)


def _release_header(p):
    """'rev 59 — v5.5.0' style header for a /changelog payload."""
    tag = str(p.get("tag") or "")
    name = str(p.get("name") or "")
    rev = re.fullmatch(r"[vV]?(\d+)", tag.strip())
    ver = re.search(r"\bv?(\d+\.\d+\.\d+)\b", name + " " + tag)
    if rev and ver:
        return f"rev {rev.group(1)} \u2014 v{ver.group(1)}"
    if rev:
        return f"rev {rev.group(1)}" + (f" \u2014 {name}" if name and name != tag else "")
    return name or tag or "Latest release"


class WhatsNewDialog(QDialog):
    """Latest release notes from the app's /changelog route, formatted."""

    def __init__(self, parent, api):
        super().__init__(parent)
        self.setWindowTitle("What's New")
        install_fit(self, 560, 520, 360, 280)
        lay = QVBoxLayout(self)
        self.status = QLabel("Loading release notes\u2026")
        lay.addWidget(self.status)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setFixedHeight(6)
        lay.addWidget(self.busy)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        # Parentless worker kept in the registry: closing the dialog while the
        # request is still running must not destroy a running QThread.
        w = _ApiFetchWorker(api.changelog)
        _COMPONENT_WORKERS.add(w)
        w.result.connect(self._show, Qt.QueuedConnection)
        w.error.connect(self._fail, Qt.QueuedConnection)
        w.finished.connect(lambda w=w: _COMPONENT_WORKERS.discard(w))
        w.start()

    def _show(self, payload):
        self.busy.setVisible(False)
        payload = payload or {}
        if not payload.get("ok"):
            self._fail(str(payload.get("error") or "no data"))
            return
        head = _release_header(payload)
        date = str(payload.get("published_at") or "")[:10]
        self.status.setText(head + (f"   ({date})" if date else ""))
        body = _md_to_html(payload.get("body"))
        self.view.setHtml(f"<h3>{head}</h3>" + (body or "<p>This release has no notes.</p>"))

    def _fail(self, msg):
        self.busy.setVisible(False)
        self.status.setStyleSheet(f"color: {gui_palette.DANGER};")
        self.status.setText(f"Couldn't load the release notes ({msg}). Check your connection and try again.")


class PairDialog(QDialog):
    """Step-by-step pairing for the browser extension. Same token as
    Settings > Pairing, which stays as the secondary entry."""

    def __init__(self, parent, token):
        super().__init__(parent)
        self.setWindowTitle("Pair Extension")
        install_fit(self, 480, 360, 340, 260)
        lay = QVBoxLayout(self)
        intro = QLabel(
            "<h3 style='margin-bottom:2px;'>Pair the browser extension</h3>"
            "<p>Pairing is usually automatic: with this app running, open the extension "
            "popup and it should show as connected.</p>"
            "<p>If it can't reach the app, pair it by hand:</p>"
            "<ol>"
            "<li>Copy the token below.</li>"
            "<li>In your browser, right-click the Video Grabber icon and choose "
            "<b>Options</b>.</li>"
            "<li>Paste the token, press <b>Save</b>, then <b>Test connection</b>.</li>"
            "</ol>")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.token_edit = QLineEdit(token or "")
        self.token_edit.setReadOnly(True)
        row.addWidget(self.token_edit, 1)
        copy = QPushButton("Copy token")
        copy.setObjectName("primary")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        lay.addLayout(row)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color: {gui_palette.SUCCESS};")
        lay.addWidget(self.note)
        lay.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _copy(self):
        QApplication.clipboard().setText(self.token_edit.text())
        self.note.setText("Token copied to the clipboard.")


def _help_pages():
    """(section title, HTML) for the Help dialog."""
    dim = f"color:{gui_palette.MUTED};"
    return [
        ("Capabilities", f"""
<h3>What Video Grabber does</h3>
<p>It downloads videos, audio, images, documents, archives and other files, from the browser
extension or from links you add with the + button.</p>
<ul>
<li><b>Three engines.</b> yt-dlp handles most sites, including YouTube and HLS/DASH streams.
Streamlink handles live streams such as Twitch. A built-in segmented downloader fetches direct
files in parallel pieces. The app picks one per link, and Settings &gt; Rules can override it per site.</li>
<li><b>Queue.</b> Downloads run in order. The toolbar pauses or resumes the whole queue,
Alt+Up / Alt+Down reorder queued items, and the Queue entry in the sidebar shows only what is
running or waiting. Tools &gt; Downloads &gt; Bandwidth profiles manages bandwidth profiles.</li>
<li><b>Categories.</b> Files are sorted into Video, Music, Images, Compressed, Documents,
Programs, Fonts and Other. Settings &gt; Downloads &gt; Destinations sets the folders.</li>
<li><b>After a download.</b> Right-click a row to open it, move it to another category, convert
it (mp4, mkv, mp3, wav, m4a, flac, opus), reorder it or delete it. Settings &gt; Post-Download
covers what happens when a download finishes; Settings &gt; Sounds covers the completion sound.</li>
<li><b>Media Toolkit.</b> Compress, trim, or turn a clip into a GIF.</li>
<li><b>Look and feel.</b> Themes, text size, Blanc minimal mode, a collapsible sidebar and an
optional handheld shell.</li>
<li><b>Updates.</b> yt-dlp updates itself in the background. The app asks before it installs an
update. ffmpeg and deno update from Settings &gt; Updates.</li>
</ul>
<p>Basics: click a row to see its details below, double-click a finished file to open it, and
drag finished files out into other apps.</p>"""),
        ("FAQ", f"""
<h3>Common questions</h3>
<p><b>Why did my download fail?</b><br>Click the failed row: the details pane shows the reason.
Usual causes are an expired link (send it again from the page), a site that needs your login
(send it through the extension so your browser cookies go with it), or an engine that needs an
update. Try Tools &gt; Downloads &gt; Retry all failed, then Tools &gt; Updates &gt; Check for
updates. If a new yt-dlp broke a site that worked before, use Tools &gt; Updates &gt; Roll back
yt-dlp.</p>
<p><b>Why is a download "held"?</b><br>YouTube sometimes asks to confirm you're not a bot, and the
app waits before trying again. Switch network or VPN and press Resume to skip the wait.</p>
<p><b>Why is the pill not showing?</b><br>Open the extension popup, press the cog, and check that
<i>Show the pill on videos</i> is on and the site isn't in the hidden list. The pill sits on video
elements; if a page plays video another way, use the popup's list of detected media or
<b>Grab all</b>. Reload the page after changing a setting.</p>
<p><b>The extension says the app is unreachable.</b><br>Start Video Grabber. If it is running, use
Tools &gt; Pairing &gt; Pair Extension and follow the steps.</p>
<p><b>Where do my files go?</b><br>Settings &gt; Downloads &gt; Destinations: a default folder
plus an optional folder per category.</p>
<p><b>Why did a file land in Other?</b><br>The category follows the file type. Add a rule in
Settings &gt; Rules to send a site's downloads to a category.</p>
<p><b>Why no sound when a download finishes?</b><br>In Settings &gt; Sounds turn the
completion sound on, pick a sound and press Test Sound. If the test falls back to the Windows
beep and you hear nothing, your Windows sound scheme may be set to No Sounds.</p>
<p><b>I sent the same link twice.</b><br>A link that is already queued or downloading is not
added again.</p>"""),
        ("Troubleshooting", f"""
<h3>When something goes wrong</h3>
<p><b>Logs.</b> Tools &gt; Downloads &gt; Open logs folder. Settings, the job list and the logs live
in the VideoGrabber folder inside your Downloads folder. Attach the newest log when you report
a problem.</p>
<p><b>Repair and retry.</b></p>
<ul>
<li>Tools &gt; Downloads &gt; Retry all failed requeues every failed download.</li>
<li>Right-click a download to download it again from scratch.</li>
<li>Tools &gt; Updates &gt; Roll back yt-dlp returns to the yt-dlp that shipped with the app.</li>
<li>Recordings made by the extension are repaired with ffmpeg automatically.</li>
<li>Tools &gt; Pairing &gt; Pair Extension fixes an extension that lost its connection.</li>
<li>Tools &gt; Media &gt; Trim UI memory frees memory if the window feels sluggish.</li>
</ul>
<p><b>Updates.</b> Settings &gt; Updates shows the yt-dlp and app versions (press Refresh) and
checks ffmpeg and deno. Downloaded updates apply the next time the app starts.</p>
<p style="{dim}">Nothing here changes your downloaded files.</p>"""),
        ("Extension guide", f"""
<h3>The browser extension</h3>
<p><b>The pill.</b> A small button appears over videos. Click it to open the menu: pick a quality
(the app remembers your pick per site), extract audio, record, or choose <b>Start Queue</b>. On some
sites the pill sends the video in one click instead. Drag the pill to move it, double-click it to
reset its position, and right-click it to snap it to a corner, start the queue or hide it on the
current site. Hovering an image can show a small pill too.</p>
<p><b>The popup.</b> Click the toolbar icon. <b>Current Page</b> lists the media the extension
detected; <b>Other Pages</b> lists other tabs. Tick items and press <b>Download</b>. <b>Grab all</b>
scans the page for every media file and link. The <b>&hellip;</b> menu holds Merge, Copy, Toggle,
Sort, Filter, Clear and Search. The cog opens the extension settings (pill, sniffer,
notifications).</p>
<p><b>Shortcuts.</b> Ctrl+Shift+D downloads the largest media detected on the page. Ctrl+Shift+A
opens the add dialog.</p>
<p><b>Options page.</b> Holds the pairing token, an option to send the browser's own downloads to
Video Grabber (off by default) and a switch for in-page downloads on claude.ai.</p>
<p><b>Pairing.</b> Usually automatic. If not, see Tools &gt; Pairing &gt; Pair Extension.</p>"""),
        ("Shell guide", f"""
<h3>The handheld shell</h3>
<p>An optional console body around the window. Turn it on in Settings &gt; Theme (Handheld
shell), or flip it from anywhere with Ctrl+Shift+S. It works best with a window of about
900 by 640.</p>
<ul>
<li><b>D-pad up / down</b> moves the selection; <b>left / right</b> changes the filter.</li>
<li><b>A</b> resumes and <b>B</b> pauses the selected download.</li>
<li><b>START</b> pauses or resumes the whole queue. <b>SELECT</b> jumps to search.</li>
<li>The <b>power light</b> switches the backlight.</li>
</ul>
<p>Colors, backlight and the window frame are in Settings &gt; Theme (Detailed view).</p>"""),
        ("Toolkit guide", f"""
<h3>Media Toolkit</h3>
<p>Open it from Tools &gt; Media &gt; Media Toolkit. Preview a video, pick a range on the
timeline with the two draggable handles, then use one of the tabs:</p>
<ul>
<li><b>Compress</b> re-encodes the video smaller, optionally only the selected range.</li>
<li><b>Trim</b> cuts the Start to End range. <i>Exact Cut</i> re-encodes for a frame-accurate
cut.</li>
<li><b>Make GIF</b> turns the range into a GIF (Web Optimized 480p or High Quality 1080p).</li>
</ul>
<p>Results land next to the original and never overwrite it (<i>name</i>_compressed.mp4,
<i>name</i>_trimmed, <i>name</i>_clip.gif; a "(2)" is added if the name is taken).</p>
<p><b>Keys:</b> Space play/pause, I set start, O set end, comma and period step one frame.</p>"""),
    ]


class HelpDialog(QDialog):
    """Help with sections: capabilities, FAQ, troubleshooting and guides."""

    def __init__(self, parent, pair_fn=None, whats_new_fn=None):
        super().__init__(parent)
        self.setWindowTitle("Help")
        install_fit(self, 780, 540, 520, 320)
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(170)
        self.stack = QStackedWidget()
        for title, html in _help_pages():
            self.nav.addItem(title)
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            view.setHtml(html)
            self.stack.addWidget(view)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        row = QHBoxLayout()
        if pair_fn:
            pair = QPushButton("Pair Extension\u2026")
            pair.setObjectName("primary")
            pair.clicked.connect(pair_fn)
            row.addWidget(pair)
        if whats_new_fn:
            wn = QPushButton("What's New\u2026")
            wn.clicked.connect(whats_new_fn)
            row.addWidget(wn)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)


class UpdateDialog(QDialog):
    """Prominent new-version prompt. self.choice ends up as "now",
    "later" (4 h snooze) or "skip" (this version only). The caller saves
    the snooze or skip; closing the window counts as "later"."""

    def __init__(self, parent, tag, current):
        super().__init__(parent)
        self.choice = "later"
        self.setWindowTitle("Video Grabber update")
        self.setModal(True)
        self.setMinimumWidth(320)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        title = QLabel(f"Video Grabber {tag} is available")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        body = QLabel(
            f"You're on v{current}. Update now for the latest fixes and features. "
            "The app restarts briefly while the files are swapped.")
        body.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(body)
        row = QHBoxLayout()
        skip = QPushButton("Skip This Version")
        later = QPushButton("Remind Me Later")
        now = QPushButton("Update Now")
        now.setDefault(True)
        for btn, choice in ((skip, "skip"), (later, "later"), (now, "now")):
            btn.setMinimumHeight(30)
            btn.clicked.connect(lambda _=False, c=choice: self._pick(c))
        row.addWidget(skip)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(now)
        lay.addLayout(row)

    def _pick(self, choice):
        self.choice = choice
        self.accept()
