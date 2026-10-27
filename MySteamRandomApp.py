"""Backlog Roulette — рандомайзер «Во что сыграть дальше?» для библиотеки Steam.

Интерфейс в стиле Windows 11 (PySide6 + PySide6-Fluent-Widgets), логика — в core.py.
"""

import os
import random
import re
import sys
import threading
import time

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QObject, QPropertyAnimation, QRectF,
                            Qt, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QListWidgetItem, QStackedWidget, QVBoxLayout,
                               QWidget)
from qfluentwidgets import (Action, BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon as FIF, InfoBar,
                            IndeterminateProgressBar, InfoBarPosition, LineEdit, ListWidget, MessageBox,
                            MessageBoxBase,
                            MSFluentWindow, NavigationItemPosition, PasswordLineEdit, PlainTextEdit,
                            PrimaryPushButton, ProgressBar, PushButton, RoundMenu, ScrollArea,
                            SearchLineEdit, Slider, StrongBodyLabel,
                            SubtitleLabel, SwitchButton, TextEdit, Theme, TitleLabel, TransparentToolButton,
                            isDarkTheme, setTheme, themeColor)

import core

__version__ = "2.0.6"
APP_NAME = "Backlog Roulette"
APP_ID = "byRedFox.BacklogRoulette"  # для Windows: своя иконка и группа на панели задач

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


def notify(kind, window, title, text="", duration=3500):
    """Всплывающее уведомление: kind = success | info | warning | error."""
    getattr(InfoBar, kind)(title, text, orient=Qt.Horizontal, isClosable=True,
                           duration=duration, position=InfoBarPosition.TOP, parent=window)


