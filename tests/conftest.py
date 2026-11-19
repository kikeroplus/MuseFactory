import sys
from pathlib import Path

# 生成の共通部品 (GenCommon) は隣のフォルダ (main.py と同じ)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "GenCommon"))
