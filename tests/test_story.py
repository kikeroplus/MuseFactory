"""フェーズ4: ストーリーモード。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.core import output, renderer
from app.core.config import LLMProfile
from app.core.llm_client import LLMClient
from app.core.targets import TargetProfile
from app.core.tasks import story
from app.util import log
from tests.fake_server import FakeServer

HERO = "silver-haired young man, blue cape, scarred armor, one-handed sword"
LORD = "black horns, red eyes, jet-black robe"
PLAN = {
    "title": "勇者と魔王", "world": "dark fantasy, oil painting style, dusk tones",
    "common_suffix": "highly detailed, cinematic composition",
    "characters": [{"id": "hero", "name": "勇者", "look": HERO}, {"id": "demon_lord", "name": "魔王", "look": LORD}],
    "scenes": [
        {"no": 1, "title": "旅立ち", "summary": "勇者が村を出る", "characters": ["hero"]},
        {"no": 2, "title": "魔王城", "summary": "魔王城を見上げる", "characters": ["勇者"]},
        {"no": 3, "title": "対峙", "summary": "玉座の間で魔王と向き合う", "characters": ["hero", "demon_lord"]},
        {"no": 4, "title": "激突", "summary": "剣と魔法がぶつかる", "characters": ["hero", "demon_lord"]},
        {"no": 5, "title": "決着", "summary": "勇者の一撃が魔王を貫く", "characters": ["hero", "demon_lord"]},
        {"no": 6, "title": "夜明け", "summary": "朝日の中で剣を下ろす", "characters": ["hero"]},
    ],
}


def responder(payload):
    system = payload["messages"][0]["content"]
    if system.startswith("You plan a short visual story"):
        return json.dumps(PLAN, ensure_ascii=False)
    if system.startswith("You help plan"):
        return json.dumps({"questions": ["結末はハッピーエンド?", "勇者の年齢は?"]}, ensure_ascii=False)
    if system.startswith("You describe one scene"):
        return json.dumps({"subject": "the hero", "pose": "standing", "background": "castle", "lighting": "dusk"})
    if system.startswith("You turn one scene"):
        data = json.loads(payload["messages"][-1]["content"])
        data.update({"motion": "walks forward", "camera_motion": "slow pan"})
        return json.dumps(data)
    # レンダリング: わざと見た目を言い換える (look がそのまま残るか確かめるため)
    return "A brave warrior with pale hair stands before a dark castle at dusk."


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


class SheetTest(unittest.TestCase):
    def test_normalize_ids_and_scene_refs(self):
        sheet = story.normalize_sheet(PLAN)
        self.assertEqual([c["id"] for c in sheet["characters"]], ["hero", "demon_lord"])
        self.assertEqual(sheet["scenes"][1]["characters"], ["hero"])  # 名前で書かれていても id に直す
        self.assertEqual([s["no"] for s in sheet["scenes"]], [1, 2, 3, 4, 5, 6])

    def test_fixed_character_wins(self):
        fixed = [{"id": "hero", "name": "勇者", "look": "red-haired girl knight"}]
        sheet = story.normalize_sheet(PLAN, fixed)
        hero = next(c for c in sheet["characters"] if c["id"] == "hero")
        self.assertEqual(hero["look"], "red-haired girl knight")
        self.assertEqual(len(sheet["characters"]), 2)

    def test_duplicate_and_odd_ids(self):
        raw = {"characters": [{"id": "Hero King!", "name": "a", "look": "x"}, {"id": "hero king", "name": "b", "look": "y"},
                              {"name": "魔王", "look": "z"}],
               "scenes": [{"title": "t", "summary": "s", "characters": ["魔王", "nobody"]}]}
        sheet = story.normalize_sheet(raw)
        ids = [c["id"] for c in sheet["characters"]]
        self.assertEqual(ids[:2], ["hero_king", "hero_king_2"])
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(sheet["scenes"][0]["characters"], [ids[2]])

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = story.save_sheet(story.normalize_sheet(PLAN), Path(tmp))
            self.assertEqual(path.name, "勇者と魔王.json")
            self.assertEqual(story.load_sheet(path)["characters"][0]["look"], HERO)
            cpath = story.save_character({"id": "hero", "name": "勇者", "look": HERO}, Path(tmp))
            self.assertEqual(story.load_character(cpath)["look"], HERO)

    def test_variations(self):
        v = story.parse_variations("昼版: bright daylight\n夜版：night\nアニメ調\n")
        self.assertEqual([(x.label, x.modifier) for x in v],
                         [("昼版", "bright daylight"), ("夜版", "night"), ("アニメ調", "アニメ調")])

    def test_file_stem(self):
        self.assertEqual(story.scene_file_stem({"no": 1, "title": "旅立ち/出発?"}), "001_旅立ち_出発_")


class VerbatimTest(unittest.TestCase):
    def test_missing_phrase_is_prepended(self):
        t = TargetProfile(name="T")
        text, added = renderer.ensure_verbatim("A warrior stands.", [HERO], t)
        self.assertEqual(added, [HERO])
        self.assertTrue(text.startswith(HERO + "."))

    def test_present_phrase_untouched(self):
        t = TargetProfile(name="T")
        text, added = renderer.ensure_verbatim(f"A {HERO.upper()} stands.", [HERO], t)
        self.assertEqual(added, [])


class StoryFlowTest(unittest.TestCase):
    """確認手順: 構成案 (6 シーン前後) → 直す → 連番ファイル。全シーンにキャラの look が入る。"""

    def test_plan_scenes_files(self):
        image = TargetProfile(name="Krea2", element_order=["subject", "pose", "background"])
        video = TargetProfile(name="Wan", type="video", mode="i2v")
        with FakeServer(responder=responder) as server, tempfile.TemporaryDirectory() as tmp:
            client = LLMClient(LLMProfile(name="fake", endpoint=server.endpoint, model_id="qwen-9b-instruct"))
            self.assertEqual(story.ask_questions(client, "ファンタジーで勇者が魔王と戦ってる一連のやつ"),
                             ["結末はハッピーエンド?", "勇者の年齢は?"])
            sheet = story.plan(client, "ファンタジーで勇者が魔王と戦ってる一連のやつ", 0, "起承転結", 60,
                               [("結末はハッピーエンド?", "はい")])
            plan_request = server.requests_chat()[-1]["messages"][-1]["content"]
            self.assertIn("はい", plan_request)
            self.assertEqual(len(sheet["scenes"]), 6)
            # 画面で直したつもり: シーン 2 のタイトルを変える
            sheet["scenes"][1]["title"] = "魔王城を望む"
            folder = Path(tmp) / "勇者と魔王"
            reserved: set = set()
            texts = {}
            for index, scene in enumerate(sheet["scenes"]):
                base = story.scene_intermediate(client, sheet, index)
                r = story.render_scene(client, sheet, scene, base, image)
                path = output.free_prompt_path(folder, story.scene_file_stem(scene), reserved)
                output.save_prompt(path, r.text, base)
                texts[path.name] = r.text
                if index == 0:
                    v = story.video_intermediate(client, sheet, index, base, 5)
                    self.assertEqual(v["subject"], base["subject"])  # look は動画でも変わらない
                    self.assertEqual(v["motion"], "walks forward")
                    vr = story.render_scene(client, sheet, scene, v, video)
                    vpath = output.free_prompt_path(folder / output.VIDEO_DIR, story.scene_file_stem(scene) + "_video", reserved)
                    output.save_prompt(vpath, vr.text, v)
            names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.txt"))

        self.assertEqual([n for n in names if "/" not in n],
                         ["001_旅立ち.txt", "002_魔王城を望む.txt", "003_対峙.txt", "004_激突.txt", "005_決着.txt", "006_夜明け.txt"])
        self.assertIn("video/001_旅立ち_video.txt", names)
        for name, text in texts.items():
            self.assertIn(HERO, text, name)  # 勇者は全シーンに出る
            self.assertTrue(text.endswith("highly detailed, cinematic composition"), name)
        for name in ("003_対峙.txt", "004_激突.txt", "005_決着.txt"):
            self.assertIn(LORD, texts[name])
        self.assertNotIn(LORD, texts["001_旅立ち.txt"])


if __name__ == "__main__":
    unittest.main()
