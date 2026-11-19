"""メイン画面 (PromptWordCrafter の構成を踏襲。SPEC 7.5)。

上: 出力フォルダの欄 → 機能ごとのグループ
本体: フォルダツリー | ファイル一覧 | 作業タブ (機能ごと。フェーズごとに中身を作る)
下: ステータスバー (左に直前の結果、右に「実行中: タスク / LLM プロファイル」・進み具合・キャンセル)

画像・動画の生成 (ComfyUI) は PWC と同じ共通部品 (GenCommon) で行う (SPEC 5.10)。
テキストファイル 1 つが 1 本のプロンプト。結果はプレビューの下の帯に出す。
"""

import os
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QDir, QEvent, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QFileSystemModel, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTabWidget, QTreeView, QVBoxLayout, QWidget)
from gencommon.generation import GenerationController

from .. import paths
from ..core import config
from ..core.job_queue import JobQueue
from ..core.router import Router
from ..util import files
from . import style
from ..core import pwc_link
from .connection_dialog import ConnectionDialog
from .extract_tab import ExtractTab
from .generate_tab import GenerateTab
from .process_tab import ProcessTab
from .jobs_bridge import JobsBridge
from ..gen import prismlab_link, results, text_io
from ..gen.results_view import ResultsStrip

# 一覧に出すファイル (プロンプトと中間表現)
LIST_SUFFIXES = {".txt", ".json"}

