"""
Video Grabber — desktop companion app (v2: full download-manager feature set)
==============================================================================
A local HTTP server + Tkinter GUI that the Chrome extension talks to.
Handles both:
  - "generic" direct files (mp4, zip, exe, pdf, etc.) via a resumable,
    pausable chunked HTTP downloader with a global speed limiter.
  - "ytdlp" jobs (HLS/.m3u8, DASH/.mpd, or a page URL like a YouTube link)
    via yt-dlp used as a library.

Freeze with PyInstaller — see build_exe.md.
"""

import json
import os
import re
import sys
import time
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

APP_PORT = 5757
HOME = Path.home() / "Downloads" / "VideoGrabber"
CONFIG_PATH = HOME / "settings.json"
JOBS_PATH = HOME / "jobs.json"

CATEGORY_MAP = {
    ".mp4": "Video", ".mkv": "Video", ".webm": "Video", ".avi": "Video", ".mov": "Video", ".m4v": "Video",
    ".mp3": "Music", ".wav": "Music", ".flac": "Music", ".m4a": "Music", ".aac": "Music",
    ".zip": "Compressed", ".rar": "Compressed", ".7z": "Compressed", ".tar": "Compressed", ".gz": "Compressed",
    ".pdf": "Documents", ".doc": "Documents", ".docx": "Documents", ".txt": "Documents", ".ppt": "Documents", ".pptx": "Documents",
    ".exe": "Programs", ".msi": "Programs", ".dmg": "Programs",
}
DIRECT_EXT_RE = re.compile(r"\.(mp4|m4v|mov|webm|mkv|avi|mp3|wav|flac|m4a|aac|zip|rar|7z|tar|gz|pdf|docx?|pptx?|txt|exe|msi|dmg)(\?|#|$)", re.I)

app = Flask(__name__)
CORS(app)

JOBS = {}          # job_id -> dict (see new_job())
JOB_COUNTER = 0
JOB_LOCK = threading.Lock()
LOG_QUEUE = queue.Queue()

STATE = {
    "output_dir": str(HOME),
    "max_concurrent": 3,
    "speed_limit_kbps": 0,      # 0 = unlimited
    "queue_running": True,
    "clipboard_monitor": False,
}


def log(msg):
    LOG_QUEUE.put(f"[{time.strftime('%H:%M:%S')}] {msg}")


def load_settings():
    if CONFIG_PATH.exists():
        try:
            STATE.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass


def save_settings():
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(STATE, indent=2))


def save_jobs_snapshot():
    """Persist a plain (non-Event) view of jobs so history survives restarts."""
    try:
        snap = {}
        for jid, j in JOBS.items():
            snap[jid] = {k: v for k, v in j.items() if k not in ("pause_evt", "stop_evt")}
        JOBS_PATH.write_text(json.dumps(snap, indent=2))
    except Exception as e:
        log(f"couldn't save job history: {e}")


def load_jobs_snapshot():
    if JOBS_PATH.exists():
        try:
            snap = json.loads(JOBS_PATH.read_text())
            for jid, j in snap.items():
                j["pause_evt"] = threading.Event()
                j["stop_evt"] = threading.Event()
                # Anything mid-flight on last shutdown is now stale.
                if j["status"] in ("downloading", "queued"):
                    j["status"] = "stopped"
                JOBS[jid] = j
            global JOB_COUNTER
            JOB_COUNTER = max([int(k) for k in JOBS.keys()] + [0])
        except Exception as e:
            log(f"couldn't load job history: {e}")


def next_job_id():
    global JOB_COUNTER
    with JOB_LOCK:
        JOB_COUNTER += 1
        return str(JOB_COUNTER)


def guess_filename(url, content_disposition=None):
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if m:
            return unquote(m.group(1))
    path = urlparse(url).path
    name = os.path.basename(path) or "download"
    return unquote(name)


def category_for(filename):
    ext = os.path.splitext(filename)[1].lower()
    return CATEGORY_MAP.get(ext, "Other")


def detect_type(url):
    if ".m3u8" in url.lower() or ".mpd" in url.lower():
        return "ytdlp"
    if DIRECT_EXT_RE.search(url):
        return "generic"
    return "ytdlp"  # probably a page URL (YouTube etc.) — let yt-dlp figure it out


