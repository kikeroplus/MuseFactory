import tempfile
import unittest
from pathlib import Path

from app.core.config import LLMProfile
from app.core.llm_client import (LLMClient, LLMConnectionError, LLMJSONError, LLMTimeoutError,
                                 parse_json, strip_think)
from app.util import log
from tests.fake_server import FakeServer, dead_url


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


def profile(endpoint: str, **kw) -> LLMProfile:
    return LLMProfile(name="test", endpoint=endpoint, model_id="qwen-9b-instruct", **kw)


class StripThinkTest(unittest.TestCase):
    def test_removes_block(self):
        self.assertEqual(strip_think("<think>a\nb</think>\n\nanswer"), "answer")

    def test_case_and_multiple(self):
        self.assertEqual(strip_think("<THINK>x</THINK>one <think>y</think>two"), "one two")

    def test_unclosed_think_is_dropped(self):
        self.assertEqual(strip_think("answer<think>cut off in the mid"), "answer")

    def test_close_without_open(self):
        # テンプレートが <think> を先に入れるモデルは、閉じタグだけが返る
        self.assertEqual(strip_think("reasoning here...</think>\nanswer"), "answer")

    def test_plain(self):
        self.assertEqual(strip_think("  plain  "), "plain")


class ParseJsonTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_text_around(self):
        self.assertEqual(parse_json('Here it is:\n{"a": [1, 2]}\nHope this helps.'), {"a": [1, 2]})

    def test_trailing_commas(self):
        self.assertEqual(parse_json('{"a": [1, 2,], "b": {"c": "x",},}'), {"a": [1, 2], "b": {"c": "x"}})

    def test_comma_inside_string_kept(self):
        self.assertEqual(parse_json('{"a": "x, }",}'), {"a": "x, }"})

    def test_array(self):
        self.assertEqual(parse_json('```\n[{"no": 1},]\n```'), [{"no": 1}])

    def test_broken(self):
        with self.assertRaises(ValueError):
            parse_json('{"a": ')


class ClientTest(unittest.TestCase):
    def test_chat_strips_think(self):
        with FakeServer(replies=["<think>hmm</think>OK"]) as server:
            result = LLMClient(profile(server.endpoint)).chat("sys", "hi")
        self.assertEqual(result.text, "OK")
        self.assertIn("<think>", result.raw)

    def test_keep_think(self):
        with FakeServer(replies=["<think>hmm</think>OK"]) as server:
            result = LLMClient(profile(server.endpoint, thinking="keep")).chat("sys", "hi")
        self.assertIn("<think>", result.text)

    def test_json_schema_sent(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        with FakeServer(replies=['{"a": "x"}']) as server:
            data, _ = LLMClient(profile(server.endpoint)).chat_json("sys", "hi", schema)
            sent = server.requests_chat()[0]
        self.assertEqual(data, {"a": "x"})
        self.assertEqual(sent["response_format"]["json_schema"]["schema"], schema)

    def test_no_schema_support_puts_rule_in_system(self):
        schema = {"type": "object"}
        with FakeServer(replies=['{"a": "x"}']) as server:
            LLMClient(profile(server.endpoint, supports_json_schema=False)).chat_json("sys", "hi", schema)
            sent = server.requests_chat()[0]
        self.assertNotIn("response_format", sent)
        self.assertIn("JSON", sent["messages"][0]["content"])

    def test_json_repaired_without_retry(self):
        with FakeServer(replies=['<think>x</think>```json\n{"a": 1,}\n```']) as server:
            data, _ = LLMClient(profile(server.endpoint)).chat_json("sys", "hi", {"type": "object"})
            self.assertEqual(len(server.requests_chat()), 1)
        self.assertEqual(data, {"a": 1})

    def test_json_retry_once_then_success(self):
        with FakeServer(replies=["not json at all", '{"a": 2}']) as server:
            data, _ = LLMClient(profile(server.endpoint)).chat_json("sys", "hi", {"type": "object"})
            self.assertEqual(len(server.requests_chat()), 2)
        self.assertEqual(data, {"a": 2})

    def test_json_fails_after_one_retry(self):
        with FakeServer(replies=["nope"]) as server:
            with self.assertRaises(LLMJSONError) as ctx:
                LLMClient(profile(server.endpoint)).chat_json("sys", "hi", {"type": "object"})
            self.assertEqual(len(server.requests_chat()), 2)
        self.assertEqual(ctx.exception.raw, "nope")

    def test_merge_user_and_no_think(self):
        with FakeServer() as server:
            LLMClient(profile(server.endpoint, system_prompt_mode="merge_user", thinking="off")).chat("RULE", "INPUT")
            sent = server.requests_chat()[0]["messages"]
        self.assertEqual(sent[0], {"role": "system", "content": "RULE"})
        self.assertIn("RULE", sent[1]["content"])
        self.assertIn("INPUT", sent[1]["content"])
        self.assertTrue(sent[1]["content"].endswith("/no_think"))

    def test_connection_error(self):
        with self.assertRaises(LLMConnectionError):
            LLMClient(profile(dead_url(), timeout_sec=3)).chat("", "hi")

    def test_timeout_error(self):
        with FakeServer(delay=1.5) as server:
            with self.assertRaises(LLMTimeoutError):
                LLMClient(profile(server.endpoint, timeout_sec=0.3)).chat("", "hi")

    def test_connection_test(self):
        with FakeServer(models=["qwen-9b-instruct", "other"]) as server:
            models, result = LLMClient(profile(server.endpoint)).test_connection()
        self.assertEqual(models, ["other", "qwen-9b-instruct"])
        self.assertEqual(result.text, "OK")


if __name__ == "__main__":
    unittest.main()
