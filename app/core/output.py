"""出力ファイルの置き場所と名前 (SPEC 5.9 / 6 章)。

- 出力フォルダ直下には、生成に使うプロンプト .txt だけを置く (PWC がフォルダの .txt をまとめて生成するため)
- 中間表現 .json・*_neg.txt・*_ja.txt は .muse/ サブフォルダに、プロンプトと同じ名前の元で置く
- ストーリーの動画ペアは video/ サブフォルダ
- 上書きしない: 同じ名前があれば「_2」「_3」… を付ける (付けた名前は .muse のものとそろえる)
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..util import files
from . import intermediate

MUSE_DIR = ".muse"
VIDEO_DIR = "video"


def sidecar_dir(prompt_path: Path) -> Path:
    return prompt_path.parent / MUSE_DIR


def json_path(prompt_path: Path) -> Path:
    return sidecar_dir(prompt_path) / f"{prompt_path.stem}.json"


def negative_path(prompt_path: Path) -> Path:
    return sidecar_dir(prompt_path) / f"{prompt_path.stem}_neg.txt"


def japanese_path(prompt_path: Path) -> Path:
    return sidecar_dir(prompt_path) / f"{prompt_path.stem}_ja.txt"


def _taken(prompt_path: Path) -> bool:
    return any(p.exists() for p in (prompt_path, json_path(prompt_path), negative_path(prompt_path),
                                    japanese_path(prompt_path)))


SERIAL_DIGITS = 4


def next_serial(folder: Path, head: str) -> int:
    """「(頭)_(通し番号)_…」の次の番号。フォルダと .muse にある同じ頭の番号のいちばん大きいもの + 1。
    名前の順に並べると作った順になるように、桁はそろえる (SERIAL_DIGITS 桁)。"""
    head = files.safe_name(head)
    pattern = re.compile(re.escape(head.lower()) + r"_(\d{%d,})(?:_|$)" % SERIAL_DIGITS)
    largest = 0
    for place in (folder, folder / MUSE_DIR):
        try:
            stems = [p.stem.lower() for p in place.iterdir() if p.is_file()]
        except OSError:
            continue
        for stem in stems:
            match = pattern.match(stem)
            if match:
                largest = max(largest, int(match.group(1)))
    return largest + 1


def processed_stem(stem: str, op_suffix: str, model_slug: str = "", known_slugs: tuple = ()) -> str:
    """加工したファイルの名前の元。同じ加工の印 (と重なりよけの番号) を外してから末尾に付け直すので、
    同じ加工を何度かけても名前は伸びない (muse_0001_Krea2_mod.txt を指示で変える → muse_0001_Krea2_mod_2.txt)。

    model_slug: モデル変換・画像→動画で付けるモデルの名前。known_slugs のどれかが付いた同じ加工の印を外す。"""
    stem = files.safe_name(stem)
    mark = re.escape(op_suffix)
    if known_slugs or model_slug:
        slugs = sorted({*known_slugs, model_slug} - {""}, key=len, reverse=True)
        mark += "_(?:" + "|".join(re.escape(s) for s in slugs) + ")"
    stripped = re.sub(rf"_{mark}(?:_\d+)?(?=_|$)", "", stem, flags=re.IGNORECASE) or stem
    return f"{stripped}_{op_suffix}" + (f"_{model_slug}" if model_slug else "")


def serial_stem(head: str, serial: int, rest: str = "") -> str:
    stem = f"{files.safe_name(head)}_{serial:0{SERIAL_DIGITS}d}"
    return f"{stem}_{rest}" if rest else stem


def free_prompt_path(folder: Path, stem: str, reserved: Optional[set] = None) -> Path:
    """folder/stem.txt と .muse の仲間がどれもまだないパス (なければ _2, _3…)。

    reserved: 同じ保存の中ですでに決めたパス (まだ書いていないものとぶつからないように)。
    """
    stem = files.safe_name(stem)
    reserved = reserved if reserved is not None else set()
    candidate = folder / f"{stem}.txt"
    n = 2
    while _taken(candidate) or candidate in reserved:
        candidate = folder / f"{stem}_{n}.txt"
        n += 1
    reserved.add(candidate)
    return candidate


@dataclass
class Saved:
    prompt: Path
    json: Optional[Path] = None
    negative: Optional[Path] = None


def save_prompt(prompt_path: Path, text: str, data: Optional[dict] = None, negative: str = "",
                save_json: bool = True) -> Saved:
    """prompt_path は free_prompt_path() で決めたもの。"""
    files.write_text_atomic(prompt_path, text.strip() + "\n")
    saved = Saved(prompt_path)
    if data is not None and save_json:
        saved.json = json_path(prompt_path)
        files.write_text_atomic(saved.json, intermediate.to_json(data))
    if negative.strip():
        saved.negative = negative_path(prompt_path)
        files.write_text_atomic(saved.negative, negative.strip() + "\n")
    return saved
