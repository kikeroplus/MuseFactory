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


# ---------------------------------------------------------------- 一覧・直す (フェーズ5)

def categories(folder: Optional[Path] = None) -> list[str]:
    """保存のあるカテゴリ (名前順)。"""
    base = folder or PARTS_DIR
    return sorted(p.stem for p in base.glob("*.json")) if base.is_dir() else []


def save(category: str, items: list[dict], folder: Optional[Path] = None) -> None:
    files.write_json(_path(category, folder), items)


def search(items: list[dict], query: str, favorites_only: bool = False) -> list[int]:
    """当てはまる項目の番号。query は空白で区切った語をすべて含むもの (大文字小文字は区別しない)。"""
    words = [w.lower() for w in query.split()]
    out = []
    for i, item in enumerate(items):
        if favorites_only and not item.get("favorite"):
            continue
        text = item.get("text", "").lower()
        if all(w in text for w in words):
            out.append(i)
    return out


def remove(category: str, indexes: list[int], folder: Optional[Path] = None) -> int:
    items = load(category, folder)
    drop = set(indexes)
    kept = [item for i, item in enumerate(items) if i not in drop]
    save(category, kept, folder)
    return len(items) - len(kept)


def set_favorite(category: str, indexes: list[int], favorite: bool, folder: Optional[Path] = None) -> None:
    items = load(category, folder)
    for i in indexes:
        if 0 <= i < len(items):
            items[i]["favorite"] = favorite
    save(category, items, folder)


def export_wildcard(items: list[dict], path: Path) -> Path:
    """1 行 1 項目の .txt。上書きしない (_2, _3 …)。"""
    target = files.unique_path(path)
    files.write_text_atomic(target, "\n".join(i["text"] for i in items) + "\n")
    return target


# ---------------------------------------------------------------- ミックス生成

# カテゴリ → 中間表現の項目 (characters は subject に入れる)
CATEGORY_FIELD = {"characters": "subject", "character": "subject"}


def field_for(category: str) -> str:
    from .intermediate import TEXT_FIELDS
    field = CATEGORY_FIELD.get(category, category)
    return field if field in TEXT_FIELDS else "extra"


def mix(choices: dict[str, Optional[str]], pools: dict[str, list[str]], count: int, seed=None) -> list[dict]:
    """カテゴリごとに「指定 (choices の値)」か「ランダム (None。pools から選ぶ)」で組み合わせ、中間表現を count 本作る。

    LLM は使わない (組み合わせは決まった形。レンダリングだけ LLM)。同じ組み合わせはなるべく重ねない。
    """
    import random
    from . import intermediate
    rng = random.Random(seed)
    results: list[dict] = []
    seen: set = set()
    for _ in range(count):
        for _attempt in range(20):
            picked = {}
            for category, fixed in choices.items():
                pool = pools.get(category) or []
                if fixed:
                    picked[category] = fixed
                elif pool:
                    picked[category] = rng.choice(pool)
            key = tuple(sorted(picked.items()))
            if key not in seen:
                break
        seen.add(key)
        data = intermediate.new("image", "library")
        data["source"]["instruction"] = "mix: " + ", ".join(f"{k}={v}" for k, v in picked.items())
        for category, text in picked.items():
            field = field_for(category)
            data[field] = ", ".join(filter(None, [data.get(field, ""), text]))
        if not data.get("subject"):
            data["subject"] = data.get("extra") or "a person"
        results.append(data)
    return results
