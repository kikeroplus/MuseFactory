"""フェーズ5: PNG のメタデータ・ライブラリ・プロファイル管理・画像からプロンプト。"""

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from PySide6.QtGui import QColor, QGuiApplication, QImage

from app.core import library, profiles_io
from app.core.config import LLMProfile
from app.core.llm_client import LLMClient
from app.core.tasks import vision
from app.util import files, log, png_meta
from tests.fake_server import FakeServer

_app = QGuiApplication.instance() or QGuiApplication([])

GRAPH = {
    "3": {"class_type": "KSampler", "inputs": {"positive": ["6", 0], "negative": ["7", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a girl in a white dress on a beach at sunset"},
          "_meta": {"title": "CLIP Text Encode (Prompt)"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality, extra fingers and very long negative"},
          "_meta": {"title": "CLIP Text Encode"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}},
}


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_png(path: Path, extra_chunks: list[bytes]):
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    image.save(str(path), "PNG")
    raw = path.read_bytes()
    iend = raw.rfind(b"IEND") - 4
    path.write_bytes(raw[:iend] + b"".join(extra_chunks) + raw[iend:])


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


class PngTest(unittest.TestCase):
    def test_comfy_prompt_skips_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            make_png(path, [chunk(b"tEXt", b"prompt\x00" + json.dumps(GRAPH).encode("utf-8"))])
            self.assertEqual(png_meta.prompt_text(path), "a girl in a white dress on a beach at sunset")
            self.assertEqual(files.read_prompt_source(path), "a girl in a white dress on a beach at sunset")

    def test_utf8_in_text_and_itxt(self):
        graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "夕暮れの海辺の少女"}}}
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.png"
            make_png(a, [chunk(b"tEXt", b"prompt\x00" + json.dumps(graph, ensure_ascii=False).encode("utf-8"))])
            self.assertEqual(png_meta.prompt_text(a), "夕暮れの海辺の少女")
            b = Path(tmp) / "b.png"
            body = "1girl, smile\nNegative prompt: bad hands\nSteps: 20".encode("utf-8")
            make_png(b, [chunk(b"iTXt", b"parameters\x00\x01\x00\x00\x00" + zlib.compress(body))])
            self.assertEqual(png_meta.prompt_text(b), "1girl, smile")

    def test_no_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.png"
            make_png(path, [])
            self.assertIsNone(png_meta.prompt_text(path))
            with self.assertRaises(ValueError):
                files.read_prompt_source(path)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        library.add("pose", [("standing", "a.txt"), ("kneeling", "b.txt"), ("arms crossed", "c.txt")], self.folder)
        library.add("background", [("beach at sunset", "a.txt"), ("castle hall", "c.txt")], self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_favorite_remove_export(self):
        self.assertEqual(library.categories(self.folder), ["background", "pose"])
        items = library.load("pose", self.folder)
        self.assertEqual(library.search(items, "ING"), [0, 1])
        library.set_favorite("pose", [1], True, self.folder)
        items = library.load("pose", self.folder)
        self.assertEqual(library.search(items, "", favorites_only=True), [1])
        self.assertEqual(library.remove("pose", [0], self.folder), 1)
        items = library.load("pose", self.folder)
        self.assertEqual([i["text"] for i in items], ["kneeling", "arms crossed"])
        path = library.export_wildcard(items, self.folder / "out" / "pose.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), "kneeling\narms crossed\n")
        self.assertEqual(library.export_wildcard(items, self.folder / "out" / "pose.txt").name, "pose_2.txt")

    def test_mix(self):
        pools = {c: [i["text"] for i in library.load(c, self.folder)] for c in ("pose", "background")}
        results = library.mix({"pose": None, "background": "castle hall", "characters": "a knight"}, pools, 3, seed=1)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["background"] == "castle hall" and r["subject"] == "a knight" for r in results))
        self.assertEqual(len({r["pose"] for r in results}), 3)  # 3 通りあるので重ならない
        self.assertEqual(results[0]["source"]["type"], "library")


