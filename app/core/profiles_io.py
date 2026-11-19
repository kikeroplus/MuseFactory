"""プロファイルの管理 (SPEC 5.6): 保存・複製・削除・自動作成・お手本の追加。

モデルプロファイルは profiles/targets/、LLM プロファイルは profiles/llms/ に 1 つ 1 ファイル (JSON)。
画面では JSON をそのまま直す。保存の前に、読めるか・必要な項目があるかを確かめる。
"""

import json
import re
from pathlib import Path
from typing import Optional

from .. import paths
from . import prompts
from ..util import files
from .config import LLMProfile
from .intermediate import TEXT_FIELDS
from .llm_client import LLMClient
from .targets import TargetProfile

TARGET, LLM = "target", "llm"
FOLDERS = {TARGET: paths.TARGET_PROFILES_DIR, LLM: paths.LLM_PROFILES_DIR}
EXAMPLE_LIMIT = 5  # お手本の上限の既定 (settings.json の example_limit で変えられる)

TARGET_TEMPLATE = {
    "name": "新しいモデル", "type": "image", "mode": "t2i", "output_language": "en", "style": "natural_sentence",
    "max_length": 120, "negative_supported": False,
    "element_order": ["subject", "pose", "expression", "outfit", "background", "lighting", "camera", "style"],
    "skip_elements": [], "prefix": "", "suffix": "",
    "system_prompt": "You write text-to-image prompts for ...", "example_prompts": [],
    "temperature": 0.7, "reference_image_mode": False,
}
LLM_TEMPLATE = {
    "name": "新しい LLM", "endpoint": "http://192.168.55.3:1234/v1", "model_id": "", "supports_json_schema": True,
    "supports_vision": False, "thinking": "strip", "system_prompt_mode": "system", "default_temperature": 0.7,
    "context_length": 8192, "timeout_sec": 180,
}


def validate(kind: str, text: str) -> dict:
    """JSON の文を読んで確かめる。だめなら ValueError (理由つき)。"""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"JSON として読めません: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("{ … } の形にしてください")
    try:
        if kind == TARGET:
            profile = TargetProfile.from_dict(data)
            if profile.type not in ("image", "video"):
                raise ValueError("type は image か video にしてください")
            unknown = [e for e in profile.element_order + profile.skip_elements if e not in TEXT_FIELDS + ["timeline"]]
            if unknown:
                raise ValueError(f"知らない要素があります: {', '.join(unknown)} (使えるのは {', '.join(TEXT_FIELDS)})")
        else:
            profile = LLMProfile.from_dict(data)
            if profile.thinking not in ("strip", "off", "keep"):
                raise ValueError("thinking は strip / off / keep のどれかにしてください")
            if profile.system_prompt_mode not in ("system", "merge_user"):
                raise ValueError("system_prompt_mode は system か merge_user にしてください")
    except TypeError as exc:
        raise ValueError(f"項目の形が違います: {exc}") from exc
    return data


def file_name_for(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return files.safe_name(slug or name)


def save(kind: str, data: dict, path: Optional[Path] = None, folder: Optional[Path] = None) -> Path:
    """path があればそこに書き直し、なければ名前から新しいファイル (重ならない名前) を作る。"""
    base = folder or FOLDERS[kind]
    if path is None:
        path = files.unique_path(base / f"{file_name_for(data['name'])}.json")
    files.write_json(path, data)
    return path


def names_in(kind: str, folder: Optional[Path] = None) -> dict[str, Path]:
    out = {}
    for path in sorted((folder or FOLDERS[kind]).glob("*.json")):
        data = files.read_json(path, None)
        if isinstance(data, dict) and data.get("name"):
            out.setdefault(str(data["name"]), path)
    return out


def unique_name(kind: str, name: str, folder: Optional[Path] = None) -> str:
    taken = names_in(kind, folder)
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def duplicate(kind: str, path: Path, folder: Optional[Path] = None) -> Path:
    data = files.read_json(path, None)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} を読めません")
    data["name"] = unique_name(kind, f"{data.get('name', '')} のコピー", folder)
    return save(kind, data, folder=folder)


# ---------------------------------------------------------------- お手本

def add_example(path: Path, text: str, limit: int = EXAMPLE_LIMIT, drop_oldest: bool = False) -> tuple[bool, int]:
    """モデルプロファイルの example_prompts に足す。

    上限を超えるときは drop_oldest=True のときだけ古いほうから消す (False なら足さずに (False, 超える数) を返す)。
    戻り値: (足したか, 消した / 消す数)。同じ文がもうあれば足さない。
    """
    data = files.read_json(path, None)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} を読めません")
    examples = [e for e in data.get("example_prompts") or [] if isinstance(e, str)]
    text = text.strip()
    if not text or text in examples:
        return False, 0
    over = len(examples) + 1 - limit
    if over > 0 and not drop_oldest:
        return False, over
    examples.append(text)
    removed = 0
    if over > 0:
        examples = examples[over:]
        removed = over
    data["example_prompts"] = examples
    files.write_json(path, data)
    return True, removed


# ---------------------------------------------------------------- 自動作成