def new_job(url, filename=None, category=None, referer=None, cookie=None, user_agent=None, job_type=None):
    jid = next_job_id()
    jtype = job_type or detect_type(url)
    fname = filename or guess_filename(url)
    JOBS[jid] = {
        "id": jid, "url": url, "filename": fname,
        "category": category or category_for(fname),
        "type": jtype, "status": "queued",
        "size_total": 0, "size_done": 0, "speed": "", "error": None,
        "referer": referer, "cookie": cookie, "user_agent": user_agent,
        "created_ts": time.time(),
        "pause_evt": threading.Event(), "stop_evt": threading.Event(),
    }
    log(f"job {jid} queued: {fname}")
    save_jobs_snapshot()
    return jid


class SpeedLimiter:
    """Simple shared token bucket so all active generic downloads together
    stay under STATE['speed_limit_kbps']."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last = time.time()
        self.tokens = 0.0

    def consume(self, n):
        limit = STATE["speed_limit_kbps"] * 1024
        if limit <= 0:
            return
        with self.lock:
            now = time.time()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(limit, self.tokens + elapsed * limit)
            self.tokens -= n
            deficit = -self.tokens
        if deficit > 0:
            time.sleep(deficit / limit)


LIMITER = SpeedLimiter()


def dest_path_for(job):
    cat_dir = Path(STATE["output_dir"]) / job["category"]
    cat_dir.mkdir(parents=True, exist_ok=True)
    return cat_dir / job["filename"]


def run_generic(job_id):
    job = JOBS[job_id]
    dest = dest_path_for(job)
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if job.get("user_agent"):
        headers["User-Agent"] = job["user_agent"]

    existing = part.stat().st_size if part.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"

    try:
        with requests.get(job["url"], headers=headers, stream=True, timeout=20) as r:
            if r.status_code not in (200, 206):
                job["status"] = "error"
                job["error"] = f"HTTP {r.status_code}"
                log(f"job {job_id}: server returned {r.status_code}")
                return
            total = int(r.headers.get("Content-Length", 0)) + existing
            job["size_total"] = total
            job["size_done"] = existing
            mode = "ab" if existing else "wb"
            last_report = time.time()
            last_bytes = existing
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if job["stop_evt"].is_set():
                        job["status"] = "stopped"
                        log(f"job {job_id}: stopped")
                        return
                    if job["pause_evt"].is_set():
                        job["status"] = "paused"
                        log(f"job {job_id}: paused")
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    LIMITER.consume(len(chunk))
                    job["size_done"] += len(chunk)
                    now = time.time()
                    if now - last_report >= 1:
                        rate = (job["size_done"] - last_bytes) / (now - last_report)
                        job["speed"] = f"{rate/1024:.0f} KB/s"
                        last_report, last_bytes = now, job["size_done"]
            part.rename(dest)
            job["status"] = "done"
            job["speed"] = ""
            log(f"job {job_id}: complete ✓ ({dest.name})")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")
    finally:
        save_jobs_snapshot()


class DownloadCancelled(Exception):
    pass


def run_ytdlp(job_id):
    job = JOBS[job_id]
    headers = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if job.get("user_agent"):
        headers["User-Agent"] = job["user_agent"]

    cat_dir = Path(STATE["output_dir"]) / job["category"]
    cat_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(cat_dir / (Path(job["filename"]).stem + ".%(ext)s"))

    def hook(d):
        while job["pause_evt"].is_set():
            job["status"] = "paused"
            time.sleep(0.5)
        if job["stop_evt"].is_set():
            raise DownloadCancelled()
        if d["status"] == "downloading":
            job["status"] = "downloading"
            job["size_total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            job["size_done"] = d.get("downloaded_bytes", 0)
            job["speed"] = f"{(d.get('speed') or 0)/1024:.0f} KB/s"
        elif d["status"] == "finished":
            job["speed"] = ""

    ydl_opts = {
        "outtmpl": outtmpl,
        "http_headers": headers,
        "progress_hooks": [hook],
        "quiet": True, "no_warnings": True,
        "merge_output_format": "mp4",
        "continuedl": True,
    }
    if STATE["speed_limit_kbps"] > 0:
        ydl_opts["ratelimit"] = STATE["speed_limit_kbps"] * 1024

    job["status"] = "downloading"
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([job["url"]])
        job["status"] = "done"
        log(f"job {job_id}: complete ✓")
    except DownloadCancelled:
        job["status"] = "stopped"
        log(f"job {job_id}: stopped")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")
    finally:
        save_jobs_snapshot()


def start_job_thread(job_id):
    job = JOBS[job_id]
    job["pause_evt"].clear()
    job["stop_evt"].clear()
    job["status"] = "downloading"
    target = run_generic if job["type"] == "generic" else run_ytdlp
    threading.Thread(target=target, args=(job_id,), daemon=True).start()


def dispatcher_loop():
    while True:
        time.sleep(1)
        if not STATE["queue_running"]:
            continue
        active = sum(1 for j in JOBS.values() if j["status"] == "downloading")
        if active >= STATE["max_concurrent"]:
            continue
        for jid, j in list(JOBS.items()):
            if j["status"] == "queued":
                start_job_thread(jid)
                break


# --------------------------------------------------------------- API ------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "output_dir": STATE["output_dir"]})


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    jid = new_job(
        url, filename=data.get("filename"), category=data.get("category"),
        referer=data.get("referer"), cookie=data.get("cookie"),
        user_agent=data.get("user_agent"), job_type=data.get("type"),
    )
    return jsonify({"job_id": jid})


@app.route("/batch", methods=["POST"])
def batch():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items", [])
    ids = []
    for it in items:
        if not it.get("url"):
            continue
        ids.append(new_job(
            it["url"], filename=it.get("filename"),
            referer=data.get("referer"), cookie=data.get("cookie"),
            user_agent=data.get("user_agent"),
        ))
    return jsonify({"job_ids": ids})


def _job_action(job_id, action):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if action == "pause":
        job["pause_evt"].set()
    elif action == "resume":
        job["pause_evt"].clear()
        job["stop_evt"].clear()
        job["status"] = "queued"
    elif action == "stop":
        job["stop_evt"].set()
        job["pause_evt"].clear()
    elif action == "delete":
        job["stop_evt"].set()
        JOBS.pop(job_id, None)
    save_jobs_snapshot()
    return jsonify({"ok": True})


@app.route("/pause/<job_id>", methods=["POST"])
def pause(job_id):
    return _job_action(job_id, "pause")


@app.route("/resume/<job_id>", methods=["POST"])
def resume(job_id):
    return _job_action(job_id, "resume")


@app.route("/stop/<job_id>", methods=["POST"])
def stop(job_id):
    return _job_action(job_id, "stop")


@app.route("/delete/<job_id>", methods=["POST"])
def delete(job_id):
    return _job_action(job_id, "delete")


@app.route("/jobs", methods=["GET"])
def jobs():
    return jsonify({jid: {k: v for k, v in j.items() if k not in ("pause_evt", "stop_evt")}
                     for jid, j in JOBS.items()})


def run_server():
    app.run(host="127.0.0.1", port=APP_PORT, debug=False, use_reloader=False)


# ---------------------------------------------------------------- GUI -----
STATUS_COLORS = {
    "queued": "#999", "downloading": "#4caf50", "paused": "#f9a825",
    "stopped": "#e53935", "done": "#4caf50", "error": "#e53935",
}


def human_size(n):
    if not n:
        return "-"
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class GUI:
    def __init__(self, root):
        self.root = root
        root.title("Video Grabber")
        root.geometry("880x520")
        DARK_BG, DARK_FG, DARK_PANEL = "#1e1e1e", "#e0e0e0", "#262626"
        root.configure(bg=DARK_BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=DARK_PANEL, fieldbackground=DARK_PANEL, foreground=DARK_FG, rowheight=24)
        style.configure("Treeview.Heading", background="#333", foreground=DARK_FG)
        style.map("Treeview", background=[("selected", "#3a6ea5")])

        # ---- menu ----
        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Export queue…", command=self.export_queue)
        filemenu.add_command(label="Import queue…", command=self.import_queue)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=root.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        root.config(menu=menubar)

        # ---- toolbar ----
        bar = tk.Frame(root, bg=DARK_BG)
        bar.pack(fill="x", padx=6, pady=6)
        for text, cmd in [
            ("+ Add URL", self.add_url), ("+ Batch", self.add_batch),
            ("Resume", self.resume_sel), ("Pause", self.pause_sel), ("Stop", self.stop_sel),
            ("Delete", self.delete_sel), ("Delete Completed", self.delete_completed),
            ("Start Queue", self.start_queue), ("Stop Queue", self.stop_queue),
            ("Scheduler", self.open_scheduler), ("Options", self.open_options),
        ]:
            tk.Button(bar, text=text, command=cmd, bg="#333", fg=DARK_FG,
                      activebackground="#444", activeforeground=DARK_FG,
                      relief="flat", padx=8).pack(side="left", padx=2)

        body = tk.Frame(root, bg=DARK_BG)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # ---- category sidebar ----
        side = tk.Frame(body, bg=DARK_PANEL, width=140)
        side.pack(side="left", fill="y")
        tk.Label(side, text="Categories", bg=DARK_PANEL, fg=DARK_FG, font=("", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 4))
        self.cat_var = tk.StringVar(value="All")
        for c in ["All", "Video", "Music", "Compressed", "Documents", "Programs", "Other", "Finished", "Unfinished"]:
            tk.Radiobutton(side, text=c, variable=self.cat_var, value=c, command=self.refresh,
                           bg=DARK_PANEL, fg=DARK_FG, selectcolor="#444", activebackground=DARK_PANEL,
                           activeforeground=DARK_FG, anchor="w").pack(fill="x", padx=6)

        # ---- job table ----
        cols = ("name", "size", "progress", "speed", "status", "category")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        for c, w in [("name", 260), ("size", 90), ("progress", 90), ("speed", 90), ("status", 90), ("category", 90)]:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w)
        self.tree.pack(side="left", fill="both", expand=True)

        # ---- log ----
        self.log_box = tk.Text(root, height=6, bg="#111", fg="#8bc34a", state="disabled")
        self.log_box.pack(fill="x", padx=6, pady=(0, 6))

        self.root.after(300, self.drain_log)
        self.root.after(500, self.refresh)

    # -- helpers --
    def selected_ids(self):
        return list(self.tree.selection())

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        filt = self.cat_var.get()
        for jid, j in sorted(JOBS.items(), key=lambda kv: -kv[1]["created_ts"]):
            if filt == "Finished" and j["status"] != "done":
                continue
            if filt == "Unfinished" and j["status"] == "done":
                continue
            if filt not in ("All", "Finished", "Unfinished") and j["category"] != filt:
                continue
            pct = f"{(j['size_done']/j['size_total']*100):.0f}%" if j["size_total"] else ("100%" if j["status"] == "done" else "-")
            self.tree.insert("", "end", iid=jid, values=(
                j["filename"], human_size(j["size_total"]), pct, j["speed"], j["status"], j["category"],
            ))
        self.root.after(700, self.refresh)

    def drain_log(self):
        try:
            while True:
                line = LOG_QUEUE.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(300, self.drain_log)

    # -- toolbar actions --
    def add_url(self):
        url = simpledialog.askstring("Add URL", "Paste a video / file / page URL:")
        if url:
            new_job(url.strip())

    def add_batch(self):
        win = tk.Toplevel(self.root)
        win.title("Add batch download")
        tk.Label(win, text="One URL per line:").pack(anchor="w", padx=8, pady=(8, 0))
        txt = tk.Text(win, width=70, height=12)
        txt.pack(padx=8, pady=8)

        def submit():
            for line in txt.get("1.0", "end").splitlines():
                line = line.strip()
                if line:
                    new_job(line)
            win.destroy()

        tk.Button(win, text="Add all", command=submit).pack(pady=(0, 8))

    def pause_sel(self):
        for jid in self.selected_ids():
            JOBS[jid]["pause_evt"].set()

    def resume_sel(self):
        for jid in self.selected_ids():
            j = JOBS[jid]
            j["pause_evt"].clear()
            j["stop_evt"].clear()
            j["status"] = "queued"

    def stop_sel(self):
        for jid in self.selected_ids():
            JOBS[jid]["stop_evt"].set()

    def delete_sel(self):
        for jid in self.selected_ids():
            JOBS[jid]["stop_evt"].set()
            JOBS.pop(jid, None)
        save_jobs_snapshot()

    def delete_completed(self):
        for jid in [j for j, v in JOBS.items() if v["status"] == "done"]:
            JOBS.pop(jid, None)
        save_jobs_snapshot()

    def start_queue(self):
        STATE["queue_running"] = True
        save_settings()

    def stop_queue(self):
        STATE["queue_running"] = False
        save_settings()

    def open_options(self):
        win = tk.Toplevel(self.root)
        win.title("Options")
        tk.Label(win, text="Save folder:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        dir_var = tk.StringVar(value=STATE["output_dir"])
        tk.Entry(win, textvariable=dir_var, width=40).grid(row=0, column=1, padx=4)
        tk.Button(win, text="Browse…", command=lambda: dir_var.set(
            filedialog.askdirectory(initialdir=dir_var.get()) or dir_var.get()
        )).grid(row=0, column=2, padx=4)

        tk.Label(win, text="Max simultaneous downloads:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        conc_var = tk.IntVar(value=STATE["max_concurrent"])
        tk.Spinbox(win, from_=1, to=10, textvariable=conc_var, width=5).grid(row=1, column=1, sticky="w")

        tk.Label(win, text="Speed limit (KB/s, 0=unlimited):").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        speed_var = tk.IntVar(value=STATE["speed_limit_kbps"])
        tk.Entry(win, textvariable=speed_var, width=8).grid(row=2, column=1, sticky="w")

        clip_var = tk.BooleanVar(value=STATE["clipboard_monitor"])
        tk.Checkbutton(win, text="Monitor clipboard for downloadable links", variable=clip_var).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=8, pady=6)

        def save():
            STATE["output_dir"] = dir_var.get()
            STATE["max_concurrent"] = conc_var.get()
            STATE["speed_limit_kbps"] = speed_var.get()
            STATE["clipboard_monitor"] = clip_var.get()
            save_settings()
            win.destroy()

        tk.Button(win, text="Save", command=save).grid(row=4, column=0, columnspan=3, pady=10)

    def open_scheduler(self):
        win = tk.Toplevel(self.root)
        win.title("Scheduler")
        tk.Label(win, text="Start queue at (HH:MM, 24h, today):").grid(row=0, column=0, padx=8, pady=8)
        time_var = tk.StringVar()
        tk.Entry(win, textvariable=time_var, width=8).grid(row=0, column=1)

        def arm():
            target = time_var.get().strip()
            try:
                hh, mm = map(int, target.split(":"))
            except Exception:
                messagebox.showerror("Scheduler", "Use HH:MM, e.g. 23:30")
                return

            def waiter():
                while True:
                    now = time.localtime()
                    if now.tm_hour == hh and now.tm_min == mm:
                        STATE["queue_running"] = True
                        log(f"Scheduler: queue started at {target}")
                        return
                    time.sleep(20)

            threading.Thread(target=waiter, daemon=True).start()
            log(f"Scheduler armed for {target}")
            win.destroy()

        tk.Button(win, text="Arm", command=arm).grid(row=1, column=0, columnspan=2, pady=8)

    def export_queue(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="queue.json")
        if not path:
            return
        data = [{"url": j["url"], "filename": j["filename"], "category": j["category"]} for j in JOBS.values()]
        Path(path).write_text(json.dumps(data, indent=2))

    def import_queue(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = json.loads(Path(path).read_text())
        for item in data:
            new_job(item["url"], filename=item.get("filename"), category=item.get("category"))


def clipboard_watcher(root):
    last_seen = None
    while True:
        time.sleep(1.5)
        if not STATE["clipboard_monitor"]:
            continue
        try:
            content = root.clipboard_get()
        except Exception:
            content = None
        if content and content != last_seen and content.startswith("http") and DIRECT_EXT_RE.search(content):
            last_seen = content
            if messagebox.askyesno("Video Grabber", f"Download this from clipboard?\n\n{content[:120]}"):
                new_job(content.strip())


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    load_settings()
    load_jobs_snapshot()

    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=dispatcher_loop, daemon=True).start()
    log(f"Server started on port {APP_PORT}. Saving to {STATE['output_dir']}")

    root = tk.Tk()
    gui = GUI(root)
    threading.Thread(target=clipboard_watcher, args=(root,), daemon=True).start()
    root.mainloop()
    save_jobs_snapshot()


if __name__ == "__main__":
    main()
