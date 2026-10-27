"""Health, version, disk space and stats routes."""

from flask import Blueprint, jsonify

from settings import APP_VERSION, disk_usage_for, STATE
import api

health_bp = Blueprint("routes_health", __name__)


# --------------------------------------------------------------- API ------
@health_bp.route("/health", methods=["GET"])
def health():
    # Supervisor Architecture (contract §3): the identity payload lets
    # the launcher's sentinel check and Recovery's ghost check tell OUR
    # backend apart from any other service on this port. Unauthenticated
    # by design (it reveals nothing but name + version).
    return jsonify({"ok": True, "app": "videograbber", "version": APP_VERSION})

@health_bp.route("/ping", methods=["GET"])
def ping():
    """Liveness + pairing-state probe for the Options 'Test connection'
    button. Unlike /pair this never flips _PAIR_GRANTED, so it can be
    called any number of times per session."""
    # live read: see the note in api.py
    return jsonify({"ok": True, "paired": api._PAIR_GRANTED, "version": APP_VERSION})


@health_bp.route("/version", methods=["GET"])
def version():
    return jsonify({"version": APP_VERSION})

@health_bp.route("/diskspace", methods=["GET"])
def diskspace():
    return jsonify(disk_usage_for())

@health_bp.route("/stats", methods=["GET"])
def stats():
    return jsonify({"samples": STATE.get("download_stats", [])[-600:]})
