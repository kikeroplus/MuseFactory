"""フェーズ2: 中間表現・レンダラー・ざっくり生成・保存の名前。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.core import intermediate, output, renderer
from app.core.config import LLMProfile, load_target_profiles
from app.core.llm_client import LLMClient
from app.core.targets import TargetProfile
from app.core.tasks import generate
from app.util import files, log
from tests.fake_server import FakeServer

GEN_REPLY = {
    "subject": "girl in a white dress", "pose": "looking back over her shoulder", "expression": "gentle smile",
    "outfit": "white sundress", "background": "seaside at sunset", "lighting": "golden hour",
    "camera": "medium shot", "style": "film photo", "motion": "", "camera_motion": "", "extra": "",
    "negative": "blurry, extra fingers",
}


def responder(payload):
    """JSON を頼まれたら中間表現、そうでなければプロンプト文を返す。"""
    if "response_format" in payload:
        return json.dumps(GEN_REPLY)
    return "<think>ok</think>Prompt: A girl in a white sundress looks back over her shoulder at a sunset beach."


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


def target(**kw) -> TargetProfile:
    base = dict(name="T", element_order=["subject", "pose", "background"], max_length=10)
    base.update(kw)
    return TargetProfile(**base)


class IntermediateTest(unittest.TestCase):
    def test_normalize_fills_and_moves_unknown(self):
        data = intermediate.normalize({"subject": "hero", "pose": ["standing", "arms crossed"], "mood": "calm",
                                       "timeline": [{"start": 0, "end": 2, "action": "walks"}, "bad"]})
        self.assertEqual(data["pose"], "standing, arms crossed")
        self.assertEqual(data["extra"], "mood: calm")
        self.assertEqual(data["outfit"], "")
        self.assertEqual(data["timeline"], [{"start": 0.0, "end": 2.0, "action": "walks"}])

    def test_empty_subject_rejected(self):
        with self.assertRaises(ValueError):
            intermediate.normalize({"pose": "x"})

    def test_roundtrip_keeps_meta(self):
        data = intermediate.normalize({"subject": "hero"}, intermediate.new("video"))
        data["target"] = "Krea2"
        again = intermediate.load(intermediate.to_json(data))
        self.assertEqual(again["id"], data["id"])
        self.assertEqual(again["kind"], "video")
        self.assertEqual(again["target"], "Krea2")
        self.assertEqual(again["extra"], "")

    def test_schema_is_flat(self):
        schema = intermediate.schema(include_video=False)
        self.assertNotIn("motion", schema["properties"])
        self.assertTrue(all(v == {"type": "string"} for v in schema["properties"].values()))


class RendererTest(unittest.TestCase):
    def setUp(self):
        self.data = intermediate.normalize(dict(GEN_REPLY, motion="turns around", camera_motion="slow pan"))

    def test_order_and_skip(self):
        t = target(skip_elements=["background"])
        names = [n for n, _ in renderer.ordered_elements(self.data, t)]
        self.assertEqual(names[:2], ["subject", "pose"])
        self.assertNotIn("background", names)
        self.assertNotIn("negative", names)
        self.assertNotIn("motion", names)  # 画像のモデルには動きを渡さない

    def test_video_gets_motion(self):
        t = target(type="video", element_order=["subject", "motion", "camera_motion"], skip_elements=["outfit", "background"])
        names = [n for n, _ in renderer.ordered_elements(self.data, t)]
        self.assertEqual(names[:3], ["subject", "motion", "camera_motion"])
        self.assertNotIn("outfit", names)

    def test_prompt_has_examples_and_language(self):
        t = target(system_prompt="SYS", example_prompts=["EXAMPLE ONE"], output_language="en")
        system, user = renderer.build_prompt(self.data, t)
        self.assertTrue(system.startswith("SYS"))
        self.assertIn("EXAMPLE ONE", system)
        self.assertIn("English", system)
        self.assertTrue(user.startswith("subject: girl in a white dress"))

    def test_finish_prefix_suffix_negative_and_count(self):
        result = renderer.finish('"Prompt: a girl on a beach"', self.data,
                                 target(prefix="masterpiece.", suffix="film grain.", negative_supported=True))
        self.assertEqual(result.text, "masterpiece. a girl on a beach film grain.")
        self.assertEqual(result.negative, "blurry, extra fingers")
        self.assertEqual((result.count, result.unit), (8, "語"))
        no_neg = renderer.finish("a girl", self.data, target(negative_supported=False))
        self.assertEqual(no_neg.negative, "")

    def test_tags_joined_with_comma(self):
        result = renderer.finish("1girl, beach", self.data, target(style="tags", prefix="masterpiece,", suffix="best quality"))
        self.assertEqual(result.text, "masterpiece, 1girl, beach, best quality")

    def test_over_limit_and_japanese_count(self):
        self.assertTrue(renderer.finish("one two three four five six seven eight nine ten eleven", self.data,
                                        target(max_length=10)).over_limit)
        count, unit = renderer.measure("夕暮れ の海", "ja")
        self.assertEqual((count, unit), (5, "文字"))


class GenerateTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(generate.fill_level(0), "fill_low")
        self.assertEqual(generate.fill_level(50), "fill_mid")
        self.assertEqual(generate.fill_level(100), "fill_high")
        self.assertEqual(generate.temperature_for(0), 0.3)
        self.assertEqual(generate.temperature_for(100), 0.95)

    def test_previous_subjects_passed(self):
        _, user = generate.build_prompt("海辺の女の子", "image", 50, 2, 3, ["girl A"])
        self.assertIn("variation 2 of 3", user)
        self.assertIn("girl A", user)


class NamingTest(unittest.TestCase):
    def test_unique_including_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.txt").write_text("x", encoding="utf-8")
            (folder / ".muse").mkdir()
            (folder / ".muse" / "a_2.json").write_text("{}", encoding="utf-8")
            reserved: set = set()
            first = output.free_prompt_path(folder, "a", reserved)
            second = output.free_prompt_path(folder, "a", reserved)
        self.assertEqual(first.name, "a_3.txt")
        self.assertEqual(second.name, "a_4.txt")

    def test_safe_name(self):
        self.assertEqual(files.safe_name('勇者:魔王/戦い?'), "勇者_魔王_戦い_")

    def test_utf8_without_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.txt"
            output.save_prompt(path, "日本語")
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))


class EndToEndTest(unittest.TestCase):
    """確認手順: 1 つの指示から 2 モデル × 2 本のファイルと中間表現 JSON。skip_elements と negative_supported が効く。"""

    def test_two_models_two_variations(self):
        targets, warnings = load_target_profiles()
        self.assertEqual(warnings, [])
        krea, wan = targets["Krea2"], targets["Wan2.2 I2V"]  # Krea2: ネガティブなし / Wan I2V: ネガティブあり・見た目は飛ばす
        with FakeServer(responder=responder) as server, tempfile.TemporaryDirectory() as tmp:
            client = LLMClient(LLMProfile(name="fake", endpoint=server.endpoint, model_id="qwen-9b-instruct"))
            folder = Path(tmp)
            reserved: set = set()
            previous: list[str] = []
            saved = []
            for i in (1, 2):
                data = generate.generate_one(client, "夕暮れの海辺で白いワンピースの女の子が振り返る", "video", 50, i, 2, previous)
                previous.append(data["subject"])
                for t in (krea, wan):
                    result = renderer.render(client, data, t)
                    path = output.free_prompt_path(folder, f"muse_{t.slug}_{i:02d}", reserved)
                    saved.append(output.save_prompt(path, result.text, data, result.negative))
            render_requests = [r for r in server.requests_chat() if "response_format" not in r]
            listing = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())

        self.assertEqual(listing, [
            ".muse/muse_Krea2_01.json", ".muse/muse_Krea2_02.json",
            ".muse/muse_Wan2.2I2V_01.json", ".muse/muse_Wan2.2I2V_01_neg.txt",
            ".muse/muse_Wan2.2I2V_02.json", ".muse/muse_Wan2.2I2V_02_neg.txt",
            "muse_Krea2_01.txt", "muse_Krea2_02.txt", "muse_Wan2.2I2V_01.txt", "muse_Wan2.2I2V_02.txt",
        ])
        # Wan2.2 I2V は outfit / background を渡さない。Krea2 は渡す
        krea_user = render_requests[0]["messages"][-1]["content"]
        wan_user = render_requests[1]["messages"][-1]["content"]
        self.assertIn("background: seaside at sunset", krea_user)
        self.assertNotIn("background", wan_user)
        self.assertNotIn("outfit", wan_user)
        # 2 本目には 1 本目の subject を渡して違うものにさせる
        gen_requests = [r for r in server.requests_chat() if "response_format" in r]
        self.assertIn("girl in a white dress", gen_requests[1]["messages"][-1]["content"])
        self.assertIsNone(saved[0].negative)
        self.assertIsNotNone(saved[1].negative)


if __name__ == "__main__":
    unittest.main()
