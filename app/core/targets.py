"""モデルプロファイル (SPEC 3.2): 画像・動画生成モデルごとの書き方。"""

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional


@dataclass
class TargetProfile:
    name: str
    type: str = "image"  # image / video
    mode: str = "t2i"  # t2i / t2v / i2v / edit など
    output_language: str = "en"
    style: str = "natural_sentence"  # natural_sentence / tags
    max_length: int = 120  # 単語数の目安 (output_language が ja なら文字数)
    negative_supported: bool = False
    element_order: list[str] = field(default_factory=list)
    skip_elements: list[str] = field(default_factory=list)
    prefix: str = ""
    suffix: str = ""
    system_prompt: str = ""
    example_prompts: list[str] = field(default_factory=list)
    temperature: float = 0.7
    reference_image_mode: bool = False
    source: Optional[Path] = field(default=None, compare=False, repr=False)

    @property
    def is_video(self) -> bool:
        return self.type == "video"

    @property
    def slug(self) -> str:
        """ファイル名に入れる短い名前 (空白・記号を除く)。"""
        from ..util.files import safe_name
        return safe_name(self.name.replace(" ", "").replace("/", "-"), "model")

    @classmethod
    def from_dict(cls, data: dict, source: Optional[Path] = None) -> "TargetProfile":
        known = {f.name for f in fields(cls)} - {"source"}
        values = {k: v for k, v in data.items() if k in known}
        if not values.get("name"):
            raise ValueError("name がありません")
        for key in ("element_order", "skip_elements", "example_prompts"):
            if key in values and not isinstance(values[key], list):
                raise ValueError(f"{key} はリストにしてください")
        profile = cls(**values)
        profile.source = source
        return profile
