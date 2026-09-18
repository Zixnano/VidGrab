"""Shared fixtures: stub heavy deps so backend modules import anywhere."""
import sys
import types

import pytest


def _mk(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


@pytest.fixture(autouse=True, scope="session")
def stub_heavy_deps():
    _mk("yt_dlp", YoutubeDL=object)
    _mk("logging_setup", log=lambda *a, **k: None, LOG_QUEUE=[])
    yield
