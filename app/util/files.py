"""ファイル名と書き込み (SPEC 6 章: 上書き禁止・UTF-8 BOM なし)。"""

import json
import os
import re
from pathlib import Path

_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str, fallback: str = "untitled") -> str:
    """ファイル名に使えない文字を「_」に置き換える。末尾の空白・ピリオドも除く。"""
    text = _INVALID.sub("_", name).strip().rstrip(". ")
    return text or fallback


def unique_path(path: Path) -> Path:
    """同名があれば「_2」「_3」… を付けた、まだないパスを返す。"""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def write_text_atomic(path: Path, text: str) -> None:
    """UTF-8 (BOM なし) で、別名に書いてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(temp, path)


def read_json(path: Path, default=None):
    """JSON を読む。ないとき・壊れているときは default (エラーにしない)。"""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "utf-16", "latin-1")  # PWC と同じ順


def read_text(path: Path) -> str:
    """利用者のテキストを読む (PWC と同じ順で文字コードを試す)。"""
    raw = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def has_japanese(text: str) -> bool:
    return any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in text)