def draft_schema() -> dict:
    strings = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "type": {"type": "string", "enum": ["image", "video"]},
            "mode": {"type": "string"}, "output_language": {"type": "string"},
            "style": {"type": "string", "enum": ["natural_sentence", "tags"]},
            "max_length": {"type": "integer"}, "negative_supported": {"type": "boolean"},
            "element_order": strings, "skip_elements": strings, "prefix": {"type": "string"},
            "suffix": {"type": "string"}, "system_prompt": {"type": "string"}, "example_prompts": strings,
            "temperature": {"type": "number"}, "reference_image_mode": {"type": "boolean"},
        },
        "required": ["name", "type", "mode", "output_language", "style", "max_length", "negative_supported",
                     "element_order", "skip_elements", "prefix", "suffix", "system_prompt", "example_prompts",
                     "temperature", "reference_image_mode"],
    }


def draft_target(client: LLMClient, guide: str, name_hint: str = "") -> dict:
    """公式のガイドや作例の文から、モデルプロファイルの下書きを作る (保存はしない)。"""
    user = prompts.fill("profile_draft_user", model=name_hint.strip() or "(guess from the guide)", guide=guide.strip())
    raw, _ = client.chat_json(prompts.fill("profile_draft_system", elements=", ".join(TEXT_FIELDS)), user,
                              draft_schema(), temperature=0.3, purpose="profile draft")
    data = dict(TARGET_TEMPLATE)
    if isinstance(raw, dict):
        data.update({k: v for k, v in raw.items() if k in TARGET_TEMPLATE})
    data["element_order"] = [e for e in data.get("element_order") or [] if e in TEXT_FIELDS] or TARGET_TEMPLATE["element_order"]
    data["skip_elements"] = [e for e in data.get("skip_elements") or [] if e in TEXT_FIELDS]
    if name_hint.strip():
        data["name"] = name_hint.strip()
    return data


# ---------------------------------------------------------------- 使えるモデルから LLM プロファイルを作る

def available_models(endpoint: str, timeout: float = 10) -> list[dict]:
    """LLM サーバーに入っているモデルの一覧。埋め込み用のモデルは外す。

    LM Studio の /api/v0/models なら種類 (vlm / llm)・最大文脈長・読み込み中かも取れる。
    取れないサーバーなら OpenAI 互換の /v1/models (ID だけ) にする。
    戻り値は [{"id", "type", "arch", "max_context", "loaded"}]。type が分からなければ ""。
    """
    from .llm_client import LLMError, http_json

    base = endpoint.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    body = None
    try:
        body = http_json(root + "/api/v0/models", timeout=timeout)
    except LLMError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        body = http_json(base + "/models", timeout=timeout)
    out = []
    for item in body.get("data", []) if isinstance(body, dict) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        model_id = str(item["id"])
        kind = str(item.get("type") or "")
        if kind in ("embeddings", "embedding") or "embed" in model_id.lower():
            continue
        out.append({"id": model_id, "type": kind, "arch": str(item.get("arch") or ""),
                    "max_context": int(item.get("max_context_length") or 0),
                    "loaded": item.get("state") == "loaded"})
    return sorted(out, key=lambda m: m["id"])


def host_label(endpoint: str, folder: Optional[Path] = None) -> str:
    """同じ endpoint の既存プロファイルの名前の「@…」を使う (なければホスト名)。"""
    for path in names_in(LLM, folder).values():
        data = files.read_json(path, None)
        if isinstance(data, dict) and str(data.get("endpoint", "")).rstrip("/") == endpoint.rstrip("/"):
            name = str(data.get("name", ""))
            if " @" in name:
                return name.rsplit(" @", 1)[1]
    m = re.match(r"\w+://([^/:]+)", endpoint)
    return m.group(1) if m else endpoint


def profile_from_model(info: dict, endpoint: str, folder: Optional[Path] = None) -> dict:
    """一覧の 1 件から LLM プロファイルを作る (まだ保存しない)。"""
    short = info["id"].rsplit("/", 1)[-1]
    data = dict(LLM_TEMPLATE)
    data["name"] = unique_name(LLM, f"{short} @{host_label(endpoint, folder)}", folder)
    data["endpoint"] = endpoint.rstrip("/")
    data["model_id"] = info["id"]
    data["supports_vision"] = info.get("type") == "vlm"
    # Gemma は system の役割が効きにくいので、指示を user に重ねる (既存の Gemma のプロファイルと同じ)
    if "gemma" in (info.get("arch") or info["id"]).lower():
        data["system_prompt_mode"] = "merge_user"
    if info.get("max_context"):
        data["context_length"] = min(int(info["max_context"]), LLM_TEMPLATE["context_length"])
    return data


def registered_models(folder: Optional[Path] = None) -> dict[tuple[str, str], str]:
    """(endpoint, model_id) → もう登録してあるプロファイルの名前。"""
    out = {}
    for name, path in names_in(LLM, folder).items():
        data = files.read_json(path, None)
        if isinstance(data, dict):
            out.setdefault((str(data.get("endpoint", "")).rstrip("/"), str(data.get("model_id", ""))), name)
    return out