class ProfilesTest(unittest.TestCase):
    def test_validate(self):
        ok = profiles_io.validate(profiles_io.TARGET, json.dumps(profiles_io.TARGET_TEMPLATE))
        self.assertEqual(ok["name"], "新しいモデル")
        with self.assertRaisesRegex(ValueError, "JSON"):
            profiles_io.validate(profiles_io.TARGET, "{oops")
        bad = dict(profiles_io.TARGET_TEMPLATE, element_order=["subject", "hair"])
        with self.assertRaisesRegex(ValueError, "hair"):
            profiles_io.validate(profiles_io.TARGET, json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "thinking"):
            profiles_io.validate(profiles_io.LLM, json.dumps(dict(profiles_io.LLM_TEMPLATE, thinking="maybe")))

    def test_save_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = profiles_io.save(profiles_io.TARGET, dict(profiles_io.TARGET_TEMPLATE, name="Krea2"), folder=folder)
            self.assertEqual(path.name, "krea2.json")
            copy = profiles_io.duplicate(profiles_io.TARGET, path, folder)
            self.assertEqual(files.read_json(copy)["name"], "Krea2 のコピー")
            again = profiles_io.duplicate(profiles_io.TARGET, path, folder)
            self.assertEqual(files.read_json(again)["name"], "Krea2 のコピー (2)")

    def test_add_example_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            files.write_json(path, dict(profiles_io.TARGET_TEMPLATE, example_prompts=["one", "two"]))
            self.assertEqual(profiles_io.add_example(path, "three", limit=3), (True, 0))
            self.assertEqual(profiles_io.add_example(path, "three", limit=3), (False, 0))  # 同じ文は足さない
            self.assertEqual(profiles_io.add_example(path, "four", limit=3), (False, 1))  # 上限: 聞く
            self.assertEqual(profiles_io.add_example(path, "four", limit=3, drop_oldest=True), (True, 1))
            self.assertEqual(files.read_json(path)["example_prompts"], ["two", "three", "four"])

    def test_available_models(self):
        # 素の OpenAI 互換サーバー (ID だけ) でも読める。埋め込み用は外す
        with FakeServer(models=["b/qwen-9b", "a-gemma-4", "text-embedding-nomic"]) as server:
            models = profiles_io.available_models(server.endpoint)
        self.assertEqual([m["id"] for m in models], ["a-gemma-4", "b/qwen-9b"])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            profiles_io.save(profiles_io.LLM, dict(profiles_io.LLM_TEMPLATE, name="Qwen 9B @LLM機",
                                                   endpoint="http://h:1234/v1", model_id="qwen/qwen3.5-9b"), folder=folder)
            self.assertEqual(profiles_io.registered_models(folder), {("http://h:1234/v1", "qwen/qwen3.5-9b"): "Qwen 9B @LLM機"})
            vlm = {"id": "google/gemma-4-e4b", "type": "vlm", "arch": "gemma4", "max_context": 131072, "loaded": False}
            data = profiles_io.profile_from_model(vlm, "http://h:1234/v1/", folder)
            self.assertEqual(data["name"], "gemma-4-e4b @LLM機")  # 同じサーバーの「@…」を使う
            self.assertEqual(data["endpoint"], "http://h:1234/v1")
            self.assertTrue(data["supports_vision"])
            self.assertEqual(data["system_prompt_mode"], "merge_user")
            self.assertEqual(data["context_length"], 8192)
            small = profiles_io.profile_from_model({"id": "gemma-2", "type": "llm", "arch": "gemma2", "max_context": 4096,
                                                    "loaded": False}, "http://other:1234/v1", folder)
            self.assertEqual(small["name"], "gemma-2 @other")
            self.assertFalse(small["supports_vision"])
            self.assertEqual(small["context_length"], 4096)
            profiles_io.validate(profiles_io.LLM, json.dumps(data))

    def test_draft(self):
        reply = dict(profiles_io.TARGET_TEMPLATE, name="Flux Pro", type="image", element_order=["subject", "hair", "style"],
                     example_prompts=["A cat on a sofa."])
        with FakeServer(responder=lambda p: json.dumps(reply)) as server:
            client = LLMClient(LLMProfile(name="fake", endpoint=server.endpoint, model_id="qwen-9b-instruct"))
            data = profiles_io.draft_target(client, "Use natural sentences...", "")
            sent = server.requests_chat()[0]["messages"][-1]["content"]
        self.assertIn("Use natural sentences", sent)
        self.assertEqual(data["name"], "Flux Pro")
        self.assertEqual(data["element_order"], ["subject", "style"])  # 知らない要素は落とす
        profiles_io.validate(profiles_io.TARGET, json.dumps(data))


class VisionTest(unittest.TestCase):
    def test_image_sent_and_parsed(self):
        reply = {"subject": "a cat", "pose": "sitting", "expression": "", "outfit": "", "background": "sofa",
                 "lighting": "soft", "camera": "close-up", "style": "photo", "extra": "", "negative": ""}
        with tempfile.TemporaryDirectory() as tmp, FakeServer(responder=lambda p: json.dumps(reply)) as server:
            path = Path(tmp) / "big.png"
            image = QImage(3000, 1500, QImage.Format.Format_RGB32)
            image.fill(QColor("#aa8844"))
            image.save(str(path))
            url = vision.image_data_url(path)
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            client = LLMClient(LLMProfile(name="v", endpoint=server.endpoint, model_id="m", supports_vision=True))
            data = vision.image_to_intermediate(client, path)
            content = server.requests_chat()[0]["messages"][-1]["content"]
        self.assertEqual(data["subject"], "a cat")
        self.assertEqual(content[1]["type"], "image_url")
        no_vision = LLMClient(LLMProfile(name="t", endpoint="http://x/v1", model_id="m"))
        with self.assertRaisesRegex(ValueError, "画像を見られません"):
            vision.image_to_intermediate(no_vision, path)


if __name__ == "__main__":
    unittest.main()
