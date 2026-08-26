"""py2app configuration for the thin macOS bundle."""

from __future__ import annotations

from setuptools import setup

APP = ["src/lune/app.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": None,
    "plist": {
        "CFBundleDisplayName": "Lune",
        "CFBundleIdentifier": "dev.lune.voice-companion",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": (
            "Lune needs microphone access only while local voice listening is enabled."
        ),
    },
    "packages": ["lune"],
    "extra_scripts": ["src/lune/engine.py"],
}

setup(
    app=APP,
    name="Lune",
    options={"py2app": OPTIONS},
)
