"""5.4 ストーリーモード。3 段階を別々のリクエストにする (小さいモデルで 1 回にまとめると後半が崩れるため)。

① 解釈: 確認の質問 (最大 3 問。task story_plan)
② 設定シートと構成案: JSON (task story_plan)。画面で直せる。library/stories・library/characters に保存できる
③ 各シーン: シーンごとに 1 リクエストで中間表現 (task render) → 対象モデル向けにレンダリング
   登場キャラの look は毎回そのまま差し込み (subject に入れ、レンダリング後に入っているか確かめる)、
   world は style に、common_suffix は末尾に、全シーンで付ける
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import intermediate, prompts, renderer
from ..llm_client import LLMClient
from ..targets import TargetProfile
from ...util import files
from . import generate as generate_task

PLAN_TEMPERATURE = 0.8
SCENE_TEMPERATURE = 0.6


# ---------------------------------------------------------------- 設定シート

def empty_sheet() -> dict:
    return {"title": "", "world": "", "common_suffix": "", "characters": [], "scenes": []}


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(text).lower()).strip("_")
    return slug or fallback


def normalize_sheet(raw, fixed_characters: Optional[list[dict]] = None, scene_count: int = 0) -> dict:
    """LLM の返事 (または画面・ファイルから) を設定シートの形にそろえる。

    - キャラの id は小文字の英数字にし、重ならないようにする
    - fixed_characters (読み込んだキャラ) は、同じ id か名前のものを置き換えて先頭に置く (look をそのまま使う)
    - シーンは 1 から振り直し、登場キャラは知っている id だけにする (名前で書かれていても id に直す)
    """
    if not isinstance(raw, dict):
        raise ValueError("設定シートが JSON のオブジェクトではありません")
    sheet = empty_sheet()
    for key in ("title", "world", "common_suffix"):
        sheet[key] = " ".join(str(raw.get(key) or "").split())
    characters: list[dict] = []
    seen: set[str] = set()

    def add_character(item: dict, fallback: str):
        name = " ".join(str(item.get("name") or "").split())
        look = " ".join(str(item.get("look") or "").split())
        cid = _slug(item.get("id") or "", "") or _slug(name, fallback)
        base, n = cid, 2
        while cid in seen:
            cid = f"{base}_{n}"
            n += 1
        seen.add(cid)
        characters.append({"id": cid, "name": name or cid, "look": look})

    fixed = [c for c in (fixed_characters or []) if isinstance(c, dict)]
    for i, item in enumerate(fixed, start=1):
        add_character(item, f"c{i}")
    fixed_keys = {c["id"] for c in characters} | {c["name"] for c in characters}
    for i, item in enumerate(raw.get("characters") or [], start=len(characters) + 1):
        if not isinstance(item, dict):
            continue
        if _slug(item.get("id") or "", "") in fixed_keys or str(item.get("name") or "").strip() in fixed_keys:
            continue  # 読み込んだキャラが優先
        add_character(item, f"c{i}")
    sheet["characters"] = characters

    by_name = {c["name"]: c["id"] for c in characters}
    known = {c["id"] for c in characters}
    scenes = []
    for item in raw.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        summary = " ".join(str(item.get("summary") or "").split())
        if not title and not summary:
            continue
        ids = []
        for ref in item.get("characters") or []:
            ref = str(ref).strip()
            cid = ref if ref in known else by_name.get(ref) or (_slug(ref, "") if _slug(ref, "") in known else "")
            if cid and cid not in ids:
                ids.append(cid)
        scenes.append({"no": len(scenes) + 1, "title": title or f"シーン{len(scenes) + 1}",
                       "summary": summary, "characters": ids})
    if scene_count and len(scenes) > scene_count:
        scenes = scenes[:scene_count]
    sheet["scenes"] = scenes
    if not sheet["title"]:
        sheet["title"] = "story"
    return sheet


def sheet_schema() -> dict:
    character = {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                                  "look": {"type": "string"}},
                 "required": ["id", "name", "look"]}
    scene = {"type": "object", "properties": {"no": {"type": "integer"}, "title": {"type": "string"},
                                              "summary": {"type": "string"},
                                              "characters": {"type": "array", "items": {"type": "string"}}},
             "required": ["no", "title", "summary", "characters"]}
    return {"type": "object",
            "properties": {"title": {"type": "string"}, "world": {"type": "string"},
                           "common_suffix": {"type": "string"},
                           "characters": {"type": "array", "items": character},
                           "scenes": {"type": "array", "items": scene}},
            "required": ["title", "world", "common_suffix", "characters", "scenes"]}


# ---------------------------------------------------------------- ① 質問 / ② 構成案

def ask_questions(client: LLMClient, instruction: str) -> list[str]:
    schema = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
              "required": ["questions"]}
    raw, _ = client.chat_json(prompts.load("story_questions_system"), instruction.strip(), schema,
                              temperature=0.5, purpose="story questions")
    questions = raw.get("questions") if isinstance(raw, dict) else raw
    if not isinstance(questions, list):
        return []
    return [" ".join(str(q).split()) for q in questions if str(q).strip()][:3]


def build_plan_prompt(instruction: str, scene_count: int = 0, tempo: str = "", omakase: int = 50,
                      answers: Optional[list[tuple[str, str]]] = None,
                      fixed_characters: Optional[list[dict]] = None) -> tuple[str, str]:
    parts = prompts.sections("story_plan_parts")
    scene_rule = parts["scenes_fixed"].replace("{count}", str(scene_count)) if scene_count else parts["scenes_auto"]
    tempo_rule = parts["tempo"].replace("{tempo}", tempo.strip()) if tempo.strip() else parts["tempo_none"]
    fill_rule = prompts.sections("generate_levels")[generate_task.fill_level(omakase)]
    system = prompts.fill("story_plan_system", scene_rule=scene_rule, tempo_rule=tempo_rule, fill_rule=fill_rule)
    answered = [(q, a) for q, a in (answers or []) if a.strip()]
    answers_text = parts["answers"].replace("{answers}", "\n".join(f"Q: {q}\nA: {a}" for q, a in answered)) if answered else ""
    fixed_text = ""
    if fixed_characters:
        listing = "\n".join(json.dumps({k: c.get(k, "") for k in ("id", "name", "look")}, ensure_ascii=False)
                            for c in fixed_characters)
        fixed_text = parts["fixed"].replace("{characters}", listing)
    user = prompts.fill("story_plan_user", instruction=instruction.strip(), answers=answers_text, fixed=fixed_text)
    return system, user.strip()


def plan(client: LLMClient, instruction: str, scene_count: int = 0, tempo: str = "", omakase: int = 50,
         answers: Optional[list[tuple[str, str]]] = None, fixed_characters: Optional[list[dict]] = None) -> dict:
    system, user = build_plan_prompt(instruction, scene_count, tempo, omakase, answers, fixed_characters)
    raw, _ = client.chat_json(system, user, sheet_schema(),
                              temperature=max(0.3, min(1.0, PLAN_TEMPERATURE + (omakase - 50) / 250)),
                              purpose="story plan")
    sheet = normalize_sheet(raw, fixed_characters, scene_count)
    if not sheet["scenes"]:
        raise ValueError("構成案にシーンがありません")
    return sheet


# ---------------------------------------------------------------- ③ 各シーン

def scene_characters(sheet: dict, scene: dict) -> list[dict]:
    by_id = {c["id"]: c for c in sheet.get("characters", [])}
    return [by_id[cid] for cid in scene.get("characters", []) if cid in by_id]


def looks_for(sheet: dict, scene: dict) -> list[str]:
    return [c["look"] for c in scene_characters(sheet, scene) if c.get("look")]


def _neighbor(sheet: dict, index: int) -> str:
    scenes = sheet.get("scenes", [])
    if 0 <= index < len(scenes):
        s = scenes[index]
        return f"{s.get('title', '')}: {s.get('summary', '')}"
    return "(none)"


def scene_prompt(sheet: dict, index: int) -> tuple[str, str]:
    scene = sheet["scenes"][index]
    chars = scene_characters(sheet, scene)
    listing = "\n".join(f"- {c['name']} ({c['id']})" for c in chars) or "- (no characters; scenery only)"
    system = prompts.fill("story_scene_system", world=sheet.get("world", "") or "(free)", characters=listing)
    user = prompts.fill("story_scene_user", no=str(scene["no"]), total=str(len(sheet["scenes"])),
                        title=scene.get("title", ""), summary=scene.get("summary", ""),
                        previous=_neighbor(sheet, index - 1), next=_neighbor(sheet, index + 1))
    return system, user


def scene_intermediate(client: LLMClient, sheet: dict, index: int) -> dict:
    """シーンの中間表現。キャラの look と world は LLM に任せず、決まった形で差し込む。"""
    scene = sheet["scenes"][index]
    system, user = scene_prompt(sheet, index)
    raw, _ = client.chat_json(system, user, intermediate.schema(include_video=False), temperature=SCENE_TEMPERATURE,
                              purpose=f"story scene {scene['no']}")
    if isinstance(raw, dict) and not str(raw.get("subject") or "").strip():
        raw["subject"] = "scene"
    base = intermediate.new("image", "story", instruction=f"{sheet.get('title', '')} #{scene['no']}")
    data = intermediate.normalize(raw, base)
    return apply_sheet(data, sheet, scene)


def apply_sheet(data: dict, sheet: dict, scene: dict) -> dict:
    """look を subject に、world を style に差し込む (毎回同じ文)。"""
    data = dict(data)
    chars = scene_characters(sheet, scene)
    if chars:
        # 日本語の名前は英語のプロンプトに混ぜない (役は LLM の subject に英語で入っている)
        who = " and ".join(c["look"] if c.get("look") else c["id"] for c in chars)
        role = data.get("subject", "")
        data["subject"] = f"{who}" + (f", {role}" if role and role.lower() != "scene" else "")
    world = sheet.get("world", "")
    if world:
        data["style"] = renderer.join_parts([world, data.get("style", "")], ", ")
    return data


def video_intermediate(client: LLMClient, sheet: dict, index: int, data: dict, seconds: float = 5) -> dict:
    """動画用: 前のシーンの終わりから続き、次のシーンにつながる動きを足す。"""
    from . import process
    system = prompts.fill("story_video_system", seconds=f"{seconds:g}",
                          previous=_neighbor(sheet, index - 1), next=_neighbor(sheet, index + 1))
    raw, _ = client.chat_json(system, process._fields_json(data, True), intermediate.schema(include_video=True),
                              temperature=SCENE_TEMPERATURE, purpose=f"story video {index + 1}")
    result = intermediate.normalize(raw, dict(data))
    result["kind"] = "video"
    # look と world は変えさせない
    result["subject"], result["style"] = data["subject"], data["style"]
    return result


@dataclass
class Variation:
    label: str  # サブフォルダの名前
    modifier: str  # 足す指示 (英語でも日本語でも)


def parse_variations(text: str) -> list[Variation]:
    """「昼版: bright daylight」のように 1 行 1 つ。「:」がなければ名前をそのまま指示にする。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, modifier = line.partition(":") if ":" in line else line.partition("：")
        label, modifier = label.strip(), (modifier.strip() or label.strip())
        if label:
            out.append(Variation(files.safe_name(label), modifier))
    return out


