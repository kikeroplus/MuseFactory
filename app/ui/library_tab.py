"""作業タブ「ライブラリ」(SPEC 5.5)。

左: カテゴリ / 真ん中: 項目 (検索・★・削除・足す・ワイルドカードで書き出す) / 下: ミックス生成。
ミックスは LLM を使わずに組み合わせて中間表現を作り、各モデル向けのレンダリングは「ざっくり生成」のタブで行う
(プレビュー・直す・保存・お手本は、そちらの画面をそのまま使う)。
"""

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import intermediate, library
from ..core.targets import TargetProfile
from . import style

UNUSED, RANDOM, RANDOM_FAV = "(使わない)", "ランダム", "★からランダム"


class LibraryTab(QWidget):
    def __init__(self, targets: dict[str, TargetProfile], current_folder: Callable[[], str],
                 on_mix: Callable[[list[dict], list[TargetProfile]], bool], parent=None):
        super().__init__(parent)
        self.targets = targets
        self.current_folder = current_folder
        self.on_mix = on_mix
        self.items: list[dict] = []
        self.visible_rows: list[int] = []  # 表の行 → items の番号
        self.mix_combos: dict[str, QComboBox] = {}
        self._build()
        self.reload()

    def _build(self):
        layout = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        top = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("カテゴリ"))
        self.category_list = QListWidget()
        lv.addWidget(self.category_list, 1)
        self.new_category_button = QPushButton("カテゴリを足す...")
        lv.addWidget(self.new_category_button)
        self.reload_button = QPushButton("読み直す")
        lv.addWidget(self.reload_button)
        top.addWidget(left)

        middle = QWidget()
        mv = QVBoxLayout(middle)
        mv.setContentsMargins(0, 0, 0, 0)
        srow = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("検索 (空白で区切ると全部を含むもの)")
        self.fav_only = QCheckBox("★だけ")
        srow.addWidget(self.search_edit, 1)
        srow.addWidget(self.fav_only)
        mv.addLayout(srow)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["★", "テキスト", "元のファイル", "登録日"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        mv.addWidget(self.table, 1)
        arow = QHBoxLayout()
        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("項目を足す (Enter)")
        self.fav_button = QPushButton("★ を付ける / 外す")
        self.delete_button = QPushButton("削除")
        self.export_button = QPushButton("ワイルドカードで書き出す...")
        self.export_button.setToolTip("表に出ているもの (選んでいればそれだけ) を、1 行 1 項目の .txt に書き出します")
        arow.addWidget(self.add_edit, 1)
        for b in (self.fav_button, self.delete_button, self.export_button):
            arow.addWidget(b)
        mv.addLayout(arow)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet(f"color: {style.HINT};")
        mv.addWidget(self.count_label)
        top.addWidget(middle)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 4)
        split.addWidget(top)

        mix_box = QGroupBox("ミックス生成 (カテゴリごとに選んで組み合わせ、各モデル向けに書く)")
        mx = QHBoxLayout(mix_box)
        self.mix_grid_widget = QWidget()
        self.mix_grid = QGridLayout(self.mix_grid_widget)
        self.mix_grid.setContentsMargins(0, 0, 0, 0)
        mx.addWidget(self.mix_grid_widget, 3)
        right = QVBoxLayout()
        right.addWidget(QLabel("対象モデル"))
        self.mix_targets = QListWidget()
        right.addWidget(self.mix_targets, 1)
        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("本数"))
        self.mix_count = QSpinBox()
        self.mix_count.setRange(1, 50)
        self.mix_count.setValue(4)
        nrow.addWidget(self.mix_count)
        nrow.addStretch(1)
        right.addLayout(nrow)
        self.mix_button = QPushButton("ミックスして作る")
        self.mix_button.setToolTip("組み合わせた中間表現を、「ざっくり生成」のタブで各モデル向けに書きます")
        right.addWidget(self.mix_button)
        mx.addLayout(right, 2)
        split.addWidget(mix_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split)

        self.category_list.currentRowChanged.connect(lambda _r: self.load_items())
        self.search_edit.textChanged.connect(lambda _t: self.fill_table())
        self.fav_only.toggled.connect(lambda _c: self.fill_table())
        self.table.cellDoubleClicked.connect(lambda r, c: self.toggle_favorite() if c == 0 else None)
        self.add_edit.returnPressed.connect(self.add_item)
        self.fav_button.clicked.connect(self.toggle_favorite)
        self.delete_button.clicked.connect(self.delete_items)
        self.export_button.clicked.connect(self.export)
        self.new_category_button.clicked.connect(self.new_category)
        self.reload_button.clicked.connect(self.reload)
        self.mix_button.clicked.connect(self.start_mix)
        self.refresh_targets()

    # ------------------------------------------------------------ 一覧

    def category(self) -> str:
        item = self.category_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def reload(self):
        current = self.category()
        names = library.categories()
        for element in intermediate.ELEMENTS + ["characters"]:
            if element not in names:
                names.append(element)
        self.category_list.blockSignals(True)
        self.category_list.clear()
        for name in names:
            count = len(library.load(name))
            item = QListWidgetItem(f"{name}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            if not count:
                item.setForeground(Qt.GlobalColor.gray)
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)
        names_now = [self.category_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.category_list.count())]
        self.category_list.setCurrentRow(names_now.index(current) if current in names_now else 0)
        self.load_items()
        self._build_mix_rows(names)

    def load_items(self):
        self.items = library.load(self.category()) if self.category() else []
        self.fill_table()

    def fill_table(self):
        self.visible_rows = library.search(self.items, self.search_edit.text(), self.fav_only.isChecked())
        self.table.setRowCount(len(self.visible_rows))
        for row, index in enumerate(self.visible_rows):
            item = self.items[index]
            self.table.setItem(row, 0, QTableWidgetItem("★" if item.get("favorite") else ""))
            self.table.setItem(row, 1, QTableWidgetItem(item.get("text", "")))
            source = QTableWidgetItem(Path(item.get("source", "")).name)
            source.setToolTip(item.get("source", ""))
            self.table.setItem(row, 2, source)
            self.table.setItem(row, 3, QTableWidgetItem(item.get("added", "")))
        self.count_label.setText(f"{len(self.visible_rows)} / {len(self.items)} 件")

    def selected_indexes(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.visible_rows[r] for r in rows if 0 <= r < len(self.visible_rows)]

    def toggle_favorite(self):
        indexes = self.selected_indexes()
        if not indexes:
            return
        make = not all(self.items[i].get("favorite") for i in indexes)
        library.set_favorite(self.category(), indexes, make)
        self.load_items()

    def delete_items(self):
        indexes = self.selected_indexes()
        if not indexes:
            return
        if len(indexes) > 1 and QMessageBox.question(
                self, "ライブラリ", f"{len(indexes)} 件を削除しますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        library.remove(self.category(), indexes)
        self.reload()

    def add_item(self):
        text = self.add_edit.text().strip()
        if text and self.category():
            library.add(self.category(), [(text, "(手で足した)")])
            self.add_edit.clear()
            self.reload()

    def new_category(self):
        name, ok = QInputDialog.getText(self, "カテゴリを足す", "カテゴリの名前 (英数字。要素名と同じにするとミックスでその要素に入ります)")
        name = name.strip()
        if ok and name:
            library.save(name, library.load(name))
            self.reload()

    def export(self):
        indexes = self.selected_indexes() or self.visible_rows
        if not indexes:
            return
        start = str(Path(self.current_folder().strip() or ".") / f"{self.category()}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "ワイルドカードで書き出す", start, "テキスト (*.txt)")
        if not path:
            return
        written = library.export_wildcard([self.items[i] for i in indexes], Path(path))
        self.count_label.setText(f"{len(indexes)} 件を書き出しました: {written}")

    # ------------------------------------------------------------ ミックス

    def refresh_targets(self):
        checked = {self.mix_targets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.mix_targets.count())
                   if self.mix_targets.item(i).checkState() == Qt.CheckState.Checked}
        self.mix_targets.clear()
        for name, t in self.targets.items():
            if t.is_video:
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
            self.mix_targets.addItem(item)

    def _build_mix_rows(self, names: list[str]):
        keep = {name: combo.currentText() for name, combo in self.mix_combos.items()}
        while self.mix_grid.count():
            widget = self.mix_grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.mix_combos = {}
        shown = [n for n in names if library.load(n)]
        for i, name in enumerate(shown):
            label = QLabel(f"{name} → {library.field_for(name)}")
            combo = QComboBox()
            combo.addItems([UNUSED, RANDOM, RANDOM_FAV] + [item["text"] for item in library.load(name)])
            combo.setMinimumContentsLength(18)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setCurrentText(keep.get(name, RANDOM))
            self.mix_grid.addWidget(label, i // 2, (i % 2) * 2)
            self.mix_grid.addWidget(combo, i // 2, (i % 2) * 2 + 1)
            self.mix_combos[name] = combo
        if not shown:
            self.mix_grid.addWidget(QLabel("ライブラリが空です (「抽出」で「パーツライブラリに登録」すると貯まります)"), 0, 0)

    def mix_plan(self) -> tuple[dict, dict]:
        choices, pools = {}, {}
        for name, combo in self.mix_combos.items():
            value = combo.currentText()
            if value == UNUSED:
                continue
            items = library.load(name)
            if value == RANDOM_FAV:
                items = [i for i in items if i.get("favorite")]
            pools[name] = [i["text"] for i in items]
            choices[name] = None if value in (RANDOM, RANDOM_FAV) else value
        return choices, pools

    def start_mix(self):
        targets = [self.targets[self.mix_targets.item(i).data(Qt.ItemDataRole.UserRole)]
                   for i in range(self.mix_targets.count())
                   if self.mix_targets.item(i).checkState() == Qt.CheckState.Checked]
        if not targets:
            QMessageBox.information(self, "ミックス生成", "対象モデルを 1 つ以上チェックしてください。")
            return
        choices, pools = self.mix_plan()
        empty = [n for n, v in choices.items() if v is None and not pools.get(n)]
        if empty:
            QMessageBox.information(self, "ミックス生成", f"選べる項目がありません: {', '.join(empty)} (★が付いていないなど)")
            return
        if not choices:
            QMessageBox.information(self, "ミックス生成", "使うカテゴリを 1 つ以上選んでください。")
            return
        datas = library.mix(choices, pools, self.mix_count.value())
        self.on_mix(datas, targets)
