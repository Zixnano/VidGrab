"""Local-only Video Grabber companion subsystem.

Importing this package pulls in no Qt and starts no threads. The desktop app calls
companion.integration.install(main_window); the Flask routes live in companion.api_routes.
"""
from .core import CompanionCore

__all__ = ["CompanionCore"]
