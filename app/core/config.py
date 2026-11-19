"""設定 (config/settings.json) と LLM プロファイル (profiles/llms/*.json) の読み込み。

読めない・壊れているときは既定値で動かす (エラーにしない)。読めなかったものは warnings に残し、画面で知らせる。
"""

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

from .. import paths
from ..util import files

# SPEC 3.4。値は優先順位つきの候補 (LLM プロファイル名) のリスト。
# generate / negative / profile_draft は SPEC 7.5 で足したもの。設定にないタスクは render の割り当てを使う。
TASKS = ["generate", "extract", "translate", "render", "enhance", "negative",
         "story_plan", "image_to_prompt", "profile_draft"]
FALLBACK_TASK = "render"

DEFAULT_SETTINGS = {
    "task_routing": {
        "generate": ["Qwen 9B @LLM機"],
        "extract": ["Qwen 9B @LLM機"],
        "translate": ["Qwen 9B @LLM機"],
        "render": ["Qwen 9B @LLM機"],
        "enhance": ["Qwen 9B @LLM機"],
        "negative": ["Qwen 9B @LLM機"],
        "story_plan": ["Qwen 9B @LLM機"],
        "image_to_prompt": ["Qwen 9B @LLM機", "Gemma Vision @LLM機"],
        "profile_draft": ["Qwen 9B @LLM機"],
    },
    # LLM プロファイル名 → 同じ PC の ComfyUI。PC1/PC2 の LM Studio を使うときに足す (例: "Qwen 14B @PC1": "http://192.168.11.31:8188")
    "comfyui_hosts": {},
    "save_intermediate_json": True,
    "keep_japanese_on_translate": True,
    "notify_pwc_on_save": False,
    "default_output_dir": "",
    "router_cache_sec": 30,
    "health_timeout_sec": 3,
}


@dataclass
class LLMProfile:
    """SPEC 3.3。"""
    name: str
    endpoint: str
    model_id: str
    supports_json_schema: bool = True
    supports_vision: bool = False
    thinking: str = "strip"  # strip / off / keep
    system_prompt_mode: str = "system"  # system / merge_user
    default_temperature: float = 0.7
    context_length: int = 8192
    timeout_sec: float = 180
    # thinking: off のときに付ける指示 (Qwen3 系は /no_think)。モデルに合わせてファイルで変えられる
    no_think_directive: str = "/no_think"
    source: Optional[Path] = field(default=None, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: dict, source: Optional[Path] = None) -> "LLMProfile":
        known = {f.name for f in fields(cls)} - {"source"}
        values = {k: v for k, v in data.items() if k in known}
        if not values.get("name") or not values.get("endpoint") or "model_id" not in values:
            raise ValueError("name / endpoint / model_id がありません")
        profile = cls(**values)
        profile.source = source
        return profile


def load_settings(path: Path = paths.SETTINGS_FILE) -> tuple[dict, list[str]]:
    """設定を読む。足りない項目は既定値で埋める。戻り値は (設定, 知らせる文)。"""
    warnings: list[str] = []
    data = files.read_json(path, None)
    if data is None:
        if path.exists():
            warnings.append(f"{path.name} を読めないので既定の設定で動きます")
        data = {}
    elif not isinstance(data, dict):
        warnings.append(f"{path.name} の形が違うので既定の設定で動きます")
        data = {}
    settings = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
    for key, value in data.items():
        if key in ("task_routing", "comfyui_hosts") and isinstance(value, dict):
            settings[key].update(value)
        else:
            settings[key] = value
    return settings, warnings


def save_settings(settings: dict, path: Path = paths.SETTINGS_FILE) -> None:
    files.write_json(path, settings)


def ensure_settings_file(path: Path = paths.SETTINGS_FILE) -> None:
    """初回起動時に既定の設定ファイルを書いておく (利用者が開いて直せるように)。"""
    if not path.exists():
        save_settings(DEFAULT_SETTINGS, path)


def candidates_for(settings: dict, task: str) -> list[str]:
    routing = settings.get("task_routing") or {}
    names = routing.get(task)
    if not names:
        names = routing.get(FALLBACK_TASK) or []
    if isinstance(names, str):
        names = [names]
    return [str(n) for n in names]


def load_llm_profiles(folder: Path = paths.LLM_PROFILES_DIR) -> tuple[dict[str, LLMProfile], list[str]]:
    """profiles/llms/*.json を名前順に読む。戻り値は ({名前: プロファイル}, 知らせる文)。"""
    profiles: dict[str, LLMProfile] = {}
    warnings: list[str] = []
    for path in sorted(folder.glob("*.json")):
        data = files.read_json(path, None)
        if not isinstance(data, dict):
            warnings.append(f"LLM プロファイル {path.name} を読めません")
            continue
        try:
            profile = LLMProfile.from_dict(data, path)
        except (TypeError, ValueError) as exc:
            warnings.append(f"LLM プロファイル {path.name}: {exc}")
            continue
        if profile.name in profiles:
            warnings.append(f"LLM プロファイルの名前「{profile.name}」が重なっています ({path.name} は使いません)")
            continue
        profiles[profile.name] = profile
    return profiles, warnings


def load_target_profiles(folder: Path = paths.TARGET_PROFILES_DIR) -> tuple[dict, list[str]]:
    """profiles/targets/*.json (SPEC 3.2) を名前順に読む。戻り値は ({名前: TargetProfile}, 知らせる文)。"""
    from .targets import TargetProfile
    profiles: dict = {}
    warnings: list[str] = []
    for path in sorted(folder.glob("*.json")):
        data = files.read_json(path, None)
        if not isinstance(data, dict):
            warnings.append(f"モデルプロファイル {path.name} を読めません")
            continue
        try:
            profile = TargetProfile.from_dict(data, path)
        except (TypeError, ValueError) as exc:
            warnings.append(f"モデルプロファイル {path.name}: {exc}")
            continue
        if profile.name in profiles:
            warnings.append(f"モデルプロファイルの名前「{profile.name}」が重なっています ({path.name} は使いません)")
            continue
        profiles[profile.name] = profile
    return profiles, warnings
