"""app/core/tasks/prompts/*.txt に分けた指示文を読む (SPEC 8 章: コードに直書きしない)。"""

from functools import lru_cache

from .. import paths


@lru_cache(maxsize=None)
def load(name: str) -> str:
    return (paths.PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8-sig").strip()


def fill(name: str, **values: str) -> str:
    """{key} を置き換える (JSON の波かっこを壊さないよう format は使わない)。"""
    text = load(name)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
