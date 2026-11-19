# -*- mode: python ; coding: utf-8 -*-
# EXE の作り方: このフォルダで  py -3.11 -m PyInstaller --noconfirm MuseFactory.spec
# 出来た dist\MuseFactory.exe を Desktop\Application に写す。データは EXE の隣の MuseFactory フォルダに置く。


a = Analysis(
    ['main.py'],
    pathex=['..\\GenCommon'],
    binaries=[],
    datas=[
        ('app\\core\\tasks\\prompts', 'app\\core\\tasks\\prompts'),  # LLM に送る指示文
        ('profiles\\targets', 'defaults\\profiles\\targets'),  # 初めて起動したときに写す既定のプロファイル
        ('profiles\\llms', 'defaults\\profiles\\llms'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tests'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MuseFactory',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)
