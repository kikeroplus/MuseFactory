"""ジョブキューの知らせ (ワーカーのスレッドから来る) を、Qt の Signal で画面のスレッドに渡す。"""

from PySide6.QtCore import QObject, Signal


class JobsBridge(QObject):
    event = Signal(str, object)  # (event 名, data の dict)

    def __call__(self, event: str, data: dict) -> None:
        # 別のスレッドから emit すると、受け手 (画面) のスレッドで順に呼ばれる
        self.event.emit(event, data)