def confirm(window, title, text, yes="Да"):
    box = MessageBox(title, text, window)
    box.yesButton.setText(yes)
    box.cancelButton.setText("Отмена")
    return bool(box.exec())


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
    """Фоновая музыка с плавным появлением и затуханием.

    Источник — встроенный трек, свой файл (играет по кругу) или папка: тогда треки
    перемешиваются при каждом запуске и играют по очереди.
    """

    trackChanged = Signal()

    CROSSFADE_MS = 4000  # сколько секунд конца трека звучит вместе с началом следующего
    FADE_IN_MS = 1500  # появление музыки при запуске и ручном переключении

    def __init__(self, custom_path="", parent=None):
        super().__init__(parent)
        self.default_path = core.find_resource(core.MUSIC_NAME)
        self.path = None
        self.is_custom = False
        self.playlist, self.index, self.folder = [], 0, None
        self._errors_in_row = 0
        self.enabled = False
        self.volume = 0.4
        self._crossfading = False  # следующий трек уже запущен поверх доигрывающего
        self._loaded_at = 0.0  # когда запущен текущий трек
        self._stopped_checks = 0

        # Два проигрывателя, как деки у диджея: пока один доигрывает, второй уже начинает следующий трек
        self.decks = [self._make_deck(), self._make_deck()]
        self.current = 0

        # Страховка: если музыка остановилась, а сигнала о конце трека не было — включаем следующий
        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self._check_stopped)
        self.watchdog.start(3000)
        self.set_source(custom_path)

    def _make_deck(self):
        audio = QAudioOutput(self)
        player = QMediaPlayer(self)
        player.setAudioOutput(audio)
        fade = QPropertyAnimation(audio, b"volume", self)
        deck = {"player": player, "audio": audio, "fade": fade}
        player.mediaStatusChanged.connect(lambda status: self._on_status(deck, status))
        player.errorOccurred.connect(lambda error, message="": self._on_error(deck, error, message))
        player.positionChanged.connect(lambda position: self._on_position(deck, position))
        fade.finished.connect(lambda: self._on_fade_finished(deck))
        return deck

    @property
    def deck(self):
        return self.decks[self.current]

    @property
    def player(self):
        return self.deck["player"]

    @property
    def available(self):
        return self.path is not None

    def set_source(self, custom_path):
        """Папка, свой файл или (если пусто или файла уже нет) встроенный apptheme.mp3."""
        self.playlist, self.index, self.folder = [], 0, None
        if custom_path and os.path.isdir(custom_path):
            files = core.list_audio_files(custom_path)
            if files:
                random.shuffle(files)
                self.playlist, self.folder = files, custom_path
            else:
                core.log.warning("В папке с музыкой нет аудиофайлов: %s", custom_path)
        self.is_custom = bool(self.playlist) or (bool(custom_path) and os.path.isfile(custom_path))
        if self.playlist:
            path = self.playlist[0]
        else:
            path = custom_path if self.is_custom else self.default_path
        self._start(path, crossfade=False)

    def next_track(self, crossfade=False):
        if not self.playlist:
            return
        self.index += 1
        if self.index >= len(self.playlist):  # круг пройден — перемешиваем заново
            random.shuffle(self.playlist)
            self.index = 0
        self._start(self.playlist[self.index], crossfade)

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
        self._crossfading = False
        self._loaded_at = time.monotonic()
        self._stopped_checks = 0
        # Один трек играет по кругу, плейлист — по очереди
        new["player"].setLoops(1 if self.playlist else -1)  # -1 = QMediaPlayer.Loops.Infinite
        new["player"].setSource(QUrl.fromLocalFile(path) if path else QUrl())
        if path:
            core.log.info("Музыка%s: %s", " (кроссфейд)" if crossfade else "", path)
        if self.enabled and path:
            new["audio"].setVolume(0.0)
            new["player"].play()
            self._fade(new, self.volume, self.CROSSFADE_MS if crossfade else self.FADE_IN_MS)
        self.trackChanged.emit()

    def _on_position(self, deck, position):
        # За 4 секунды до конца запускаем следующий трек поверх текущего
        if deck is not self.deck or not (self.playlist and self.enabled) or self._crossfading:
            return
        duration = deck["player"].duration()
        if duration > self.CROSSFADE_MS * 3 and duration - position < self.CROSSFADE_MS:
            self._crossfading = True
            self._errors_in_row = 0
            self.next_track(crossfade=True)

    def _on_status(self, deck, status):
        # Короткий трек (или неточная длина mp3) закончился раньше кроссфейда — просто включаем следующий
        if deck is self.deck and status == QMediaPlayer.EndOfMedia and self.playlist:
            self._errors_in_row = 0
            self.next_track()

    def _check_stopped(self):
        # Срабатывает, только если текущий трек стоит две проверки подряд и прошло больше 10 секунд с его старта
        if not (self.enabled and self.playlist) or time.monotonic() - self._loaded_at < 10:
            self._stopped_checks = 0
            return
        stopped = self.player.playbackState() == QMediaPlayer.StoppedState
        self._stopped_checks = self._stopped_checks + 1 if stopped else 0
        if self._stopped_checks >= 2:
            core.log.info("Музыка остановилась без сигнала о конце трека — включаю следующий")
            self.next_track()

    def _on_error(self, deck, error, message=""):
        if deck is not self.deck:
            return
        core.log.warning("Не удалось воспроизвести %s: %s", self.path, message or error)
        # Битый файл в папке пропускаем, но не крутимся бесконечно, если битые все
        self._errors_in_row += 1
        if self.playlist and self._errors_in_row < len(self.playlist):
            QTimer.singleShot(0, self.next_track)

    def set_volume(self, percent):
        self.volume = percent / 100
        if not self.enabled:
            return
        fade = self.deck["fade"]
        if fade.state() == QAbstractAnimation.Running:
            fade.setEndValue(self.volume)
        else:
            self.deck["audio"].setVolume(self.volume)

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not self.available:
            return
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
        elif not self.enabled:
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


# --- Барабан с обложками ----------------------------------------------------
class CoverReel(QWidget):
    """Лента обложек: быстро прокручивается и плавно останавливается на выбранной игре."""

    spinFinished = Signal(dict)
    cardPassed = Signal()  # очередная карточка прошла через центр — для щелчка

    CARD_W, CARD_H, GAP = 200, 300, 28
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

        # Дальние карточки рисуем первыми, центральную — поверх остальных
        for distance, x, game in sorted(cards, key=lambda card: -card[0]):
            scale = max(0.72, 1 - 0.14 * distance)
            w, h = self.CARD_W * scale, self.CARD_H * scale
            # При появлении карточки всплывают снизу: сначала центральная, потом боковые
            lag = min(1.0, max(0.0, self.intro * 1.6 - distance * 0.2))
            y = cy - h / 2 + (1 - lag) * 70
            painter.setOpacity(max(0.2, 1 - 0.32 * distance) * lag)
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


