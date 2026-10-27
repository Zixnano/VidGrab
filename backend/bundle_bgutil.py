"""Bundle the bgutil PO-token provider into a Video Grabber runtime\\ folder.

    python bundle_pot_provider.py --runtime path\\to\\runtime
    python bundle_pot_provider.py --runtime path\\to\\runtime --verify-only

What it does (needs internet; run it on the machine that builds the release):

  1. Installs the yt-dlp plugin (pure Python, no dependencies) into
     runtime\\site-packages, where launcher.py already puts sys.path.
  2. Downloads the provider SERVER source for the SAME version into
     runtime\\bgutil-pot-server and runs `deno install` there, using the
     runtime's own deno. No Node.js is needed.
  3. Starts the server on a spare port, checks /ping reports the pinned
     version, stops it. Exit code 0 only if that worked.

Plugin and server versions must match; bump PROVIDER_VERSION for both at once
(check PyPI for the newest bgutil-ytdlp-pot-provider). runtime\\ is not touched
by thin app updates, so a bump ships with a full install package.

--verify-only runs just step 3 against an existing folder. Run it on the
UNPACKED release too: node_modules uses links, and a zip tool that doesn't
preserve or follow them will break the server.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PROVIDER_VERSION = "2.0.1"
REPO = "Brainicism/bgutil-ytdlp-pot-provider"
SMOKE_PORT = 4499  # not 4416, so a running app's provider isn't disturbed


def _deno_path(runtime: Path, override: str = None) -> str:
    if override:
        return override
    for name in ("deno.exe", "deno"):
        p = runtime / name
        if p.is_file():
            return str(p)
    found = shutil.which("deno")
    if found:
        return found
    sys.exit("deno not found in the runtime folder or on PATH (use --deno).")


def install_plugin(runtime: Path, version: str, python: str) -> None:
    target = runtime / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] plugin bgutil-ytdlp-pot-provider=={version} -> {target}")
    subprocess.run(
        [python, "-m", "pip", "install", "--target", str(target), "--no-deps",
         "--upgrade", "--disable-pip-version-check",
         f"bgutil-ytdlp-pot-provider=={version}"],
        check=True)
    plugin_dir = target / "yt_dlp_plugins" / "extractor"
    if not (plugin_dir / "getpot_bgutil_http.py").is_file():
        sys.exit(f"plugin files not found under {plugin_dir}")


def install_server(runtime: Path, version: str, deno: str) -> None:
    dest = runtime / "bgutil-pot-server"
    url = f"https://codeload.github.com/{REPO}/zip/refs/tags/{version}"
    print(f"[2/3] server {version}: downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "VideoGrabber-bundler"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(tmp)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        src = roots[0] / "server" if roots else None
        if not src or not (src / "src" / "main.ts").is_file():
            sys.exit("downloaded archive has no server/src/main.ts")
        if dest.exists():
            shutil.rmtree(dest)  # also removes node_modules links without following them
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(
            "node_modules", "Dockerfile", "README.md", "scripts", "eslint.config.mjs"))
    print(f"      deno install (native canvas module) in {dest}")
    subprocess.run([deno, "install", "--allow-scripts=npm:canvas", "--frozen"],
                   cwd=str(dest), check=True,
                   env=dict(os.environ, DENO_NO_UPDATE_CHECK="1"))
    (dest / "bgutil-version.txt").write_text(version + "\n", encoding="utf-8")


def smoke_test(runtime: Path, version: str, deno: str, port: int = SMOKE_PORT) -> None:
    server = runtime / "bgutil-pot-server"
    modules = server / "node_modules"
    if not (server / "src" / "main.ts").is_file() or not modules.is_dir():
        sys.exit(f"{server} is missing src\\main.ts or node_modules; run without --verify-only first.")
    print(f"[3/3] smoke test: starting the server on port {port}")
    cmd = [deno, "run", "--no-prompt", "--allow-env", "--allow-net", "--allow-ffi=.",
           "--allow-read=.", "../src/main.ts", "--port", str(port)]
    proc = subprocess.Popen(cmd, cwd=str(modules), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=dict(os.environ, DENO_NO_UPDATE_CHECK="1"))
    out = []
    try:
        deadline = time.time() + 60
        reply = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
                    reply = json.loads(r.read().decode())
                    break
            except Exception:
                time.sleep(0.5)
        if reply is None:
            proc.terminate()
            try:
                out = proc.communicate(timeout=5)[0].decode("utf-8", "replace").splitlines()
            except Exception:
                pass
            tail = "\n".join(out[-15:])
            sys.exit(f"server did not answer /ping (exit code {proc.poll()}).\n{tail}")
        if reply.get("version") != version:
            sys.exit(f"server reports version {reply.get('version')!r}, expected {version!r}")
        print(f"      ok: /ping -> {reply}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


def folder_size_mb(p: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(p, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runtime", required=True, help="the runtime\\ folder beside VideoGrabber.exe")
    ap.add_argument("--version", default=PROVIDER_VERSION)
    ap.add_argument("--python", default=sys.executable, help="python used for pip (any 3.8+)")
    ap.add_argument("--deno", default=None, help="deno executable (default: runtime\\deno.exe)")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()
    runtime = Path(a.runtime).resolve()
    if not runtime.is_dir():
        sys.exit(f"runtime folder not found: {runtime}")
    deno = _deno_path(runtime, a.deno)
    print(f"runtime: {runtime}\ndeno:    {deno}")
    if not a.verify_only:
        install_plugin(runtime, a.version, a.python)
        install_server(runtime, a.version, deno)
    smoke_test(runtime, a.version, deno)
    print(f"done. bgutil-pot-server is {folder_size_mb(runtime / 'bgutil-pot-server'):.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
