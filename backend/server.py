"""
Video Grabber — desktop companion app
======================================
Runs a small local HTTP server the Chrome extension talks to, and hands
download jobs off to yt-dlp (used as a Python library, not a subprocess,
so this file freezes cleanly into a single .exe with PyInstaller).

Start it, leave it running in the background, then click "Download this
video" / "Send to Grabber" in the browser.

Build a standalone .exe:
    pip install -r requirements.txt
    pyinstaller --onefile --noconsole --name VideoGrabber --collect-all yt_dlp server.py
(see build_exe.md for details / troubleshooting)
"""

import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, scrolledtext
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from yt_dlp import YoutubeDL

APP_PORT = 5757
DEFAULT_OUTPUT_DIR = str(Path.home() / "Downloads" / "VideoGrabber")

app = Flask(__name__)
CORS(app)  # the request originates from a chrome-extension:// / page origin

JOBS = {}          # job_id -> dict(status, progress, filename, error)
JOB_COUNTER = 0
JOB_LOCK = threading.Lock()
LOG_QUEUE = queue.Queue()   # for the GUI log panel
STATE = {"output_dir": DEFAULT_OUTPUT_DIR}


def log(msg):
    LOG_QUEUE.put(f"[{time.strftime('%H:%M:%S')}] {msg}")


def next_job_id():
    global JOB_COUNTER
    with JOB_LOCK:
        JOB_COUNTER += 1
        return str(JOB_COUNTER)


def run_download(job_id, url, referer, cookie, user_agent, filename):
    os.makedirs(STATE["output_dir"], exist_ok=True)
    headers = {}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    if user_agent:
        headers["User-Agent"] = user_agent

    outtmpl = os.path.join(
        STATE["output_dir"],
        f"{filename or '%(title)s'}.%(ext)s" if filename else "%(title)s.%(ext)s",
    )

    def hook(d):
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "").strip()
            JOBS[job_id]["progress"] = pct
            log(f"job {job_id}: {pct} of {d.get('filename', url)}")
        elif d["status"] == "finished":
            JOBS[job_id]["progress"] = "100%"
            log(f"job {job_id}: download finished, merging/post-processing…")

    ydl_opts = {
        "outtmpl": outtmpl,
        "http_headers": headers,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # Deliberately NOT attempting DRM-protected extraction/decryption.
    }

    JOBS[job_id]["status"] = "downloading"
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        JOBS[job_id]["status"] = "done"
        log(f"job {job_id}: complete ✓")
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)
        log(f"job {job_id}: FAILED — {e}")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "output_dir": STATE["output_dir"]})


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400

    job_id = next_job_id()
    JOBS[job_id] = {"status": "queued", "url": url, "progress": "0%", "error": None}
    log(f"job {job_id}: queued — {url[:100]}")

    t = threading.Thread(
        target=run_download,
        args=(job_id, url, data.get("referer"), data.get("cookie"),
              data.get("user_agent"), data.get("filename")),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.route("/jobs", methods=["GET"])
def jobs():
    return jsonify(JOBS)


def run_server():
    app.run(host="127.0.0.1", port=APP_PORT, debug=False, use_reloader=False)


# ---------------------------------------------------------------- GUI -----
class GUI:
    def __init__(self, root):
        self.root = root
        root.title("Video Grabber")
        root.geometry("560x400")

        top = tk.Frame(root)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="Save folder:").pack(side="left")
        self.dir_var = tk.StringVar(value=STATE["output_dir"])
        tk.Entry(top, textvariable=self.dir_var, width=48).pack(side="left", padx=4)
        tk.Button(top, text="Browse…", command=self.browse).pack(side="left")

        tk.Label(root, text=f"Listening on http://127.0.0.1:{APP_PORT}  "
                             f"(keep this window open while browsing)",
                 fg="#555").pack(anchor="w", padx=8)

        self.log_box = scrolledtext.ScrolledText(root, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)

        self.root.after(200, self.drain_log)

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)
            STATE["output_dir"] = d

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
        self.root.after(200, self.drain_log)


def main():
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    log(f"Server started on port {APP_PORT}. Saving to {STATE['output_dir']}")

    root = tk.Tk()
    GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
