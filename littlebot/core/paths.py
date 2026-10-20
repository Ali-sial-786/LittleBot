"""Well-known filesystem locations."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../littlebot
PROJECT_ROOT = PACKAGE_ROOT.parent  # repository root
CONFIG_DIR = PROJECT_ROOT / "config"
ASSETS_DIR = PROJECT_ROOT / "assets"
SOUNDS_DIR = ASSETS_DIR / "sounds"
IMAGES_DIR = ASSETS_DIR / "images"
ACTIVITY_SCRIPTS_DIR = ASSETS_DIR / "activities"
