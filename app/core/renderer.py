"""中間表現 + モデルプロファイル → プロンプト文 (SPEC 4.4)。

LLM に渡すもの: プロファイルの system_prompt・example_prompts (お手本)・element_order 順に並べた要素
(skip_elements は除く)・max_length・output_language。結果に prefix / suffix を付ける。
ネガティブは、negative_supported が true で中間表現の negative が空でなければ別に返す (保存は *_neg.txt)。
"""

import re
from dataclasses import dataclass
from typing import Optional

from . import intermediate, prompts
from .llm_client import LLMClient
from .targets import TargetProfile

LANGUAGE_NAMES = {"en": "English", "ja": "Japanese", "zh": "Chinese"}
STYLE_RULES = {
    "natural_sentence": "Write natural, flowing sentences (not a list of tags).",
    "tags": "Write a comma-separated list of short tags (no sentences).",
}


@dataclass
class RenderResult:
    text: str
    negative: str
    count: int  # 単語数 (日本語・中国語は文字数)
    unit: str  # 「語」「文字」
    max_length: int

    @property
    def over_limit(self) -> bool:
        return bool(self.max_length) and self.count > self.max_length


def measure(text: str, language: str) -> tuple[int, str]:
    if language in ("ja", "zh"):
        return len(re.sub(r"\s", "", text)), "文字"
    return len(text.split()), "語"


def ordered_elements(data: dict, target: TargetProfile) -> list[tuple[str, str]]:
    """渡す要素: element_order の順、そのあとに順番の決まっていない残り。skip_elements と空は除く。"""
    skip = set(target.skip_elements) | {"negative"}
    if not target.is_video:
        skip |= set(intermediate.VIDEO_FIELDS)
    names = [n for n in target.element_order if n in intermediate.TEXT_FIELDS]
    names += [n for n in intermediate.TEXT_FIELDS if n not in names]
    items = [(n, data.get(n, "")) for n in names if n not in skip and data.get(n, "")]
    if target.is_video and "timeline" not in skip and data.get("timeline"):
        steps = "; ".join(f"{t['start']:g}-{t['end']:g}s: {t['action']}" for t in data["timeline"])
        items.append(("timeline", steps))
    return items


def build_prompt(data: dict, target: TargetProfile) -> tuple[str, str]:
    """(system, user) を組み立てる。"""
    unit = "characters" if target.output_language in ("ja", "zh") else "words"
    examples = ""
    if target.example_prompts:
        examples = prompts.fill("render_examples",
                                examples="\n".join(f"- {e}" for e in target.example_prompts if e.strip()))
    rules = prompts.fill(
        "render_rules",
        language=LANGUAGE_NAMES.get(target.output_language, target.output_language),
        style_rule=STYLE_RULES.get(target.style, ""),
        max_length=str(target.max_length),
        unit=unit,
        examples=examples,
    )
    system = f"{target.system_prompt.strip()}\n\n{rules}".strip()
    user = "\n".join(f"{name}: {value}" for name, value in ordered_elements(data, target))
    return system, user


_LABEL = re.compile(r"^\s*(?:\*\*)?(?:final\s+)?prompt(?:\*\*)?\s*[:：]\s*", re.IGNORECASE)


def clean_output(text: str) -> str:
    """LLM がつけがちな飾り (コードブロック・「Prompt:」・全体の引用符) を取る。"""
    text = text.strip()
    previous = None
    while text != previous:  # 「"Prompt: …"」のように重なっていることがあるので、変わらなくなるまで
        previous = text
        fence = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        text = _LABEL.sub("", text)
        if len(text) >= 2 and ((text[0] == text[-1] and text[0] in "\"'")
                               or (text[0], text[-1]) in (("“", "”"), ("「", "」"))):
            text = text[1:-1].strip()
    return text


def join_parts(parts: list[str], sep: str) -> str:
    """前後の文をつなぐ。タグ列は「, 」、自然文は空白でつなぐ (prefix / suffix の句読点はそのまま)。"""
    items = [p.strip() for p in parts if p and p.strip()]
    if sep.strip() == ",":
        items = [p.strip(",").strip() for p in items]
    return sep.join(items)


def finish(text: str, data: dict, target: TargetProfile) -> RenderResult:
    """LLM の出力に prefix / suffix を付けて数える。"""
    body = clean_output(text)
    full = join_parts([target.prefix, body, target.suffix], ", " if target.style == "tags" else " ")
    negative = data.get("negative", "") if target.negative_supported else ""
    count, unit = measure(full, target.output_language)
    return RenderResult(full, negative, count, unit, target.max_length)


def render(client: LLMClient, data: dict, target: TargetProfile,
           temperature: Optional[float] = None) -> RenderResult:
    system, user = build_prompt(data, target)
    result = client.chat(system, user, temperature=target.temperature if temperature is None else temperature,
                         purpose=f"render {target.name}")
    if not result.text.strip():
        raise ValueError("LLM の返事が空でした")
    return finish(result.text, data, target)
