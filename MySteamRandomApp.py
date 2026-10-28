"""Backlog Roulette — рандомайзер «Во что сыграть дальше?» для библиотеки Steam.

Интерфейс в стиле Windows 11 (PySide6 + PySide6-Fluent-Widgets), логика — в core.py.
"""

import faulthandler
import json
import math
import os
import random
import re
import sys
import threading
import time
from urllib.parse import quote_plus

LAUNCHED = time.monotonic()  # для замера запуска: отсюда начинается загрузка Qt и библиотек

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation,
                            QRect, QRectF, QSize, QSizeF, Qt, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QImage, QLinearGradient, QMovie,
                           QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient)
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer, QSoundEffect
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QFrame, QGraphicsOpacityEffect,
                               QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QLayout,
                               QListWidgetItem, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)
from qfluentwidgets import (Action, BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon as FIF, InfoBar,
                            IndeterminateProgressBar, InfoBarPosition, LineEdit, ListWidget, MessageBox,
                            MessageBoxBase,
                            MSFluentWindow, NavigationItemPosition, PasswordLineEdit, PlainTextEdit,
                            PrimaryPushButton, PrimaryToolButton, ProgressBar, PushButton, RoundMenu, ScrollArea,
                            SearchLineEdit, Slider, StrongBodyLabel,
                            SubtitleLabel, SwitchButton, TextEdit, Theme, TitleLabel, TransparentPushButton,
                            TransparentToggleToolButton, TransparentToolButton,
                            FluentIconBase, isDarkTheme, setTheme, themeColor)

# Кнопки бокового меню Fluent шириной 64 точки, а значок в них нарисован с отступом под эту ширину.
# «О программе» туда не помещается, поэтому меню делаем шире, а значок сдвигаем к середине кнопки.
try:
    from qfluentwidgets.components.navigation.navigation_bar import NavigationBarPushButton

    _draw_nav_icon = NavigationBarPushButton._drawIcon

    def _draw_centered_nav_icon(self, painter):
        painter.save()
        painter.translate((self.width() - 64) / 2, 0)
        _draw_nav_icon(self, painter)
        painter.restore()

    NavigationBarPushButton._drawIcon = _draw_centered_nav_icon
except (ImportError, AttributeError):
    pass

# Встроенный браузер. Движок нужно загрузить до создания QApplication. Нет его — ссылки
# открываются во внешнем браузере, как раньше.
try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None

import core

__version__ = "2.2"
APP_NAME = "Backlog Roulette"
APP_ID = "byRedFox.BacklogRoulette"  # для Windows: своя иконка и группа на панели задач
AUTHOR = "Sergey Tsepa"
AUTHOR_NICK = "byRedFox"
COPYRIGHT = f"© 2026 {AUTHOR}"
REPO_URL = "https://github.com/byRedFox/BacklogRoulette"
LICENSE_NAME = "GNU GPL v3"  # как у PySide6-Fluent-Widgets, на котором построен интерфейс

THEMES = {"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}
THEME_KEYS = ["auto", "light", "dark"]
THEME_TITLES = ["Как в системе", "Светлая", "Тёмная"]

# Сколько новых игр без обложек должно появиться (например, после импорта),
# чтобы снова показать экран загрузки вместо барабана
LOADING_THRESHOLD = 5


# --- Вспомогательное --------------------------------------------------------
class Worker(QObject):
    """Выполняет функцию в фоновом потоке, чтобы окно не зависало на сетевых запросах.

    Сигналы доходят до слотов виджетов в главном потоке через очередь Qt.
    """

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)  # родитель держит объект живым, пока идёт работа
        self.fn, self.args = fn, args

    def start(self):
        self.finished.connect(self.deleteLater)
        self.failed.connect(self.deleteLater)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = self.fn(*self.args)
        except Exception as e:  # noqa: BLE001 — показываем любую ошибку пользователю
            core.log.exception("Ошибка в фоновой задаче %s", getattr(self.fn, "__name__", self.fn))
            self.failed.emit(core.mask_secrets(f"{type(e).__name__}: {e}"))
        else:
            self.finished.emit(result)


class Timing:
    """Замер для лога: сколько секунд прошло от начала операции до каждого её этапа.

    В лог уходит одна строка: «Замер: запуск — 2.41 с (модули 1.10, окно собрано 2.05, …)».
    По этим строкам видно, где программа на самом деле ждёт, — их и стоит ускорять.
    """

    def __init__(self, name, start=None):
        self.name = name
        self.start = time.monotonic() if start is None else start
        self.marks = []
        self.finished = False

    def mark(self, label):
        self.marks.append(f"{label} {time.monotonic() - self.start:.2f}")

    def done(self, label=None):
        if self.finished:
            return
        if label:
            self.mark(label)
        self.finished = True
        core.log.info("Замер: %s — %.2f с (%s)", self.name, time.monotonic() - self.start, ", ".join(self.marks))


def notify(kind, window, title, text="", duration=3500):
    """Всплывающее уведомление: kind = success | info | warning | error."""
    getattr(InfoBar, kind)(title, text, orient=Qt.Horizontal, isClosable=True,
                           duration=duration, position=InfoBarPosition.TOP, parent=window)


def confirm(window, title, text, yes="Да"):
    box = MessageBox(title, text, window)
    box.yesButton.setText(yes)
    box.cancelButton.setText("Отмена")
    core.log.info("Открыто окно «%s»", title)  # если программа застрянет на окне — это будет видно в логе
    result = bool(box.exec())
    core.log.info("Окно «%s» закрыто: %s", title, "да" if result else "отмена")
    return result


HANG_SECONDS = 15


def install_hang_watchdog(app):
    """Если окно перестанет отвечать дольше HANG_SECONDS, в logs/hang.log запишется, где застряла программа.

    Таймер в главном потоке раз в секунду откладывает запись. Застрял главный поток — таймер не сработал,
    и faulthandler из своего потока сохраняет стеки всех потоков.
    """
    try:
        os.makedirs(core.LOGS_DIR, exist_ok=True)
        hang_file = open(core.HANG_LOG_FILE, "a", encoding="utf-8")  # открыт до выхода из программы
    except OSError:
        return

    def rearm():
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(HANG_SECONDS, repeat=False, file=hang_file)

    timer = QTimer(app)
    timer.timeout.connect(rearm)
    timer.start(1000)
    rearm()
    app.hang_watchdog = (timer, hang_file)
    app.aboutToQuit.connect(faulthandler.cancel_dump_traceback_later)


class CoverPrefetcher(QObject):
    """Скачивает недостающие обложки в несколько потоков и сообщает о прогрессе.

    Игры, для которых обложку не нашли, запоминаются в covers_not_found.json,
    чтобы не искать их заново при каждом запуске.
    """

    coverReady = Signal(str)  # название игры, для которой появилась обложка
    progress = Signal(int, int)  # сколько обработано, сколько всего в текущей загрузке
    finished = Signal()

    WORKERS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending = []
        self._busy = set()  # названия в очереди или в работе
        self._failed = core.load_not_found()
        self._active = 0
        self._done = self._total = 0

    @property
    def running(self):
        with self._lock:
            return self._active > 0

    def enqueue(self, games, front=False):
        """Ставит в очередь игры без обложек. Возвращает, сколько добавлено."""
        with self._lock:
            new = []
            for game in games:
                name = game["name"]
                if name in self._busy or name in self._failed or core.find_cover(name):
                    continue
                self._busy.add(name)
                new.append(dict(game))  # копия: список могут менять в главном потоке
            if not new:
                return 0
            if self._active == 0:
                self._done = self._total = 0
            self._pending = new + self._pending if front else self._pending + new
            self._total += len(new)
            to_start = self.WORKERS - self._active
            self._active = self.WORKERS
            done, total = self._done, self._total
        for _ in range(to_start):
            threading.Thread(target=self._run, daemon=True).start()
        self.progress.emit(done, total)
        return len(new)

    def clear(self):
        """Очищает очередь и список ненайденных (при очистке библиотеки)."""
        with self._lock:
            for game in self._pending:
                self._busy.discard(game["name"])
            self._pending.clear()
            self._failed.clear()

    def retry(self, games):
        """Повторная попытка для ненайденных — после смены ключа SteamGridDB."""
        with self._lock:
            self._failed.clear()
            core.save_not_found(set())
        return self.enqueue(games)

    def _run(self):
        key = core.read_text(core.API_KEY_FILE)
        while True:
            with self._lock:
                if not self._pending:
                    self._active -= 1
                    last = self._active == 0
                    break
                game = self._pending.pop(0)
            name = game["name"]
            try:
                path = core.find_cover(name) or core.download_cover(game, key)[0]
            except Exception:  # noqa: BLE001 — одна неудачная обложка не должна останавливать загрузку
                core.log.exception("Ошибка при скачивании обложки «%s»", name)
                path = None
            with self._lock:
                self._busy.discard(name)
                self._done += 1
                if not path:
                    self._failed.add(name)
                    try:
                        core.save_not_found(self._failed)
                    except OSError:
                        pass
                done, total = self._done, self._total
            if path:
                self.coverReady.emit(name)
            self.progress.emit(done, total)
        if last:
            self.finished.emit()


# --- Музыка -----------------------------------------------------------------
class MusicPlayer(QObject):
    """Музыка программы с плавным появлением, затуханием и кроссфейдом между треками.

    Источник — встроенный трек, свой файл, своя папка или скачанный саундтрек Steam (это тоже папка).
    Треки папки идут по порядку, по кругу; можно повторять один трек. Один файл всегда играет по кругу.
    «Вперемешку» играет треки из многих альбомов сразу: из всей музыки, что знает программа
    (кроме встроенной темы), или только из скачанных саундтреков. Список треков для этого даёт вкладка
    «Саундтреки» — плеер получает его готовым.
    """

    trackChanged = Signal()
    stateChanged = Signal()  # музыку включили, выключили, поставили на паузу, продолжили или сменили «вперемешку»
    positionChanged = Signal(int)
    durationChanged = Signal(int)

    CROSSFADE_MS = 4000  # сколько секунд конца трека звучит вместе с началом следующего
    FADE_IN_MS = 1500  # появление музыки при запуске и ручном переключении
    PAUSE_FADE_MS = 400
    RESTART_MS = 3000  # «Предыдущий» после трёх секунд трека — к началу этого трека, раньше — к прошлому
    BUILTIN_TITLE = "Soliloquy — Matthew Pablo"
    SHUFFLE_MODES = ("off", "all", "soundtracks")

    def __init__(self, custom_path="", shuffle_mode="off", repeat_one=False, parent=None):
        super().__init__(parent)
        self.default_path = core.find_resource(core.MUSIC_NAME)
        self.path = None
        self.source = ""  # папка или файл, который играет; пусто — встроенный трек или «вперемешку»
        self.is_custom = False
        self.mixed = False  # играет «вперемешку» из нескольких альбомов
        # Треки по порядку, их названия и альбомы ({"name", "image", "folder"}) — у каждого трека свой
        self.tracks, self.titles, self.track_albums = [], [], []
        self.order, self.index = [], 0  # порядок воспроизведения (номера треков) и место в нём
        self.shuffle_mode = shuffle_mode if shuffle_mode in self.SHUFFLE_MODES else "off"
        self.repeat_one = repeat_one
        self._errors_in_row = 0
        self.enabled = False
        self.paused = False  # пауза кнопкой плеера; в отличие от выключения в настройки не пишется
        self.ducked = False  # музыка притихла на время трейлера
        self.volume = 0.4
        self._crossfading = False  # следующий трек уже запущен поверх доигрывающего
        self._loaded_at = 0.0  # когда запущен текущий трек
        self._stopped_checks = 0
        self._stalled_checks = 0
        self._last_position = -1

        # Два проигрывателя, как деки у диджея: пока один доигрывает, второй уже начинает следующий трек
        self.decks = [self._make_deck(), self._make_deck()]
        self.current = 0

        # Звуковые устройства Windows поменялись (Steam при выходе убирает свои виртуальные колонки
        # Steam Streaming, отключили наушники и т. п.). Сам вывод звука Qt на новое устройство
        # не переходит и молчит, поэтому пересоздаём его на устройстве по умолчанию.
        self.devices = QMediaDevices(self)
        self.devices.audioOutputsChanged.connect(self._on_outputs_changed)
        self._output_id = QMediaDevices.defaultAudioOutput().id()

        # Страховка: если музыка остановилась, а сигнала о конце трека не было — включаем следующий;
        # если трек «играет», но время в нём не идёт — звук завис, пересоздаём вывод звука
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self._check_stopped)
        self.watchdog.start(3000)
        self.set_source(custom_path)

    def _make_deck(self):
        player = QMediaPlayer(self)
        deck = {"player": player}
        self._attach_audio(deck)
        player.mediaStatusChanged.connect(lambda status: self._on_status(deck, status))
        player.errorOccurred.connect(lambda error, message="": self._on_error(deck, error, message))
        player.positionChanged.connect(lambda position: self._on_position(deck, position))
        player.durationChanged.connect(lambda duration: deck is self.deck and self.durationChanged.emit(duration))
        return deck

    def _attach_audio(self, deck):
        """Новый вывод звука на устройстве Windows по умолчанию — громкость и затухание переносятся."""
        old = deck.get("audio")
        volume = old.volume() if old else 0.0
        if deck.get("fade"):
            deck["fade"].stop()
            deck["fade"].deleteLater()
        audio = QAudioOutput(QMediaDevices.defaultAudioOutput(), self)
        audio.setVolume(volume)
        deck["player"].setAudioOutput(audio)
        fade = QPropertyAnimation(audio, b"volume", self)
        fade.finished.connect(lambda: self._on_fade_finished(deck))
        deck["audio"], deck["fade"] = audio, fade
        if old:
            old.deleteLater()

    def _renew_output(self, reason):
        core.log.info("Музыка: %s — переключаю звук на «%s»", reason, QMediaDevices.defaultAudioOutput().description())
        self._output_id = QMediaDevices.defaultAudioOutput().id()
        for deck in self.decks:
            player = deck["player"]
            state, position = player.playbackState(), player.position()
            self._attach_audio(deck)
            if deck is self.deck and state == QMediaPlayer.PlayingState:
                # Плеер мог встать вместе со звуком: перезапускаем с того же места
                player.pause()
                player.setPosition(position)
                player.play()
                if not self.ducked:
                    self._fade(deck, self.volume, self.PAUSE_FADE_MS)
            elif deck is not self.deck:
                player.stop()  # хвост кроссфейда не нужен
        self._errors_in_row = 0
        self._stalled_checks = 0

    def _on_outputs_changed(self):
        # Даже если устройство по умолчанию то же, после смены списка вывод звука Qt мог отвалиться
        changed = QMediaDevices.defaultAudioOutput().id() != self._output_id
        self._renew_output("сменилось звуковое устройство Windows" if changed
                           else "поменялся список звуковых устройств")

    @property
    def deck(self):
        return self.decks[self.current]

    @property
    def player(self):
        return self.deck["player"]

    @property
    def available(self):
        return self.path is not None

    @property
    def playing(self):
        return self.enabled and not self.paused and self.available

    @property
    def current_track(self):
        """Номер играющего трека в self.tracks (None — играть нечего)."""
        return self.order[self.index] if self.order else None

    @property
    def title(self):
        track = self.current_track
        return self.titles[track] if track is not None else ""

    @property
    def album(self):
        """Альбом играющего трека: {"name", "image", "folder"}."""
        track = self.current_track
        return self.track_albums[track] if track is not None else {}

    @property
    def folder(self):
        """Папка (или файл) альбома, из которого играет трек; пусто — встроенная тема."""
        return self.album.get("folder", "") if self.is_custom else ""

    @property
    def shuffled(self):
        return self.shuffle_mode != "off"

    def position(self):
        return self.player.position()

    def duration(self):
        return self.player.duration()

    @staticmethod
    def _source_tracks(path):
        if path and os.path.isdir(path):
            tracks = core.folder_tracks(path)
            if not tracks:
                core.log.warning("В папке с музыкой нет аудиофайлов: %s", path)
            return tracks
        return [path] if path and os.path.isfile(path) else []

    def set_source(self, path, start=None, album=None):
        """Папка, свой файл или (если пусто или файла уже нет) встроенный apptheme.mp3 — по порядку.

        start — с какого трека начать; album — {"name", "image"} для плеера.
        """
        tracks = self._source_tracks(path)
        self.is_custom = bool(tracks)
        self.mixed = False
        self.source = path if tracks else ""
        if tracks:
            titles = core.track_titles(tracks)
            album = dict(album or self._default_album(path), folder=path)
        else:
            tracks = [self.default_path] if self.default_path else []
            titles = [self.BUILTIN_TITLE] * len(tracks)
            album = {"name": "Встроенная тема", "image": core.find_resource(core.ICON_NAME), "folder": ""}
        self._load(tracks, titles, [album] * len(tracks), start)
        self._start(self.tracks[self.current_track] if self.tracks else None, crossfade=False)

    def play_mix(self, entries, start=None):
        """«Вперемешку» по многим альбомам. entries — [{"path", "title", "album"}, ...]; start — первый трек
        (по умолчанию случайный)."""
        self.is_custom, self.mixed, self.source = True, True, ""
        self._load([e["path"] for e in entries], [e["title"] for e in entries], [e["album"] for e in entries],
                   start)
        self._start(self.tracks[self.current_track], crossfade=False)

    def play(self, path, start=None, album=None, entries=None):
        """Включить альбом (с трека start). Если включено «вперемешку» и этот альбом в нём есть —
        начинаем с него, а дальше играет вся подборка вперемешку."""
        if self.shuffled and entries:
            tracks = [e["path"] for e in entries]
            if start not in tracks:
                own = [p for p in self._source_tracks(path) if p in tracks]
                start = random.choice(own) if own else None
            if start in tracks:
                self.play_mix(entries, start)
                return
        self.set_source(path, start, album)

    def set_shuffle_mode(self, mode, entries=None):
        """Сменить «вперемешку». Играющий трек не прерывается, если он есть в новой подборке."""
        self.shuffle_mode = mode
        current = self.path if self.is_custom else None
        if mode == "off":
            if self.mixed:  # возвращаемся к альбому играющего трека, по порядку
                album = self.album
                tracks = self._source_tracks(album.get("folder"))
                if current in tracks:
                    self.mixed, self.source = False, album["folder"]
                    self._load(tracks, core.track_titles(tracks), [album] * len(tracks), current)
                    self.trackChanged.emit()
            else:
                self._make_order(self.current_track)
        elif entries:
            tracks = [e["path"] for e in entries]
            if current in tracks:
                self.is_custom, self.mixed, self.source = True, True, ""
                self._load(tracks, [e["title"] for e in entries], [e["album"] for e in entries], current)
                self.trackChanged.emit()
            else:
                self.play_mix(entries)  # играла встроенная тема или альбом не из подборки
        else:
            self._make_order(self.current_track)  # подборка пуста — перемешиваем то, что играет
        self.player.setLoops(self._loops())
        self.stateChanged.emit()

    def update_mix(self, entries):
        """Список музыки поменялся (нашлись новые саундтреки) — обновляем подборку, не прерывая трек."""
        if self.mixed and entries:
            self.set_shuffle_mode(self.shuffle_mode, entries)

    def _load(self, tracks, titles, albums, start=None):
        self.tracks, self.titles, self.track_albums = list(tracks), list(titles), list(albums)
        self._errors_in_row = 0
        self._make_order(self.tracks.index(start) if start in self.tracks else None)

    def _default_album(self, path):
        if os.path.isdir(path):
            return {"name": os.path.basename(os.path.normpath(path)), "image": None}
        return {"name": "Свой трек", "image": None}

    def set_album(self, path, album):
        """Название и картинка альбома стали известны позже — саундтреки ищутся в фоне."""
        changed = False
        for i, old in enumerate(self.track_albums):
            if core.same_path(old.get("folder"), path) and (old.get("name"), old.get("image")) != (
                    album.get("name"), album.get("image")):
                self.track_albums[i] = dict(album, folder=old["folder"])
                changed = True
        if changed:
            self.trackChanged.emit()

    def _make_order(self, first=None):
        """Порядок треков: по номерам или вперемешку. first — какой трек встанет текущим."""
        self.order = list(range(len(self.tracks)))
        if self.shuffled:
            random.shuffle(self.order)
            if first is not None:
                self.order.remove(first)
                self.order.insert(0, first)
            self.index = 0  # без first подборка начинается со случайного трека
        else:
            self.index = first or 0

    def _loops(self):
        return -1 if len(self.tracks) < 2 or self.repeat_one else 1  # -1 = QMediaPlayer.Loops.Infinite

    def next_track(self, crossfade=False):
        if len(self.tracks) < 2:
            return
        self.index += 1
        if self.index >= len(self.order):  # круг пройден
            if self.shuffled:  # перемешиваем заново, но не начинаем с только что сыгравшего трека
                last = self.order[-1]
                random.shuffle(self.order)
                if self.order[0] == last:
                    self.order[0], self.order[-1] = self.order[-1], self.order[0]
            self.index = 0
        self._start(self.tracks[self.current_track], crossfade)

    def prev_track(self):
        if not self.tracks:
            return
        if len(self.tracks) < 2 or self.player.position() > self.RESTART_MS:
            self.seek(0)
            return
        self.index = (self.index - 1) % len(self.order)
        self._start(self.tracks[self.current_track], crossfade=False)

    def set_repeat_one(self, repeat_one):
        self.repeat_one = repeat_one
        self.player.setLoops(self._loops())
        self.stateChanged.emit()  # кнопки повтора во всех плеерах

    def seek(self, position):
        self.player.setPosition(max(0, int(position)))

    def set_paused(self, paused):
        if paused == self.paused:
            return
        self.paused = paused
        if self.enabled and self.available and not self.ducked:
            if paused:
                self._fade(self.deck, 0.0, self.PAUSE_FADE_MS)  # по окончании затухания встанет на паузу
            else:
                self.player.play()
                self._fade(self.deck, self.volume, self.PAUSE_FADE_MS)
        self.stateChanged.emit()

    def _start(self, path, crossfade):
        """Запускает трек на свободном проигрывателе. При кроссфейде старый плавно затихает параллельно."""
        old = self.deck
        if crossfade and self.enabled:
            self._fade(old, 0.0, self.CROSSFADE_MS)  # по окончании затухания он остановится сам
        else:
            old["fade"].stop()
            old["player"].stop()
        other = self.decks[1 - self.current]
        if other is not old:  # второй проигрыватель мог ещё доигрывать прошлый кроссфейд
            other["fade"].stop()
            other["player"].stop()

        self.current = 1 - self.current
        new = self.deck
        self.path = path
        self.paused = False  # выбранный трек играет, даже если до этого стояла пауза
        self._crossfading = False
        self._loaded_at = time.monotonic()
        self._stopped_checks = 0
        # Один трек (или «повтор трека») играет по кругу, папка — по очереди
        new["player"].setLoops(self._loops())
        new["player"].setSource(QUrl.fromLocalFile(path) if path else QUrl())
        if path:
            core.log.info("Музыка%s: %s", " (кроссфейд)" if crossfade else "", path)
        if self.enabled and path:
            new["audio"].setVolume(0.0)
            new["player"].play()
            self._fade(new, self.volume, self.CROSSFADE_MS if crossfade else self.FADE_IN_MS)
        self.trackChanged.emit()
        self.stateChanged.emit()

    def _on_position(self, deck, position):
        if deck is not self.deck:
            return
        self.positionChanged.emit(position)
        # За 4 секунды до конца запускаем следующий трек поверх текущего
        if (len(self.tracks) < 2 or self.repeat_one or not self.playing or self._crossfading or self.ducked):
            return
        duration = deck["player"].duration()
        if duration > self.CROSSFADE_MS * 3 and duration - position < self.CROSSFADE_MS:
            self._crossfading = True
            self._errors_in_row = 0
            self.next_track(crossfade=True)

    def _on_status(self, deck, status):
        if deck is self.deck and status == QMediaPlayer.BufferedMedia:
            self._errors_in_row = 0  # трек открылся — значит, звук и файл в порядке
        # Короткий трек (или неточная длина mp3) закончился раньше кроссфейда — просто включаем следующий
        if deck is self.deck and status == QMediaPlayer.EndOfMedia and len(self.tracks) > 1:
            self._errors_in_row = 0
            self.next_track()

    def _check_stopped(self):
        if not self.playing or self.ducked or time.monotonic() - self._loaded_at < 10:
            self._stopped_checks = self._stalled_checks = 0
            self._last_position = -1
            return
        state = self.player.playbackState()
        # Трек «играет», а время стоит три проверки подряд (9 секунд) — звук завис
        position = self.player.position()
        stalled = state == QMediaPlayer.PlayingState and position == self._last_position
        self._last_position = position
        self._stalled_checks = self._stalled_checks + 1 if stalled else 0
        if self._stalled_checks >= 3:
            self._renew_output("трек играет, но время в нём стоит")
            return
        # Срабатывает, только если текущий трек стоит две проверки подряд и прошло больше 10 секунд с его старта
        if len(self.tracks) < 2:
            self._stopped_checks = 0
            return
        stopped = state == QMediaPlayer.StoppedState
        self._stopped_checks = self._stopped_checks + 1 if stopped else 0
        if self._stopped_checks >= 2:
            core.log.info("Музыка остановилась без сигнала о конце трека — включаю следующий")
            self.next_track()

    def _on_error(self, deck, error, message=""):
        if deck is not self.deck:
            return
        core.log.warning("Не удалось воспроизвести %s: %s", self.path, message or error)
        if error == QMediaPlayer.ResourceError and os.path.exists(self.path or "") and not self._errors_in_row:
            # Файл на месте, а открыть не вышло — обычно это отвалился вывод звука, а не битый трек.
            # Пересоздаём вывод и пробуем этот же трек ещё раз (один раз, чтобы не крутиться по кругу).
            self._renew_output("ошибка вывода звука")
            self._errors_in_row = 1
            path = self.path
            QTimer.singleShot(0, lambda: self._start(path, crossfade=False))
            return
        # Битый файл в папке пропускаем, но не крутимся бесконечно, если битые все
        self._errors_in_row += 1
        if len(self.tracks) > 1 and self._errors_in_row < len(self.tracks):
            QTimer.singleShot(0, self.next_track)

    def set_volume(self, percent):
        self.volume = percent / 100
        if not self.playing or self.ducked:
            return
        fade = self.deck["fade"]
        if fade.state() == QAbstractAnimation.Running:
            fade.setEndValue(self.volume)
        else:
            self.deck["audio"].setVolume(self.volume)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.paused = False
        if self.available:
            # Доигрывающий хвост прошлого трека при выключении/включении сразу останавливаем
            other = self.decks[1 - self.current]
            other["fade"].stop()
            other["player"].stop()
            if enabled:
                if self.player.playbackState() != QMediaPlayer.PlayingState:
                    self.deck["audio"].setVolume(0.0)
                    self.player.play()
                self._fade(self.deck, self.volume, self.FADE_IN_MS)
            else:
                self._fade(self.deck, 0.0, 600)
        self.stateChanged.emit()

    def set_ducked(self, ducked):
        """На время трейлера музыка плавно затихает и встаёт на паузу, потом так же плавно возвращается."""
        if ducked == self.ducked:
            return
        self.ducked = ducked
        if not self.playing:
            return
        if ducked:
            self._fade(self.deck, 0.0, 600)
        else:
            if self.player.playbackState() != QMediaPlayer.PlayingState:
                self.player.play()
            self._fade(self.deck, self.volume, self.FADE_IN_MS)

    def _fade(self, deck, value, duration):
        fade = deck["fade"]
        fade.stop()
        fade.setDuration(duration)
        fade.setStartValue(deck["audio"].volume())
        fade.setEndValue(float(value))
        fade.start()

    def _on_fade_finished(self, deck):
        if deck is not self.deck:
            deck["player"].stop()  # прошлый трек затих после кроссфейда
        elif not self.playing or self.ducked:
            deck["player"].pause()


