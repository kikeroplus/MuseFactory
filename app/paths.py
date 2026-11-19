"""アプリのフォルダと、その下の置き場所。

開発中はリポジトリの直下を基準にする。
EXE にしたときは「EXE のあるフォルダ\\MuseFactory」を基準にする (Desktop\\Application には
ほかのアプリの EXE や設定も並ぶので、config / profiles / library / logs をそこにまとめる)。
初めて起動したときは、EXE に入れておいた既定のプロファイルなどをそこに写す。
"""

import shutil
import sys
from pathlib import Path

APP_NAME = "MuseFactory"
FROZEN = bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    if FROZEN:
        return Path(sys.executable).resolve().parent / APP_NAME
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
# LLM に送る指示文 (コードに直書きしない。SPEC 8 章)。EXE では中に入れておいたものを使う
PROMPTS_DIR = Path(__file__).resolve().parent / "core" / "tasks" / "prompts"
# EXE に入れておく既定のプロファイル (spec の datas で defaults/profiles に置く)
DEFAULTS_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "defaults"


def ensure_data_dirs() -> None:
    """EXE で初めて起動したとき、既定のプロファイルを写す。あるファイルは上書きしない。"""
    if not FROZEN:
        return
    source = DEFAULTS_DIR / "profiles"
    if source.is_dir():
        for path in source.rglob("*.json"):
            dest = PROFILES_DIR / path.relative_to(source)
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
    for folder in (CONFIG_DIR, LIBRARY_DIR, LOGS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
