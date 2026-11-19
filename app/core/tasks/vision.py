"""5.8 画像からプロンプト: 画像を見せて中間表現を作る (task image_to_prompt。supports_vision の LLM)。"""

import base64
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

from .. import intermediate, prompts
from ..llm_client import LLMClient

MAX_SIDE = 1024  # 送る画像の長い辺 (8GB VRAM の LLM に重すぎないように)


def image_data_url(path: Path, max_side: int = MAX_SIDE) -> str:
    """画像を縮めて JPEG にし、data URL にする。読めなければ ValueError。"""
    image = QImage(str(path))
    if image.isNull():
        raise ValueError(f"画像を読めません: {path.name}")
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.convertToFormat(QImage.Format.Format_RGB888).save(buffer, "JPEG", 90)
    return "data:image/jpeg;base64," + base64.b64encode(bytes(data)).decode("ascii")


def image_to_intermediate(client: LLMClient, path: Path) -> dict:
    if not client.profile.supports_vision:
        raise ValueError(f"{client.profile.name} は画像を見られません (supports_vision が false)")
    raw, _ = client.chat_json(prompts.load("image_system"), "Describe this image.",
                              intermediate.schema(include_video=False), temperature=0.3,
                              image_url=image_data_url(path), purpose=f"image {path.name}")
    base = intermediate.new("image", "image", files=[str(path)])
    return intermediate.normalize(raw, base)
