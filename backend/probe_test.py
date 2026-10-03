"""Step 1 checkpoint. Run with the app running:
    python probe_test.py <playlist_url> [single_video_url] [bad_playlist_url]
Reads the API token from ~/Downloads/VideoGrabber/settings.json."""
import json, sys, time
from pathlib import Path
import requests

tok = json.loads((Path.home() / "Downloads/VideoGrabber/settings.json").read_text())["api_token"]
H = {"X-API-Token": tok}

def probe(**body):
    t = time.time()
    r = requests.post("http://127.0.0.1:5757/probe", json=body, headers=H, timeout=180)
    j = r.json()
    print(f"  HTTP {r.status_code} in {time.time()-t:.1f}s")
    return j

def show(j):
    if "error" in j:
        print("  error:", j.get("code"), "|", j["error"][:150]); return
    pl = j["playlist"]
    print("  is_playlist:", j["is_playlist"], "| playlist:", pl and (pl["id"], pl["title"], pl["total"]))
    print("  start/limit/truncated:", j["start"], j["limit"], j["truncated"], "| items:", len(j["items"]))
    for it in j["items"][:3] + j["items"][-2:]:
        print("   ", it["index"], it["id"], it["available"], it["duration"], it["title"][:50])
    idx = [i["index"] for i in j["items"]]
    print("  indexes ascending, no repeats:", idx == sorted(set(idx)))

a = sys.argv[1:]
print("1) playlist, default page"); show(probe(url=a[0]))
print("2) playlist, start=5 limit=3"); show(probe(url=a[0], start=5, limit=3))
if len(a) > 1:
    print("3) single video"); show(probe(url=a[1]))
if len(a) > 2:
    print("4) bad/private/deleted playlist"); show(probe(url=a[2]))
print("5) not a URL"); show(probe(url="hello"))
print("6) bad params"); show(probe(url=a[0], start="1"))
