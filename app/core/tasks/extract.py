"""5.2 抽出の書き出し。分解 (process.decompose) の結果から、選んだ要素だけを出す。

- 要素ごとに 1 ファイル: (元の名前)_extract_(要素).txt
- 元のファイルごとに 1 ファイル: (元の名前)_extract_(要素)_(要素).txt に、選んだ要素を ", " でつないだ 1 行
- ワイルドカード形式: 要素ごとに全ファイル分を 1 行 1 項目で (要素).txt。完全に同じ行は 1 つにする
- パーツライブラリに登録
- 削除する要素 (取り出すの反対。2026-11-19 利用者の希望): 元の文から選んだ要素のところだけを消し
  (LLM は消す語句を書き抜くだけ。消すのはこちら)、
  (元の名前)_without_(要素)_(要素).txt に書き出す。言い回しは元の文のまま (組み直さない)
どれも上書きしない (同じ名前があれば _2, _3 …)。
"""

import re
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


def per_source_name(source: Path, elements: list[str]) -> str:
    return f"{files.safe_name(source.stem)}_extract_{'_'.join(elements)}.txt"


def write_per_source(results: list[Extracted], elements: list[str], folder: Path) -> list[Path]:
    """元のファイルごとに 1 ファイル。選んだ要素の値を要素の順に ", " でつないだ 1 行 (空の要素は飛ばす。
    全部空なら書かない)。2026-11-20 利用者の希望。"""
    written = []
    for result in results:
        parts = [" ".join(result.data.get(element, "").split()) for element in elements]
        text = ", ".join(p for p in parts if p)
        if not text:
            continue
        path = files.unique_path(folder / per_source_name(result.source, elements))
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

REMOVE_SCHEMA = {
    "type": "object",
    "properties": {"phrases": {"type": "array", "items": {"type": "string"}}},
    "required": ["phrases"],
    "additionalProperties": False,
}
_SEPARATORS = ",、，;"
_JOINERS = r"(?:and|with|in|on|at|of|while|wearing|holding|by|under|near|behind|from|&)"


def remove_elements(client: LLMClient, text: str, elements: list[str], found: dict = None) -> str:
    """元の文から elements のところだけを消した文 (ほかの言葉・記法はそのまま)。
    found: 分解で見つかった要素の文 (あれば消すところの手がかりとして渡す)。

    小さいモデルは「全文を写しながら一部を消す」が苦手で、元の文をそのまま返してしまう
    (2026-11-20 Qwen 9B)。そこで LLM には消す語句を元の文から書き抜かせ、消すのはこちらでやる。"""
    text = text.strip()
    found = found or {}
    phrases: list[str] = []
    for element in elements:  # まとめて頼むと取りこぼす・取り違えるので 1 要素ずつ
        words = " ".join(str(found.get(element, "")).split())
        line = f'- {element}' + (f': "{words}"' if words else "")
        phrases += [w.strip() for w in re.split(r"[,、，;\n]", str(found.get(element, ""))) if w.strip()]
        data, _ = client.chat_json(prompts.load("remove_system"),
                                   prompts.fill("remove_user", remove=line, prompt=text),
                                   REMOVE_SCHEMA, temperature=REMOVE_TEMPERATURE, purpose="remove " + element)
        listed = data.get("phrases", []) if isinstance(data, dict) else []
        phrases += [p for p in listed if isinstance(p, str)]
    return delete_phrases(text, phrases)


def delete_phrases(text: str, phrases: list[str]) -> str:
    """text から phrases を消し、消したあとに残る区切り・つなぎの言葉を片づける。
    元の文に見つからない語句 (書き換えて返してきたもの) は消さない。"""
    spans = []
    for phrase in phrases:
        phrase = phrase.strip().strip(_SEPARATORS + " ").strip()
        if not phrase:
            continue
        pattern = re.escape(phrase)
        if re.match(r"\w", phrase[0]) and phrase[0].isascii():
            pattern = r"(?<![\w])" + pattern  # 単語の途中では消さない (英字)
        if re.match(r"\w", phrase[-1]) and phrase[-1].isascii():
            pattern += r"(?![\w])"
        spans += [m.span() for m in re.finditer(pattern, text, flags=re.IGNORECASE)]
    if not spans:
        return text
    keep = [True] * len(text)
    for start, end in spans:
        for i in range(start, end):
            keep[i] = False
    out = "".join(ch if k else "\0" for ch, k in zip(text, keep))
    return "\n".join(_tidy_line(line) for line in out.split("\n")).strip()


def _tidy_line(line: str) -> str:
    if "\0" not in line:
        return line
    pieces = re.split(r"([,、，;])", line)
    kept: list[tuple[str, str]] = []  # (項目, その後ろの区切り)
    for i in range(0, len(pieces), 2):
        piece = pieces[i]
        sep = pieces[i + 1] if i + 1 < len(pieces) else ""
        if "\0" in piece:
            piece = re.sub(r"\0+", lambda m: _gap(piece, m), piece)
            piece = re.sub(r"[(\[{]\s*(?::\s*[\d.]+)?\s*[)\]}]", " ", piece)  # 中身が消えた (…:1.2)
            piece = re.sub(r"\s+", " ", piece).strip()
            piece = re.sub(rf"^(?:{_JOINERS}\s+)+|(?:\s+{_JOINERS})+$", "", piece, flags=re.IGNORECASE).strip()
            piece = re.sub(rf"\s+{_JOINERS}\s+(?={_JOINERS}\b)", " ", piece, flags=re.IGNORECASE)
            piece = piece.strip(" -:")
        if piece.strip():
            kept.append((piece, sep))
    if not kept:
        return ""
    result = ""
    for i, (piece, sep) in enumerate(kept):
        if i > 0:
            prev_sep = kept[i - 1][1] or ","
            result += prev_sep + ("" if prev_sep in "、，" else " ")
            piece = piece.lstrip()
        result += piece if i == 0 else piece.strip()
    tail = line.rstrip()
    if tail and tail[-1] in _SEPARATORS:  # 行末の区切りは元のまま残す
        result = result.rstrip() + tail[-1]
    return result.rstrip()


def _gap(piece: str, match: re.Match) -> str:
    """消したところを詰める。前後が英字などなら空白 1 つ、日本語どうしなら詰める。"""
    before = piece[match.start() - 1] if match.start() > 0 else ""
    after = piece[match.end()] if match.end() < len(piece) else ""
    if (before and not before.isascii()) or (after and not after.isascii()):
        return ""
    return " "


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
