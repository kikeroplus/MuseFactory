"""作業タブ「ストーリー」(SPEC 5.4)。

① 指示 (と、AI の確認の質問) → ② 設定シートと構成案 (直せる・保存できる) → ③ 各シーンのプロンプト (直して保存)。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSlider, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import paths
from ..core import output, renderer
from ..core.job_queue import CANCELLED, DONE, Job
from ..core.llm_client import LLMClient
from ..core.targets import TargetProfile
from ..core.tasks import generate as generate_task
from ..core.tasks import story
from ..util import files
from . import style
from .common import FolderRow

STORIES_DIR = paths.LIBRARY_DIR / "stories"
CHARACTERS_DIR = paths.LIBRARY_DIR / "characters"


@dataclass
class StoryItem:
    scene: dict
    variation: str  # サブフォルダ名 ("" ならなし)
    video: bool
    target: TargetProfile
    text: str = ""
    data: Optional[dict] = None
    error: str = ""
    pending: bool = True
    saved: Optional[Path] = None
    checked: bool = True

    @property
    def label(self) -> str:
        head = f"{story.scene_file_stem(self.scene)}"
        if self.variation:
            head = f"[{self.variation}] {head}"
        return head + ("  (動画)" if self.video else "")


def _item(text: str, editable: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if not editable:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class StoryTab(QWidget):
    def __init__(self, targets: dict[str, TargetProfile], queue, bridge, settings: dict,
                 current_folder: Callable[[], str], on_saved: Callable[[list[Path]], None],
                 open_in_pwc: Callable[[Path, list[Path]], None],
                 generate_files: Optional[Callable[[list[Path]], None]] = None, parent=None):
        super().__init__(parent)
        self.targets = targets
        self.queue = queue
        self.settings = settings
        self.current_folder = current_folder
        self.on_saved = on_saved
        self.open_in_pwc = open_in_pwc
        self.generate_files = generate_files
        self.fixed_characters: list[dict] = []
        self.questions: list[str] = []
        self.items: list[StoryItem] = []
        self.sheet_used: Optional[dict] = None  # 各シーンを作ったときの設定シート
        self._plan_batch = None
        self._scene_batch = None
        self._job_scene: dict[int, int] = {}
        self._loading = False
        self.last_saved: list[Path] = []
        self._build()
        bridge.event.connect(self.on_job_event)

    # ------------------------------------------------------------ 画面

    def _build(self):
        layout = QVBoxLayout(self)
        vsplit = QSplitter(Qt.Orientation.Vertical)
        top = QSplitter(Qt.Orientation.Horizontal)

        # ① 指示
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("① 指示")
        bv = QVBoxLayout(box)
        self.instruction = QPlainTextEdit()
        self.instruction.setPlaceholderText("例: ファンタジーで勇者が魔王と戦ってる一連のやつ")
        bv.addWidget(self.instruction, 1)
        form = QFormLayout()
        self.scene_spin = QSpinBox()
        self.scene_spin.setRange(0, 30)
        self.scene_spin.setSpecialValueText("おまかせ")
        form.addRow("シーン数", self.scene_spin)
        self.tempo_combo = QComboBox()
        self.tempo_combo.setEditable(True)
        self.tempo_combo.addItems(["", "起承転結", "クライマックス多め", "静かに始まり、最後に盛り上がる", "テンポよく短く"])
        form.addRow("テンポ", self.tempo_combo)
        omakase_row = QHBoxLayout()
        self.omakase = QSlider(Qt.Orientation.Horizontal)
        self.omakase.setRange(0, 100)
        self.omakase.setValue(60)
        self.omakase_label = QLabel()
        omakase_row.addWidget(self.omakase, 1)
        omakase_row.addWidget(self.omakase_label)
        form.addRow("おまかせ度", omakase_row)
        self.ask_check = QCheckBox("AI に質問させる (最大 3 問)")
        form.addRow(self.ask_check)
        bv.addLayout(form)
        chars_row = QHBoxLayout()
        chars_row.addWidget(QLabel("使うキャラ"))
        self.load_char_button = QPushButton("キャラを読み込む...")
        self.load_char_button.setToolTip("library/characters に保存したキャラを、この話にも出します (見た目はそのまま)")
        self.remove_char_button = QPushButton("外す")
        chars_row.addStretch(1)
        chars_row.addWidget(self.load_char_button)
        chars_row.addWidget(self.remove_char_button)
        bv.addLayout(chars_row)
        self.fixed_list = QListWidget()
        self.fixed_list.setMaximumHeight(60)
        bv.addWidget(self.fixed_list)
        self.plan_button = QPushButton("構成案を作る")
        bv.addWidget(self.plan_button)
        lv.addWidget(box, 1)

        self.question_box = QGroupBox("AI からの質問 (空欄はおまかせ)")
        self.question_form = QFormLayout(self.question_box)
        qbuttons = QHBoxLayout()
        self.answer_button = QPushButton("回答して構成案を作る")
        self.skip_button = QPushButton("質問をとばす")
        qbuttons.addWidget(self.answer_button)
        qbuttons.addWidget(self.skip_button)
        self.question_buttons = QWidget()
        self.question_buttons.setLayout(qbuttons)
        self.question_box.hide()
        lv.addWidget(self.question_box)
        top.addWidget(left)

        # ② 設定シートと構成案
        sheet_box = QGroupBox("② 設定シートと構成案 (直せます)")
        sv = QVBoxLayout(sheet_box)
        sform = QFormLayout()
        self.title_edit = QLineEdit()
        self.world_edit = QLineEdit()
        self.world_edit.setToolTip("全シーンの style に入ります (英語)")
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setToolTip("全シーンのプロンプトの末尾に付けます (英語)")
        sform.addRow("タイトル", self.title_edit)
        sform.addRow("世界観 (world)", self.world_edit)
        sform.addRow("共通の末尾", self.suffix_edit)
        sv.addLayout(sform)
        self.char_table = QTableWidget(0, 3)
        self.char_table.setHorizontalHeaderLabels(["id", "名前", "見た目 (look。全シーンにそのまま入ります)"])
        self.char_table.verticalHeader().setVisible(False)
        self.char_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.char_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.char_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        sv.addWidget(self.char_table, 2)
        crow = QHBoxLayout()
        self.add_char_button = QPushButton("キャラを足す")
        self.del_char_button = QPushButton("キャラを消す")
        self.save_char_button = QPushButton("選んだキャラをライブラリに保存")
        for b in (self.add_char_button, self.del_char_button, self.save_char_button):
            crow.addWidget(b)
        crow.addStretch(1)
        sv.addLayout(crow)
        self.scene_table = QTableWidget(0, 4)
        self.scene_table.setHorizontalHeaderLabels(["No", "タイトル", "内容", "登場 (id をカンマで)"])
        self.scene_table.verticalHeader().setVisible(False)
        self.scene_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        sh = self.scene_table.horizontalHeader()
        sh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        sh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        sh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        sh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        sv.addWidget(self.scene_table, 3)
        srow = QHBoxLayout()
        self.add_scene_button = QPushButton("シーンを足す")
        self.del_scene_button = QPushButton("シーンを消す")
        self.up_button = QPushButton("▲ 上へ")
        self.down_button = QPushButton("▼ 下へ")
        self.save_sheet_button = QPushButton("設定シートを保存...")
        self.load_sheet_button = QPushButton("読み込む...")
        for b in (self.add_scene_button, self.del_scene_button, self.up_button, self.down_button):
            srow.addWidget(b)
        srow.addStretch(1)
        srow.addWidget(self.save_sheet_button)
        srow.addWidget(self.load_sheet_button)
        sv.addLayout(srow)
        top.addWidget(sheet_box)
        top.setStretchFactor(0, 2)
        top.setStretchFactor(1, 5)
        vsplit.addWidget(top)

        # ③ 各シーン
        out_box = QGroupBox("③ 各シーンのプロンプト")
        ov = QVBoxLayout(out_box)
        orow = QHBoxLayout()
        orow.addWidget(QLabel("画像のモデル"))
        self.image_combo = QComboBox()
        for name, t in self.targets.items():
            if not t.is_video:
                self.image_combo.addItem(name)
        orow.addWidget(self.image_combo)
        self.video_check = QCheckBox("動画用もペアで")
        orow.addWidget(self.video_check)
        self.video_combo = QComboBox()
        for name, t in self.targets.items():
            if t.is_video:
                self.video_combo.addItem(name)
        orow.addWidget(self.video_combo)
        self.seconds_spin = QDoubleSpinBox()
        self.seconds_spin.setRange(1, 30)
        self.seconds_spin.setValue(5)
        self.seconds_spin.setSuffix(" 秒")
        orow.addWidget(self.seconds_spin)
        orow.addStretch(1)
        ov.addLayout(orow)
        vrow = QHBoxLayout()
        self.variations_edit = QPlainTextEdit()
        self.variations_edit.setPlaceholderText("バリエーション (1 行 1 つ。サブフォルダに分けて出します)\n"
                                                "例: 昼版: bright daylight\n    夜版: night, moonlight")
        self.variations_edit.setMaximumHeight(64)
        vrow.addWidget(self.variations_edit, 2)
        right = QVBoxLayout()
        self.out_folder = FolderRow("出力フォルダ", "空なら フォルダ欄のフォルダ\\(タイトル)")
        right.addWidget(self.out_folder)
        self.scenes_button = QPushButton("各シーンを作る")
        right.addWidget(self.scenes_button)
        vrow.addLayout(right, 3)
        ov.addLayout(vrow)
        rsplit = QSplitter(Qt.Orientation.Horizontal)
        self.result_list = QListWidget()
        rsplit.addWidget(self.result_list)
        self.result_edit = QPlainTextEdit()
        rsplit.addWidget(self.result_edit)
        rsplit.setStretchFactor(0, 1)
        rsplit.setStretchFactor(1, 2)
        ov.addWidget(rsplit, 1)
        brow = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {style.HINT};")
        brow.addWidget(self.summary, 1)
        self.save_button = QPushButton("チェックしたものを保存")
        self.save_generate_button = QPushButton("保存して生成する...")
        self.save_generate_button.setVisible(self.generate_files is not None)
        self.pwc_button = QPushButton("PWC で開く")
        for b in (self.save_button, self.save_generate_button, self.pwc_button):
            b.setEnabled(False)
            brow.addWidget(b)
        ov.addLayout(brow)
        vsplit.addWidget(out_box)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        layout.addWidget(vsplit)

        self.omakase.valueChanged.connect(self._update_omakase_label)
        self._update_omakase_label()
        self.plan_button.clicked.connect(self.start_plan)
        self.answer_button.clicked.connect(lambda: self.submit_plan(self._answers()))
        self.skip_button.clicked.connect(lambda: self.submit_plan([]))
        self.load_char_button.clicked.connect(self.load_fixed_character)
        self.remove_char_button.clicked.connect(self.remove_fixed_character)
        self.add_char_button.clicked.connect(lambda: self._append_char({"id": "", "name": "", "look": ""}))
        self.del_char_button.clicked.connect(lambda: self._delete_rows(self.char_table))
        self.save_char_button.clicked.connect(self.save_characters)
        self.add_scene_button.clicked.connect(self.add_scene)
        self.del_scene_button.clicked.connect(lambda: (self._delete_rows(self.scene_table), self._renumber()))
        self.up_button.clicked.connect(lambda: self.move_scene(-1))
        self.down_button.clicked.connect(lambda: self.move_scene(1))
        self.save_sheet_button.clicked.connect(self.save_sheet)
        self.load_sheet_button.clicked.connect(self.load_sheet)
        self.video_check.toggled.connect(self._update_video_widgets)
        self.scenes_button.clicked.connect(self.start_scenes)
        self.result_list.currentRowChanged.connect(self.show_item)
        self.result_list.itemChanged.connect(self._on_checked)
        self.result_edit.textChanged.connect(self._on_edited)
        self.save_button.clicked.connect(lambda: self.save())
        self.save_generate_button.clicked.connect(lambda: self.save(generate=True))
        self.pwc_button.clicked.connect(lambda: self.open_in_pwc(self.last_saved[0].parent, self.last_saved))
        self._update_video_widgets()

    def _update_omakase_label(self):
        value = self.omakase.value()
        word = {"fill_low": "指示どおり", "fill_mid": "ほどほどに補う", "fill_high": "大胆に"}[generate_task.fill_level(value)]
        self.omakase_label.setText(f"{value} ({word})")

    def _update_video_widgets(self):
        on = self.video_check.isChecked()
        self.video_combo.setEnabled(on)
        self.seconds_spin.setEnabled(on)

    def busy(self) -> bool:
        return self._plan_batch is not None or self._scene_batch is not None

    def _update_buttons(self):
        busy = self.busy()
        self.plan_button.setEnabled(not busy)
        self.answer_button.setEnabled(not busy)
        self.skip_button.setEnabled(not busy)
        self.scenes_button.setEnabled(not busy)
        self.plan_button.setText("作っています…" if self._plan_batch is not None else "構成案を作る")
        self.scenes_button.setText("作っています…" if self._scene_batch is not None else "各シーンを作る")
        has = any(not i.pending and not i.error for i in self.items)
        self.save_button.setEnabled(not busy and has)
        self.save_generate_button.setEnabled(not busy and has)
        self.pwc_button.setEnabled(bool(self.last_saved))

    # ------------------------------------------------------------ ① 指示 → ② 構成案

    def start_plan(self):
        instruction = self.instruction.toPlainText().strip()
        if not instruction:
            QMessageBox.information(self, "ストーリー", "指示を入れてください。")
            return
        if self.busy():
            return
        if self.ask_check.isChecked():
            job = Job("story_plan", "確認の質問", lambda p, t: (t.check(), story.ask_questions(LLMClient(p), instruction))[1])
            job.kind = "questions"  # type: ignore[attr-defined]
            self._plan_batch = self.queue.submit([job], name="ストーリー: 質問", quiet=True)
        else:
            self.submit_plan([])
        self._update_buttons()

    def _show_questions(self, questions: list[str]):
        while self.question_form.rowCount():
            self.question_form.removeRow(0)
        self.questions = questions
        self.question_edits = []
        for q in questions:
            edit = QLineEdit()
            label = QLabel(q)
            label.setWordWrap(True)
            self.question_form.addRow(label)
            self.question_form.addRow(edit)
            self.question_edits.append(edit)
        self.question_buttons = QWidget()
        row = QHBoxLayout(self.question_buttons)
        row.setContentsMargins(0, 0, 0, 0)
        self.answer_button = QPushButton("回答して構成案を作る")
        self.skip_button = QPushButton("質問をとばす")
        row.addWidget(self.answer_button)
        row.addWidget(self.skip_button)
        self.answer_button.clicked.connect(lambda: self.submit_plan(self._answers()))
        self.skip_button.clicked.connect(lambda: self.submit_plan([]))
        self.question_form.addRow(self.question_buttons)
        self.question_box.show()

    def _answers(self) -> list[tuple[str, str]]:
        return [(q, e.text().strip()) for q, e in zip(self.questions, getattr(self, "question_edits", []))]

    def submit_plan(self, answers: list[tuple[str, str]]):
        if self._plan_batch is not None:
            return
        self.question_box.hide()
        instruction = self.instruction.toPlainText().strip()
        count = self.scene_spin.value()
        tempo = self.tempo_combo.currentText()
        omakase = self.omakase.value()
        fixed = [dict(c) for c in self.fixed_characters]

        def run(profile, token):
            token.check()
            return story.plan(LLMClient(profile), instruction, count, tempo, omakase, answers, fixed)

        job = Job("story_plan", "構成案", run)
        job.kind = "plan"  # type: ignore[attr-defined]
        self._plan_batch = self.queue.submit([job], name="ストーリー: 構成案", quiet=True)
        self.summary.setText("構成案を作っています…")
        self._update_buttons()

    # ------------------------------------------------------------ 設定シートの表

    def set_sheet(self, sheet: dict):
        self.title_edit.setText(sheet.get("title", ""))
        self.world_edit.setText(sheet.get("world", ""))
        self.suffix_edit.setText(sheet.get("common_suffix", ""))
        self.char_table.setRowCount(0)
        for c in sheet.get("characters", []):
            self._append_char(c)
        self.scene_table.setRowCount(0)
        for s in sheet.get("scenes", []):
            self._append_scene(s)

    def _append_char(self, c: dict):
        row = self.char_table.rowCount()
        self.char_table.insertRow(row)
        for col, key in enumerate(("id", "name", "look")):
            self.char_table.setItem(row, col, _item(c.get(key, "")))

    def _append_scene(self, s: dict, row: Optional[int] = None):
        row = self.scene_table.rowCount() if row is None else row
        self.scene_table.insertRow(row)
        self.scene_table.setItem(row, 0, _item(str(s.get("no", row + 1)), editable=False))
        self.scene_table.setItem(row, 1, _item(s.get("title", "")))
        self.scene_table.setItem(row, 2, _item(s.get("summary", "")))
        self.scene_table.setItem(row, 3, _item(", ".join(s.get("characters", []))))

    def add_scene(self):
        rows = sorted({i.row() for i in self.scene_table.selectedIndexes()})
        row = rows[-1] + 1 if rows else self.scene_table.rowCount()
        self._append_scene({"title": "", "summary": "", "characters": []}, row)
        self._renumber()

    def _delete_rows(self, table: QTableWidget):
        for row in sorted({i.row() for i in table.selectedIndexes()}, reverse=True):
            table.removeRow(row)

    def move_scene(self, step: int):
        row = self.scene_table.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.scene_table.rowCount():
            return
        for col in range(1, 4):
            a, b = self.scene_table.takeItem(row, col), self.scene_table.takeItem(target, col)
            self.scene_table.setItem(row, col, b)
            self.scene_table.setItem(target, col, a)
        self.scene_table.selectRow(target)
        self._renumber()

    def _renumber(self):
        for row in range(self.scene_table.rowCount()):
            self.scene_table.setItem(row, 0, _item(str(row + 1), editable=False))

    def _text(self, table: QTableWidget, row: int, col: int) -> str:
        item = table.item(row, col)
        return item.text().strip() if item else ""

    def sheet_from_ui(self) -> dict:
        raw = {
            "title": self.title_edit.text(), "world": self.world_edit.text(),
            "common_suffix": self.suffix_edit.text(),
            "characters": [{"id": self._text(self.char_table, r, 0), "name": self._text(self.char_table, r, 1),
                            "look": self._text(self.char_table, r, 2)} for r in range(self.char_table.rowCount())],
            "scenes": [{"title": self._text(self.scene_table, r, 1), "summary": self._text(self.scene_table, r, 2),
                        "characters": [x.strip() for x in self._text(self.scene_table, r, 3).replace("、", ",").split(",")
                                       if x.strip()]}
                       for r in range(self.scene_table.rowCount())],
        }
        return story.normalize_sheet(raw)

    def save_sheet(self):
        sheet = self.sheet_from_ui()
        try:
            path = story.save_sheet(sheet, STORIES_DIR)
        except OSError as exc:
            QMessageBox.warning(self, "ストーリー", f"保存できません: {exc}")
            return
        self.summary.setText(f"設定シートを保存しました: {path}")

    def load_sheet(self):
        path, _ = QFileDialog.getOpenFileName(self, "設定シートを読み込む", str(STORIES_DIR), "設定シート (*.json)")
        if not path:
            return
        try:
            self.set_sheet(story.load_sheet(Path(path)))
        except ValueError as exc:
            QMessageBox.warning(self, "ストーリー", str(exc))
            return
        self.summary.setText(f"設定シートを読み込みました: {Path(path).name}")

    def save_characters(self):
        rows = sorted({i.row() for i in self.char_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "ストーリー", "保存するキャラを表で選んでください。")
            return
        saved = []
        for r in rows:
            c = {"id": self._text(self.char_table, r, 0), "name": self._text(self.char_table, r, 1),
                 "look": self._text(self.char_table, r, 2)}
            if c["look"] or c["name"]:
                saved.append(story.save_character(c, CHARACTERS_DIR).name)
        self.summary.setText("ライブラリに保存しました: " + ", ".join(saved))

    def load_fixed_character(self):
        paths_, _ = QFileDialog.getOpenFileNames(self, "キャラを読み込む", str(CHARACTERS_DIR), "キャラ (*.json)")
        for p in paths_:
            try:
                c = story.load_character(Path(p))
            except ValueError as exc:
                QMessageBox.warning(self, "ストーリー", str(exc))
                continue
            self.fixed_characters.append(c)
            item = QListWidgetItem(f"{c['name']}  ({c['look'][:60]})")
            item.setToolTip(c["look"])
            self.fixed_list.addItem(item)

    def remove_fixed_character(self):
        row = self.fixed_list.currentRow()
        if row >= 0:
            self.fixed_list.takeItem(row)
            del self.fixed_characters[row]

    # ------------------------------------------------------------ ③ 各シーン

    def start_scenes(self):
        if self.busy():
            return
        sheet = self.sheet_from_ui()
        if not sheet["scenes"]:
            QMessageBox.information(self, "ストーリー", "先に構成案を作るか、シーンを足してください。")
            return
        image_target = self.targets.get(self.image_combo.currentText())
        video_target = self.targets.get(self.video_combo.currentText()) if self.video_check.isChecked() else None
        if image_target is None:
            QMessageBox.information(self, "ストーリー", "画像のモデルを選んでください。")
            return
        if self.video_check.isChecked() and video_target is None:
            QMessageBox.information(self, "ストーリー", "動画のモデルを選んでください。")
            return
        unsaved = [i for i in self.items if not i.pending and not i.error and i.saved is None]
        if unsaved and QMessageBox.question(
                self, "ストーリー", f"保存していない結果が {len(unsaved)} 件あります。捨てて作り直しますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.set_sheet(sheet)  # 直した結果 (id の振り直しなど) を表にも出す
        variations = story.parse_variations(self.variations_edit.toPlainText()) or [None]
        seconds = self.seconds_spin.value()
        self.sheet_used = sheet
        self.items = []
        jobs = []
        self._job_scene.clear()
        for index, scene in enumerate(sheet["scenes"]):
            for v in variations:
                self.items.append(StoryItem(scene, v.label if v else "", False, image_target))
                if video_target is not None:
                    self.items.append(StoryItem(scene, v.label if v else "", True, video_target))
            job = Job("render", story.scene_file_stem(scene),
                      self._make_scene_job(sheet, index, variations, image_target, video_target, seconds))
            self._job_scene[job.id] = scene["no"]
            jobs.append(job)
        self._refresh_list()
        self._scene_batch = self.queue.submit(jobs, name="ストーリー: 各シーン", quiet=True)
        self._update_buttons()

    @staticmethod
    def _make_scene_job(sheet, index, variations, image_target, video_target, seconds):
        def run(profile, token):
            client = LLMClient(profile)
            scene = sheet["scenes"][index]
            token.check()
            base = story.scene_intermediate(client, sheet, index)
            out = []  # (変化の名前, 動画か, 文, 中間表現)
            for v in variations:
                token.check()
                data = story.with_variation(base, v)
                r = story.render_scene(client, sheet, scene, data, image_target)
                out.append((v.label if v else "", False, r.text, data))
            if video_target is not None:
                token.check()
                vbase = story.video_intermediate(client, sheet, index, base, seconds)
                for v in variations:
                    token.check()
                    data = story.with_variation(vbase, v)
                    r = story.render_scene(client, sheet, scene, data, video_target)
                    out.append((v.label if v else "", True, r.text, data))
            return out
        return run

    def on_job_event(self, event: str, data: dict):
        job = data.get("job")
        batch = data.get("batch")
        if event == "job_finished" and job is not None and self._plan_batch is not None and job.batch is self._plan_batch:
            kind = getattr(job, "kind", "")
            if job.status == DONE and kind == "questions":
                self._plan_batch = None
                if job.result:
                    self._show_questions(job.result)
                    self.summary.setText("AI からの質問に答えてください (とばしても作れます)。")
                else:
                    self.summary.setText("質問はありませんでした。構成案を作ります。")
                    self.submit_plan([])
            elif job.status == DONE and kind == "plan":
                self.set_sheet(job.result)
                self.summary.setText(f"構成案ができました ({len(job.result['scenes'])} シーン)。直してから「各シーンを作る」へ。")
            elif job.status == CANCELLED:
                self.summary.setText("キャンセルしました")
            else:
                self.summary.setText(f"✗ {job.error or job.status}")
        if event == "job_finished" and job is not None and job.id in self._job_scene:
            no = self._job_scene.pop(job.id)
            for item in self.items:
                if item.scene["no"] != no:
                    continue
                item.pending = False
                if job.status == DONE:
                    match = next((r for r in job.result if r[0] == item.variation and r[1] == item.video), None)
                    if match:
                        item.text, item.data, item.error = match[2], match[3], ""
                    else:
                        item.error = "結果がありません"
                elif job.status == CANCELLED:
                    item.error = "キャンセルしました"
                else:
                    item.error = job.error or job.status
            self._refresh_list()
        if event == "batch_finished" and batch is not None:
            if batch is self._plan_batch:
                self._plan_batch = None
            if batch is self._scene_batch:
                self._scene_batch = None
            self._update_buttons()

    # ------------------------------------------------------------ 結果の一覧

    def _refresh_list(self):
        self._loading = True
        row = self.result_list.currentRow()
        self.result_list.clear()
        for item in self.items:
            if item.error:
                text, color = f"✗ {item.label}  {item.error}", (style.HINT if item.error == "キャンセルしました" else style.ERROR)
            elif item.pending:
                text, color = f"… {item.label}", style.HINT
            else:
                count, unit = renderer.measure(item.text, item.target.output_language)
                text = ("✓ 保存済み  " if item.saved else "") + f"{item.label}  {count} {unit}"
                color = style.WARN if item.target.max_length and count > item.target.max_length else ""
            entry = QListWidgetItem(text)
            if color:
                entry.setForeground(QColor(color))
            if not item.pending and not item.error:
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                entry.setCheckState(Qt.CheckState.Checked if item.checked else Qt.CheckState.Unchecked)
            self.result_list.addItem(entry)
        self._loading = False
        if self.items:
            self.result_list.setCurrentRow(max(0, min(row, len(self.items) - 1)))
        done = sum(1 for i in self.items if not i.pending and not i.error)
        self.summary.setText(f"{done} / {len(self.items)} 件できました" if self.items else "")
        self._update_buttons()

    def current_item(self) -> Optional[StoryItem]:
        row = self.result_list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def show_item(self, _row: int):
        if self._loading:
            return
        item = self.current_item()
        self._loading = True
        if item is None or item.pending or item.error:
            self.result_edit.setPlainText(item.error if item else "")
            self.result_edit.setReadOnly(True)
        else:
            self.result_edit.setPlainText(item.text)
            self.result_edit.setReadOnly(False)
        self._loading = False

    def _on_edited(self):
        if self._loading:
            return
        item = self.current_item()
        if item is not None and not item.pending and not item.error:
            item.text = self.result_edit.toPlainText()
            item.saved = None

    def _on_checked(self, entry: QListWidgetItem):
        if self._loading:
            return
        row = self.result_list.row(entry)
        if 0 <= row < len(self.items):
            self.items[row].checked = entry.checkState() == Qt.CheckState.Checked

    # ------------------------------------------------------------ 保存

    def output_base(self) -> Path:
        text = self.out_folder.text()
        if text:
            return Path(text)
        title = (self.sheet_used or {}).get("title") or self.title_edit.text() or "story"
        return Path(self.current_folder().strip() or ".") / files.safe_name(title)

    def save(self, generate: bool = False):
        items = [i for i in self.items if i.checked and not i.pending and not i.error and i.text.strip()]
        if not items:
            QMessageBox.information(self, "ストーリー", "保存するものにチェックを付けてください。")
            return
        base = self.output_base()
        save_json = bool(self.settings.get("save_intermediate_json", True))
        reserved: set = set()
        saved: list[Path] = []
        try:
            for item in items:
                folder = base / item.variation if item.variation else base
                if item.video:
                    folder = folder / output.VIDEO_DIR
                stem = story.scene_file_stem(item.scene) + ("_video" if item.video else "")
                path = output.free_prompt_path(folder, stem, reserved)
                data = dict(item.data or {})
                data["target"] = item.target.name
                output.save_prompt(path, item.text, data if item.data else None, "", save_json)
                item.saved = path
                item.checked = False
                saved.append(path)
            if self.sheet_used is not None:
                # どの設定シートで作ったかを .muse に残す (あとで同じキャラで続きを作れるように)
                files.write_json(base / output.MUSE_DIR / "story_sheet.json", self.sheet_used)
        except OSError as exc:
            QMessageBox.warning(self, "ストーリー", f"保存できません: {exc}")
        self._refresh_list()
        if saved:
            self.last_saved = [p for p in saved if p.parent.name != output.VIDEO_DIR] or saved
            self.on_saved(saved)
            self.summary.setText(f"{len(saved)} 件を保存しました: {base}")
            if generate and self.generate_files is not None:
                self.generate_files(saved, what=f"ストーリー「{(self.sheet_used or {}).get('title', '')}」")
        self._update_buttons()

    def open_output(self):
        base = self.output_base()
        if base.is_dir():
            os.startfile(str(base))
