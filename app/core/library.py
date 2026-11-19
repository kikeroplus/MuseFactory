"""パーツライブラリ (SPEC 5.5) の保存。画面はフェーズ5で作る (フェーズ3は抽出からの登録だけ)。

library/parts/(カテゴリ).json に、項目ごとに テキスト・元ファイル・登録日・お気に入り を持つ。
同じテキストは 2 度登録しない。
"""

import datetime
from pathlib import Path
from typing import Optional

from .. import paths
from ..util import files

PARTS_DIR = paths.LIBRARY_DIR / "parts"


def _path(category: str, folder: Optional[Path] = None) -> Path:
    return (folder or PARTS_DIR) / f"{files.safe_name(category)}.json"


def load(category: str, folder: Optional[Path] = None) -> list[dict]:
    items = files.read_json(_path(category, folder), [])
    return [i for i in items if isinstance(i, dict) and i.get("text")] if isinstance(items, list) else []


def add(category: str, entries: list[tuple[str, str]], folder: Optional[Path] = None) -> int:
    """(テキスト, 元ファイル) を足す。戻り値は足した数 (重なりは数えない)。"""
    items = load(category, folder)
    known = {i["text"] for i in items}
    today = datetime.date.today().isoformat()
    added = 0
    for text, source in entries:
        text = " ".join(text.split())
        if not text or text in known:
            continue
        items.append({"text": text, "source": source, "added": today, "favorite": False})
        known.add(text)
        added += 1
    if added:
        files.write_json(_path(category, folder), items)
    return added
