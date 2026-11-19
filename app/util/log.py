"""logs/ に日付ごとのログを書く (SPEC 6 章)。

LLM へのリクエストの要約・応答時間・エラーを残す。書けなくてもアプリは止めない。
"""

import datetime
import threading
from pathlib import Path
from typing import Optional

from .. import paths

_lock = threading.Lock()
_dir_override: Optional[Path] = None


def set_dir(folder: Optional[Path]) -> None:
    """テスト用: 書き込み先を変える (None で既定に戻す)。"""
    global _dir_override
    _dir_override = folder


def _log_dir() -> Path:
    return _dir_override or paths.LOGS_DIR


def summarize(text: str, limit: int = 120) -> str:
    """ログ用に 1 行へ縮める。"""
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def write(kind: str, message: str) -> None:
    now = datetime.datetime.now()
    line = f"{now:%H:%M:%S} [{kind}] {message}\n"
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with _lock, open(folder / f"{now:%Y-%m-%d}.log", "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def info(message: str) -> None:
    write("INFO", message)


def error(message: str) -> None:
    write("ERROR", message)
