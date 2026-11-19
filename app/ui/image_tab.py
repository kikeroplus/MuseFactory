"""作業タブ「画像から」(SPEC 5.8): 画像を見られる LLM (task image_to_prompt) で中間表現を作り、選んだモデル向けに書く。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
                               QWidget)

from ..core import output, renderer
from ..core.job_queue import CANCELLED, DONE, Job
from ..core.llm_client import LLMClient
from ..core.targets import TargetProfile
from ..core.tasks import vision
from ..util import files
from . import style
from .common import FolderRow

IMAGE_FILTER = "画像 (*.png *.jpg *.jpeg *.webp)"


@dataclass
class ImageItem:
    source: Path
    text: str = ""
    data: Optional[dict] = None
    error: str = ""
    pending: bool = True
    saved: Optional[Path] = None


class ImageTab(QWidget):
    def __init__(self, targets: dict[str, TargetProfile], queue, bridge, router, settings: dict,
                 current_folder: Callable[[], str], on_saved: Callable[[list[Path]], None],
                 generate_files: Optional[Callable[[list[Path]], None]] = None, parent=None):
        super().__init__(parent)
        self.targets = targets
        self.queue = queue
        self.router = router
        self.settings = settings
        self.current_folder = current_folder
        self.on_saved = on_saved
        self.generate_files = generate_files
        self.items: list[ImageItem] = []
        self._job_item: dict[int, ImageItem] = {}
        self._batch = None
        self._target: Optional[TargetProfile] = None
        self._loading = False
        self._build()
        bridge.event.connect(self.on_job_event)

    def _build(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        ibox = QGroupBox("画像")
        iv = QVBoxLayout(ibox)
        self.image_list = QListWidget()
        iv.addWidget(self.image_list)
        row = QHBoxLayout()
        self.add_button = QPushButton("画像を選んで足す...")
        self.clear_button = QPushButton("全部外す")
        row.addWidget(self.add_button)
        row.addWidget(self.clear_button)
        iv.addLayout(row)
        top.addWidget(ibox, 3)
        obox = QGroupBox("書き方")
        form = QFormLayout(obox)
        self.target_combo = QComboBox()
        form.addRow("対象モデル", self.target_combo)
        self.out_folder = FolderRow("保存先", "空なら フォルダ欄のフォルダ")
        form.addRow(self.out_folder)
        self.run_button = QPushButton("プロンプトにする")
        self.run_button.setToolTip("画像を見られる LLM (タスク割り当ての image_to_prompt) に見せます")
        form.addRow(self.run_button)
        top.addWidget(obox, 3)
        layout.addLayout(top, 2)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.result_list = QListWidget()
        split.addWidget(self.result_list)
        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumWidth(200)
        split.addWidget(self.thumb)
        self.editor = QPlainTextEdit()
        split.addWidget(self.editor)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 2)
        layout.addWidget(split, 4)
        brow = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {style.HINT};")
        brow.addWidget(self.summary, 1)
        self.save_button = QPushButton("保存")
        self.save_generate_button = QPushButton("保存して生成する...")
        self.save_generate_button.setVisible(self.generate_files is not None)
        for b in (self.save_button, self.save_generate_button):
            b.setEnabled(False)
            brow.addWidget(b)
        layout.addLayout(brow)

        self.add_button.clicked.connect(self.add_images)
        self.clear_button.clicked.connect(self.image_list.clear)
        self.run_button.clicked.connect(self.start)
        self.result_list.currentRowChanged.connect(self.show_item)
        self.editor.textChanged.connect(self._on_edited)
        self.save_button.clicked.connect(lambda: self.save())
        self.save_generate_button.clicked.connect(lambda: self.save(generate=True))
        self.refresh_targets()

    def refresh_targets(self):
        current = self.target_combo.currentText()
        self.target_combo.clear()
        self.target_combo.addItems([n for n, t in self.targets.items() if not t.is_video] +
                                   [n for n, t in self.targets.items() if t.is_video])
        if self.target_combo.findText(current) >= 0:
            self.target_combo.setCurrentText(current)

    def add_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "画像を選ぶ", self.current_folder(), IMAGE_FILTER)
        known = {self.image_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.image_list.count())}
        for p in paths:
            if p not in known:
                item = QListWidgetItem(Path(p).name)
                item.setData(Qt.ItemDataRole.UserRole, p)
                self.image_list.addItem(item)

    def start(self):
        sources = [Path(self.image_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.image_list.count())]
        target = self.targets.get(self.target_combo.currentText())
        if not sources or target is None:
            QMessageBox.information(self, "画像から", "画像と対象モデルを選んでください。")
            return
        if self._batch is not None:
            return
        self._target = target
        self.items = [ImageItem(p) for p in sources]
        self._job_item.clear()
        jobs = []
        router = self.router
        for item in self.items:
            def run(profile, token, path=item.source):
                token.check()
                data = vision.image_to_intermediate(LLMClient(profile), path)
                token.check()
                result = renderer.render(LLMClient(router.resolve("render")), data, target)
                return data, result.text
            job = Job("image_to_prompt", item.source.name, run)
            self._job_item[job.id] = item
            jobs.append(job)
        self._refresh()
        self._batch = self.queue.submit(jobs, name="画像から", quiet=True)
        self.run_button.setEnabled(False)

    def on_job_event(self, event, data):
        job = data.get("job")
        if event == "job_finished" and job is not None and job.id in self._job_item:
            item = self._job_item.pop(job.id)
            item.pending = False
            if job.status == DONE:
                item.data, item.text = job.result
            else:
                item.error = "キャンセルしました" if job.status == CANCELLED else (job.error or job.status)
            self._refresh()
        if event == "batch_finished" and data.get("batch") is self._batch:
            self._batch = None
            self.run_button.setEnabled(True)
            self._refresh()

    def _refresh(self):
        self._loading = True
        row = self.result_list.currentRow()
        self.result_list.clear()
        for item in self.items:
            mark = "… " if item.pending else "✗ " if item.error else "✓ 保存済み  " if item.saved else ""
            entry = QListWidgetItem(mark + item.source.name + (f"  {item.error}" if item.error else ""))
            if item.error:
                entry.setForeground(QColor(style.ERROR))
            self.result_list.addItem(entry)
        self._loading = False
        if self.items:
            self.result_list.setCurrentRow(max(0, min(row, len(self.items) - 1)))
        ok = any(not i.pending and not i.error for i in self.items) and self._batch is None
        self.save_button.setEnabled(ok)
        self.save_generate_button.setEnabled(ok)

    def show_item(self, row):
        if self._loading or not 0 <= row < len(self.items):
            return
        item = self.items[row]
        pix = QPixmap(str(item.source))
        self.thumb.setPixmap(pix.scaled(300, 300, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation) if not pix.isNull() else QPixmap())
        self._loading = True
        self.editor.setPlainText(item.error or item.text)
        self.editor.setReadOnly(bool(item.error or item.pending))
        self._loading = False

    def _on_edited(self):
        row = self.result_list.currentRow()
        if not self._loading and 0 <= row < len(self.items) and not self.items[row].error:
            self.items[row].text = self.editor.toPlainText()
            self.items[row].saved = None

    def save(self, generate: bool = False):
        items = [i for i in self.items if not i.pending and not i.error and i.text.strip()]
        folder = Path(self.out_folder.text() or self.current_folder().strip() or ".")
        if not folder.is_dir():
            QMessageBox.information(self, "画像から", f"保存先のフォルダがありません: {folder}")
            return
        target = self._target
        reserved: set = set()
        saved = []
        try:
            for item in items:
                path = output.free_prompt_path(folder, f"{files.safe_name(item.source.stem)}_img2prompt_{target.slug}", reserved)
                data = dict(item.data or {})
                data["target"] = target.name
                output.save_prompt(path, item.text, data, "", bool(self.settings.get("save_intermediate_json", True)))
                item.saved = path
                saved.append(path)
        except OSError as exc:
            QMessageBox.warning(self, "画像から", f"保存できません: {exc}")
        self._refresh()
        if saved:
            self.on_saved(saved)
            self.summary.setText(f"{len(saved)} 件を保存しました: {folder}")
            if generate and self.generate_files is not None:
                self.generate_files(saved, what="画像から作ったプロンプト")