class ReelSounds(QObject):
    """Щелчки барабана и звук остановки. Звуки синтезируются в core.generate_sounds()."""

    TICK_POOL = 6  # щелчки идут чаще, чем длится один звук, поэтому играем по кругу из нескольких
    VOLUME = 0.5

    def __init__(self, enabled=True, parent=None):
        super().__init__(parent)
        self.enabled = enabled
        self._ticks, self._win, self._next = [], None, 0
        try:
            paths = core.generate_sounds()
        except OSError:
            return  # без звуков программа работает как обычно
        for _ in range(self.TICK_POOL):
            self._ticks.append(self._effect(paths["tick"], self.VOLUME * 0.6))
        self._win = self._effect(paths["win"], self.VOLUME)

    def _effect(self, path, volume):
        effect = QSoundEffect(self)
        effect.setSource(QUrl.fromLocalFile(path))
        effect.setVolume(volume)
        return effect

    def tick(self):
        if self.enabled and self._ticks:
            self._ticks[self._next].play()
            self._next = (self._next + 1) % len(self._ticks)

    def win(self):
        if self.enabled and self._win:
            self._win.play()


def fluent_icon(name, fallback):
    """Значок Fluent по имени: в старых версиях PySide6-Fluent-Widgets части значков нет."""
    return getattr(FIF, name, fallback)


class MediaIcon(FluentIconBase):
    """Значки плеера, которых нет среди Fluent: предыдущий и следующий трек, «вперемешку».

    Рисуются как обычные значки Fluent: чёрные в светлой теме, белые в тёмной и на нажатой кнопке.
    SVG-файлы пишутся в cache/icons при первом показе.
    """

    SHAPES = {
        "prev": '<rect x="3" y="3.5" width="1.6" height="9" rx="0.8"/>'
                '<path d="M12.4 3.9v8.2c0 .6-.6.9-1.1.6L5.6 8.6a.7.7 0 0 1 0-1.2l5.7-4.1c.5-.3 1.1 0 1.1.6z"/>',
        "next": '<rect x="11.4" y="3.5" width="1.6" height="9" rx="0.8"/>'
                '<path d="M3.6 3.9v8.2c0 .6.6.9 1.1.6l5.7-4.1a.7.7 0 0 0 0-1.2L4.7 3.3c-.5-.3-1.1 0-1.1.6z"/>',
        "shuffle": '<g fill="none" stroke="{color}" stroke-width="1.3" stroke-linecap="round" '
                   'stroke-linejoin="round">'
                   '<path d="M1.8 4.5h2.1c1.3 0 2.1.6 2.8 1.7l2.6 3.6c.7 1.1 1.5 1.7 2.8 1.7h1.9"/>'
                   '<path d="M1.8 11.5h2.1c1.3 0 2.1-.6 2.8-1.7"/><path d="M9.3 6.2c.7-1.1 1.5-1.7 2.8-1.7h1.9"/>'
                   '<path d="M12.4 2.8 14.1 4.5l-1.7 1.7"/><path d="M12.4 9.8 14.1 11.5l-1.7 1.7"/></g>',
        "repeat": '<g fill="none" stroke="{color}" stroke-width="1.3" stroke-linecap="round" '
                  'stroke-linejoin="round">'
                  '<path d="M2.6 8V6.6a2 2 0 0 1 2-2h8.6"/><path d="M11.4 2.8 13.2 4.6l-1.8 1.8"/>'
                  '<path d="M13.4 8v1.4a2 2 0 0 1-2 2H2.8"/><path d="M4.6 13.2 2.8 11.4l1.8-1.8"/></g>',
    }
    # «Только саундтреки» — те же стрелки и диск пластинки снизу
    SHAPES["shuffle_ost"] = SHAPES["shuffle"] + '<circle cx="8" cy="14.2" r="1.5"/>'
    # «Повтор трека» — та же петля с единицей внутри
    SHAPES["repeat_one"] = SHAPES["repeat"] + ('<path d="M7.2 7.1 8.3 6.4v3.4" fill="none" stroke="{color}" '
                                               'stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>')

    def __init__(self, name):
        self.name = name

    def path(self, theme=Theme.AUTO):
        dark = isDarkTheme() if theme == Theme.AUTO else theme == Theme.DARK
        color = "#ffffff" if dark else "#000000"
        path = os.path.join(core.CACHE_DIR, "icons", f"{self.name}_{'white' if dark else 'black'}.svg")
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" '
                        f'fill="{color}">{self.SHAPES[self.name].format(color=color)}</svg>')
        return path


PREV_ICON = MediaIcon("prev")
NEXT_ICON = MediaIcon("next")
SHUFFLE_TIPS = {"off": "Вперемешку: выключено — альбом играет по порядку",
                "all": "Вперемешку: вся музыка — саундтреки и своя музыка из «Настроек»",
                "soundtracks": "Вперемешку: только скачанные саундтреки"}


def shuffle_tooltip(mode):
    return SHUFFLE_TIPS[mode] + "\nНажмите, чтобы переключить: выкл → вся музыка → только саундтреки"


def make_shuffle_button(win, parent):
    """Кнопка «вперемешку»: по кругу выключено → вся музыка → только саундтреки."""
    button = TransparentToggleToolButton(MediaIcon("shuffle"), parent)
    button.clicked.connect(win.cycle_shuffle)
    return button


def refresh_shuffle_button(button, mode):
    button.setChecked(mode != "off")
    button.setIcon(MediaIcon("shuffle_ost" if mode == "soundtracks" else "shuffle"))
    button.setToolTip(shuffle_tooltip(mode))


def make_repeat_button(win, parent):
    """Кнопка «повтор трека»: включена — один трек по кругу, выключена — альбом по кругу."""
    button = TransparentToggleToolButton(MediaIcon("repeat"), parent)
    button.clicked.connect(win.toggle_repeat)
    return button


def refresh_repeat_button(button, repeat_one):
    button.setChecked(repeat_one)
    button.setIcon(MediaIcon("repeat_one" if repeat_one else "repeat"))
    button.setToolTip("Повтор трека: включён — играет один трек по кругу" if repeat_one
                      else "Повтор трека: выключен — альбом играет по кругу")


