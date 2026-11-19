"""5.1 ざっくり生成: 日本語の指示 → 中間表現 (本数ぶん、バリエーションをつける)。

1 リクエストで 1 本 (小さいモデルで崩れないように)。前に作ったものの subject を渡して、違うものにさせる。
おまかせ度 (0〜100) は temperature と「指示にない要素をどこまで補うか」の指示文に反映する。
"""

import re
from functools import lru_cache

from .. import intermediate, prompts
from ..llm_client import LLMClient


@lru_cache(maxsize=None)
def _levels() -> dict[str, str]:
    text = prompts.load("generate_levels")
    parts = re.split(r"^\[(\w+)\]\s*$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def temperature_for(omakase: int) -> float:
    """おまかせ度 0 → 0.3、100 → 0.95。"""
    omakase = max(0, min(100, int(omakase)))
    return round(0.3 + 0.65 * omakase / 100, 2)


def fill_level(omakase: int) -> str:
    return "fill_low" if omakase < 34 else "fill_mid" if omakase < 67 else "fill_high"


def build_prompt(instruction: str, kind: str, omakase: int, index: int, count: int,
                 previous: list[str]) -> tuple[str, str]:
    levels = _levels()
    system = prompts.fill("generate_system",
                          kind_rule=levels["kind_video" if kind == "video" else "kind_image"],
                          fill_rule=levels[fill_level(omakase)])
    avoid = ""
    if previous:
        avoid = levels["avoid"].replace("{previous}", " / ".join(previous[-5:]))
    user = prompts.fill("generate_user", instruction=instruction.strip(), index=str(index), count=str(count),
                        avoid=avoid)
    return system, user


def generate_one(client: LLMClient, instruction: str, kind: str, omakase: int, index: int, count: int,
                 previous: list[str]) -> dict:
    system, user = build_prompt(instruction, kind, omakase, index, count, previous)
    raw, _ = client.chat_json(system, user, intermediate.schema(include_video=(kind == "video")),
                              temperature=temperature_for(omakase), purpose=f"generate {index}/{count}")
    base = intermediate.new(kind, "generate", instruction=instruction)
    return intermediate.normalize(raw, base)
