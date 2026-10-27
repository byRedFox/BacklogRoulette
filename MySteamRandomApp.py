"""My Steam Random — рандомайзер «Во что сыграть дальше?» для библиотеки Steam.

Интерфейс в стиле Windows 11 (PySide6 + PySide6-Fluent-Widgets), логика — в core.py.
"""

import random
import re
import sys
import threading

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QObject, QPropertyAnimation, QRectF,
                            Qt, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QListWidgetItem, QStackedWidget, QVBoxLayout,
                               QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon as FIF, InfoBar,
                            InfoBarPosition, LineEdit, ListWidget, MessageBox, MessageBoxBase,
                            MSFluentWindow, NavigationItemPosition, PasswordLineEdit, PrimaryPushButton,
                            ProgressBar, PushButton, ScrollArea, SearchLineEdit, Slider, StrongBodyLabel,
                            SubtitleLabel, SwitchButton, TextEdit, Theme, TitleLabel, TransparentToolButton,
                            isDarkTheme, setTheme, themeColor)

import core

__version__ = "2.0"

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
            self.failed.emit(str(e))
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
    """Фоновая мелодия по кругу с плавным появлением и затуханием."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = core.find_resource(core.MUSIC_NAME)
        self.enabled = False
        self.volume = 0.4

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setLoops(-1)  # QMediaPlayer.Loops.Infinite — играть бесконечно
        if self.path:
            self.player.setSource(QUrl.fromLocalFile(self.path))

        self.fade = QPropertyAnimation(self.audio, b"volume", self)
        self.fade.finished.connect(self._on_fade_finished)

    @property
    def available(self):
        return self.path is not None

    def set_volume(self, percent):
        self.volume = percent / 100
        if not self.enabled:
            return
        if self.fade.state() == QAbstractAnimation.Running:
            self.fade.setEndValue(self.volume)
        else:
            self.audio.setVolume(self.volume)

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not self.available:
            return
        if enabled:
            if self.player.playbackState() != QMediaPlayer.PlayingState:
                self.audio.setVolume(0.0)
                self.player.play()
            self._fade_to(self.volume, 1500)
        else:
            self._fade_to(0.0, 600)

    def _fade_to(self, value, duration):
        self.fade.stop()
        self.fade.setDuration(duration)
        self.fade.setStartValue(self.audio.volume())
        self.fade.setEndValue(float(value))
        self.fade.start()

    def _on_fade_finished(self):
        if not self.enabled:
            self.player.pause()


# --- Барабан с обложками ----------------------------------------------------
class CoverReel(QWidget):
    """Лента обложек: быстро прокручивается и плавно останавливается на выбранной игре."""

    spinFinished = Signal(dict)

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

    def show_idle(self, games):
        """Неподвижная лента из случайных игр."""
        self.spin_anim.stop()
        self.glow_anim.stop()
        self.winner, self.glow = None, 0.0
        if games:
            pool = self._pool(games)
            self.strip = [random.choice(pool) for _ in range(self.SIDE * 2 + 1)]
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
        """Возвращает (id выбранных коллекций, их appid, appid для пропуска)."""
        chosen = self.collection_list.checked() if self.collection_list else []
        include = set().union(*(c["apps"] for c in chosen)) if chosen else set()
        skip = self.hidden_apps if self.skip_hidden and self.skip_hidden.isChecked() else set()
        return [c["id"] for c in chosen], include, skip


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
        self.count_label = CaptionLabel("", self)

        header = QHBoxLayout()
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.music_button)

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
        self.done_button = PushButton("Пройдена", view, FIF.ACCEPT)
        self.done_button.clicked.connect(self.mark_done)
        self.cover_button = PushButton("Своя обложка", view, FIF.PHOTO)
        self.cover_button.clicked.connect(self.choose_cover)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        for button in (self.spin_button, self.done_button, self.cover_button):
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

    def update_count(self):
        self.count_label.setText(f"Игр в списке: {len(self.win.library.games)}")

    def update_buttons(self):
        spinning = self.reel.is_spinning()
        self.spin_button.setEnabled(not spinning)
        self.done_button.setEnabled(not spinning and self.current is not None)
        self.cover_button.setEnabled(not spinning and self.current is not None)

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

        self.import_button = PrimaryPushButton("Импорт из Steam", self, FIF.DOWNLOAD)
        self.import_button.clicked.connect(self.import_from_steam)
        self.add_button = PushButton("Добавить", self, FIF.ADD)
        self.add_button.clicked.connect(self.add_games)
        self.delete_button = PushButton("Удалить выбранные", self, FIF.DELETE)
        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button = PushButton("Очистить список", self, FIF.BROOM)
        self.clear_button.clicked.connect(self.clear_library)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        for button in (self.import_button, self.add_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_button)

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
        settings = core.load_settings()
        api_key, steam_id = settings["steam_api_key"], settings["steam_id"]
        if not api_key or not steam_id:
            notify("warning", self.win, "Нужны ключ и SteamID", "Укажите их на вкладке «Настройки».")
            self.win.switchTo(self.win.settings_page)
            return
        # Сначала получаем библиотеку: в окне выбора коллекций нужны только игры Steam
        self.import_button.setEnabled(False)
        notify("info", self.win, "Загружаю библиотеку Steam…",
               "Семейные игры ищутся по списку магазина — это может занять до минуты.", duration=4000)
        worker = Worker(core.load_steam_library, api_key, steam_id, parent=self)
        worker.finished.connect(self.on_owned_loaded)
        worker.failed.connect(self.on_import_failed)
        worker.start()

    def on_owned_loaded(self, result):
        self.import_button.setEnabled(True)
        owned = result["games"]
        if not owned:
            notify("warning", self.win, "Steam не вернул ни одной игры",
                   "Проверьте SteamID64 и что «Игровая информация» в приватности профиля открыта.", duration=8000)
            return
        if result["collections_error"]:
            notify("warning", self.win, "Не удалось прочитать коллекции Steam",
                   result["collections_error"], duration=6000)

        settings = core.load_settings()
        known_ids = {game["appid"] for game in owned}
        family_count = sum(game["family"] for game in owned)
        dialog = ImportDialog(self.win, result["collections"], known_ids, family_count, settings)
        if not dialog.exec():
            return
        include_ids, include, skip = dialog.selection()
        unplayed, replace = dialog.unplayed.isChecked(), dialog.replace.isChecked()
        core.save_settings(import_include=include_ids, import_replace=replace,
                           import_skip_hidden=bool(dialog.skip_hidden and dialog.skip_hidden.isChecked()))

        selected = [
            game for game in owned
            if (not include or game["appid"] in include)
            and game["appid"] not in skip
            and not (unplayed and (game["playtime"] or 0) > 0)
        ]
        added = updated = 0
        for game in selected:
            status = self.win.library.add(game["name"], game["appid"])
            added += status == "added"
            updated += status == "updated"

        removed = 0
        if replace:
            keep = {game["appid"] for game in selected}
            for game in list(self.win.library.games):
                if game.get("appid") and game["appid"] not in keep:  # без appid — добавлены вручную
                    self.win.library.remove(game)
                    removed += 1

        if (added or updated or removed) and not self.win.save_library():
            return
        self.win.libraryChanged.emit()
        text = f"Подходит под выбор: {len(selected)}. Добавлено новых: {added}."
        if removed:
            text += f" Убрано из списка: {removed}."
        notify("success", self.win, "Импорт завершён", text, duration=7000)
        if added >= LOADING_THRESHOLD:
            self.win.switchTo(self.win.home_page)  # там уже идёт загрузка обложек

    def on_import_failed(self, message):
        self.import_button.setEnabled(True)
        notify("error", self.win, "Не удалось получить библиотеку Steam", message, duration=8000)


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

        sound = QGridLayout()
        sound.setHorizontalSpacing(16)
        sound.addWidget(BodyLabel("Музыка", self), 0, 0)
        sound.addWidget(self.music_switch, 0, 1)
        sound.addWidget(BodyLabel("Громкость", self), 1, 0)
        sound.addWidget(self.volume_slider, 1, 1)
        sound.setColumnStretch(2, 1)
        if not self.win.music.available:
            sound.addWidget(CaptionLabel(f"Файл {core.MUSIC_NAME} не найден рядом с программой", self), 2, 1)

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

        layout = QVBoxLayout(self)
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
        layout.addWidget(CaptionLabel(f"My Steam Random {__version__}", self))
        layout.addWidget(CaptionLabel("Музыка: «Soliloquy» — Matthew Pablo (matthewpablo.com), "
                                      "лицензия CC-BY 3.0, opengameart.org/content/soliloquy", self))

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


# --- Главное окно -----------------------------------------------------------
class MainWindow(MSFluentWindow):
    libraryChanged = Signal()

    def __init__(self):
        super().__init__()
        self.settings = core.load_settings()
        self.library = core.GameLibrary()
        self.load_message = self.library.load()

        self.music = MusicPlayer(self)
        self.music.set_volume(int(self.settings["music_volume"]))
        self.covers = CoverPrefetcher(self)

        self.home_page = HomePage(self)
        self.library_page = LibraryPage(self)
        self.settings_page = SettingsPage(self)
        self.addSubInterface(self.home_page, FIF.GAME, "Рулетка")
        self.addSubInterface(self.library_page, FIF.LIBRARY, "Игры")
        self.addSubInterface(self.settings_page, FIF.SETTING, "Настройки",
                             position=NavigationItemPosition.BOTTOM)
        self.libraryChanged.connect(self.on_library_changed)

        self.setWindowTitle(f"My Steam Random {__version__}")
        icon = core.find_resource(core.ICON_NAME)
        if icon:
            self.setWindowIcon(QIcon(icon))
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

    def set_music_enabled(self, enabled, save=True):
        self.music.set_enabled(enabled)
        self.home_page.set_music_icon(enabled)
        self.settings_page.set_music_switch(enabled)
        if save:
            core.save_settings(music_enabled=enabled)

    def toggle_music(self):
        self.set_music_enabled(not self.music.enabled)


def main():
    app = QApplication(sys.argv)
    setTheme(THEMES.get(core.load_settings()["theme"], Theme.AUTO))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
