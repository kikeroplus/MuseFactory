"""接続テストの画面 (SPEC 6 章「接続テストボタン (LLM プロファイルごと)」と 4.2 の確認)。

上: LLM プロファイルごとの接続テスト。モデル一覧を取り、短い質問を 1 つ送る (ジョブキューを通すので 1 件ずつ)。
下: タスク割り当て。候補を順に確かめ、どれが採用されるかを出す (生きているか・ComfyUI が生成中か)。
"""

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..core.config import TASKS
from ..core.job_queue import CANCELLED, DONE, FAILED, RUNNING, Job
from ..core.llm_client import LLMClient, LLMError
from ..util import files, log
from . import style


class _RouteSignals(QObject):
    done = Signal(object)  # [(task, [(name, Health)], 採用名 or "")]
    models = Signal(str, object, str)  # (プロファイル名, モデル一覧, エラー文)


class ConnectionDialog(QDialog):
    def __init__(self, profiles, router, queue, bridge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("接続テスト")
        self.resize(900, 620)
        self.profiles = profiles
        self.router = router
        self.queue = queue
        self._batch = None
        self._row_of_job: dict[int, int] = {}

        layout = QVBoxLayout(self)

        # ---- LLM プロファイル
        llm_group = QGroupBox("LLM プロファイル")
        llm_layout = QVBoxLayout(llm_group)
        self.llm_table = QTableWidget(len(profiles), 4)
        self.llm_table.setHorizontalHeaderLabels(["名前", "接続先", "モデル", "結果"])
        self.llm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.llm_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.llm_table.verticalHeader().setVisible(False)
        header = self.llm_table.horizontalHeader()
        for col in range(3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for row, profile in enumerate(profiles.values()):
            for col, text in enumerate([profile.name, profile.endpoint, profile.model_id, ""]):
                self.llm_table.setItem(row, col, QTableWidgetItem(text))
        llm_layout.addWidget(self.llm_table)
        llm_buttons = QHBoxLayout()
        self.test_selected_button = QPushButton("選んだものをテスト")
        self.test_all_button = QPushButton("すべてテスト")
        self.cancel_button = QPushButton("テストをやめる")
        self.cancel_button.setEnabled(False)
        llm_buttons.addWidget(self.test_selected_button)
        llm_buttons.addWidget(self.test_all_button)
        llm_buttons.addWidget(self.cancel_button)
        self.model_button = QPushButton("モデルを変える...")
        self.model_button.setToolTip("選んだプロファイルの接続先からモデル一覧を取り、使うモデルを選び直します (プロファイルのファイルに保存)")
        llm_buttons.addWidget(self.model_button)
        llm_buttons.addStretch(1)
        hint = QLabel("モデル一覧を取ってから短い質問を 1 つ送ります。ほかの処理と同じ順番待ちに並びます。")
        hint.setStyleSheet(f"color: {style.HINT};")
        llm_buttons.addWidget(hint)
        llm_layout.addLayout(llm_buttons)
        layout.addWidget(llm_group, 1)

        # ---- タスク割り当て
        route_group = QGroupBox("タスク割り当て (config/settings.json の task_routing)")
        route_layout = QVBoxLayout(route_group)
        self.route_table = QTableWidget(len(TASKS), 3)
        self.route_table.setHorizontalHeaderLabels(["タスク", "候補 (上から順に試す)", "採用"])
        self.route_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.route_table.verticalHeader().setVisible(False)
        rheader = self.route_table.horizontalHeader()
        rheader.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        rheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rheader.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for row, task in enumerate(TASKS):
            self.route_table.setItem(row, 0, QTableWidgetItem(task))
            self.route_table.setItem(row, 1, QTableWidgetItem(" → ".join(router.candidates(task))))
            self.route_table.setItem(row, 2, QTableWidgetItem(""))
        route_layout.addWidget(self.route_table)
        route_buttons = QHBoxLayout()
        self.route_button = QPushButton("割り当てを確かめる")
        self.route_button.setToolTip("各候補の /models と、同じ PC の ComfyUI の /queue を問い合わせ直します")
        route_buttons.addWidget(self.route_button)
        route_buttons.addStretch(1)
        route_layout.addLayout(route_buttons)
        layout.addWidget(route_group, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("閉じる")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.test_selected_button.clicked.connect(self.test_selected)
        self.test_all_button.clicked.connect(lambda: self.start_tests(list(range(len(self.profiles)))))
        self.cancel_button.clicked.connect(self.cancel_tests)
        self.route_button.clicked.connect(self.check_routes)
        self.model_button.clicked.connect(self.change_model)
        bridge.event.connect(self.on_job_event)
        self._route_signals = _RouteSignals()
        self._route_signals.done.connect(self.on_routes_checked)
        self._route_signals.models.connect(self.on_models_fetched)

    def done(self, result):
        self.cancel_tests()  # 閉じたら残りのテストは取り消す
        super().done(result)

    # ------------------------------------------------------------ 接続テスト

    def test_selected(self):
        rows = sorted({i.row() for i in self.llm_table.selectedIndexes()})
        if not rows:
            rows = [self.llm_table.currentRow()] if self.llm_table.currentRow() >= 0 else []
        if rows:
            self.start_tests(rows)

    def start_tests(self, rows: list[int]):
        if not rows or self._batch is not None:
            return
        names = list(self.profiles)
        jobs = []
        self._row_of_job.clear()
        for row in rows:
            profile = self.profiles[names[row]]
            job = Job("connection_test", profile.name, self._make_test(), fixed_profile=profile)
            self._row_of_job[job.id] = row
            jobs.append(job)
            self._set_result(row, "待機中…", style.HINT)
        self._set_busy(True)
        self._batch = self.queue.submit(jobs, name="接続テスト", quiet=True)

    @staticmethod
    def _make_test():
        def run(profile, token):
            token.check()
            models, result = LLMClient(profile).test_connection()
            return models, result
        return run

    def cancel_tests(self):
        if self._batch is not None:
            self.queue.cancel(self._batch)

    def on_job_event(self, event: str, data: dict):
        job = data.get("job")
        batch = data.get("batch")
        if job is not None and job.id in self._row_of_job:
            row = self._row_of_job[job.id]
            if event == "job_started" and job.status == RUNNING:
                self._set_result(row, "テスト中…", style.HINT)
            elif event == "job_finished":
                self._show_job_result(row, job)
        if event == "batch_finished" and batch is not None and batch is self._batch:
            self._batch = None
            self._set_busy(False)

    def _show_job_result(self, row: int, job: Job):
        profile = self.profiles[job.profile_name] if job.profile_name in self.profiles else None
        if job.status == DONE:
            models, result = job.result
            reply = log.summarize(result.text, 40) or "(空の返事)"
            text = f"✓ {result.elapsed:.1f} 秒  返事「{reply}」"
            color = style.OK
            if profile is not None and models and profile.model_id not in models:
                # LM Studio の設定によっては一覧にないモデルも読み込んで答える。割り当てでは使えない扱いになる
                text += f"  ※モデル {profile.model_id} が一覧にありません (一覧: {', '.join(models[:6])}"
                text += " …)" if len(models) > 6 else ")"
                color = style.WARN
            self._set_result(row, text, color)
        elif job.status == FAILED:
            self._set_result(row, f"✗ {job.error}", style.ERROR)
        elif job.status == CANCELLED:
            self._set_result(row, "やめました", style.HINT)

    def _set_result(self, row: int, text: str, color: str):
        item = QTableWidgetItem(text)
        item.setForeground(QColor(color))
        item.setToolTip(text)
        self.llm_table.setItem(row, 3, item)

    def _set_busy(self, busy: bool):
        self.test_selected_button.setEnabled(not busy)
        self.test_all_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    # ------------------------------------------------------------ モデルを変える

    def change_model(self):
        row = self.llm_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "モデルを変える", "上の表でプロファイルを選んでください。")
            return
        profile = list(self.profiles.values())[row]
        self.model_button.setEnabled(False)
        self._set_result(row, "モデル一覧を取っています…", style.HINT)
        signals = self._route_signals

        def work():
            try:
                signals.models.emit(profile.name, LLMClient(profile).list_models(timeout=10), "")
            except LLMError as exc:
                signals.models.emit(profile.name, [], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def on_models_fetched(self, name: str, models: list, error: str):
        self.model_button.setEnabled(True)
        row = list(self.profiles).index(name)
        profile = self.profiles[name]
        if error or not models:
            self._set_result(row, f"✗ モデル一覧を取れません: {error or '一覧が空です'}", style.ERROR)
            return
        self._set_result(row, "", style.HINT)
        models = [m for m in models if "embed" not in m.lower()] or models
        current = models.index(profile.model_id) if profile.model_id in models else 0
        model, ok = QInputDialog.getItem(self, "モデルを変える", f"{name} で使うモデル", models, current, False)
        if not ok or model == profile.model_id:
            return
        if profile.source is not None:
            data = files.read_json(profile.source, None)
            if not isinstance(data, dict):
                QMessageBox.warning(self, "モデルを変える", f"{profile.source.name} を読めません。")
                return
            data["model_id"] = model
            try:
                files.write_json(profile.source, data)
            except OSError as exc:
                QMessageBox.warning(self, "モデルを変える", f"保存できません: {exc}")
                return
        profile.model_id = model
        self.router.invalidate(name)
        self.llm_table.setItem(row, 2, QTableWidgetItem(model))
        self._set_result(row, f"モデルを {model} にしました", style.OK)

    # ------------------------------------------------------------ タスク割り当て

    def check_routes(self):
        self.route_button.setEnabled(False)
        for row in range(len(TASKS)):
            self.route_table.setItem(row, 2, QTableWidgetItem("確かめています…"))
        router = self.router
        signals = self._route_signals

        def work():
            router.invalidate()
            rows = []
            for task in TASKS:
                states = router.explain(task)
                adopted = next((name for name, h in states if h.ok), "")
                rows.append((task, states, adopted))
            signals.done.emit(rows)

        threading.Thread(target=work, daemon=True).start()

    def on_routes_checked(self, rows):
        self.route_button.setEnabled(True)
        for row, (task, states, adopted) in enumerate(rows):
            parts = []
            for name, health in states:
                mark = "✓" if health.ok else "✗"
                parts.append(f"{mark} {name}" + ("" if health.ok else f" ({health.reason})"))
            cand = QTableWidgetItem(" → ".join(parts) or "候補がありません")
            cand.setToolTip("\n".join(parts))
            self.route_table.setItem(row, 1, cand)
            item = QTableWidgetItem(adopted or "使える LLM がありません")
            item.setForeground(QColor(style.OK if adopted else style.ERROR))
            self.route_table.setItem(row, 2, item)
