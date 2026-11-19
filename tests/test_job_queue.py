import tempfile
import threading
import unittest
from pathlib import Path

from app.core.config import LLMProfile
from app.core.job_queue import CANCELLED, DONE, FAILED, Job, JobQueue, group_by_profile
from app.core.llm_client import LLMConnectionError, LLMError
from app.core.router import NoLLMAvailable
from app.util import log


def setUpModule():
    global _logdir
    _logdir = tempfile.TemporaryDirectory()
    log.set_dir(Path(_logdir.name))


def tearDownModule():
    log.set_dir(None)
    _logdir.cleanup()


class FakeRouter:
    """task 名 → プロファイル名の表で答える。down に入れた名前は使えない扱い。"""

    def __init__(self, table):
        self.table = table  # task -> [names]
        self.down: set[str] = set()
        self.invalidated: list[str] = []

    def resolve(self, task):
        for name in self.table.get(task, []):
            if name not in self.down:
                return LLMProfile(name=name, endpoint="http://x/v1", model_id="m")
        raise NoLLMAvailable(task, [])

    def invalidate(self, name=None):
        self.invalidated.append(name)


class Recorder:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event, data):
        with self.lock:
            self.events.append((event, data))

    def names(self):
        return [e for e, _ in self.events]


class GroupTest(unittest.TestCase):
    def test_group_keeps_first_seen_order(self):
        jobs = [Job("t", str(i), lambda p, t: None, profile_name=n) for i, n in enumerate("ABABCA")]
        self.assertEqual([j.profile_name for j in group_by_profile(jobs)], list("AAABBC"))
        self.assertEqual([j.label for j in group_by_profile(jobs)], ["0", "2", "5", "1", "3", "4"])


class QueueTest(unittest.TestCase):
    def test_runs_serially_grouped_by_profile(self):
        router = FakeRouter({"small": ["9B"], "big": ["14B"]})
        order, running, overlap = [], [0], []
        lock = threading.Lock()

        def work(label):
            def run(profile, token):
                with lock:
                    running[0] += 1
                    overlap.append(running[0])
                order.append((label, profile.name))
                with lock:
                    running[0] -= 1
                return label
            return run

        tasks = ["small", "big", "small", "big"]
        jobs = [Job(t, f"j{i}", work(f"j{i}")) for i, t in enumerate(tasks)]
        rec = Recorder()
        q = JobQueue(router, rec)
        batch = q.submit(jobs)
        self.assertTrue(q.wait_idle())
        self.assertEqual(order, [("j0", "9B"), ("j2", "9B"), ("j1", "14B"), ("j3", "14B")])
        self.assertEqual(max(overlap), 1)
        self.assertTrue(all(j.status == DONE for j in batch.jobs))
        self.assertEqual(rec.names().count("batch_finished"), 1)
        q.close()

    def test_failure_is_skipped_and_listed(self):
        router = FakeRouter({"t": ["A"]})

        def bad(profile, token):
            raise LLMError("壊れた返事")

        jobs = [Job("t", "ok1", lambda p, t: 1), Job("t", "bad", bad), Job("t", "ok2", lambda p, t: 2)]
        q = JobQueue(router)
        batch = q.submit(jobs)
        q.wait_idle()
        self.assertEqual([j.status for j in batch.jobs], [DONE, FAILED, DONE])
        self.assertEqual([j.label for j in batch.failed], ["bad"])
        self.assertIn("壊れた返事", batch.failed[0].error)
        q.close()

    def test_cancel_midway(self):
        router = FakeRouter({"t": ["A"]})
        started = threading.Event()
        release = threading.Event()

        def slow(profile, token):
            started.set()
            release.wait(5)
            return "late"

        jobs = [Job("t", "first", slow)] + [Job("t", f"j{i}", lambda p, t: 1) for i in range(5)]
        rec = Recorder()
        q = JobQueue(router, rec)
        batch = q.submit(jobs)
        self.assertTrue(started.wait(5))
        q.cancel(batch)
        release.set()
        self.assertTrue(q.wait_idle())
        self.assertTrue(all(j.status == CANCELLED for j in batch.jobs))
        self.assertEqual(rec.names().count("batch_finished"), 1)
        q.close()

    def test_connection_error_falls_back_once(self):
        router = FakeRouter({"t": ["A", "B"]})
        used = []

        def run(profile, token):
            used.append(profile.name)
            if profile.name == "A":
                router.down.add("A")
                raise LLMConnectionError("切れた")
            return "ok"

        q = JobQueue(router)
        batch = q.submit([Job("t", "j", run)])
        q.wait_idle()
        self.assertEqual(used, ["A", "B"])
        self.assertEqual(batch.jobs[0].status, DONE)
        self.assertEqual(batch.jobs[0].profile_name, "B")
        self.assertIn("A", router.invalidated)
        q.close()

    def test_fixed_profile_does_not_fall_back(self):
        router = FakeRouter({"t": ["A", "B"]})
        fixed = LLMProfile(name="X", endpoint="http://x/v1", model_id="m")
        used = []

        def run(profile, token):
            used.append(profile.name)
            raise LLMConnectionError("切れた")

        q = JobQueue(router)
        batch = q.submit([Job("t", "j", run, fixed_profile=fixed)])
        q.wait_idle()
        self.assertEqual(used, ["X"])
        self.assertEqual(batch.jobs[0].status, FAILED)
        q.close()

    def test_no_llm_fails_job(self):
        q = JobQueue(FakeRouter({}))
        batch = q.submit([Job("t", "j", lambda p, t: 1)])
        q.wait_idle()
        self.assertEqual(batch.jobs[0].status, FAILED)
        q.close()


if __name__ == "__main__":
    unittest.main()
