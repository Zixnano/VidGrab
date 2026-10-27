"""Settings, theme and log routes."""

import queue

from flask import Blueprint, jsonify, request

from logging_setup import log, LOG_QUEUE
from settings import save_settings, STATE

settings_bp = Blueprint("routes_settings", __name__)


@settings_bp.route("/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True, silent=True) or {}
    for k, v in data.items():
        if k == "queue_running":
            # Deliberately ignored here (see /queue-running); logged so the
            # log shows if anything tries to flip it through this route.
            log("settings: ignoring queue_running write (use /queue-running)")
        if k in ("api_token", "queue_running") or k not in STATE:
            continue
        # Dict-valued settings (e.g. per_category_dirs) are merged key-by-key
        # rather than replaced outright, so setting one category's override
        # (as the "Remember this path" checkbox does) doesn't wipe the rest.
        if isinstance(STATE[k], dict) and isinstance(v, dict):
            STATE[k].update(v)
        else:
            STATE[k] = v
    save_settings()
    return jsonify({"ok": True})

@settings_bp.route("/theme", methods=["GET"])
def theme():
    """Resolved color tokens, so the extension popup, options page and
    in-page pill can follow the app's Theme settings."""
    from palette import resolve_palette
    pal = resolve_palette(STATE)
    keys = ("bg_base", "bg_panel", "bg_elevated", "bg_sel", "bg_hover", "border",
            "border_hi", "text", "text_soft", "text_muted", "text_dim", "accent",
            "accent_dim", "text_on_accent", "success", "warning", "error", "info",
            "radius", "font", "font_mono")
    return jsonify({k: pal[k] for k in keys if k in pal})

@settings_bp.route("/logs", methods=["GET"])
def logs_endpoint():
    lines = []
    while not LOG_QUEUE.empty():
        try:
            lines.append(LOG_QUEUE.get_nowait())
        except queue.Empty:
            break
    return jsonify({"logs": lines[-200:]})
