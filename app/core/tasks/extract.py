"""5.2 抽出の書き出し。分解 (process.decompose) の結果から、選んだ要素だけを出す。

- 要素ごとに 1 ファイル: (元の名前)_extract_(要素).txt
- ワイルドカード形式: 要素ごとに全ファイル分を 1 行 1 項目で (要素).txt。完全に同じ行は 1 つにする
- パーツライブラリに登録
どれも上書きしない (同じ名前があれば _2, _3 …)。
"""

from dataclasses import dataclass
from pathlib import Path

from .. import library
from ...util import files


@dataclass
class Extracted:
    source: Path
    data: dict


def values(results: list[Extracted], element: str) -> list[tuple[str, Path]]:
    return [(" ".join(r.data.get(element, "").split()), r.source) for r in results if r.data.get(element, "").strip()]


def write_per_element(results: list[Extracted], elements: list[str], folder: Path) -> list[Path]:
    written = []
    for result in results:
        for element in elements:
            text = " ".join(result.data.get(element, "").split())
            if not text:
                continue
            path = files.unique_path(folder / f"{files.safe_name(result.source.stem)}_extract_{element}.txt")
            files.write_text_atomic(path, text + "\n")
            written.append(path)
    return written


def write_wildcards(results: list[Extracted], elements: list[str], folder: Path) -> list[Path]:
    written = []
    for element in elements:
        lines: list[str] = []
        for text, _source in values(results, element):
            if text not in lines:
                lines.append(text)
        if not lines:
            continue
        path = files.unique_path(folder / f"{element}.txt")
        files.write_text_atomic(path, "\n".join(lines) + "\n")
        written.append(path)
    return written


def add_to_library(results: list[Extracted], elements: list[str], parts_folder=None) -> dict[str, int]:
    counts = {}
    for element in elements:
        entries = [(text, str(source)) for text, source in values(results, element)]
        if entries:
            counts[element] = library.add(element, entries, parts_folder)
    return counts
