"""MuseFactory の、共通部品 (gencommon) に渡すアプリごとの決まり (generation-standard 11 章)。

PWC と同じく、テキストファイル 1 つが 1 本のプロンプト。出来たファイルはテキストのフォルダの中の
「生成結果」フォルダに、動画なら「(元の名前)_video.mp4」、画像なら「(元の名前)_t2i.png」で保存する。
名前と置き場所を PWC とそろえるので、同じフォルダを PWC で開いても結果が帯に出る。
"""

from pathlib import Path

from gencommon import app, comfy

from .text_io import RESULTS_DIR

APP_NAME = "MuseFactory"
SUFFIXES = ("_t2i", "_video")  # 出来たファイルの名前に付けるもの (画像 / 動画)


def results_dir(source: Path) -> Path:
    """そのテキストから生成したファイルの置き場所。"""
    return source.parent / RESULTS_DIR


def _output_path(source: Path, request, is_video: bool, ext: str) -> Path:
    return results_dir(source) / (source.stem + ("_video" if is_video else "_t2i") + ext)


PROFILE = app.Profile(
    name=APP_NAME,
    reads=False,
    source_word="テキストファイル",
    unit="件",
    prompt_word="本文",
    prompt_where="プレビュー",
    column="ファイル",
    output_path=_output_path,
    workflow_hint=(
        "選んだテキストファイルの本文が、そのままワークフローのプロンプトに入ります "
        f"(文字欄に「{comfy.PROMPT_MARKER}」があればそこ。なければ prompt 入力、CLIPTextEncode の順。"
        "複数あるときはタイトルに Positive とあるもの)。"
        "プロンプトを入れる場所のないワークフローは登録できません。seed は毎回ランダムになります。"
        f"出来たファイルはテキストファイルのフォルダの中の「{RESULTS_DIR}」フォルダに、動画なら「(元の名前)_video.mp4」、"
        "画像なら「(元の名前)_t2i.png」で保存します (PromptWordCrafter と同じ)。"
    ),
    open_on_done=True,
    skip_empty=True,
)


def setup() -> list[str]:
    """起動したときに呼ぶ。戻り値は、共有の設定に移したときの知らせ (MuseFactory は自分の古い設定を持たない)。"""
    return app.setup(PROFILE)
