"""タスク → LLM プロファイルの割り当てと、接続先のフォールバック (SPEC 4.2)。

候補を先頭から順に見て、最初に使えるものを採る:
1. GET {endpoint}/models に短いタイムアウトで返事があるか (使うモデルが一覧にあるか)
2. comfyui_hosts に対応があれば GET /queue を見て、queue_running が空でなければ生成中なので飛ばす
結果は少しの間 (既定 30 秒) 覚えておき、毎回は問い合わせない。
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import config
from .config import LLMProfile
from .llm_client import LLMClient, LLMError, http_json


@dataclass
class Health:
    ok: bool
    reason: str  # 使えないときの理由 (使えるときは「使えます」など)
    checked_at: float


class NoLLMAvailable(LLMError):
    def __init__(self, task: str, reasons: list[tuple[str, str]]):
        self.task = task
        self.reasons = reasons
        detail = " / ".join(f"{name}: {reason}" for name, reason in reasons) or "候補がありません"
        super().__init__(f"「{task}」に使える LLM がありません ({detail})")


def _models(profile: LLMProfile, timeout: float) -> list[str]:
    return LLMClient(profile).list_models(timeout=timeout)


def _comfy_busy(host: str, timeout: float) -> Optional[bool]:
    """ComfyUI が生成中なら True、空いていれば False、返事がなければ None。"""
    try:
        body = http_json(host.rstrip("/") + "/queue", timeout=timeout)
    except LLMError:
        return None
    running = body.get("queue_running") if isinstance(body, dict) else None
    return bool(running)


class Router:
    def __init__(self, settings: dict, profiles: dict[str, LLMProfile],
                 models_fn: Callable[[LLMProfile, float], list[str]] = _models,
                 comfy_busy_fn: Callable[[str, float], Optional[bool]] = _comfy_busy,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.profiles = profiles
        self._models_fn = models_fn
        self._comfy_busy_fn = comfy_busy_fn
        self._clock = clock
        self._cache: dict[str, Health] = {}
        self._lock = threading.Lock()

    @property
    def cache_sec(self) -> float:
        return float(self.settings.get("router_cache_sec", 30))

    @property
    def timeout(self) -> float:
        return float(self.settings.get("health_timeout_sec", 3))

    def candidates(self, task: str) -> list[str]:
        return config.candidates_for(self.settings, task)

    def invalidate(self, name: Optional[str] = None) -> None:
        """覚えた結果を捨てる (接続に失敗したときなど)。name なしなら全部。"""
        with self._lock:
            if name is None:
                self._cache.clear()
            else:
                self._cache.pop(name, None)

    def check(self, name: str, force: bool = False) -> Health:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(name)
        if cached is not None and not force and now - cached.checked_at < self.cache_sec:
            return cached
        health = self._probe(name, now)
        with self._lock:
            self._cache[name] = health
        return health

    def _probe(self, name: str, now: float) -> Health:
        profile = self.profiles.get(name)
        if profile is None:
            return Health(False, "このプロファイルがありません", now)
        try:
            models = self._models_fn(profile, self.timeout)
        except LLMError as exc:
            return Health(False, str(exc), now)
        if models and profile.model_id not in models:
            return Health(False, f"モデル {profile.model_id} がありません", now)
        host = (self.settings.get("comfyui_hosts") or {}).get(name)
        if host:
            busy = self._comfy_busy_fn(host, self.timeout)
            if busy:
                return Health(False, "同じ PC の ComfyUI が生成中", now)
        return Health(True, "使えます", now)

    def resolve(self, task: str) -> LLMProfile:
        """task に使う LLM プロファイル。全滅なら NoLLMAvailable。"""
        reasons: list[tuple[str, str]] = []
        for name in self.candidates(task):
            health = self.check(name)
            if health.ok:
                return self.profiles[name]
            reasons.append((name, health.reason))
        raise NoLLMAvailable(task, reasons)

    def explain(self, task: str, force: bool = False) -> list[tuple[str, Health]]:
        """画面用: 候補それぞれの状態。"""
        return [(name, self.check(name, force)) for name in self.candidates(task)]
