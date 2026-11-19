"""フェーズ3.5: 生成の取り込み (アプリ側に置いた部分。ComfyUI とのやりとりは GenCommon)。"""

import tempfile
import unittest
from pathlib import Path

from app.gen import profile, results


class GenProfileTest(unittest.TestCase):
    def test_output_names_match_pwc(self):
        source = Path(r"C:\work\prompts\muse_Krea2_01.txt")
        self.assertEqual(profile._output_path(source, None, False, ".png"),
                         Path(r"C:\work\prompts\生成結果\muse_Krea2_01_t2i.png"))
        self.assertEqual(profile._output_path(source, None, True, ".mp4"),
                         Path(r"C:\work\prompts\生成結果\muse_Krea2_01_video.mp4"))
        self.assertEqual(profile.PROFILE.name, "MuseFactory")
        self.assertTrue(profile.PROFILE.skip_empty)

    def test_normalize_like_pwc(self):
        self.assertEqual(results._normalize("a\r\nb\rc\u2028d\u00a0e"), "a\nb\nc\nd e")

    def test_find_results_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "scene.txt"
            source.write_text("x", encoding="utf-8")
            (folder / "生成結果").mkdir()
            (folder / "生成結果" / "scene_t2i.png").write_bytes(b"")
            (folder / "生成結果" / "scene_video (2).mp4").write_bytes(b"")
            (folder / "生成結果" / "scene2_t2i.png").write_bytes(b"")  # 別のテキストのもの
            found = results.find_results(source, "x", records=[])
        self.assertEqual(sorted(r.path.name for r in found), ["scene_t2i.png", "scene_video (2).mp4"])


if __name__ == "__main__":
    unittest.main()
