import tempfile
import unittest
from pathlib import Path

from app.core.config import LLMProfile
from app.core.router import NoLLMAvailable, Router
from app.util import log
from tests.fake_server import FakeServer, dead_url


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


def prof(name, endpoint, model="qwen-9b-instruct"):
    return LLMProfile(name=name, endpoint=endpoint, model_id=model)


def settings(routing, hosts=None):
    return {"task_routing": routing, "comfyui_hosts": hosts or {}, "router_cache_sec": 30,
            "health_timeout_sec": 1}


class RouterTest(unittest.TestCase):
    def test_fallback_from_dead_endpoint(self):
        with FakeServer() as alive:
            profiles = {"dead": prof("dead", dead_url()), "alive": prof("alive", alive.endpoint)}
            router = Router(settings({"render": ["dead", "alive"]}), profiles)
            self.assertEqual(router.resolve("render").name, "alive")
            states = dict(router.explain("render"))
        self.assertFalse(states["dead"].ok)
        # Windows では閉じたポートへの接続も拒否まで 2 秒ほどかかるので、タイムアウトになることもある
        self.assertRegex(states["dead"].reason, "接続できません|返事がありません")

    def test_unknown_profile_and_missing_model_skipped(self):
        with FakeServer(models=["other-model"]) as a, FakeServer() as b:
            profiles = {"nomodel": prof("nomodel", a.endpoint), "ok": prof("ok", b.endpoint)}
            router = Router(settings({"render": ["missing", "nomodel", "ok"]}), profiles)
            self.assertEqual(router.resolve("render").name, "ok")
            reasons = {n: h.reason for n, h in router.explain("render")}
        self.assertIn("ありません", reasons["missing"])
        self.assertIn("qwen-9b-instruct", reasons["nomodel"])

    def test_comfy_busy_skipped(self):
        with FakeServer() as llm1, FakeServer() as llm2, FakeServer(queue_running=[["job"]]) as comfy:
            profiles = {"pc1": prof("pc1", llm1.endpoint), "main": prof("main", llm2.endpoint)}
            router = Router(settings({"story_plan": ["pc1", "main"]}, {"pc1": comfy.url}), profiles)
            self.assertEqual(router.resolve("story_plan").name, "main")
            comfy.queue_running = []
            router.invalidate()
            self.assertEqual(router.resolve("story_plan").name, "pc1")

    def test_comfy_unreachable_does_not_block(self):
        with FakeServer() as llm:
            profiles = {"pc1": prof("pc1", llm.endpoint)}
            router = Router(settings({"render": ["pc1"]}, {"pc1": dead_url()}), profiles)
            self.assertEqual(router.resolve("render").name, "pc1")

    def test_all_down(self):
        profiles = {"a": prof("a", dead_url()), "b": prof("b", dead_url())}
        router = Router(settings({"render": ["a", "b"]}), profiles)
        with self.assertRaises(NoLLMAvailable) as ctx:
            router.resolve("render")
        self.assertEqual([n for n, _ in ctx.exception.reasons], ["a", "b"])

    def test_unknown_task_uses_render(self):
        with FakeServer() as llm:
            router = Router(settings({"render": ["x"]}), {"x": prof("x", llm.endpoint)})
            self.assertEqual(router.resolve("negative").name, "x")

    def test_cache(self):
        calls = []
        now = [0.0]

        def models(profile, timeout):
            calls.append(profile.name)
            return [profile.model_id]

        router = Router(settings({"render": ["x"]}), {"x": prof("x", "http://unused/v1")},
                        models_fn=models, clock=lambda: now[0])
        router.resolve("render")
        now[0] = 29
        router.resolve("render")
        self.assertEqual(len(calls), 1)
        now[0] = 31
        router.resolve("render")
        self.assertEqual(len(calls), 2)
        router.invalidate("x")
        router.resolve("render")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
