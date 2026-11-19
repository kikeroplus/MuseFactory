"""作業タブ「ざっくり生成」(SPEC 5.1)。

指示 → 中間表現 (本数ぶん) → 選んだモデルごとにレンダリング → プレビューで直して保存。
LLM へのリクエストはすべてジョブキューを通す (生成のジョブがすべて終わってから、レンダリングのジョブをまとめて並べる)。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from ..core import intermediate, output, renderer
from ..core.job_queue import DONE, FAILED, Job
from ..core.llm_client import LLMClient
from ..core.targets import TargetProfile
from ..core.tasks import generate
from ..util import files
from . import style


@dataclass
class Variation:
    index: int  # 1 から
    data: Optional[dict] = None
    error: str = ""


@dataclass
class ResultItem:
    variation: Variation
    target: TargetProfile
    text: str = ""
    negative: str = ""
    error: str = ""
    pending: bool = True
    saved: Optional[Path] = None
    checked: bool = True
    data: Optional[dict] = field(default=None)  # このモデル向けに直した中間表現 (なければ variation のもの)

    @property
    def intermediate(self) -> Optional[dict]:
        return self.data if self.data is not None else self.variation.data


class GenerateTab(QWidget):
    def __init__(self, targets: dict[str, TargetProfile], queue, bridge, settings: dict,
                 folder: Callable[[], str], on_saved: Callable[[list[Path]], None],
                 open_in_pwc: Callable[[Path, list[Path]], None], save_settings: Callable[[], None],
                 generate_files: Optional[Callable[[list[Path]], None]] = None,
                 add_example: Optional[Callable[[str, str], None]] = None, parent=None):
        super().__init__(parent)
        self.targets = targets
        self.add_example = add_example
        self.queue = queue
        self.settings = settings
        self.folder = folder
        self.on_saved = on_saved
        self.open_in_pwc = open_in_pwc
        self.save_settings = save_settings
        self.generate_files = generate_files
        self.items: list[ResultItem] = []
        self.variations: list[Variation] = []
        self._gen_batch = None
        self._render_batches: set[int] = set()
        self._job_item: dict[int, ResultItem] = {}
        self._loading = False
        self.last_saved: list[Path] = []
        self._build()
        bridge.event.connect(self.on_job_event)

    # ------------------------------------------------------------ 画面

    def _build(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        instruction_box = QGroupBox("指示 (ざっくりで OK)")
        ib = QVBoxLayout(instruction_box)
        self.instruction = QPlainTextEdit()
        self.instruction.setPlaceholderText("例: 夕暮れの海辺で白いワンピースの女の子が振り返る")
        ib.addWidget(self.instruction)
        top.addWidget(instruction_box, 3)

        target_box = QGroupBox("対象モデル (複数可)")
        tb = QVBoxLayout(target_box)
        self.target_list = QListWidget()
        self.refresh_targets()
        tb.addWidget(self.target_list)
        top.addWidget(target_box, 2)

        option_box = QGroupBox("オプション")
        form = QFormLayout(option_box)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 20)
        self.count_spin.setValue(2)
        form.addRow("本数", self.count_spin)
        omakase_row = QHBoxLayout()
        self.omakase = QSlider(Qt.Orientation.Horizontal)
        self.omakase.setRange(0, 100)
        self.omakase.setValue(50)
        self.omakase_label = QLabel()
        omakase_row.addWidget(self.omakase, 1)
        omakase_row.addWidget(self.omakase_label)
        form.addRow("おまかせ度", omakase_row)
        self.name_edit = QLineEdit("muse")
        self.name_edit.setToolTip("保存するファイル名の頭。「(頭)_(通し番号)_(モデル).txt」になります。番号は保存するたびに続きから付くので、名前の順が作った順になります")
        form.addRow("ファイル名", self.name_edit)
        self.generate_button = QPushButton("生成する")
        form.addRow(self.generate_button)
        top.addWidget(option_box, 2)
        layout.addLayout(top, 2)

        # 結果 | 直す欄
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.result_list = QListWidget()
        splitter.addWidget(self.result_list)

        editor = QWidget()
        ed = QVBoxLayout(editor)
        ed.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("プロンプト"))
        self.count_label = QLabel("")
        head.addStretch(1)
        head.addWidget(self.count_label)
        self.example_button = QPushButton("このプロンプトをお手本に追加")
        self.example_button.setToolTip("このモデルのプロファイルの example_prompts に足します (次から LLM に見本として渡します)")
        self.example_button.setVisible(self.add_example is not None)
        head.addWidget(self.example_button)
        ed.addLayout(head)
        self.text_edit = QPlainTextEdit()
        ed.addWidget(self.text_edit, 3)
        self.negative_label = QLabel("ネガティブ (.muse に *_neg.txt で保存)")
        ed.addWidget(self.negative_label)
        self.negative_edit = QPlainTextEdit()
        self.negative_edit.setMaximumHeight(70)
        ed.addWidget(self.negative_edit)
        jhead = QHBoxLayout()
        jhead.addWidget(QLabel("中間表現 (JSON。.muse に保存)"))
        jhead.addStretch(1)
        self.rerender_button = QPushButton("この中間表現で作り直す")
        self.rerender_button.setToolTip("JSON を直したあと、このモデル向けのプロンプトを作り直します")
        jhead.addWidget(self.rerender_button)
        ed.addLayout(jhead)
        self.json_edit = QPlainTextEdit()
        ed.addWidget(self.json_edit, 2)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 5)

        bottom = QHBoxLayout()
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet(f"color: {style.HINT};")
        bottom.addWidget(self.summary_label, 1)
        self.pwc_check = QCheckBox("保存したら PWC で開く")
        self.pwc_check.setChecked(bool(self.settings.get("notify_pwc_on_save")))
        bottom.addWidget(self.pwc_check)
        self.save_button = QPushButton("チェックしたものを保存")
        bottom.addWidget(self.save_button)
        self.save_generate_button = QPushButton("保存して生成する...")
        self.save_generate_button.setToolTip("チェックしたものを保存し、そのファイルで ComfyUI の生成の画面を開きます")
        self.save_generate_button.setVisible(self.generate_files is not None)
        bottom.addWidget(self.save_generate_button)
        self.pwc_button = QPushButton("PWC で開く")
        self.pwc_button.setToolTip("最後に保存したファイルのフォルダを PromptWordCrafter で開き、そのファイルを選びます")
        self.pwc_button.setEnabled(False)
        bottom.addWidget(self.pwc_button)
        layout.addLayout(bottom)

        self.omakase.valueChanged.connect(self._update_omakase_label)
        self._update_omakase_label()
        self.generate_button.clicked.connect(self.start)
        self.result_list.currentRowChanged.connect(self.show_item)
        self.result_list.itemChanged.connect(self.on_item_checked)
        self.text_edit.textChanged.connect(self.on_text_edited)
        self.negative_edit.textChanged.connect(self.on_negative_edited)
        self.rerender_button.clicked.connect(self.rerender_current)
        self.example_button.clicked.connect(self._add_current_example)
        self.save_button.clicked.connect(lambda: self.save_checked())
        self.save_generate_button.clicked.connect(lambda: self.save_checked(generate=True))
        self.pwc_button.clicked.connect(lambda: self.open_in_pwc(self.last_saved[0].parent, self.last_saved))
        self.pwc_check.toggled.connect(self.on_pwc_check)
        self._set_editor_enabled(False)

    def refresh_targets(self):
        """モデルプロファイルを読み直したとき。チェックは名前で残す。"""
        checked = {n for n in (self.target_list.item(r).data(Qt.ItemDataRole.UserRole)
                               for r in range(self.target_list.count())) if n} & set(self._checked_names())
        self.target_list.clear()
        for name, target in self.targets.items():
            item = QListWidgetItem(f"{name}  ({target.mode})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
            self.target_list.addItem(item)
        if not self.targets:
            self.target_list.addItem("(profiles/targets/ にモデルプロファイルがありません)")

    def _checked_names(self) -> list[str]:
        out = []
        for row in range(self.target_list.count()):
            item = self.target_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) and item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _add_current_example(self):
        item = self.current_item()
        if item is None or item.pending or item.error or self.add_example is None:
            return
        self.add_example(item.target.name, self.text_edit.toPlainText())

    def start_from_intermediates(self, datas: list[dict], targets: list[TargetProfile], name: str = "mix") -> bool:
        """中間表現はできている (ライブラリのミックスなど) ので、各モデル向けのレンダリングだけする。"""
        if self.busy():
            QMessageBox.information(self, "ざっくり生成", "作っている途中です。終わってからにしてください。")
            return False
        unsaved = [i for i in self.items if not i.pending and not i.error and i.saved is None]
        if unsaved and QMessageBox.question(
                self, "ざっくり生成", f"保存していない結果が {len(unsaved)} 件あります。捨てて作りますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return False
        self.variations = [Variation(i, data) for i, data in enumerate(datas, start=1)]
        self.items = [ResultItem(v, t) for v in self.variations for t in targets]
        self.name_edit.setText(name)
        self._submit_render(self.items, f"{name} (各モデル向け)")
        return True

    def _update_omakase_label(self):
        value = self.omakase.value()
        word = {"fill_low": "指示どおり", "fill_mid": "ほどほどに補う", "fill_high": "大胆に盛る"}[generate.fill_level(value)]
        self.omakase_label.setText(f"{value} ({word} / temp {generate.temperature_for(value)})")

    def on_pwc_check(self, checked: bool):
        self.settings["notify_pwc_on_save"] = checked
        self.save_settings()

    def selected_targets(self) -> list[TargetProfile]:
        result = []
        for row in range(self.target_list.count()):
            item = self.target_list.item(row)
            name = item.data(Qt.ItemDataRole.UserRole)
            if name and item.checkState() == Qt.CheckState.Checked:
                result.append(self.targets[name])
        return result

    def busy(self) -> bool:
        return self._gen_batch is not None or bool(self._render_batches)

    # ------------------------------------------------------------ 生成

    def start(self):
        instruction = self.instruction.toPlainText().strip()
        targets = self.selected_targets()
        if not instruction:
            QMessageBox.information(self, "ざっくり生成", "指示を入れてください。")
            return
        if not targets:
            QMessageBox.information(self, "ざっくり生成", "対象モデルを 1 つ以上チェックしてください。")
            return
        if self.busy():
            return
        unsaved = [i for i in self.items if not i.pending and not i.error and i.saved is None]
        if unsaved and QMessageBox.question(
                self, "ざっくり生成", f"保存していない結果が {len(unsaved)} 件あります。捨てて生成しますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        count = self.count_spin.value()
        omakase = self.omakase.value()
        kind = "video" if any(t.is_video for t in targets) else "image"
        self.variations = [Variation(i) for i in range(1, count + 1)]
        self.items = [ResultItem(v, t) for v in self.variations for t in targets]
        self._refresh_list()
        previous: list[str] = []
        jobs = []
        for variation in self.variations:
            jobs.append(Job("generate", f"案 {variation.index}",
                            self._make_generate(instruction, kind, omakase, variation.index, count, previous)))
        self._gen_jobs = {job.id: variation for job, variation in zip(jobs, self.variations)}
        self._gen_batch = self.queue.submit(jobs, name="ざっくり生成", quiet=True)
        self._update_buttons()

    @staticmethod
    def _make_generate(instruction, kind, omakase, index, count, previous):
        def run(profile, token):
            token.check()
            data = generate.generate_one(LLMClient(profile), instruction, kind, omakase, index, count, previous)
            previous.append(data.get("subject", ""))
            return data
        return run

    @staticmethod
    def _make_render(data: dict, target: TargetProfile):
        def run(profile, token):
            token.check()
            return renderer.render(LLMClient(profile), data, target)
        return run

    def _submit_render(self, items: list[ResultItem], name: str):
        jobs = []
        for item in items:
            data = item.intermediate
            if data is None:
                continue
            item.pending = True
            item.error = ""
            job = Job("render", f"案 {item.variation.index} / {item.target.name}", self._make_render(data, item.target))
            self._job_item[job.id] = item
            jobs.append(job)
        if jobs:
            batch = self.queue.submit(jobs, name=name, quiet=True)
            self._render_batches.add(batch.id)
        self._refresh_list()
        self._update_buttons()

    def on_job_event(self, event: str, data: dict):
        job = data.get("job")
        batch = data.get("batch")
        if event == "job_finished" and job is not None:
            if self._gen_batch is not None and job.batch is self._gen_batch:
                variation = self._gen_jobs.get(job.id)
                if variation is not None:
                    if job.status == DONE:
                        variation.data = job.result
                    else:
                        variation.error = job.error or job.status
                        for item in self.items:
                            if item.variation is variation:
                                item.pending = False
                                item.error = f"中間表現を作れません: {variation.error}"
                    self._refresh_list()
            elif job.id in self._job_item:
                item = self._job_item.pop(job.id)
                item.pending = False
                if job.status == DONE:
                    result: renderer.RenderResult = job.result
                    item.text, item.negative, item.error = result.text, result.negative, ""
                    item.saved = None
                    item.checked = True
                else:
                    item.error = job.error or job.status
                self._refresh_list()
        if event == "batch_finished" and batch is not None:
            if batch is self._gen_batch:
                self._gen_batch = None
                ready = [i for i in self.items if i.variation.data is not None]
                self._submit_render(ready, "ざっくり生成 (各モデル向け)")
            elif batch.id in self._render_batches:
                self._render_batches.discard(batch.id)
                self._update_summary()
            self._update_buttons()

    def rerender_current(self):
        item = self.current_item()
        if item is None or self.busy():
            return
        try:
            data = intermediate.load(self.json_edit.toPlainText())
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "ざっくり生成", f"中間表現の JSON を読めません: {exc}")
            return
        item.data = data
        self._submit_render([item], "作り直し")

    # ------------------------------------------------------------ 一覧と直す欄

    def current_item(self) -> Optional[ResultItem]:
        row = self.result_list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _item_label(self, item: ResultItem) -> tuple[str, str]:
        head = f"{item.variation.index:02d}  {item.target.name}"
        if item.error:
            return f"✗ {head}  {item.error}", style.ERROR
        if item.pending:
            return f"… {head}  作っています", style.HINT
        count, unit = renderer.measure(item.text, item.target.output_language)
        mark = "✓ 保存済み  " if item.saved else ""
        over = item.target.max_length and count > item.target.max_length
        text = f"{mark}{head}  {count} {unit}" + (" (目安超え)" if over else "")
        return text, ""

    def _refresh_list(self):
        self._loading = True
        row = self.result_list.currentRow()
        self.result_list.clear()
        for item in self.items:
            text, color = self._item_label(item)
            entry = QListWidgetItem(text)
            if color:
                entry.setForeground(QColor(color))
            if not item.pending and not item.error:
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                entry.setCheckState(Qt.CheckState.Checked if item.checked else Qt.CheckState.Unchecked)
            if item.saved:
                entry.setToolTip(str(item.saved))
            self.result_list.addItem(entry)
        self._loading = False
        if self.items:
            self.result_list.setCurrentRow(max(0, min(row, len(self.items) - 1)))
        self._update_summary()

    def _update_one(self, item: ResultItem):
        row = self.items.index(item)
        entry = self.result_list.item(row)
        if entry is None:
            return
        self._loading = True
        text, color = self._item_label(item)
        entry.setText(text)
        entry.setForeground(QColor(color) if color else self.result_list.palette().text().color())
        self._loading = False

    def show_item(self, row: int):
        if self._loading:
            return
        item = self.items[row] if 0 <= row < len(self.items) else None
        self._loading = True
        if item is None or item.pending or item.error:
            self.text_edit.setPlainText(item.error if item else "")
            self.negative_edit.clear()
            data = item.intermediate if item else None
            self.json_edit.setPlainText(intermediate.to_json(data) if data else "")
            self._set_editor_enabled(False)
            self.json_edit.setEnabled(bool(data))
            self.rerender_button.setEnabled(bool(data) and not self.busy())
        else:
            self.text_edit.setPlainText(item.text)
            self.negative_edit.setPlainText(item.negative)
            self.json_edit.setPlainText(intermediate.to_json(item.intermediate) if item.intermediate else "")
            self._set_editor_enabled(True)
            supported = item.target.negative_supported
            self.negative_edit.setEnabled(supported)
            self.negative_label.setText("ネガティブ (.muse に *_neg.txt で保存)" if supported
                                        else f"ネガティブ ({item.target.name} は使わないので保存しません)")
        self._loading = False
        self._update_count()

    def _set_editor_enabled(self, enabled: bool):
        for widget in (self.text_edit, self.negative_edit, self.json_edit):
            widget.setEnabled(enabled)
        self.rerender_button.setEnabled(enabled and not self.busy())

    def on_text_edited(self):
        if self._loading:
            return
        item = self.current_item()
        if item is not None and not item.pending and not item.error:
            item.text = self.text_edit.toPlainText()
            item.saved = None
            self._update_one(item)
        self._update_count()

    def on_negative_edited(self):
        if self._loading:
            return
        item = self.current_item()
        if item is not None and not item.pending and not item.error:
            item.negative = self.negative_edit.toPlainText()
            item.saved = None
            self._update_one(item)

    def on_item_checked(self, entry: QListWidgetItem):
        if self._loading:
            return
        row = self.result_list.row(entry)
        if 0 <= row < len(self.items):
            self.items[row].checked = entry.checkState() == Qt.CheckState.Checked

    def _update_count(self):
        item = self.current_item()
        if item is None or item.pending or item.error:
            self.count_label.setText("")
            return
        count, unit = renderer.measure(self.text_edit.toPlainText(), item.target.output_language)
        limit = item.target.max_length
        over = limit and count > limit
        self.count_label.setText(f"{count} {unit} / 目安 {limit}" + ("  (超えています)" if over else ""))
        self.count_label.setStyleSheet(f"color: {style.OVER if over else style.HINT};")

    def _update_summary(self):
        done = [i for i in self.items if not i.pending and not i.error]
        failed = [i for i in self.items if i.error]
        pending = [i for i in self.items if i.pending]
        parts = []
        if self.items:
            parts.append(f"{len(done)} / {len(self.items)} 件できました")
        if pending:
            parts.append(f"作っているもの {len(pending)} 件")
        if failed:
            parts.append(f"失敗 {len(failed)} 件 (一覧の赤い行)")
        self.summary_label.setText("、".join(parts))

    def _update_buttons(self):
        busy = self.busy()
        self.generate_button.setEnabled(not busy)
        self.generate_button.setText("生成中…" if busy else "生成する")
        self.save_button.setEnabled(not busy and any(not i.pending and not i.error for i in self.items))
        self.save_generate_button.setEnabled(self.save_button.isEnabled())
        item = self.current_item()
        self.rerender_button.setEnabled(not busy and item is not None and item.intermediate is not None)
        self.example_button.setEnabled(item is not None and not item.pending and not item.error)
        self.pwc_button.setEnabled(bool(self.last_saved))

    # ------------------------------------------------------------ 保存

    def save_checked(self, generate: bool = False):
        targets = [i for i in self.items if i.checked and not i.pending and not i.error and i.text.strip()]
        if not targets:
            QMessageBox.information(self, "ざっくり生成", "保存するものにチェックを付けてください。")
            return
        folder_text = self.folder().strip()
        folder = Path(folder_text) if folder_text else None
        if folder is None or not folder.is_dir():
            QMessageBox.information(self, "ざっくり生成", "上のフォルダ欄に、保存するフォルダを入れてください。")
            return
        base = files.safe_name(self.name_edit.text().strip() or "muse")
        save_json = bool(self.settings.get("save_intermediate_json", True))
        reserved: set = set()
        saved: list[Path] = []
        serial = output.next_serial(folder, base)  # 名前の順 = 作った順 (保存するたびに続きの番号)
        try:
            for n, item in enumerate(targets):
                path = output.free_prompt_path(folder, output.serial_stem(base, serial + n, item.target.slug), reserved)
                data = dict(item.intermediate or {})
                data["target"] = item.target.name  # どのモデル向けに出したか (出し直すときの手がかり)
                result = output.save_prompt(path, item.text, data, item.negative if item.target.negative_supported else "",
                                            save_json)
                item.saved = result.prompt
                item.checked = False
                saved.append(result.prompt)
        except OSError as exc:
            QMessageBox.warning(self, "ざっくり生成", f"保存できません: {exc}")
        self._refresh_list()
        if saved:
            self.last_saved = saved
            self._update_buttons()
            self.on_saved(saved)
            if generate and self.generate_files is not None:
                self.generate_files(saved)
            elif self.pwc_check.isChecked():
                self.open_in_pwc(folder, saved)
