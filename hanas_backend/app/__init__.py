"""Shared application package constants."""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
APP_VERSION = "0.3.8"
__version__ = APP_VERSION

__all__ = ["APP_DIR", "PROJECT_DIR", "APP_VERSION", "__version__"]