class MiniPlayer(QWidget):
    """Маленький плеер в правом верхнем углу рулетки, страницы игры и браузера:
    нота — перейти на вкладку «Саундтреки» к тому, что играет, выключить звук, предыдущий трек, пауза,
    следующий трек, вперемешку, повтор трека. Название трека — в подсказках."""

    def __init__(self, win, parent):
        super().__init__(parent)
        self.win = win
        self.music_button = TransparentToolButton(FIF.MUSIC, self)
        self.music_button.clicked.connect(win.show_playing_music)
        self.mute_button = TransparentToolButton(FIF.VOLUME, self)
        self.mute_button.clicked.connect(win.toggle_music)
        self.prev_button = TransparentToolButton(PREV_ICON, self)
        self.prev_button.clicked.connect(win.music.prev_track)
        self.play_button = TransparentToolButton(FIF.PAUSE, self)
        self.play_button.clicked.connect(win.play_pause)
        self.next_button = TransparentToolButton(NEXT_ICON, self)
        self.next_button.clicked.connect(lambda: win.music.next_track())
        self.shuffle_button = make_shuffle_button(win, self)
        self.repeat_button = make_repeat_button(win, self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for button in (self.music_button, self.mute_button, self.prev_button, self.play_button, self.next_button,
                       self.shuffle_button, self.repeat_button):
            layout.addWidget(button)
        win.music.trackChanged.connect(self.refresh)
        win.music.stateChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        music = self.win.music
        track = f"«{music.title}»" + (f" — {music.album['name']}" if music.album.get("name") else "")
        self.music_button.setToolTip(f"К «Саундтрекам»: {track}" if music.title else "К «Саундтрекам»")
        self.mute_button.setIcon(FIF.VOLUME if music.enabled else FIF.MUTE)
        self.mute_button.setToolTip("Выключить музыку" if music.enabled else "Включить музыку")
        self.play_button.setIcon(FIF.PAUSE if music.playing else FIF.PLAY)
        self.play_button.setToolTip(("Пауза: " if music.playing else "Слушать: ") + track)
        several = len(music.tracks) > 1
        self.prev_button.setVisible(several)
        self.next_button.setVisible(several)
        self.repeat_button.setVisible(several)  # один трек и так играет по кругу
        self.prev_button.setToolTip("Предыдущий трек")
        self.next_button.setToolTip(f"Следующий трек (сейчас {track})")
        refresh_shuffle_button(self.shuffle_button, music.shuffle_mode)
        refresh_repeat_button(self.repeat_button, music.repeat_one)
        self.adjustSize()


# --- Барабан с обложками ----------------------------------------------------
class CoverReel(QWidget):
    """Лента обложек: быстро прокручивается и плавно останавливается на выбранной игре."""

    spinFinished = Signal(dict)
    cardPassed = Signal()  # очередная карточка прошла через центр — для щелчка

    CARD_W, CARD_H, GAP = 240, 360, 32
    SIDE = 3  # сколько карточек держим слева и справа от центральной
    SPIN_DURATION = 4200  # мс

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(self.CARD_H + 40)
        self.strip = []  # игры на ленте по порядку
        self.offset = 0.0  # сдвиг ленты в пикселях
        self.winner = None
        self.glow = 0.0  # яркость рамки вокруг выпавшей игры
        self.intro = 1.0  # появление ленты: 0 — не видна, 1 — на месте
        self._last_index = 0  # для щелчков: какая карточка сейчас в центре
        self._covers = {}  # кэш: название -> QPixmap или None

        self.spin_anim = QVariantAnimation(self)
        self.spin_anim.setEasingCurve(QEasingCurve.OutQuart)  # резкий старт, долгое торможение
        self.spin_anim.valueChanged.connect(self._set_offset)
        self.spin_anim.finished.connect(self._on_spin_done)

        self.glow_anim = self._make_anim(450, self._set_glow)
        self.intro_anim = self._make_anim(900, self._set_intro)

    def _make_anim(self, duration, slot):
        anim = QVariantAnimation(self)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(slot)
        return anim

    @property
    def step(self):
        return self.CARD_W + self.GAP

    def is_spinning(self):
        return self.spin_anim.state() == QAbstractAnimation.Running

    def center_index(self):
        return min(max(round(self.offset / self.step), 0), max(len(self.strip) - 1, 0))

    @staticmethod
    def _pool(games):
        """Для заполнения ленты берём игры с обложками, если их достаточно."""
        covered = [g for g in games if core.find_cover(g["name"])]
        return covered if len(covered) >= 10 else games

    def show_idle(self, games, center=None):
        """Неподвижная лента из случайных игр; center — игра по центру."""
        self.spin_anim.stop()
        self.glow_anim.stop()
        self.winner, self.glow = center, (1.0 if center else 0.0)
        if games:
            pool = self._pool(games)
            self.strip = [random.choice(pool) for _ in range(self.SIDE * 2 + 1)]
            if center:
                self.strip[self.SIDE] = center
            self.offset = self.SIDE * self.step
        else:
            self.strip, self.offset = [], 0.0
        self.update()

    def play_intro(self):
        self.intro_anim.stop()
        self.intro = 0.0
        self.intro_anim.start()

    def spin(self, games, winner):
        pool = self._pool(games)
        # Оставляем видимые сейчас карточки, чтобы лента не «прыгала» при старте
        ci = self.center_index()
        if self.strip and ci >= self.SIDE and ci + self.SIDE < len(self.strip):
            head = self.strip[ci - self.SIDE: ci + self.SIDE + 1]
        else:
            head = [random.choice(pool) for _ in range(self.SIDE * 2 + 1)]
        filler = [random.choice(pool) for _ in range(random.randint(26, 34))]
        tail = [random.choice(pool) for _ in range(self.SIDE)]
        self.strip = head + filler + [winner] + tail

        for game in self.strip:  # подгружаем обложки заранее, чтобы анимация не дёргалась
            self._cover(game["name"])

        self.winner, self.glow = winner, 0.0
        self.glow_anim.stop()
        start = self.SIDE * self.step
        end = (len(self.strip) - self.SIDE - 1) * self.step
        self.offset = float(start)
        self._last_index = round(self.offset / self.step)
        self.spin_anim.setDuration(self.SPIN_DURATION)
        self.spin_anim.setStartValue(float(start))
        self.spin_anim.setEndValue(float(end))
        self.spin_anim.start()

    def refresh_cover(self, name):
        self._covers.pop(name, None)
        self.update()

    def clear_cache(self):
        self._covers.clear()
        self.update()

    def _cover(self, name):
        if name not in self._covers:
            path = core.find_cover(name)
            pixmap = QPixmap(path) if path else None
            self._covers[name] = pixmap if pixmap and not pixmap.isNull() else None
        return self._covers[name]

    def _set_offset(self, value):
        self.offset = value
        index = round(value / self.step)
        if index != self._last_index:
            self._last_index = index
            self.cardPassed.emit()
        self.update()

    def _set_glow(self, value):
        self.glow = value
        self.update()

    def _set_intro(self, value):
        self.intro = value
        self.update()

    def _on_spin_done(self):
        self.glow_anim.start()
        self.spinFinished.emit(self.winner)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)

        if not self.strip:
            painter.setPen(QColor(255, 255, 255, 180) if isDarkTheme() else QColor(0, 0, 0, 150))
            painter.drawText(self.rect(), Qt.AlignCenter, "Список пуст — добавьте игры на вкладке «Игры»")
            return

        cx, cy = self.width() / 2, self.height() / 2
        cards = []
        for i, game in enumerate(self.strip):
            x = cx + i * self.step - self.offset
            if -self.CARD_W < x < self.width() + self.CARD_W:
                cards.append((abs(x - cx) / self.step, x, game))

        # Дальние карточки рисуем первыми, центральную — поверх остальных.
        # В широком окне чётких карточек три, а дальние притушены. В узком окне видно всего три —
        # тогда притушены уже соседние, и чёткой остаётся одна центральная. Между ними — плавно.
        compact = min(1.0, max(0.0, (4 * self.step - self.width()) / self.step))
        fade = 0.32 + 0.32 * compact
        for distance, x, game in sorted(cards, key=lambda card: -card[0]):
            scale = max(0.72, 1 - 0.14 * distance)
            w, h = self.CARD_W * scale, self.CARD_H * scale
            # При появлении карточки всплывают снизу: сначала центральная, потом боковые
            lag = min(1.0, max(0.0, self.intro * 1.6 - distance * 0.2))
            y = cy - h / 2 + (1 - lag) * 70
            painter.setOpacity(max(0.2, 1 - fade * distance) * lag)
            self._draw_card(painter, QRectF(x - w / 2, y, w, h), game)

        if self.winner and self.glow > 0:
            painter.setOpacity(self.glow)
            rect = QRectF(cx - self.CARD_W / 2, cy - self.CARD_H / 2, self.CARD_W, self.CARD_H)
            painter.setPen(QPen(themeColor(), 4))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), 14, 14)

    def _draw_card(self, painter, rect, game):
        dark = isDarkTheme()
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)

        painter.save()
        painter.setClipPath(path)
        pixmap = self._cover(game["name"])
        if pixmap:
            painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
        else:
            # Обложки нет — карточка с названием
            painter.fillRect(rect, QColor(255, 255, 255, 22) if dark else QColor(0, 0, 0, 14))
            painter.setPen(QColor(255, 255, 255, 220) if dark else QColor(0, 0, 0, 200))
            font = painter.font()
            font.setPointSizeF(11)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect.adjusted(14, 14, -14, -14), Qt.AlignCenter | Qt.TextWordWrap, game["name"])
        painter.restore()

        painter.setPen(QPen(QColor(255, 255, 255, 45) if dark else QColor(0, 0, 0, 35), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)


# --- Страница игры: картинки и видео ------------------------------------------
class ImageLoader(QObject):
    """Скачивает картинки по очереди в одном фоновом потоке. Нужные прямо сейчас ставятся в начало."""

    loaded = Signal(str, object)  # адрес, байты (None — не скачалось)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._queue = []
        self._running = False

    def request(self, url, front=False):
        with self._lock:
            if url in self._queue:
                self._queue.remove(url)
            if front:
                self._queue.insert(0, url)
            else:
                self._queue.append(url)
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def clear(self):
        with self._lock:
            self._queue.clear()

    def _run(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                url = self._queue.pop(0)
            content = core.fetch_image(url)
            try:
                self.loaded.emit(url, content)
            except RuntimeError:
                return  # страницу уже закрыли вместе с программой


class Thumb(QWidget):
    """Превью скриншота или трейлера в ленте под большим просмотром."""

    clicked = Signal()
    W, H = 112, 63

    def __init__(self, video, parent=None):
        super().__init__(parent)
        self.video = video
        self.pixmap = None
        self.selected = False
        self.setFixedSize(self.W + 6, self.H + 6)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Трейлер" if video else "Скриншот")

    def set_pixmap(self, pixmap):
        self.pixmap = pixmap
        self.update()

    def set_selected(self, selected):
        self.selected = selected
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rect = QRectF(3, 3, self.W, self.H)
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        painter.save()
        painter.setClipPath(path)
        if self.pixmap:
            painter.drawPixmap(rect, self.pixmap, QRectF(self.pixmap.rect()))
        else:
            painter.fillRect(rect, QColor(128, 128, 128, 40))
        painter.restore()

        if self.video:  # кнопка ▶ поверх превью трейлера
            center = rect.center()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(center, 15, 15)
            painter.setBrush(QColor(255, 255, 255))
            painter.drawPolygon(QPolygonF([QPointF(center.x() - 5, center.y() - 8),
                                           QPointF(center.x() - 5, center.y() + 8),
                                           QPointF(center.x() + 9, center.y())]))
        if self.selected:
            painter.setPen(QPen(themeColor(), 2.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1.5, -1.5, 1.5, 1.5), 7, 7)


class MediaView(QWidget):
    """Большой просмотр скриншота: скриншот целиком, по центру, со скруглёнными углами.
    Вокруг него ничего не рисуем — там видна общая размытая подложка страницы."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap = None
        self.text = ""

    def show_pixmap(self, pixmap):
        self.pixmap, self.text = pixmap, ""
        self.update()

    def show_text(self, text):
        self.pixmap, self.text = None, text
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        dark = isDarkTheme()
        if self.pixmap:
            scale = min(rect.width() / self.pixmap.width(), rect.height() / self.pixmap.height())
            w, h = self.pixmap.width() * scale, self.pixmap.height() * scale
            target = QRectF(rect.x() + (rect.width() - w) / 2, rect.y() + (rect.height() - h) / 2, w, h)
            path = QPainterPath()
            path.addRoundedRect(target, 10, 10)  # скругляем сам скриншот, а не пустую область вокруг
            painter.setClipPath(path)
            painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        elif self.text:
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            painter.setClipPath(path)
            painter.fillRect(rect, QColor(255, 255, 255, 16) if dark else QColor(0, 0, 0, 16))
            painter.setPen(QColor(255, 255, 255, 170) if dark else QColor(0, 0, 0, 150))
            painter.drawText(rect.adjusted(24, 24, -24, -24), Qt.AlignCenter | Qt.TextWordWrap, self.text)


class VideoView(QGraphicsView):
    """Трейлер рисуется внутри обычного виджета, а не в отдельном системном окне, как у QVideoWidget.

    Отдельное окно ломало затемнённые диалоги Fluent: они переставали перерисовываться,
    и программа выглядела зависшей. Заодно видео честно прокручивается вместе со страницей.
    Картинок в сцене может быть две: пока одна показывает трейлер, вторая невидимо готовит 1080p.
    """

    clicked = Signal()
    moved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.items = []
        self.item = self.add_item()
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor(0, 0, 0))
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def add_item(self, hidden=False):
        item = QGraphicsVideoItem()
        item.setAspectRatioMode(Qt.KeepAspectRatioByExpanding)  # без чёрных полос, края кадра обрезаются
        item.setSize(QSizeF(self.viewport().size()))
        if hidden:
            item.setOpacity(0.0)
        self.scene().addItem(item)
        self.items.append(item)
        return item

    def set_current(self, item):
        """Показать item; остальные картинки убираются из сцены (их проигрыватели останавливает хозяин)."""
        item.setOpacity(1.0)
        for old in self.items:
            if old is not item:
                self.scene().removeItem(old)
        self.items, self.item = [item], item

    def remove_item(self, item):
        if item in self.items and item is not self.item:
            self.scene().removeItem(item)
            self.items.remove(item)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        size = QSizeF(self.viewport().size())
        for item in self.items:
            item.setSize(size)
        self.scene().setSceneRect(QRectF(QPointF(0, 0), size))

    def mouseMoveEvent(self, event):
        self.moved.emit()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


class LiveButton(QWidget):
    """Значок «живой фон» в шапке страницы игры: тёмный кружок со значком видео.

    Пока живой фон качается, по краю кружка бежит кольцо загрузки, как при установке приложения в iOS
    (размер файла неизвестен — по кругу крутится дуга). Живой фон включён — кружок цвета темы.
    """

    clicked = Signal()
    SIZE = 36
    RING = 3

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.progress = None  # None — не качается, -1 — качается, сколько осталось неизвестно, 0–100 — проценты
        self.active = False
        self.hover = False
        self._angle = 0
        self.spin_timer = QTimer(self)  # крутит дугу, пока размер файла неизвестен
        self.spin_timer.setInterval(30)
        self.spin_timer.timeout.connect(self._spin)

    def set_progress(self, progress):
        self.progress = progress
        if progress is not None and progress < 0:
            self.spin_timer.start()
        else:
            self.spin_timer.stop()
        self.update()

    def set_active(self, active):
        self.active = active
        self.update()

    def _spin(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def enterEvent(self, event):
        self.hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(255, 255, 255, 170 if self.hover else 110), 1))
        painter.setBrush(themeColor() if self.active else QColor(0, 0, 0, 170 if self.hover else 130))
        painter.drawEllipse(rect)
        side = 18
        offset = (self.SIZE - side) // 2
        FIF.VIDEO.icon(color=WHITE).paint(painter, QRect(offset, offset, side, side))
        if self.progress is None:
            return
        ring = rect.adjusted(self.RING / 2 + 1, self.RING / 2 + 1, -self.RING / 2 - 1, -self.RING / 2 - 1)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 60), self.RING))
        painter.drawEllipse(ring)
        pen = QPen(themeColor(), self.RING)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        if self.progress < 0:
            painter.drawArc(ring, -self._angle * 16, 90 * 16)
        elif self.progress > 0:
            painter.drawArc(ring, 90 * 16, -round(360 * 16 * self.progress / 100))  # от верха по часовой


class HeroHeader(QWidget):
    """Шапка страницы игры: широкий фон, который внизу растворяется в окне, логотип или название поверх
    и большая кнопка ▶ по центру, если у игры есть трейлер."""

    playClicked = Signal()
    liveRequested = Signal()  # нажали значок, а живой фон ещё не скачан — страница игры начинает загрузку
    MIN_HEIGHT, MAX_HEIGHT = 360, 600
    PLAY_RADIUS = 40
    LOGO_MAX_UPSCALE = 2.0  # маленькие логотипы увеличиваем, но не сильнее — иначе станут мыльными

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.MIN_HEIGHT)
        self.setMouseTracking(True)
        self.hero = self.logo = None
        self.static = None  # обычный фон — к нему возвращаемся, выключив живой
        self.movie = None  # играющий живой фон: его кадры подменяют self.hero
        self.live_path = None  # скачанный живой фон этой игры
        self._movie_held = False  # на время трейлера живой фон стоит
        # Значок «живой фон» — только у игр, для которых он нашёлся
        self.live_button = LiveButton(self)
        self.live_button.clicked.connect(self._on_live_clicked)
        self.live_button.hide()
        self.title = ""
        self.has_trailer = False
        self.play_hover = False
        self.intro = 1.0
        self.intro_anim = QVariantAnimation(self)
        self.intro_anim.setDuration(700)
        self.intro_anim.setStartValue(0.0)
        self.intro_anim.setEndValue(1.0)
        self.intro_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.intro_anim.valueChanged.connect(self._set_intro)

    def fit_width(self, width):
        # Фоны Steam примерно 3:1; чуть выше, чтобы шапка смотрелась крупно, но не съедала всё окно
        self.setFixedHeight(max(self.MIN_HEIGHT, min(round(width * 0.38), self.MAX_HEIGHT)))

    def set_game(self, title):
        self._stop_movie()
        self.live_path = None
        self.live_button.set_progress(None)
        self.live_button.set_active(False)
        self.live_button.hide()
        self.hero = self.logo = self.static = None
        self.title = title
        self.has_trailer = False
        self.play_intro()

    def set_trailer(self, has_trailer):
        self.has_trailer = has_trailer
        self.update()

    def _play_center(self):
        return QPointF(self.width() / 2, self.height() * 0.36)

    def _logo_box(self):
        """Рамка под логотип: по центру, под кнопкой ▶ (или в середине нижней части, если трейлера нет)."""
        w, h = self.width(), self.height()
        box_w = min(w * 0.5, 560)
        bottom = h - 28
        top = self._play_center().y() + self.PLAY_RADIUS + 18 if self.has_trailer else h * 0.4
        box_h = min(bottom - top, h * 0.32)
        return QRectF((w - box_w) / 2, bottom - box_h, box_w, box_h)

    def _over_play(self, pos):
        if not self.has_trailer:
            return False
        d = QPointF(pos) - self._play_center()
        return d.x() ** 2 + d.y() ** 2 <= (self.PLAY_RADIUS + 6) ** 2

    def mouseMoveEvent(self, event):
        hover = self._over_play(event.position())
        if hover != self.play_hover:
            self.play_hover = hover
            self.setCursor(Qt.PointingHandCursor if hover else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self.play_hover:
            self.play_hover = False
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._over_play(event.position()):
            self.playClicked.emit()
            return
        super().mousePressEvent(event)

    def set_hero(self, pixmap):
        self.static = pixmap
        if self.movie:
            return  # сейчас играет живой фон — обычный вернётся, когда его выключат
        self.hero = pixmap
        self.play_intro()

    # --- Живой фон ---
    # Сначала всегда обычный фон. В фоне только проверяется, есть ли у игры живой, — и если есть, слева
    # вверху появляется значок. Первое нажатие качает живой фон (кольцо на значке) и включает его,
    # следующие переключают шапку между живым и обычным фоном. Сам живой фон не включается и заранее
    # не качается: файлы тяжёлые, а анимация заметно нагружает страницу.
    def show_live(self, path=None):
        """У игры есть живой фон; path — он уже скачан. False — скачанный файл Qt не открывает."""
        if path and not QMovie(path).isValid():
            return False
        self.live_path = path
        self.live_button.set_progress(None)
        self.live_button.set_active(False)
        self._update_live_button()
        self.live_button.show()
        self.live_button.raise_()
        return True

    def set_live_progress(self, progress):
        self.live_button.set_progress(progress)
        self.live_button.setToolTip("Живой фон загружается…")

    def live_ready(self, path):
        """Живой фон скачался — сразу включаем: его для этого и качали. False — Qt не может его открыть."""
        self.live_button.set_progress(None)
        if not QMovie(path).isValid():
            self._update_live_button()
            return False
        self.live_path = path
        self._toggle_live(True)
        return True

    def live_failed(self):
        self.live_button.set_progress(None)
        self._update_live_button()

    def _on_live_clicked(self):
        if self.live_button.progress is not None:
            return  # уже качается
        if self.live_path:
            self._toggle_live(self.movie is None)
        else:
            self.set_live_progress(-1)
            self.liveRequested.emit()

    def _toggle_live(self, on):
        self._stop_movie()
        if on and self.live_path:
            movie = QMovie(self.live_path, parent=self)  # кадры не кэшируем: заняли бы сотни мегабайт
            movie.frameChanged.connect(self._on_movie_frame)
            self.movie = movie
            movie.start()
            self._sync_movie()
        else:
            self.hero = self.static
            self.update()
        self.live_button.set_active(self.movie is not None)
        self._update_live_button()

    def _update_live_button(self):
        if self.movie:
            tip = "Вернуть обычный фон"
        elif self.live_path:
            tip = "Включить живой фон"
        else:
            tip = "Включить живой фон — он скачается из SteamGridDB"
        self.live_button.setToolTip(tip)

    def hold_movie(self, held):
        """Поставить живой фон на паузу (трейлер поверх) или продолжить."""
        self._movie_held = held
        self._sync_movie()

    def _sync_movie(self):
        if self.movie and self.movie.state() != QMovie.NotRunning:
            self.movie.setPaused(self._movie_held or not self.isVisible())

    def _stop_movie(self):
        if self.movie:
            self.movie.stop()
            self.movie.deleteLater()
            self.movie = None

    def _on_movie_frame(self, _frame):
        self.hero = self.movie.currentPixmap()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.live_button.move(16, 16)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_movie()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_movie()  # страницу игры не видно — кадры не крутим

    def set_logo(self, pixmap):
        self.logo = pixmap
        self.update()

    def play_intro(self):
        self.intro_anim.stop()
        self.intro = 0.0
        self.intro_anim.start()

    def _set_intro(self, value):
        self.intro = value
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        painter.setOpacity(self.intro)

        if self.hero:
            # Рисуем фон в отдельный слой и «стираем» его низ градиентом — так он растворяется в окне
            ratio = self.devicePixelRatioF()
            layer = QPixmap(round(w * ratio), round(h * ratio))
            layer.setDevicePixelRatio(ratio)
            layer.fill(Qt.transparent)
            p = QPainter(layer)
            p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(0, 0, w, h), 12, 12)
            p.setClipPath(clip)
            scale = max(w / self.hero.width(), h / self.hero.height())  # заполняем целиком, лишнее обрезаем
            sw, sh = self.hero.width() * scale, self.hero.height() * scale
            p.drawPixmap(QRectF((w - sw) / 2, (h - sh) / 2, sw, sh), self.hero, QRectF(self.hero.rect()))
            fade = QLinearGradient(0, 0, 0, h)
            fade.setColorAt(0.0, QColor(0, 0, 0, 255))
            fade.setColorAt(0.55, QColor(0, 0, 0, 255))
            fade.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            p.fillRect(QRectF(0, 0, w, h), fade)
            p.end()
            painter.drawPixmap(0, 0, layer)

        # Логотип или название — по центру, под кнопкой ▶, в рамке одного размера для всех игр.
        # При появлении слегка всплывают.
        box = self._logo_box().translated(0, (1 - self.intro) * 20)
        if self.logo:
            scale = min(box.width() / self.logo.width(), box.height() / self.logo.height(), self.LOGO_MAX_UPSCALE)
            lw, lh = self.logo.width() * scale, self.logo.height() * scale
            target = QRectF(box.center().x() - lw / 2, box.center().y() - lh / 2, lw, lh)
            painter.drawPixmap(target, self.logo, QRectF(self.logo.rect()))
        elif self.title:
            font = QFont(self.font())
            font.setPixelSize(max(26, min(round(h * 0.11), 48)))
            font.setBold(True)
            painter.setFont(font)
            flags = Qt.AlignCenter | Qt.TextWordWrap
            if self.hero:
                painter.setPen(QColor(0, 0, 0, 150))
                painter.drawText(box.translated(2, 2), flags, self.title)
                painter.setPen(QColor(255, 255, 255))
            else:
                painter.setPen(QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0))
            painter.drawText(box, flags, self.title)

        if self.has_trailer:
            center, r = self._play_center(), self.PLAY_RADIUS * (1.08 if self.play_hover else 1.0)
            painter.setPen(QPen(QColor(255, 255, 255, 200), 2))
            painter.setBrush(themeColor() if self.play_hover else QColor(0, 0, 0, 140))
            painter.drawEllipse(center, r, r)
            painter.setPen(Qt.NoPen)
            painter.setBrush(WHITE)
            s = r * 0.42  # треугольник чуть смещён вправо — так он выглядит по центру
            painter.drawPolygon(QPolygonF([QPointF(center.x() - s * 0.7, center.y() - s),
                                           QPointF(center.x() - s * 0.7, center.y() + s),
                                           QPointF(center.x() + s, center.y())]))


# --- Диалоги ----------------------------------------------------------------
class CheckList(ScrollArea):
    """Прокручиваемый список флажков для выбора коллекций."""

    def __init__(self, collections, checked_ids, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFixedHeight(150)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        self.boxes = []
        for collection in collections:
            box = CheckBox(f"{collection['name']} ({len(collection['apps'])})", inner)
            box.setChecked(collection["id"] in checked_ids)
            layout.addWidget(box)
            self.boxes.append((collection, box))
        layout.addStretch(1)
        self.setWidget(inner)
        # Прозрачный фон у области прокрутки и её содержимого
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")

    def checked(self):
        return [collection for collection, box in self.boxes if box.isChecked()]


class AddGamesDialog(MessageBoxBase):
    def __init__(self, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Добавить игры", self))
        self.edit = TextEdit(self)
        self.edit.setPlaceholderText("По одной игре в строке (или через «|»)")
        self.edit.setMinimumSize(420, 220)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("Добавить")
        self.cancelButton.setText("Отмена")

    def names(self):
        return re.split(r"[|\n]", self.edit.toPlainText())


class ImportDialog(MessageBoxBase):
    """Выбор коллекций для импорта. Считаются только игры Steam — свои и семейной библиотеки:
    сторонние игры, добавленные в коллекции вручную, пропускаются."""

    def __init__(self, parent, collections, known_ids, family_count, settings):
        super().__init__(parent)
        self.hidden_apps = set()
        visible = []
        for collection in collections or []:
            steam_apps = collection["apps"] & known_ids
            if collection["id"] == "hidden":
                self.hidden_apps = steam_apps
            elif steam_apps:
                visible.append({**collection, "apps": steam_apps})

        self.viewLayout.addWidget(SubtitleLabel("Импорт из Steam", self))
        if visible:
            self.viewLayout.addWidget(StrongBodyLabel("Из каких коллекций брать игры", self))
            hint = CaptionLabel("Ничего не отмечено — все ваши игры и семейные игры из коллекций. "
                                "В скобках — сколько в коллекции игр Steam; сторонние игры не учитываются.", self)
            hint.setWordWrap(True)
            self.viewLayout.addWidget(hint)
            self.collection_list = CheckList(visible, set(settings["import_include"]), self)
            self.viewLayout.addWidget(self.collection_list)
        else:
            self.collection_list = None
            hint = CaptionLabel("Коллекции Steam на этом ПК не найдены — будут импортированы только ваши "
                                "игры. Проверьте, что Steam установлен и SteamID указан верно.", self)
            hint.setWordWrap(True)
            self.viewLayout.addWidget(hint)
        if family_count:
            self.viewLayout.addWidget(CaptionLabel(f"Найдено игр семейной библиотеки: {family_count}", self))

        self.skip_hidden = None
        if self.hidden_apps:
            self.skip_hidden = CheckBox(f"Пропускать скрытые игры ({len(self.hidden_apps)})", self)
            self.skip_hidden.setChecked(settings["import_skip_hidden"])
            self.viewLayout.addWidget(self.skip_hidden)
        self.unplayed = CheckBox("Только игры, в которые я ещё не играл", self)
        self.unplayed.setChecked(settings["import_unplayed"])
        self.viewLayout.addWidget(self.unplayed)
        if family_count:
            note = CaptionLabel("Время в семейных играх Steam не сообщает — они считаются неначатыми.", self)
            note.setWordWrap(True)
            self.viewLayout.addWidget(note)
        self.replace = CheckBox("Заменить мой список: убрать игры Steam, которых нет в выборе", self)
        self.replace.setChecked(settings["import_replace"])
        self.viewLayout.addWidget(self.replace)
        self.viewLayout.addWidget(CaptionLabel("Игры, добавленные вручную, останутся в любом случае.", self))

        self.widget.setMinimumWidth(500)
        self.yesButton.setText("Импортировать")
        self.cancelButton.setText("Отмена")

    def selection(self):
        """Выбор пользователя в виде, который сохраняется для быстрой синхронизации."""
        chosen = self.collection_list.checked() if self.collection_list else []
        return {
            "import_include": [c["id"] for c in chosen],
            "import_skip_hidden": bool(self.skip_hidden and self.skip_hidden.isChecked()),
            "import_unplayed": self.unplayed.isChecked(),
            "import_replace": self.replace.isChecked(),
        }


class SteamActionDialog(MessageBoxBase):
    """Окно «ждём Steam»: показывает, что программа не зависла, пока Steam откликается.

    Закрывается само: когда команда передана в Steam (для установки и магазина), когда игра
    появилась в реестре Steam как запущенная, или когда окно Steam перехватило фокус.
    """

    TITLES = {"run": "Запускаем игру", "install": "Готовим установку", "store": "Открываем магазин Steam"}
    TIMEOUT = 120  # секунд
    DELIVERED_CLOSE = 1.5  # через сколько секунд после передачи команды закрыть окно (установка, магазин)
    RUN_WAIT = 15  # сколько ждать, пока запущенная игра появится в реестре Steam

    def __init__(self, parent, game, action):
        super().__init__(parent)
        self.game, self.action = game, action
        self.appid = game.get("appid") if game else None
        self.started = time.monotonic()
        self.delivered_at = None  # когда команда ушла в Steam
        self.steam_was_running = core.steam_running()
        # Не «done»: так называется встроенный метод QDialog.done(), через который окно закрывается
        self.finished_waiting = False

        self.viewLayout.addWidget(SubtitleLabel(self.TITLES[action], self))
        if game:
            name = BodyLabel(f"«{game['name']}»", self)
            name.setWordWrap(True)
            self.viewLayout.addWidget(name)
        self.status = CaptionLabel("", self)
        self.status.setWordWrap(True)
        self.viewLayout.addWidget(self.status)
        # Пока ждём — бегущая полоса, когда готово — заполненная
        self.busy_bar = IndeterminateProgressBar(self)
        self.done_bar = ProgressBar(self)
        self.done_bar.setValue(100)
        self.done_bar.hide()
        self.viewLayout.addWidget(self.busy_bar)
        self.viewLayout.addWidget(self.done_bar)

        self.yesButton.hide()
        self.cancelButton.setText("Скрыть")
        self.widget.setMinimumWidth(420)
        self.set_status("Передаём команду в Steam…" if self.steam_was_running else
                        "Steam не запущен — запускаем его, обычно это 20–40 секунд.")

        QApplication.instance().applicationStateChanged.connect(self._on_app_state)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(300)

    def set_status(self, text):
        self.status.setText(text)

    def _elapsed(self):
        return time.monotonic() - self.started

    def delivered(self):
        """Команда передана Windows → Steam (сама ссылка открылась без ошибки)."""
        if self.delivered_at is None:
            self.delivered_at = time.monotonic()

    def _poll(self):
        if self.finished_waiting:
            return
        now = time.monotonic()
        if self.action == "run" and self.appid and core.running_app_id() == self.appid:
            self.finish("Игра запущена! Приятной игры 🎮")
            return
        if not self.steam_was_running and core.steam_running():
            self.steam_was_running = True
            self.set_status("Steam запустился, передаём команду…")
        if self.delivered_at is not None and self.steam_was_running:
            since = now - self.delivered_at
            if self.action != "run" and since > self.DELIVERED_CLOSE:
                self.finish("Команда передана в Steam.", delay=500)
            elif self.action == "run" and since > self.RUN_WAIT:
                self.finish("Steam получил команду запуска — продолжите в его окне.", delay=1000)
        if self._elapsed() > self.TIMEOUT and not self.finished_waiting:
            self.finish("Steam долго не отвечает. Проверьте окно Steam — команда могла дойти.", delay=4000)

    def _on_app_state(self, state):
        # Окно Steam вышло на передний план — значит, команда дошла
        if state != Qt.ApplicationActive and not self.finished_waiting:
            text = {"run": "Steam готовит запуск — продолжите в его окне.",
                    "install": "Steam открыл окно установки.",
                    "store": "Магазин Steam открыт."}[self.action]
            # Окно закрывается сразу, чтобы при возврате в программу его уже не было
            self.finish(text, delay=0)

    def fail(self, message):
        if self.finished_waiting:
            return
        self.finish(f"Не удалось открыть Steam: {message}", delay=6000, ok=False)

    def finish(self, text, delay=1500, ok=True):
        self.finished_waiting = True
        self.timer.stop()
        self.busy_bar.hide()
        self.done_bar.setVisible(ok)
        self.set_status(text)
        core.log.info("Steam (%s, %s): %s за %.1f с", self.action, self.appid, text, self._elapsed())
        self.cancelButton.setText("Закрыть")
        QTimer.singleShot(delay, self._close)

    def reject(self):
        # Закрытие кнопкой: перестаём следить за Steam
        self.finished_waiting = True
        self.timer.stop()
        self._disconnect()
        super().reject()

    def _disconnect(self):
        try:
            QApplication.instance().applicationStateChanged.disconnect(self._on_app_state)
        except (RuntimeError, TypeError):
            pass

    def _close(self):
        if self.isVisible():
            self.reject()


class ProgressRelay(QObject):
    """Передаёт прогресс из фонового потока в окно: сигнал доставляется в главный поток через очередь Qt."""

    progress = Signal(str, int)

    def report(self, text, percent):
        self.progress.emit(text, percent)


class ImportProgressDialog(MessageBoxBase):
    """Окно прогресса загрузки библиотеки Steam. «Скрыть» убирает окно, загрузка продолжается."""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        self.status = CaptionLabel("Подключаюсь к Steam…", self)
        self.status.setWordWrap(True)
        self.viewLayout.addWidget(self.status)
        self.bar = ProgressBar(self)
        self.bar.setRange(0, 100)
        self.viewLayout.addWidget(self.bar)
        self.hint = CaptionLabel("Первый раз до минуты: ищутся названия семейных игр. Дальше они берутся из кэша.",
                                 self)
        self.hint.setWordWrap(True)
        self.viewLayout.addWidget(self.hint)

        self.yesButton.hide()
        self.cancelButton.setText("Скрыть")
        self.widget.setMinimumWidth(460)
        self.was_hidden = False  # не «closed»/«done» — чтобы не задеть встроенные имена Qt

    def set_progress(self, text, percent):
        if self.was_hidden:
            return
        self.status.setText(text)
        self.bar.setValue(max(self.bar.value(), percent))  # полоса не откатывается назад

    def reject(self):
        self.was_hidden = True
        super().reject()

    def close_now(self):
        if not self.was_hidden and self.isVisible():
            self.reject()


class ClearDialog(MessageBoxBase):
    def __init__(self, parent, count):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Очистить список?", self))
        text = BodyLabel(f"Из списка будут удалены все игры: {count}. Ключи и настройки останутся.", self)
        text.setWordWrap(True)
        self.viewLayout.addWidget(text)
        self.covers = CheckBox("Удалить и скачанные обложки (в том числе выбранные вручную)", self)
        self.viewLayout.addWidget(self.covers)
        self.widget.setMinimumWidth(460)
        self.yesButton.setText("Очистить")
        self.cancelButton.setText("Отмена")


# --- Страница игры ----------------------------------------------------------
def format_ms(ms):
    seconds = max(ms, 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


WHITE = QColor(255, 255, 255)


class OverlayBar(QWidget):
    """Полоса управления поверх видео: затемнение плавно уходит в прозрачность к середине кадра."""

    def __init__(self, parent, from_top):
        super().__init__(parent)
        self.from_top = from_top

    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        dark, clear = QColor(0, 0, 0, 190), QColor(0, 0, 0, 0)
        gradient.setColorAt(0.0, dark if self.from_top else clear)
        gradient.setColorAt(1.0, clear if self.from_top else dark)
        painter.fillRect(self.rect(), gradient)


class StreamRelay(QObject):
    """Сигналы загрузки потока трейлера из фонового потока в окно. Первое число — номер загрузки."""

    progress = Signal(int, int, int)  # номер, высота кадра, проценты
    ready = Signal(int, str)  # номер, путь к готовому mp4
    failed = Signal(int, str)  # номер, причина
    preview = Signal(int, str)  # номер, путь к быстрому старту в низком качестве (пусто — не собрался)


class VideoOverlay(QWidget):
    """Трейлер на всю рабочую область окна (под заголовком окна), кнопки — поверх видео.

    Видео заполняет область целиком, лишние края кадра обрезаются — чёрных полос нет.
    Кнопки прячутся через пару секунд без движения мыши и появляются снова, если её сдвинуть.
    Esc или ✕ — закрыть, пробел — пауза, ← → — на 5 секунд. Адреса трейлера пробуются по очереди.
    """

    failed = Signal()  # ни один адрес не открылся
    closed = Signal()
    HIDE_CONTROLS_MS = 2500
    TOP_H, BOTTOM_H = 64, 84

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.sources, self.index, self.title = [], 0, ""
        self._drag_value = None  # куда тянут ползунок; перематываем, когда тянуть перестали
        self.stream_token = 0  # меняется при каждом новом адресе и закрытии — старая загрузка потока бросается
        self.on_hd, self.waiting_hd = False, False
        # 1080p, который готовится вторым проигрывателем, пока играет обычный mp4:
        # {"player", "audio", "item", "target" — с какого места он встанет, "ready", "tries"}
        self.hd = None
        # Быстрый старт: если обычных mp4 у трейлера нет, сначала качается самый лёгкий вариант потока —
        # за пару секунд, — и трейлер идёт им, пока готовится 1080p
        self.preview_state, self.on_preview = None, False
        self.hd_url, self.hd_state, self.hd_height, self.hd_percent = None, None, 0, 0
        self.group_len = 0
        self.relay = StreamRelay(self)
        self.relay.progress.connect(self._on_stream_progress)
        self.relay.ready.connect(self._on_stream_ready)
        self.relay.failed.connect(self._on_stream_failed)
        self.relay.preview.connect(self._on_preview)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.hide()

        # Экран загрузки: размытая картинка из шапки игры и полоса прогресса по центру.
        # load_target — сколько процентов скачано (None — сколько ждать, неизвестно, полоса «бегает»),
        # load_shown — сколько сейчас нарисовано: полоса догоняет цель плавно, а не скачками
        self.poster = QPixmap()
        self.loading = False
        self.load_text = ""
        self.load_target = None
        self.load_shown = 0.0
        self.load_started = 0.0
        self.load_timer = QTimer(self)
        self.load_timer.setInterval(16)
        self.load_timer.timeout.connect(self._tick_loading)
        self._size_logged = False

        self.view = VideoView(self)
        self.view.clicked.connect(self.toggle)
        self.view.moved.connect(self.show_controls)
        self.player, self.audio = self._make_player(self.view.item)

        # Под кнопками всегда тёмное видео, поэтому текст и значки — белые
        self.top_bar = OverlayBar(self, from_top=True)
        self.bottom_bar = OverlayBar(self, from_top=False)
        self.title_label = StrongBodyLabel("", self.top_bar)
        self.title_label.setTextColor(WHITE, WHITE)
        self.status_label = CaptionLabel("", self.top_bar)
        self.status_label.setTextColor(WHITE, WHITE)
        close_button = TransparentToolButton(FIF.CLOSE.icon(color=WHITE), self.top_bar)
        close_button.setToolTip("Закрыть (Esc). Пробел — пауза, ← → — на 5 секунд")
        close_button.clicked.connect(self.close_video)
        self.pause_button = TransparentToolButton(FIF.PAUSE.icon(color=WHITE), self.bottom_bar)
        self.pause_button.clicked.connect(self.toggle)
        self.slider = Slider(Qt.Horizontal, self.bottom_bar)
        self.time_label = CaptionLabel("0:00 / 0:00", self.bottom_bar)
        self.time_label.setTextColor(WHITE, WHITE)

        # Перемотка. Клик по полосе — сразу. Перетаскивание: пока тянут, двигаем только ползунок и время,
        # а перематываем через четверть секунды после последнего движения. Так перемотка срабатывает,
        # даже если Slider из Fluent-Widgets не присылает sliderReleased.
        if hasattr(self.slider, "clicked"):
            self.slider.clicked.connect(self.seek)
        self.slider.sliderMoved.connect(self._on_drag)
        self.slider.sliderReleased.connect(self._finish_drag)
        self.drag_timer = QTimer(self)
        self.drag_timer.setSingleShot(True)
        self.drag_timer.setInterval(250)
        self.drag_timer.timeout.connect(self._finish_drag)

        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(24, 10, 16, 0)
        top.addWidget(self.title_label, 1, Qt.AlignTop)
        top.addWidget(self.status_label, 0, Qt.AlignTop)
        top.addWidget(close_button, 0, Qt.AlignTop)
        bottom = QHBoxLayout(self.bottom_bar)
        bottom.setContentsMargins(24, 0, 24, 14)
        bottom.addWidget(self.pause_button, 0, Qt.AlignBottom)
        bottom.addWidget(self.slider, 1, Qt.AlignBottom)
        bottom.addWidget(self.time_label, 0, Qt.AlignBottom)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(self.HIDE_CONTROLS_MS)
        self.hide_timer.timeout.connect(self._auto_hide_controls)

        win.installEventFilter(self)  # растягиваемся вместе с окном

    # --- Кнопки поверх видео ---
    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self.view.setGeometry(0, 0, w, h)
        self.top_bar.setGeometry(0, 0, w, self.TOP_H)
        self.bottom_bar.setGeometry(0, h - self.BOTTOM_H, w, self.BOTTOM_H)
        self.top_bar.raise_()
        self.bottom_bar.raise_()

    def show_controls(self):
        self.top_bar.show()
        self.bottom_bar.show()
        self.view.viewport().unsetCursor()
        self.hide_timer.start()

    def _auto_hide_controls(self):
        # На паузе, во время загрузки и пока тянут ползунок кнопки не прячем
        bars_hovered = self.top_bar.underMouse() or self.bottom_bar.underMouse()
        if (self.player.playbackState() != QMediaPlayer.PlayingState or self.loading
                or bars_hovered or self._drag_value is not None):
            self.hide_timer.start()
            return
        self.top_bar.hide()
        self.bottom_bar.hide()
        self.view.viewport().setCursor(Qt.BlankCursor)

    def mouseMoveEvent(self, event):
        self.show_controls()
        super().mouseMoveEvent(event)

    # --- Проигрыватели ---
    def _make_player(self, item):
        """Проигрыватель для картинки item. Его сигналы слушаем, только пока он играет на экране
        (или, если это готовящийся 1080p, — своими обработчиками)."""
        player = QMediaPlayer(self)
        audio = QAudioOutput(self)
        audio.setVolume(0.8)
        player.setAudioOutput(audio)
        player.setVideoOutput(item)
        item.nativeSizeChanged.connect(lambda size: item is self.view.item and self._on_native_size(size))
        player.playbackStateChanged.connect(lambda state: player is self.player and self._on_state(state))
        player.mediaStatusChanged.connect(
            lambda status: self._route(player, self._on_status, self._on_hd_status, status))
        player.positionChanged.connect(
            lambda position: self._route(player, self._on_position, self._on_hd_position, position))
        player.durationChanged.connect(
            lambda duration: player is self.player and self.slider.setRange(0, max(duration, 0)))
        player.errorOccurred.connect(
            lambda error, message="": self._route(player, self._on_error, self._on_hd_error, error, message))
        return player, audio

    def _route(self, player, main_handler, hd_handler, *args):
        if player is self.player:
            main_handler(*args)
        elif self.hd and player is self.hd["player"]:
            hd_handler(*args)

    @staticmethod
    def _release(player, audio, item):
        """Остановить и удалить проигрыватель, который больше не показывается."""
        player.stop()
        player.setSource(QUrl())
        player.deleteLater()
        audio.deleteLater()
        item.deleteLater()

    def _drop_hd(self):
        """Бросить готовящийся 1080p (закрыли трейлер, открыли другой или файл не открылся)."""
        hd, self.hd = self.hd, None
        if hd:
            self.view.remove_item(hd["item"])
            QTimer.singleShot(0, lambda: self._release(hd["player"], hd["audio"], hd["item"]))

    def play(self, sources, title, poster=None):
        """Трейлер начинается сразу — обычным mp4 из Steam (он открывается за секунду-две, но мельче).

        Параллельно в фоне качается поток HLS в 1080p. Как только он собран в файл, второй проигрыватель
        невидимо открывает его чуть впереди текущего места, и в тот момент, когда трейлер туда доходит,
        картинка и звук переключаются на 1080p — без паузы. Уже скачанный 1080p открывается сразу.
        """
        sources = list(sources)
        self.title = title
        self.poster = poster if poster and not poster.isNull() else QPixmap()
        self.title_label.setText(f"Трейлер — {title}")
        self.status_label.setText("")
        self.stream_token += 1
        self._drop_hd()
        self.on_hd = False  # сейчас играет собранный 1080p
        self.preview_state, self.on_preview = None, False
        self.hd_url, self.hd_state, self.hd_height, self.hd_percent = None, None, 0, 0
        self.waiting_hd = False  # обычные mp4 не открылись — ждём 1080p на экране загрузки

        # Адреса идут трейлер за трейлером, у каждого HLS — перед его mp4 (см. core._movie_sources).
        # Если перед первым HLS стоит mp4 — Steam отдал этому трейлеру полноценный файл, поток не нужен.
        streams = [i for i, url in enumerate(sources) if self._is_stream(url)]
        if streams and streams[0] == 0:
            self.hd_url = sources[0]
            end = streams[1] if len(streams) > 1 else len(sources)
            group = sources[1:end]  # mp4 этого же трейлера — им и начинаем
            others = [url for url in sources[end:] if not self._is_stream(url)]  # другие трейлеры — на крайний случай
            self.sources, self.group_len = group + others, len(group)
        else:
            self.sources = [url for url in sources if not self._is_stream(url)]
            self.group_len = len(self.sources)
        self.index = 0

        self._fit()
        self._set_loading("Открываю трейлер…")
        self.show()
        self.raise_()
        self.setFocus()
        self.show_controls()
        self.win.music.set_ducked(True)

        cached = core.cached_trailer(self.hd_url) if self.hd_url else None
        if cached:
            self.hd_state = "ready"
            self._open_hd(cached)
            return
        if self.hd_url:
            self._fetch_stream(self.hd_url)
        self._try_current()

    @staticmethod
    def _is_stream(url):
        return ".m3u8" in url.split("?")[0]

    # --- Экран загрузки ---
    def _set_loading(self, text, percent=None):
        if not self.loading:
            self.loading = True
            self.load_shown = 0.0
            self.load_started = time.monotonic()
            self.view.hide()  # пока кадров нет, вместо чёрного окна — картинка игры
            self.load_timer.start()
        self.load_text = text
        if percent is not None and self.load_target is not None:
            percent = max(percent, self.load_target)  # полоса не откатывается назад
        self.load_target = percent
        self.update()

    def _stop_loading(self):
        if not self.loading:
            return
        self.loading = False
        self.load_target = None
        self.load_timer.stop()
        self.view.show()
        self.top_bar.raise_()
        self.bottom_bar.raise_()

    def _tick_loading(self):
        if self.load_target is not None:
            self.load_shown += (self.load_target - self.load_shown) * 0.12
        self.update()

    def _try_current(self):
        """Следующий обычный mp4. Кончились mp4 этого трейлера — ждём 1080p, а если и он не вышел — другие."""
        if self.index >= self.group_len and self.hd_state == "loading":
            self.waiting_hd = True
            self.on_preview = False
            self._set_loading(f"Загружаю трейлер {self.hd_height}p" if self.hd_height else "Загружаю трейлер",
                              self.hd_percent)
            if self.preview_state is None:
                self._fetch_preview(self.hd_url)  # обычных mp4 нет — начнём с лёгкого варианта потока
            return
        if self.index >= len(self.sources):
            self.close_video()
            self.failed.emit()
            return
        url = self.sources[self.index]
        core.log.info("Трейлер «%s», вариант %d из %d: %s", self.title, self.index + 1, len(self.sources), url)
        self._size_logged = False
        self.player.setSource(QUrl(url))
        self.player.play()

    def _fetch_stream(self, url):
        """Поток HLS Qt не открывает: скачиваем кусочки и склеиваем в mp4 в фоне, потом играем файл."""
        token = self.stream_token
        self.hd_state = "loading"
        core.log.info("Трейлер «%s»: в фоне качается 1080p: %s", self.title, url)

        def run():
            try:
                path = core.trailer_file(url, progress=lambda h, p: self.relay.progress.emit(token, h, p),
                                         cancelled=lambda: token != self.stream_token)
            except core.TrailerCancelled:
                return
            except Exception as e:  # noqa: BLE001 — не вышло с потоком, остаёмся на обычном mp4
                core.log.exception("Трейлер «%s»: поток не собрался", self.title)
                self.relay.failed.emit(token, str(e) or type(e).__name__)
                return
            self.relay.ready.emit(token, path)

        threading.Thread(target=run, daemon=True).start()

    def _fetch_preview(self, url):
        """Самый лёгкий вариант того же потока — им трейлер начнётся, пока качается 1080p."""
        token = self.stream_token
        self.preview_state = "loading"
        core.log.info("Трейлер «%s»: качаю быстрый старт в низком качестве", self.title)

        def run():
            try:
                path = core.trailer_file(url, cancelled=lambda: token != self.stream_token, preview=True)
            except core.TrailerCancelled:
                return
            except Exception:  # noqa: BLE001 — без быстрого старта просто ждём 1080p
                core.log.exception("Трейлер «%s»: быстрый старт не собрался", self.title)
                path = ""
            self.relay.preview.emit(token, path)

        threading.Thread(target=run, daemon=True).start()

    def _on_preview(self, token, path):
        if token != self.stream_token or not self.isVisible():
            return
        self.preview_state = "ready" if path else "failed"
        if not path or not self.waiting_hd or self.hd_state != "loading":
            return  # не собрался или 1080p уже готов — быстрый старт не нужен
        self.waiting_hd, self.on_preview = False, True
        self._size_logged = False
        core.log.info("Трейлер «%s»: начинаю с быстрого старта, 1080p подменит его без паузы", self.title)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

    def _on_stream_progress(self, token, height, percent):
        if token != self.stream_token or not self.isVisible():
            return
        self.hd_height, self.hd_percent = height, percent
        if self.waiting_hd:
            self._set_loading(f"Загружаю трейлер {height}p", percent)
        else:
            self.status_label.setText(f"{height}p загружается · {percent}%")  # видео уже идёт

    def _on_stream_ready(self, token, path):
        if token != self.stream_token or not self.isVisible():
            return
        self.hd_state = "ready"
        self.status_label.setText("")
        if not self.loading and not self.waiting_hd:
            self._prepare_hd(path)  # обычный mp4 уже идёт — подменяем его незаметно
        else:
            self._open_hd(path)  # на экране загрузки — сразу открываем 1080p

    def _open_hd(self, path):
        """Открыть собранный 1080p с начала, пока на экране загрузка."""
        self.on_hd, self.waiting_hd, self.on_preview = True, False, False
        self._size_logged = False
        core.log.info("Трейлер «%s»: открываю 1080p", self.title)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

    # --- Незаметное переключение на 1080p ---
    HD_LEAD_MS = 1200  # 1080p встаёт чуть впереди текущего места — успевает перемотаться

    def _prepare_hd(self, path):
        self._drop_hd()
        item = self.view.add_item(hidden=True)
        player, audio = self._make_player(item)
        self.hd = {"player": player, "audio": audio, "item": item, "target": None, "ready": False, "tries": 0}
        core.log.info("Трейлер «%s»: 1080p готов, готовлю переключение с %s", self.title,
                      format_ms(self.player.position()))
        player.setSource(QUrl.fromLocalFile(path))
        player.pause()  # открыть и показать кадр, но не играть

    def _aim_hd(self, playing=None):
        """Перемотать готовящийся 1080p туда, где трейлер окажется через мгновение (на паузе — туда, где он стоит)."""
        hd = self.hd
        if playing is None:
            playing = self.player.playbackState() == QMediaPlayer.PlayingState
        target = self.player.position() + (self.HD_LEAD_MS * (1 + hd["tries"]) if playing else 0)
        duration = hd["player"].duration()
        if duration > 0 and target > duration - 1000:
            core.log.info("Трейлер «%s»: почти досмотрен — остаюсь на обычном качестве", self.title)
            self._drop_hd()
            return
        hd["target"], hd["ready"] = target, False
        hd["player"].setPosition(target)
        hd["player"].pause()

    def _on_hd_status(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self.hd["target"] is None:
            self._aim_hd()

    def _on_hd_position(self, position):
        hd = self.hd
        if hd["target"] is None or abs(position - hd["target"]) > 1000:
            return
        hd["ready"] = True  # 1080p перемотался и ждёт на нужном кадре
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            self._switch_to_hd()  # на паузе — переключаем сразу, кадр тот же

    def _check_hd_switch(self, position):
        """Трейлер дошёл до места, где ждёт 1080p, — переключаемся. Не успел перемотаться — целимся дальше."""
        hd = self.hd
        if not hd or hd["target"] is None or position < hd["target"]:
            return
        if hd["ready"] or hd["tries"] >= 3:
            self._switch_to_hd()
        else:
            hd["tries"] += 1
            self._aim_hd(playing=True)

    def _switch_to_hd(self):
        hd, self.hd = self.hd, None
        old = (self.player, self.audio, self.view.item)
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        old[1].setVolume(0.0)  # старый звук смолкает в тот же миг, когда вступает новый
        self.player, self.audio = hd["player"], hd["audio"]
        if playing:
            self.player.play()
        self.view.set_current(hd["item"])
        self.on_hd, self.on_preview = True, False
        self.slider.setRange(0, max(self.player.duration(), 0))
        self._on_state(self.player.playbackState())
        core.log.info("Трейлер «%s»: переключился на 1080p на %s", self.title, format_ms(self.player.position()))
        QTimer.singleShot(0, lambda: self._release(*old))  # не останавливаем проигрыватель внутри его сигнала

    def _on_hd_error(self, error, message=""):
        core.log.warning("Трейлер «%s»: 1080p не открылся: %s — остаюсь на обычном качестве",
                         self.title, message or error)
        self.hd_state = "failed"
        self._drop_hd()

    def _on_stream_failed(self, token, message):
        if token != self.stream_token or not self.isVisible():
            return
        self.hd_state = "failed"
        self.status_label.setText("")
        if self.waiting_hd:  # обычные mp4 этого трейлера тоже не открылись — пробуем остальные
            self.waiting_hd = False
            self._try_current()

    def _on_error(self, error, message=""):
        if not self.isVisible():
            return
        if self.on_hd:
            # Собранный файл не открылся — возвращаемся к обычным mp4
            core.log.warning("Трейлер «%s»: 1080p не открылся: %s", self.title, message or error)
            self.on_hd, self.hd_state = False, "failed"
        elif self.on_preview:
            # Быстрый старт не открылся — просто ждём 1080p на экране загрузки
            core.log.warning("Трейлер «%s»: быстрый старт не открылся: %s", self.title, message or error)
            self.on_preview, self.preview_state = False, "failed"
        else:
            core.log.warning("Трейлер, вариант %d не открылся: %s", self.index + 1, message or error)
            self.index += 1
        QTimer.singleShot(0, self._try_current)  # не меняем источник прямо внутри сигнала плеера

    def _on_status(self, status):
        if status in (QMediaPlayer.BufferedMedia, QMediaPlayer.LoadedMedia):
            if self.loading:
                core.log.info("Трейлер «%s» открылся (%s) за %.1f с", self.title,
                              "1080p" if self.on_hd else "быстрый старт" if self.on_preview
                              else f"вариант {self.index + 1}",
                              time.monotonic() - self.load_started)
            self._stop_loading()
        elif status == QMediaPlayer.EndOfMedia:
            self.close_video()  # досмотрели — возвращаемся на страницу игры

    def _on_native_size(self, size):
        # Размер кадра известен только после первого кадра — пишем его в лог один раз на трейлер
        if not self._size_logged and not size.isEmpty():
            self._size_logged = True
            core.log.info("Трейлер «%s»: кадр %d×%d", self.title, round(size.width()), round(size.height()))
        full_hd = size.width() >= 1920 or size.height() >= 1080
        if full_hd and not self.on_hd and not self.on_preview and self.hd_state in ("loading", "ready"):
            # Обычный mp4 уже в 1080p — качать и подменять его тем же качеством из потока незачем
            core.log.info("Трейлер «%s»: mp4 уже в 1080p, загрузку потока отменяю", self.title)
            self.stream_token += 1  # фоновая загрузка увидит это и остановится
            self.hd_state = "skipped"
            self.status_label.setText("")
            self._drop_hd()

    def _on_state(self, state):
        icon = FIF.PAUSE if state == QMediaPlayer.PlayingState else FIF.PLAY
        self.pause_button.setIcon(icon.icon(color=WHITE))
        if state != QMediaPlayer.PlayingState:
            self.show_controls()  # на паузе кнопки видны всегда

    def _on_position(self, position):
        self._check_hd_switch(position)
        position = self.player.position()  # если только что переключились на 1080p — уже его время
        if self._drag_value is not None or self.slider.isSliderDown():
            return  # ползунок сейчас тянут — не перебиваем его
        self.slider.setValue(position)
        self.time_label.setText(f"{format_ms(position)} / {format_ms(self.player.duration())}")

    def _on_drag(self, value):
        self._drag_value = value
        self.time_label.setText(f"{format_ms(value)} / {format_ms(self.player.duration())}")
        self.drag_timer.start()  # перезапуск: перемотаем, когда ползунок замрёт
        self.show_controls()

    def _finish_drag(self):
        self.drag_timer.stop()
        if self._drag_value is None:
            return
        value, self._drag_value = self._drag_value, None
        self.seek(value)

    def toggle(self):
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        if playing:
            self.player.pause()
        else:
            self.player.play()
        if self.hd and self.hd["target"] is not None:
            self._aim_hd(playing=not playing)  # 1080p ждёт там, где трейлер теперь окажется

    def seek(self, position):
        duration = self.player.duration()
        if duration <= 0:
            return
        position = max(0, min(int(position), duration - 500))
        if not self.player.isSeekable():
            core.log.warning("Трейлер «%s»: проигрыватель говорит, что перемотка недоступна, пробую всё равно",
                             self.title)
        self.player.setPosition(position)
        self.slider.setValue(position)
        if self.hd and self.hd["target"] is not None:
            self.hd["tries"] = 0
            self._aim_hd()  # перемотали — 1080p перематывается следом
        self.setFocus()  # после клика по полосе Esc и пробел снова работают

    def close_video(self):
        if not self.isVisible():
            return
        self.stream_token += 1  # бросаем загрузку потока, если она ещё идёт
        self._drop_hd()
        self._stop_loading()
        self.hide_timer.stop()
        self.drag_timer.stop()
        self._drag_value = None
        self.player.stop()
        self.player.setSource(QUrl())  # отпускаем видеопоток
        self.view.viewport().unsetCursor()
        self.hide()
        self.win.music.set_ducked(False)
        self.closed.emit()

    def _fit(self):
        top = self.win.titleBar.height()  # заголовок окна оставляем: им двигают и закрывают окно
        self.setGeometry(0, top, self.win.width(), self.win.height() - top)

    def eventFilter(self, obj, event):
        if obj is self.win and event.type() == QEvent.Resize and self.isVisible():
            self._fit()
        return False

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close_video()
        elif event.key() == Qt.Key_Space:
            self.toggle()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right):
            step = 5000 if event.key() == Qt.Key_Right else -5000
            self.seek(self.player.position() + step)
            self.show_controls()
        else:
            super().keyPressEvent(event)

    LOAD_BAR_W, LOAD_BAR_H = 420, 6
    LOAD_FROM, LOAD_TO = QColor(124, 77, 255), QColor(0, 150, 255)  # цвета значка программы

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        if not self.loading:
            return  # дальше рисует само видео
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        t = time.monotonic() - self.load_started

        # Размытая картинка игры на всё окно, медленно и едва заметно приближается
        if not self.poster.isNull():
            zoom = 1.0 + min(t, 30) * 0.002
            scale = max(w / self.poster.width(), h / self.poster.height()) * zoom
            pw, ph = self.poster.width() * scale, self.poster.height() * scale
            painter.setOpacity(min(1.0, t / 0.4))  # мягко проявляется
            painter.drawPixmap(QRectF((w - pw) / 2, (h - ph) / 2, pw, ph), self.poster, QRectF(self.poster.rect()))
            painter.setOpacity(1.0)
        # Затемнение: в центре плотнее, чтобы надпись и полоса читались на любой картинке
        shade = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.7)
        shade.setColorAt(0, QColor(0, 0, 0, 150))
        shade.setColorAt(1, QColor(0, 0, 0, 90))
        painter.fillRect(self.rect(), shade)

        bar_w = min(self.LOAD_BAR_W, w * 0.6)
        x, y = (w - bar_w) / 2, h / 2

        title_font = resized_font(self.font(), 10)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(WHITE)
        painter.drawText(QRectF(0, y - 64, w, 40), Qt.AlignHCenter | Qt.AlignBottom,
                         QFontMetrics(title_font).elidedText(self.title, Qt.ElideRight, round(w * 0.8)))

        # Дорожка полосы и заполненная часть
        track = QRectF(x, y, bar_w, self.LOAD_BAR_H)
        radius = self.LOAD_BAR_H / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 45))
        painter.drawRoundedRect(track, radius, radius)
        if self.load_target is None:
            # Сколько ждать, неизвестно: по дорожке туда-сюда ходит отрезок
            seg = bar_w * 0.28
            phase = (math.sin(t * 2.6) + 1) / 2
            fill = QRectF(x + (bar_w - seg) * phase, y, seg, self.LOAD_BAR_H)
        else:
            fill = QRectF(x, y, max(self.LOAD_BAR_H, bar_w * self.load_shown / 100), self.LOAD_BAR_H)
        gradient = QLinearGradient(x, 0, x + bar_w, 0)
        gradient.setColorAt(0, self.LOAD_FROM)
        gradient.setColorAt(1, self.LOAD_TO)
        # Мягкое свечение под полосой
        glow = QColor(self.LOAD_TO)
        glow.setAlpha(60)
        painter.setBrush(glow)
        painter.drawRoundedRect(fill.adjusted(-2, -3, 2, 3), radius + 3, radius + 3)
        painter.setBrush(gradient)
        painter.drawRoundedRect(fill, radius, radius)

        caption_font = resized_font(self.font(), 0)
        painter.setFont(caption_font)
        painter.setPen(QColor(255, 255, 255, 200))
        caption = self.load_text
        if self.load_target is not None:
            caption += f" · {round(self.load_shown)}%"
        painter.drawText(QRectF(0, y + 18, w, 24), Qt.AlignHCenter | Qt.AlignTop, caption)


def resized_font(font, delta_px):
    """Шрифт крупнее или мельче на delta_px пикселей. Fluent задаёт размеры в пикселях, Qt по умолчанию — в пунктах."""
    font = QFont(font)
    if font.pixelSize() > 0:
        font.setPixelSize(max(font.pixelSize() + delta_px, 9))
    else:
        font.setPointSizeF(max(font.pointSizeF() + delta_px * 0.75, 7))
    return font


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


class CenteredFlowLayout(QLayout):
    """Элементы в ряд по центру; не помещаются по ширине — переносятся на следующую строку,
    и каждая строка тоже выравнивается по центру. Скрытые элементы пропускаются."""

    def __init__(self, parent=None, spacing=10):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect, apply):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        rows, row, row_width = [], [], 0
        for item in self._items:
            if item.isEmpty():
                continue
            w = min(item.sizeHint().width(), area.width())
            needed = w if not row else row_width + self._spacing + w
            if row and needed > area.width():
                rows.append((row, row_width))
                row, row_width = [], 0
                needed = w
            row.append((item, w))
            row_width = needed
        if row:
            rows.append((row, row_width))

        y = area.y()
        for i, (row, row_width) in enumerate(rows):
            height = max(item.sizeHint().height() for item, _ in row)
            x = area.x() + (area.width() - row_width) // 2
            if apply:
                for item, w in row:
                    h = item.sizeHint().height()
                    item.setGeometry(QRect(x, y + (height - h) // 2, w, h))
                    x += w + self._spacing
            y += height + (self._spacing if i < len(rows) - 1 else 0)
        return y - rect.y() + m.bottom()


def centered(widget, max_width):
    """Строка, в которой виджет не шире max_width и стоит по центру; в узком окне — на всю ширину."""
    widget.setMaximumWidth(max_width)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    row.addWidget(widget, 100)
    row.addStretch(1)
    return row


class InfoChip(QWidget):
    """Плашка с подписью и значением: «Жанры / Экшен, Приключения».
    Со ссылкой — нажимается, подсвечивается при наведении и помечена стрелкой ↗.
    Куда открыть ссылку, решает страница: сигнал linkClicked."""

    linkClicked = Signal(str)
    MAX_WIDTH = 380
    PAD_X, PAD_Y = 16, 9

    def __init__(self, caption, value, url=None, parent=None):
        super().__init__(parent)
        self.caption, self.value, self.url = caption, value, url
        self.hover = False
        self.caption_font = resized_font(self.font(), -2)
        self.value_font = QFont(self.font())
        self.value_font.setBold(True)
        if url:
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip(url if len(url) < 120 else url[:117] + "…")

    def _value_text(self):
        return self.value + ("  ↗" if self.url else "")

    def sizeHint(self):
        cap, val = QFontMetrics(self.caption_font), QFontMetrics(self.value_font)
        width = max(cap.horizontalAdvance(self.caption), val.horizontalAdvance(self._value_text())) + 2 * self.PAD_X
        return QSize(min(width + 2, self.MAX_WIDTH), cap.height() + val.height() + 2 * self.PAD_Y + 2)

    def minimumSizeHint(self):
        return QSize(80, self.sizeHint().height())

    def enterEvent(self, event):
        self.hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if self.url and event.button() == Qt.LeftButton:
            self.linkClicked.emit(self.url)
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        active = bool(self.url) and self.hover
        fill = QColor(255, 255, 255, 30 if active else 18) if dark else QColor(255, 255, 255, 200 if active else 150)
        border = themeColor() if active else (QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 30))
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 10, 10)

        width = self.width() - 2 * self.PAD_X
        cap, val = QFontMetrics(self.caption_font), QFontMetrics(self.value_font)
        painter.setFont(self.caption_font)
        painter.setPen(QColor(255, 255, 255, 150) if dark else QColor(0, 0, 0, 140))
        painter.drawText(self.PAD_X, self.PAD_Y + cap.ascent(), cap.elidedText(self.caption, Qt.ElideRight, width))
        painter.setFont(self.value_font)
        painter.setPen(themeColor() if self.url else (WHITE if dark else QColor(0, 0, 0)))
        text = val.elidedText(self._value_text(), Qt.ElideRight, width)
        if text != self._value_text() and not self.toolTip():
            self.setToolTip(self.value)  # не влезло — полное значение во всплывающей подсказке
        painter.drawText(self.PAD_X, self.PAD_Y + cap.height() + 2 + val.ascent(), text)


class Spoiler(QWidget):
    """Раскрывающийся блок: заголовок-кнопка «▸ Название», по клику показывает содержимое."""

    toggled = Signal(bool)

    def __init__(self, title, parent=None, align=Qt.AlignLeft):
        super().__init__(parent)
        self.title = title
        self.expanded = False
        self.button = TransparentPushButton("", self)
        self.button.clicked.connect(self.toggle)
        self.body = QWidget(self)
        self.body.hide()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 4, 0, 4)
        self.body_layout.setSpacing(8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.button, 0, align)
        layout.addWidget(self.body)
        self._update_button()

    def set_title(self, title):
        self.title = title
        self._update_button()

    def set_expanded(self, expanded):
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self.body.setVisible(expanded)
        self._update_button()
        self.toggled.emit(expanded)

    def toggle(self):
        self.set_expanded(not self.expanded)

    def _update_button(self):
        self.button.setText(("▾  " if self.expanded else "▸  ") + self.title)


class AchievementRow(QWidget):
    """Одно достижение: значок, название, описание, доля игроков и отметка, если получено."""

    ICON = 48

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.icon = QLabel(self)
        self.icon.setFixedSize(self.ICON, self.ICON)
        self.icon.setStyleSheet("background: rgba(128,128,128,40); border-radius: 6px")
        name = StrongBodyLabel(item["name"], self)
        if item["description"]:
            text = item["description"]
        elif item["hidden"]:
            text = "Скрытое достижение — описание откроется, когда вы его получите"
        else:
            text = ""
        description = CaptionLabel(text, self)
        description.setWordWrap(True)

        status = []
        if item["unlocked"]:
            status.append("✓ Получено" + (f" {time.strftime('%d.%m.%Y', time.localtime(item['unlocked_at']))}"
                                          if item["unlocked_at"] else ""))
        if item["percent"] is not None:
            status.append(f"есть у {item['percent']:.1f}% игроков".replace(".", ","))
        status_label = CaptionLabel(" · ".join(status), self)
        if item["unlocked"]:
            status_label.setTextColor(themeColor(), themeColor())

        text_column = QVBoxLayout()
        text_column.setSpacing(0)
        text_column.addWidget(name)
        if text:
            text_column.addWidget(description)
        text_column.addWidget(status_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.icon, 0, Qt.AlignTop)
        layout.addLayout(text_column, 1)

    def set_pixmap(self, pixmap):
        self.icon.setStyleSheet("")
        self.icon.setPixmap(pixmap.scaled(self.ICON, self.ICON, Qt.KeepAspectRatio, Qt.SmoothTransformation))


# Русские издания с обзорами: прямой адрес обзора узнать нельзя, поэтому ищем его поиском по сайту
REVIEW_SITES = (("StopGame", "stopgame.ru"), ("Игромания", "igromania.ru"), ("Zone of Games", "zoneofgames.ru"))


class GamePage(QWidget):
    """Страница выпавшей игры: шапка с фоном и логотипом, кнопки, скриншоты и трейлеры, описание.

    Всё, что качается из интернета, загружается в фоне. Если за это время открыли другую игру,
    опоздавшие ответы отбрасываются по номеру открытия (_token).
    """

    CHIP_ORDER = ("genres", "release", "developers", "reviews", "metacritic", "achievements")
    SIDE_MARGIN = 32  # как отступ логотипа в шапке
    CONTENT_WIDTH = 1400  # колонка страницы на очень широком экране
    TEXT_WIDTH = 900  # описание: длинные строки читать тяжело
    ACHIEVEMENT_WIDTH = 420  # ширина колонки достижений — по ней решаем, сколько колонок влезает
    MAX_ACHIEVEMENTS = 300  # у некоторых игр их тысячи — столько строк окно не потянет

    def __init__(self, home):
        super().__init__()
        self.home, self.win = home, home.win
        self.game = None
        self.media = []  # скриншоты: [{"thumb": адрес превью, "full": адрес}]
        self.thumbs = []
        self.trailer_sources = []  # адреса трейлеров по порядку попыток
        self.achievements = None
        self.icon_rows = {}  # адрес значка достижения → строки, которые его ждут
        self.pixmaps = {}  # адрес → QPixmap
        self._token = 0
        self._wanted = None  # какой большой скриншот сейчас ждём
        self.overlay = None  # окно трейлера создаётся при первом трейлере
        self.backdrop = None  # размытый фон игры под всей страницей
        self.backdrop_opacity = 0.0
        self.backdrop_anim = QVariantAnimation(self)
        self.backdrop_anim.setDuration(900)
        self.backdrop_anim.setStartValue(0.0)
        self.backdrop_anim.setEndValue(1.0)
        self.backdrop_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.backdrop_anim.valueChanged.connect(self._set_backdrop_opacity)

        self.loader = ImageLoader(self)
        self.loader.loaded.connect(self._on_image)

        self.header = HeroHeader(self)
        self.header.playClicked.connect(self.play_trailer)
        self.header.liveRequested.connect(self._download_live)
        self._live_url = None  # адрес живого фона открытой игры (если он есть и ещё не скачан)

        self.play_button = PrimaryPushButton("Играть", self, FIF.PLAY)
        self.play_button.clicked.connect(self.play)
        self.spin_button = PushButton("Крутить ещё", self, FIF.SYNC)
        self.spin_button.clicked.connect(self.home.spin_again)
        self.done_button = PushButton("Пройдена", self, FIF.ACCEPT)
        self.done_button.clicked.connect(self.home.mark_done)
        self.cover_button = PushButton("Своя обложка", self, FIF.PHOTO)
        self.cover_button.clicked.connect(self.home.choose_cover)
        self.store_button = PushButton("Магазин Steam", self, FIF.SHOPPING_CART)
        self.store_button.clicked.connect(self.open_store)
        self.soundtrack_button = PushButton("Слушать саундтрек", self, FIF.MUSIC)
        self.soundtrack_button.clicked.connect(self.listen_soundtrack)
        self.soundtrack_button.hide()
        back_button = TransparentPushButton("К рулетке", self, FIF.LEFT_ARROW)
        back_button.clicked.connect(self.home.back_to_reel)

        # Кнопки по центру; в узком окне переносятся на вторую строку
        buttons_box = QWidget(self)
        buttons = CenteredFlowLayout(buttons_box, spacing=12)
        for button in (self.play_button, self.spin_button, self.done_button, self.cover_button, self.store_button,
                       self.soundtrack_button, back_button):
            buttons.addWidget(button)
        self.buttons = buttons

        # Скриншот на всю ширину колонки; трейлер — кнопкой ▶ в шапке, поверх всего окна
        self.media_view = MediaView(self)
        self.media_view.setMinimumHeight(200)

        # Лента превью по центру; не помещается — прокручивается вбок
        self.strip = QWidget()
        self.strip_layout = QHBoxLayout(self.strip)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(6)
        self.strip_layout.addStretch(1)
        self.strip_layout.addStretch(1)  # превью встают между двумя растяжками
        self.strip_scroll = ScrollArea(self)
        self.strip_scroll.setWidget(self.strip)
        self.strip_scroll.setWidgetResizable(True)
        self.strip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.strip_scroll.setFixedHeight(Thumb.H + 6 + 14)  # превью и горизонтальная полоса прокрутки
        self.strip_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")

        # Жанры, дата, разработчик, оценки и обзоры — плашками по центру, со ссылками нажимаются
        self.chips_box = QWidget(self)
        self.chips = CenteredFlowLayout(self.chips_box, spacing=10)
        self.chip_values = {}  # ключ → (подпись, значение, ссылка)
        self.press_links = []

        # Описание: колонка по центру, не шире удобной для чтения строки
        self.about_box = QWidget(self)
        about = QVBoxLayout(self.about_box)
        about.setContentsMargins(0, 0, 0, 0)
        about.setSpacing(8)
        self.about_title = SubtitleLabel("Об игре", self.about_box)
        self.description = BodyLabel("", self.about_box)
        self.description.setWordWrap(True)
        self.description.setFont(resized_font(self.description.font(), 2))  # краткое описание — как вступление
        self.about_text = BodyLabel("", self.about_box)
        self.about_text.setWordWrap(True)
        self.about_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.translated_note = CaptionLabel("Описание переведено автоматически", self.about_box)
        self.translated_note.hide()
        self.players_title = SubtitleLabel("Что пишут игроки", self.about_box)
        self.players_title.hide()
        self.players_box = QVBoxLayout()
        self.players_box.setSpacing(6)
        about.addWidget(self.about_title)
        about.addWidget(self.description)
        about.addWidget(self.about_text)
        about.addWidget(self.translated_note)
        about.addSpacing(12)
        about.addWidget(self.players_title)
        about.addLayout(self.players_box)

        # Достижения — по центру, под спойлером
        self.achievements_box = Spoiler("Достижения", self, align=Qt.AlignHCenter)
        self.achievements_box.toggled.connect(self._on_achievements_toggled)
        self.achievements_progress = ProgressBar(self.achievements_box.body)
        self.achievements_progress.setFixedWidth(320)
        self.achievements_note = CaptionLabel("", self.achievements_box.body)
        self.achievements_note.setWordWrap(True)
        self.achievements_note.setAlignment(Qt.AlignHCenter)
        self.achievements_list = QGridLayout()
        self.achievements_list.setHorizontalSpacing(32)
        self.achievements_list.setVerticalSpacing(14)
        self.achievement_rows = []
        self.achievement_columns = 0
        self.achievements_box.body_layout.addWidget(self.achievements_progress, 0, Qt.AlignHCenter)
        self.achievements_box.body_layout.addWidget(self.achievements_note)
        self.achievements_box.body_layout.addSpacing(6)
        self.achievements_box.body_layout.addLayout(self.achievements_list)

        # Шапка во всю ширину, всё остальное — одной колонкой по центру с отступами по бокам.
        # Колонка не шире CONTENT_WIDTH, текст — не шире TEXT_WIDTH, чтобы строки не растягивались.
        column = QWidget(self)
        inner = QVBoxLayout(column)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(16)
        inner.addWidget(buttons_box)
        inner.addWidget(self.media_view)
        inner.addWidget(self.strip_scroll)
        inner.addWidget(self.chips_box)
        inner.addSpacing(8)
        inner.addLayout(centered(self.about_box, self.TEXT_WIDTH))
        inner.addSpacing(8)
        inner.addWidget(self.achievements_box)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 24)  # полоса прокрутки Fluent рисуется поверх, место под неё не нужно
        layout.setSpacing(16)
        layout.addWidget(self.header)
        sides = centered(column, self.CONTENT_WIDTH)
        sides.setContentsMargins(self.SIDE_MARGIN, 0, self.SIDE_MARGIN, 0)
        layout.addLayout(sides)
        layout.addStretch(1)
        self.scroll = ScrollArea(self)
        self.scroll.setWidget(content)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

    # --- Открытие игры ---
    def show_game(self, game):
        self.stop_video()
        self._token += 1
        self.game = game
        self._live_url = None
        # Замер: когда страница на экране, когда пришли фон и описание (по ним видно, что ждёт пользователь)
        self.timing = Timing(f"карточка «{game['name']}»")
        self._waiting = {"фон", "описание"} if game.get("appid") else {"фон"}
        token = self._token
        QTimer.singleShot(20000, lambda: token == self._token and self.timing.done("не всё пришло за 20 с"))
        self.media, self._wanted = [], None
        self.pixmaps.clear()
        self.loader.clear()
        for thumb in self.thumbs:
            self.strip_layout.removeWidget(thumb)  # сразу, иначе новые превью встанут вперемешку со старыми
            thumb.deleteLater()
        self.thumbs = []
        self.trailer_sources = []
        self.header.set_game(game["name"])
        self.backdrop = None
        self.backdrop_anim.stop()
        self.update()
        self.translated_note.hide()
        self.about_text.hide()
        self.players_title.hide()
        clear_layout(self.players_box)
        self.chip_values = {}
        self._rebuild_chips()
        self._reset_achievements()
        self.update_buttons()
        self.scroll.verticalScrollBar().setValue(0)

        key = core.read_text(core.API_KEY_FILE)
        for kind in core.ART_KINDS:
            self._run(core.get_artwork, (dict(game), kind, key), lambda path, k=kind: self._on_art(k, path))
        self._run(core.find_animated_hero, (dict(game), key), self._on_animated_hero)

        if game.get("appid"):
            self.about_title.show()
            self.description.setText("Загружаю описание из Steam…")
            self.media_view.show_text("Загружаю скриншоты…")
            self._run(core.get_game_details, (game["appid"],), self._on_details, self._on_details_failed)
            # Отзывы — отдельно от описания: меняются каждый день и не должны задерживать страницу
            self._run(core.get_reviews, (game["appid"],), self._on_reviews,
                      lambda message: core.log.warning("Отзывы Steam не загрузились: %s", message))
            settings = core.load_settings()
            self._run(core.get_achievements, (game["appid"], settings["steam_api_key"], settings["steam_id"]),
                      self._on_achievements, self._on_achievements_failed)
        else:
            self._show_without_details("Игра добавлена вручную, без Steam ID, — описания и скриншотов "
                                       "из магазина Steam для неё нет.")
        timing = self.timing
        QTimer.singleShot(0, lambda: timing.mark("на экране"))  # сработает, когда страница нарисована

    def _timing_mark(self, label):
        """Этап карточки готов; пришло всё, чего ждали, — замер уходит в лог."""
        if label not in self._waiting:
            return
        self._waiting.discard(label)
        self.timing.mark(label)
        if not self._waiting:
            self.timing.done()

    def _run(self, fn, args, on_done, on_fail=None):
        """Фоновая задача; ответ принимается, только если за это время не открыли другую игру."""
        token = self._token
        worker = Worker(fn, *args, parent=self)
        worker.finished.connect(lambda result: token == self._token and on_done(result))
        if on_fail:
            worker.failed.connect(lambda message: token == self._token and on_fail(message))
        worker.start()

    def _on_art(self, kind, path):
        if kind == "hero":
            self._timing_mark("фон")
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            return
        if kind == "hero":
            self.header.set_hero(pixmap)
            self._run(core.make_backdrop, (path,), self._on_backdrop)
        else:
            self.header.set_logo(pixmap)

    def _on_animated_hero(self, info):
        """У игры есть живой фон в SteamGridDB — в шапке появляется значок. Качается он по нажатию."""
        if not info:
            return
        self._live_url = info["url"]
        if not self.header.show_live(info["path"]):
            core.log.warning("Живой фон %s не открылся (нет поддержки WebP в Qt?) — значок не показываю",
                             info["path"])

    def _download_live(self):
        """Нажали значок живого фона — качаем его в фоне, ход загрузки рисуется кольцом на значке."""
        token, game, url = self._token, dict(self.game), self._live_url
        if not url:
            self.header.live_failed()
            return
        relay = ProgressRelay(self)
        relay.progress.connect(lambda _text, percent: token == self._token and self.header.set_live_progress(percent))
        worker = Worker(core.download_animated_hero, game, url, lambda percent: relay.report("", percent),
                        lambda: token != self._token, parent=self)
        worker.finished.connect(lambda path: token == self._token and self._on_live_downloaded(path))
        worker.failed.connect(lambda message: token == self._token and self._on_live_failed(message))
        worker.finished.connect(relay.deleteLater)
        worker.failed.connect(relay.deleteLater)
        worker.start()

    def _on_live_downloaded(self, path):
        if not path:
            self.header.live_failed()
        elif not self.header.live_ready(path):
            core.log.warning("Живой фон %s не открылся (нет поддержки WebP в Qt?)", path)
            notify("warning", self.win, "Живой фон не открылся", "Остаётся обычный фон.")

    def _on_live_failed(self, message):
        self.header.live_failed()
        notify("warning", self.win, "Не удалось скачать живой фон", message)

    # --- Размытая подложка под всей страницей ---
    def _on_backdrop(self, path):
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            return
        self.backdrop = pixmap
        self.backdrop_anim.stop()
        self.backdrop_anim.start()

    def _set_backdrop_opacity(self, value):
        self.backdrop_opacity = value
        self.update()

    def paintEvent(self, event):
        if not self.backdrop:
            return
        painter = QPainter(self)
        # Подложка не прокручивается вместе со страницей и заполняет её целиком. Растянутую копию храним:
        # иначе каждый кадр живого фона в шапке заново растягивал бы картинку на всю страницу
        w, h = self.width(), self.height()
        key = (w, h, self.backdrop.cacheKey())
        if getattr(self, "_backdrop_key", None) != key:
            scale = max(w / self.backdrop.width(), h / self.backdrop.height())
            self._backdrop_scaled = self.backdrop.scaled(max(1, round(self.backdrop.width() * scale)),
                                                         max(1, round(self.backdrop.height() * scale)),
                                                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._backdrop_key = key
        scaled = self._backdrop_scaled
        painter.setOpacity(self.backdrop_opacity * (0.55 if isDarkTheme() else 0.35))
        painter.drawPixmap((w - scaled.width()) // 2, (h - scaled.height()) // 2, scaled)

    def _show_without_details(self, text):
        self.chip_values = {}
        self._rebuild_chips()
        self.about_title.hide()
        self.about_text.hide()
        self.description.setText(text)
        cover = core.find_cover(self.game["name"])
        pixmap = QPixmap(cover) if cover else QPixmap()
        if pixmap.isNull():
            self.media_view.show_text("Нет картинок")
        else:
            self.media_view.show_pixmap(pixmap)

    def _on_details(self, details):
        self._timing_mark("описание")
        if not details:
            self._show_without_details("Магазин Steam не отдаёт страницу этой игры — возможно, её сняли с продажи.")
            return
        release = details["release_date"] + (" (ещё не вышла)" if details["coming_soon"] else "")
        values = {
            "genres": ("Жанры", ", ".join(details["genres"]), None),
            "release": ("Дата выхода", release, None),
            "developers": ("Разработчик", ", ".join(details["developers"]), None),
        }
        if details["metacritic"]:
            values["metacritic"] = ("Metacritic", f"{details['metacritic']} из 100", details.get("metacritic_url"))
        self.chip_values.update({k: v for k, v in values.items() if v[1]})
        # Обзоры русских изданий: прямой адрес обзора узнать нельзя — ищем его по сайту издания
        name = self.game["name"]
        self.press_links = [(title, f"https://www.google.com/search?q={quote_plus(f'site:{site} {name} обзор')}")
                            for title, site in REVIEW_SITES]
        self._rebuild_chips()

        self.description.setText(details["description"] or "Steam не прислал описание.")
        about = details.get("about") or ""
        self.about_text.setText(about)
        self.about_text.setVisible(bool(about))
        self.translated_note.setText("Описание переведено автоматически")
        self.translated_note.setVisible(bool(details.get("translated")))
        if core.needs_translation(details):
            # Сначала показываем оригинал, перевод подставим, когда переводчик ответит
            self.translated_note.setText("Перевожу описание на русский…")
            self.translated_note.show()
            self._run(core.translate_details, (details,), self._on_translated,
                      lambda message: self.translated_note.hide())

        # Трейлеры — кнопкой ▶ в шапке: все адреса всех трейлеров по очереди, первый открывшийся играет
        self.trailer_sources = [url for movie in details["movies"]
                                for url in (movie.get("sources") or [movie.get("url")]) if url]
        self.header.set_trailer(bool(self.trailer_sources))

        self.media = [{"thumb": s["thumb"], "full": s["full"]} for s in details["screenshots"]]
        for i, item in enumerate(self.media):
            thumb = Thumb(False, self.strip)
            thumb.clicked.connect(lambda i=i: self.select(i))
            self.strip_layout.insertWidget(i + 1, thumb)  # после левой растяжки
            self.thumbs.append(thumb)
            if item["thumb"]:
                self.loader.request(item["thumb"])
        self.strip_scroll.setVisible(len(self.media) > 1)  # один скриншот — лента не нужна
        if self.media:
            self.select(0)
        else:
            self.media_view.show_text("У игры нет скриншотов в магазине Steam")

    def _on_translated(self, details):
        """Перевод описания пришёл — подменяем оригинал. None — переводчик не ответил, остаётся оригинал."""
        if not details:
            self.translated_note.hide()
            return
        self.description.setText(details["description"] or "Steam не прислал описание.")
        about = details.get("about") or ""
        self.about_text.setText(about)
        self.about_text.setVisible(bool(about))
        self.translated_note.setText("Описание переведено автоматически")
        self.translated_note.show()

    # --- Плашки ---
    def _rebuild_chips(self):
        clear_layout(self.chips)
        chips = []
        for key in self.CHIP_ORDER:
            if key in self.chip_values:
                caption, value, url = self.chip_values[key]
                chips.append(InfoChip(caption, value, url, self.chips_box))
        if self.chip_values:  # обзоры изданий — только у игр из магазина
            for title, url in self.press_links:
                chips.append(InfoChip("Обзор", title, url, self.chips_box))
        for chip in chips:
            chip.linkClicked.connect(self.home.open_link)
            self.chips.addWidget(chip)
        self.chips_box.setVisible(bool(self.chip_values))
        self.chips.invalidate()

    # --- Отзывы игроков ---
    def _on_reviews(self, reviews):
        if not reviews:
            return
        core.log.info("Отзывы Steam «%s»: %s, всего %s, цитат %d", self.game["name"], reviews.get("score") or "—",
                      reviews.get("total"), len(reviews.get("excerpts") or []))
        if reviews.get("total"):
            percent = round(100 * reviews["positive"] / reviews["total"])
            count = f"{reviews['total']:,}".replace(",", " ")
            value = f"{reviews['score']} · {percent}% из {count}" if reviews.get("score") else f"{percent}% из {count}"
            url = f"https://store.steampowered.com/app/{self.game['appid']}/#app_reviews_hash"
            self.chip_values["reviews"] = ("Отзывы в Steam", value, url)
            if "genres" in self.chip_values or "release" in self.chip_values:
                self._rebuild_chips()  # иначе плашки появятся вместе с описанием
        self._show_player_reviews(reviews.get("excerpts") or [])

    def _show_player_reviews(self, excerpts):
        clear_layout(self.players_box)
        self.players_title.setVisible(bool(excerpts))
        for review in excerpts:
            verdict = "👍 Рекомендует" if review["up"] else "👎 Не рекомендует"
            meta = f"{verdict} · {review['hours']:g} ч в игре".replace(".", ",")
            if review.get("translated"):
                meta += " · переведено"
            caption = CaptionLabel(meta, self.about_box)
            text = BodyLabel(f"«{review['text']}»", self.about_box)
            text.setWordWrap(True)
            self.players_box.addWidget(caption)
            self.players_box.addWidget(text)
            self.players_box.addSpacing(10)

    # --- Достижения ---
    def _reset_achievements(self):
        self.achievements = None
        self.icon_rows = {}
        self.achievement_rows = []
        self.achievement_columns = 0
        clear_layout(self.achievements_list)
        self.achievements_box.set_expanded(False)
        self.achievements_box.hide()

    def _on_achievements(self, data):
        if not data:
            return  # у игры нет достижений — блока нет
        self.achievements_box.show()
        if data.get("error"):
            self.achievements_box.set_title("Достижения")
            self.achievements_progress.hide()
            self.achievements_note.setText(data["error"])
            return
        self.achievements = data
        total, unlocked = len(data["items"]), data["unlocked"]
        self.chip_values["achievements"] = ("Достижения", f"{unlocked} из {total}" if unlocked is not None
                                            else str(total), None)
        if "genres" in self.chip_values or "release" in self.chip_values:
            self._rebuild_chips()
        if unlocked is None:
            self.achievements_box.set_title(f"Достижения — {total}")
            self.achievements_progress.hide()
            self.achievements_note.setText(data.get("progress_note") or "Steam не отдал ваш прогресс в этой игре.")
        else:
            self.achievements_box.set_title(f"Достижения — получено {unlocked} из {total}")
            self.achievements_progress.show()
            self.achievements_progress.setRange(0, total)
            self.achievements_progress.setValue(unlocked)
            self.achievements_note.setText("")
        if total > self.MAX_ACHIEVEMENTS:
            self.achievements_note.setText((self.achievements_note.text() + " " if self.achievements_note.text()
                                            else "") + f"Показаны первые {self.MAX_ACHIEVEMENTS}.")
        self.achievements_note.setVisible(bool(self.achievements_note.text()))
        if self.achievements_box.expanded:
            self._build_achievements()

    def _on_achievements_failed(self, message):
        core.log.warning("Достижения для «%s» не загрузились: %s", self.game["name"], message)

    def _on_achievements_toggled(self, expanded):
        if expanded and self.achievements and not self.achievement_rows:
            self._build_achievements()

    def _build_achievements(self):
        """Строки создаются при первом раскрытии блока — так страница открывается быстрее."""
        clear_layout(self.achievements_list)
        self.icon_rows = {}
        self.achievement_rows = []
        for item in self.achievements["items"][:self.MAX_ACHIEVEMENTS]:
            row = AchievementRow(item, self.achievements_box.body)
            self.achievement_rows.append(row)
            url = item["icon"] if item["unlocked"] else item["icon_gray"]
            if url:
                self.icon_rows.setdefault(url, []).append(row)
                if url in self.pixmaps:
                    row.set_pixmap(self.pixmaps[url])
                else:
                    self.loader.request(url)
        self.achievement_columns = 0
        self._place_achievements()

    def _place_achievements(self):
        """Сколько колонок влезает в ширину блока — столько и делаем; при смене размера окна перекладываем."""
        if not self.achievement_rows:
            return
        width = self.achievements_box.width() or self.width()
        columns = max(1, min(3, width // self.ACHIEVEMENT_WIDTH))
        if columns == self.achievement_columns:
            return
        self.achievement_columns = columns
        for row in self.achievement_rows:
            self.achievements_list.removeWidget(row)
        for c in range(3):
            self.achievements_list.setColumnStretch(c, 1 if c < columns else 0)
        for i, row in enumerate(self.achievement_rows):
            self.achievements_list.addWidget(row, i // columns, i % columns)

    def _on_details_failed(self, message):
        self._timing_mark("описание")
        self._show_without_details(f"Не удалось загрузить страницу из Steam: {message}")

    # --- Скриншоты ---
    def select(self, index):
        item = self.media[index]
        for i, thumb in enumerate(self.thumbs):
            thumb.set_selected(i == index)
        self._wanted = item["full"]
        if item["full"] in self.pixmaps:
            self.media_view.show_pixmap(self.pixmaps[item["full"]])
            return
        preview = self.pixmaps.get(item["thumb"])  # пока качается большая картинка — показываем превью
        if preview:
            self.media_view.show_pixmap(preview)
        else:
            self.media_view.show_text("Загружаю…")
        self.loader.request(item["full"], front=True)

    def _on_image(self, url, content):
        pixmap = QPixmap()
        if not content or not pixmap.loadFromData(content):
            return
        self.pixmaps[url] = pixmap
        for item, thumb in zip(self.media, self.thumbs):
            if item["thumb"] == url:
                thumb.set_pixmap(pixmap)
        if url == self._wanted:
            self.media_view.show_pixmap(pixmap)
        for row in self.icon_rows.get(url, []):
            row.set_pixmap(pixmap)

    # --- Трейлер ---
    def play_trailer(self):
        if self.trailer_sources:
            self.play_video(self.trailer_sources)

    def play_video(self, sources):
        if self.overlay is None:
            self.overlay = VideoOverlay(self.win)
            self.overlay.failed.connect(self._on_video_failed)
            self.overlay.closed.connect(lambda: self.header.hold_movie(False))
        self.header.hold_movie(True)  # под трейлером живой фон не нужен — не отнимаем у видео время
        self.overlay.play(sources, self.game["name"], self.backdrop)

    def stop_video(self):
        if self.overlay:
            self.overlay.close_video()

    def _on_video_failed(self):
        notify("warning", self.win, "Трейлер не воспроизводится",
               "Проигрыватель не открыл видео из Steam. Трейлер можно посмотреть в магазине Steam.")

    # --- Кнопки ---
    def update_buttons(self):
        appid = self.game.get("appid") if self.game else None
        installed = bool(appid) and core.is_installed(appid)
        self.play_button.setVisible(bool(appid))
        self.store_button.setVisible(bool(appid))
        self.play_button.setText("Играть" if installed else "Установить")
        self.play_button.setIcon(FIF.PLAY if installed else FIF.DOWNLOAD)
        music_page = getattr(self.win, "music_page", None)
        album = music_page.album_for_game(self.game) if music_page and self.game else None
        owned = music_page.owned_for_game(self.game) if music_page and self.game and not album else None
        self.soundtrack_button.setVisible(bool(album or owned))
        if album:
            self.soundtrack_button.setText("Слушать саундтрек")
            self.soundtrack_button.setIcon(FIF.MUSIC)
            self.soundtrack_button.setToolTip(f"«{album['name']}» — скачан в Steam, треков: {len(album['tracks'])}")
        elif owned:
            self.soundtrack_button.setText("Скачать саундтрек")
            self.soundtrack_button.setIcon(FIF.DOWNLOAD)
            self.soundtrack_button.setToolTip(f"«{owned['name']}» есть в вашей библиотеке Steam, но не скачан. "
                                              "Скачается — появится на вкладке «Саундтреки».")
        self.buttons.invalidate()

    def listen_soundtrack(self):
        album = self.win.music_page.album_for_game(self.game)
        if album:
            self.win.music_page.play_album(album)
            notify("success", self.win, "Играет саундтрек",
                   f"«{album['name']}». Управлять музыкой — кнопками справа вверху или на вкладке «Саундтреки».")
            return
        owned = self.win.music_page.owned_for_game(self.game)
        if owned:
            self.win.music_page.download(owned)

    def play(self):
        self.stop_video()
        self.win.launch_game(self.game)

    def open_store(self):
        self.stop_video()
        self.win.open_in_steam(f"steam://store/{self.game['appid']}", self.game, "store")

    def showEvent(self, event):
        super().showEvent(event)
        if self.game:
            self.update_buttons()  # игру могли установить, пока мы были на другой вкладке

    def hideEvent(self, event):
        super().hideEvent(event)
        self.stop_video()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_media)

    def _fit_media(self):
        self.header.fit_width(self.scroll.viewport().width())
        self._place_achievements()
        # 16:9 по ширине страницы, но не выше ~60% окна — чтобы под скриншотом было видно ленту и описание
        width = self.media_view.width()
        limit = max(220, round(self.scroll.viewport().height() * 0.6))
        self.media_view.setFixedHeight(max(220, min(round(width * 9 / 16), limit)))


# --- Страница «Рулетка» -----------------------------------------------------
class LinkDialog(MessageBoxBase):
    """Куда открыть ссылку: во встроенном браузере программы или во внешнем."""

    def __init__(self, parent, url):
        super().__init__(parent)
        self.choice = None  # "inside" | "outside"
        self.viewLayout.addWidget(SubtitleLabel("Открыть ссылку", self))
        host = QUrl(url).host().removeprefix("www.") or url
        text = BodyLabel(f"Страница {host} может открыться прямо в программе — вернуться к карточке "
                         f"можно одной кнопкой. Или во внешнем браузере.", self)
        text.setWordWrap(True)
        self.viewLayout.addWidget(text)
        self.remember_box = CheckBox("Больше не спрашивать", self)
        self.remember_box.setToolTip("Поменять выбор можно в «Настройках» → «Ссылки»")
        self.viewLayout.addSpacing(4)
        self.viewLayout.addWidget(self.remember_box)
        self.widget.setMinimumWidth(460)

        self.yesButton.setText("Открыть в программе")
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(lambda: self._choose("inside"))
        outside = PushButton("Во внешнем браузере", self.buttonGroup)
        outside.clicked.connect(lambda: self._choose("outside"))
        self.buttonLayout.insertWidget(1, outside, 1, Qt.AlignVCenter)
        self.cancelButton.setText("Отмена")

    def _choose(self, choice):
        self.choice = choice
        self.accept()


class WebView(QWebEngineView if QWebEngineView else QWidget):
    """Ссылки «в новом окне» открываем здесь же — других окон у встроенного браузера нет."""

    def createWindow(self, kind):
        return self


_web_profile = None


def web_profile(widget):
    """Общий профиль встроенного браузера: куки и кэш сайтов в cache/web.

    Через него же программа узнаёт, что куплено в Steam, — вход в магазин запоминается в куках.
    Профиль принадлежит главному окну и создаётся позже его вкладок. При выходе Qt удаляет детей по порядку,
    так что страницы исчезают раньше своего профиля — иначе Qt ругается в консоль.
    """
    global _web_profile
    if _web_profile is None:
        _web_profile = QWebEngineProfile("BacklogRoulette", widget.window())
        storage = os.path.join(core.CACHE_DIR, "web")
        _web_profile.setPersistentStoragePath(storage)
        _web_profile.setCachePath(os.path.join(storage, "cache"))
        # Вход в магазин Steam — «сессионная» кука; без этого он забывался бы при каждом закрытии программы
        _web_profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    return _web_profile


class BrowserPage(QWidget):
    """Встроенный браузер на всю рабочую область — третий экран после рулетки и карточки игры.

    Движок браузера создаётся при запуске программы (см. preload). Куки и кэш сайтов хранятся
    в cache/web — второй раз страницы открываются быстрее, согласие на куки не спрашивают заново.
    """

    backRequested = Signal()
    MUSIC_SPACE = 286  # справа поверх лежит мини-плеер

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = None

        self.return_button = PrimaryPushButton("К карточке", self, FIF.RETURN)
        self.return_button.clicked.connect(self.backRequested)
        outside = TransparentPushButton("Во внешнем браузере", self, FIF.GLOBE)
        outside.clicked.connect(self.open_outside)
        self.back_button = TransparentToolButton(FIF.LEFT_ARROW, self)
        self.back_button.setToolTip("Назад")
        self.forward_button = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.forward_button.setToolTip("Вперёд")
        reload = TransparentToolButton(FIF.SYNC, self)
        reload.setToolTip("Обновить")
        self.title_label = StrongBodyLabel("", self)
        self.title_label.setMinimumWidth(80)
        self.bar = ProgressBar(self)
        self.bar.setFixedHeight(3)
        self.bar.hide()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(20, 12, self.MUSIC_SPACE, 8)
        toolbar.setSpacing(6)
        toolbar.addWidget(self.return_button)
        toolbar.addWidget(outside)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.back_button)
        toolbar.addWidget(self.forward_button)
        toolbar.addWidget(reload)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.title_label, 1)

        self.page_box = QVBoxLayout(self)
        self.page_box.setContentsMargins(0, 0, 0, 0)
        self.page_box.setSpacing(0)
        self.page_box.addLayout(toolbar)
        self.page_box.addWidget(self.bar)
        self._reload = reload

    def preload(self, host):
        """Движок браузера — сразу при запуске: крошечный и невидимый, прямо в главном окне.

        Когда браузер впервые появляется в окне, Windows пересоздаёт окно под видео-ускорение — видно,
        как оно закрывается и открывается снова. Если это случилось ещё до показа окна, потом браузер
        открывается без мигания. При первой ссылке тот же движок переезжает на свою страницу.
        """
        self._ensure_view(host)
        self.view.setGeometry(0, 0, 1, 1)
        self.view.lower()  # под всеми страницами окна
        self.view.setUrl(QUrl("about:blank"))

    def _ensure_view(self, parent=None):
        if self.view:
            return
        self.view = WebView(parent or self)
        self.view.setPage(QWebEnginePage(web_profile(self), self.view))
        self.view.titleChanged.connect(self._on_title)
        self.view.urlChanged.connect(self._update_nav)
        self.view.loadStarted.connect(lambda: (self.bar.setValue(0), self.bar.show()))
        self.view.loadProgress.connect(self.bar.setValue)
        self.view.loadFinished.connect(self._on_loaded)
        self.back_button.clicked.connect(self.view.back)
        self.forward_button.clicked.connect(self.view.forward)
        self._reload.clicked.connect(self.view.reload)

    def open(self, url, back_text="К карточке"):
        self._ensure_view()
        if self.view.parentWidget() is not self:  # заготовка с запуска — переносим на страницу
            self.page_box.addWidget(self.view, 1)
            self.view.show()
        self.return_button.setText(back_text)
        self._timing = Timing(f"браузер, {QUrl(url).host() or url}")  # до конца загрузки страницы
        self.title_label.setText(QUrl(url).host())
        self.view.setUrl(QUrl(url))
        self._update_nav()

    def _on_loaded(self, ok):
        self.bar.hide()
        timing, self._timing = getattr(self, "_timing", None), None
        if timing:
            timing.done("страница загружена" if ok else "страница не открылась")

    def open_outside(self):
        if self.view:
            QDesktopServices.openUrl(self.view.url())

    def _on_title(self, title):
        fm = QFontMetrics(self.title_label.font())
        self.title_label.setText(fm.elidedText(title, Qt.ElideRight, max(80, self.title_label.width())))
        self.title_label.setToolTip(title)

    def _update_nav(self, *_):
        history = self.view.history() if self.view else None
        self.back_button.setEnabled(bool(history and history.canGoBack()))
        self.forward_button.setEnabled(bool(history and history.canGoForward()))

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.view:
            # Ушли к карточке — видео и звук на странице не должны играть в фоне
            self.view.page().setAudioMuted(True)

    def showEvent(self, event):
        super().showEvent(event)
        if self.view:
            self.view.page().setAudioMuted(False)


class FilmBackdrop(QWidget):
    """Фон вкладки «Рулетка»: наклонные ленты «фотоплёнки» из скачанных обложек, соседние едут навстречу.

    Фон размыт и притушен, чтобы барабан поверх него читался чётко. Рисуем его в картинку втрое меньше
    окна и растягиваем: так дешевле, а растяжение само добавляет мягкости.
    """

    SCALE = 3  # во сколько раз картинка фона меньше окна
    ANGLE = 18  # наклон лент в градусах: верх уходит влево, как «\»
    SPEED = 8  # пикселей маленькой картинки в секунду — на экране втрое больше
    FPS = 30
    STRIPS = 8  # разных лент; если на экран нужно больше — они повторяются
    CORNER = 10  # скругление левого верхнего угла области страниц у MSFluentWindow

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.strips = []  # QPixmap лент
        self.frame = None  # последний кадр в уменьшенном виде
        self.started = time.monotonic()
        self._building = False
        self._dirty = False  # пока ленты собирались, скачались новые обложки
        self.opacity = 0.0

        self.timer = QTimer(self)
        self.timer.setInterval(1000 // self.FPS)
        self.timer.timeout.connect(self._tick)
        self.fade_anim = QVariantAnimation(self)
        self.fade_anim.setDuration(900)
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(1.0)
        self.fade_anim.valueChanged.connect(self._set_opacity)

    def rebuild(self):
        """Собрать ленты заново из обложек на диске — в фоне, окно не подвисает."""
        if self._building:
            self._dirty = True
            return
        self._building = True
        worker = Worker(core.film_strips, self.STRIPS, parent=self)
        worker.finished.connect(self._on_built)
        worker.failed.connect(self._on_build_failed)
        worker.start()

    def rebuild_if_empty(self):
        if not self.strips:
            self.rebuild()

    def _on_built(self, images):
        self._building = False
        had_strips = bool(self.strips)
        self.strips = [QPixmap.fromImage(QImage(image.tobytes("raw", "RGB"), image.width, image.height,
                                                image.width * 3, QImage.Format_RGB888).copy())
                       for image in images]
        if self.strips and not had_strips:
            self.fade_anim.start()  # первое появление — плавно
        self._render()
        self.update()
        if self._dirty:
            self._dirty = False
            self.rebuild()

    def _on_build_failed(self, message):
        self._building = False
        core.log.warning("Фон рулетки не собрался: %s", message)

    def _set_opacity(self, value):
        self.opacity = value
        self.update()

    def _tick(self):
        if self.strips and not self.window().isMinimized():
            self._render()
            self.update()

    def _render(self):
        w = max(1, math.ceil(self.width() / self.SCALE))
        h = max(1, math.ceil(self.height() / self.SCALE))
        if self.frame is None or self.frame.width() != w or self.frame.height() != h:
            self.frame = QImage(w, h, QImage.Format_RGB32)
        self.frame.fill(QColor(*core.FILM_COLOR))
        if not self.strips:
            return
        painter = QPainter(self.frame)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)  # плавный сдвиг на доли пикселя

        t = time.monotonic() - self.started
        half = math.hypot(w, h) / 2  # повёрнутые ленты должны закрыть кадр целиком, с углами
        strip_w = self.strips[0].width()
        count = math.ceil(2 * half / strip_w) + 1
        painter.save()
        painter.translate(w / 2, h / 2)
        painter.rotate(-self.ANGLE)
        left = -count * strip_w / 2
        for i in range(count):
            pixmap = self.strips[i % len(self.strips)]
            period = pixmap.height()
            direction = 1 if i % 2 else -1  # соседние ленты едут в разные стороны
            shift = (t * self.SPEED * direction + i * 37) % period
            painter.save()
            painter.translate(left + i * strip_w, -half - period + shift)
            painter.drawTiledPixmap(QRectF(0, 0, strip_w, 2 * half + 2 * period), pixmap)
            painter.restore()
        painter.restore()

        # Вуаль цвета темы, в центре — плотнее: там барабан
        dark = isDarkTheme()
        wash = QColor(22, 22, 26, 165) if dark else QColor(240, 240, 244, 185)
        painter.fillRect(0, 0, w, h, wash)
        glow = QRadialGradient(QPointF(w / 2, h * 0.52), max(w, h) * 0.55)
        glow.setColorAt(0, QColor(wash.red(), wash.green(), wash.blue(), 70))
        glow.setColorAt(1, QColor(wash.red(), wash.green(), wash.blue(), 0))
        painter.fillRect(0, 0, w, h, glow)
        painter.end()

    def paintEvent(self, event):
        if not self.strips:
            return
        if self.frame is None:
            self._render()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.setOpacity(self.opacity)
        corner = QPainterPath()  # скругляем только левый верхний угол, как у самой области страниц
        corner.addRoundedRect(QRectF(0, 0, self.width() + self.CORNER, self.height() + self.CORNER),
                              self.CORNER, self.CORNER)
        painter.setClipPath(corner)
        painter.drawImage(QRectF(0, 0, self.frame.width() * self.SCALE, self.frame.height() * self.SCALE),
                          self.frame)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.frame = None

    def showEvent(self, event):
        super().showEvent(event)
        self.rebuild_if_empty()
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()  # на другой вкладке и на странице игры фон не крутим


class HomePage(QWidget):
    """Вкладка «Рулетка»: барабан с одной кнопкой «Крутить», а после выбора — страница выпавшей игры."""

    OPEN_DELAY = 1200  # мс: рамка вокруг выпавшей игры успевает вспыхнуть, потом открывается её страница
    MARGINS = (36, 20, 36, 28)  # отступы барабана; страница игры идёт от края до края

    def __init__(self, window):
        super().__init__()
        self.setObjectName("homePage")
        self.win = window
        self.current = None
        self.backdrop = FilmBackdrop(self)

        # Мини-плеер не входит в раскладку, а лежит поверх в правом верхнем углу —
        # и над фоном рулетки, и над шапкой страницы игры, и над браузером
        self.title = TitleLabel("Во что сыграть дальше?", self)
        self.mini_player = MiniPlayer(self.win, self)
        # Кнопки «назад/вперёд» появляются и прячутся — ставим плеер на место, когда он уже обновился
        self.win.music.trackChanged.connect(lambda: QTimer.singleShot(0, self._place_music_buttons))
        self.count_label = CaptionLabel("", self)

        self.top_bar = QWidget(self)  # заголовок и число игр; на странице игры прячется целиком
        top = QVBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        top.addWidget(self.title)
        top.addWidget(self.count_label)

        self.stack = QStackedWidget(self)
        self.loading_view = self._build_loading_view()
        self.reel_view = self._build_reel_view()
        self.game_page = GamePage(self)
        self.browser = BrowserPage(self) if QWebEngineView else None
        self.stack.addWidget(self.loading_view)
        self.stack.addWidget(self.reel_view)
        self.stack.addWidget(self.game_page)
        if self.browser:
            self.browser.backRequested.connect(self._leave_browser)
            self.stack.addWidget(self.browser)
        self._browser_return = None  # куда вернуться из браузера, если он открыт не с карточки игры

        self.page_layout = QVBoxLayout(self)
        self.page_layout.setContentsMargins(*self.MARGINS)
        self.page_layout.setSpacing(12)
        self.page_layout.addWidget(self.top_bar)
        self.page_layout.addWidget(self.stack, 1)

        self.win.libraryChanged.connect(self.on_library_changed)
        self.win.covers.coverReady.connect(self.reel.refresh_cover)
        self.win.covers.progress.connect(self.on_cover_progress)
        self.win.covers.finished.connect(self.on_covers_finished)
        self.reel.show_idle(self.win.library.games)
        self.stack.setCurrentWidget(self.reel_view)
        self.update_count()
        self.update_buttons()

    def _build_loading_view(self):
        view = QWidget()
        title = SubtitleLabel("Подготавливаем обложки", view)
        text = BodyLabel("Скачиваем картинки для ваших игр, чтобы барабан крутился красиво.\n"
                         "Это нужно один раз — дальше обложки берутся с диска.", view)
        text.setAlignment(Qt.AlignCenter)
        self.progress_bar = ProgressBar(view)
        self.progress_bar.setFixedWidth(420)
        self.progress_label = CaptionLabel("", view)
        skip = PushButton("Пропустить", view, FIF.RIGHT_ARROW)
        skip.clicked.connect(self.show_reel)

        layout = QVBoxLayout(view)
        layout.setSpacing(14)
        layout.addStretch(1)
        for widget in (title, text, self.progress_bar, self.progress_label):
            layout.addWidget(widget, 0, Qt.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(skip, 0, Qt.AlignHCenter)
        layout.addStretch(1)
        return view

    def _build_reel_view(self):
        view = QWidget()
        self.reel = CoverReel(view)
        self.reel.spinFinished.connect(self.on_spin_finished)
        self.reel.cardPassed.connect(self.win.sounds.tick)

        self.name_label = SubtitleLabel("", view)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setWordWrap(True)
        self.name_effect = QGraphicsOpacityEffect(self.name_label)
        self.name_label.setGraphicsEffect(self.name_effect)
        self.name_anim = QPropertyAnimation(self.name_effect, b"opacity", self)
        self.name_anim.setDuration(500)

        self.spin_button = PrimaryPushButton("Крутить", view, FIF.SYNC)
        self.spin_button.setMinimumWidth(200)
        self.spin_button.clicked.connect(self.spin)

        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addStretch(1)
        layout.addWidget(self.reel)
        layout.addWidget(self.name_label)
        layout.addSpacing(8)
        layout.addWidget(self.spin_button, 0, Qt.AlignHCenter)
        layout.addStretch(1)
        return view

    # --- Переходы между барабаном, загрузкой и страницей игры ---
    def _set_view(self, view):
        self.stack.setCurrentWidget(view)
        full = view is self.game_page or (self.browser is not None and view is self.browser)
        # У страницы игры и браузера свой заголовок и своя подложка, поэтому они
        # занимают всю рабочую область, без отступов и полос по краям
        self.top_bar.setVisible(not full)
        self.backdrop.setVisible(not full)
        self.page_layout.setContentsMargins(*((0, 0, 0, 0) if full else self.MARGINS))
        self._place_music_buttons()

    def _on_page(self):
        """Открыта страница игры или браузер с её ссылкой — оттуда к загрузке обложек не уводим."""
        return self.stack.currentWidget() in (self.game_page, self.browser)

    def open_link(self, url):
        """Ссылка с карточки игры: в программе или во внешнем браузере — как выбрано, иначе спрашиваем."""
        if not self.browser:
            QDesktopServices.openUrl(QUrl(url))
            return
        choice = core.load_settings().get("link_open", "ask")
        if choice not in ("inside", "outside"):
            dialog = LinkDialog(self.win, url)
            core.log.info("Открыто окно «Открыть ссылку»: %s", url)
            dialog.exec()
            choice = dialog.choice
            core.log.info("Окно «Открыть ссылку» закрыто: %s%s", choice or "отмена",
                          ", больше не спрашивать" if choice and dialog.remember_box.isChecked() else "")
            if choice and dialog.remember_box.isChecked():
                core.save_settings(link_open=choice)
                self.win.settings_page.show_link_mode(choice)
        if choice == "outside":
            QDesktopServices.openUrl(QUrl(url))
        elif choice == "inside":
            self.open_browser(url)

    def open_browser(self, url, back_text="К карточке", on_back=None):
        """Встроенный браузер. on_back — что сделать по кнопке возврата (по умолчанию — к карточке игры)."""
        self._browser_return = on_back
        self.browser.open(url, back_text)
        self._set_view(self.browser)

    def _leave_browser(self):
        callback, self._browser_return = self._browser_return, None
        if self.game_page.game:
            self._set_view(self.game_page)
        else:
            self.show_reel()
        if callback:
            callback()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.backdrop.setGeometry(self.rect())
        self.backdrop.lower()
        self._place_music_buttons()

    def _place_music_buttons(self):
        """Мини-плеер — в правом верхнем углу, на одной линии с заголовком «Во что сыграть дальше?»."""
        _, top, right, _ = self.MARGINS
        size = self.mini_player.sizeHint()
        y = top + (self.title.sizeHint().height() - size.height()) // 2
        self.mini_player.setGeometry(self.width() - right - size.width(), y, size.width(), size.height())
        self.mini_player.raise_()

    def show_loading(self):
        # Со страницы игры не уводим — обложки докачаются в фоне
        if self.reel.is_spinning() or self._on_page():
            return
        self._set_view(self.loading_view)

    def show_reel(self):
        if self.stack.currentWidget() is self.reel_view:
            return
        if self.current is None:
            self.reel.show_idle(self.win.library.games)
        self._set_view(self.reel_view)
        self.reel.play_intro()

    def back_to_reel(self):
        self.show_reel()

    def open_game(self, game):
        """Страница игры — после прокрутки или из списка игр."""
        if self.reel.is_spinning():
            return
        self.current = game
        # При возврате к рулетке барабан стоит на этой игре
        self.reel.show_idle(self.win.library.games, center=game)
        self.show_name(game["name"])
        self.game_page.show_game(game)
        self._set_view(self.game_page)
        if not core.find_cover(game["name"]):
            self.win.covers.enqueue([game], front=True)

    def spin_again(self):
        self.show_reel()
        self.spin()

    def on_cover_progress(self, done, total):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_label.setText(f"{done} из {total}")
        if done >= 10:
            self.backdrop.rebuild_if_empty()  # первые обложки уже есть — фон можно показать

    def on_covers_finished(self):
        self.backdrop.rebuild()  # в ленты попадут и только что скачанные обложки
        if self.stack.currentWidget() is self.loading_view:
            # Небольшая пауза, чтобы полоса успела дойти до конца
            QTimer.singleShot(400, self.show_reel)

    # --- Барабан ---
    def update_count(self):
        self.count_label.setText(f"Игр в списке: {len(self.win.library.games)}")

    def update_buttons(self):
        self.spin_button.setEnabled(not self.reel.is_spinning())

    def show_name(self, text):
        self.name_label.setText(text)
        self.name_anim.stop()
        self.name_anim.setStartValue(0.0)
        self.name_anim.setEndValue(1.0)
        self.name_anim.start()

    def on_library_changed(self):
        self.update_count()
        if self.reel.is_spinning():
            return
        if self.current is not None and self.current not in self.win.library.games:
            self.current = None
            self.name_label.setText("")
            if self._on_page():
                self.show_reel()  # игру убрали из списка — её страница больше не нужна
        if self.current is None:
            self.reel.show_idle(self.win.library.games)
        self.update_buttons()

    def spin(self):
        games = self.win.library.games
        if self.reel.is_spinning():
            return
        if not games:
            notify("warning", self.win, "Список пуст", "Добавьте игры на вкладке «Игры».")
            return
        # Не выдаём ту же игру два раза подряд
        candidates = [g for g in games if g is not self.current] or games
        self.current = None
        self.name_label.setText("Крутим…")
        self.reel.spin(games, random.choice(candidates))
        self.update_buttons()

    def on_spin_finished(self, game):
        self.win.sounds.win()
        self.current = game
        self.show_name(game["name"])
        self.update_buttons()
        if not core.find_cover(game["name"]):
            self.win.covers.enqueue([game], front=True)
        QTimer.singleShot(self.OPEN_DELAY, lambda: self._open_after_spin(game))

    def _open_after_spin(self, game):
        # За время паузы могли закрутить заново или уйти с барабана
        if self.current is game and not self.reel.is_spinning() and self.stack.currentWidget() is self.reel_view:
            self.open_game(game)

    def mark_done(self):
        if not self.current:
            return
        name = self.current["name"]
        if not confirm(self.win, "Игра пройдена?", f"Убрать «{name}» из списка вместе с обложкой?", "Убрать"):
            return
        self.win.library.remove(self.current)
        if not self.win.save_library():
            return
        try:
            core.delete_cover(name)
        except OSError:
            pass  # обложка не критична
        self.current = None
        self.name_label.setText("")
        self.show_reel()  # со страницы пройденной игры — обратно к барабану
        self.win.libraryChanged.emit()
        notify("success", self.win, "Отличная работа!", f"«{name}» убрана из списка.")

    def choose_cover(self):
        if not self.current:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Выберите обложку", "",
                                              "Изображения (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                core.save_cover_bytes(self.current["name"], f.read())
        except OSError as e:
            notify("error", self.win, "Не удалось сохранить обложку", str(e))
            return
        self.reel.refresh_cover(self.current["name"])
        notify("success", self.win, "Обложка сохранена")


# --- Страница «Игры» --------------------------------------------------------
class LibraryPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("libraryPage")
        self.win = window

        self.title = TitleLabel("Мои игры", self)
        self.count_label = CaptionLabel("", self)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("Поиск по названию")
        self.search.textChanged.connect(self.refresh)

        self.list = ListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.show_menu)
        self.list.itemDoubleClicked.connect(lambda item: self.win.launch_game(self.game_of(item)))

        self.import_button = PrimaryPushButton("Импорт из Steam", self, FIF.DOWNLOAD)
        self.import_button.clicked.connect(self.import_from_steam)
        self.sync_button = PushButton("Синхронизировать", self, FIF.UPDATE)
        self.sync_button.setToolTip("Повторить импорт с прошлым выбором коллекций — без окна настроек")
        self.sync_button.clicked.connect(self.quick_sync)
        self.add_button = PushButton("Добавить", self, FIF.ADD)
        self.add_button.clicked.connect(self.add_games)
        self.delete_button = PushButton("Удалить выбранные", self, FIF.DELETE)
        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button = PushButton("Очистить список", self, FIF.BROOM)
        self.clear_button.clicked.connect(self.clear_library)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        for button in (self.import_button, self.sync_button, self.add_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_button)
        self._quick = False
        self.set_import_busy(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addWidget(self.title)
        layout.addWidget(self.count_label)
        layout.addWidget(self.search)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

        self.win.libraryChanged.connect(self.refresh)
        self.refresh()

    def game_of(self, item):
        return self.win.library.find(item.text()) if item else None

    def show_menu(self, pos):
        item = self.list.itemAt(pos)
        game = self.game_of(item)
        if not game:
            return
        if not item.isSelected():
            self.list.clearSelection()
            item.setSelected(True)

        menu = RoundMenu(parent=self)
        appid = game.get("appid")
        if appid:
            installed = core.is_installed(appid)
            launch = Action(FIF.PLAY if installed else FIF.DOWNLOAD, "Играть" if installed else "Установить", menu)
            launch.triggered.connect(lambda: self.win.launch_game(game))
            store = Action(FIF.SHOPPING_CART, "Открыть в магазине Steam", menu)
            store.triggered.connect(lambda: self.win.open_in_steam(f"steam://store/{appid}", game, "store"))
            menu.addActions([launch, store])
        else:
            hint = Action(FIF.INFO, "Добавлена вручную — нет в Steam", menu)
            hint.setEnabled(False)
            menu.addAction(hint)

        show = Action(FIF.GAME, "Открыть страницу игры", menu)
        show.triggered.connect(lambda: self.open_page(game))
        menu.addAction(show)
        menu.addSeparator()
        count = len(self.list.selectedItems())
        delete = Action(FIF.DELETE, "Удалить из списка" if count == 1 else f"Удалить выбранные ({count})", menu)
        delete.triggered.connect(self.delete_selected)
        menu.addAction(delete)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def open_page(self, game):
        self.win.switchTo(self.win.home_page)
        self.win.home_page.open_game(game)

    def refresh(self):
        query = self.search.text().casefold().strip()
        self.list.clear()
        for game in sorted(self.win.library.games, key=lambda g: g["name"].casefold()):
            if query and query not in game["name"].casefold():
                continue
            self.list.addItem(QListWidgetItem(game["name"]))
        self.count_label.setText(f"Всего игр: {len(self.win.library.games)}")

    def add_games(self):
        dialog = AddGamesDialog(self.win)
        if not dialog.exec():
            return
        added = [core.clean_name(n) for n in dialog.names() if self.win.library.add(n) == "added"]
        if not added:
            notify("info", self.win, "Нечего добавлять", "Поле пустое или игры уже в списке.")
            return
        if self.win.save_library():
            self.win.libraryChanged.emit()
            notify("success", self.win, f"Добавлено игр: {len(added)}",
                   ", ".join(added[:5]) + ("…" if len(added) > 5 else ""))

    def delete_selected(self):
        names = [item.text() for item in self.list.selectedItems()]
        if not names:
            notify("info", self.win, "Ничего не выбрано", "Выделите игры в списке (Ctrl или Shift — несколько).")
            return
        if not confirm(self.win, "Удалить игры?", f"Убрать из списка игр: {len(names)}?", "Удалить"):
            return
        for name in names:
            game = self.win.library.find(name)
            if game:
                self.win.library.remove(game)
        if self.win.save_library():
            self.win.libraryChanged.emit()
            notify("success", self.win, f"Удалено игр: {len(names)}")

    def clear_library(self):
        count = len(self.win.library.games)
        if not count:
            notify("info", self.win, "Список и так пуст")
            return
        dialog = ClearDialog(self.win, count)
        if not dialog.exec():
            return
        self.win.covers.clear()
        self.win.library.clear()
        if not self.win.save_library():
            return
        text = f"Удалено игр: {count}."
        if dialog.covers.isChecked():
            try:
                text += f" Удалено обложек: {core.clear_covers()}."
            except OSError as e:
                notify("error", self.win, "Не все обложки удалось удалить", str(e))
            self.win.home_page.reel.clear_cache()
        self.win.libraryChanged.emit()
        notify("success", self.win, "Список очищен", text + " Можно импортировать заново.", duration=6000)

    def import_from_steam(self):
        self._load_library(quick=False)

    def quick_sync(self):
        self._load_library(quick=True)

    def _load_library(self, quick):
        settings = core.load_settings()
        api_key, steam_id = settings["steam_api_key"], settings["steam_id"]
        if not api_key or not steam_id:
            notify("warning", self.win, "Нужны ключ и SteamID", "Укажите их на вкладке «Настройки».")
            self.win.switchTo(self.win.settings_page)
            return
        self._quick = quick
        self.set_import_busy(True)
        self._progress_dialog = ImportProgressDialog(
            self.win, "Синхронизация со Steam" if quick else "Загружаю библиотеку Steam")
        self._progress_dialog.show()
        relay = ProgressRelay(self)
        relay.progress.connect(self._on_progress)
        worker = Worker(core.load_steam_library, api_key, steam_id, relay.report, parent=self)
        worker.finished.connect(self.on_library_loaded)
        worker.failed.connect(self.on_import_failed)
        worker.finished.connect(relay.deleteLater)
        worker.failed.connect(relay.deleteLater)
        worker.start()

    def _on_progress(self, text, percent):
        try:
            self._progress_dialog.set_progress(text, percent)
        except RuntimeError:
            pass  # окно уже удалено

    def _close_progress(self):
        try:
            self._progress_dialog.close_now()
        except RuntimeError:
            pass

    def set_import_busy(self, busy):
        self.import_button.setEnabled(not busy)
        self.import_button.setText("Загружаю библиотеку…" if busy else "Импорт из Steam")
        self.sync_button.setEnabled(not busy and core.load_settings()["import_done"])

    def on_library_loaded(self, library):
        self.set_import_busy(False)
        self._close_progress()
        if not library["games"]:
            notify("warning", self.win, "Steam не вернул ни одной игры",
                   "Проверьте SteamID64 и что «Игровая информация» в приватности профиля открыта.", duration=8000)
            return
        if library["collections_error"]:
            notify("warning", self.win, "Не удалось прочитать коллекции Steam",
                   library["collections_error"], duration=6000)

        settings = core.load_settings()
        if self._quick:
            choice = {key: settings[key] for key in
                      ("import_include", "import_skip_hidden", "import_unplayed", "import_replace")}
        else:
            known_ids = {game["appid"] for game in library["games"]}
            family_count = sum(game["family"] for game in library["games"])
            dialog = ImportDialog(self.win, library["collections"], known_ids, family_count, settings)
            if not dialog.exec():
                return
            choice = dialog.selection()
            core.save_settings(import_done=True, **choice)
            self.set_import_busy(False)
        self.apply_import(library, choice)

    def apply_import(self, library, choice):
        selected, missing = core.select_games(library, choice)
        if choice["import_include"] and len(missing) == len(choice["import_include"]):
            # Ни одной выбранной коллекции больше нет — не импортируем всю библиотеку по ошибке
            notify("warning", self.win, "Выбранные коллекции не найдены",
                   "Возможно, их переименовали или удалили в Steam. Откройте «Импорт из Steam» и выберите заново.",
                   duration=8000)
            return

        added = 0
        for game in selected:
            added += self.win.library.add(game["name"], game["appid"]) == "added"

        removed = 0
        if choice["import_replace"]:
            keep = {game["appid"] for game in selected}
            for game in list(self.win.library.games):
                if game.get("appid") and game["appid"] not in keep:  # без appid — добавлены вручную
                    self.win.library.remove(game)
                    removed += 1

        if not self.win.save_library():
            return
        core.log.info("%s: выбор %s, подходит %d, добавлено %d, убрано %d",
                      "Синхронизация" if self._quick else "Импорт", choice, len(selected), added, removed)
        self.win.libraryChanged.emit()
        text = f"Подходит под выбор: {len(selected)}. Добавлено новых: {added}."
        if removed:
            text += f" Убрано из списка: {removed}."
        if missing:
            text += f" Не найдено коллекций из прошлого выбора: {len(missing)}."
        notify("success", self.win, "Синхронизация завершена" if self._quick else "Импорт завершён",
               text, duration=7000)
        if added >= LOADING_THRESHOLD:
            self.win.switchTo(self.win.home_page)  # там уже идёт загрузка обложек

    def on_import_failed(self, message):
        self.set_import_busy(False)
        self._close_progress()
        notify("error", self.win, "Не удалось получить библиотеку Steam",
               message + "\nПодробности — на вкладке «Логи».", duration=8000)


# --- Страница «Музыка» ------------------------------------------------------
STEAM_LOGIN_URL = "https://store.steampowered.com/login/"


class SteamOwnedApps(QObject):
    """Что куплено в Steam: магазин отдаёт это на странице dynamicstore/userdata, если вы вошли на сайт.

    Страницу открываем невидимой во встроенном браузере — вход в магазин хранится в его куках.
    finished(список appid) или finished(None), если вход не выполнен; failed(текст) — не удалось открыть.
    """

    finished = Signal(object)
    failed = Signal(str)

    URL = "https://store.steampowered.com/dynamicstore/userdata/"
    TIMEOUT_MS = 30000

    def __init__(self, parent):
        super().__init__(parent)
        self._done = False
        self.page = QWebEnginePage(web_profile(parent), self)
        self.page.loadFinished.connect(self._on_loaded)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self._finish(error="магазин Steam не ответил за 30 секунд"))

    def start(self):
        self.timer.start(self.TIMEOUT_MS)
        self.page.load(QUrl(f"{self.URL}?t={int(time.time())}"))  # без старой копии из кэша

    def _on_loaded(self, ok):
        if self._done:
            return
        if not ok:
            self._finish(error="не удалось открыть магазин Steam")
            return
        self.page.toPlainText(self._on_text)

    def _on_text(self, text):
        if self._done:
            return
        try:
            data = json.loads(text)
        except ValueError:
            self._finish(error="магазин Steam прислал не список покупок")
            return
        owned = [appid for appid in (data.get("rgOwnedApps") or []) if isinstance(appid, int)]
        self._finish(owned or None)  # без входа магазин присылает пустые списки

    def _finish(self, owned=None, error=None):
        if self._done:
            return
        self._done = True
        self.timer.stop()
        if error:
            self.failed.emit(error)
        else:
            self.finished.emit(owned)
        self.deleteLater()


class AlbumBackdrop(QWidget):
    """Размытая обложка играющего альбома за списком его треков.

    К списку альбомов слева и к плееру снизу картинка плавно растворяется, при смене альбома —
    плавно перетекает в новую. Сам виджет лежит под остальными и не мешает кликам.
    """

    LEFT_FADE = 160  # ширина перехода к списку альбомов
    BOTTOM_FADE = 90  # и к плееру
    FADE_MS = 450

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.path = None
        self._source = QImage()  # размытая картинка, ещё не растянутая под размер
        self._frame = self._previous = QPixmap()
        self._previous_opacity = 0.0
        self.opacity = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(self.FADE_MS)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._on_step)

    def set_image(self, path):
        """Показать обложку path (None — спрятать)."""
        if path == self.path:
            return
        self._previous, self._previous_opacity = self._frame, self.opacity
        self.path = path
        self._source = self._blurred(path) if path else QImage()
        self._frame = self._compose()
        self.animation.stop()
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0 if not self._frame.isNull() else 0.0)
        self.opacity = 0.0
        self.animation.start()

    @staticmethod
    def _blurred(path):
        blurred = core.blurred_cover(path)
        if not blurred:
            return QImage()
        width, height, data = blurred
        return QImage(data, width, height, width * 3, QImage.Format_RGB888).copy()  # copy — данные не наши

    def _on_step(self, value):
        self.opacity = value
        self.update()

    def _compose(self):
        """Картинка под нынешний размер: размытие, затемнение под тему и плавные края."""
        if self._source.isNull() or self.width() < 2 or self.height() < 2:
            return QPixmap()
        ratio = self.devicePixelRatioF()
        size = QSize(round(self.width() * ratio), round(self.height() * ratio))
        image = self._source.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        frame = QPixmap(size)
        frame.fill(Qt.transparent)
        painter = QPainter(frame)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(QRect(0, 0, size.width(), size.height()), image,
                          QRect((image.width() - size.width()) // 2, (image.height() - size.height()) // 3,
                                size.width(), size.height()))
        # Подложка не должна спорить с текстом: в тёмной теме притемняем, в светлой — высветляем
        painter.fillRect(frame.rect(), QColor(0, 0, 0, 120) if isDarkTheme() else QColor(255, 255, 255, 150))
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        left = QLinearGradient(0, 0, self.LEFT_FADE * ratio, 0)
        left.setColorAt(0, QColor(0, 0, 0, 0))
        left.setColorAt(1, QColor(0, 0, 0, 255))
        painter.fillRect(frame.rect(), left)
        bottom = QLinearGradient(0, size.height() - self.BOTTOM_FADE * ratio, 0, size.height())
        bottom.setColorAt(0, QColor(0, 0, 0, 255))
        bottom.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillRect(frame.rect(), bottom)
        painter.end()
        frame.setDevicePixelRatio(ratio)
        return frame

    def refresh(self):
        """Размер или тема поменялись — перерисовать."""
        self._frame = self._compose()
        self._previous = QPixmap()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self._previous.isNull() and self.opacity < 1:
            painter.setOpacity(self._previous_opacity * (1 - self.opacity))
            painter.drawPixmap(0, 0, self._previous)
        if not self._frame.isNull():
            painter.setOpacity(self.opacity)
            painter.drawPixmap(0, 0, self._frame)


class MusicPage(QWidget):
    """Вкладка «Саундтреки»: слева — скачанные и купленные саундтреки Steam,
    справа — треки выбранного альбома, внизу — плеер. Встроенная тема и своя музыка — в «Настройках».

    Двойной клик по альбому или треку — слушать. Скачанные саундтреки ищутся в фоне при запуске,
    а «Синхронизировать со Steam» показывает и купленные, но не скачанные — их можно скачать отсюда.
    """

    LIST_ICON = 44  # картинки альбомов в списке слева
    ALBUM_COVER = 128
    BAR_COVER = 56
    WATCH_MS = 15000  # пока Steam качает альбом, раз в 15 секунд смотрим, не появился ли он
    WATCH_LIMIT = 3600  # и бросаем через час
    RESCAN_AFTER = 10  # секунд: при открытии вкладки скачанные альбомы ищутся заново, но не чаще
    TRACK_WORDS = ("original", "soundtrack", "ost", "official", "music", "саундтрек")

    def __init__(self, window):
        super().__init__()
        self.setObjectName("musicPage")
        self.win = window
        self.music = window.music
        self.albums = []  # скачанные саундтреки Steam
        self.owned = core.cached_owned_soundtracks()  # купленные саундтреки — с прошлой синхронизации
        self._synced = core.soundtracks_synced()
        self.sources = []  # {"kind": album | store, "key", "path", "name", "caption", "image"}
        self.row_sources = []  # источник каждой строки списка слева (None — заголовок раздела)
        self.shown = None  # чьи треки показаны справа
        self.shown_tracks, self.shown_titles = [], []
        self._pixmaps = {}
        self._scanning = False
        self._scan_quiet = False
        self._last_scan = 0.0
        self._syncing = False
        self._awaiting_login = False
        self._watch = {}  # appid альбома, который попросили скачать → когда
        self._mix_started = False  # «вперемешку» с прошлого запуска уже собрано
        self._drag_value = None

        settings = window.settings
        self.backdrop = AlbumBackdrop(self)  # создаётся первым — лежит под остальными виджетами

        # Слева — саундтреки
        self.source_list = ListWidget(self)
        self.source_list.setIconSize(QSize(self.LIST_ICON, self.LIST_ICON))
        self.source_list.setFixedWidth(300)
        self.source_list.currentRowChanged.connect(self._on_source_row)
        self.source_list.itemDoubleClicked.connect(
            lambda item: self.play_source(self._source_of_row(self.source_list.row(item))))
        self.sync_button = PushButton("Синхронизировать со Steam", self, FIF.SYNC)
        self.sync_button.setToolTip("Найти скачанные саундтреки и узнать, какие куплены, но ещё не скачаны")
        self.sync_button.clicked.connect(self.sync_steam)
        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(self.WATCH_MS)
        self.watch_timer.timeout.connect(self._check_downloads)

        # Справа — альбом и его треки
        self.album_cover = QLabel(self)
        self.album_cover.setFixedSize(self.ALBUM_COVER, self.ALBUM_COVER)
        self.album_cover.setAlignment(Qt.AlignCenter)
        self.album_title = SubtitleLabel("", self)
        self.album_title.setWordWrap(True)
        self.album_caption = CaptionLabel("", self)
        self.album_caption.setWordWrap(True)
        self.listen_button = PrimaryPushButton("Слушать", self, FIF.PLAY)
        self.listen_button.clicked.connect(lambda: self.play_source(self.shown))
        self.game_button = PushButton("Страница игры", self, FIF.GAME)
        self.game_button.clicked.connect(self.open_game)
        self.track_list = ListWidget(self)
        self.track_list.itemDoubleClicked.connect(self._on_track_double_clicked)

        # Внизу — плеер
        self.bar_cover = QLabel(self)
        self.bar_cover.setFixedSize(self.BAR_COVER, self.BAR_COVER)
        self.bar_cover.setAlignment(Qt.AlignCenter)
        self.bar_title = StrongBodyLabel("", self)
        self.bar_album = CaptionLabel("", self)
        for label in (self.bar_title, self.bar_album):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # длинное название не раздвигает плеер
        self.shuffle_button = make_shuffle_button(self.win, self)
        self.prev_button = TransparentToolButton(PREV_ICON, self)
        self.prev_button.setToolTip("Предыдущий трек (после трёх секунд — к началу трека)")
        self.prev_button.clicked.connect(self.music.prev_track)
        self.play_button = PrimaryToolButton(FIF.PLAY, self)
        self.play_button.clicked.connect(self.win.play_pause)
        self.next_button = TransparentToolButton(NEXT_ICON, self)
        self.next_button.setToolTip("Следующий трек")
        self.next_button.clicked.connect(lambda: self.music.next_track())
        self.repeat_button = make_repeat_button(self.win, self)
        self.position_label = CaptionLabel("0:00", self)
        self.seek_slider = Slider(Qt.Horizontal, self)
        self.duration_label = CaptionLabel("0:00", self)
        self.mute_button = TransparentToolButton(FIF.VOLUME, self)
        self.mute_button.clicked.connect(self.win.toggle_music)
        self.volume_slider = Slider(Qt.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(settings["music_volume"]))
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.valueChanged.connect(self._on_volume)
        self.volume_timer = QTimer(self)  # громкость сохраняем, когда ползунок замер
        self.volume_timer.setSingleShot(True)
        self.volume_timer.setInterval(600)
        self.volume_timer.timeout.connect(lambda: core.save_settings(music_volume=self.volume_slider.value()))

        # Перемотка как у трейлера: клик — сразу, перетаскивание — когда ползунок замер
        if hasattr(self.seek_slider, "clicked"):
            self.seek_slider.clicked.connect(self.music.seek)
        self.seek_slider.sliderMoved.connect(self._on_drag)
        self.seek_slider.sliderReleased.connect(self._finish_drag)
        self.drag_timer = QTimer(self)
        self.drag_timer.setSingleShot(True)
        self.drag_timer.setInterval(250)
        self.drag_timer.timeout.connect(self._finish_drag)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(self.source_list, 1)
        left.addWidget(self.sync_button)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.listen_button)
        actions.addWidget(self.game_button)
        actions.addStretch(1)
        info = QVBoxLayout()
        info.setSpacing(4)
        info.addWidget(self.album_title)
        info.addWidget(self.album_caption)
        info.addStretch(1)
        info.addLayout(actions)
        header = QHBoxLayout()
        header.setSpacing(18)
        header.addWidget(self.album_cover, 0, Qt.AlignTop)
        header.addLayout(info, 1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addLayout(header)
        right.addWidget(self.track_list, 1)

        body = QHBoxLayout()
        body.setSpacing(24)
        body.addLayout(left)
        body.addLayout(right, 1)

        now_text = QVBoxLayout()
        now_text.setSpacing(0)
        now_text.addStretch(1)
        now_text.addWidget(self.bar_title)
        now_text.addWidget(self.bar_album)
        now_text.addStretch(1)
        now = QHBoxLayout()
        now.setSpacing(12)
        now.addWidget(self.bar_cover)
        now.addLayout(now_text, 1)
        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.addStretch(1)
        for button in (self.shuffle_button, self.prev_button, self.play_button, self.next_button, self.repeat_button):
            controls.addWidget(button)
        controls.addStretch(1)
        seek = QHBoxLayout()
        seek.setSpacing(8)
        seek.addWidget(self.position_label)
        seek.addWidget(self.seek_slider, 1)
        seek.addWidget(self.duration_label)
        center = QVBoxLayout()
        center.setSpacing(2)
        center.addLayout(controls)
        center.addLayout(seek)
        volume = QHBoxLayout()
        volume.setSpacing(4)
        volume.addStretch(1)
        volume.addWidget(self.mute_button)
        volume.addWidget(self.volume_slider)
        bar = QHBoxLayout()
        bar.setSpacing(24)
        bar.addLayout(now, 3)
        bar.addLayout(center, 4)
        bar.addLayout(volume, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 20)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("Саундтреки", self))
        layout.addLayout(body, 1)
        layout.addSpacing(4)
        layout.addLayout(bar)

        self.music.trackChanged.connect(self._on_track)
        self.music.stateChanged.connect(self._on_state)
        self.music.positionChanged.connect(self._on_position)
        self.music.durationChanged.connect(self._on_duration)
        self.build_sources()
        self._on_track()
        self._on_state()

    # --- Список слева ---
    def build_sources(self):
        albums = [{"kind": "album", "key": "album:" + os.path.normcase(a["folder"]), "path": a["folder"],
                   "name": a["name"],
                   "caption": (f"Саундтрек к «{a['game_name']}»" if a.get("game_name")
                               else f"Треков: {len(a['tracks'])}"),
                   "image": a.get("image"), "album": a} for a in self.albums]
        downloaded = {a["appid"] for a in self.albums if a["appid"]}
        store = [{"kind": "store", "key": f"store:{item['appid']}", "path": "", "name": item["name"],
                  "caption": (f"Не скачан · к «{item['game_name']}»" if item.get("game_name") else "Не скачан"),
                  "image": item.get("image"), "item": item}
                 for item in self.owned if item["appid"] not in downloaded]
        self.sources = albums + store

        previous = self.shown
        self.source_list.blockSignals(True)
        self.source_list.clear()
        self.row_sources = []
        self._add_header("Скачанные")
        for source in albums:
            self._add_source_row(source)
        if not albums:
            self._add_header("   Ищу…" if self._scanning else "   Скачанных саундтреков нет")
        if store:
            self._add_header(f"Куплены, но не скачаны — {len(store)}")
            for source in store:
                self._add_source_row(source)
        self.source_list.blockSignals(False)
        # Справа остаётся тот же альбом, что был открыт, а при первом показе — тот, что играет
        target = next((s for s in self.sources if previous and s["key"] == previous["key"]), None)
        self.select_source(target or self.playing_source() or (self.sources[0] if self.sources else None))

    def _add_source_row(self, source):
        item = QListWidgetItem(f"{source['name']}\n{source['caption']}")
        pixmap = self._square(source.get("image"), self.LIST_ICON)
        if not pixmap.isNull():
            item.setIcon(QIcon(pixmap))
        else:
            item.setIcon((FIF.DOWNLOAD if source["kind"] == "store" else FIF.MUSIC).icon())
        item.setSizeHint(QSize(0, self.LIST_ICON + 14))
        item.setToolTip("Двойной клик — скачать в Steam" if source["kind"] == "store" else source["path"])
        self.source_list.addItem(item)
        self.row_sources.append(source)

    def _add_header(self, text):
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        self.source_list.addItem(item)
        self.row_sources.append(None)

    def _source_of_row(self, row):
        return self.row_sources[row] if 0 <= row < len(self.row_sources) else None

    def _find_source(self, path):
        if not path:
            return None
        return next((s for s in self.sources if s["kind"] == "album" and core.same_path(s["path"], path)), None)

    def _is_current(self, source):
        """Из этого ли альбома сейчас играет трек."""
        return bool(source) and source["kind"] == "album" and core.same_path(source["path"], self.music.folder)

    def playing_source(self):
        return self._find_source(self.music.folder)

    def _square(self, path, size):
        """Квадратная картинка для списка и плеера. Широкие шапки Steam вписываем целиком, остальное обрезаем."""
        key = (path, size)
        if key not in self._pixmaps:
            pixmap = QPixmap(path) if path else QPixmap()
            if not pixmap.isNull():
                ratio = self.devicePixelRatioF()
                side = round(size * ratio)
                if pixmap.width() > pixmap.height() * 1.3:
                    pixmap = pixmap.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                else:
                    pixmap = pixmap.scaled(side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    pixmap = pixmap.copy((pixmap.width() - side) // 2, (pixmap.height() - side) // 2, side, side)
                pixmap.setDevicePixelRatio(ratio)
            self._pixmaps[key] = pixmap
        return self._pixmaps[key]

    def _placeholder(self, size):
        return fluent_icon("ALBUM", FIF.MUSIC).icon().pixmap(size // 2, size // 2)

    # --- Треки справа ---
    def select_source(self, source):
        row = next((i for i, s in enumerate(self.row_sources) if s is source), -1)
        self.source_list.blockSignals(True)
        self.source_list.setCurrentRow(row)
        self.source_list.blockSignals(False)
        self.show_source(source)

    def _on_source_row(self, row):
        source = self._source_of_row(row)
        if source:
            self.show_source(source)

    def show_source(self, source):
        self.shown = source
        if not source:  # саундтреков нет совсем
            self.shown_tracks, self.shown_titles = [], []
            self.track_list.clear()
            self.album_cover.setPixmap(self._placeholder(self.ALBUM_COVER))
            self.album_title.setText("Саундтреков пока нет")
            self.album_caption.setText("Скачайте саундтрек в Steam или нажмите «Синхронизировать со Steam» — "
                                       "появятся и купленные, но не скачанные. Встроенная тема и своя музыка — "
                                       "в «Настройках».")
            self.listen_button.hide()
            self.game_button.hide()
            self._update_backdrop()
            return
        self.listen_button.show()
        store = source["kind"] == "store"
        if store:
            tracks, titles = [], []
        else:
            tracks = source["album"]["tracks"]
            titles = core.track_titles(tracks)
        self.shown_tracks, self.shown_titles = list(tracks), list(titles)
        self.track_list.clear()
        for _ in titles:
            self.track_list.addItem(QListWidgetItem(""))
        if store:
            hint = QListWidgetItem("Треки появятся здесь, когда Steam скачает альбом")
            hint.setFlags(Qt.NoItemFlags)
            self.track_list.addItem(hint)

        cover = self._square(source.get("image"), self.ALBUM_COVER)
        self.album_cover.setPixmap(cover if not cover.isNull() else self._placeholder(self.ALBUM_COVER))
        self.album_title.setText(source["name"])
        parts = ["Куплен в Steam, но не скачан на этот ПК"] if store else [f"Треков: {len(tracks)}"]
        album = source.get("album") or source.get("item") or {}
        if album.get("game_name"):
            parts.append(f"саундтрек к игре «{album['game_name']}»")
        self.album_caption.setText(" · ".join(parts))
        self.game_button.setVisible(bool(self._library_game(source)))
        self.listen_button.setText("Скачать в Steam" if store else "Слушать")
        self.listen_button.setIcon(FIF.DOWNLOAD if store else FIF.PLAY)
        self.listen_button.setEnabled(store or bool(tracks))
        self._mark_tracks()
        self._update_backdrop()

    def _update_backdrop(self):
        """Размытая обложка — когда справа открыт тот альбом, что играет."""
        self.backdrop.set_image(self.shown.get("image") if self._is_current(self.shown) else None)

    def _place_backdrop(self):
        """От правого края списка альбомов до края окна и от верха до конца списка треков."""
        x = self.source_list.geometry().right() + 1
        bottom = self.track_list.geometry().bottom() + 12
        self.backdrop.setGeometry(x, 0, max(0, self.width() - x), max(0, bottom))
        self.backdrop.lower()

    def _mark_tracks(self):
        """Играющий трек — жирным и со значком ▶. Ищем по файлу: «вперемешку» номера треков другие."""
        playing = self.music.path if self._is_current(self.shown) else None
        for i, title in enumerate(self.shown_titles):
            item = self.track_list.item(i)
            if not item:
                break
            current = playing is not None and self.shown_tracks[i] == playing
            font = item.font()
            font.setBold(current)
            item.setFont(font)
            item.setText(("▶   " if current else f"{i + 1}.   ") + title)

    def _on_track_double_clicked(self, item):
        track = self.track_list.row(item)
        if 0 <= track < len(self.shown_tracks):
            self.play_source(self.shown, start=self.shown_tracks[track])

    def _library_game(self, source):
        appid = (source.get("album") or source.get("item") or {}).get("game")
        return next((g for g in self.win.library.games if g.get("appid") == appid), None) if appid else None

    def open_game(self):
        game = self._library_game(self.shown) if self.shown else None
        if game:
            self.win.library_page.open_page(game)

    # --- Что играет ---
    def play_source(self, source, start=None):
        if not source:
            return
        if source["kind"] == "store":
            self.download(source["item"])
            return
        core.save_settings(music_path=source["path"])
        album = {"name": source["name"], "image": source.get("image")}
        self.music.play(source["path"], start=start, album=album,
                        entries=self.library_entries(self.music.shuffle_mode))
        if not self.music.enabled:
            self.win.set_music_enabled(True)

    def library_entries(self, mode):
        """Треки для «вперемешку»: скачанные саундтреки, а для «вся музыка» — ещё и своя музыка из «Настроек».
        Встроенная тема не входит никогда."""
        if mode == "off":
            return None
        entries = []
        folders = [(a["folder"], a["tracks"], {"name": a["name"], "image": a.get("image")}) for a in self.albums]
        custom = core.load_settings().get("music_custom", "")
        if mode == "all" and custom and os.path.exists(custom) and not any(
                core.same_path(custom, folder) for folder, _, _ in folders):
            tracks = self.music._source_tracks(custom)
            folders.append((custom, tracks, self.music._default_album(custom)))
        for folder, tracks, album in folders:
            album = dict(album, folder=folder)
            for path, title in zip(tracks, core.track_titles(tracks)):
                entries.append({"path": path, "title": title, "album": album})
        return entries

    def play_album(self, album):
        """Саундтрек с карточки игры. Уже играет — просто снимаем с паузы."""
        source = self._find_source(album["folder"])
        if not source:
            return
        if self._is_current(source) and self.music.enabled:
            self.music.set_paused(False)
        else:
            self.play_source(source)

    def album_for_game(self, game):
        """Скачанный саундтрек игры: по ссылке со страницы альбома в магазине, а без неё — по названию."""
        appid = game.get("appid")
        if appid:
            for album in self.albums:
                if album.get("game") == appid:
                    return album
        name = game["name"].casefold()
        for album in self.albums:
            if album.get("game"):
                continue  # альбом точно от другой игры
            title = album["name"].casefold()
            if title.startswith(name) and title[len(name):].lstrip(" -:–—").startswith(self.TRACK_WORDS):
                return album
        return None

    def owned_for_game(self, game):
        """Купленный, но не скачанный саундтрек игры — известен после «Синхронизировать со Steam»."""
        appid = game.get("appid")
        if not appid:
            return None
        downloaded = {a["appid"] for a in self.albums if a["appid"]}
        return next((item for item in self.owned if item.get("game") == appid and item["appid"] not in downloaded),
                    None)

    def download(self, item):
        """Окно скачивания в Steam. Пока альбом качается, проверяем папки Steam и сами показываем его."""
        url = f"steam://install/{item['appid']}"
        core.log.info("Скачать саундтрек «%s»: %s", item["name"], url)
        Worker(core.open_url, url, parent=self).start()  # Steam может отвечать несколько секунд
        self._watch[item["appid"]] = time.monotonic()
        self.watch_timer.start()
        notify("info", self.win, "Steam откроет окно скачивания",
               f"«{item['name']}». Когда альбом скачается, он сам появится на вкладке «Саундтреки».", 6000)

    def _on_track(self):
        music = self.music
        several = len(music.tracks) > 1
        for button in (self.prev_button, self.next_button, self.repeat_button):
            button.setEnabled(several)
        self.seek_slider.setValue(0)
        self.position_label.setText("0:00")
        self._on_duration(music.duration())
        self._refresh_bar()
        # Саундтрек включили с карточки игры — пусть вкладка откроется на нём
        playing = self.playing_source()
        if playing and not self.isVisible() and playing is not self.shown:
            self.select_source(playing)
        else:
            self._mark_tracks()
            self._update_backdrop()
        for row, source in enumerate(self.row_sources):
            item = self.source_list.item(row)
            if source and item:
                font = item.font()
                font.setBold(self._is_current(source))
                item.setFont(font)

    def _refresh_bar(self):
        music = self.music
        cover = self._square(music.album.get("image"), self.BAR_COVER)
        self.bar_cover.setPixmap(cover if not cover.isNull() else self._placeholder(self.BAR_COVER))
        self._elide(self.bar_title, music.title or "Музыка не выбрана")
        self._elide(self.bar_album, music.album.get("name", ""))

    @staticmethod
    def _elide(label, text):
        label.setText(QFontMetrics(label.font()).elidedText(text, Qt.ElideRight, max(60, label.width())))
        label.setToolTip(text)

    def _on_state(self):
        music = self.music
        self.play_button.setIcon(FIF.PAUSE if music.playing else FIF.PLAY)
        self.play_button.setToolTip("Пауза" if music.playing else "Слушать")
        self.mute_button.setIcon(FIF.VOLUME if music.enabled else FIF.MUTE)
        self.mute_button.setToolTip("Выключить музыку" if music.enabled else "Включить музыку")
        refresh_shuffle_button(self.shuffle_button, music.shuffle_mode)
        refresh_repeat_button(self.repeat_button, music.repeat_one)

    def _on_position(self, position):
        if self._drag_value is not None or self.seek_slider.isSliderDown():
            return  # ползунок сейчас тянут — не перебиваем его
        self.seek_slider.setValue(position)
        self.position_label.setText(format_ms(position))

    def _on_duration(self, duration):
        self.seek_slider.setRange(0, max(duration, 0))
        self.duration_label.setText(format_ms(duration))

    def _on_drag(self, value):
        self._drag_value = value
        self.position_label.setText(format_ms(value))
        self.drag_timer.start()

    def _finish_drag(self):
        self.drag_timer.stop()
        if self._drag_value is None:
            return
        value, self._drag_value = self._drag_value, None
        self.music.seek(value)

    def _on_volume(self, value):
        self.music.set_volume(value)
        self.volume_timer.start()

    # --- Саундтреки Steam ---
    def reload_soundtracks(self, quiet=False):
        """Скачанные альбомы — с диска. quiet — проверка в фоне: без надписей и без записи в лог."""
        if self._scanning:
            return
        self._scanning, self._scan_quiet = True, quiet
        self._last_scan = time.monotonic()
        self._scan_timing = None if quiet else Timing("поиск саундтреков")  # фоновые проверки не меряем
        worker = Worker(core.find_soundtracks, not quiet, parent=self)
        worker.finished.connect(self._on_soundtracks_found)
        worker.failed.connect(self._on_scan_failed)
        worker.start()

    @staticmethod
    def _albums_key(albums):
        return [(a["folder"], len(a["tracks"])) for a in albums]

    def _on_soundtracks_found(self, albums):
        if self._scan_timing:
            self._scan_timing.mark(f"на диске {len(albums)}")
        self._check_watched(albums)
        if self._scan_quiet and self._albums_key(albums) == self._albums_key(self.albums):
            self._finish_scan()  # ничего не изменилось — список не перерисовываем
            return
        # Картинки и игры уже известных альбомов сохраняем, пока магазин отвечает про новые
        known = {a["folder"]: a for a in self.albums}
        for album in albums:
            old = known.get(album["folder"], {})
            for key in ("game", "game_name", "image"):
                if key in old:
                    album[key] = old[key]
        self.albums = albums
        self.build_sources()
        if not albums:
            self._finish_scan()
            self._refresh_game_page()
            return
        self._sync_album_info()
        # Картинки и привязка к играм — из магазина Steam и кэша клиента Steam, тоже в фоне
        worker = Worker(core.soundtrack_details, [dict(a) for a in albums], parent=self)
        worker.finished.connect(self._on_soundtrack_details)
        worker.failed.connect(self._on_scan_failed)
        worker.start()

    def _on_soundtrack_details(self, albums):
        self.albums = albums
        self._finish_scan()
        self.build_sources()
        self._sync_album_info()
        self._sync_mix()
        self._refresh_game_page()

    def _sync_mix(self):
        """Саундтреки нашлись — «вперемешку» получает их. При запуске программы, если «вперемешку»
        было включено, подборка собирается здесь: до этого плеер знает только альбом из настроек."""
        mode = self.music.shuffle_mode
        if mode == "off":
            return
        entries = self.library_entries(mode)
        if self.music.mixed:
            self.music.update_mix(entries)
        elif not self._mix_started and entries:
            self._mix_started = True
            self.music.set_shuffle_mode(mode, entries)

    def _on_scan_failed(self, message):
        core.log.warning("Саундтреки Steam не прочитались: %s", message)
        self._finish_scan()
        self.build_sources()

    def _finish_scan(self):
        self._scanning = False
        timing, self._scan_timing = getattr(self, "_scan_timing", None), None
        if timing:
            timing.done("обложки и игры")

    def _refresh_game_page(self):
        game_page = self.win.home_page.game_page
        if game_page.game:
            game_page.update_buttons()  # на открытой карточке может появиться «Слушать» или «Скачать саундтрек»

    def _sync_album_info(self):
        """Название альбома и картинка в плеере — как в списке, а не просто имя папки."""
        playing = self.playing_source()
        if playing and playing["kind"] == "album":
            self.music.set_album(playing["path"], {"name": playing["name"], "image": playing.get("image")})

    # --- Купленные саундтреки ---
    def sync_steam(self):
        """Скачанные альбомы — с диска, купленные — из магазина Steam (нужен вход во встроенном браузере)."""
        if self._syncing:
            return
        self.reload_soundtracks()
        if not QWebEngineView:
            notify("warning", self.win, "Покупки Steam не узнать",
                   "Для этого нужен встроенный браузер (PySide6 с QtWebEngine). Скачанные саундтреки видны и так.")
            return
        self._set_syncing(True, "Узнаю, что куплено…")
        reader = SteamOwnedApps(self)
        reader.finished.connect(self._on_owned_apps)
        reader.failed.connect(self._on_sync_failed)
        reader.start()

    def _set_syncing(self, syncing, text=""):
        """Пока идёт синхронизация, её ход пишется на самой кнопке."""
        self._syncing = syncing
        self.sync_button.setEnabled(not syncing)
        self.sync_button.setText(text if syncing else "Синхронизировать со Steam")

    def _on_owned_apps(self, owned):
        if owned is None:
            self._set_syncing(False)
            core.log.info("Магазин Steam: вход не выполнен — предлагаю войти во встроенном браузере")
            if confirm(self.win, "Войдите в магазин Steam",
                       "Какие саундтреки куплены, Steam показывает только после входа на сайт магазина. "
                       "Войдите один раз во встроенном браузере программы: пароль вводится на сайте Steam, "
                       "программа его не видит. Потом нажмите «К саундтрекам».", "Войти"):
                self._awaiting_login = True
                self.win.switchTo(self.win.home_page)
                self.win.home_page.open_browser(STEAM_LOGIN_URL, "К саундтрекам", self._back_from_login)
            return
        self._set_syncing(True, "Ищу саундтреки среди покупок…")
        worker = Worker(core.owned_soundtracks, owned, parent=self)
        worker.finished.connect(self._on_owned_soundtracks)
        worker.failed.connect(self._on_sync_failed)
        worker.start()

    def _on_owned_soundtracks(self, items):
        self.owned = items
        self._synced = True
        self._set_syncing(False)
        self.build_sources()
        self._refresh_game_page()
        downloaded = {a["appid"] for a in self.albums if a["appid"]}
        missing = sum(item["appid"] not in downloaded for item in items)
        notify("success", self.win, "Синхронизировано со Steam",
               f"Купленных саундтреков: {len(items)}, из них не скачано: {missing}.")

    def _on_sync_failed(self, message):
        core.log.warning("Синхронизация саундтреков со Steam не удалась: %s", message)
        self._set_syncing(False)
        notify("warning", self.win, "Не удалось узнать покупки в Steam", message)

    def _back_from_login(self):
        self.win.switchTo(self)
        if self._awaiting_login:
            self._awaiting_login = False
            QTimer.singleShot(0, self.sync_steam)

    def _check_watched(self, albums):
        """Альбом, который попросили скачать, появился на диске — сообщаем."""
        for album in albums:
            if album["appid"] in self._watch:
                del self._watch[album["appid"]]
                notify("success", self.win, "Саундтрек скачан", f"«{album['name']}» — можно слушать.")
        if not self._watch:
            self.watch_timer.stop()

    def _check_downloads(self):
        now = time.monotonic()
        self._watch = {appid: since for appid, since in self._watch.items() if now - since < self.WATCH_LIMIT}
        if not self._watch:
            self.watch_timer.stop()
            return
        self.reload_soundtracks(quiet=True)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh_bar)
        QTimer.singleShot(0, self._place_backdrop)
        if self._last_scan and time.monotonic() - self._last_scan > self.RESCAN_AFTER:
            self.reload_soundtracks(quiet=True)  # альбом могли скачать, пока программа была открыта

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            self.backdrop.refresh()  # тема сменилась — подложка притемняется или высветляется заново

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._place_backdrop)
        QTimer.singleShot(0, self._refresh_bar)  # название трека подрезается по ширине плеера


# --- Страница «Настройки» ---------------------------------------------------
class SettingsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("settingsPage")
        self.win = window
        settings = self.win.settings

        self.griddb_edit = LineEdit(self)
        self.griddb_edit.setText(core.read_text(core.API_KEY_FILE))
        self.steam_key_edit = PasswordLineEdit(self)
        self.steam_key_edit.setText(settings["steam_api_key"])
        self.steam_id_edit = LineEdit(self)
        self.steam_id_edit.setText(settings["steam_id"])
        self.save_button = PrimaryPushButton("Сохранить ключи", self, FIF.SAVE)
        self.save_button.clicked.connect(self.save_keys)

        keys = QGridLayout()
        keys.setHorizontalSpacing(16)
        keys.setVerticalSpacing(4)
        rows = [
            ("SteamGridDB API key", self.griddb_edit, "steamgriddb.com → Preferences → API (обложки)"),
            ("Steam Web API key", self.steam_key_edit, "steamcommunity.com/dev/apikey (импорт библиотеки)"),
            ("SteamID64", self.steam_id_edit, "17 цифр, например 7656119…; можно узнать на steamid.io"),
        ]
        for row, (label, edit, hint) in enumerate(rows):
            edit.setMinimumWidth(360)
            keys.addWidget(BodyLabel(label, self), row * 2, 0)
            keys.addWidget(edit, row * 2, 1)
            keys.addWidget(CaptionLabel(hint, self), row * 2 + 1, 1)
        keys.setColumnStretch(2, 1)

        # Фоновая музыка: встроенная тема или своя папка/трек. Саундтреки Steam — на своей вкладке.
        # До 2.2 своя папка жила только в music_path — запоминаем её отдельно, чтобы не потерялась
        if (not settings.get("music_custom") and settings["music_path"]
                and not core.is_steam_music(settings["music_path"])):
            core.save_settings(music_custom=settings["music_path"])
        self.music_edit = LineEdit(self)
        self.music_edit.setReadOnly(True)
        self.music_edit.setMinimumWidth(360)
        builtin_button = PushButton("Встроенная тема", self, FIF.HOME)
        builtin_button.clicked.connect(self.play_builtin)
        self.custom_button = PushButton("Своя музыка", self, FIF.PLAY)
        self.custom_button.clicked.connect(self.play_custom)
        choose_file = PushButton("Файл…", self, FIF.MUSIC)
        choose_file.clicked.connect(self.choose_music_file)
        choose_folder = PushButton("Папка…", self, FIF.FOLDER)
        choose_folder.setToolTip("Все треки из папки и подпапок")
        choose_folder.clicked.connect(self.choose_music_folder)
        music_buttons = QHBoxLayout()
        music_buttons.setSpacing(8)
        for button in (builtin_button, self.custom_button, choose_file, choose_folder):
            music_buttons.addWidget(button)
        music_buttons.addStretch(1)

        self.sounds_switch = SwitchButton(self)
        self.sounds_switch.setOnText("Вкл")
        self.sounds_switch.setOffText("Выкл")
        self.sounds_switch.setChecked(settings["sounds_enabled"])
        self.sounds_switch.checkedChanged.connect(self.set_sounds_enabled)

        sound = QGridLayout()
        sound.setHorizontalSpacing(16)
        sound.setVerticalSpacing(8)
        sound.addWidget(BodyLabel("Музыка", self), 0, 0)
        sound.addWidget(self.music_edit, 0, 1)
        sound.addLayout(music_buttons, 1, 1)
        sound.addWidget(CaptionLabel("Саундтреки Steam, громкость и плеер — на вкладке «Саундтреки». "
                                     "Пауза, треки и «вперемешку» — кнопками справа вверху на рулетке.", self),
                        2, 1)
        sound.addWidget(BodyLabel("Звуки барабана", self), 3, 0)
        sound.addWidget(self.sounds_switch, 3, 1)
        sound.setColumnStretch(2, 1)
        self.credit_label = CaptionLabel("Встроенная тема: «Soliloquy» — Matthew Pablo (matthewpablo.com), "
                                         "лицензия CC-BY 3.0, opengameart.org/content/soliloquy", self)
        self.credit_label.setWordWrap(True)
        self.win.music.trackChanged.connect(self.update_music)
        self.update_music()

        self.theme_box = ComboBox(self)
        self.theme_box.addItems(THEME_TITLES)
        theme = settings["theme"] if settings["theme"] in THEME_KEYS else "auto"
        self.theme_box.setCurrentIndex(THEME_KEYS.index(theme))
        self.theme_box.currentIndexChanged.connect(self.change_theme)

        appearance = QHBoxLayout()
        appearance.addWidget(BodyLabel("Тема", self))
        appearance.addSpacing(16)
        appearance.addWidget(self.theme_box)
        appearance.addStretch(1)

        # Где открывать ссылки с карточки игры (Metacritic, отзывы, обзоры)
        self.link_box = ComboBox(self)
        self.link_box.addItems(LINK_MODE_TITLES)
        self.show_link_mode(core.load_settings().get("link_open", "ask"))
        self.link_box.currentIndexChanged.connect(self.change_link_mode)
        links = QHBoxLayout()
        links.addWidget(BodyLabel("Открывать ссылки", self))
        links.addSpacing(16)
        links.addWidget(self.link_box)
        links.addStretch(1)
        links_hint = CaptionLabel("Для ссылок с карточки игры: Metacritic, отзывы Steam, обзоры изданий", self)
        if not QWebEngineView:
            self.link_box.setEnabled(False)
            links_hint.setText("Встроенный браузер недоступен — ссылки открываются во внешнем браузере")

        # Настроек много — кладём их в прокручиваемую область
        content = QWidget()
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("Настройки", self))
        layout.addSpacing(8)
        layout.addWidget(StrongBodyLabel("Ключи", self))
        layout.addLayout(keys)
        layout.addWidget(self.save_button, 0, Qt.AlignLeft)
        layout.addSpacing(16)
        layout.addWidget(StrongBodyLabel("Звук", self))
        layout.addLayout(sound)
        layout.addWidget(self.credit_label)
        layout.addSpacing(16)
        layout.addWidget(StrongBodyLabel("Оформление", self))
        layout.addLayout(appearance)
        layout.addSpacing(16)
        layout.addWidget(StrongBodyLabel("Ссылки", self))
        layout.addLayout(links)
        layout.addWidget(links_hint)
        layout.addStretch(1)

    def set_sounds_enabled(self, enabled):
        self.win.sounds.enabled = enabled
        core.save_settings(sounds_enabled=enabled)
        if enabled:
            self.win.sounds.win()  # пример звука

    # --- Фоновая музыка ---
    def update_music(self):
        music = self.win.music
        custom = core.load_settings().get("music_custom", "")
        if not music.is_custom:
            text = (f"Играет встроенная тема: «{MusicPlayer.BUILTIN_TITLE}»" if music.available
                    else f"Файл {core.MUSIC_NAME} не найден — выберите свой трек или папку")
        elif core.same_path(music.folder, custom):
            text = f"Играет своя музыка: {custom}" + (" (вперемешку со всей музыкой)" if music.mixed else "")
        else:
            text = (f"Играет саундтрек «{music.album.get('name', '')}»"
                    + (" (вперемешку)" if music.mixed else ""))
        self.music_edit.setText(text)
        self.music_edit.setCursorPosition(0)
        exists = bool(custom) and os.path.exists(custom)
        self.custom_button.setEnabled(exists)
        self.custom_button.setToolTip(custom if exists else "Сначала выберите файл или папку")
        self.credit_label.setVisible(not music.is_custom)  # автора указываем, пока играет встроенная тема

    def play_builtin(self):
        core.save_settings(music_path="")
        self._play("")

    def play_custom(self):
        custom = core.load_settings().get("music_custom", "")
        if custom and os.path.exists(custom):
            core.save_settings(music_path=custom)
            self._play(custom)

    def _play(self, path):
        # Своя музыка при включённом «вперемешку: вся музыка» начинает подборку; встроенная тема — всегда одна
        music = self.win.music
        music.play(path, entries=self.win.music_page.library_entries(music.shuffle_mode) if path else None)
        if not music.enabled:
            self.win.set_music_enabled(True)

    def choose_music_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите музыку", "",
            "Аудио (" + " ".join("*" + ext for ext in core.AUDIO_EXTENSIONS) + ")")
        if path:
            self._use_custom(path, os.path.basename(path))

    def choose_music_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку с музыкой")
        if not folder:
            return
        count = len(core.folder_tracks(folder))
        if not count:
            notify("warning", self.win, "В папке нет музыки",
                   "Поддерживаются " + ", ".join(core.AUDIO_EXTENSIONS) + ".")
            return
        self._use_custom(folder, f"Треков в папке: {count}.")

    def _use_custom(self, path, text):
        core.save_settings(music_custom=path)
        self.play_custom()
        notify("success", self.win, "Музыка выбрана", text)

    def save_keys(self):
        griddb_key = self.griddb_edit.text().strip()
        steam_key = self.steam_key_edit.text().strip()
        steam_id = self.steam_id_edit.text().strip()
        if steam_id and not re.fullmatch(r"\d{17}", steam_id):
            notify("warning", self.win, "Неверный SteamID64", "Он должен состоять из 17 цифр.")
            return
        try:
            core.write_text(core.API_KEY_FILE, griddb_key)
            core.save_settings(steam_api_key=steam_key, steam_id=steam_id)
        except OSError as e:
            notify("error", self.win, "Не удалось сохранить настройки", str(e))
            return
        notify("success", self.win, "Ключи сохранены")
        self.win.covers.retry(self.win.library.games)  # с новым ключом могут найтись обложки
        try:
            core.clear_artwork_misses()  # …а также фоны и логотипы для страниц игр
        except OSError:
            pass

    def change_theme(self, index):
        key = THEME_KEYS[index]
        setTheme(THEMES[key])
        core.save_settings(theme=key)
        self.win.home_page.reel.update()  # карточки без обложек перекрашиваются под тему
        QTimer.singleShot(0, self.win.music_page.backdrop.refresh)  # подложка альбома — тоже

    def show_link_mode(self, mode):
        """Показать выбор в списке, не сохраняя его заново (например, после «Больше не спрашивать»)."""
        self.link_box.blockSignals(True)
        self.link_box.setCurrentIndex(LINK_MODES.index(mode) if mode in LINK_MODES else 0)
        self.link_box.blockSignals(False)

    def change_link_mode(self, index):
        core.save_settings(link_open=LINK_MODES[index])


# Где открывать ссылки с карточки игры
LINK_MODES = ("ask", "inside", "outside")
LINK_MODE_TITLES = ("Спрашивать каждый раз", "В программе", "Во внешнем браузере")


# --- Страница «О программе» -------------------------------------------------
class AboutPage(QWidget):
    """Значок, название с версией, автор, лицензия и ссылки на проект."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("aboutPage")
        self.win = window

        icon = QLabel(self)
        icon_path = core.find_resource(core.ICON_NAME)
        if icon_path:
            icon.setPixmap(QIcon(icon_path).pixmap(96, 96))
        icon.setFixedSize(96, 96)

        name = SubtitleLabel(f"{APP_NAME} {__version__}", self)
        about = BodyLabel("Рандомайзер «Во что поиграть дальше?» для библиотеки Steam: крутите барабан "
                          "с обложками — он сам выберет, во что играть.", self)
        about.setWordWrap(True)
        author = BodyLabel(f"Автор: {AUTHOR} ({AUTHOR_NICK})", self)
        rights = CaptionLabel(f"{COPYRIGHT}. Свободная программа, распространяется по лицензии "
                              f"{LICENSE_NAME}. Steam — торговая марка Valve Corporation; "
                              f"программа не связана с Valve.", self)
        rights.setWordWrap(True)

        github = PushButton("Страница проекта", self, FIF.GITHUB)
        github.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPO_URL)))
        issues = PushButton("Сообщить об ошибке", self, FIF.FEEDBACK)
        issues.setToolTip("Откроется GitHub. Приложите логи: вкладка «Логи» → «Копировать всё»")
        issues.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPO_URL + "/issues")))
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(github)
        buttons.addWidget(issues)
        buttons.addStretch(1)

        text = QVBoxLayout()
        text.setSpacing(4)
        for widget in (name, about, author, rights):
            text.addWidget(widget)
        text.addSpacing(6)
        text.addLayout(buttons)
        card = QHBoxLayout()
        card.setSpacing(20)
        card.addWidget(icon, 0, Qt.AlignTop)
        card.addLayout(text, 1)
        card_box = QWidget(self)
        card_box.setLayout(card)
        card_box.setMaximumWidth(760)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("О программе", self))
        layout.addSpacing(8)
        layout.addWidget(card_box)
        layout.addStretch(1)


