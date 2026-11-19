"""アプリのフォルダと、その下の置き場所。

EXE にしたときは EXE のあるフォルダ、開発中はリポジトリの直下を基準にする。
"""

import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
PROFILES_DIR = BASE_DIR / "profiles"
TARGET_PROFILES_DIR = PROFILES_DIR / "targets"
LLM_PROFILES_DIR = PROFILES_DIR / "llms"
LIBRARY_DIR = BASE_DIR / "library"
CONFIG_DIR = BASE_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
UI_STATE_FILE = CONFIG_DIR / "ui_state.json"
LOGS_DIR = BASE_DIR / "logs"
# LLM に送る指示文 (コードに直書きしない。SPEC 8 章)
PROMPTS_DIR = Path(__file__).resolve().parent / "core" / "tasks" / "prompts"
