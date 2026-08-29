"""py2app configuration for the thin macOS bundle."""

from __future__ import annotations

from py2app.build_app import py2app as Py2AppCommand
from setuptools import setup

APP = ["src/lune/app.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": None,
    "plist": {
        "CFBundleDisplayName": "Lune",
        "CFBundleIdentifier": "dev.lune.voice-companion",
        # Lune is now a normal windowed app: the Dock presence is part of the
        # discoverable multi-thread desktop UI, not a hidden menu-bar utility.
        "LSUIElement": False,
        "NSMicrophoneUsageDescription": ("Lune 只會在你按下「打給 Lune」後使用麥克風。"),
    },
    # `lune` keeps the bundled HTML/CSS/JS package data beside the Python
    # modules; pywebview is dynamically imported by the desktop-only path.
    "packages": ["lune", "webview"],
    "includes": ["webview.platforms.cocoa"],
    "extra_scripts": ["src/lune/engine.py"],
}


class LunePy2AppCommand(Py2AppCommand):  # type: ignore[misc]
    """Keep PEP 621 dependency metadata out of py2app's removed installer hook."""

    def finalize_options(self) -> None:
        self.distribution.install_requires = []
        super().finalize_options()


setup(
    app=APP,
    cmdclass={"py2app": LunePy2AppCommand},
    name="Lune",
    options={"py2app": OPTIONS},
)
