"""作業タブ「LLM 比較」(SPEC 5.7): 同じ指示を選んだ LLM プロファイルそれぞれで実行し、結果と時間を並べる。

中身は「ざっくり生成」と同じ (指示 → 中間表現 → 対象モデル向けのプロンプト)。割り当ては使わず、選んだプロファイルに送る。
"""

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QSlider, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import intermediate, renderer
from ..core.job_queue import CANCELLED, DONE, RUNNING, Job
from ..core.llm_client import LLMClient
from ..core.tasks import generate
from . import style


class CompareTab(QWidget):
    def __init__(self, targets, llm_profiles, queue, bridge, parent=None):
        super().__init__(parent)
        self.targets = targets
        self.llm_profiles = llm_profiles
        self.queue = queue
        self._batch = None
        self._row_of_job: dict[int, int] = {}
        self.results: dict[int, tuple] = {}
        self._build()
        bridge.event.connect(self.on_job_event)

    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        box = QGroupBox("同じ入力")
        bv = QVBoxLayout(box)
        self.instruction = QPlainTextEdit()
        self.instruction.setPlaceholderText("例: 雨の夜の路地裏で傘をさす女性")
        bv.addWidget(self.instruction)
        form = QFormLayout()
        self.target_combo = QComboBox()
        form.addRow("対象モデル", self.target_combo)
        orow = QHBoxLayout()
        self.omakase = QSlider(Qt.Orientation.Horizontal)
        self.omakase.setRange(0, 100)
        self.omakase.setValue(50)
        self.omakase_label = QLabel("50")
        self.omakase.valueChanged.connect(lambda v: self.omakase_label.setText(str(v)))
        orow.addWidget(self.omakase, 1)
        orow.addWidget(self.omakase_label)
        form.addRow("おまかせ度", orow)
        bv.addLayout(form)
        top.addWidget(box, 3)
        lbox = QGroupBox("比べる LLM プロファイル")
        lv = QVBoxLayout(lbox)
        self.llm_list = QListWidget()
        lv.addWidget(self.llm_list)
        self.run_button = QPushButton("比べる")
        lv.addWidget(self.run_button)
        hint = QLabel("1 つずつ順に送ります (モデルの入れ替えの時間も含めて測ります)")
        hint.setStyleSheet(f"color: {style.HINT};")
        hint.setWordWrap(True)
        lv.addWidget(hint)
        top.addWidget(lbox, 2)
        layout.addLayout(top, 2)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["LLM プロファイル", "時間", "語数", "プロンプト"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        for col in range(3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        split.addWidget(self.table)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("行を選ぶと、プロンプトと中間表現の全文を出します")
        split.addWidget(self.detail)
        layout.addWidget(split, 4)

        self.run_button.clicked.connect(self.start)
        self.table.currentCellChanged.connect(lambda r, *_: self.show_detail(r))
        self.refresh_targets()
        self.refresh_llms()

    def refresh_targets(self):
        current = self.target_combo.currentText()
        self.target_combo.clear()
        self.target_combo.addItems(list(self.targets))
        if self.target_combo.findText(current) >= 0:
            self.target_combo.setCurrentText(current)

    def refresh_llms(self):
        checked = {self.llm_list.item(i).text() for i in range(self.llm_list.count())
                   if self.llm_list.item(i).checkState() == Qt.CheckState.Checked}
        self.llm_list.clear()
        for name in self.llm_profiles:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
            self.llm_list.addItem(item)

    def start(self):
        instruction = self.instruction.toPlainText().strip()
        names = [self.llm_list.item(i).text() for i in range(self.llm_list.count())
                 if self.llm_list.item(i).checkState() == Qt.CheckState.Checked]
        target = self.targets.get(self.target_combo.currentText())
        if not instruction or not names or target is None:
            QMessageBox.information(self, "LLM 比較", "指示・対象モデル・LLM プロファイル (1 つ以上) を選んでください。")
            return
        if self._batch is not None:
            return
        omakase = self.omakase.value()
        kind = "video" if target.is_video else "image"
        self.table.setRowCount(len(names))
        self.results.clear()
        self._row_of_job.clear()
        jobs = []
        for row, name in enumerate(names):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self._set(row, 1, "待機", style.HINT)
            job = Job("generate", name, self._make_job(instruction, kind, omakase, target),
                      fixed_profile=self.llm_profiles[name])
            self._row_of_job[job.id] = row
            jobs.append(job)
        self._batch = self.queue.submit(jobs, name="LLM 比較", quiet=True)
        self.run_button.setEnabled(False)

    @staticmethod
    def _make_job(instruction, kind, omakase, target):
        def run(profile, token):
            client = LLMClient(profile)
            start = time.monotonic()
            token.check()
            data = generate.generate_one(client, instruction, kind, omakase, 1, 1, [])
            middle = time.monotonic()
            token.check()
            result = renderer.render(client, data, target)
            return data, result, middle - start, time.monotonic() - middle
        return run

    def _set(self, row, col, text, color=""):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QColor(color))
        self.table.setItem(row, col, item)

    def on_job_event(self, event, data):
        job = data.get("job")
        if job is not None and job.id in self._row_of_job:
            row = self._row_of_job[job.id]
            if event == "job_started" and job.status == RUNNING:
                self._set(row, 1, "実行中…", style.HINT)
            elif event == "job_finished":
                if job.status == DONE:
                    inter, result, t1, t2 = job.result
                    self.results[row] = job.result
                    self._set(row, 1, f"{job.elapsed:.1f} 秒 (中間 {t1:.1f} + 書く {t2:.1f})")
                    over = result.over_limit
                    self._set(row, 2, f"{result.count} {result.unit}", style.OVER if over else "")
                    self._set(row, 3, result.text)
                elif job.status == CANCELLED:
                    self._set(row, 1, "キャンセルしました", style.HINT)
                else:
                    self._set(row, 1, f"✗ {job.error}", style.ERROR)
                self.table.resizeRowsToContents()
        if event == "batch_finished" and data.get("batch") is self._batch:
            self._batch = None
            self.run_button.setEnabled(True)

    def show_detail(self, row):
        if row not in self.results:
            self.detail.clear()
            return
        inter, result, _t1, _t2 = self.results[row]
        self.detail.setPlainText(result.text + "\n\n----- 中間表現 -----\n" + intermediate.to_json(inter))
