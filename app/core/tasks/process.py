"""抽出 (5.2) と加工 (5.3) の LLM の仕事。どれも 1 リクエスト 1 仕事。

- decompose: プロンプト文 → 中間表現 (task extract。元の言葉のまま、温度低め)
- enhance: 空いた・薄い要素を補う (task enhance)
- to_video: 画像の中間表現に motion / camera_motion / timeline を足す (task enhance。秒数で動きの量を変える)
- translate_text: プロンプト文をそのまま英訳 (task translate。記法は残す)
- make_negative: 中間表現に合ったネガティブ (task negative)
"""

import json
from pathlib import Path
from typing import Optional

from .. import intermediate, output, prompts
from ..llm_client import LLMClient
from ...util import files

EXTRACT_TEMPERATURE = 0.2
TRANSLATE_TEMPERATURE = 0.2
ENHANCE_TEMPERATURE = 0.8
NEGATIVE_TEMPERATURE = 0.5


def _fields_json(data: dict, include_video: bool) -> str:
    names = [n for n in intermediate.TEXT_FIELDS if include_video or n not in intermediate.VIDEO_FIELDS]
    return json.dumps({n: data.get(n, "") for n in names}, ensure_ascii=False, indent=1)


def decompose(client: LLMClient, text: str, source: Optional[Path] = None) -> dict:
    raw, _ = client.chat_json(prompts.load("extract_system"), prompts.fill("extract_user", prompt=text.strip()),
                              intermediate.schema(include_video=True), temperature=EXTRACT_TEMPERATURE,
                              purpose=f"extract {source.name if source else ''}".strip())
    base = intermediate.new("image", "extract", files=[str(source)] if source else [])
    data = intermediate.normalize(raw, base)
    if data.get("motion") or data.get("camera_motion"):  # 動きが書いてあれば動画のプロンプト
        data["kind"] = "video"
    data["lang"] = "ja" if files.has_japanese(" ".join(data.get(n, "") for n in intermediate.TEXT_FIELDS)) else "en"
    return data


def existing_intermediate(prompt_path: Path) -> Optional[dict]:
    """MuseFactory が作ったファイルなら .muse/(名前).json の中間表現を返す (読めなければ None)。"""
    path = output.json_path(prompt_path)
    if not path.is_file():
        return None
    try:
        return intermediate.load(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def enhance(client: LLMClient, data: dict) -> dict:
    video = data.get("kind") == "video"
    rule = prompts.sections("generate_levels")["kind_video" if video else "kind_image"]
    raw, _ = client.chat_json(prompts.fill("enhance_system", kind_rule=rule), _fields_json(data, video),
                              intermediate.schema(include_video=video), temperature=ENHANCE_TEMPERATURE,
                              purpose="enhance")
    result = intermediate.normalize(raw, dict(data))
    result["lang"] = "en"
    return result


def amount_for(seconds: float) -> str:
    return "short" if seconds <= 4 else "medium" if seconds <= 8 else "long"


def to_video_schema() -> dict:
    schema = intermediate.schema(include_video=True)
    schema["properties"]["timeline"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"start": {"type": "number"}, "end": {"type": "number"}, "action": {"type": "string"}},
            "required": ["start", "end", "action"],
        },
    }
    schema["required"] = schema["required"] + ["timeline"]
    return schema


def to_video(client: LLMClient, data: dict, seconds: float) -> dict:
    seconds_text = f"{seconds:g}"
    system = prompts.fill("to_video_system", seconds=seconds_text,
                          amount_rule=prompts.sections("to_video_amount")[amount_for(seconds)])
    raw, _ = client.chat_json(system, _fields_json(data, True), to_video_schema(),
                              temperature=ENHANCE_TEMPERATURE, purpose=f"to_video {seconds_text}s")
    result = intermediate.normalize(raw, dict(data))
    result["kind"] = "video"
    # timeline は秒数の中に収める
    result["timeline"] = [
        {"start": min(t["start"], seconds), "end": min(max(t["end"], t["start"]), seconds), "action": t["action"]}
        for t in result.get("timeline", [])
    ]
    return result


def translate_text(client: LLMClient, text: str) -> str:
    from ..renderer import clean_output
    result = client.chat(prompts.load("translate_system"), text.strip(), temperature=TRANSLATE_TEMPERATURE,
                         purpose="translate")
    translated = clean_output(result.text)
    if not translated:
        raise ValueError("訳が空でした")
    return translated


def make_negative(client: LLMClient, data: dict, max_items: int = 15) -> str:
    from ..renderer import clean_output
    result = client.chat(prompts.fill("negative_system", max_items=str(max_items)),
                         _fields_json(data, data.get("kind") == "video"), temperature=NEGATIVE_TEMPERATURE,
                         purpose="negative")
    negative = " ".join(clean_output(result.text).split())
    if not negative:
        raise ValueError("ネガティブが空でした")
    return negative
