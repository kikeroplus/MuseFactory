"""作業タブで共通の部品: 入力ファイルの一覧、フォルダの欄、変更前と変更後の並べ表示。"""

import difflib
import html
import os
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QTextEdit,
                               QVBoxLayout, QWidget)

from ..core import output
from . import style

INPUT_SUFFIXES = {".txt"}
REMOVED_BG = "#ffd7d5"
ADDED_BG = "#d4f4dd"


def prompt_files_in(folder: Path) -> list[Path]:
    """フォルダ直下のプロンプト .txt (.muse の中は見ない)。"""
    try:
        return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in INPUT_SUFFIXES),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


class InputFilesBox(QGroupBox):
    """処理するファイルの一覧。メイン画面の一覧で選んだもの・フォルダの全部・ファイルを選んで、足していく。"""

    changed = Signal()

    def __init__(self, selected_in_main: Callable[[], list[Path]], current_folder: Callable[[], str], parent=None):
        super().__init__("入力ファイル", parent)
        self.selected_in_main = selected_in_main
        self.current_folder = current_folder
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list, 1)
        row1 = QHBoxLayout()
        self.add_selected_button = QPushButton("一覧で選んだものを足す")
        self.add_selected_button.setToolTip("左のファイル一覧で選んでいる .txt を足します")
        self.add_folder_button = QPushButton("フォルダの .txt を全部")
        self.add_folder_button.setToolTip("上のフォルダ欄のフォルダ直下の .txt を全部足します")
        row1.addWidget(self.add_selected_button)
        row1.addWidget(self.add_folder_button)
        layout.addLayout(row1)
        row2 = QHBoxLayout()
        self.add_files_button = QPushButton("ファイルを選んで足す...")
        self.remove_button = QPushButton("外す")
        self.clear_button = QPushButton("全部外す")
        row2.addWidget(self.add_files_button)
        row2.addWidget(self.remove_button)
        row2.addWidget(self.clear_button)
        layout.addLayout(row2)
        self.count_label = QLabel("0 件")
        self.count_label.setStyleSheet(f"color: {style.HINT};")
        layout.addWidget(self.count_label)

        self.add_selected_button.clicked.connect(lambda: self.add([p for p in self.selected_in_main()
                                                                   if p.suffix.lower() in INPUT_SUFFIXES]))
        self.add_folder_button.clicked.connect(self.add_folder)
        self.add_files_button.clicked.connect(self.choose_files)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button.clicked.connect(self.clear)

    def files(self) -> list[Path]:
        return [Path(self.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.list.count())]

    def add(self, paths: list[Path]):
        known = {os.path.normcase(str(p)) for p in self.files()}
        for path in paths:
            key = os.path.normcase(str(path))
            if key in known or not path.is_file():
                continue
            known.add(key)
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.list.addItem(item)
        self._changed()

    def add_folder(self):
        folder = Path(self.current_folder().strip())
        if folder.is_dir():
            self.add(prompt_files_in(folder))

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "ファイルを選ぶ", self.current_folder(), "テキスト (*.txt)")
        self.add([Path(p) for p in paths])

    def remove_selected(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._changed()

    def clear(self):
        self.list.clear()
        self._changed()

    def _changed(self):
        self.count_label.setText(f"{self.list.count()} 件")
        self.changed.emit()


class FolderRow(QWidget):
    """「出力フォルダ [欄] [選択]」。"""

    def __init__(self, label: str, placeholder: str = "", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        layout.addWidget(self.edit, 1)
        button = QPushButton("選択")
        button.clicked.connect(self.choose)
        layout.addWidget(button)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, text: str):
        self.edit.setText(text)

    def choose(self):
        folder = QFileDialog.getExistingDirectory(self, "フォルダを選ぶ", self.text())
        if folder:
            self.edit.setText(os.path.normpath(folder))


def resolve_output_folder(text: str, source: Path, default_sub: str = "") -> Path:
    """出力フォルダの欄が空なら、元のファイルのフォルダ (default_sub があればその中)。"""
    if text:
        return Path(text)
    return source.parent / default_sub if default_sub else source.parent


# ---------------------------------------------------------------- 変更前と変更後

_TOKEN = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def diff_ranges(before: str, after: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """語ごとに比べて、変更前で消えた範囲と、変更後で増えた範囲 (文字の位置) を返す。"""
    a, b = tokens(before), tokens(after)
    a_pos = [0]
    for t in a:
        a_pos.append(a_pos[-1] + len(t))
    b_pos = [0]
    for t in b:
        b_pos.append(b_pos[-1] + len(t))
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("delete", "replace") and i2 > i1:
            removed.append((a_pos[i1], a_pos[i2]))
        if op in ("insert", "replace") and j2 > j1:
            added.append((b_pos[j1], b_pos[j2]))
    return removed, added


class DiffView(QWidget):
    """左に変更前 (消えたところに色)、右に変更後 (増えたところに色。直せる)。"""

    edited = Signal()

    def __init__(self, before_label: str = "変更前", after_label: str = "変更後 (直せます)", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.before_label = QLabel(before_label)
        lv.addWidget(self.before_label)
        self.before = QTextEdit()
        self.before.setReadOnly(True)
        lv.addWidget(self.before)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.after_label = QLabel(after_label)
        head.addWidget(self.after_label)
        head.addStretch(1)
        self.info_label = QLabel("")
        head.addWidget(self.info_label)
        rv.addLayout(head)
        self.after = QPlainTextEdit()
        rv.addWidget(self.after)
        splitter.addWidget(left)
        splitter.addWidget(right)
        layout.addWidget(splitter)
        self._before_text = ""
        self._loading = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._highlight)
        self.after.textChanged.connect(self._on_edit)

    def set_texts(self, before: str, after: str, editable: bool = True):
        self._loading = True
        self._before_text = before
        self.after.setPlainText(after)
        self.after.setReadOnly(not editable)
        self._loading = False
        self._highlight()

    def text(self) -> str:
        return self.after.toPlainText()

    def _on_edit(self):
        if not self._loading:
            self._timer.start()
            self.edited.emit()

    def _highlight(self):
        before = self._before_text
        after = self.after.toPlainText()
        removed, added = diff_ranges(before, after)
        # 変更前: HTML で消えたところに色
        parts, pos = [], 0
        for start, end in removed:
            parts.append(html.escape(before[pos:start]))
            parts.append(f'<span style="background:{REMOVED_BG}">{html.escape(before[start:end])}</span>')
            pos = end
        parts.append(html.escape(before[pos:]))
        self.before.setHtml('<div style="white-space: pre-wrap;">' + "".join(parts) + "</div>")
        # 変更後: 増えたところに色 (文字は直せるまま)
        selections = []
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(ADDED_BG))
        for start, end in added:
            sel = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self.after.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)
        self.after.setExtraSelections(selections)
