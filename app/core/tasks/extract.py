"""5.2 抽出の書き出し。分解 (process.decompose) の結果から、選んだ要素だけを出す。

- 要素ごとに 1 ファイル: (元の名前)_extract_(要素).txt
- ワイルドカード形式: 要素ごとに全ファイル分を 1 行 1 項目で (要素).txt。完全に同じ行は 1 つにする
- パーツライブラリに登録
- 削除する要素 (取り出すの反対。2026-11-19 利用者の希望): 元の文から選んだ要素のところだけを LLM に消させ、
  (元の名前)_without_(要素)_(要素).txt に書き出す。言い回しは元の文のまま (組み直さない)
どれも上書きしない (同じ名前があれば _2, _3 …)。
"""

from dataclasses import dataclass
from pathlib import Path

from .. import library, prompts
from ..llm_client import LLMClient
from ...util import files

REMOVED = "_removed"  # 中間表現の dict に入れる、要素を消したあとの文 (書き出し用。.muse には残さない)
REMOVE_TEMPERATURE = 0.2  # 消すだけ: ほかは写す


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


# ---------------------------------------------------------------- 削除する要素

def remove_elements(client: LLMClient, text: str, elements: list[str], found: dict = None) -> str:
    """元の文から elements のところだけを消した文 (ほかの言葉・記法はそのまま)。
    found: 分解で見つかった要素の文 (あれば消すところの手がかりとして渡す)。"""
    from ..renderer import clean_output
    found = found or {}
    lines = []
    for element in elements:
        words = " ".join(str(found.get(element, "")).split())
        lines.append(f'- {element}' + (f': "{words}"' if words else ""))
    result = client.chat(prompts.load("remove_system"),
                         prompts.fill("remove_user", remove="\n".join(lines), prompt=text.strip()),
                         temperature=REMOVE_TEMPERATURE, purpose="remove " + ",".join(elements))
    removed = clean_output(result.text)
    if removed.startswith("Prompt:"):  # 受け取った形のまま返すことがある
        removed = removed[len("Prompt:"):].strip()
    return removed


def removed_name(source: Path, elements: list[str]) -> str:
    return f"{files.safe_name(source.stem)}_without_{'_'.join(elements)}.txt"


def write_removed(results: list[Extracted], elements: list[str], folder: Path) -> list[Path]:
    """要素を消したあとの文を 1 ファイルずつ書き出す (空なら書かない)。"""
    written = []
    for result in results:
        text = str(result.data.get(REMOVED, "")).strip()
        if not text:
            continue
        path = files.unique_path(folder / removed_name(result.source, elements))
        files.write_text_atomic(path, text + "\n")
        written.append(path)
    return written
