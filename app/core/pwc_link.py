"""PromptWordCrafter (PWC) で開く (SPEC 5.9)。

PWC → PrismLab と同じ送り方:
PWC の 2 重起動を止めるパイプ (PromptWordCrafter-(ユーザー名)) に 1 行の JSON を書き、PWC が読み終えて切るのを待つ。
動いていなければ JSON を一時フォルダに書いて「PromptWordCrafter.exe --open (JSON のパス)」で起動する。
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .. import paths

APP_NAME = "PromptWordCrafter"
SENDER = "MuseFactory"
SERVER_NAME = f"{APP_NAME}-{getpass.getuser()}"
OPEN_ARG = "--open"


def message(folder: Path, select: list[Path]) -> dict:
    return {"cmd": "open", "from": SENDER, "folder": str(folder), "select": [str(p) for p in select]}


def find_command() -> Optional[list[str]]:
    """PWC を起動するコマンド (デスクトップの Application、なければ隣の PWC の dist、開発中は main.py)。"""
    candidates = [Path.home() / "Desktop" / "Application" / f"{APP_NAME}.exe"]
    for parent in [paths.BASE_DIR, *paths.BASE_DIR.parents][:3]:
        candidates.append(parent / APP_NAME / "dist" / f"{APP_NAME}.exe")
    exe = next((p for p in candidates if p.is_file()), None)
    if exe is not None:
        return [str(exe)]
    if not getattr(sys, "frozen", False):
        for parent in [paths.BASE_DIR, *paths.BASE_DIR.parents][:3]:
            script = parent / APP_NAME / "main.py"
            if script.is_file():
                return [sys.executable, str(script)]
    return None


def send(data: dict) -> str:
    """送る。うまくいけば空、だめなら理由。Qt のイベントループの中 (画面のスレッド) で呼ぶ。"""
    from PySide6.QtNetwork import QLocalSocket

    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if sys.platform == "win32":
        # PWC が前に出るのを Windows に拒まれないよう、こちらから許可しておく
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(700):
        sock.write(raw + b"\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        if not sock.waitForDisconnected(3000):
            sock.abort()
            return "PromptWordCrafter が受け取りませんでした (応答がありません)。"
        return ""
    command = find_command()
    if command is None:
        return "PromptWordCrafter が見つかりません (デスクトップの Application に PromptWordCrafter.exe を置いてください)。"
    try:
        fd, temp = tempfile.mkstemp(prefix="pwc_open_", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        cwd = str(Path(command[-1]).parent)
        subprocess.Popen(command + [OPEN_ARG, temp], cwd=cwd)  # noqa: S603 - 決まった PWC に渡す
    except OSError as exc:
        return f"PromptWordCrafter を起動できません: {exc}"
    return ""


def open_in_pwc(folder: Path, select: Optional[list[Path]] = None) -> str:
    return send(message(folder, list(select or [])))