# --- Страница «Рулетка» -----------------------------------------------------
class HomePage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("homePage")
        self.win = window
        self.current = None

        self.title = TitleLabel("Во что сыграть дальше?", self)
        self.music_button = TransparentToolButton(FIF.MUSIC, self)
        self.music_button.setToolTip("Музыка вкл/выкл")
        self.music_button.clicked.connect(self.win.toggle_music)
        self.next_track_button = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.next_track_button.clicked.connect(self.win.music.next_track)
        self.win.music.trackChanged.connect(self.update_track_button)
        self.count_label = CaptionLabel("", self)

        header = QHBoxLayout()
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.music_button)
        header.addWidget(self.next_track_button)
        self.update_track_button()

        self.stack = QStackedWidget(self)
        self.loading_view = self._build_loading_view()
        self.reel_view = self._build_reel_view()
        self.stack.addWidget(self.loading_view)
        self.stack.addWidget(self.reel_view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self.count_label)
        layout.addWidget(self.stack, 1)

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
        self.spin_button.setMinimumWidth(180)
        self.spin_button.clicked.connect(self.spin)
        self.play_button = PushButton("Играть", view, FIF.PLAY)
        self.play_button.clicked.connect(self.play)
        self.done_button = PushButton("Пройдена", view, FIF.ACCEPT)
        self.done_button.clicked.connect(self.mark_done)
        self.cover_button = PushButton("Своя обложка", view, FIF.PHOTO)
        self.cover_button.clicked.connect(self.choose_cover)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        for button in (self.spin_button, self.play_button, self.done_button, self.cover_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addStretch(1)
        layout.addWidget(self.reel)
        layout.addWidget(self.name_label)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return view

    # --- Экран загрузки обложек ---
    def show_loading(self):
        if self.reel.is_spinning():
            return
        self.stack.setCurrentWidget(self.loading_view)

    def show_reel(self):
        if self.stack.currentWidget() is self.reel_view:
            return
        if self.current is None:
            self.reel.show_idle(self.win.library.games)
        self.stack.setCurrentWidget(self.reel_view)
        self.reel.play_intro()

    def on_cover_progress(self, done, total):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_label.setText(f"{done} из {total}")

    def on_covers_finished(self):
        if self.stack.currentWidget() is self.loading_view:
            # Небольшая пауза, чтобы полоса успела дойти до конца
            QTimer.singleShot(400, self.show_reel)

    # --- Барабан ---
    def set_music_icon(self, enabled):
        self.music_button.setIcon(FIF.MUSIC if enabled else FIF.MUTE)

    def update_track_button(self):
        music = self.win.music
        self.next_track_button.setVisible(bool(music.playlist))
        if music.playlist:
            self.next_track_button.setToolTip(f"Следующий трек (сейчас: {os.path.basename(music.path)})")

    def show_game(self, game):
        """Показать игру на барабане без прокрутки — например, выбранную в списке."""
        if self.reel.is_spinning():
            return
        self.reel.show_idle(self.win.library.games, center=game)
        self.current = game
        self.show_name(game["name"])
        self.update_buttons()
        self.show_reel()
        if not core.find_cover(game["name"]):
            self.win.covers.enqueue([game], front=True)

    def update_count(self):
        self.count_label.setText(f"Игр в списке: {len(self.win.library.games)}")

    def update_buttons(self):
        spinning = self.reel.is_spinning()
        selected = not spinning and self.current is not None
        self.spin_button.setEnabled(not spinning)
        self.done_button.setEnabled(selected)
        self.cover_button.setEnabled(selected)

        appid = self.current.get("appid") if self.current else None
        self.play_button.setEnabled(selected and bool(appid))
        if appid and not core.is_installed(appid):
            self.play_button.setText("Установить")
            self.play_button.setIcon(FIF.DOWNLOAD)
        else:
            self.play_button.setText("Играть")
            self.play_button.setIcon(FIF.PLAY)
        self.play_button.setToolTip("" if appid or not self.current else
                                    "Игра добавлена вручную, без Steam ID — запустить её из Steam нельзя")

    def play(self):
        self.win.launch_game(self.current)
        self.update_buttons()

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

        show = Action(FIF.GAME, "Показать на рулетке", menu)
        show.triggered.connect(lambda: self.show_on_reel(game))
        menu.addAction(show)
        menu.addSeparator()
        count = len(self.list.selectedItems())
        delete = Action(FIF.DELETE, "Удалить из списка" if count == 1 else f"Удалить выбранные ({count})", menu)
        delete.triggered.connect(self.delete_selected)
        menu.addAction(delete)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def show_on_reel(self, game):
        self.win.switchTo(self.win.home_page)
        self.win.home_page.show_game(game)

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

        self.music_switch = SwitchButton(self)
        self.music_switch.setOnText("Вкл")
        self.music_switch.setOffText("Выкл")
        self.music_switch.setChecked(settings["music_enabled"])
        self.music_switch.checkedChanged.connect(self.win.set_music_enabled)
        self.volume_slider = Slider(Qt.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(settings["music_volume"]))
        self.volume_slider.setFixedWidth(240)
        self.volume_slider.valueChanged.connect(self.win.music.set_volume)
        self.volume_slider.sliderReleased.connect(self.save_volume)

        self.music_path_edit = LineEdit(self)
        self.music_path_edit.setReadOnly(True)
        self.music_path_edit.setMinimumWidth(360)
        choose_music = PushButton("Файл…", self, FIF.MUSIC)
        choose_music.clicked.connect(self.choose_music)
        choose_folder = PushButton("Папка…", self, FIF.FOLDER)
        choose_folder.setToolTip("Все треки из папки и подпапок — в случайном порядке при каждом запуске")
        choose_folder.clicked.connect(self.choose_music_folder)
        reset_music = PushButton("Встроенная", self, FIF.HOME)
        reset_music.clicked.connect(self.reset_music)
        self.next_track_button = PushButton("Следующий трек", self, FIF.RIGHT_ARROW)
        self.next_track_button.clicked.connect(self.win.music.next_track)
        track_buttons = QHBoxLayout()
        track_buttons.setSpacing(8)
        for button in (choose_music, choose_folder, reset_music, self.next_track_button):
            track_buttons.addWidget(button)
        track_buttons.addStretch(1)

        self.sounds_switch = SwitchButton(self)
        self.sounds_switch.setOnText("Вкл")
        self.sounds_switch.setOffText("Выкл")
        self.sounds_switch.setChecked(settings["sounds_enabled"])
        self.sounds_switch.checkedChanged.connect(self.set_sounds_enabled)

        sound = QGridLayout()
        sound.setHorizontalSpacing(16)
        sound.setVerticalSpacing(8)
        sound.addWidget(BodyLabel("Музыка", self), 0, 0)
        sound.addWidget(self.music_switch, 0, 1)
        sound.addWidget(BodyLabel("Громкость", self), 1, 0)
        sound.addWidget(self.volume_slider, 1, 1)
        sound.addWidget(BodyLabel("Трек", self), 2, 0)
        sound.addWidget(self.music_path_edit, 2, 1)
        sound.addLayout(track_buttons, 3, 1)
        sound.addWidget(BodyLabel("Звуки барабана", self), 4, 0)
        sound.addWidget(self.sounds_switch, 4, 1)
        sound.setColumnStretch(2, 1)

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
        layout.addSpacing(16)
        layout.addWidget(StrongBodyLabel("Оформление", self))
        layout.addLayout(appearance)
        layout.addStretch(1)
        layout.addWidget(CaptionLabel(f"{APP_NAME} {__version__} — рандомайзер для библиотеки Steam", self))
        self.credit_label = CaptionLabel("Музыка: «Soliloquy» — Matthew Pablo (matthewpablo.com), "
                                         "лицензия CC-BY 3.0, opengameart.org/content/soliloquy", self)
        layout.addWidget(self.credit_label)
        self.win.music.trackChanged.connect(self.update_music_path)
        self.update_music_path()

    def update_music_path(self):
        music = self.win.music
        if music.playlist:
            text = (f"Папка: {music.folder} — трек {music.index + 1} из {len(music.playlist)}: "
                    f"{os.path.basename(music.path)}")
        elif music.is_custom:
            text = music.path
        elif music.available:
            text = "Встроенная: «Soliloquy» — Matthew Pablo"
        else:
            text = f"Файл {core.MUSIC_NAME} не найден — выберите свой трек"
        self.music_path_edit.setText(text)
        self.music_path_edit.setCursorPosition(0)
        self.next_track_button.setVisible(bool(music.playlist))
        self.credit_label.setVisible(not music.is_custom)  # автор указывается, пока играет встроенный трек

    def choose_music(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите музыку", "",
            "Аудио (" + " ".join("*" + ext for ext in core.AUDIO_EXTENSIONS) + ")")
        if path:
            self._set_music(path, os.path.basename(path))

    def choose_music_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку с музыкой")
        if not folder:
            return
        count = len(core.list_audio_files(folder))
        if not count:
            notify("warning", self.win, "В папке нет музыки",
                   "Поддерживаются " + ", ".join(core.AUDIO_EXTENSIONS) + ".")
            return
        self._set_music(folder, f"Треков в папке: {count}. Порядок перемешивается при каждом запуске.")

    def _set_music(self, path, text):
        core.save_settings(music_path=path)
        self.win.music.set_source(path)
        notify("success", self.win, "Музыка выбрана", text)

    def reset_music(self):
        core.save_settings(music_path="")
        self.win.music.set_source("")

    def set_sounds_enabled(self, enabled):
        self.win.sounds.enabled = enabled
        core.save_settings(sounds_enabled=enabled)
        if enabled:
            self.win.sounds.win()  # пример звука

    def set_music_switch(self, enabled):
        self.music_switch.blockSignals(True)
        self.music_switch.setChecked(enabled)
        self.music_switch.blockSignals(False)

    def save_volume(self):
        core.save_settings(music_volume=self.volume_slider.value())

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

    def change_theme(self, index):
        key = THEME_KEYS[index]
        setTheme(THEMES[key])
        core.save_settings(theme=key)
        self.win.home_page.reel.update()  # карточки без обложек перекрашиваются под тему


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
        clear = PushButton("Очистить", self, FIF.DELETE)
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
        if not confirm(self.win, "Очистить логи?", "Все записи журнала будут удалены.", "Очистить"):
            return
        try:
            core.clear_log()
        except OSError as e:
            notify("error", self.win, "Не удалось очистить логи", str(e))
            return
        core.log.info("Логи очищены")
        self.refresh()


# --- Главное окно -----------------------------------------------------------
class MainWindow(MSFluentWindow):
    libraryChanged = Signal()

    def __init__(self):
        super().__init__()
        self.settings = core.load_settings()
        self.library = core.GameLibrary()
        self.load_message = self.library.load()

        self.music = MusicPlayer(self.settings["music_path"], self)
        self.music.set_volume(int(self.settings["music_volume"]))
        self.sounds = ReelSounds(bool(self.settings["sounds_enabled"]), self)
        self.covers = CoverPrefetcher(self)

        self.home_page = HomePage(self)
        self.library_page = LibraryPage(self)
        self.settings_page = SettingsPage(self)
        self.logs_page = LogsPage(self)
        self.addSubInterface(self.home_page, FIF.GAME, "Рулетка")
        self.addSubInterface(self.library_page, FIF.LIBRARY, "Игры")
        self.addSubInterface(self.logs_page, FIF.DOCUMENT, "Логи",
                             position=NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.settings_page, FIF.SETTING, "Настройки",
                             position=NavigationItemPosition.BOTTOM)
        self.libraryChanged.connect(self.on_library_changed)

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
        QTimer.singleShot(300, self._after_show)

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
        self.music.set_enabled(enabled)
        self.home_page.set_music_icon(enabled)
        self.settings_page.set_music_switch(enabled)
        if save:
            core.save_settings(music_enabled=enabled)

    def toggle_music(self):
        self.set_music_enabled(not self.music.enabled)


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


def main():
    core.setup_logging()
    core.log.info("Запуск %s %s (Python %s)", APP_NAME, __version__, sys.version.split()[0])
    if sys.platform == "win32":
        # Без этого Windows при запуске из .py показывает на панели задач значок Python
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = core.find_resource(core.ICON_NAME)
    if icon:
        app.setWindowIcon(QIcon(icon))  # для главного окна и всех диалогов
    setTheme(THEMES.get(core.load_settings()["theme"], Theme.AUTO))
    window = MainWindow()
    install_error_hooks(window)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
