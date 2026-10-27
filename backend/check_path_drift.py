"""Read-only: list jobs whose GUI path changes now that gui_qt._path_for
calls settings._dest_for. Usage: python check_path_drift.py [jobs.json] [settings.json]"""
import json
import sys
from pathlib import Path

import settings


def old_path_for(job, st):
    """The pre-change gui_qt._path_for body, kept here for comparison only."""
    d = job.get("dest")
    if d:
        try:
            if Path(d).exists():
                return Path(d)
        except (OSError, ValueError):
            pass
    category = job.get("category", "Other")
    override = st.get("per_category_dirs", {}).get(category)
    base = Path(override) if override else Path(st.get("output_dir", str(settings.HOME)))
    cat_dir = base if override else base / category
    sub = job.get("subdir")
    if sub:
        for ch in '<>:"/\\|?*':
            sub = sub.replace(ch, "_")
        cat_dir = cat_dir / sub
    name = job["filename"]
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return cat_dir / name


def new_path_for(job):
    d = job.get("dest")
    if d:
        try:
            if Path(d).exists():
                return Path(d)
        except (OSError, ValueError):
            pass
    return settings._dest_for(job)


jobs_file = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.JOBS_PATH
cfg_file = Path(sys.argv[2]) if len(sys.argv) > 2 else settings.CONFIG_PATH
if cfg_file.exists():
    settings.STATE.update(settings._read_settings_json(cfg_file))
jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
changed = 0
for jid, j in jobs.items():
    try:
        old, new = old_path_for(j, settings.STATE), new_path_for(j)
    except Exception as e:
        print(f"job {jid}: ERROR {type(e).__name__}: {e}")
        changed += 1
        continue
    if old != new:
        changed += 1
        print(f"job {jid} [{j.get('status')}] old_exists={old.exists()} new_exists={new.exists()}")
        print(f"   old: {old}\n   new: {new}")
print(f"{changed} of {len(jobs)} jobs change path")
