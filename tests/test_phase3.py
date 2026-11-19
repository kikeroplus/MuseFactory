"""フェーズ3: 抽出・加工の中身 (LLM は偽のサーバー)。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.core import intermediate, library, output
from app.core.config import LLMProfile
from app.core.job_queue import CANCELLED, DONE, Job, JobQueue
from app.core.llm_client import LLMClient
from app.core.tasks import process
from app.core.tasks.extract import Extracted, add_to_library, write_per_element, write_wildcards
from app.ui.common import diff_ranges
from app.util import files, log
from tests.fake_server import FakeServer

PROMPTS = {
    "a.txt": "1girl, standing, smiling, school uniform, classroom",
    "b.txt": "少女が腕を組んで立つ、笑顔、制服",
    "c.txt": "a knight kneeling, solemn face, castle hall",
}
DECOMPOSED = {
    "a.txt": {"subject": "1girl", "pose": "standing", "expression": "smiling", "outfit": "school uniform",
              "background": "classroom"},
    "b.txt": {"subject": "少女", "pose": "腕を組んで立つ", "expression": "笑顔", "outfit": "制服"},
    "c.txt": {"subject": "a knight", "pose": "kneeling", "expression": "solemn face", "background": "castle hall"},
}


def responder(payload):
    system = payload["messages"][0]["content"] if payload["messages"][0]["role"] == "system" else ""
    user = payload["messages"][-1]["content"]
    if system.startswith("You split"):
        for name, text in PROMPTS.items():
            if text in user:
                return json.dumps(DECOMPOSED[name], ensure_ascii=False)
        return json.dumps({"subject": "x"})
    if system.startswith("You translate"):
        return "A girl standing with arms crossed, smiling, school uniform"
    if system.startswith("You improve"):
        data = json.loads(user)
        data.update({"lighting": "soft window light", "camera": "medium shot"})
        return json.dumps(data)
    if system.startswith("You turn a still"):
        data = json.loads(user)
        data.update({"motion": "she waves", "camera_motion": "slow push in",
                     "timeline": [{"start": 0, "end": 3, "action": "smiles"}, {"start": 3, "end": 99, "action": "waves"}]})
        return json.dumps(data)
    if system.startswith("You write negative"):
        return "blurry, extra fingers, dark night"
    return "A rendered prompt."


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        for name, text in PROMPTS.items():
            (self.folder / name).write_text(text, encoding="utf-8")
        self.server = FakeServer(responder=responder).__enter__()
        self.client = LLMClient(LLMProfile(name="fake", endpoint=self.server.endpoint, model_id="qwen-9b-instruct"))

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()


class ExtractTest(Base):
    """確認手順: 3 ファイルからポーズと表情を抽出し、両方の形式で出せる。"""

    def decompose_all(self):
        out = []
        for name in PROMPTS:
            path = self.folder / name
            out.append(Extracted(path, process.decompose(self.client, files.read_text(path), path)))
        return out

    def test_three_files_both_formats(self):
        results = self.decompose_all()
        self.assertEqual(len(self.server.requests_chat()), 3)  # 1 ファイル 1 リクエスト
        self.assertEqual(results[1].data["lang"], "ja")
        out = self.folder / "extract"
        per = write_per_element(results, ["pose", "expression"], out)
        wild = write_wildcards(results, ["pose", "expression"], out)
        self.assertEqual(sorted(p.name for p in per), [
            "a_extract_expression.txt", "a_extract_pose.txt", "b_extract_expression.txt", "b_extract_pose.txt",
            "c_extract_expression.txt", "c_extract_pose.txt"])
        self.assertEqual((out / "pose.txt").read_text(encoding="utf-8"), "standing\n腕を組んで立つ\nkneeling\n")
        self.assertEqual([p.name for p in wild], ["pose.txt", "expression.txt"])
        # もう一度書き出しても上書きしない
        again = write_wildcards(results, ["pose"], out)
        self.assertEqual(again[0].name, "pose_2.txt")

    def test_wildcard_dedupes_exact(self):
        r = [Extracted(Path("x.txt"), {"pose": "standing"}), Extracted(Path("y.txt"), {"pose": "standing"}),
             Extracted(Path("z.txt"), {"pose": "Standing"})]
        path = write_wildcards(r, ["pose"], self.folder / "w")[0]
        self.assertEqual(path.read_text(encoding="utf-8"), "standing\nStanding\n")

    def test_library(self):
        lib = self.folder / "lib"
        results = self.decompose_all()
        counts = add_to_library(results, ["pose"], lib)
        self.assertEqual(counts, {"pose": 3})
        self.assertEqual(add_to_library(results, ["pose"], lib), {"pose": 0})  # 重なりは足さない
        items = library.load("pose", lib)
        self.assertEqual(items[0]["source"], str(self.folder / "a.txt"))
        self.assertFalse(items[0]["favorite"])

    def test_existing_intermediate_used(self):
        prompt = self.folder / "made.txt"
        data = intermediate.normalize({"subject": "hero", "pose": "kneeling"})
        output.save_prompt(prompt, "hero kneeling", data)
        self.assertEqual(process.existing_intermediate(prompt)["pose"], "kneeling")
        self.assertIsNone(process.existing_intermediate(self.folder / "a.txt"))


class ProcessTest(Base):
    def test_translate(self):
        text = files.read_text(self.folder / "b.txt")
        self.assertEqual(process.translate_text(self.client, text), "A girl standing with arms crossed, smiling, school uniform")
        sent = self.server.requests_chat()[0]
        self.assertEqual(sent["messages"][-1]["content"], text)

    def test_translate_keeps_japanese_copy(self):
        # 加工タブの保存と同じ置き方: (名前)_en.txt と .muse/(名前)_en_ja.txt
        path = output.free_prompt_path(self.folder, "b_en")
        output.save_prompt(path, "A girl standing")
        files.write_text_atomic(output.japanese_path(path), PROMPTS["b.txt"] + "\n")
        self.assertTrue((self.folder / "b_en.txt").is_file())
        self.assertEqual((self.folder / ".muse" / "b_en_ja.txt").read_text(encoding="utf-8").strip(), PROMPTS["b.txt"])
        self.assertEqual((self.folder / "b.txt").read_text(encoding="utf-8"), PROMPTS["b.txt"])  # 元はそのまま

    def test_enhance_fills_empty(self):
        data = intermediate.normalize(DECOMPOSED["a.txt"])
        enhanced = process.enhance(self.client, data)
        self.assertEqual(enhanced["lighting"], "soft window light")
        self.assertEqual(enhanced["id"], data["id"])
        self.assertEqual(enhanced["pose"], "standing")

    def test_to_video_timeline_clipped(self):
        data = intermediate.normalize(DECOMPOSED["a.txt"])
        video = process.to_video(self.client, data, 5)
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["motion"], "she waves")
        self.assertEqual(video["timeline"][-1], {"start": 3.0, "end": 5.0, "action": "waves"})
        system = self.server.requests_chat()[0]["messages"][0]["content"]
        self.assertIn("5 seconds", system)
        self.assertEqual(process.amount_for(3), "short")
        self.assertEqual(process.amount_for(10), "long")

    def test_negative(self):
        data = intermediate.normalize(DECOMPOSED["c.txt"])
        self.assertEqual(process.make_negative(self.client, data), "blurry, extra fingers, dark night")


class CancelTest(unittest.TestCase):
    """確認手順: 途中のキャンセルが効く (送った 1 件は返事を待って捨て、残りは送らない)。"""

    def test_cancel_extract_batch(self):
        with tempfile.TemporaryDirectory() as tmp, FakeServer(responder=responder, delay=0.5) as server:
            folder = Path(tmp)
            for name, text in PROMPTS.items():
                (folder / name).write_text(text, encoding="utf-8")
            profile = LLMProfile(name="fake", endpoint=server.endpoint, model_id="qwen-9b-instruct")

            class Router:
                def resolve(self, task):
                    return profile

                def invalidate(self, name=None):
                    pass

            started = threading.Event()

            def make(path):
                def run(p, token):
                    token.check()
                    started.set()
                    return process.decompose(LLMClient(p), files.read_text(path), path)
                return run

            queue = JobQueue(Router())
            batch = queue.submit([Job("extract", n, make(folder / n)) for n in PROMPTS])
            self.assertTrue(started.wait(5))
            queue.cancel(batch)
            self.assertTrue(queue.wait_idle(10))
            self.assertEqual([j.status for j in batch.jobs], [CANCELLED] * 3)
            self.assertEqual(len(server.requests_chat()), 1)
            queue.close()


class DiffTest(unittest.TestCase):
    def test_ranges(self):
        removed, added = diff_ranges("a girl standing", "a girl sitting quietly")
        self.assertEqual(["a girl standing"[s:e] for s, e in removed], ["standing"])
        self.assertEqual(["a girl sitting quietly"[s:e] for s, e in added], ["sitting quietly"])

    def test_japanese(self):
        removed, added = diff_ranges("少女 笑顔", "少女 泣き顔")
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(added), 1)


if __name__ == "__main__":
    unittest.main()
