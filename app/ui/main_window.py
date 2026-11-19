"""メイン画面 (PromptWordCrafter の構成を踏襲。SPEC 7.5)。

上: 出力フォルダの欄 → 機能ごとのグループ
本体: フォルダツリー | ファイル一覧 | 作業タブ (機能ごと。フェーズごとに中身を作る)
下: ステータスバー (左に直前の結果、右に「実行中: タスク / LLM プロファイル」・進み具合・キャンセル)
"""

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QDir, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFileSystemModel, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTabWidget, QTreeView, QVBoxLayout, QWidget)

from .. import paths
from ..core import config
from ..core.job_queue import JobQueue
from ..core.router import Router
from ..util import files
from . import style
from ..core import pwc_link
from .connection_dialog import ConnectionDialog
from .generate_tab import GenerateTab
from .jobs_bridge import JobsBridge

# 一覧に出すファイル (プロンプトと中間表現)
LIST_SUFFIXES = {".txt", ".json"}

# 作業タブ (名前, 作るフェーズ)
WORK_TABS = [
    ("抽出", 3), ("加工", 3), ("ストーリー", 4), ("ライブラリ", 5), ("LLM 比較", 5),
]


class MainWindow(QMainWindow):
    def __init__(self, notes: Optional[list[str]] = None):
        super().__init__()
        self.setWindowTitle("MuseFactory")
        self.ui_state = files.read_json(paths.UI_STATE_FILE, {}) or {}
        if not isinstance(self.ui_state, dict):
            self.ui_state = {}

        self.notes: list[str] = list(notes or [])
        self.settings, warn = config.load_settings()
        self.notes += warn
        self.llm_profiles, warn = config.load_llm_profiles()
        self.notes += warn
        self.target_profiles, warn = config.load_target_profiles()
        self.notes += warn

        self.router = Router(self.settings, self.llm_profiles)
        self.bridge = JobsBridge(self)
        self.queue = JobQueue(self.router, self.bridge)
        self.bridge.event.connect(self.on_job_event)
        self._connection_dialog: Optional[ConnectionDialog] = None
        self.current_path: Optional[Path] = None

        self._build_ui()
        self._connect_signals()
        self._restore_state()
        folder = self.ui_state.get("folder") or self.settings.get("default_output_dir") or ""
        if folder and Path(folder).is_dir():
            self.folder_edit.setText(folder)
            self.load_folder()
        if self.notes:
            self.statusBar().showMessage(" / ".join(self.notes))
        else:
            self.statusBar().showMessage(
                f"LLM プロファイル {len(self.llm_profiles)} 件、モデルプロファイル {len(self.target_profiles)} 件を読みました。")

    # ------------------------------------------------------------ 画面

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(6)

        # 出力フォルダ
        folder_bar = QHBoxLayout()
        folder_bar.addWidget(QLabel("フォルダ"))
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("プロンプトファイルを作る・読むフォルダ")
        folder_bar.addWidget(self.folder_edit, 1)
        self.browse_button = QPushButton("選択")
        folder_bar.addWidget(self.browse_button)
        self.refresh_button = QPushButton("更新")
        self.refresh_button.setToolTip("フォルダツリーとファイル一覧を読み直します (F5)")
        folder_bar.addWidget(self.refresh_button)
        root.addLayout(folder_bar)

        # 機能ごとのグループ
        tool_row = QHBoxLayout()

        llm_group = QGroupBox("LLM")
        llm_bar = QHBoxLayout(llm_group)
        self.connection_button = QPushButton("接続テスト")
        self.connection_button.setToolTip("LLM プロファイルごとの接続テストと、タスク割り当ての確認")
        llm_bar.addWidget(self.connection_button)
        self.open_config_button = QPushButton("設定フォルダを開く")
        self.open_config_button.setToolTip("config / profiles をエクスプローラーで開きます (直したら再起動)")
        llm_bar.addWidget(self.open_config_button)
        tool_row.addWidget(llm_group)

        manage_group = QGroupBox("管理")
        manage_bar = QHBoxLayout(manage_group)
        self.profiles_button = QPushButton("プロファイル管理")
        self.profiles_button.setEnabled(False)
        self.profiles_button.setToolTip("フェーズ5で作ります")
        manage_bar.addWidget(self.profiles_button)
        tool_row.addWidget(manage_group)

        pwc_group = QGroupBox("PromptWordCrafter")
        pwc_bar = QHBoxLayout(pwc_group)
        self.pwc_button = QPushButton("PWC で開く")
        self.pwc_button.setToolTip("このフォルダを PromptWordCrafter で開きます (一覧で選んでいるファイルを選んだ状態で)。"
                                   "そこから生成するか、PrismLab に送ります")
        pwc_bar.addWidget(self.pwc_button)
        tool_row.addWidget(pwc_group)
        tool_row.addStretch(1)
        root.addLayout(tool_row)

        # フォルダツリー | ファイル一覧 | 作業タブ
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.dir_model = QFileSystemModel(self)
        self.dir_model.setFilter(QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.dir_model.setRootPath("")
        self.folder_tree = QTreeView()
        self.folder_tree.setModel(self.dir_model)
        for col in range(1, self.dir_model.columnCount()):
            self.folder_tree.hideColumn(col)
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.header().setStretchLastSection(False)
        self.folder_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.folder_tree.header().resizeSection(0, 1200)
        self.folder_tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.splitter.addWidget(self.folder_tree)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.splitter.addWidget(self.file_list)

        self.tabs = QTabWidget()
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("ファイル一覧で選んだファイルの中身を表示します")
        self.tabs.addTab(self.preview, "プレビュー")
        self.generate_tab = GenerateTab(self.target_profiles, self.queue, self.bridge, self.settings,
                                        lambda: self.folder_edit.text(), self.on_files_saved, self.open_in_pwc,
                                        self.save_settings)
        self.tabs.addTab(self.generate_tab, "ざっくり生成")
        for name, phase in WORK_TABS:
            placeholder = QLabel(f"「{name}」はフェーズ{phase}で作ります。")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(f"color: {style.HINT};")
            self.tabs.addTab(placeholder, name)
        self.splitter.addWidget(self.tabs)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 4)
        root.addWidget(self.splitter, 1)

        # ステータスバー (右: 実行中・進み具合・キャンセル)
        self.running_label = QLabel("待機中")
        self.running_label.setStyleSheet(f"color: {style.HINT};")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(160)
        self.progress.setFormat("%v / %m")
        self.progress.hide()
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.setToolTip("待っている処理を取り消します (送ってしまった 1 件は返事を待って捨てます)")
        self.cancel_button.hide()
        self.statusBar().addPermanentWidget(self.running_label)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_button)

    def _connect_signals(self):
        self.browse_button.clicked.connect(self.choose_folder)
        self.refresh_button.clicked.connect(self.load_folder)
        self.folder_edit.returnPressed.connect(self.load_folder)
        QShortcut(QKeySequence("F5"), self, activated=self.load_folder)
        self.folder_tree.clicked.connect(self.on_tree_clicked)
        self.file_list.currentItemChanged.connect(self.on_file_changed)
        self.connection_button.clicked.connect(self.open_connection_dialog)
        self.open_config_button.clicked.connect(lambda: os.startfile(str(paths.BASE_DIR)))
        self.cancel_button.clicked.connect(self.cancel_jobs)
        self.pwc_button.clicked.connect(self.open_folder_in_pwc)

    def _restore_state(self):
        geometry = self.ui_state.get("geometry")
        restored = False
        if geometry:
            restored = self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        # 保存した位置が画面の外 (モニターの構成が変わった) なら既定の位置に出す
        screen = self.screen().availableVirtualGeometry() if self.screen() else None
        if not restored or (screen is not None and not screen.intersects(self.frameGeometry())):
            self.resize(1400, 860)
        sizes = self.ui_state.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 3:
            self.splitter.setSizes([int(s) for s in sizes])
        else:
            self.splitter.setSizes([240, 260, 900])

    def save_state(self):
        self.ui_state["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.ui_state["splitter"] = self.splitter.sizes()
        self.ui_state["folder"] = self.folder_edit.text().strip()
        try:
            files.write_json(paths.UI_STATE_FILE, self.ui_state)
        except OSError:
            pass

    def closeEvent(self, event):
        if self.queue.pending_count():
            answer = QMessageBox.question(
                self, "MuseFactory", "処理中のものがあります。取り消して閉じますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.queue.close()
        self.save_state()
        super().closeEvent(event)

    # ------------------------------------------------------------ フォルダとファイル

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "フォルダを選ぶ", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(os.path.normpath(folder))
            self.load_folder()

    def load_folder(self):
        folder = Path(self.folder_edit.text().strip())
        if not folder.is_dir():
            self.statusBar().showMessage(f"フォルダがありません: {folder}")
            return
        keep = self.current_path
        self.file_list.clear()
        try:
            entries = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in LIST_SUFFIXES),
                             key=lambda p: p.name.lower())
        except OSError as exc:
            self.statusBar().showMessage(f"フォルダを読めません: {exc}")
            return
        for path in entries:
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.file_list.addItem(item)
            if keep is not None and path == keep:
                self.file_list.setCurrentItem(item)
        index = self.dir_model.index(str(folder))
        if index.isValid():
            self.folder_tree.setCurrentIndex(index)
            self.folder_tree.scrollTo(index)
        self.statusBar().showMessage(f"{folder}  ({len(entries)} 件)")

    def on_tree_clicked(self, index):
        path = self.dir_model.filePath(index)
        if path:
            self.folder_edit.setText(os.path.normpath(path))
            self.load_folder()

    def on_file_changed(self, current, _previous):
        if current is None:
            self.current_path = None
            self.preview.clear()
            return
        self.current_path = Path(current.data(Qt.ItemDataRole.UserRole))
        try:
            self.preview.setPlainText(self.current_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as exc:
            self.preview.setPlainText(f"(読めません: {exc})")
        self.tabs.setCurrentWidget(self.preview)

    def on_files_saved(self, saved: list[Path]):
        """作業タブで保存したら、そのフォルダを表示していれば一覧を読み直す。"""
        if not saved:
            return
        folder = saved[0].parent
        current = Path(self.folder_edit.text().strip())
        if current.is_dir() and current.resolve() == folder.resolve():
            self.load_folder()
        self.statusBar().showMessage(f"{len(saved)} 件を保存しました: {folder}")

    def save_settings(self):
        try:
            config.save_settings(self.settings)
        except OSError as exc:
            self.statusBar().showMessage(f"設定を保存できません: {exc}")

    # ------------------------------------------------------------ PromptWordCrafter

    def open_folder_in_pwc(self):
        folder = Path(self.folder_edit.text().strip())
        if not folder.is_dir():
            self.statusBar().showMessage("フォルダ欄にフォルダを入れてください。")
            return
        selected = [Path(i.data(Qt.ItemDataRole.UserRole)) for i in self.file_list.selectedItems()]
        self.open_in_pwc(folder, [p for p in selected if p.suffix.lower() == ".txt"])

    def open_in_pwc(self, folder: Path, select: list[Path]):
        error = pwc_link.open_in_pwc(folder, select)
        if error:
            QMessageBox.warning(self, "PWC で開く", error)
        else:
            self.statusBar().showMessage(f"PromptWordCrafter で開きました: {folder}")

    # ------------------------------------------------------------ LLM とジョブ

    def open_connection_dialog(self):
        if self._connection_dialog is not None:
            self._connection_dialog.raise_()
            self._connection_dialog.activateWindow()
            return
        dialog = ConnectionDialog(self.llm_profiles, self.router, self.queue, self.bridge, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda: setattr(self, "_connection_dialog", None))
        self._connection_dialog = dialog
        dialog.show()

    def cancel_jobs(self):
        self.queue.cancel()
        self.running_label.setText("キャンセル中… (送った 1 件の返事を待っています)")

    def on_job_event(self, event: str, data: dict):
        job = data.get("job")
        batch = data.get("batch") or (job.batch if job is not None else None)
        if event == "job_started" and job is not None:
            self.running_label.setText(f"実行中: {job.task} / {job.profile_name}")
            self.running_label.setStyleSheet("")
        if event in ("batch_added", "job_started", "job_finished") and batch is not None:
            self.progress.setMaximum(batch.total)
            self.progress.setValue(batch.finished)
            self.progress.show()
            self.cancel_button.show()
        if event == "batch_finished" and batch is not None:
            failed = batch.failed
            summary = f"{batch.name}: {batch.total - len(failed)} / {batch.total} 件 終わりました"
            if failed:
                summary += f" (失敗 {len(failed)} 件)"
            self.statusBar().showMessage(summary)
            # 頼んだ画面が自分で失敗を出すもの (接続テストなど) は、ここでは出さない
            if failed and not batch.quiet:
                lines = [f"・{j.label} ({j.profile_name or '割り当てなし'}): {j.error}" for j in failed]
                QMessageBox.warning(self, "失敗した処理", "\n".join(lines[:30]))
        if event == "idle":
            self.running_label.setText("待機中")
            self.running_label.setStyleSheet(f"color: {style.HINT};")
            self.progress.hide()
            self.cancel_button.hide()
