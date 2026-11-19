"""「プロファイル管理」の画面 (SPEC 5.6)。

モデルプロファイル / LLM プロファイルの一覧と、JSON をそのまま直す欄。新規・複製・削除 (ごみ箱へ)・保存。
モデルプロファイルは「ガイドから下書き...」で、公式のガイドや作例を貼り付けて LLM に下書きを作らせられる (保存は確かめてから)。
"""

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ..core import profiles_io
from ..core.job_queue import CANCELLED, DONE, Job
from ..core.llm_client import LLMClient, LLMError
from ..gen import results
from ..util import files
from . import style


class _Pane(QWidget):
    def __init__(self, kind: str, owner: "ProfilesDialog"):
        super().__init__()
        self.kind = kind
        self.owner = owner
        self.path: Optional[Path] = None  # 直している (None なら新規・未保存)
        self._loading = False
        self.dirty = False
        layout = QHBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        lv.addWidget(self.list)
        row = QHBoxLayout()
        self.new_button = QPushButton("新規")
        self.copy_button = QPushButton("複製")
        self.delete_button = QPushButton("削除")
        self.delete_button.setToolTip("ファイルをごみ箱に送ります")
        for b in (self.new_button, self.copy_button, self.delete_button):
            row.addWidget(b)
        lv.addLayout(row)
        if kind == profiles_io.TARGET:
            self.draft_button = QPushButton("ガイドから下書き...")
            self.draft_button.setToolTip("公式のプロンプトガイドや作例を貼り付けると、LLM がこのモデルのプロファイルの下書きを作ります")
            lv.addWidget(self.draft_button)
            self.draft_button.clicked.connect(owner.open_draft)
        else:
            self.available_button = QPushButton("使えるモデルから追加...")
            self.available_button.setToolTip("LLM サーバー (LM Studio など) に入っているモデルの一覧から選んで、プロファイルを作ります")
            lv.addWidget(self.available_button)
            self.available_button.clicked.connect(self.add_available)
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel("")
        self.file_label.setStyleSheet(f"color: {style.HINT};")
        rv.addWidget(self.file_label)
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        rv.addWidget(self.editor, 1)
        srow = QHBoxLayout()
        self.message = QLabel("")
        self.message.setWordWrap(True)
        srow.addWidget(self.message, 1)
        self.save_button = QPushButton("保存")
        srow.addWidget(self.save_button)
        rv.addLayout(srow)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        layout.addWidget(split)

        self.list.currentItemChanged.connect(self.on_select)
        self.editor.textChanged.connect(self._on_edit)
        self.new_button.clicked.connect(self.new)
        self.copy_button.clicked.connect(self.copy)
        self.delete_button.clicked.connect(self.delete)
        self.save_button.clicked.connect(self.save)
        self.reload()

    def reload(self, select: Optional[Path] = None):
        self.list.blockSignals(True)
        self.list.clear()
        for name, path in profiles_io.names_in(self.kind).items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.list.addItem(item)
        self.list.blockSignals(False)
        rows = [Path(self.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.list.count())]
        if select is not None and select in rows:
            self.list.setCurrentRow(rows.index(select))
        elif rows:
            self.list.setCurrentRow(0)
        self.on_select(self.list.currentItem(), None)

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        return QMessageBox.question(self, "プロファイル管理", "保存していない変更があります。捨てますか?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def on_select(self, current, _previous):
        if current is None:
            self.show_data(None, {})
            return
        path = Path(current.data(Qt.ItemDataRole.UserRole))
        self.show_data(path, files.read_json(path, {}))

    def show_data(self, path: Optional[Path], data: dict, note: str = ""):
        self._loading = True
        self.path = path
        self.editor.setPlainText(json.dumps(data, ensure_ascii=False, indent=2) if data else "")
        self._loading = False
        self.dirty = path is None and bool(data)
        self.file_label.setText(str(path) if path else "(新規・まだ保存していません)")
        self.message.setText(note)
        self.message.setStyleSheet(f"color: {style.HINT};")

    def _on_edit(self):
        if not self._loading:
            self.dirty = True

    def new(self):
        if not self.confirm_discard():
            return
        template = dict(profiles_io.TARGET_TEMPLATE if self.kind == profiles_io.TARGET else profiles_io.LLM_TEMPLATE)
        template["name"] = profiles_io.unique_name(self.kind, template["name"])
        self.list.clearSelection()
        self.show_data(None, template, "名前と中身を直して「保存」してください。")

    def copy(self):
        if self.path is None or not self.confirm_discard():
            return
        try:
            path = profiles_io.duplicate(self.kind, self.path)
        except ValueError as exc:
            QMessageBox.warning(self, "プロファイル管理", str(exc))
            return
        self.reload(path)
        self.owner.changed()

    def delete(self):
        if self.path is None:
            return
        if QMessageBox.question(self, "プロファイル管理", f"{self.list.currentItem().text()} を削除しますか? (ごみ箱へ)",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        problem = results.send_to_recycle_bin([self.path])
        if problem:
            QMessageBox.warning(self, "プロファイル管理", problem)
            return
        self.dirty = False
        self.reload()
        self.owner.changed()

    def add_available(self):
        if not self.confirm_discard():
            return
        dialog = AvailableModelsDialog(self.endpoint_hint(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.add_models(dialog.chosen(), dialog.endpoint())

    def add_models(self, chosen: list[dict], endpoint: str) -> list[Path]:
        made = []
        try:
            for info in chosen:
                made.append(profiles_io.save(self.kind, profiles_io.profile_from_model(info, endpoint)))
        except OSError as exc:
            QMessageBox.warning(self, "プロファイル管理", f"保存できません: {exc}")
        if made:
            self.dirty = False
            self.reload(made[-1])
            self.message.setText(f"{len(made)} 件の LLM プロファイルを作りました。中身 (画像を見られるか・文脈長など) を確かめてください。")
            self.message.setStyleSheet(f"color: {style.OK};")
            self.owner.changed()
        return made

    def endpoint_hint(self) -> str:
        """選んでいるプロファイル (なければ最初のもの) の endpoint。"""
        candidates = [self.path] if self.path else []
        candidates += list(profiles_io.names_in(self.kind).values())
        for path in candidates:
            data = files.read_json(path, None)
            if isinstance(data, dict) and data.get("endpoint"):
                return str(data["endpoint"])
        return profiles_io.LLM_TEMPLATE["endpoint"]

    def save(self) -> bool:
        try:
            data = profiles_io.validate(self.kind, self.editor.toPlainText())
        except ValueError as exc:
            self.message.setText(f"保存できません: {exc}")
            self.message.setStyleSheet(f"color: {style.ERROR};")
            return False
        taken = profiles_io.names_in(self.kind)
        if data["name"] in taken and (self.path is None or taken[data["name"]] != self.path):
            self.message.setText(f"保存できません: 「{data['name']}」という名前はもうあります")
            self.message.setStyleSheet(f"color: {style.ERROR};")
            return False
        try:
            path = profiles_io.save(self.kind, data, self.path)
        except OSError as exc:
            self.message.setText(f"保存できません: {exc}")
            self.message.setStyleSheet(f"color: {style.ERROR};")
            return False
        self.dirty = False
        self.reload(path)
        self.message.setText("保存しました")
        self.message.setStyleSheet(f"color: {style.OK};")
        self.owner.changed()
        return True


class _FetchSignals(QObject):
    done = Signal(int, object, str)  # (何回目の取り直しか, 一覧, エラー文)


class AvailableModelsDialog(QDialog):
    """LLM サーバーに入っているモデルの一覧。チェックしたものをプロファイルにする。"""

    KINDS = {"vlm": "画像も見られる", "llm": "文章だけ"}

    def __init__(self, endpoint: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使えるモデルから追加")
        self.resize(900, 520)
        self.models: list[dict] = []
        self._fetch_no = 0
        self._signals = _FetchSignals()
        self._signals.done.connect(self.on_fetched)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("LLM サーバー"))
        self.endpoint_edit = QLineEdit(endpoint)
        row.addWidget(self.endpoint_edit, 1)
        self.fetch_button = QPushButton("一覧を取る")
        row.addWidget(self.fetch_button)
        layout.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["モデル ID", "種類", "最大文脈長", "状態", "登録済み"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("チェックしたものを追加")
        self.ok_button.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("やめる")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fetch_button.clicked.connect(self.fetch)
        self.endpoint_edit.returnPressed.connect(self.fetch)
        self.table.itemChanged.connect(lambda *_: self._update_ok())
        self.fetch()

    def endpoint(self) -> str:
        return self.endpoint_edit.text().strip().rstrip("/")

    def fetch(self):
        endpoint = self.endpoint()
        if not endpoint:
            return
        self._fetch_no += 1
        no, signals = self._fetch_no, self._signals
        self.fetch_button.setEnabled(False)
        self.table.setRowCount(0)
        self.models = []
        self._update_ok()
        self._set_status("一覧を取っています…", style.HINT)

        def work():
            try:
                signals.done.emit(no, profiles_io.available_models(endpoint), "")
            except LLMError as exc:
                signals.done.emit(no, [], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, text: str, color: str):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def on_fetched(self, no: int, models: list, error: str):
        if no != self._fetch_no:
            return  # 古い取り直しの結果
        self.fetch_button.setEnabled(True)
        if error:
            self._set_status(f"一覧を取れません: {error}", style.ERROR)
            return
        self.models = models
        known = profiles_io.registered_models()
        endpoint = self.endpoint()
        self.table.blockSignals(True)
        self.table.setRowCount(len(models))
        for row, info in enumerate(models):
            have = known.get((endpoint, info["id"]), "")
            cell = QTableWidgetItem(info["id"])
            cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            cell.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, cell)
            self.table.setItem(row, 1, QTableWidgetItem(self.KINDS.get(info["type"], info["type"] or "?")))
            self.table.setItem(row, 2, QTableWidgetItem(f"{info['max_context']:,}" if info["max_context"] else "?"))
            self.table.setItem(row, 3, QTableWidgetItem("読み込み中" if info["loaded"] else ""))
            mark = QTableWidgetItem(have)
            if have:
                mark.setForeground(QColor(style.HINT))
            self.table.setItem(row, 4, mark)
        self.table.blockSignals(False)
        self._set_status(f"{len(models)} 個のモデルがあります (埋め込み用は外しています)。追加したいものにチェックを付けてください。"
                         if models else "モデルが 1 つもありません", style.HINT)
        self._update_ok()

    def chosen(self) -> list[dict]:
        out = []
        for row, info in enumerate(self.models):
            cell = self.table.item(row, 0)
            if cell is not None and cell.checkState() == Qt.CheckState.Checked:
                out.append(info)
        return out

    def _update_ok(self):
        self.ok_button.setEnabled(bool(self.chosen()))


class DraftDialog(QDialog):
    """ガイドの文を貼り付ける画面。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ガイドから下書き")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("モデルの名前 (空なら LLM が決めます)"))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("公式のプロンプトガイドや作例を貼り付けてください (長すぎると切れるので、要点だけで十分です)"))
        self.guide = QPlainTextEdit()
        layout.addWidget(self.guide, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("下書きを作る")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("やめる")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ProfilesDialog(QDialog):
    def __init__(self, queue, bridge, on_changed: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle("プロファイル管理")
        self.resize(1100, 720)
        self.queue = queue
        self.on_changed = on_changed
        self._draft_job = None
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.target_pane = _Pane(profiles_io.TARGET, self)
        self.llm_pane = _Pane(profiles_io.LLM, self)
        self.tabs.addTab(self.target_pane, "モデルプロファイル (画像・動画のモデル)")
        self.tabs.addTab(self.llm_pane, "LLM プロファイル")
        layout.addWidget(self.tabs)
        hint = QLabel("保存すると、ほかの画面のモデルの一覧にもすぐ反映します。タスク割り当ては「接続テスト」の画面の「割り当てを変える...」で直します。")
        hint.setStyleSheet(f"color: {style.HINT};")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("閉じる")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        bridge.event.connect(self.on_job_event)

    def changed(self):
        self.on_changed()

    def reject(self):
        if self.target_pane.confirm_discard() and self.llm_pane.confirm_discard():
            super().reject()

    def open_draft(self):
        if self._draft_job is not None or not self.target_pane.confirm_discard():
            return
        dialog = DraftDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.guide.toPlainText().strip():
            return
        guide, name = dialog.guide.toPlainText(), dialog.name_edit.text()

        def run(profile, token):
            token.check()
            return profiles_io.draft_target(LLMClient(profile), guide, name)

        self._draft_job = Job("profile_draft", "プロファイルの下書き", run)
        self.queue.submit([self._draft_job], name="プロファイルの下書き", quiet=True)
        self.target_pane.draft_button.setEnabled(False)
        self.target_pane.message.setText("下書きを作っています…")
        self.target_pane.message.setStyleSheet(f"color: {style.HINT};")

    def on_job_event(self, event, data):
        job = data.get("job")
        if event != "job_finished" or job is None or job is not self._draft_job:
            return
        self._draft_job = None
        self.target_pane.draft_button.setEnabled(True)
        if job.status == DONE:
            draft = job.result
            draft["name"] = profiles_io.unique_name(profiles_io.TARGET, draft.get("name") or "新しいモデル")
            self.tabs.setCurrentWidget(self.target_pane)
            self.target_pane.list.clearSelection()
            self.target_pane.show_data(None, draft, "LLM の下書きです。中身を確かめて、直してから「保存」してください。")
        elif job.status == CANCELLED:
            self.target_pane.message.setText("やめました")
        else:
            self.target_pane.message.setText(f"下書きを作れません: {job.error}")
            self.target_pane.message.setStyleSheet(f"color: {style.ERROR};")
