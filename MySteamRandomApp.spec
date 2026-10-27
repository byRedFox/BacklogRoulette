# -*- mode: python ; coding: utf-8 -*-
# Сборка: python -m PyInstaller --noconfirm MySteamRandomApp.spec  (или просто build.bat)

import os

# Иконка и музыка необязательны: без них сборка всё равно пройдёт
ICON = 'icon.ico' if os.path.exists('icon.ico') else None
DATAS = [(name, '.') for name in ('icon.ico', 'apptheme.mp3') if os.path.exists(name)]

a = Analysis(
    ['MySteamRandomApp.py'],
    pathex=[],
    binaries=[],
    datas=DATAS,  # вшиваются в .exe
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Лишние библиотеки, которые PyInstaller может зацепить и раздуть .exe
    excludes=['tkinter', 'pandas', 'numpy', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2'],
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
    name='BacklogRoulette',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX ломает DLL Qt
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON] if ICON else None,
)
