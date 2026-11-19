"""LM Studio (OpenAI 互換 API) とのやりとり (SPEC 4.1)。

- chat/completions を呼ぶ。thinking: strip なら <think>…</think> を除く
- JSON を求めるときは: スキーマ指定 → 失敗したら簡易修復して読み直す → それでもだめなら 1 回だけ再試行
  → それでもだめなら LLMJSONError (呼ぶ側はログと画面に出し、処理全体は止めない)
- タイムアウトと接続エラーは別の例外にする
"""

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from ..util import log
from . import prompts
from .config import LLMProfile


class LLMError(Exception):
    """LLM とのやりとりの失敗 (下の 4 つの親)。"""


class LLMConnectionError(LLMError):
    """接続できない (サーバーが落ちている・IP が違うなど)。"""


class LLMTimeoutError(LLMError):
    """待っても返事が来ない。"""


class LLMResponseError(LLMError):
    """返事は来たが使えない (HTTP エラー・形が違う)。"""


class LLMJSONError(LLMResponseError):
    """再試行しても JSON として読めなかった。raw に最後の返事を持つ。"""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


# ---------------------------------------------------------------- 返事の後始末

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)


def strip_think(text: str) -> str:
    """<think>…</think> を除く。

    - 閉じていない <think> (途中で切れた) は、そこから後ろを捨てる
    - 開きのない </think> (テンプレートが <think> を先に入れるモデル) は、そこまでを捨てる
    """
    text = _THINK_BLOCK.sub("", text or "")
    close = None
    for close in _THINK_CLOSE.finditer(text):
        pass
    if close is not None:
        text = text[close.end():]
    opened = _THINK_OPEN.search(text)
    if opened:
        text = text[: opened.start()]
    return text.strip()


_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _outer_json(text: str) -> str:
    """最初の { か [ から、対応する最後の } か ] まで。"""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return text
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    return text[start : end + 1] if end > start else text[start:]


def _remove_trailing_commas(text: str) -> str:
    """文字列の外にある「, }」「, ]」のカンマを除く。"""
    out = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == ",":
            rest = text[i + 1 :].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        out.append(ch)
    return "".join(out)


def parse_json(text: str) -> Any:
    """JSON として読む。だめなら簡易修復 (コードブロック・前後の文・末尾カンマ) して読み直す。

    読めなければ ValueError。
    """
    text = (text or "").strip()
    attempts = [text]
    fence = _FENCE.search(text)
    if fence:
        attempts.append(fence.group(1).strip())
    attempts.append(_outer_json(attempts[-1]))
    attempts.append(_remove_trailing_commas(attempts[-1]))
    last_error: Optional[Exception] = None
    for candidate in attempts:
        try:
            return json.loads(candidate)
        except ValueError as exc:
            last_error = exc
    raise ValueError(f"JSON として読めません: {last_error}")


# ---------------------------------------------------------------- 通信

@dataclass
class ChatResult:
    text: str  # 後始末したあとの本文
    raw: str  # サーバーが返したままの本文
    elapsed: float  # 秒
    model: str


