"""LLM へのリクエストを 1 件ずつ順に送るジョブキュー (SPEC 4.3)。

- ワーカーは 1 本だけ。LLM 機は他の PC からも使われるので、同時には送らない
- まとめて足したジョブは、使う LLM プロファイルごとにまとめて並べ替える (8GB VRAM はモデルの入れ替えが遅いため)
- 進み具合 (n/N)・キャンセル・失敗したジョブは飛ばして一覧に残す
- Qt に頼らない。知らせは listener(event, data) で、ワーカーのスレッドから呼ぶ
  (画面側は Qt の Signal に渡して、画面のスレッドで受ける: app/ui/jobs_bridge.py)

ジョブの中身 run(profile, token) は、使う LLM プロファイルとキャンセルの札を受け取る。
1 つのジョブで LLM を何回か呼ぶときは、呼ぶ前に token.check() を呼ぶ (キャンセルならそこで止まる)。
送ってしまったリクエストは途中で止められないので、キャンセルしたときは返事を待ってから捨てる。
"""

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..util import log
from .config import LLMProfile
from .llm_client import LLMConnectionError, LLMError, LLMTimeoutError

WAITING, RUNNING, DONE, FAILED, CANCELLED = "待機", "実行中", "完了", "失敗", "キャンセル"


class Cancelled(Exception):
    """token.check() がキャンセルを見つけた。"""


class CancelToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


_ids = itertools.count(1)


@dataclass
class Job:
    task: str  # タスク割り当ての名前 (extract / render など)
    label: str  # 画面に出す名前 (ファイル名など)
    run: Callable[[LLMProfile, CancelToken], Any]
    id: int = field(default_factory=lambda: next(_ids))
    profile_name: str = ""  # 実際に使った (使う予定の) LLM プロファイル
    status: str = WAITING
    result: Any = None
    error: str = ""
    elapsed: float = 0.0
    batch: Optional["Batch"] = field(default=None, repr=False)
    # 割り当てを使わず、この LLM プロファイルに送る (接続テスト・LLM 比較)。つながらなくてもほかに回さない
    fixed_profile: Optional[LLMProfile] = field(default=None, repr=False)


@dataclass
class Batch:
    name: str
    jobs: list[Job]
    id: int = field(default_factory=lambda: next(_ids))
    token: CancelToken = field(default_factory=CancelToken)
    reported: bool = False  # batch_finished を知らせたか
    quiet: bool = False  # 失敗の一覧を画面全体には出さない (頼んだ画面が自分で出す)

    @property
    def total(self) -> int:
        return len(self.jobs)

    @property
    def finished(self) -> int:
        return sum(1 for j in self.jobs if j.status in (DONE, FAILED, CANCELLED))

    @property
    def failed(self) -> list[Job]:
        return [j for j in self.jobs if j.status == FAILED]


def group_by_profile(jobs: list[Job]) -> list[Job]:
    """同じ LLM プロファイルのジョブを続けて並べる (グループの順は最初に出てきた順。中の順は保つ)。"""
    order: dict[str, int] = {}
    for job in jobs:
        order.setdefault(job.profile_name, len(order))
    return sorted(jobs, key=lambda j: order[j.profile_name])  # sorted は安定


Listener = Callable[[str, dict], None]


