"""生成した PNG に埋め込まれたプロンプトを読む (SPEC 5.2「ComfyUI で生成した PNG のメタデータ」)。

- ComfyUI: テキストチャンク "prompt" に API 形式のグラフ (JSON)。文字を持つノード (CLIPTextEncode の text、
  プロンプト用の文字列ノードなど) のうち、タイトルやつながりからネガティブと分かるものを除いた文
- A1111 / Forge: "parameters" の「Negative prompt:」より前
外部のライブラリは使わず、PNG のチャンクを直接読む (tEXt / zTXt / iTXt)。
"""

import json
import struct
import zlib
from pathlib import Path
from typing import Optional

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TEXT_KEYS = ("text", "prompt", "string", "value", "text_g", "text_l", "positive")


def text_chunks(path: Path) -> dict[str, str]:
    """PNG のテキストチャンクを {キー: 文字} で返す。PNG でなければ空。"""
    chunks: dict[str, str] = {}
    with open(path, "rb") as f:
        if f.read(8) != PNG_SIGNATURE:
            return chunks
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            length, kind = struct.unpack(">I4s", head)
            data = f.read(length)
            f.read(4)  # CRC
            try:
                if kind == b"tEXt":
                    key, _, value = data.partition(b"\x00")
                    chunks[key.decode("latin-1")] = value.decode("latin-1")
                elif kind == b"zTXt":
                    key, _, rest = data.partition(b"\x00")
                    chunks[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
                elif kind == b"iTXt":
                    key, _, rest = data.partition(b"\x00")
                    compressed, _method = rest[0], rest[1]
                    _lang, _, rest = rest[2:].partition(b"\x00")
                    _tkey, _, value = rest.partition(b"\x00")
                    if compressed:
                        value = zlib.decompress(value)
                    chunks[key.decode("latin-1")] = value.decode("utf-8", "replace")
            except (zlib.error, IndexError, UnicodeDecodeError):
                continue
            if kind == b"IEND":
                break
    # tEXt は latin-1 と決まっているが、ComfyUI などは UTF-8 のまま書くので読み直す
    for key, value in list(chunks.items()):
        try:
            chunks[key] = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return chunks


def _is_negative(node_id: str, node: dict, graph: dict) -> bool:
    title = str((node.get("_meta") or {}).get("title") or "").lower()
    if "negative" in title or "ネガ" in title:
        return True
    # KSampler などの negative 入力につながっているか
    for other in graph.values():
        if not isinstance(other, dict):
            continue
        neg = (other.get("inputs") or {}).get("negative")
        if isinstance(neg, list) and neg and str(neg[0]) == str(node_id):
            return True
    return False


def comfy_prompt(graph: dict) -> str:
    """API 形式のグラフから、ポジティブのプロンプトらしい文を取り出す (長いものを優先)。"""
    texts: list[str] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict) or _is_negative(node_id, node, graph):
            continue
        for key in TEXT_KEYS:
            value = (node.get("inputs") or {}).get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in texts:
                texts.append(value.strip())
    texts.sort(key=len, reverse=True)
    return texts[0] if texts else ""


def prompt_text(path: Path) -> Optional[str]:
    """PNG から読めたプロンプト (なければ None)。"""
    chunks = text_chunks(path)
    if "prompt" in chunks:
        try:
            graph = json.loads(chunks["prompt"])
        except ValueError:
            graph = None
        if isinstance(graph, dict):
            text = comfy_prompt(graph)
            if text:
                return text
    if "parameters" in chunks:
        text = chunks["parameters"].split("Negative prompt:")[0].strip()
        if text:
            return text
    return None
