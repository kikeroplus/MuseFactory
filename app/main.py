"""起動。2 つ目を起動したら、先に動いているほうを前に出して自分は終わる (PWC と同じ)。"""

import getpass
import sys

from PySide6.QtCore import Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .core import config
from .gen import profile as gen_profile
from .ui.main_window import MainWindow

SERVER_NAME = f"MuseFactory-{getpass.getuser()}"


def _notify_running_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _start_server(window: MainWindow) -> QLocalServer:
    server = QLocalServer(window)
    if not server.listen(SERVER_NAME):
        QLocalServer.removeServer(SERVER_NAME)
        server.listen(SERVER_NAME)

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        if window.isMinimized():
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(on_connection)
    return server


def main():
    app = QApplication(sys.argv)
    if _notify_running_instance():
        sys.exit(0)
    notes = []
    try:
        merged = gen_profile.setup()  # 生成の設定の置き場所を決める (GenCommon。PWC などと共有)
        if merged:
            notes.append("共有の設定: " + "。".join(merged))
    except Exception as exc:  # noqa: BLE001 - 生成が使えなくても LLM の機能は使えるように
        notes.append(f"生成の設定を読めません: {exc}")
    try:
        config.ensure_settings_file()  # 初回は既定の設定ファイルを書いておく
    except OSError as exc:
        notes.append(f"設定ファイルを書けません: {exc}")
    window = MainWindow(notes)
    _start_server(window)
    window.show()
    sys.exit(app.exec())
