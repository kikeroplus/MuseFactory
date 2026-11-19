"""作業タブ「加工」(SPEC 5.3): 指示で変える・盛る・英訳・モデル変換・画像→動画・ネガティブ生成。

ファイルごとに 1 ジョブ。中で LLM を何回か呼ぶときは、それぞれのタスクの割り当て (router) に従う。
結果は変更前と変更後を並べて見せ、直してから保存する。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from ..core import output, renderer
from ..core.job_queue import CANCELLED, DONE, Job
from ..core.llm_client import LLMClient
from ..core.targets import TargetProfile
from ..core.tasks import process
from ..util import files
from . import style
from .common import DiffView, FolderRow, InputFilesBox, resolve_output_folder

# (キー, 画面の名前, 主なタスク, 名前に付けるもの)
OPERATIONS = [
    ("modify", "指示で変える (例: 女性の服を赤に)", "enhance", "mod"),
    ("enhance", "盛る (空いた・薄い要素を補う)", "enhance", "enhance"),
    ("translate", "英訳 (日本語版も .muse に残す)", "translate", "en"),
    ("convert", "モデル変換 (別のモデル向けに書き直す)", "render", "conv"),
    ("to_video", "画像→動画 (動きを足して動画向けに)", "enhance", "tovideo"),
    ("negative", "ネガティブ生成 (.muse に *_neg.txt)", "negative", "neg"),
]


@dataclass
class ProcessResult:
    source: Path
    before: str = ""
    after: str = ""
    data: Optional[dict] = None
    negative: str = ""
    error: str = ""
    cancelled: bool = False
    pending: bool = True
    saved: Optional[Path] = None
    checked: bool = True
    note: str = ""


class ProcessTab(QWidget):
    def __init__(self, targets: dict[str, TargetProfile], queue, bridge, router, settings: dict,
                 selected_in_main: Callable[[], list[Path]], current_folder: Callable[[], str],
                 on_saved: Callable[[list[Path]], None], open_in_pwc: Callable[[Path, list[Path]], None],
                 generate_files: Optional[Callable[[list[Path]], None]] = None,
                 add_example: Optional[Callable[[str, str], None]] = None, parent=None):
        super().__init__(parent)
        self.targets = targets
        self.add_example = add_example
        self.queue = queue
        self.router = router
        self.settings = settings
        self.on_saved = on_saved
        self.open_in_pwc = open_in_pwc
        self.generate_files = generate_files
        self.items: list[ProcessResult] = []
        self._job_item: dict[int, ProcessResult] = {}
        self._batch = None
        self._op = None
        self._target: Optional[TargetProfile] = None
        self._loading = False
        self.last_saved: list[Path] = []
        self._build(selected_in_main, current_folder)
        bridge.event.connect(self.on_job_event)

    # ------------------------------------------------------------ 画面

    def _build(self, selected_in_main, current_folder):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.inputs = InputFilesBox(selected_in_main, current_folder)
        top.addWidget(self.inputs, 3)

        box = QGroupBox("加工")
        form = QFormLayout(box)
        self.op_combo = QComboBox()
        for key, label, _task, _suffix in OPERATIONS:
            self.op_combo.addItem(label, key)
        form.addRow("処理", self.op_combo)
        self.instruction_edit = QPlainTextEdit()
        self.instruction_edit.setPlaceholderText("例: 女性の服を赤に変えて / 夜の場面にして / 猫を足して\n"
                                                 "(何行でも書けます。Ctrl+Enter で加工する)")
        self.instruction_edit.setToolTip("指示に関係するところだけ書き換え、ほかの言葉はそのまま残します (Ctrl+Enter で加工する)")
        self.instruction_edit.setTabChangesFocus(True)
        lines = self.instruction_edit.fontMetrics().lineSpacing()
        self.instruction_edit.setFixedHeight(lines * 4 + 12)  # 4 行ぶん
        self.instruction_label = QLabel("指示")
        form.addRow(self.instruction_label, self.instruction_edit)
        self.target_combo = QComboBox()
        form.addRow("対象モデル", self.target_combo)
        self.seconds_spin = QDoubleSpinBox()
        self.seconds_spin.setRange(1, 30)
        self.seconds_spin.setValue(5)
        self.seconds_spin.setSuffix(" 秒")
        self.seconds_label = QLabel("動画の長さ")
        form.addRow(self.seconds_label, self.seconds_spin)
        self.use_existing_check = QCheckBox("MuseFactory で作ったファイルは保存した中間表現を使う")
        self.use_existing_check.setChecked(True)
        form.addRow(self.use_existing_check)
        self.out_folder = FolderRow("出力フォルダ", "空なら元のファイルと同じフォルダ (画像→動画は その中の video)")
        form.addRow(self.out_folder)
        self.run_button = QPushButton("加工する")
        form.addRow(self.run_button)
        top.addWidget(box, 4)
        layout.addLayout(top, 2)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.result_list = QListWidget()
        splitter.addWidget(self.result_list)
        self.diff = DiffView()
        splitter.addWidget(self.diff)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 4)

        bottom = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {style.HINT};")
        bottom.addWidget(self.summary, 1)
        self.save_button = QPushButton("チェックしたものを保存")
        self.save_button.setEnabled(False)
        bottom.addWidget(self.save_button)
        self.save_generate_button = QPushButton("保存して生成する...")
        self.save_generate_button.setToolTip("チェックしたものを保存し、そのファイルで ComfyUI の生成の画面を開きます (ネガティブ生成では使えません)")
        self.save_generate_button.setEnabled(False)
        self.save_generate_button.setVisible(self.generate_files is not None)
        bottom.addWidget(self.save_generate_button)
        self.example_button = QPushButton("お手本に追加")
        self.example_button.setToolTip("表示中の変更後の文を、対象モデルのプロファイルの example_prompts に足します")
        self.example_button.setVisible(self.add_example is not None)
        self.example_button.clicked.connect(self._add_current_example)
        bottom.addWidget(self.example_button)
        self.pwc_button = QPushButton("PWC で開く")
        self.pwc_button.setEnabled(False)
        self.pwc_button.setToolTip("最後に保存したファイルのフォルダを PromptWordCrafter で開きます")
        bottom.addWidget(self.pwc_button)
        layout.addLayout(bottom)

        self.op_combo.currentIndexChanged.connect(self._on_op_changed)
        self.run_button.clicked.connect(self.start)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self.instruction_edit, activated=self.start,
                      context=Qt.ShortcutContext.WidgetShortcut)
        self.result_list.currentRowChanged.connect(self.show_item)
        self.result_list.itemChanged.connect(self._on_checked)
        self.diff.edited.connect(self._on_edited)
        self.save_button.clicked.connect(lambda: self.save())
        self.save_generate_button.clicked.connect(lambda: self.save(generate=True))
        self.pwc_button.clicked.connect(lambda: self.open_in_pwc(self.last_saved[0].parent, self.last_saved))
        self._on_op_changed()

    def refresh_targets(self):
        self._on_op_changed()

    def _add_current_example(self):
        item = self.current_item()
        if item is None or item.pending or item.error or self._target is None or self._op == "negative":
            return
        self.add_example(self._target.name, self.diff.text())

    def op(self) -> str:
        return self.op_combo.currentData()

    def _on_op_changed(self):
        op = self.op()
        current = self.target_combo.currentText()
        self.target_combo.clear()
        for name, target in self.targets.items():
            if op == "to_video" and not target.is_video:
                continue
            if op == "negative" and not target.negative_supported:
                continue
            self.target_combo.addItem(name)
        if current:
            index = self.target_combo.findText(current)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self.target_combo.setEnabled(op not in ("translate", "modify"))
        self.target_combo.setToolTip("英訳では使いません" if op == "translate" else
                                     "元のプロンプトの書き方のまま変えるので使いません" if op == "modify" else
                                     "ネガティブを使うモデルだけ" if op == "negative" else "")
        self.instruction_edit.setVisible(op == "modify")
        self.instruction_label.setVisible(op == "modify")
        self.seconds_spin.setVisible(op == "to_video")
        self.seconds_label.setVisible(op == "to_video")

    # ------------------------------------------------------------ 実行

    def busy(self) -> bool:
        return self._batch is not None

    def start(self):
        sources = self.inputs.files()
        op = self.op()
        if not sources:
            QMessageBox.information(self, "加工", "入力ファイルを足してください。")
            return
        no_target = op in ("translate", "modify")
        target = None if no_target else self.targets.get(self.target_combo.currentText())
        if not no_target and target is None:
            QMessageBox.information(self, "加工", "対象モデルを選んでください。")
            return
        instruction = self.instruction_edit.toPlainText().strip()
        if op == "modify" and not instruction:
            QMessageBox.information(self, "加工", "指示を入れてください (例: 女性の服を赤に変えて)。")
            self.instruction_edit.setFocus()
            return
        if self.busy():
            return
        unsaved = [i for i in self.items if not i.pending and not i.error and i.saved is None]
        if unsaved and QMessageBox.question(
                self, "加工", f"保存していない結果が {len(unsaved)} 件あります。捨てて加工しますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._op, self._target = op, target
        task = next(t for k, _l, t, _s in OPERATIONS if k == op)
        seconds = self.seconds_spin.value()
        use_existing = self.use_existing_check.isChecked()
        self.items = [ProcessResult(path) for path in sources]
        self._job_item.clear()
        jobs = []
        for item in self.items:
            job = Job(task, item.source.name, self._make_job(op, item.source, target, seconds, use_existing,
                                                             instruction))
            self._job_item[job.id] = item
            jobs.append(job)
        self._refresh_list()
        self._batch = self.queue.submit(jobs, name="加工", quiet=True)
        self._update_buttons()

    def _make_job(self, op: str, path: Path, target: Optional[TargetProfile], seconds: float, use_existing: bool,
                  instruction: str = ""):
        router = self.router

        def client_for(task: str, profile) -> LLMClient:
            # ジョブの主なタスクはそのプロファイル、ほかの段はそれぞれの割り当てで
            return LLMClient(profile) if task == job_task else LLMClient(router.resolve(task))

        job_task = next(t for k, _l, t, _s in OPERATIONS if k == op)

        def run(profile, token) -> ProcessResult:
            token.check()
            text = files.read_prompt_source(path).strip()
            result = ProcessResult(path, before=text, pending=False)
            if op == "translate":
                if not text:
                    raise ValueError("ファイルが空です")
                result.after = process.translate_text(client_for("translate", profile), text)
                return result
            if op == "modify":
                # 1. 中間表現で何が変わるかを決める (照明など指示から外れて変わるものも拾える)
                # 2. 文は直接書き換える (言い回しを残すため。モデル向けに書き直すと全部の言葉が変わる)
                if not text:
                    raise ValueError("ファイルが空です")
                data = process.existing_intermediate(path) if use_existing else None
                if data is None:
                    data = process.decompose(client_for("extract", profile), text, path)
                token.check()
                changed = process.modify(client_for("enhance", profile), data, instruction)
                changed["source"] = {"type": "modify", "files": [str(path)], "instruction": instruction}
                token.check()
                result.after = process.modify_text(client_for("enhance", profile), text, instruction,
                                                   process.changed_fields(data, changed))
                result.data = changed
                if result.after.strip() == text:
                    result.note = "変わりませんでした (指示に当てはまるところがないかもしれません)"
                negative = output.negative_path(path)
                result.negative = files.read_text(negative).strip() if negative.is_file() else ""
                return result
            data = process.existing_intermediate(path) if use_existing else None
            if data is not None:
                result.note = "保存した中間表現を使いました"
            else:
                if not text:
                    raise ValueError("ファイルが空です")
                data = process.decompose(client_for("extract", profile), text, path)
            token.check()
            if op == "negative":
                existing = output.negative_path(path)
                result.before = files.read_text(existing).strip() if existing.is_file() else ""
                result.after = process.make_negative(client_for("negative", profile), data)
                result.data = data
                return result
            if op == "enhance":
                data = process.enhance(client_for("enhance", profile), data)
            elif op == "to_video":
                data = process.to_video(client_for("enhance", profile), data, seconds)
            token.check()
            rendered = renderer.render(client_for("render", profile), data, target)
            result.after, result.negative, result.data = rendered.text, rendered.negative, data
            return result

        return run

    def on_job_event(self, event: str, data: dict):
        job = data.get("job")
        if job is not None and job.id in self._job_item and event == "job_finished":
            item = self._job_item.pop(job.id)
            if job.status == DONE:
                done: ProcessResult = job.result
                item.before, item.after, item.data = done.before, done.after, done.data
                item.negative, item.note, item.error = done.negative, done.note, ""
            elif job.status == CANCELLED:
                item.cancelled = True
                item.error = "キャンセルしました"
            else:
                item.error = job.error or job.status
            item.pending = False
            self._refresh_list()
        if event == "batch_finished" and data.get("batch") is self._batch:
            self._batch = None
            self._update_buttons()

    # ------------------------------------------------------------ 一覧

    def _label(self, item: ProcessResult) -> tuple[str, str]:
        if item.cancelled:
            return f"{item.source.name}  キャンセルしました", style.HINT
        if item.error:
            return f"✗ {item.source.name}  {item.error}", style.ERROR
        if item.pending:
            return f"… {item.source.name}", style.HINT
        mark = "✓ 保存済み  " if item.saved else ""
        return f"{mark}{item.source.name}", ""

    def _refresh_list(self):
        self._loading = True
        row = self.result_list.currentRow()
        self.result_list.clear()
        for item in self.items:
            text, color = self._label(item)
            entry = QListWidgetItem(text)
            if color:
                entry.setForeground(QColor(color))
            if not item.pending and not item.error:
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                entry.setCheckState(Qt.CheckState.Checked if item.checked else Qt.CheckState.Unchecked)
            entry.setToolTip(str(item.saved or item.source))
            self.result_list.addItem(entry)
        self._loading = False
        if self.items:
            self.result_list.setCurrentRow(max(0, min(row, len(self.items) - 1)))
        done = sum(1 for i in self.items if not i.pending and not i.error)
        failed = sum(1 for i in self.items if i.error and not i.cancelled)
        self.summary.setText(f"{done} / {len(self.items)} 件できました" + (f"、失敗 {failed} 件" if failed else ""))
        self._update_buttons()

    def current_item(self) -> Optional[ProcessResult]:
        row = self.result_list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def show_item(self, _row: int):
        if self._loading:
            return
        item = self.current_item()
        if item is None:
            self.diff.set_texts("", "", editable=False)
            return
        ok = not item.pending and not item.error
        self.diff.set_texts(item.before, item.after if ok else item.error, editable=ok)
        info = item.note
        target = self._target if self._op != "modify" else self._source_target(item)
        if ok and target is not None and self._op != "negative":
            count, unit = renderer.measure(item.after, target.output_language)
            info = f"{count} {unit} / 目安 {target.max_length}" + (f"  {info}" if info else "")
        self.diff.info_label.setText(info)

    def _source_target(self, item: ProcessResult) -> Optional[TargetProfile]:
        """指示で変えたもの: 元のファイルを作ったときのモデル (.muse の中間表現に書いてあれば)。"""
        name = (item.data or {}).get("target")
        return self.targets.get(name) if name else None

    def _on_edited(self):
        item = self.current_item()
        if item is not None and not item.pending and not item.error:
            item.after = self.diff.text()
            item.saved = None

    def _on_checked(self, entry: QListWidgetItem):
        if self._loading:
            return
        row = self.result_list.row(entry)
        if 0 <= row < len(self.items):
            self.items[row].checked = entry.checkState() == Qt.CheckState.Checked

    def _update_buttons(self):
        busy = self.busy()
        self.run_button.setEnabled(not busy)
        self.run_button.setText("加工中…" if busy else "加工する")
        self.save_button.setEnabled(not busy and any(not i.pending and not i.error for i in self.items))
        self.save_generate_button.setEnabled(self.save_button.isEnabled() and self._op != "negative")
        self.pwc_button.setEnabled(bool(self.last_saved))

    # ------------------------------------------------------------ 保存

    def save(self, generate: bool = False):
        items = [i for i in self.items if i.checked and not i.pending and not i.error and i.after.strip()]
        if not items:
            QMessageBox.information(self, "加工", "保存するものにチェックを付けてください。")
            return
        op, target = self._op, self._target
        suffix = next(s for k, _l, _t, s in OPERATIONS if k == op)
        save_json = bool(self.settings.get("save_intermediate_json", True))
        keep_ja = bool(self.settings.get("keep_japanese_on_translate", True))
        reserved: set = set()
        saved: list[Path] = []
        try:
            for item in items:
                stem = files.safe_name(item.source.stem)
                if op == "negative":
                    path = files.unique_path(output.negative_path(item.source))
                    files.write_text_atomic(path, item.after.strip() + "\n")
                elif op == "modify":
                    path = output.free_prompt_path(resolve_output_folder(self.out_folder.text(), item.source),
                                                   output.processed_stem(stem, suffix), reserved)
                    output.save_prompt(path, item.after, item.data, item.negative, save_json)
                elif op == "translate":
                    path = output.free_prompt_path(resolve_output_folder(self.out_folder.text(), item.source),
                                                   output.processed_stem(stem, suffix), reserved)
                    output.save_prompt(path, item.after)
                    if keep_ja:
                        files.write_text_atomic(output.japanese_path(path), item.before.strip() + "\n")
                else:
                    sub = output.VIDEO_DIR if op == "to_video" else ""
                    with_model = op in ("convert", "to_video")
                    name = output.processed_stem(stem, suffix, target.slug if with_model else "",
                                                 tuple(t.slug for t in self.targets.values()) if with_model else ())
                    path = output.free_prompt_path(resolve_output_folder(self.out_folder.text(), item.source, sub),
                                                   name, reserved)
                    data = dict(item.data or {})
                    data["target"] = target.name
                    output.save_prompt(path, item.after, data if item.data else None,
                                       item.negative if target.negative_supported else "", save_json)
                item.saved = path
                item.checked = False
                saved.append(path)
        except OSError as exc:
            QMessageBox.warning(self, "加工", f"保存できません: {exc}")
        self._refresh_list()
        if saved:
            self.last_saved = [p for p in saved if p.suffix == ".txt" and output.MUSE_DIR not in p.parts]
            self._update_buttons()
            self.on_saved(saved)
            self.summary.setText(f"{len(saved)} 件を保存しました")
            if generate and self.generate_files is not None and self.last_saved:
                self.generate_files(self.last_saved)