# --- Страница «Логи» --------------------------------------------------------
class LogsPage(QWidget):
    """Журнал работы программы: что делалось и что сломалось. Ключи API в нём скрыты."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("logsPage")
        self.win = window

        self.view = PlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(PlainTextEdit.NoWrap)
        font = QFont("Consolas")
        font.setPointSize(9)
        self.view.setFont(font)

        refresh = PrimaryPushButton("Обновить", self, FIF.SYNC)
        refresh.clicked.connect(self.refresh)
        copy = PushButton("Копировать всё", self, FIF.COPY)
        copy.clicked.connect(self.copy_all)
        open_folder = PushButton("Открыть папку", self, FIF.FOLDER)
        open_folder.clicked.connect(self.open_folder)
        clear = PushButton("Начать заново", self, FIF.DELETE)
        clear.setToolTip("Текущий журнал переносится в app.log.1, он не теряется")
        clear.clicked.connect(self.clear)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        for button in (refresh, copy, open_folder):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(clear)

        hint = CaptionLabel("Если что-то сломалось — нажмите «Копировать всё» и пришлите текст. "
                            "Ключи API в логах заменены на ***.", self)
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("Логи", self))
        layout.addWidget(hint)
        layout.addWidget(self.view, 1)
        layout.addLayout(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        text = core.read_log()
        self.view.setPlainText(text or "Логи пока пусты.")
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())  # самые свежие записи — внизу

    def copy_all(self):
        QApplication.clipboard().setText(core.read_log())
        notify("success", self.win, "Логи скопированы", "Можно вставить их в сообщение.")

    def open_folder(self):
        os.makedirs(core.LOGS_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(core.LOGS_DIR))

    def clear(self):
        if not confirm(self.win, "Начать журнал заново?",
                       "Текущие записи сохранятся в файле logs\\app.log.1, а здесь журнал начнётся с чистого листа.",
                       "Начать заново"):
            return
        try:
            core.clear_log()
        except OSError as e:
            notify("error", self.win, "Не удалось начать журнал заново", str(e))
            return
        core.log.info("Журнал начат заново, прошлые записи — в app.log.1")
        self.refresh()


# --- Главное окно -----------------------------------------------------------
class MainWindow(MSFluentWindow):
    libraryChanged = Signal()

    def __init__(self, startup=None):
        super().__init__()
        startup = startup or Timing("окно")
        if QWebEngineView:
            # Встроенный браузер переводит окно на отрисовку через видеокарту, и тогда прозрачные места
            # под эффектом Mica становятся чёрными: в светлой теме чернеют меню и фон страниц.
            # Поэтому с браузером окно рисует обычный сплошной фон своей темы.
            self.setMicaEffectEnabled(False)
        self.settings = core.load_settings()
        self.library = core.GameLibrary()
        self.load_message = self.library.load()

        self.music = MusicPlayer(self.settings["music_path"], self.settings["music_shuffle_mode"],
                                 self.settings["music_repeat"] == "one", self)
        self.music.set_volume(int(self.settings["music_volume"]))
        self.sounds = ReelSounds(bool(self.settings["sounds_enabled"]), self)
        self.covers = CoverPrefetcher(self)
        startup.mark("список игр и музыка")

        self.home_page = HomePage(self)
        self.library_page = LibraryPage(self)
        self.music_page = MusicPage(self)
        self.settings_page = SettingsPage(self)
        self.logs_page = LogsPage(self)
        self.about_page = AboutPage(self)
        startup.mark("вкладки")
        self.addSubInterface(self.home_page, FIF.GAME, "Рулетка")
        self.addSubInterface(self.library_page, FIF.LIBRARY, "Игры")
        self.addSubInterface(self.music_page, FIF.MUSIC, "Саундтреки")
        self.addSubInterface(self.logs_page, FIF.DOCUMENT, "Логи",
                             position=NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.settings_page, FIF.SETTING, "Настройки",
                             position=NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.about_page, FIF.INFO, "О программе",
                             position=NavigationItemPosition.BOTTOM)
        self._fit_navigation()
        if self.home_page.browser:
            self.home_page.browser.preload(self)  # до показа окна — иначе оно мигнёт при первой ссылке
            startup.mark("браузер")
        self.libraryChanged.connect(self.on_library_changed)
        self.stackedWidget.currentChanged.connect(self._time_tab_switch)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        # Заголовок Fluent рисует значок сам и берёт его только у окна, не у приложения
        if not QApplication.windowIcon().isNull():
            self.setWindowIcon(QApplication.windowIcon())
        self.resize(980, 720)
        self.setMinimumSize(780, 640)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2, screen.center().y() - self.height() // 2)

        # Если обложки скачаны не все — сначала экран загрузки, барабан потом
        if self.covers.enqueue(self.library.games):
            self.home_page.show_loading()
        self.music_page.reload_soundtracks()
        QTimer.singleShot(300, self._after_show)

    def _time_tab_switch(self, index):
        """Замер переключения вкладки: от смены до момента, когда окно снова свободно (после её showEvent)."""
        page = self.stackedWidget.widget(index)
        key = page.objectName() if page else str(index)
        item = getattr(self.navigationInterface, "items", {}).get(key)  # подпись в меню: «Саундтреки», …
        timing = Timing(f"вкладка «{item.text() if item else key}»")
        QTimer.singleShot(0, lambda: timing.done("показана"))

    def _fit_navigation(self):
        """Боковое меню — по самой длинной подписи, чтобы «О программе» не обрезалось."""
        nav = self.navigationInterface
        buttons = nav.buttons() if hasattr(nav, "buttons") else []
        if not buttons:
            return
        width = max([64] + [QFontMetrics(b.font()).horizontalAdvance(b.text()) + 14 for b in buttons])
        for button in buttons:
            button.setFixedSize(width, button.height())
        nav.setFixedWidth(width + 8)  # по 4 точки отступа с каждой стороны

    def _after_show(self):
        if self.load_message:
            notify("info", self, "Список игр", self.load_message, duration=8000)
        self.set_music_enabled(bool(self.settings["music_enabled"]), save=False)
        if self.home_page.stack.currentWidget() is self.home_page.reel_view:
            self.home_page.reel.play_intro()

    def on_library_changed(self):
        if self.covers.enqueue(self.library.games) >= LOADING_THRESHOLD:
            self.home_page.show_loading()

    def save_library(self):
        try:
            self.library.save()
            return True
        except OSError as e:
            notify("error", self, "Не удалось сохранить список игр", str(e))
            return False

    def open_in_steam(self, url, game=None, action="store"):
        """Открывает ссылку Steam в фоне и показывает окно ожидания, пока Steam не откликнется."""
        dialog = SteamActionDialog(self, game, action)
        dialog.show()
        self._steam_dialog = dialog
        worker = Worker(core.open_url, url, parent=self)
        worker.finished.connect(self._on_steam_delivered)
        worker.failed.connect(self._on_steam_failed)
        worker.start()

    def _on_steam_delivered(self, _result):
        try:
            self._steam_dialog.delivered()
        except RuntimeError:
            pass  # окно уже закрыто

    def _on_steam_failed(self, message):
        notify("error", self, "Не удалось открыть Steam", "Проверьте, что Steam установлен.")
        try:
            self._steam_dialog.fail(message)
        except RuntimeError:
            pass  # окно уже закрыто

    def launch_game(self, game):
        """Steam сам запустит игру или, если она не установлена, откроет окно установки."""
        appid = game.get("appid") if game else None
        if not appid:
            return
        installed = core.is_installed(appid)
        core.log.info("%s «%s» (%s)", "Запуск" if installed else "Установка", game["name"], appid)
        if installed:
            self.open_in_steam(f"steam://run/{appid}", game, "run")
        else:
            self.open_in_steam(f"steam://install/{appid}", game, "install")

    def set_music_enabled(self, enabled, save=True):
        self.music.set_enabled(enabled)  # кнопки плееров обновятся по сигналу stateChanged
        if save:
            core.save_settings(music_enabled=enabled)

    def toggle_music(self):
        self.set_music_enabled(not self.music.enabled)

    def show_playing_music(self):
        """Нота в мини-плеере: вкладка «Саундтреки», открытая на альбоме, который играет."""
        playing = self.music_page.playing_source()
        if playing:
            self.music_page.select_source(playing)
        self.switchTo(self.music_page)

    def play_pause(self):
        """Кнопка ▶/❚❚: выключенную музыку включает, иначе ставит на паузу или продолжает."""
        if not self.music.enabled:
            self.set_music_enabled(True)
        else:
            self.music.set_paused(not self.music.paused)

    def cycle_shuffle(self):
        """Кнопка «вперемешку» в любом плеере: выключено → вся музыка → только саундтреки → выключено."""
        modes = MusicPlayer.SHUFFLE_MODES
        mode = modes[(modes.index(self.music.shuffle_mode) + 1) % len(modes)]
        core.save_settings(music_shuffle_mode=mode)
        entries = self.music_page.library_entries(mode) if mode != "off" else None
        self.music.set_shuffle_mode(mode, entries)
        if mode != "off" and not entries:
            notify("info", self, "Перемешивать пока нечего",
                   "Скачанных саундтреков нет" + (" и своя музыка не выбрана" if mode == "all" else "")
                   + " — перемешиваю то, что играет.")
        if mode != "off" and not self.music.enabled:
            self.set_music_enabled(True)

    def toggle_repeat(self):
        """Кнопка «повтор трека» в любом плеере. Остальные плееры обновятся по сигналу stateChanged."""
        repeat_one = not self.music.repeat_one
        core.save_settings(music_repeat="one" if repeat_one else "all")
        self.music.set_repeat_one(repeat_one)


def install_error_hooks(window):
    """Любая непойманная ошибка пишется в лог, а пользователь видит уведомление вместо молчаливого сбоя."""

    def on_error(exc_type, exc, tb):
        core.log.critical("Непредвиденная ошибка", exc_info=(exc_type, exc, tb))
        if threading.current_thread() is threading.main_thread():
            notify("error", window, "Что-то пошло не так",
                   f"{exc_type.__name__}: {core.mask_secrets(exc)}\nПодробности — на вкладке «Логи».", duration=10000)

    sys.excepthook = on_error
    threading.excepthook = lambda args: core.log.critical(
        "Непредвиденная ошибка в фоновом потоке",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def choose_media_backend():
    """Включает проигрыватель FFmpeg, если он есть рядом с PySide6.

    В Windows у Qt два проигрывателя: встроенный в Windows и FFmpeg. Новые трейлеры Steam идут только
    потоком HLS (1080p и ниже) — его открывает лишь FFmpeg. Встроенный отвечает «Unsupported media type»,
    и остаётся только старый маленький mp4. Ставить переменную нужно до первого QMediaPlayer.
    """
    import PySide6
    folder = os.path.join(os.path.dirname(PySide6.__file__), "plugins", "multimedia")
    try:
        plugins = sorted(f for f in os.listdir(folder) if f.lower().endswith(".dll"))
    except OSError:
        plugins = []
    has_ffmpeg = any(f.lower().startswith("ffmpegmediaplugin") for f in plugins)
    if has_ffmpeg and not os.environ.get("QT_MEDIA_BACKEND"):
        os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"
    core.log.info("PySide6 %s, проигрыватели: %s, выбран: %s", PySide6.__version__,
                  ", ".join(plugins) or "не найдены", os.environ.get("QT_MEDIA_BACKEND", "по умолчанию"))
    if not has_ffmpeg:
        core.log.warning("Проигрыватель FFmpeg не найден — трейлеры будут в низком качестве. "
                         "Переустановите PySide6 или пересоберите программу через build.bat")


def main():
    startup = Timing("запуск", start=LAUNCHED)
    core.setup_logging()
    startup.mark("модули")
    core.log.info("Запуск %s %s (Python %s) — %s", APP_NAME, __version__, sys.version.split()[0], COPYRIGHT)
    choose_media_backend()
    if sys.platform == "win32":
        # Без этого Windows при запуске из .py показывает на панели задач значок Python
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(AUTHOR)
    install_hang_watchdog(app)
    icon = core.find_resource(core.ICON_NAME)
    if icon:
        app.setWindowIcon(QIcon(icon))  # для главного окна и всех диалогов
    setTheme(THEMES.get(core.load_settings()["theme"], Theme.AUTO))
    startup.mark("Qt")
    window = MainWindow(startup)
    install_error_hooks(window)
    window.show()
    QTimer.singleShot(0, lambda: startup.done("окно на экране"))  # сработает, когда окно уже нарисовано
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