class JobQueue:
    def __init__(self, router, listener: Optional[Listener] = None):
        self.router = router
        self.listener = listener
        self._pending: deque[Job] = deque()
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self.current: Optional[Job] = None
        self._closing = False

    # ------------------------------------------------------------ 外から呼ぶもの

    def submit(self, jobs: list[Job], name: str = "", quiet: bool = False) -> Batch:
        """ジョブをまとめて足す。採用される LLM プロファイルごとに並べ替えてから並べる。"""
        batch = Batch(name or (jobs[0].label if len(jobs) == 1 else f"{len(jobs)} 件"), list(jobs))
        batch.quiet = quiet
        for job in jobs:
            job.batch = batch
            job.profile_name = job.fixed_profile.name if job.fixed_profile else self._planned_profile(job.task)
        batch.jobs = group_by_profile(batch.jobs)
        with self._cond:
            self._pending.extend(batch.jobs)
            self._ensure_worker()
            self._cond.notify_all()
        self._emit("batch_added", batch=batch)
        return batch

    def cancel(self, batch: Optional[Batch] = None) -> None:
        """batch (なければすべて) を取り消す。待機中のものは捨て、実行中のものは返事を待って捨てる。"""
        cancelled: list[Job] = []
        with self._cond:
            batches = {id(j.batch): j.batch for j in self._pending}
            if self.current is not None:
                batches[id(self.current.batch)] = self.current.batch
            for b in batches.values():
                if b is not None and (batch is None or b is batch):
                    b.token.cancel()
            keep = deque()
            for job in self._pending:
                if job.batch is not None and job.batch.token.cancelled:
                    job.status = CANCELLED
                    cancelled.append(job)
                else:
                    keep.append(job)
            self._pending = keep
        for job in cancelled:
            self._emit("job_finished", job=job)
        for b in {id(j.batch): j.batch for j in cancelled}.values():
            self._finish_batch_if_done(b)
        if cancelled and self.pending_count() == 0:
            self._emit("idle")

    def pending_count(self) -> int:
        with self._cond:
            return len(self._pending) + (1 if self.current else 0)

    def wait_idle(self, timeout: float = 10) -> bool:
        """テスト用: 空になるまで待つ。"""
        end = time.monotonic() + timeout
        with self._cond:
            while self._pending or self.current is not None:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def close(self) -> None:
        self.cancel()
        with self._cond:
            self._closing = True
            self._cond.notify_all()

    # ------------------------------------------------------------ 中

    def _emit(self, event: str, **data) -> None:
        if self.listener is not None:
            try:
                self.listener(event, data)
            except Exception as exc:  # noqa: BLE001 - 画面側の失敗でキューを止めない
                log.error(f"ジョブの知らせを受けられません ({event}): {exc}")

    def _planned_profile(self, task: str) -> str:
        try:
            return self.router.resolve(task).name
        except LLMError:
            return ""  # 今は使える先がない。番が来たときにもう一度探す

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="MuseFactory-jobs", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closing:
                    self._cond.wait()
                if self._closing:
                    return
                job = self._pending.popleft()
                self.current = job
            try:
                self._run(job)
            finally:
                with self._cond:
                    self.current = None
                    self._cond.notify_all()
            self._emit("job_finished", job=job)
            self._finish_batch_if_done(job.batch)
            if self.pending_count() == 0:
                self._emit("idle")

    def _run(self, job: Job) -> None:
        token = job.batch.token if job.batch else CancelToken()
        if token.cancelled:
            job.status = CANCELLED
            return
        start = time.monotonic()
        tried: set[str] = set()
        try:
            while True:
                profile = job.fixed_profile or self.router.resolve(job.task)
                job.profile_name = profile.name
                tried.add(profile.name)
                job.status = RUNNING
                self._emit("job_started", job=job)
                try:
                    result = job.run(profile, token)
                    break
                except (LLMConnectionError, LLMTimeoutError):
                    if job.fixed_profile is not None:
                        raise
                    # つながらなくなった先は覚えた結果を捨て、別の候補があれば 1 回だけそちらでやり直す
                    self.router.invalidate(profile.name)
                    retry = self.router.resolve(job.task)  # 全滅なら NoLLMAvailable (失敗にする)
                    if retry.name in tried or token.cancelled:
                        raise
                    log.info(f"{job.label}: {profile.name} が使えないので {retry.name} でやり直します")
            job.result = result
            job.status = CANCELLED if token.cancelled else DONE
        except Cancelled:
            job.status = CANCELLED
        except LLMError as exc:
            job.status = CANCELLED if token.cancelled else FAILED
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で全体を止めない
            job.status = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
        job.elapsed = time.monotonic() - start
        if job.status == FAILED:
            log.error(f"ジョブ失敗 {job.task} / {job.label}: {job.error}")

    def _finish_batch_if_done(self, batch: Optional[Batch]) -> None:
        if batch is None:
            return
        with self._cond:
            if batch.reported or batch.finished < batch.total:
                return
            batch.reported = True
        self._emit("batch_finished", batch=batch)