# 作業タブ (名前, 作るフェーズ)
WORK_TABS = [
    ("ストーリー", 4), ("ライブラリ", 5), ("LLM 比較", 5),
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
        self.generation = GenerationController(self, self.prompt_text_for, self.statusBar().showMessage)

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
        # 前回終わらなかった生成ジョブは、ウィンドウが出てから聞く
        QTimer.singleShot(0, self.generation.offer_resume)

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

        # 生成 (ComfyUI) グループ: 生成する | 生成ジョブ | 生成の設定 (generation-standard 9 章の並び)
        generate_group = QGroupBox("生成 (ComfyUI)")
        generate_bar = QHBoxLayout(generate_group)
        self.comfy_button = QPushButton("生成する")
        self.comfy_button.setToolTip("一覧で選んでいるテキストファイル (なければ表示中のもの) の本文で、ComfyUI で生成します")
        generate_bar.addWidget(self.comfy_button)
        self.jobs_button = QPushButton("生成ジョブ")
        self.jobs_button.setToolTip("生成ジョブの一覧 (Ctrl+J)")
        generate_bar.addWidget(self.jobs_button)
        self.comfy_settings_button = QPushButton("生成の設定")
        self.comfy_settings_button.setToolTip("接続先・ワークフロー・エンハンサーなど (PromptWordCrafter・J-Rate・ImageMimicArt と共有)")
        generate_bar.addWidget(self.comfy_settings_button)
        self.diagram_button = QPushButton("図で設定")
        self.diagram_button.setToolTip("設定・処理・送る前の確認を図のように並べた画面で、次に生成するときの設定を決めます (生成はしません)")
        generate_bar.addWidget(self.diagram_button)
        tool_row.addWidget(generate_group)

        manage_group = QGroupBox("管理")
        manage_bar = QHBoxLayout(manage_group)
        self.profiles_button = QPushButton("プロファイル管理")
        self.profiles_button.setEnabled(False)
        self.profiles_button.setToolTip("フェーズ5で作ります")
        manage_bar.addWidget(self.profiles_button)
        tool_row.addWidget(manage_group)

        pwc_group = QGroupBox("連携")
        pwc_bar = QHBoxLayout(pwc_group)
        self.pwc_button = QPushButton("PWC で開く")
        self.pwc_button.setToolTip("このフォルダを PromptWordCrafter で開きます (一覧で選んでいるファイルを選んだ状態で)")
        pwc_bar.addWidget(self.pwc_button)
        self.prismlab_button = QPushButton("PrismLab に送る")
        self.prismlab_button.setToolTip("一覧で選んでいるテキストを、PrismLab の量産の入口か物語に送ります")
        self.prismlab_menu = QMenu(self.prismlab_button)
        self.prismlab_menu.aboutToShow.connect(self._fill_prismlab_button_menu)
        self.prismlab_button.setMenu(self.prismlab_menu)
        pwc_bar.addWidget(self.prismlab_button)
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

        self.folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.splitter.addWidget(self.file_list)

        self.tabs = QTabWidget()
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("ファイル一覧で選んだファイルの中身を表示します")
        # プレビューと、その下に「生成結果」(このテキストから生成した画像・動画。PWC と同じ帯)
        self.preview_splitter = QSplitter(Qt.Orientation.Vertical)
        self.preview_splitter.addWidget(self.preview)
        self.results_strip = ResultsStrip()
        self.preview_splitter.addWidget(self.results_strip)
        self.preview_splitter.setStretchFactor(0, 1)
        self.preview_splitter.setStretchFactor(1, 0)
        self.preview_splitter.setCollapsible(0, False)
        self.tabs.addTab(self.preview_splitter, "プレビュー")
        self.generate_tab = GenerateTab(self.target_profiles, self.queue, self.bridge, self.settings,
                                        lambda: self.folder_edit.text(), self.on_files_saved, self.open_in_pwc,
                                        self.save_settings, generate_files=self.generate_files)
        self.tabs.addTab(self.generate_tab, "ざっくり生成")
        folder_text = lambda: self.folder_edit.text()  # noqa: E731
        self.extract_tab = ExtractTab(self.queue, self.bridge, self.settings, self.selected_files, folder_text,
                                      self.on_files_saved)
        self.tabs.addTab(self.extract_tab, "抽出")
        self.process_tab = ProcessTab(self.target_profiles, self.queue, self.bridge, self.router, self.settings,
                                      self.selected_files, folder_text, self.on_files_saved, self.open_in_pwc,
                                      generate_files=self.generate_files)
        self.tabs.addTab(self.process_tab, "加工")
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
        self.comfy_button.clicked.connect(self.generate_selected)
        self.jobs_button.clicked.connect(self.generation.show_jobs)
        self.comfy_settings_button.clicked.connect(self.generation.show_settings)
        self.diagram_button.clicked.connect(self.diagram_settings)
        self.generation.jobs_changed.connect(self.update_jobs_button)
        self.generation.job_done.connect(self.on_generation_done)
        self.results_strip.status.connect(self.statusBar().showMessage)
        self.results_strip.info_requested.connect(self.generation.show_record)
        QShortcut(QKeySequence("Ctrl+J"), self, activated=self.generation.show_jobs)
        self.file_list.customContextMenuRequested.connect(self.show_file_menu)
        self.folder_tree.customContextMenuRequested.connect(self.show_folder_menu)

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
        if not self.generation.confirm_close():
            event.ignore()
            return
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
        self.current_path = None
        self.preview.clear()
        self.results_strip.clear()
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
            self.results_strip.clear()
            return
        self.current_path = Path(current.data(Qt.ItemDataRole.UserRole))
        try:
            self.preview.setPlainText(files.read_text(self.current_path))
        except OSError as exc:
            self.preview.setPlainText(f"(読めません: {exc})")
        if self.current_path.suffix.lower() == ".txt":
            self.results_strip.show_for(self.current_path, self.prompt_text_for(self.current_path))
        else:
            self.results_strip.clear()
        self.tabs.setCurrentWidget(self.preview_splitter)

    def selected_files(self) -> list[Path]:
        return [Path(i.data(Qt.ItemDataRole.UserRole)) for i in self.file_list.selectedItems()]

    def on_files_saved(self, saved: list[Path]):
        """作業タブで保存したら、そのフォルダを表示していれば一覧を読み直す。"""
        if not saved:
            return
        current = Path(self.folder_edit.text().strip())
        if current.is_dir() and any(p.parent.resolve() == current.resolve() for p in saved):
            self.load_folder()
        self.statusBar().showMessage(f"{len(saved)} 件を保存しました: {saved[0].parent}")

    def save_settings(self):
        try:
            config.save_settings(self.settings)
        except OSError as exc:
            self.statusBar().showMessage(f"設定を保存できません: {exc}")

    # ------------------------------------------------------------ 生成 (ComfyUI)

    def prompt_text_for(self, path: Path) -> str:
        """ワークフローにプロンプトとして渡す本文 (PWC と同じ表記にそろえる: NBSP→空白、改行→LF)。"""
        try:
            return results._normalize(files.read_text(path))
        except OSError:
            return ""

    def generation_sources(self) -> list[Path]:
        """一覧で選んでいる .txt (なければ表示中のもの)。"""
        sources = [p for p in self.selected_files() if p.suffix.lower() == ".txt"]
        if not sources and self.current_path is not None and self.current_path.suffix.lower() == ".txt":
            sources = [self.current_path]
        return sources

    def list_txt_files(self) -> list[Path]:
        paths = [Path(self.file_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.file_list.count())]
        return [p for p in paths if p.suffix.lower() == ".txt"]

    def generate_selected(self):
        sources = self.generation_sources()
        if not sources:
            self.statusBar().showMessage("生成するテキストファイルを一覧で選んでください。")
            return
        self.generation.generate(sources, what="選択中のファイル")

    def generate_files(self, paths: list[Path], what: str = "保存したファイル"):
        """作業タブの「保存して生成する」から。"""
        sources = [p for p in paths if p.suffix.lower() == ".txt" and p.is_file()]
        if sources:
            self.generation.generate(sources, what=what)

    def diagram_settings(self):
        self.generation.show_diagram_settings(self.generation_sources())

    def generate_folder(self, folder: Path, recursive: bool):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)  # サブフォルダが多いと数えるのに時間がかかる
        try:
            sources = [p for p in text_io.list_text_files(folder, recursive) if p.suffix.lower() == ".txt"]
        finally:
            QApplication.restoreOverrideCursor()
        if not sources:
            where = "このフォルダとサブフォルダ" if recursive else "このフォルダ"
            self.statusBar().showMessage(f"{where}にテキストファイルがありません: {folder}")
            return
        what = f"フォルダ「{folder.name or folder}」" + ("とサブフォルダ" if recursive else "")
        self.generation.generate(sources, what=what)

    def update_jobs_button(self):
        active = self.generation.active_count
        paused = "・一時停止中" if self.generation.paused else ""
        self.jobs_button.setText(f"生成ジョブ ({active}{paused})" if active else "生成ジョブ")

    def on_generation_done(self, job):
        """表示中のテキストから生成したものなら、生成結果の帯に加える。"""
        if self.current_path is not None and results.same_path(job.source, self.current_path):
            self.results_strip.refresh(self.prompt_text_for(self.current_path))

    def changeEvent(self, event):
        # PrismLab などから戻ってきたら、生成結果の帯を読み直す (PWC と同じ)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                away = getattr(self, "_left_at", None)
                if away is not None and time.monotonic() - away >= 1.0 and self.current_path is not None:
                    self.results_strip.refresh(self.prompt_text_for(self.current_path))
                self._left_at = None
            elif QApplication.activeModalWidget() is None:
                self._left_at = time.monotonic()
        super().changeEvent(event)

    def show_file_menu(self, pos):
        """ファイル一覧の右クリックメニュー。"""
        item = self.file_list.itemAt(pos)
        clicked = Path(item.data(Qt.ItemDataRole.UserRole)) if item is not None else self.current_path
        selected = self.generation_sources()
        all_txt = self.list_txt_files()
        menu = QMenu(self)
        gen = QMenu("生成 (ComfyUI)", menu)  # 親を付けて作る (関数を抜けても消えないように)
        menu.addMenu(gen)
        generate = gen.addAction(f"選択中のファイルを生成する ({len(selected)} 件)..." if len(selected) > 1
                                 else "生成する (ComfyUI)...")
        generate.setEnabled(bool(selected))
        generate.triggered.connect(self.generate_selected)
        generate_all = gen.addAction(f"一覧のファイルをまとめて生成する ({len(all_txt)} 件)...")
        generate_all.setEnabled(bool(all_txt))
        generate_all.triggered.connect(lambda: self.generation.generate(all_txt, what="一覧のファイル"))
        info = gen.addAction("生成時の設定...")
        info.setEnabled(clicked is not None and self.generation.has_history(clicked))
        info.triggered.connect(lambda: self.generation.show_gen_info(clicked))
        gen.addSeparator()
        jobs = gen.addAction("生成ジョブの一覧...\tCtrl+J")
        jobs.triggered.connect(self.generation.show_jobs)
        settings = gen.addAction("生成の設定...")
        settings.triggered.connect(self.generation.show_settings)
        self._add_prismlab_menu(menu, selected)
        menu.addSeparator()
        pwc = menu.addAction("PWC で開く")
        pwc.triggered.connect(self.open_folder_in_pwc)
        menu.exec(self.file_list.viewport().mapToGlobal(pos))

    def show_folder_menu(self, pos):
        """フォルダツリーの右クリックメニュー。右クリックしただけではフォルダを開かない。"""
        index = self.folder_tree.indexAt(pos)
        if not index.isValid():
            return
        folder = Path(self.dir_model.filePath(index))
        if not folder.is_dir():
            return
        count = len([p for p in text_io.list_text_files(folder) if p.suffix.lower() == ".txt"])
        menu = QMenu(self)
        gen = QMenu("生成 (ComfyUI)", menu)
        menu.addMenu(gen)
        generate = gen.addAction(f"このフォルダのテキストをまとめて生成する ({count} 件)...")
        generate.setEnabled(count > 0)
        generate.triggered.connect(lambda: self.generate_folder(folder, recursive=False))
        generate_all = gen.addAction("サブフォルダも含めてまとめて生成する...")
        generate_all.triggered.connect(lambda: self.generate_folder(folder, recursive=True))
        gen.addSeparator()
        jobs = gen.addAction("生成ジョブの一覧...\tCtrl+J")
        jobs.triggered.connect(self.generation.show_jobs)
        settings = gen.addAction("生成の設定...")
        settings.triggered.connect(self.generation.show_settings)
        self._add_prismlab_folder_menu(menu, folder, count)
        menu.addSeparator()
        pwc = menu.addAction("PWC で開く")
        pwc.triggered.connect(lambda: self.open_in_pwc(folder, []))
        explorer = menu.addAction("エクスプローラーで開く")
        explorer.triggered.connect(lambda: os.startfile(str(folder)))
        menu.exec(self.folder_tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ PrismLab に送る (PWC と同じ)

    def _fill_prismlab_button_menu(self):
        self.prismlab_menu.clear()
        self._fill_prismlab_menu(self.prismlab_menu, self.generation_sources())

    def _add_prismlab_menu(self, menu: QMenu, selected: list[Path]) -> None:
        sub = QMenu("PrismLab に送る", menu)
        menu.addMenu(sub)
        self._fill_prismlab_menu(sub, selected)

    def _fill_prismlab_menu(self, sub: QMenu, selected: list[Path]) -> None:
        sub.setToolTipsVisible(True)
        if not selected:
            none = sub.addAction("(一覧でテキストファイルを選んでください)")
            none.setEnabled(False)
            return
        names = prismlab_link.recipe_names()
        unit = f" ({len(selected)} 件)" if len(selected) > 1 else ""
        if names:
            head = sub.addAction(f"量産の入口に{unit}:")
            head.setEnabled(False)
            for index, name in enumerate(names):
                action = sub.addAction("    " + (name or "(名前なし)"))
                action.setToolTip("選んだテキストを、このレシピの入口 (選択) にします。始めるのは PrismLab で。")
                action.triggered.connect(
                    lambda _c=False, n=name, i=index, f=list(selected): self.send_to_prismlab_recipe(f, n, i))
        else:
            none = sub.addAction("(PrismLab のレシピがありません)")
            none.setEnabled(False)
        sub.addSeparator()
        story = sub.addAction("物語: 選んでいるシーンの元のプロンプトに")
        story.setToolTip("PrismLab で開いている物語の、選んでいるシーンの元のプロンプトをこの本文にします (1 件のとき)。")
        story.setEnabled(len(selected) == 1)
        story.triggered.connect(lambda: self.send_to_prismlab_story(selected[0]))

    def _add_prismlab_folder_menu(self, menu: QMenu, folder: Path, count: int) -> None:
        sub = QMenu("PrismLab に送る", menu)
        menu.addMenu(sub)
        sub.setToolTipsVisible(True)
        names = prismlab_link.recipe_names()
        if not names:
            none = sub.addAction("(PrismLab のレシピがありません)")
            none.setEnabled(False)
            return
        scopes = [(False, f"このフォルダのテキスト ({count} 件) を量産の入口に:"), (True, "サブフォルダも含めて量産の入口に:")]
        for number, (recursive, title) in enumerate(scopes):
            if number:
                sub.addSeparator()
            head = sub.addAction(title)
            head.setEnabled(False)
            for index, name in enumerate(names):
                action = sub.addAction("    " + (name or "(名前なし)"))
                action.setEnabled(recursive or count > 0)
                action.triggered.connect(
                    lambda _c=False, n=name, i=index, r=recursive: self.send_folder_to_prismlab_recipe(folder, r, n, i))

    def send_folder_to_prismlab_recipe(self, folder: Path, recursive: bool, name: str, index: int) -> None:
        sources = [p for p in text_io.list_text_files(folder, recursive) if p.suffix.lower() == ".txt"]
        if not sources:
            self.statusBar().showMessage(f"テキストファイルがありません: {folder}")
            return
        self.send_to_prismlab_recipe(sources, name, index)

    def send_to_prismlab_recipe(self, sources: list[Path], name: str, index: int) -> None:
        problem = prismlab_link.send_to_recipe(sources, name, index)
        if problem:
            QMessageBox.warning(self, "PrismLab に送る", problem)
        else:
            self.statusBar().showMessage(f"{len(sources)} 件を PrismLab のレシピ「{name}」の入口に送りました。")

    def send_to_prismlab_story(self, source: Path) -> None:
        problem = prismlab_link.send_to_story_original(source)
        if problem:
            QMessageBox.warning(self, "PrismLab に送る", problem)
        else:
            self.statusBar().showMessage(f"{source.name} を PrismLab の物語 (選んでいるシーンの元のプロンプト) に送りました。")

    # ------------------------------------------------------------ PromptWordCrafter

    def open_folder_in_pwc(self):
        folder = Path(self.folder_edit.text().strip())
        if not folder.is_dir():
            self.statusBar().showMessage("フォルダ欄にフォルダを入れてください。")
            return
        self.open_in_pwc(folder, [p for p in self.selected_files() if p.suffix.lower() == ".txt"])

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