def with_variation(data: dict, variation: Optional[Variation]) -> dict:
    if variation is None:
        return data
    data = dict(data)
    data["extra"] = renderer.join_parts([f"variation: {variation.modifier}", data.get("extra", "")], ", ")
    return data


def render_scene(client: LLMClient, sheet: dict, scene: dict, data: dict, target: TargetProfile):
    return renderer.render(client, data, target, verbatim=looks_for(sheet, scene),
                           suffix=sheet.get("common_suffix", ""))


def scene_file_stem(scene: dict) -> str:
    return f"{int(scene['no']):03d}_{files.safe_name(scene.get('title') or 'scene')}"


# ---------------------------------------------------------------- 保存・読み込み

def save_sheet(sheet: dict, folder: Path) -> Path:
    path = files.unique_path(folder / f"{files.safe_name(sheet.get('title') or 'story')}.json")
    files.write_json(path, sheet)
    return path


def load_sheet(path: Path) -> dict:
    data = files.read_json(path, None)
    if data is None:
        raise ValueError(f"{path.name} を読めません")
    return normalize_sheet(data)


def save_character(character: dict, folder: Path) -> Path:
    path = files.unique_path(folder / f"{files.safe_name(character.get('name') or character.get('id') or 'character')}.json")
    files.write_json(path, {k: character.get(k, "") for k in ("id", "name", "look")})
    return path


def load_character(path: Path) -> dict:
    data = files.read_json(path, None)
    if not isinstance(data, dict) or not (data.get("look") or data.get("name")):
        raise ValueError(f"{path.name} はキャラのファイルではありません")
    return {"id": _slug(data.get("id") or "", "") or _slug(data.get("name") or "", "c"),
            "name": str(data.get("name") or ""), "look": " ".join(str(data.get("look") or "").split())}
