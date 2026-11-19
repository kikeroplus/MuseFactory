"""作業タブ「抽出」(SPEC 5.2)。

1 ファイルずつ LLM で中間表現に分解し、選んだ要素だけを取り出す。結果は表で直してから書き出す。
MuseFactory が作ったファイル (.muse に中間表現がある) は、LLM に頼まずそれを使える。
"""

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..core import intermediate
from ..core.job_queue import CANCELLED, DONE, Job
from ..core.llm_client import LLMClient
from ..core.tasks import process
from ..core.tasks.extract import Extracted, add_to_library, write_per_element, write_wildcards
from ..util import files
from . import style
from .common import FolderRow, InputFilesBox

ELEMENT_LABELS = {
    "subject": "主題", "pose": "ポーズ", "expression": "表情", "outfit": "服装", "background": "背景",
    "lighting": "ライティング", "camera": "カメラ", "style": "スタイル", "motion": "動き",
    "camera_motion": "カメラの動き",
}


class ExtractTab(QWidget):
    def __init__(self, queue, bridge, settings: dict, selected_in_main: Callable[[], list[Path]],
                 current_folder: Callable[[], str], on_saved: Callable[[list[Path]], None], parent=None):
        super().__init__(parent)
        self.queue = queue
        self.settings = settings
        self.current_folder = current_folder
        self.on_saved = on_saved
        self._batch = None
        self._job_row: dict[int, int] = {}
        self.sources: list[Path] = []
        self.results: dict[int, dict] = {}  # 行 → 中間表現
        self.columns: list[str] = []
        self._build(selected_in_main, current_folder)
        bridge.event.connect(self.on_job_event)

    def _build(self, selected_in_main, current_folder):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.inputs = InputFilesBox(selected_in_main, current_folder)
        top.addWidget(self.inputs, 3)

        element_box = QGroupBox("取り出す要素")
        grid = QGridLayout(element_box)
        self.element_checks: dict[str, QCheckBox] = {}
        for i, name in enumerate(intermediate.ELEMENTS):
            check = QCheckBox(f"{ELEMENT_LABELS.get(name, name)} ({name})")
            check.setChecked(name in ("pose", "expression"))
            self.element_checks[name] = check
            grid.addWidget(check, i % 5, i // 5)
        top.addWidget(element_box, 3)

        out_box = QGroupBox("書き出し")
        ov = QVBoxLayout(out_box)
        self.per_element_check = QCheckBox("要素ごとに 1 ファイル ((元の名前)_extract_(要素).txt)")
        self.per_element_check.setChecked(True)
        self.wildcard_check = QCheckBox("ワイルドカード形式 ((要素).txt に 1 行 1 項目。同じ行は 1 つに)")
        self.wildcard_check.setChecked(True)
        self.library_check = QCheckBox("パーツライブラリに登録")
        self.use_existing_check = QCheckBox("MuseFactory で作ったファイルは保存した中間表現を使う (LLM に頼まない)")
        self.use_existing_check.setChecked(True)
        for w in (self.per_element_check, self.wildcard_check, self.library_check, self.use_existing_check):
            ov.addWidget(w)
        self.out_folder = FolderRow("出力フォルダ", "空なら フォルダ欄のフォルダ\\extract")
        ov.addWidget(self.out_folder)
        buttons = QHBoxLayout()
        self.run_button = QPushButton("抽出する")
        self.save_button = QPushButton("書き出す")
        self.save_button.setEnabled(False)
        self.open_button = QPushButton("出力フォルダを開く")
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.open_button)
        ov.addLayout(buttons)
        ov.addStretch(1)
        top.addWidget(out_box, 4)
        layout.addLayout(top, 2)

        hint = QLabel("結果の表は直せます (書き出すのは直したあとの値。空の欄は書き出しません)。")
        hint.setStyleSheet(f"color: {style.HINT};")
        layout.addWidget(hint)
        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 4)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {style.HINT};")
        layout.addWidget(self.summary)

        self.run_button.clicked.connect(self.start)
        self.save_button.clicked.connect(self.save)
        self.open_button.clicked.connect(self.open_output)

    # ------------------------------------------------------------ 実行

    def selected_elements(self) -> list[str]:
        return [n for n, c in self.element_checks.items() if c.isChecked()]

    def output_folder(self) -> Path:
        text = self.out_folder.text()
        if text:
            return Path(text)
        return Path(self.current_folder().strip() or ".") / "extract"

    def start(self):
        sources = self.inputs.files()
        elements = self.selected_elements()
        if not sources:
            QMessageBox.information(self, "抽出", "入力ファイルを足してください。")
            return
        if not elements:
            QMessageBox.information(self, "抽出", "取り出す要素を 1 つ以上チェックしてください。")
            return
        if self._batch is not None:
            return
        self.sources = sources
        self.columns = elements
        self.results = {}
        self.table.clear()
        self.table.setRowCount(len(sources))
        self.table.setColumnCount(2 + len(elements))
        self.table.setHorizontalHeaderLabels(["ファイル", "状態"] + [ELEMENT_LABELS.get(e, e) for e in elements])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(2, 2 + len(elements)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        use_existing = self.use_existing_check.isChecked()
        jobs = []
        self._job_row.clear()
        for row, path in enumerate(sources):
            name = QTableWidgetItem(path.name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(str(path))
            self.table.setItem(row, 0, name)
            self._set_status(row, "待機", style.HINT)
            job = Job("extract", path.name, self._make_job(path, use_existing))
            self._job_row[job.id] = row
            jobs.append(job)
        self._batch = self.queue.submit(jobs, name="抽出", quiet=True)
        self._update_buttons()

    @staticmethod
    def _make_job(path: Path, use_existing: bool):
        def run(profile, token):
            token.check()
            if use_existing:
                data = process.existing_intermediate(path)
                if data is not None:
                    return data, "保存した中間表現"
            text = files.read_text(path).strip()
            if not text:
                raise ValueError("ファイルが空です")
            return process.decompose(LLMClient(profile), text, path), ""
        return run

    def on_job_event(self, event: str, data: dict):
        job = data.get("job")
        if job is not None and job.id in self._job_row:
            row = self._job_row[job.id]
            if event == "job_started":
                self._set_status(row, "分解中…", style.HINT)
            elif event == "job_finished":
                if job.status == DONE:
                    result, note = job.result
                    self.results[row] = result
                    self._set_status(row, "✓ " + (note or f"{job.elapsed:.1f} 秒"), style.OK)
                    for col, element in enumerate(self.columns, start=2):
                        self.table.setItem(row, col, QTableWidgetItem(result.get(element, "")))
                elif job.status == CANCELLED:
                    self._set_status(row, "キャンセルしました", style.HINT)
                else:
                    self._set_status(row, f"✗ {job.error or job.status}", style.ERROR)
        if event == "batch_finished" and data.get("batch") is self._batch:
            self._batch = None
            ok = len(self.results)
            failed = len(self.sources) - ok
            self.summary.setText(f"{ok} / {len(self.sources)} 件を分解しました" + (f" (失敗・取り消し {failed} 件)" if failed else ""))
            self._update_buttons()

    def _set_status(self, row: int, text: str, color: str):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setForeground(QColor(color))
        item.setToolTip(text)
        self.table.setItem(row, 1, item)

    def _update_buttons(self):
        busy = self._batch is not None
        self.run_button.setEnabled(not busy)
        self.run_button.setText("抽出中…" if busy else "抽出する")
        self.save_button.setEnabled(not busy and bool(self.results))

    # ------------------------------------------------------------ 書き出し

    def edited_results(self) -> list[Extracted]:
        """表で直した値を反映した結果 (分解できた行だけ)。"""
        out = []
        for row, data in sorted(self.results.items()):
            edited = dict(data)
            for col, element in enumerate(self.columns, start=2):
                item = self.table.item(row, col)
                edited[element] = item.text().strip() if item else ""
            out.append(Extracted(self.sources[row], edited))
        return out

    def save(self):
        if not (self.per_element_check.isChecked() or self.wildcard_check.isChecked() or self.library_check.isChecked()):
            QMessageBox.information(self, "抽出", "書き出し方を 1 つ以上チェックしてください。")
            return
        results = self.edited_results()
        folder = self.output_folder()
        written: list[Path] = []
        notes = []
        try:
            if self.per_element_check.isChecked():
                paths = write_per_element(results, self.columns, folder)
                written += paths
                notes.append(f"要素ごと {len(paths)} ファイル")
            if self.wildcard_check.isChecked():
                paths = write_wildcards(results, self.columns, folder)
                written += paths
                notes.append(f"ワイルドカード {len(paths)} ファイル")
            if self.library_check.isChecked():
                counts = add_to_library(results, self.columns)
                notes.append("ライブラリ " + (", ".join(f"{ELEMENT_LABELS.get(k, k)} {v} 件" for k, v in counts.items()) or "0 件"))
        except OSError as exc:
            QMessageBox.warning(self, "抽出", f"書き出せません: {exc}")
        self.summary.setText(f"書き出しました: {' / '.join(notes)}  →  {folder}")
        if written:
            self.on_saved(written)

    def open_output(self):
        folder = self.output_folder()
        if folder.is_dir():
            os.startfile(str(folder))
        else:
            self.summary.setText(f"まだフォルダがありません: {folder}")
