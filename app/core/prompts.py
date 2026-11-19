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


@lru_cache(maxsize=None)
def sections(name: str) -> dict[str, str]:
    """「[名前]」の行で区切った指示文のファイルを {名前: 本文} にする。"""
    import re
    parts = re.split(r"^\[(\w+)\]\s*$", load(name), flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
