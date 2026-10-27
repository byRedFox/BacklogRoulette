# -*- mode: python ; coding: utf-8 -*-
# Сборка: python -m PyInstaller --noconfirm MySteamRandomApp.spec  (или просто build.bat)

import os

# Иконка необязательна: без 1.ico сборка пройдёт со стандартной иконкой
ICON = '1.ico' if os.path.exists('1.ico') else None

a = Analysis(
    ['MySteamRandomApp.py'],
    pathex=[],
    binaries=[],
    datas=[(ICON, '.')] if ICON else [],  # иконка окна внутри .exe
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'numpy', 'matplotlib'],
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
    name='MySteamRandomApp',
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
    icon=[ICON] if ICON else None,
)
