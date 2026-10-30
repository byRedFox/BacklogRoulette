# -*- mode: python ; coding: utf-8 -*-
# Сборка: python -m PyInstaller --noconfirm MySteamRandomApp.spec  (или просто build.bat)

import glob
import os

import PySide6

# Иконка и музыка необязательны: без них сборка всё равно пройдёт
ICON = 'icon.ico' if os.path.exists('icon.ico') else None
DATAS = [(name, '.') for name in ('icon.ico', 'apptheme.mp3') if os.path.exists(name)]

# Проигрыватель FFmpeg для трейлеров: новые трейлеры Steam (HLS, до 1080p) открывает только он.
# Кладём его в сборку явно — без него Qt берёт проигрыватель Windows, и видео идёт в низком качестве.
QT_DIR = os.path.dirname(PySide6.__file__)
BINARIES = [(path, 'PySide6/plugins/multimedia')
            for path in glob.glob(os.path.join(QT_DIR, 'plugins', 'multimedia', '*.dll'))]
BINARIES += [(path, 'PySide6')
             for pattern in ('avcodec*.dll', 'avformat*.dll', 'avutil*.dll', 'swresample*.dll', 'swscale*.dll')
             for path in glob.glob(os.path.join(QT_DIR, pattern))]

# Движок перевода на ПК (CTranslate2 и SentencePiece): их DLL кладём в сборку явно. Нет библиотек — сборка
# всё равно пройдёт, просто переводить будет только Google
from PyInstaller.utils.hooks import collect_dynamic_libs

for package in ('ctranslate2', 'sentencepiece'):
    try:
        BINARIES += collect_dynamic_libs(package)
    except Exception:  # noqa: BLE001
        print(f'{package} не найден — перевод на ПК в сборку не войдёт')

a = Analysis(
    ['MySteamRandomApp.py'],
    pathex=[],
    binaries=BINARIES,
    datas=DATAS,  # вшиваются в .exe
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Лишние библиотеки, которые PyInstaller может зацепить и раздуть .exe. numpy не исключаем — он нужен
    # CTranslate2. А вот конвертеры CTranslate2 тянут за собой PyTorch, TensorFlow и Transformers (гигабайты),
    # хотя программе они не нужны: модель уже готовая
    excludes=['tkinter', 'pandas', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2',
              'torch', 'transformers', 'tensorflow', 'keras', 'jax', 'fairseq', 'onmt', 'opennmt', 'scipy'],
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
    # Автор, версия и права в свойствах .exe (правый клик → «Свойства» → «Подробно»)
    version='version_info.txt' if os.path.exists('version_info.txt') else None,
)
