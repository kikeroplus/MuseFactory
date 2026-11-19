"""中間表現 (SPEC 3.1)。すべての機能はこの形を通る。

小さいモデルでも崩れにくいよう、入れ子は浅く、値は短い文字列にする。
LLM から受け取ったものは normalize() で形をそろえる (足りない項目は空文字、知らない項目は extra へ)。
"""

import json
import uuid
from typing import Any, Optional

# 文字列の要素 (並びは画面とスキーマの順)
TEXT_FIELDS = ["subject", "pose", "expression", "outfit", "background", "lighting", "camera", "style",
               "motion", "camera_motion", "extra", "negative"]
# 抽出・ライブラリで扱う要素 (extra / negative は除く)
ELEMENTS = TEXT_FIELDS[:-2]
VIDEO_FIELDS = ["motion", "camera_motion", "timeline"]
KINDS = ("image", "video")
# 中身ではない項目 (normalize で extra に入れない)。target は保存したときのモデルプロファイル名
META_FIELDS = ("id", "kind", "source", "lang", "target")


def new(kind: str = "image", source_type: str = "generate", files: Optional[list[str]] = None,
        instruction: str = "") -> dict:
    data: dict[str, Any] = {"id": str(uuid.uuid4()), "kind": kind if kind in KINDS else "image"}
    for name in TEXT_FIELDS:
        data[name] = ""
    data["timeline"] = []
    data["source"] = {"type": source_type, "files": list(files or []), "instruction": instruction}
    data["lang"] = "en"
    return data


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value if _text(v))
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_text(v)}" for k, v in value.items() if _text(v))
    return " ".join(str(value).split())


def _timeline(value: Any) -> list[dict]:
    items = []
    if not isinstance(value, list):
        return items
    for entry in value:
        if not isinstance(entry, dict):
            continue
        action = _text(entry.get("action"))
        if not action:
            continue
        try:
            start = float(entry.get("start", 0) or 0)
            end = float(entry.get("end", start) or start)
        except (TypeError, ValueError):
            start = end = 0.0
        items.append({"start": start, "end": end, "action": action})
    return items


def normalize(raw: Any, base: Optional[dict] = None) -> dict:
    """LLM の返事 (dict) を中間表現にする。base があれば id・kind・source などを引き継ぐ。"""
    data = dict(base) if base else new()
    if not isinstance(raw, dict):
        raise ValueError("中間表現が JSON のオブジェクトではありません")
    extras = []
    for key, value in raw.items():
        if key in TEXT_FIELDS:
            data[key] = _text(value)
        elif key == "timeline":
            data["timeline"] = _timeline(value)
        elif key == "kind" and value in KINDS and not base:
            data["kind"] = value
        elif key in META_FIELDS:
            continue
        elif _text(value):
            extras.append(f"{key}: {_text(value)}")
    if extras:
        data["extra"] = ", ".join(filter(None, [data.get("extra", "")] + extras))
    for name in TEXT_FIELDS:
        data.setdefault(name, "")
    data.setdefault("timeline", [])
    if not data.get("subject"):
        raise ValueError("subject が空です")
    return data


def schema(include_video: bool = True) -> dict:
    """LLM に渡す JSON スキーマ (浅い。値はすべて文字列)。"""
    names = [n for n in TEXT_FIELDS if include_video or n not in VIDEO_FIELDS]
    return {
        "type": "object",
        "properties": {n: {"type": "string"} for n in names},
        "required": names,
        "additionalProperties": False,
    }


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def load(text: str) -> dict:
    raw = json.loads(text)
    base = new()
    if isinstance(raw, dict):
        for key in META_FIELDS:
            if key in raw:
                base[key] = raw[key]
    return normalize(raw, base)