# LAN の機械に直接つなぐ (環境変数のプロキシを使わない)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, payload: Optional[dict] = None, timeout: float = 10) -> Any:
    """GET (payload なし) / POST して JSON を返す。例外は LLM の 4 種類に分ける。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except OSError:
            pass
        raise LLMResponseError(f"HTTP {exc.code}: {log.summarize(detail, 200)}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise LLMTimeoutError(f"{timeout:g} 秒待っても返事がありません") from exc
        raise LLMConnectionError(f"接続できません ({exc.reason})") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise LLMTimeoutError(f"{timeout:g} 秒待っても返事がありません") from exc
    except (ConnectionError, OSError) as exc:
        raise LLMConnectionError(f"接続できません ({exc})") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise LLMResponseError("サーバーの返事が JSON ではありません") from exc


class LLMClient:
    def __init__(self, profile: LLMProfile):
        self.profile = profile

    @property
    def base_url(self) -> str:
        return self.profile.endpoint.rstrip("/")

    def list_models(self, timeout: Optional[float] = None) -> list[str]:
        body = http_json(self.base_url + "/models", timeout=timeout or 15)
        try:
            return sorted(str(item["id"]) for item in body.get("data", []))
        except (AttributeError, KeyError, TypeError) as exc:
            raise LLMResponseError("モデル一覧を読めません") from exc

    def build_messages(self, system: str, user: str, json_schema: Optional[dict] = None,
                       image_url: str = "") -> tuple[list[dict], Optional[dict]]:
        """送るメッセージと response_format を組み立てる (テストからも見る)。"""
        p = self.profile
        response_format = None
        if json_schema is not None:
            if p.supports_json_schema:
                response_format = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "strict": True, "schema": json_schema},
                }
            else:
                rule = prompts.fill("json_only", schema=json.dumps(json_schema, ensure_ascii=False))
                system = f"{system}\n\n{rule}" if system.strip() else rule
        if p.system_prompt_mode == "merge_user" and system.strip():
            # system role が効きにくいモデル: 指示を user にも重ねる
            user = prompts.fill("merge_user", system=system, user=user)
        if p.thinking == "off" and p.no_think_directive:
            user = f"{user}\n\n{p.no_think_directive}"
        messages = [{"role": "system", "content": system}] if system.strip() else []
        if image_url:
            content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": image_url}}]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user})
        return messages, response_format

    def chat(self, system: str, user: str, temperature: Optional[float] = None,
             json_schema: Optional[dict] = None, max_tokens: Optional[int] = None,
             image_url: str = "", purpose: str = "") -> ChatResult:
        p = self.profile
        messages, response_format = self.build_messages(system, user, json_schema, image_url)
        payload: dict[str, Any] = {
            "model": p.model_id,
            "messages": messages,
            "temperature": p.default_temperature if temperature is None else temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        if max_tokens:
            payload["max_tokens"] = max_tokens
        label = f"{purpose or 'chat'} / {p.name}"
        log.info(f"→ {label}: {log.summarize(user)}")
        start = time.monotonic()
        try:
            body = http_json(self.base_url + "/chat/completions", payload, timeout=p.timeout_sec)
        except LLMError as exc:
            log.error(f"✗ {label} ({time.monotonic() - start:.1f}s): {type(exc).__name__}: {exc}")
            raise
        elapsed = time.monotonic() - start
        try:
            raw = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            log.error(f"✗ {label}: 返事の形が違います")
            raise LLMResponseError("サーバーの返事を読めません") from exc
        text = strip_think(raw) if p.thinking in ("strip", "off") else raw.strip()
        log.info(f"← {label} ({elapsed:.1f}s): {log.summarize(text)}")
        return ChatResult(text=text, raw=raw, elapsed=elapsed, model=str(body.get("model") or p.model_id))

    def chat_json(self, system: str, user: str, json_schema: dict, temperature: Optional[float] = None,
                  max_tokens: Optional[int] = None, image_url: str = "", purpose: str = "") -> tuple[Any, ChatResult]:
        """JSON を返させて読む。読めなければ同じリクエストを 1 回だけやり直す。"""
        last: Optional[ChatResult] = None
        for attempt in (1, 2):
            last = self.chat(system, user, temperature, json_schema, max_tokens, image_url, purpose)
            try:
                return parse_json(last.text), last
            except ValueError as exc:
                log.error(f"JSON を読めません ({purpose or 'chat'} / {self.profile.name}, {attempt} 回目): {exc}")
        raise LLMJSONError("2 回とも JSON として読めませんでした", raw=last.text if last else "")

    def test_connection(self) -> tuple[list[str], ChatResult]:
        """接続テスト: モデル一覧を取り、短い質問を 1 つ送る。"""
        models = self.list_models(timeout=10)
        result = self.chat("", prompts.load("connection_test"), temperature=0, max_tokens=512,
                           purpose="接続テスト")
        return models, result
