"""Backlog Roulette — рандомайзер «Во что сыграть дальше?» для библиотеки Steam.

Интерфейс в стиле Windows 11 (PySide6 + PySide6-Fluent-Widgets), логика — в core.py.
"""

import faulthandler
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import quote_plus

LAUNCHED = time.monotonic()  # для замера запуска: отсюда начинается загрузка Qt и библиотек

from PySide6.QtCore import (QAbstractAnimation, QAbstractNativeEventFilter, QEasingCurve, QEvent, QObject, QPoint,
                            QPointF, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer, QUrl,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QIcon, QImage, QImageReader,
                           QKeySequence, QLinearGradient, QMovie, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
                           QRadialGradient, QShortcut, QTextCharFormat, QTextCursor, QTextFormat, QTextListFormat)
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer, QSoundEffect
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QFrame, QGraphicsOpacityEffect,
                               QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QLayout,
                               QListView, QListWidget, QListWidgetItem, QSizePolicy, QStyle, QStyledItemDelegate,
                               QStackedWidget, QVBoxLayout, QWidget)
from qfluentwidgets import (Action, BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon as FIF, InfoBar,
                            IndeterminateProgressBar, InfoBarPosition, LineEdit, ListWidget, MessageBox,
                            MessageBoxBase, RadioButton,
                            FluentWindow, NavigationItemPosition, PasswordLineEdit, PlainTextEdit,
                            PrimaryPushButton, PrimaryToolButton, ProgressBar, PushButton, RoundMenu, ScrollArea,
                            SearchLineEdit, Slider, StrongBodyLabel,
                            SubtitleLabel, SwitchButton, TextEdit, Theme, TitleLabel, TransparentPushButton,
                            TransparentToggleToolButton, TransparentToolButton,
                            FluentIconBase, isDarkTheme, setTheme, themeColor)

try:  # полосы прокрутки Fluent для обычных списков Qt (плитка на вкладке «Игры»)
    from qfluentwidgets import SmoothScrollDelegate
except ImportError:
    SmoothScrollDelegate = None

try:  # как сейчас показано боковое меню: полоской значков, раскрытым поверх страницы и т. д.
    from qfluentwidgets import NavigationDisplayMode
except ImportError:
    NavigationDisplayMode = None

try:  # точка «есть обновление» на значке «Настройки» в боковом меню
    from qfluentwidgets import DotInfoBadge, InfoBadgePosition
except ImportError:
    DotInfoBadge = InfoBadgePosition = None

# Прозрачные кнопки Fluent («Удалить с ПК», «К портам», «Другая папка…») без рамки: на размытой подложке
# альбома или игры их не видно, пока не наведёшь мышь. Поэтому вокруг них — тонкая рамка, как у плашек
# на странице игры; под мышью она цвета темы.
_paint_transparent_button = TransparentPushButton.paintEvent


def _paint_outlined_button(self, event):
    _paint_transparent_button(self, event)
    painter = QPainter(self)
    painter.setRenderHint(QPainter.Antialiasing)
    dark = isDarkTheme()
    if not self.isEnabled():
        color = QColor(255, 255, 255, 20) if dark else QColor(0, 0, 0, 16)
    elif getattr(self, "isHover", False) or self.underMouse():
        color = themeColor()
    else:
        color = QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 30)
    painter.setPen(QPen(color, 1))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
    painter.end()


TransparentPushButton.paintEvent = _paint_outlined_button

# Окна поверх программы (подключение к Steam, импорт, вопросы) — это затемнение на всё окно, и оно закрывает
# заголовок с крестиком. Поэтому заголовок поднимается над затемнением, а окно программы можно таскать и за само
# затемнение — например, чтобы перенести его на второй экран, не закрывая окна.
try:
    from qfluentwidgets.components.dialog_box.mask_dialog_base import MaskDialogBase

    try:
        from qframelesswindow.utils import startSystemMove as _start_system_move
    except ImportError:
        _start_system_move = None

    class _MaskDrag(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                window = obj.window()
                if _start_system_move:
                    _start_system_move(window, event.globalPosition().toPoint())
                elif window.windowHandle():
                    window.windowHandle().startSystemMove()
                return True
            return False

    _mask_dialog_init = MaskDialogBase.__init__

    class _TitleBarAbove(QObject):
        """Заголовок окна с кнопками «свернуть», «развернуть» и крестиком — поверх затемнения: окно
        поверх программы (например, мастер подключения к Steam) не мешает закрыть её крестиком."""

        def __init__(self, dialog, title_bar):
            super().__init__(dialog)
            self.title_bar = title_bar

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Show:
                QTimer.singleShot(0, self._raise)  # после показа: окно поднимается при открытии
            return False

        def _raise(self):
            try:
                self.title_bar.raise_()
            except RuntimeError:
                pass  # окно программы уже закрыто

    def _draggable_mask_init(self, *args, **kwargs):
        _mask_dialog_init(self, *args, **kwargs)
        mask = getattr(self, "windowMask", None)
        if mask is not None:
            self._mask_drag = _MaskDrag(self)  # держим ссылку, иначе фильтр удалится вместе с обёрткой Python
            mask.installEventFilter(self._mask_drag)
        title_bar = getattr(self.window(), "titleBar", None)
        if title_bar is not None:
            self._title_above = _TitleBarAbove(self, title_bar)
            self.installEventFilter(self._title_above)
            title_bar.raise_()

    MaskDialogBase.__init__ = _draggable_mask_init
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

__version__ = "2.5.5"
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
        except core.ServiceBusy as e:  # сайт не ответил — обычное дело, простыня в логе не нужна
            core.log.warning("%s: %s", getattr(self.fn, "__name__", self.fn), core.mask_secrets(e))
            self.failed.emit(core.mask_secrets(f"{type(e).__name__}: {e}"))
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


try:
    from qfluentwidgets import InfoBarIcon  # цветные значки «успех / сведения / внимание / ошибка»

    NOTICE_ICONS = {"success": InfoBarIcon.SUCCESS, "info": InfoBarIcon.INFORMATION,
                    "warning": InfoBarIcon.WARNING, "error": InfoBarIcon.ERROR}
except (ImportError, AttributeError):
    NOTICE_ICONS = {}


class Notice(QWidget):
    """Уведомление в стиле окон программы: карточка по центру окна — значок, заголовок, текст и крестик,
    слева полоска цвета уведомления. Закрывается сама (пока над ней мышь — ждёт), крестиком или щелчком.
    Несколько уведомлений встают столбиком, самое новое — внизу."""

    WIDTH = 460
    MIN_DURATION = 4000  # мс: короче текст не успеть прочитать
    SHADOW = 14  # поля под тень вокруг карточки
    ICON = 20
    MAX_SHOWN = 3
    # Полоска слева: светлая тема, тёмная; у «сведений» — цвет темы
    ACCENTS = {"success": (QColor(15, 123, 15), QColor(108, 203, 95)),
               "warning": (QColor(157, 93, 0), QColor(252, 225, 0)),
               "error": (QColor(196, 43, 28), QColor(255, 153, 164))}

    def __init__(self, kind, host, title, text, duration, action=None):
        super().__init__(host)
        self.kind, self.host, self.closing = kind, host, False
        s = self.SHADOW
        layout = QHBoxLayout(self)
        layout.setContentsMargins(s + 20 + self.ICON + 12, s + 14, s + 8, s + 16)
        layout.setSpacing(8)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        title_label = StrongBodyLabel(title, self)
        title_label.setWordWrap(True)
        texts.addWidget(title_label)
        if text:
            body = BodyLabel(text, self)
            body.setWordWrap(True)
            texts.addWidget(body)
        if action:  # кнопка действия: («Подробнее», функция) — нажали, уведомление закрывается
            label, callback = action
            button = PrimaryPushButton(label, self)
            button.clicked.connect(self.dismiss)
            button.clicked.connect(callback)
            texts.addSpacing(6)
            texts.addWidget(button, 0, Qt.AlignLeft)
        layout.addLayout(texts, 1)
        close = TransparentToolButton(FIF.CLOSE, self)
        close.setFixedSize(28, 28)
        close.setIconSize(QSize(10, 10))
        close.setToolTip("Закрыть")
        close.clicked.connect(self.dismiss)
        layout.addWidget(close, 0, Qt.AlignTop)

        width = min(self.WIDTH, max(300, host.width() - 64)) + 2 * s
        height = layout.totalHeightForWidth(width)
        self.resize(width, height if height > 0 else layout.sizeHint().height())

        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(0.0)
        self.setGraphicsEffect(self.effect)
        self.fade = QPropertyAnimation(self.effect, b"opacity", self)
        self.fade.setDuration(180)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.dismiss)
        self.duration = max(duration, self.MIN_DURATION) if duration > 0 else 0

        notices = getattr(host, "_notices", None)
        if notices is None:
            notices = host._notices = []
        if not hasattr(host, "_notice_watch"):  # окно поменяло размер — уведомления снова по центру
            host._notice_watch = _NoticeWatch(host)
            host.installEventFilter(host._notice_watch)
        notice_glass(host)  # всё вокруг — под матовым стеклом, чёткое только уведомление
        notices.append(self)
        for old in [n for n in notices if not n.closing][:-self.MAX_SHOWN]:
            old.dismiss()
        arrange_notices(host)
        self.show()
        self.raise_()
        self.fade.setStartValue(0.0)
        self.fade.setEndValue(1.0)
        self.fade.start()
        if self.duration:
            self.timer.start(self.duration)

    def dismiss(self):
        if self.closing:
            return
        self.closing = True
        self.timer.stop()
        notices = getattr(self.host, "_notices", [])
        if self in notices:
            notices.remove(self)
        if not any(not n.closing for n in notices):  # последнее уведомление — стекло тоже уходит
            glass = getattr(self.host, "_notice_glass", None)
            if glass is not None:
                glass.vanish()
        arrange_notices(self.host)
        self.fade.stop()
        self.fade.setStartValue(self.effect.opacity())
        self.fade.setEndValue(0.0)
        self.fade.finished.connect(self.deleteLater)
        self.fade.start()

    def enterEvent(self, event):
        self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.duration and not self.closing:
            self.timer.start(2500)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dismiss()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = isDarkTheme()
        s = self.SHADOW
        card = QRectF(self.rect()).adjusted(s, s, -s, -s)
        # Мягкая тень: слои всё шире и прозрачнее, чуть ниже карточки
        painter.setPen(Qt.NoPen)
        for i in range(s, 0, -1):
            painter.setBrush(QColor(0, 0, 0, round(7 * (1 - i / s)) + 1))
            painter.drawRoundedRect(card.adjusted(-i, -i + 3, i, i + 3), 8 + i, 8 + i)
        path = QPainterPath()
        path.addRoundedRect(card, 8, 8)
        painter.fillPath(path, QColor(43, 43, 43) if dark else QColor(255, 255, 255))
        painter.save()
        painter.setClipPath(path)
        accent = self.ACCENTS.get(self.kind)
        painter.fillRect(QRectF(card.left(), card.top(), 4, card.height()),
                         (accent[1] if dark else accent[0]) if accent else themeColor())
        painter.restore()
        painter.setPen(QPen(QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 22), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        icon = NOTICE_ICONS.get(self.kind, FIF.INFO)
        icon.icon().paint(painter, QRect(round(card.left()) + 20, round(card.top()) + 15, self.ICON, self.ICON))


class _NoticeWatch(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            arrange_notices(obj)
        return False


def arrange_notices(host):
    """Уведомления — столбиком по центру окна (тени соседних карточек заходят друг на друга)."""
    notices = [n for n in getattr(host, "_notices", []) if not n.closing]
    if not notices:
        return
    overlap = Notice.SHADOW * 2 - 10
    total = sum(n.height() for n in notices) - overlap * (len(notices) - 1)
    top = max(0, (host.height() - total) // 2)
    for notice in notices:
        notice.move((host.width() - notice.width()) // 2, top)
        notice.raise_()
        top += notice.height() - overlap


def notify(kind, window, title, text="", duration=3500, action=None):
    """Уведомление по центру окна, в общем стиле программы: kind = success | info | warning | error.
    action = (подпись, функция) — кнопка в уведомлении.

    Открыто окно поверх программы («Добавить игру», вопрос) — уведомление встаёт поверх него, а не прячется сзади.
    """
    host = window.window() if window is not None else None
    modal = QApplication.activeModalWidget()
    if modal is not None and modal.isVisible() and modal.parentWidget() is not None \
            and modal.parentWidget().window() is host:
        host = modal
    if host is not None:
        Notice(kind, host, title, text, duration, action)


# --- Матовое стекло ----------------------------------------------------------
def frosted_snapshot(host):
    """Снимок host, размытый как матовое стекло: картинка уменьшается в несколько шагов и растягивается
    обратно — так размытие получается мягким и быстрым даже на всё окно. Пустой QPixmap — снять не вышло."""
    pixmap = host.grab()
    if pixmap.isNull():
        return pixmap
    image = pixmap.toImage()
    w, h = image.width(), image.height()
    for divider in (4, 12):
        image = image.scaled(max(1, w // divider), max(1, h // divider), Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
    for divider in (4, 1):
        image = image.scaled(max(1, w // divider), max(1, h // divider), Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
    blurred = QPixmap.fromImage(image)
    blurred.setDevicePixelRatio(pixmap.devicePixelRatio())
    return blurred


class FrostedGlass(QWidget):
    """«Матовое стекло» поверх host: его размытый снимок и лёгкая вуаль цвета темы. Чётким остаётся только то,
    что лежит поверх стекла, — уведомление или кружок ожидания. Щелчки под стекло не проходят."""

    FADE_MS = 220
    TINT_DARK, TINT_LIGHT = QColor(14, 14, 18, 120), QColor(250, 250, 252, 130)

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.opacity = 0.0
        self.closing = False
        self.snapshot = frosted_snapshot(host)  # до показа стекла — иначе оно попало бы в собственный снимок
        self.setAttribute(Qt.WA_NoMousePropagation)
        self.setGeometry(host.rect())
        self.fade = QVariantAnimation(self)
        self.fade.setDuration(self.FADE_MS)
        self.fade.setEasingCurve(QEasingCurve.OutCubic)
        self.fade.valueChanged.connect(self._set_opacity)
        self.fade.finished.connect(self._on_faded)
        host.installEventFilter(self)  # окно поменяло размер — стекло тоже
        self.show()
        self.raise_()
        self._animate(1.0)

    def _animate(self, target):
        self.fade.stop()
        self.fade.setStartValue(self.opacity)
        self.fade.setEndValue(float(target))
        self.fade.start()

    def _set_opacity(self, value):
        self.opacity = float(value)
        self.update()

    def _on_faded(self):
        if self.closing:
            self.host.removeEventFilter(self)
            self.deleteLater()

    def vanish(self):
        if self.closing:
            return
        self.closing = True
        self._animate(0.0)

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() == QEvent.Resize:
            self.setGeometry(self.host.rect())  # снимок растянется — он и так размыт
        return False

    def mousePressEvent(self, event):
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setOpacity(self.opacity)
        if not self.snapshot.isNull():
            painter.drawPixmap(self.rect(), self.snapshot)
        painter.fillRect(self.rect(), self.TINT_DARK if isDarkTheme() else self.TINT_LIGHT)


class NoticeGlass(FrostedGlass):
    """Стекло под уведомлениями. Щелчок по стеклу закрывает их все."""

    def mousePressEvent(self, event):
        event.accept()
        for notice in list(getattr(self.host, "_notices", [])):
            notice.dismiss()


def notice_glass(host):
    """Под уведомлением — матовое стекло: одно на все уведомления сразу. Если на окне уже кружок ожидания —
    своё стекло у него есть, уведомление просто встаёт поверх."""
    busy = getattr(host, "_busy", None)
    if busy is not None and not busy.closing:
        return
    glass = getattr(host, "_notice_glass", None)
    if glass is not None and glass.closing:
        try:
            if glass.fade.state() == QAbstractAnimation.Running:  # ещё тает — возвращаем его, а не снимаем заново
                glass.closing = False
                glass._animate(1.0)
        except RuntimeError:
            pass  # уже удалено
    if glass is None or glass.closing:
        host._notice_glass = NoticeGlass(host)
    else:
        glass.raise_()


class BusyOverlay(FrostedGlass):
    """Программа что-то делает в фоне и ничего не показывает (например, после входа в магазин Steam узнаёт
    покупки и семейную группу) — матовое стекло и крутящийся кружок с надписью: что делается и что будет дальше."""

    RADIUS, RING = 22, 4
    TIMEOUT_MS = 120000  # страховка: что-то пошло не так — через две минуты стекло уходит само

    def __init__(self, host, title, text=""):
        super().__init__(host)
        self.title, self.text = title, text
        self._angle = 0
        self.spin_timer = QTimer(self)
        self.spin_timer.setInterval(16)
        self.spin_timer.timeout.connect(self._spin)
        self.spin_timer.start()
        QTimer.singleShot(self.TIMEOUT_MS, self._timeout)
        core.log.info("Ожидание на экране: %s%s", title, f" — {text}" if text else "")

    def set_text(self, title, text=""):
        self.title, self.text = title, text
        self.update()

    def _spin(self):
        self._angle = (self._angle + 6) % 360
        self.update()

    def _timeout(self):
        if not self.closing:
            core.log.warning("Ожидание «%s» длилось дольше %d с — убираю стекло", self.title, self.TIMEOUT_MS // 1000)
            self.vanish()

    def vanish(self):
        if not self.closing:
            core.log.info("Ожидание закончено: %s", self.title)
        super().vanish()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(self.opacity)
        dark = isDarkTheme()
        w, h = self.width(), self.height()
        center = QPointF(w / 2, h / 2 - 30)
        ring = QRectF(center.x() - self.RADIUS, center.y() - self.RADIUS, 2 * self.RADIUS, 2 * self.RADIUS)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 50) if dark else QColor(0, 0, 0, 30), self.RING))
        painter.drawEllipse(ring)
        pen = QPen(themeColor(), self.RING)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(ring, -self._angle * 16, 100 * 16)

        width = min(520, w - 48)
        top = center.y() + self.RADIUS + 20
        title_font = resized_font(self.font(), 4)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(WHITE if dark else QColor(0, 0, 0))
        title_box = QRectF((w - width) / 2, top, width, 400)
        flags = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap
        used = painter.boundingRect(title_box, flags, self.title)
        painter.drawText(title_box, flags, self.title)
        if self.text:
            painter.setFont(resized_font(self.font(), 1))
            painter.setPen(QColor(255, 255, 255, 200) if dark else QColor(0, 0, 0, 170))
            painter.drawText(QRectF((w - width) / 2, used.bottom() + 8, width, 400), flags, self.text)


def show_busy(window, title, text=""):
    """Матовое стекло с кружком ожидания поверх всего окна. Уже показано — меняется только надпись."""
    host = window.window()
    busy = getattr(host, "_busy", None)
    if busy is not None and not busy.closing:
        busy.set_text(title, text)
        return
    glass = getattr(host, "_notice_glass", None)
    if glass is not None and not glass.closing:
        glass.vanish()  # у кружка своё стекло — двух друг на друге не нужно
    host._busy = BusyOverlay(host, title, text)
    arrange_notices(host)  # уведомления, если есть, — поверх кружка


def hide_busy(window):
    busy = getattr(window.window(), "_busy", None)
    if busy is not None and not busy.closing:
        busy.vanish()


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

    def forget(self, name):
        """Игру связали со Steam — обложку для неё ищем заново, уже по номеру в Steam."""
        with self._lock:
            if name not in self._failed:
                return
            self._failed.discard(name)
            try:
                core.save_not_found(self._failed)
            except OSError:
                pass

    def upgrade(self, games):
        """В фоне: обложки, которые Steam собрал из горизонтальной шапки, — заменить настоящими из SteamGridDB.
        Каждая заменённая обложка сразу появляется на барабане (сигнал coverReady)."""
        games = [dict(game) for game in games]  # копия: список могут менять в главном потоке

        def run():
            try:
                core.upgrade_generated_covers(games, core.griddb_key(), self.coverReady.emit)
            except Exception:  # noqa: BLE001 — замена обложек не должна ронять программу
                core.log.exception("Не удалось заменить обложки, собранные Steam")

        threading.Thread(target=run, daemon=True).start()

    def _run(self):
        key = core.griddb_key()
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

    def release(self, folder):
        """Файлы папки сейчас удалят: играет трек из неё — переходим на встроенную тему, а проигрыватели
        отпускают файлы (открытый трек Windows удалить не даст). True — играло из этой папки."""
        root = os.path.normcase(os.path.normpath(folder)) + os.sep

        def inside(path):
            return bool(path) and os.path.normcase(os.path.normpath(path)).startswith(root)

        playing = inside(self.path)
        if playing:
            self.set_source("")
        for deck in self.decks:
            if deck is not self.deck:  # второй проигрыватель мог держать хвост прошлого трека
                deck["fade"].stop()
                deck["player"].stop()
                deck["player"].setSource(QUrl())
        return playing

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
    """Маленький плеер в правом верхнем углу рулетки, страницы игры и браузера.

    Обычно это одна нота в кружке. Навели мышь — кружок раскрывается влево в пульт: выключить звук,
    предыдущий трек, пауза, следующий трек, вперемешку, повтор трека. Увели — через мгновение сворачивается.
    Нота — перейти на вкладку «Саундтреки» к тому, что играет. Название трека — в подсказках.
    """

    sizeChanged = Signal()  # раскрылся или свернулся — хозяин ставит его на место у правого края
    COLLAPSE_MS = 700
    ANIM_MS = 260
    PAD, GAP = 3, 2

    def __init__(self, win, parent):
        super().__init__(parent)
        self.win = win
        self.reveal = 0.0  # 0 — одна нота, 1 — весь пульт
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
        # Слева направо; нота — справа: пульт выезжает из-под неё влево, и она остаётся под мышью
        self.controls = (self.mute_button, self.prev_button, self.play_button, self.next_button,
                         self.shuffle_button, self.repeat_button)
        self.collapse_timer = QTimer(self)
        self.collapse_timer.setSingleShot(True)
        self.collapse_timer.setInterval(self.COLLAPSE_MS)
        self.collapse_timer.timeout.connect(self._collapse)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(self.ANIM_MS)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._set_reveal)

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
        # Один трек (встроенная тема, свой файл) и так играет по кругу — листать и повторять нечего,
        # но кнопки остаются на месте, просто неактивны: пульт не «прыгает» и понятно, что они есть
        several = len(music.tracks) > 1
        for button in (self.prev_button, self.next_button, self.repeat_button):
            button.setEnabled(several)
        self.prev_button.setToolTip("Предыдущий трек" if several else "Играет один трек — он идёт по кругу")
        self.next_button.setToolTip(f"Следующий трек (сейчас {track})" if several
                                    else "Играет один трек — он идёт по кругу")
        refresh_shuffle_button(self.shuffle_button, music.shuffle_mode)
        refresh_repeat_button(self.repeat_button, music.repeat_one)
        self._set_reveal(self.reveal)

    def sizeHint(self):
        note = self.music_button.sizeHint()
        controls = sum(b.sizeHint().width() + self.GAP for b in self.controls)
        return QSize(round(2 * self.PAD + note.width() + controls * self.reveal), 2 * self.PAD + note.height())

    def _set_reveal(self, value):
        self.reveal = float(value)
        self.resize(self.sizeHint())
        self._arrange()
        self.update()
        self.sizeChanged.emit()  # хозяин ставит плеер у правого края — растёт он влево

    def _arrange(self):
        """Кнопки — от правого края: нота на месте, остальные выезжают слева из-за края «таблетки»."""
        x = self.width() - self.PAD
        for button in (self.music_button,) + self.controls[::-1]:
            size = button.sizeHint()
            x -= size.width()
            button.setGeometry(x, self.PAD, size.width(), size.height())
            x -= self.GAP
        for button in self.controls:
            button.setVisible(self.reveal > 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()

    def _animate(self, target):
        if self.reveal == target and self.anim.state() != QAbstractAnimation.Running:
            return
        self.anim.stop()
        self.anim.setStartValue(self.reveal)
        self.anim.setEndValue(float(target))
        self.anim.start()

    def enterEvent(self, event):
        self.collapse_timer.stop()
        if self.anim.endValue() != 1.0 or self.reveal < 1.0:
            self._animate(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.collapse_timer.start()
        super().leaveEvent(event)

    def _collapse(self):
        if self.underMouse():
            return
        self._animate(0.0)

    def paintEvent(self, event):
        # Кружок (свёрнут) или «таблетка» (раскрыт) под кнопками — как значок живого фона в шапке игры
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 30), 1))
        painter.setBrush(QColor(32, 32, 36, 200) if dark else QColor(255, 255, 255, 215))
        radius = rect.height() / 2
        painter.drawRoundedRect(rect, radius, radius)


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

    def _fill(self, pool, count, recent=()):
        """count случайных игр для ленты. Игра не повторяется, пока не пройдёт столько карточек, сколько
        видно на экране (если игр хватает), — иначе в маленькой библиотеке рядом стоят одинаковые обложки.
        recent — карточки, которые уже стоят перед этими."""
        strip, recent = [], list(recent)
        window = min(len(pool) - 1, self.SIDE * 2)
        for _ in range(count):
            tail = recent[-window:] if window > 0 else []
            game = random.choice([g for g in pool if not any(g is r for r in tail)] or pool)
            strip.append(game)
            recent.append(game)
        return strip

    def show_idle(self, games, center=None):
        """Неподвижная лента из случайных игр; center — игра по центру."""
        self.spin_anim.stop()
        self.glow_anim.stop()
        self.winner, self.glow = center, (1.0 if center else 0.0)
        if games:
            pool = self._pool(games)
            if center:
                left = self._fill(pool, self.SIDE, recent=[center])
                self.strip = left + [center] + self._fill(pool, self.SIDE, recent=left + [center])
            else:
                self.strip = self._fill(pool, self.SIDE * 2 + 1)
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
            head = self._fill(pool, self.SIDE * 2 + 1)
        # Прямо перед выпавшей игрой её копий нет — иначе рядом с ней встали бы одинаковые обложки
        window = min(len(pool) - 1, self.SIDE * 2)
        filler = self._fill(pool, random.randint(26, 34) - window, recent=head)
        others = [g for g in pool if g is not winner] or pool
        filler += self._fill(others, window, recent=head + filler)
        tail = self._fill(pool, self.SIDE, recent=filler + [winner])
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


class HeaderPill(QWidget):
    """Значок в шапке страницы игры — в том же стиле, что мини-плеер: кружок со значком, который при наведении
    раскрывается вправо в «таблетку» ◀ номер ▶ (у живого фона ещё ✓ — оставить этот вариант).

    Три таких значка столбиком слева сверху: живой фон (видео), фон (картинка) и логотип (Aa). Щелчок по самому
    значку: у живого фона — включить или выключить его, у фона и логотипа — следующий вариант из SteamGridDB.
    Пока вариант качается, по краю кружка бежит кольцо загрузки, а «таблетка» не сворачивается.
    Живой фон включён — значок цвета темы.
    """

    clicked = Signal()
    stepRequested = Signal(int)  # -1 — предыдущий вариант, +1 — следующий
    keepRequested = Signal()
    COLLAPSE_MS = 500
    ANIM_MS = 240
    PAD, GAP = 3, 2
    PART = 30  # значок и кнопки ◀ ▶ ✓ — квадраты такой стороны
    RING = 2.5
    SIZE = PART + 2 * PAD  # свёрнутый кружок
    opened = Signal()  # начал раскрываться — соседние значки сворачиваются, чтобы не налезать друг на друга

    def __init__(self, parent, icon, title="", keep=False):
        super().__init__(parent)
        self.icon, self.title = icon, title
        self.reveal = 0.0  # 0 — один значок, 1 — вся «таблетка»
        self.progress = None  # None — не качается, -1 — качается (сколько осталось неизвестно), 0–100 — проценты
        self.active = False
        self.expandable = False  # есть что листать — тогда при наведении раскрывается
        self._angle = 0
        self.main = TransparentToolButton(icon, self)
        self.main.clicked.connect(lambda: self.clicked.emit())
        self.prev_button = TransparentToolButton(FIF.LEFT_ARROW, self)
        self.prev_button.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.label = CaptionLabel("", self)
        self.label.setAlignment(Qt.AlignCenter)
        template = f"{title} 50 / 50" if title else "50 / 50"
        self.label.setFixedWidth(QFontMetrics(self.label.font()).horizontalAdvance(template) + 12)
        self.next_button = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.next_button.clicked.connect(lambda: self.stepRequested.emit(1))
        what = title.lower() if title else "вариант живого фона"
        self.prev_button.setToolTip(f"Предыдущий {what}" + (" из SteamGridDB" if title else ""))
        self.next_button.setToolTip(f"Следующий {what}" + (" из SteamGridDB" if title else ""))
        self.controls = [self.prev_button, self.label, self.next_button]
        self.keep_button = None
        if keep:
            self.keep_button = TransparentToolButton(FIF.ACCEPT, self)
            self.keep_button.clicked.connect(lambda: self.keepRequested.emit())
            self.controls.append(self.keep_button)
        for button in (self.main, self.prev_button, self.next_button, self.keep_button):
            if button is not None:
                # Все части квадратные и одной высоты: кольцо загрузки ровно вокруг значка, как бы ни раскрылась
                # «таблетка»
                button.setFixedSize(self.PART, self.PART)
                button.setIconSize(QSize(14, 14) if button is not self.main else QSize(16, 16))
        self.collapse_timer = QTimer(self)
        self.collapse_timer.setSingleShot(True)
        self.collapse_timer.setInterval(self.COLLAPSE_MS)
        self.collapse_timer.timeout.connect(self._collapse)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(self.ANIM_MS)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._set_reveal)
        self.spin_timer = QTimer(self)  # крутит дугу, пока размер файла неизвестен
        self.spin_timer.setInterval(30)
        self.spin_timer.timeout.connect(self._spin)
        self.set_state(0, 0)
        self._set_reveal(0.0)

    # --- Состояние ---
    def set_tip(self, text):
        self.main.setToolTip(text)

    def set_active(self, active):
        self.active = active
        self.main.setIcon(self.icon.icon(color=themeColor()) if active else self.icon)

    def set_progress(self, progress):
        self.progress = progress
        if progress is not None and progress < 0:
            self.spin_timer.start()
        else:
            self.spin_timer.stop()
        busy = progress is not None
        for button in (self.prev_button, self.next_button):
            button.setEnabled(not busy)
        if self.keep_button is not None:
            self.keep_button.setEnabled(not busy and self._keep_allowed)
        if busy and self.expandable:
            self._animate(1.0)  # пока качается — видно, какой вариант
        elif not busy:
            self.collapse_timer.start()
        self.update()

    def set_state(self, index, count, chosen=None, _busy=None):
        """Какой вариант показан (index из count) и какой сохранён (chosen — только у живого фона)."""
        number = f"{index + 1} / {count}" if count else ""
        self.label.setText(f"{self.title} {number}".strip() if self.title else number)
        self._keep_allowed = index != chosen
        if self.keep_button is not None:
            self.keep_button.setEnabled(self.progress is None and self._keep_allowed)
            self.keep_button.setToolTip("Этот вариант уже сохранён" if not self._keep_allowed else
                                        "Оставить этот вариант — остальные просмотренные удалятся с диска")

    def set_expandable(self, expandable):
        self.expandable = expandable
        if not expandable:
            self._animate(0.0)
        elif self.underMouse() or self.progress is not None:
            self._animate(1.0)

    def collapse_now(self):
        self.anim.stop()
        self._set_reveal(0.0)

    def slide(self, shown):
        """Раскрыть или свернуть «таблетку» (если есть что листать)."""
        self._animate(1.0 if shown and self.expandable else 0.0)

    # --- Раскрытие ---
    def sizeHint(self):
        controls = sum(self._part_size(w).width() + self.GAP for w in self.controls)
        return QSize(round(2 * self.PAD + self.PART + controls * self.reveal), 2 * self.PAD + self.PART)

    def _part_size(self, widget):
        return QSize(widget.width(), self.PART) if widget is self.label else QSize(self.PART, self.PART)

    def _set_reveal(self, value):
        self.reveal = float(value)
        self.resize(self.sizeHint())
        self._arrange()
        self.update()

    def _arrange(self):
        """Значок на месте слева, остальное выезжает справа из-за края «таблетки»."""
        x = self.PAD
        for widget in [self.main] + self.controls:
            size = self._part_size(widget)
            widget.setGeometry(x, self.PAD, size.width(), size.height())
            x += size.width() + self.GAP
        for widget in self.controls:
            widget.setVisible(self.reveal > 0)

    def _animate(self, target):
        if self.reveal == target and self.anim.state() != QAbstractAnimation.Running:
            return
        if self.anim.state() == QAbstractAnimation.Running and self.anim.endValue() == target:
            return
        self.anim.stop()
        self.anim.setStartValue(self.reveal)
        self.anim.setEndValue(float(target))
        self.anim.start()

    def enterEvent(self, event):
        self.collapse_timer.stop()
        if self.expandable:
            self.opened.emit()
            self._animate(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.collapse_timer.start()
        super().leaveEvent(event)

    def _collapse(self):
        if self.underMouse() or (self.progress is not None and self.expandable):
            return
        self._animate(0.0)

    def _spin(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, event):
        # Кружок (свёрнут) или «таблетка» (раскрыт) — те же цвета, что у мини-плеера
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 30), 1))
        painter.setBrush(QColor(32, 32, 36, 200) if dark else QColor(255, 255, 255, 215))
        radius = rect.height() / 2
        painter.drawRoundedRect(rect, radius, radius)
        if self.progress is None:
            return
        # Кольцо загрузки — ровно вокруг значка (он слева, квадратный)
        side = self.SIZE
        ring = QRectF(0, 0, side, side).adjusted(self.RING / 2 + 1, self.RING / 2 + 1,
                                                 -self.RING / 2 - 1, -self.RING / 2 - 1)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 50) if dark else QColor(0, 0, 0, 30), self.RING))
        painter.drawEllipse(ring)
        pen = QPen(themeColor(), self.RING)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        if self.progress < 0:
            painter.drawArc(ring, -self._angle * 16, 90 * 16)
        elif self.progress > 0:
            painter.drawArc(ring, 90 * 16, -round(360 * 16 * self.progress / 100))  # от верха по часовой


LiveButton = HeaderPill  # прежнее имя значка живого фона


class HeroHeader(QWidget):
    """Шапка страницы игры: широкий фон, который внизу растворяется в окне, логотип или название поверх
    и большая кнопка ▶ по центру, если у игры есть трейлер.

    Слева сверху — значки: «живой фон» (если он есть), а с ключом SteamGridDB — «Фон» (картинка) и «Логотип»
    («Aa»). Навели на значок — справа выезжает ◀ номер ▶; щелчок по самому значку — следующий вариант."""

    playClicked = Signal()
    artRequested = Signal(str, int)  # другой вариант фона ("hero") или логотипа ("logo"): шаг -1 или +1
    liveRequested = Signal(int)  # нужен вариант живого фона номер N, а он ещё не скачан — страница игры качает его
    liveKeepRequested = Signal(int)  # оставить вариант номер N, остальные удалить
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
        # Варианты живого фона: сколько их в SteamGridDB, какой показан и какой выбран (сохранён)
        self.live_count, self.live_index, self.live_chosen = 0, 0, None
        # Значок «живой фон» — только у игр, для которых он нашёлся; при наведении раскрывается выбор варианта
        self.live_button = HeaderPill(self, FIF.VIDEO, keep=True)
        self.live_button.clicked.connect(self._on_live_clicked)
        self.live_button.stepRequested.connect(self._step_variant)
        self.live_button.keepRequested.connect(lambda: self.liveKeepRequested.emit(self.live_index))
        self.live_button.hide()
        self.variant_bar = self.live_button  # выбор варианта теперь внутри того же значка
        # «Фон» и «Логотип» из SteamGridDB: щелчок — следующий вариант, при наведении — ◀ номер ▶
        self.art_buttons, self.art_state = {}, {}
        for kind, title, icon in (("hero", "Фон", FIF.PHOTO), ("logo", "Логотип", fluent_icon("FONT", FIF.EDIT))):
            pill = HeaderPill(self, icon, title)
            pill.set_tip(f"{title} из SteamGridDB — щелчок ставит следующий вариант, стрелки листают в обе стороны. "
                         "Выбранный остаётся")
            pill.clicked.connect(lambda k=kind: self._step_art(k, 1))
            pill.stepRequested.connect(lambda step, k=kind: self._step_art(k, step))
            pill.set_expandable(True)
            pill.hide()
            self.art_buttons[kind], self.art_state[kind] = pill, (0, 0)
        for pill in [self.live_button] + list(self.art_buttons.values()):
            pill.opened.connect(lambda p=pill: self._collapse_others(p))
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
        self.live_count, self.live_index, self.live_chosen = 0, 0, None
        self.live_button.set_progress(None)
        self.live_button.set_active(False)
        self.live_button.set_expandable(False)
        self.live_button.collapse_now()
        self.live_button.hide()
        self.hero = self.logo = self.static = None
        self.title = title
        self.has_trailer = False
        has_key = bool(core.griddb_key())
        for kind, pill in self.art_buttons.items():
            pill.set_progress(None)
            pill.setVisible(has_key)
            self.art_state[kind] = (0, 0)
            pill.set_state(0, 0)
            pill.collapse_now()
        self._place_art_buttons()
        self.play_intro()

    # --- Другой фон и логотип из SteamGridDB ---
    def _collapse_others(self, pill):
        """Раскрывается один значок — остальные сразу сворачиваются (кроме тех, где качается вариант): провели
        мышью снизу вверх — две «таблетки» не торчат одновременно."""
        for other in [self.live_button] + list(self.art_buttons.values()):
            if other is not pill and other.progress is None and other.reveal > 0:
                other.collapse_timer.stop()
                other.collapse_now()

    def _step_art(self, kind, step):
        if self.art_buttons[kind].progress is None:  # пока качается прошлый — ждём
            self.artRequested.emit(kind, step)

    def set_art_busy(self, kind, busy):
        self.art_buttons[kind].set_progress(-1 if busy else None)

    def set_art_counter(self, kind, index, count):
        self.art_state[kind] = (index, count)
        self.art_buttons[kind].set_state(index, count)

    def replace_hero(self, pixmap):
        """Новый обычный фон из SteamGridDB — его сразу видно, даже если был включён живой."""
        if self.movie:
            self._toggle_live(False)
        self.static = self.hero = pixmap
        self.update()

    def _place_art_buttons(self):
        """Значки — столбиком слева сверху: живой фон (если есть), под ним фон и логотип. Раскрываются вправо."""
        y = 16
        for pill in [self.live_button] + list(self.art_buttons.values()):
            if pill.isHidden():
                continue
            pill.move(16, y)
            pill.raise_()
            y += pill.height() + 10

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
    # Вариантов в SteamGridDB несколько — пока живой фон включён, справа от значка выезжают ◀ ▶ и ✓:
    # листать варианты (каждый качается, когда до него дошли) и оставить понравившийся.
    def show_live(self, path=None, count=1, chosen=None):
        """У игры есть живой фон: count вариантов, chosen — номер выбранного, path — он уже скачан.
        False — скачанный файл Qt не открывает."""
        if path and not QMovie(path).isValid():
            return False
        self.live_path = path
        self.live_count = max(1, count)
        self.live_chosen = chosen if path else None
        self.live_index = chosen if path and chosen is not None else 0
        self.live_button.set_progress(None)
        self.live_button.set_active(False)
        self._update_live_button()
        self.live_button.show()
        self.live_button.raise_()
        self._place_art_buttons()  # фон и логотип — под значком живого фона
        return True

    def set_live_progress(self, progress):
        self.live_button.set_progress(progress)
        self.live_button.set_tip("Живой фон загружается…")
        self._update_variant_bar()

    def live_ready(self, path, index, chosen=None):
        """Вариант index скачался — сразу включаем: его для этого и качали. chosen — какой теперь выбран.
        False — Qt не может его открыть."""
        self.live_button.set_progress(None)
        if not QMovie(path).isValid():
            self._update_live_button()
            return False
        self.live_path, self.live_index = path, index
        if chosen is not None:
            self.live_chosen = chosen
        self._toggle_live(True)
        return True

    def live_failed(self):
        self.live_button.set_progress(None)
        self._update_live_button()

    def release_live(self):
        """Отпустить файл живого фона (его сейчас переименуют или удалят) — шапка пока на обычном фоне."""
        self._stop_movie()
        self.hero = self.static
        self.update()
        self.live_button.set_progress(-1)
        self._update_variant_bar()

    def _on_live_clicked(self):
        if self.live_button.progress is not None:
            return  # уже качается
        if self.live_path:
            self._toggle_live(self.movie is None)
        else:
            self.set_live_progress(-1)
            self.liveRequested.emit(self.live_index)

    def _step_variant(self, step):
        if self.live_button.progress is not None or self.live_count < 2:
            return
        self.set_live_progress(-1)
        self.liveRequested.emit((self.live_index + step) % self.live_count)

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
        if self.live_count > 1:
            tip += f"\nВариантов в SteamGridDB: {self.live_count} — наведите, чтобы листать"
        self.live_button.set_tip(tip)
        self._update_variant_bar()

    def _update_variant_bar(self):
        """Выбор варианта живого фона раскрывается при наведении (и пока качается следующий вариант), когда
        живой фон включён и вариантов больше одного."""
        busy = self.live_button.progress is not None
        self.live_button.set_state(self.live_index, self.live_count, self.live_chosen)
        self.live_button.set_expandable(self.live_count > 1 and not self.live_button.isHidden() and (
            self.movie is not None or (busy and self.live_path is not None)))

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
            self.movie.setFileName("")  # Qt держит файл открытым — без этого Windows не даст его удалить
            self.movie.deleteLater()
            self.movie = None

    def _on_movie_frame(self, _frame):
        self.hero = self.movie.currentPixmap()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_art_buttons()

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
    """Прокручиваемый список флажков для выбора коллекций. У коллекции может быть своя подпись ("label")."""

    changed = Signal()  # отметили или сняли флажок

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
            box = CheckBox(collection.get("label") or f"{collection['name']} ({len(collection['apps'])})", inner)
            box.setChecked(collection["id"] in checked_ids)
            box.stateChanged.connect(lambda _state: self.changed.emit())
            layout.addWidget(box)
            self.boxes.append((collection, box))
        layout.addStretch(1)
        self.setWidget(inner)
        # Прозрачный фон у области прокрутки и её содержимого
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")

    def checked(self):
        return [collection for collection, box in self.boxes if box.isChecked()]


class AddGamesDialog(MessageBoxBase):
    """Несколько игр списком — без поиска в Steam, только названия."""

    def __init__(self, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Добавить игры списком", self))
        hint = CaptionLabel("Игры из списка добавятся без связи со Steam. Связать игру потом — правый клик по ней "
                            "на вкладке «Игры Steam» → «Найти в Steam».", self)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.edit = TextEdit(self)
        self.edit.setPlaceholderText("По одной игре в строке (или через «|»)")
        self.edit.setMinimumSize(420, 220)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("Добавить")
        self.cancelButton.setText("Отмена")

    def names(self):
        return re.split(r"[|\n]", self.edit.toPlainText())


class AddGameDialog(MessageBoxBase):
    """Добавить игру с поиском в магазине Steam. У найденной игры есть номер в Steam (appid), а с ним —
    описание, скриншоты, трейлеры, обложки и установка через Steam. Ключ и вход для поиска не нужны.

    С game — «Найти в Steam» для игры, добавленной вручную: найденный номер привязывается к ней.
    for_disc — то же для дисковой игры: из Steam берутся только описание, оценки и отзывы, запускается она
    по-прежнему своим файлом. Поэтому игры из списка «Игры Steam» здесь не помечаются как «уже в списке».
    """

    ICON = QSize(120, 45)  # картинки поиска Steam — 231×87
    SEARCH_DELAY = 500  # мс: ищем, когда перестали печатать

    def __init__(self, parent, game=None, term="", synced=False, for_disc=False):
        super().__init__(parent)
        self.win = parent
        self.game = game
        self.for_disc = for_disc
        self.choice = None  # {"appid", "name"} — выбранная игра
        self.plain_name = None  # «Без Steam» — название как ввели
        self.bulk = False  # «Списком…»
        self.want_owned = False  # «Показать, что есть на моём аккаунте» — войти в магазин и вернуться сюда
        self.results = []
        self._token = 0
        self._closed = False
        self._rows_by_image = {}
        self.owned = core.owned_apps()
        self.my_id = parent.my_steam_id()
        self.listed = set() if for_disc else {g["appid"] for g in parent.library.games if g.get("appid")}

        self.viewLayout.addWidget(SubtitleLabel(f"Найти в Steam: «{game['name']}»" if game else "Добавить игру", self))
        hint = CaptionLabel("Программа найдёт игру в магазине Steam и возьмёт оттуда описание, оценки, отзывы и "
                            "картинки. Запускаться игра будет по-прежнему своим файлом из её папки. Ключ и вход "
                            "для поиска не нужны." if for_disc else
                            "Программа найдёт игру в магазине Steam — у неё будут описание, скриншоты, трейлеры, "
                            "обложки и кнопка «Установить». Для поиска не нужны ни ключ, ни вход.", self)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        if synced:  # вернулись сюда после «Показать, что есть на моём аккаунте» — итог прямо в этом окне
            if self.owned:
                note = CaptionLabel(f"✓ Вход в магазин Steam есть: игр и дополнений на аккаунте — {len(self.owned)}. "
                                    "У найденных игр ниже видно, какие уже ваши.", self)
                note.setTextColor(themeColor(), themeColor())
            else:
                note = CaptionLabel("Вход в магазин Steam не выполнен — без него Steam не говорит, что есть "
                                    "на аккаунте. Найти и добавить игру можно и так.", self)
                note.setTextColor(*WARNING_TEXT)
            note.setWordWrap(True)
            self.viewLayout.addWidget(note)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("Название, номер игры или ссылка на её страницу в магазине Steam")
        self.search.textChanged.connect(lambda _text: self.search_timer.start())
        if hasattr(self.search, "searchSignal"):  # Enter и кнопка поиска
            self.search.searchSignal.connect(lambda _text: self._search())
        else:
            self.search.returnPressed.connect(self._search)
        self.viewLayout.addWidget(self.search)
        self.status = CaptionLabel("", self)
        self.status.setWordWrap(True)
        self.viewLayout.addWidget(self.status)
        self.list = ListWidget(self)
        self.list.setIconSize(self.ICON)
        self.list.setMinimumHeight(300)
        self.list.currentRowChanged.connect(self._on_row)
        self.list.itemDoubleClicked.connect(lambda _item: self._choose())
        self.viewLayout.addWidget(self.list)
        if QWebEngineView and not for_disc:
            # Что есть на аккаунте, Steam без ключа показывает только тому, кто вошёл в магазин
            text = "Обновить, что есть на моём аккаунте" if self.owned else "Показать, что есть на моём аккаунте"
            owned_button = (TransparentPushButton if self.owned else PushButton)(text, self, FIF.PEOPLE)
            owned_button.setToolTip("Вход в магазин Steam во встроенном браузере (удобнее по QR-коду). "
                                    "Потом программа сама вернётся сюда, к этому поиску")
            owned_button.clicked.connect(self._want_owned)
            self.viewLayout.addWidget(owned_button, 0, Qt.AlignLeft)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(self.SEARCH_DELAY)
        self.search_timer.timeout.connect(self._search)
        self.loader = ImageLoader(self)
        self.loader.loaded.connect(self._on_image)

        self.widget.setMinimumWidth(600)
        self.yesButton.setText("Привязать" if game else "Добавить")
        self.yesButton.setEnabled(False)
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._choose)
        self.cancelButton.setText("Отмена")
        if not game:
            plain = PushButton("Без Steam", self.buttonGroup)
            plain.setToolTip("Добавить под тем названием, что введено, — для игр не из Steam")
            plain.clicked.connect(self._add_plain)
            bulk = TransparentPushButton("Списком…", self.buttonGroup)
            bulk.setToolTip("Несколько игр сразу, по названиям, без поиска в Steam")
            bulk.clicked.connect(self._add_bulk)
            self.buttonLayout.insertWidget(1, plain, 1, Qt.AlignVCenter)
            self.buttonLayout.insertWidget(2, bulk, 1, Qt.AlignVCenter)
        term = term or (game["name"] if game else "")
        if term:
            self.search.setText(term)
            self.search_timer.stop()
            QTimer.singleShot(0, self._search)
        self.search.setFocus()

    def _want_owned(self):
        self.want_owned = True
        self.accept()

    def _caption(self, appid):
        """Подпись под найденной игрой: есть ли она на аккаунте (после входа в магазин) и установлена ли на ПК."""
        if self.for_disc:  # дисковую игру через Steam не ставят и не покупают — важен только сам номер
            return f"Магазин Steam · номер {appid}"
        owner = core.installed_owner(appid)  # файлы Steam на диске — без ключа и без входа
        installed_mine = owner is not None and (not owner or not self.my_id or owner == self.my_id)
        installed_other = owner is not None and not installed_mine
        if appid in self.owned:
            return "✓ Есть на вашем аккаунте Steam" + (" · установлена на этом ПК" if installed_mine else "")
        if installed_mine:
            return "✓ Установлена на этом ПК"
        if installed_other:
            name = core.steam_account_names().get(owner)
            return f"Установлена на этом ПК другим аккаунтом Steam{f' — {name}' if name else ''}"
        if self.owned:
            return "Нет на вашем аккаунте Steam — установить можно будет после покупки"
        return f"Магазин Steam · номер {appid}"

    def _search(self):
        self.search_timer.stop()
        term = self.search.text().strip()
        self._token += 1
        token = self._token
        self.list.clear()
        self.results, self._rows_by_image = [], {}
        self.yesButton.setEnabled(False)
        if not term:
            self.status.setText("")
            return
        self.status.setText("Ищу в магазине Steam…")
        worker = Worker(core.search_store, term, parent=self.win)  # окно могут закрыть раньше, чем придёт ответ
        worker.finished.connect(lambda items: self._on_results(token, items))
        worker.failed.connect(lambda message: self._on_failed(token, message))
        worker.start()

    def _on_results(self, token, items):
        if self._closed or token != self._token:
            return
        self.results = items
        if not items:
            self.status.setText("В магазине Steam ничего не нашлось. Попробуйте название по-английски, номер игры "
                                "или ссылку на её страницу. " + ("Игры нет в Steam — описание возьмётся из Википедии."
                                                                if self.for_disc else "Игра не из Steam — «Без Steam»."))
            return
        self.status.setText("Выберите игру и нажмите «" + self.yesButton.text() + "» (или дважды щёлкните по ней).")
        for row, item in enumerate(items):
            listed = item["appid"] in self.listed and not (self.game and self.game.get("appid") == item["appid"])
            caption = "Уже в вашем списке" if listed else self._caption(item["appid"])
            entry = QListWidgetItem(f"{item['name']}\n{caption}")
            entry.setSizeHint(QSize(0, self.ICON.height() + 14))
            if listed:
                entry.setFlags(Qt.NoItemFlags)
            self.list.addItem(entry)
            if item["image"]:
                self._rows_by_image.setdefault(item["image"], []).append(row)
                self.loader.request(item["image"])
        first = next((i for i in range(self.list.count()) if self.list.item(i).flags() & Qt.ItemIsEnabled), -1)
        self.list.setCurrentRow(first)

    def _on_failed(self, token, message):
        if self._closed or token != self._token:
            return
        self.status.setText("Магазин Steam не ответил: " + message.split(": ", 1)[-1])

    def _on_image(self, url, content):
        if self._closed:
            return
        pixmap = QPixmap()
        if not content or not pixmap.loadFromData(content):
            return
        pixmap = pixmap.scaled(self.ICON, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        for row in self._rows_by_image.get(url, []):
            item = self.list.item(row)
            if item:
                item.setIcon(QIcon(pixmap))

    def _on_row(self, row):
        item = self.list.item(row) if row >= 0 else None
        self.yesButton.setEnabled(bool(item and item.flags() & Qt.ItemIsEnabled and row < len(self.results)))

    def _choose(self):
        row = self.list.currentRow()
        item = self.list.item(row) if row >= 0 else None
        if not item or not item.flags() & Qt.ItemIsEnabled or row >= len(self.results):
            return
        self.choice = {"appid": self.results[row]["appid"], "name": self.results[row]["name"]}
        self.accept()

    def _add_plain(self):
        name = core.clean_name(self.search.text())
        if not name:
            self.status.setText("Сначала введите название игры.")
            return
        self.plain_name = name
        self.accept()

    def _add_bulk(self):
        self.bulk = True
        self.accept()

    def done(self, code):
        self._closed = True  # опоздавшие ответы поиска и картинки больше не нужны
        self.search_timer.stop()
        super().done(code)


# Выбор в окне импорта, который запоминается для быстрой синхронизации
IMPORT_KEYS = ("import_include", "import_skip_hidden", "import_unplayed", "import_replace", "import_family",
               "import_unverified")


class ImportDialog(MessageBoxBase):
    """Импорт из Steam. Сверху — что нашлось: ваши игры и семейная группа (и проверил ли её Steam), ниже —
    откуда брать игры и что пропускать, внизу — сколько игр добавится; это число пересчитывается на лету.

    Считаются только игры Steam: дополнения, программы и сторонние игры, добавленные в коллекции вручную,
    пропускаются. want_login — нажали «Войти и проверить»: окно закрылось, чтобы войти в магазин Steam
    и открыть импорт заново, уже с проверенной семейной группой.
    """

    def __init__(self, parent, library, settings, listed):
        super().__init__(parent)
        self.library, self.settings, self.listed = library, settings, listed
        self.want_login = False
        games = library["games"]
        known = {game["appid"] for game in games}
        own = {game["appid"] for game in games if not game["family"]}
        family = sum(game["family"] and game.get("verified", True) for game in games)
        guessed = {game["appid"] for game in games if not game.get("verified", True)}
        collections = library["collections"] or []
        hidden = next((c for c in collections if c["id"] == "hidden"), None)
        self.hidden_apps = hidden["apps"] & known if hidden else set()
        visible = []
        for collection in collections:
            apps = collection["apps"] & known
            if collection["id"] == "hidden" or not apps:
                continue  # «Скрытые» — отдельным флажком ниже, пустые коллекции не показываем
            sure, unsure = len(apps - guessed), len(apps & guessed)
            label = f"{collection['name']} ({sure})" + (f" + {unsure} не проверено" if unsure else "")
            if collection["id"] in core.SPECIAL_COLLECTIONS:
                label += " — служебная коллекция Steam"
            visible.append({**collection, "apps": apps, "label": label})

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 14, 0)  # справа — место под полосу прокрутки
        layout.setSpacing(6)

        def heading(text):
            layout.addSpacing(8)
            layout.addWidget(StrongBodyLabel(text, body))

        def hint(text, warning=False, indent=True):
            label = CaptionLabel(text, body)
            label.setWordWrap(True)
            if indent:
                label.setContentsMargins(28, 0, 0, 4)  # под текстом флажка, а не под самим флажком
            if warning:
                label.setTextColor(*WARNING_TEXT)
            layout.addWidget(label)
            return label

        def option(text, checked, note, warning=False):
            box = CheckBox(text, body)
            box.setChecked(checked)
            box.stateChanged.connect(self._update)
            layout.addWidget(box)
            hint(note, warning)
            return box

        # Что нашлось
        layout.addWidget(StrongBodyLabel("Что нашлось в Steam", body))
        hidden_own = len(self.hidden_apps & own)
        layout.addWidget(BodyLabel(f"Мои игры: {len(own)}" + (f" (из них скрыты в Steam: {hidden_own})"
                                                               if hidden_own else ""), body))
        if library.get("family_checked"):
            layout.addWidget(BodyLabel(f"Семейная группа: {family} игр других участников — проверено Steam"
                                       if library.get("family_member")
                                       else "Семейная группа: вы в ней не состоите — проверено Steam", body))
        else:
            layout.addWidget(BodyLabel("Семейная группа: не проверена", body))
            reason = (f"Steam не ответил: {library['family_error']}. " if library.get("family_error") else
                      "Семейную группу Steam показывает только после входа в магазин Steam во встроенном браузере. ")
            if guessed:
                reason += (f"Без проверки в ваших коллекциях нашлось игр не с вашего аккаунта: {len(guessed)} — "
                           "семейные ли они и есть ли к ним доступ сейчас, не узнать.")
            hint(reason, warning=True, indent=False)
            if QWebEngineView:
                again = library.get("store_login")
                login = PushButton("Проверить ещё раз" if again else "Войти и проверить", body, FIF.PEOPLE)
                login.setToolTip("Спросить Steam ещё раз" if again else
                                 "Вход в магазин Steam во встроенном браузере (удобнее по QR-коду), потом это окно "
                                 "откроется снова — уже с играми семейной группы")
                login.clicked.connect(self._login)
                layout.addWidget(login, 0, Qt.AlignLeft)

        # Откуда брать игры
        heading("Откуда брать игры")
        self.all_radio = RadioButton("Все мои игры", body)
        layout.addWidget(self.all_radio)
        hint("Вся ваша библиотека Steam, как «Мои игры» в клиенте Steam"
             + (", и игры семейной группы, если они отмечены ниже." if family or guessed else "."))
        self.pick_radio = RadioButton("Только из коллекций", body)
        layout.addWidget(self.pick_radio)
        self.collection_list = None
        if visible:
            hint("Ваши папки в библиотеке Steam. В скобках — сколько в коллекции игр, которые можно добавить: "
                 "дополнения, программы и сторонние игры не считаются. Пустые коллекции не показаны.")
            self.collection_list = CheckList(visible, set(settings["import_include"]), body)
            self.collection_list.changed.connect(self._update)
            row = QHBoxLayout()
            row.setContentsMargins(24, 0, 0, 0)
            row.addWidget(self.collection_list)
            layout.addLayout(row)
        else:
            self.pick_radio.setEnabled(False)
            hint("Коллекций с играми не нашлось. " + (library.get("collections_hint") or ""))
        picked = bool(set(settings["import_include"]) & {c["id"] for c in visible})
        (self.pick_radio if picked else self.all_radio).setChecked(True)
        self.all_radio.toggled.connect(self._update)

        # Что добавить и что пропустить
        heading("Что добавить")
        self.family_box = self.unverified_box = self.skip_hidden = None
        if family:
            self.family_box = option(
                f"Игры семейной группы ({family})", settings["import_family"],
                "Игры других участников семьи, которые Steam разрешает вам запускать. Время в них Steam "
                "не сообщает — для «ещё не играл» они считаются неначатыми.")
        if guessed:
            self.unverified_box = option(
                f"Непроверенные игры из коллекций ({len(guessed)})", settings["import_unverified"],
                "Игры из ваших коллекций, которых нет среди ваших покупок. Steam без входа в магазин не говорит, "
                "доступны ли они вам, — поэтому по умолчанию они не добавляются. Лучше нажать «Войти и проверить».",
                warning=True)
        if self.hidden_apps:
            self.skip_hidden = option(
                f"Пропускать скрытые игры ({len(self.hidden_apps)})", settings["import_skip_hidden"],
                "Игры, которые вы скрыли в библиотеке Steam (служебная коллекция «Скрытые»).")
        self.unplayed = option("Только игры, в которые я ещё не играл", settings["import_unplayed"],
                               "По времени в игре, которое сообщает Steam.")
        self.replace = option("Заменить мой список: убрать игры Steam, которых нет в выборе",
                              settings["import_replace"], "Игры, добавленные вручную, останутся в любом случае.")
        heading("Фильтры рулетки")
        hint("После импорта программа в фоне соберёт из магазина Steam жанры, год выхода и отзывы новых игр — по ним "
             "рулетку можно фильтровать («Фильтры» рядом с кнопкой «Крутить»). Магазин отдаёт сведения не сразу, "
             "поэтому для большой библиотеки сбор займёт время: фильтры станут доступны позже, ход сбора виден "
             "на «Рулетке». Закроете программу раньше — сбор продолжится при следующем запуске.", indent=False)
        layout.addStretch(1)

        scroll = ScrollArea(self)
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")
        # Окно не выше главного: всё, что не влезло, прокручивается
        scroll.setFixedHeight(min(layout.totalHeightForWidth(self.WIDTH) + 8, max(260, parent.height() - 300)))

        self.viewLayout.addWidget(SubtitleLabel("Импорт из Steam", self))
        self.viewLayout.addWidget(scroll)
        self.counter = StrongBodyLabel("", self)
        self.counter.setTextColor(themeColor(), themeColor())
        self.viewLayout.addWidget(self.counter)

        self.widget.setMinimumWidth(self.WIDTH + 48)
        self.widget.setMaximumWidth(self.WIDTH + 48)
        self.yesButton.setText("Импортировать")
        self.cancelButton.setText("Отмена")
        self._update()

    WIDTH = 580

    def _login(self):
        self.want_login = True
        self.accept()

    def _update(self, *_):
        """Сколько игр добавится при этом выборе и сколько из них уже в списке."""
        if self.collection_list:
            self.collection_list.setEnabled(self.pick_radio.isChecked())
        choice = self.selection()
        if self.pick_radio.isChecked() and not choice["import_include"]:
            self.counter.setText("Отметьте хотя бы одну коллекцию")
            self.yesButton.setEnabled(False)
            return
        selected, _ = core.select_games(self.library, choice)
        ids = {game["appid"] for game in selected}
        new = len(ids - self.listed)
        text = f"Будет добавлено: {new} · уже в списке: {len(ids) - new}"
        gone = len(self.listed - ids) if choice["import_replace"] else 0
        if gone:
            text += f" · уберётся из списка: {gone}"
        self.counter.setText(text if ids else "Под этот выбор не подходит ни одна игра")
        self.yesButton.setEnabled(bool(ids))

    def selection(self):
        """Выбор пользователя в виде, который сохраняется для быстрой синхронизации. Чего в окне нет
        (например, семейных игр не нашлось), остаётся как было."""
        settings = self.settings

        def checked(box, key):
            return box.isChecked() if box else bool(settings[key])

        chosen = self.collection_list.checked() if self.collection_list and self.pick_radio.isChecked() else []
        return {
            "import_include": [c["id"] for c in chosen],
            "import_skip_hidden": checked(self.skip_hidden, "import_skip_hidden"),
            "import_unplayed": self.unplayed.isChecked(),
            "import_replace": self.replace.isChecked(),
            "import_family": checked(self.family_box, "import_family"),
            "import_unverified": checked(self.unverified_box, "import_unverified"),
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
        self.hint = CaptionLabel("Семейную группу программа проверяет у Steam, если вы вошли в магазин Steam "
                                 "во встроенном браузере. Первый раз загрузка может занять до минуты.", self)
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


WARNING_TEXT = (QColor(157, 93, 0), QColor(252, 225, 0))  # цвет предупреждений Fluent: светлая и тёмная тема

# Шрифт текста в мастере подключения: основной — 14 px, подсказки — 13 (у обычных подписей Fluent 12 —
# в длинных пояснениях это мелко)
WIZARD_BODY_PX, WIZARD_CAPTION_PX = 14, 13


def wizard_text(text, parent, caption=False, warning=False):
    """Абзац текста в мастере подключения и в окне ключа SteamGridDB: с переносом строк и ровным шрифтом."""
    label = (CaptionLabel if caption else BodyLabel)(text, parent)
    label.setWordWrap(True)
    font = QFont(label.font())
    font.setPixelSize(WIZARD_CAPTION_PX if caption else WIZARD_BODY_PX)
    label.setFont(font)
    if warning:
        label.setTextColor(*WARNING_TEXT)
    return label


# Что работает без ключа Steam Web API — в мастере подключения и в окне после «Пропустить»
KEYLESS_FEATURES = (
    "Игры добавляются через «Игры Steam» → «Добавить» — с поиском по магазину Steam, описанием, трейлерами "
    "и установкой.",
    "После входа в магазин Steam во встроенном браузере видно, что есть на аккаунте: игры и саундтреки.",
    "Рулетка, музыка и браузер работают как обычно.",
)
KEYLESS_MISSING = "Без ключа не будет только коллекций при импорте, «Синхронизации» и достижений."


class SteamLaterDialog(MessageBoxBase):
    """После «Пропустить» в мастере подключения: что работает без Steam и где подключить его потом."""

    def __init__(self, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Steam можно подключить позже", self))
        for text, caption in [("Программа работает и без подключения к Steam:", False)] \
                + [("• " + line, False) for line in KEYLESS_FEATURES] \
                + [(KEYLESS_MISSING, True),
                   ("Подключить: «Настройки» → «Войти через Steam» или «Импорт из Steam» на вкладке «Игры Steam».",
                    True)]:
            self.viewLayout.addWidget(wizard_text(text, self, caption))
        self.widget.setMinimumWidth(520)
        self.widget.setMaximumWidth(520)
        self.yesButton.setText("Добавить игру")
        self.cancelButton.setText("Понятно")


class GridKeyPanel(QWidget):
    """Ключ SteamGridDB: что он даёт, как его получить, поле и проверка. Стоит в мастере подключения
    (необязательный шаг) и в отдельном окне «Настройки» → «Ключ SteamGridDB».

    Ключ проверяется одним запросом к SteamGridDB и хранится зашифрованным в settings.json (как ключ Steam).
    checked(ключ) — проверен и сохранён; busyChanged(True/False) — идёт проверка.
    """

    checked = Signal(str)
    busyChanged = Signal(bool)

    def __init__(self, win, parent=None, in_wizard=False):
        super().__init__(parent)
        self.win = win
        self._closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        def text(value, caption=False, warning=False):
            label = wizard_text(value, self, caption, warning)
            layout.addWidget(label)
            return label

        self.note = text("", caption=True)  # в мастере: что уже готово перед этим шагом
        self.note.setTextColor(themeColor(), themeColor())
        self.note.hide()
        layout.addWidget(StrongBodyLabel("Ещё красивее — с ключом SteamGridDB", self))
        text("SteamGridDB — сайт, где игроки выкладывают картинки для игр. С ключом программа возьмёт оттуда "
             "то, чего нет в Steam: вертикальные обложки, фоны и логотипы на странице игры, живые фоны.")
        text("Без ключа — официальные картинки Steam, большинству игр их хватает. "
             + ("Шаг можно пропустить: «Настройки» → «Ключ SteamGridDB»." if in_wizard else "Ключ бесплатный."),
             caption=True)
        text("1. «Открыть страницу ключа» и войдите на SteamGridDB (можно через Steam).\n"
             "2. Нажмите «Generate API Key».\n"
             "3. Скопируйте ключ и вставьте сюда.")
        open_button = PushButton("Открыть страницу ключа", self, FIF.LINK)
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(core.STEAMGRIDDB_KEY_PAGE)))
        layout.addWidget(open_button, 0, Qt.AlignLeft)
        self.key_edit = PasswordLineEdit(self)
        self.key_edit.setPlaceholderText("Ключ SteamGridDB")
        self.key_edit.setText(core.griddb_key())
        self.key_edit.returnPressed.connect(self.check)
        layout.addWidget(self.key_edit)
        self.bar = IndeterminateProgressBar(self)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.status = text("", caption=True)
        self.status.hide()
        text("Ключ личный: он хранится только на этом ПК, зашифрованным, и уходит только в SteamGridDB.",
             caption=True, warning=True)
        layout.addStretch(1)

    def set_note(self, value):
        self.note.setText(value)
        self.note.setVisible(bool(value))

    def focus(self):
        self.key_edit.setFocus()

    def check(self):
        key = self.key_edit.text().strip()
        if not key:
            self._set_status("Сначала вставьте ключ — или пропустите этот шаг.", warning=True)
            return
        self.bar.show()
        self.status.hide()
        self.busyChanged.emit(True)
        worker = Worker(core.check_griddb_key, key, parent=self.win)  # окно могут закрыть раньше ответа
        worker.finished.connect(self._on_checked)
        worker.failed.connect(self._on_failed)
        worker.start()

    def _set_status(self, value, warning=False):
        self.status.setText(value)
        self.status.setTextColor(*(WARNING_TEXT if warning else (QColor(0, 0, 0, 160), QColor(255, 255, 255, 170))))
        self.status.show()

    def _on_checked(self, key):
        if self._closed:
            return
        self.bar.hide()
        self.busyChanged.emit(False)
        try:
            core.save_settings(griddb_api_key=key)
        except OSError as e:
            self._set_status(f"Не удалось сохранить ключ: {e}", warning=True)
            return
        core.log.info("Ключ SteamGridDB сохранён (зашифрованным)")
        self.checked.emit(key)

    def _on_failed(self, message):
        if self._closed:
            return
        self.bar.hide()
        self.busyChanged.emit(False)
        self._set_status(message.split(": ", 1)[-1] + ".", warning=True)

    def close_panel(self):
        self._closed = True  # опоздавший ответ проверки больше не нужен


class SteamGridDialog(MessageBoxBase):
    """«Настройки» → «Ключ SteamGridDB»: то же, что шаг мастера, отдельным окном."""

    def __init__(self, parent):
        super().__init__(parent)
        self.saved = False
        self.viewLayout.addWidget(SubtitleLabel("Ключ SteamGridDB", self))
        self.panel = GridKeyPanel(parent, self)
        if core.griddb_key():
            self.panel.set_note("Ключ уже сохранён. Вставьте новый, чтобы заменить его.")
        self.viewLayout.addWidget(self.panel)
        self.yesButton.setText("Проверить и сохранить")
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self.panel.check)
        self.panel.busyChanged.connect(self._on_busy)
        self.panel.checked.connect(self._on_saved)
        self.cancelButton.setText("Отмена")
        self.widget.setMinimumWidth(560)
        self.widget.setMaximumWidth(560)
        QTimer.singleShot(0, self.panel.focus)

    def _on_busy(self, busy):
        self.yesButton.setEnabled(not busy)
        self.yesButton.setText("Проверяю…" if busy else "Проверить и сохранить")

    def _on_saved(self, _key):
        self.saved = True
        self.accept()

    def done(self, code):
        self.panel.close_panel()
        super().done(code)


class SteamSetupDialog(MessageBoxBase):
    """Подключение к Steam: вход через сайт Steam во внешнем браузере, затем ключ Steam Web API.

    Пароль вводится только на сайте Steam в браузере пользователя — программа получает лишь SteamID,
    и Steam подтверждает его отдельным запросом (см. core.SteamLogin). Ключ пользователь создаёт сам
    и вставляет сюда, программа сразу проверяет его и хранит зашифрованным.

    Steam выдаёт ключ не всем аккаунтам. Тогда шаг «Без ключа» рассказывает, что работает и так, и спрашивает,
    можно ли запомнить SteamID. Запомнили — в следующий раз мастер откроется сразу на шаге ключа. Не запомнили —
    SteamID помнится до закрытия программы (MainWindow.session_steam_id), чтобы не водить по входу заново.
    Второй раз «Без ключа» с тем же аккаунтом ничего не спрашивает — просто закрывает мастер.
    """

    LOGIN, KEY, NOKEY, GRID, DONE = range(5)

    def __init__(self, parent):
        super().__init__(parent)
        self.win = parent
        settings = core.load_settings()
        self.had_key = bool(settings["steam_api_key"])  # ключ уже есть — «Без ключа» не предлагаем
        no_key = settings.get("steam_setup") == "no_key" and not self.had_key
        self.was_no_key = no_key  # раньше работали без ключа — в конце расскажем, что с ключом добавилось
        saved_id = settings["steam_id"] if no_key else ""  # вход с прошлого раза уже подтверждён Steam
        session_id = getattr(parent, "session_steam_id", "") if no_key else ""  # вход в этом запуске, не запомнен
        self.steam_id = saved_id or session_id
        self.remembered = bool(saved_id)
        self._repeat_id = self.steam_id  # «Без ключа» с этим аккаунтом уже выбирали — второй раз не спрашиваем
        self.session_id = ""  # не запомнили SteamID — программа помнит его до закрытия
        self.repeated = False
        self.names = {}  # SteamID → имя профиля Steam
        self.connected = False  # ключ проверен и сохранён
        self.no_key = False  # выбрали «Без ключа»
        # Нажали «Пропустить» на первом шаге. Мастер закрыли иначе (Alt+F4, Esc, закрыли программу) —
        # это не отказ: при следующем запуске мастер откроется снова
        self.skipped = False
        self.want_import = False
        self._login = None
        self._closed = False
        self._switch_from = ""  # «Сменить аккаунт»: с какого аккаунта уходим
        self._pending_id = ""
        # Необязательный шаг «ключ SteamGridDB» — после ключа Steam (или «Без ключа»), если ключа SteamGridDB ещё нет
        self.need_grid = not settings["griddb_api_key"]
        self.grid_saved = False
        self._after_grid = None  # "done" — дальше страница «Готово», "accept" — закрыть мастер

        self.viewLayout.addWidget(SubtitleLabel("Подключение к Steam", self))
        self.step_label = CaptionLabel("", self)
        self.viewLayout.addWidget(self.step_label)
        self.pages = QStackedWidget(self)
        self.pages.addWidget(self._login_page())
        self.pages.addWidget(self._key_page())
        self.pages.addWidget(self._nokey_page())
        self.grid_panel = GridKeyPanel(parent, in_wizard=True)
        self.grid_panel.busyChanged.connect(self._on_grid_busy)
        self.grid_panel.checked.connect(lambda _key: self._leave_grid(saved=True))
        self.pages.addWidget(self.grid_panel)
        self.pages.addWidget(self._done_page())
        self.viewLayout.addWidget(self.pages)

        # Что делают кнопки, решает шаг: войти, проверить ключ, запомнить SteamID, импортировать
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_primary)
        self.cancelButton.clicked.disconnect()
        self.cancelButton.clicked.connect(self._on_secondary)
        self.widget.setMinimumWidth(560)
        self.widget.setMaximumWidth(560)
        if self.steam_id:
            self._show_account(self.steam_id)
            self.previous_label.show()
            core.log.info("Мастер подключения: в прошлый раз ключа не было — открываю шаг ключа (SteamID %s)",
                          core.short_steam_id(self.steam_id))
            self._show_step(self.KEY)
        else:
            self.login_note.setVisible(no_key)
            self._show_step(self.LOGIN)

    # --- Аккаунт ---
    def _who(self, steam_id):
        """«Имя (SteamID …1234)» — имя, если уже узнали, иначе только последние цифры SteamID."""
        name = self.names.get(steam_id)
        return f"{name} (SteamID {core.short_steam_id(steam_id)})" if name else f"SteamID {core.short_steam_id(steam_id)}"

    def _show_account(self, steam_id):
        """Строка «Вы вошли в Steam»: сразу с SteamID, имя профиля подставляется, когда придёт."""
        self.account_label.setText(f"Вы вошли в Steam: {self.names.get(steam_id, '')} (SteamID {steam_id})"
                                   if self.names.get(steam_id) else f"Вы вошли в Steam: SteamID {steam_id}")
        if steam_id in self.names:
            return
        worker = Worker(core.steam_persona_name, steam_id, parent=self.win)  # окно могут закрыть раньше ответа
        worker.finished.connect(lambda name: self._on_persona(steam_id, name))
        worker.start()

    def _on_persona(self, steam_id, name):
        if self._closed or not name:
            return
        self.names[steam_id] = name
        if steam_id == self.steam_id:
            self._show_account(steam_id)
            if self.pages.currentIndex() == self.NOKEY:
                self._show_step(self.NOKEY)  # в вопросе про SteamID — тоже с именем

    # --- Шаги ---
    @staticmethod
    def _page():
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        return page, layout

    @staticmethod
    def _text(text, parent, caption=False, warning=False):
        return wizard_text(text, parent, caption, warning)

    def _login_page(self):
        page, layout = self._page()
        self.login_note = self._text(
            "В прошлый раз Steam не выдал ключ, а аккаунт не запомнен — поэтому снова вход. Ключа всё ещё нет? "
            "На следующем шаге снова будет «Без ключа».", page, warning=True)
        self.login_note.hide()
        layout.addWidget(self.login_note)
        layout.addWidget(self._text(
            "Войдите в Steam — в вашем браузере откроется официальная страница входа. Программа возьмёт из Steam "
            "список ваших игр.", page))
        layout.addWidget(self._text(
            "Пароль и коды Steam Guard вводятся только на сайте Steam. Программа получает лишь номер аккаунта "
            "(SteamID).", page, caption=True))
        layout.addWidget(self._text(
            "«Sign into 127.0.0.1» на странице Steam — это сама программа на вашем ПК, а не чужой сайт.",
            page, caption=True))
        layout.addWidget(self._text(
            "Не проходит вход по QR-коду или Steam Guard? Отключите VPN или прокси и откройте страницу входа "
            "ещё раз.", page, caption=True))
        self.login_bar = IndeterminateProgressBar(page)
        self.login_bar.hide()
        self.login_status = self._text("", page, caption=True)
        self.login_status.hide()
        self.reopen_button = TransparentPushButton("Открыть страницу входа ещё раз", page, FIF.LINK)
        self.reopen_button.hide()
        self.reopen_button.clicked.connect(self._open_login_page)
        # Смена аккаунта: браузер помнит вход, поэтому сначала выйти из Steam на его сайте
        self.steam_site_button = TransparentPushButton("Открыть сайт Steam в браузере", page, FIF.GLOBE)
        self.steam_site_button.hide()
        self.steam_site_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://store.steampowered.com/")))
        self.same_button = TransparentPushButton("", page, FIF.ACCEPT)
        self.same_button.hide()
        self.same_button.clicked.connect(lambda: self._use_account(self._pending_id))
        layout.addWidget(self.login_bar)
        layout.addWidget(self.login_status)
        layout.addWidget(self.reopen_button, 0, Qt.AlignLeft)
        layout.addWidget(self.steam_site_button, 0, Qt.AlignLeft)
        layout.addWidget(self.same_button, 0, Qt.AlignLeft)
        layout.addStretch(1)
        return page

    def _key_page(self):
        page, layout = self._page()
        self.account_label = StrongBodyLabel("", page)
        layout.addWidget(self.account_label)
        other = TransparentPushButton("Не ваш аккаунт? Сменить", page, FIF.PEOPLE)
        other.setToolTip("Как войти в Steam другим аккаунтом — браузер помнит прошлый вход")
        other.clicked.connect(self._login_again)
        layout.addWidget(other, 0, Qt.AlignLeft)
        self.previous_label = self._text(
            "В прошлый раз Steam не выдал ключ этому аккаунту. Появился — вставьте ниже, нет — «Без ключа».",
            page, warning=True)
        self.previous_label.hide()
        layout.addWidget(self.previous_label)
        layout.addWidget(self._text(
            "Нужен ваш ключ Steam Web API — с ним программа читает список игр. Выдаёт его только сам Steam:", page))
        layout.addWidget(self._text(
            "1. «Открыть страницу ключа» — откроется в том же браузере.\n"
            "2. В «Domain Name» впишите localhost, отметьте согласие и нажмите «Register». Попросит "
            "подтверждение в приложении Steam — подтвердите.\n"
            "3. Скопируйте строку после «Key» и вставьте сюда.", page))
        open_button = PushButton("Открыть страницу ключа", page, FIF.LINK)
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(core.STEAM_API_KEY_PAGE)))
        layout.addWidget(open_button, 0, Qt.AlignLeft)
        self.key_edit = PasswordLineEdit(page)
        self.key_edit.setPlaceholderText("Ключ: 32 знака из цифр и букв A–F")
        self.key_edit.returnPressed.connect(self._on_primary)
        layout.addWidget(self.key_edit)
        self.key_bar = IndeterminateProgressBar(page)
        self.key_bar.hide()
        self.key_status = self._text("", page, caption=True)
        self.key_status.hide()
        layout.addWidget(self.key_bar)
        layout.addWidget(self.key_status)
        layout.addWidget(self._text(
            "Никому не сообщайте ключ: кто его просит — мошенник. Программа хранит ключ только на этом ПК, "
            "зашифрованным.", page, caption=True, warning=True))
        layout.addWidget(self._text(
            "Steam выдаёт ключ не всем — например, если на аккаунте ещё нет игр («You will be granted access…»). "
            "Тогда нажмите «Без ключа».", page, caption=True))
        layout.addStretch(1)
        return page

    def _nokey_page(self):
        page, layout = self._page()
        layout.addWidget(StrongBodyLabel("Программа работает и без ключа", page))
        for line in KEYLESS_FEATURES:
            layout.addWidget(self._text("• " + line, page))
        layout.addWidget(self._text(KEYLESS_MISSING, page, caption=True))
        self.remember_text = self._text("", page)
        layout.addWidget(self.remember_text)
        layout.addWidget(self._text(
            "Ключ можно добавить позже: «Настройки» → «Войти через Steam».", page, caption=True))
        back = TransparentPushButton("Назад к ключу", page, FIF.LEFT_ARROW)
        back.clicked.connect(lambda: self._show_step(self.KEY))
        layout.addWidget(back, 0, Qt.AlignLeft)
        layout.addStretch(1)
        return page

    def _done_page(self):
        page, layout = self._page()
        self.done_title = StrongBodyLabel("", page)
        self.done_text = self._text("", page)
        layout.addWidget(self.done_title)
        layout.addWidget(self.done_text)
        self.private_text = self._text(
            "Steam не показал игры: в приватности профиля скрыта «Игровая информация». Сделайте её открытой — "
            "и импорт заработает.", page, caption=True, warning=True)
        self.private_button = PushButton("Открыть настройки приватности", page, FIF.LINK)
        self.private_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(core.STEAM_PRIVACY_PAGE)))
        layout.addWidget(self.private_text)
        layout.addWidget(self.private_button, 0, Qt.AlignLeft)
        layout.addStretch(1)
        return page

    def _show_step(self, step):
        self.pages.setCurrentIndex(step)
        total = 3 if self.need_grid else 2
        grid_label = ("Шаг 3 из 3 — картинки SteamGridDB (необязательно)" if self._after_grid == "done"
                      else "Картинки SteamGridDB (необязательно)")
        self.step_label.setText((f"Шаг 1 из {total} — вход в Steam", f"Шаг 2 из {total} — ключ Steam Web API",
                                 "Без ключа", grid_label, "Готово")[step])
        self.yesButton.setText(("Войти через Steam", "Проверить ключ", "Запомнить SteamID", "Проверить и сохранить",
                                "Импортировать игры")[step])
        self.yesButton.setEnabled(True)
        self.cancelButton.setText(("Пропустить", "Отмена" if self.had_key else "Без ключа", "Не запоминать",
                                   "Пропустить", "Позже")[step])
        self.cancelButton.setToolTip("")
        if step == self.GRID:
            self.grid_panel.focus()
            self.cancelButton.setToolTip("Без ключа SteamGridDB — картинки только из Steam. Добавить потом: "
                                         "«Настройки» → «Ключ SteamGridDB»")
        elif step == self.KEY:
            self.key_edit.setFocus()
            self.cancelButton.setToolTip("Ключа по-прежнему нет — закрыть мастер, программа работает как раньше"
                                         if self._known_no_key() else "")
        elif step == self.NOKEY:
            self.remember_text.setText(
                f"Запомнить аккаунт ({self._who(self.steam_id)}) на этом ПК? Это только номер — не пароль и не "
                "ключ. По нему программа проверит, что в клиент Steam вошли вы, и разделит саундтреки по "
                "аккаунтам. Стереть — «Настройки» → «Удалить ключ и выйти». Не запомните — программа забудет его "
                "при закрытии.")

    def _known_no_key(self):
        """«Без ключа» с этим аккаунтом уже выбирали — второй раз ничего не спрашиваем."""
        return bool(self._repeat_id) and self.steam_id == self._repeat_id

    def _on_primary(self):
        step = self.pages.currentIndex()
        if step == self.LOGIN:
            self._start_login()
        elif step == self.KEY:
            self._check_key()
        elif step == self.NOKEY:
            self._finish_no_key(remember=True)
        elif step == self.GRID:
            self.grid_panel.check()
        else:
            self.want_import = True
            self.accept()

    def _on_secondary(self):
        step = self.pages.currentIndex()
        if step == self.KEY and not self.had_key:
            if self._known_no_key():
                self._close_no_key()
            else:
                self._show_step(self.NOKEY)
        elif step == self.NOKEY:
            self._finish_no_key(remember=False)
        elif step == self.GRID:
            self._leave_grid(saved=False)
        else:
            self.skipped = step == self.LOGIN  # «Пропустить»; на остальных шагах тут «Отмена» и «Позже»
            self.reject()

    # --- Ключ SteamGridDB ---
    def _open_grid(self, after, note):
        """Необязательный шаг SteamGridDB, если ключа ещё нет; иначе сразу дальше."""
        self._after_grid = after
        if not self.need_grid:
            self._leave_grid(saved=False)
            return
        self.grid_panel.set_note(note)
        self._show_step(self.GRID)

    def _on_grid_busy(self, busy):
        self.yesButton.setEnabled(not busy)
        self.yesButton.setText("Проверяю…" if busy else "Проверить и сохранить")
        self.cancelButton.setEnabled(not busy)

    def _leave_grid(self, saved):
        self.cancelButton.setEnabled(True)
        if saved:
            self.grid_saved = True
            core.log.info("Мастер подключения: ключ SteamGridDB добавлен")
        elif self.need_grid:
            core.log.info("Мастер подключения: шаг SteamGridDB пропущен")
        if self._after_grid == "done":
            if saved:
                self.done_text.setText(self.done_text.text() + "\n\nКлюч SteamGridDB сохранён.")
            self._show_step(self.DONE)
        else:
            self.accept()

    def _login_again(self):
        """«Сменить аккаунт»: страница входа Steam сама откроет тот аккаунт, что помнит браузер, —
        поэтому объясняем, как сначала выйти из него на сайте Steam."""
        self._switch_from, self.steam_id = self.steam_id, ""
        self.previous_label.hide()
        self.login_status.hide()
        self.same_button.hide()
        self.login_note.setText(
            f"Браузер помнит вход, поэтому страница Steam снова откроет {self._who(self._switch_from)}. Сначала "
            f"выйдите: сайт Steam → имя аккаунта справа вверху → «Выйти из аккаунта». Потом — «Войти через Steam».")
        self.login_note.show()
        self.steam_site_button.show()
        self._show_step(self.LOGIN)

    def _use_account(self, steam_id):
        self._switch_from = ""
        self.same_button.hide()
        self.steam_site_button.hide()
        self.login_status.hide()
        self.login_note.hide()
        self.steam_id = steam_id
        self.previous_label.setVisible(self._known_no_key())
        self._show_account(steam_id)
        self._show_step(self.KEY)

    def _close_no_key(self):
        """Ключа по-прежнему нет, а «Без ключа» с этим аккаунтом уже выбирали: настройки не трогаем."""
        self.no_key = self.repeated = True
        self.session_id = "" if self.remembered else self.steam_id
        core.log.info("Мастер подключения: ключа по-прежнему нет — закрываю, всё остаётся как было")
        self.accept()

    def _finish_no_key(self, remember):
        try:
            core.save_settings(steam_setup="no_key", steam_id=self.steam_id if remember else "")
        except OSError as e:
            notify("error", self.win, "Не удалось сохранить настройки", str(e))
            return
        self.no_key = True
        self.session_id = "" if remember else self.steam_id
        core.log.info("Steam без ключа: SteamID %s", core.short_steam_id(self.steam_id) if remember
                      else "не запомнен — пользователь отказался, помню до закрытия программы")
        self._open_grid("accept", "✓ Готово: программа работает без ключа Steam. Остался один необязательный шаг.")

    # --- Вход ---
    def _start_login(self):
        if self._login:
            return
        try:
            self._login = core.SteamLogin()
        except OSError as e:
            self._set_login_status(f"Не удалось подготовить вход: {e}", warning=True)
            return
        core.log.info("Вход через Steam: открываю страницу входа во внешнем браузере")
        self._open_login_page()
        self.yesButton.setEnabled(False)
        self.yesButton.setText("Жду вход в браузере…")
        self.login_bar.show()
        self.reopen_button.show()
        self._set_login_status("Войдите на странице Steam — программа продолжит сама.")
        worker = Worker(self._login.wait, lambda: self._closed, parent=self.win)
        worker.finished.connect(self._on_login)
        worker.failed.connect(self._on_login_failed)
        worker.start()

    def _open_login_page(self):
        if self._login:
            QDesktopServices.openUrl(QUrl(self._login.url()))

    def _set_login_status(self, text, warning=False):
        self.login_status.setText(text)
        if warning:
            self.login_status.setTextColor(*WARNING_TEXT)
        else:
            self.login_status.setTextColor(QColor(0, 0, 0, 160), QColor(255, 255, 255, 170))
        self.login_status.show()

    def _reset_login(self):
        self._login = None
        self.login_bar.hide()
        self.reopen_button.hide()
        self.yesButton.setEnabled(True)
        self.yesButton.setText("Войти через Steam")

    def _on_login(self, steam_id):
        if self._closed or not steam_id:
            return
        self._reset_login()
        self.raise_()
        self.activateWindow()  # пользователь вернётся из браузера — окно уже на следующем шаге
        if self._switch_from and steam_id == self._switch_from:
            # Хотели сменить аккаунт, а браузер снова вошёл прежним — не идём дальше молча
            self._pending_id = steam_id
            self._set_login_status(f"Steam снова вошёл аккаунтом {self._who(steam_id)}: браузер всё ещё "
                                   f"помнит его. Выйдите из него на сайте Steam и нажмите «Войти через Steam» "
                                   f"ещё раз — или продолжайте с этим аккаунтом.", warning=True)
            self.same_button.setText(f"Продолжить с аккаунтом {self.names.get(steam_id) or core.short_steam_id(steam_id)}")
            self.same_button.show()
            return
        self._use_account(steam_id)

    def _on_login_failed(self, message):
        if self._closed:
            return
        self._reset_login()
        self._set_login_status("Не получилось войти: " + message.split(": ", 1)[-1] + ".", warning=True)

    # --- Ключ ---
    def _check_key(self):
        key = self.key_edit.text().strip()
        if not core.STEAM_KEY_RE.fullmatch(key):
            self._set_key_status("Ключ — это 32 знака из цифр и букв A–F. Скопируйте его со страницы целиком.",
                                 warning=True)
            return
        self.yesButton.setEnabled(False)
        self.yesButton.setText("Проверяю…")
        self.key_bar.show()
        self.key_status.hide()
        worker = Worker(core.check_steam_key, key, self.steam_id, parent=self.win)
        worker.finished.connect(lambda info: self._on_key_checked(key, info))
        worker.failed.connect(self._on_key_failed)
        worker.start()

    def _set_key_status(self, text, warning=False):
        self.key_status.setText(text)
        self.key_status.setTextColor(*(WARNING_TEXT if warning else (QColor(0, 0, 0, 160), QColor(255, 255, 255, 170))))
        self.key_status.show()

    def _end_key_check(self):
        self.key_bar.hide()
        self.yesButton.setEnabled(True)
        self.yesButton.setText("Проверить ключ")

    def _on_key_failed(self, message):
        if self._closed:
            return
        self._end_key_check()
        self._set_key_status(message.split(": ", 1)[-1] + ".", warning=True)

    def _on_key_checked(self, key, info):
        if self._closed:
            return
        self._end_key_check()
        try:
            core.save_settings(steam_api_key=key, steam_id=self.steam_id, steam_setup="done")
        except OSError as e:
            self._set_key_status(f"Не удалось сохранить ключ: {e}", warning=True)
            return
        self.connected = True
        core.log.info("Steam подключён: SteamID %s", core.short_steam_id(self.steam_id))
        name = f" — {info['name']}" if info.get("name") else ""
        self.done_title.setText(f"Steam подключён{name}")
        if info["private"]:
            self.done_text.setText("Ключ работает и сохранён.")
        else:
            # Ключ Steam Web API видит только ваши собственные игры — семейные и саундтреки Steam показывает
            # после входа в магазин, а он предлагается уже в окне импорта
            self.done_text.setText(
                f"Ваших игр в Steam: {info['games']} — пока это только купленные вами. Игры семейной группы и "
                "саундтреки появятся после входа в магазин Steam: окно импорта предложит войти («Войти и "
                "проверить»).\n\n«Импортировать игры» — " + ("откроет окно импорта с прошлым выбором."
                                                            if core.load_settings().get("import_done") else
                                                            "добавить все игры или только из выбранных коллекций."))
        if self.was_no_key:
            self.done_text.setText(self.done_text.text() + (
                "\n\nС ключом добавились импорт всей библиотеки и коллекций, «Синхронизировать» на вкладке "
                "«Игры Steam» и достижения в карточке игры. Уже добавленные игры останутся."))
        self.private_text.setVisible(info["private"])
        self.private_button.setVisible(info["private"])
        self._open_grid("done", f"✓ Steam подключён{name}: ключ проверен и сохранён. Остался один необязательный шаг.")

    def reject(self):
        self._closed = True  # вход в браузере, если он ещё ждёт, прекращается
        self.grid_panel.close_panel()
        super().reject()


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
    и каждая строка тоже выравнивается по центру (center=False — по левому краю). Скрытые элементы пропускаются."""

    def __init__(self, parent=None, spacing=10, center=True):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self._center = center
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
            x = area.x() + ((area.width() - row_width) // 2 if self._center else 0)
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

# Игра уже есть на аккаунте Steam (по последней синхронизации покупок) — в магазин идут за подарком или DLC
OWNED_STORE_TEXT = "Подарить или DLC"
OWNED_STORE_TIP = ("Игра уже есть на вашем аккаунте Steam. На её странице в магазине можно купить её в подарок "
                   "или докупить дополнения (DLC)")


def step_art(page, game, kind, step=1):
    """Значки «Фон» и «Логотип» в шапке страницы игры (Steam, порта или дисковой): другой вариант из SteamGridDB,
    step — вперёд (+1) или назад (-1). Он сохраняется вместо прежнего — при следующем открытии страницы будет он."""
    page.header.set_art_busy(kind, True)
    page._run(core.next_griddb_art, (dict(game), kind, core.griddb_key(), step),
              lambda result: _on_art_step(page, kind, result),
              lambda message: _on_art_step_failed(page, kind, message))


def _on_art_step(page, kind, result):
    page.header.set_art_busy(kind, False)
    if not result:
        notify("info", page.win, "В SteamGridDB вариантов нет",
               ("Фона" if kind == "hero" else "Логотипа") + " для этой игры там пока не выложили.", 5000)
        return
    page.header.set_art_counter(kind, result["index"], result["count"])
    pixmap = QPixmap.fromImage(QImage(result["path"]))
    if pixmap.isNull():
        return
    if kind == "hero":
        page.header.replace_hero(pixmap)
        page._run(core.make_backdrop, (result["path"],), page._on_backdrop)  # подложка страницы — из нового фона
    else:
        page.header.set_logo(pixmap)


def _on_art_step_failed(page, kind, message):
    page.header.set_art_busy(kind, False)
    notify("warning", page.win, "SteamGridDB не ответил", message.split(": ", 1)[-1], 6000)


class ShotThumb(QLabel):
    """Миниатюра снимка экрана в ленте записи. Файла нет (удалили из папки) — подпись вместо картинки."""

    SIZE = QSize(160, 90)
    clicked = Signal()

    def __init__(self, path, parent):
        super().__init__(parent)
        self.path = path
        self.setFixedSize(self.SIZE)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("QLabel {background: rgba(128, 128, 128, 40); border-radius: 6px; color: gray}")
        if path:
            self.setText("…")
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip(f"{os.path.basename(path)} — открыть")
        else:
            self.setText("файла нет")
            self.setToolTip("Снимок удалили из папки screenshots")

    def load(self):
        reader = QImageReader(self.path)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(self.SIZE, Qt.KeepAspectRatio))  # читается сразу маленьким
        image = reader.read()
        if image.isNull():
            self.setText("не открылся")
            return
        self.setPixmap(QPixmap.fromImage(image))

    def mouseReleaseEvent(self, event):
        if self.path and event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class NotesDialog(MessageBoxBase):
    """«Свои заметки» игры: общий текст об игре и записи по сессиям с датой — мысли после игры, наброски обзора
    или статьи. Слева — «Об игре» и записи, новые сверху; справа — редактор с простым оформлением.

    Всё сохраняется само (core.save_note): через секунду после правки, при переходе к другой записи и при закрытии
    окна. Пустые записи не сохраняются — нажали «Новая запись» и ничего не написали, после закрытия её не будет."""

    SAVE_DELAY_MS = 1000
    DELETE_CONFIRM_MS = 3000
    LIST_WIDTH = 230
    PREVIEW_CHARS = 30
    # «Точно удалить?» — красная кнопка, одинаково заметная в светлой и тёмной теме
    DELETE_ARMED_QSS = """
        PushButton { background-color: #C42B1C; color: white; border: 1px solid #A32317; }
        PushButton:hover { background-color: #D13438; }
        PushButton:pressed { background-color: #A32317; color: rgba(255, 255, 255, 0.85); }
    """

    def __init__(self, parent, game):
        super().__init__(parent)
        self.win, self.game = parent, game
        self.note = core.load_note(game)
        self.current = None  # что в редакторе: None — «Об игре», иначе запись (словарь из note["sessions"])
        self.rows = []  # что в каждой строке списка: None — «Об игре», иначе запись
        self._opened = False  # в редакторе уже текст из заметки — можно забирать его обратно
        self.dirty = False
        self.save_failed = ""
        self._delete_armed = False
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(self.SAVE_DELAY_MS)
        self.save_timer.timeout.connect(self.save)
        self.delete_timer = QTimer(self)
        self.delete_timer.setSingleShot(True)
        self.delete_timer.setInterval(self.DELETE_CONFIRM_MS)
        self.delete_timer.timeout.connect(self._reset_delete)

        title = SubtitleLabel(f"Свои заметки — {game['name']}", self)
        title.setWordWrap(True)
        hint = CaptionLabel("Хранятся только на этом ПК, в папке notes рядом с программой, и сохраняются сами.", self)
        hint.setWordWrap(True)

        new_button = PrimaryPushButton("Новая запись", self, FIF.ADD)
        new_button.setToolTip("Запись с сегодняшней датой — например, мысли после игровой сессии")
        new_button.clicked.connect(self.add_session)
        self.entries = ListWidget(self)
        self.entries.setFixedWidth(self.LIST_WIDTH)
        self.entries.currentRowChanged.connect(self._on_row)
        self.delete_button = PushButton("Удалить запись", self, FIF.DELETE)
        self.delete_button.clicked.connect(self.delete_session)
        self._delete_style = self.delete_button.styleSheet()  # обычный вид кнопки — вернуть после «Точно удалить?»
        export_button = PushButton("Экспорт в .md", self, FIF.SAVE)
        export_button.setToolTip("Вся заметка одним файлом Markdown: его открывает любой редактор, "
                                 "текст легко перенести в обзор или статью")
        export_button.clicked.connect(self.export)
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(new_button)
        left.addWidget(self.entries, 1)
        left.addWidget(self.delete_button)
        left.addWidget(export_button)

        self.entry_title = StrongBodyLabel("", self)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        for text, tip, handler, style in (
                ("Заголовок", "Заголовок раздела — ещё раз, чтобы убрать", self._heading, None),
                ("Ж", "Жирный (Ctrl+B)", self._bold, "bold"),
                ("К", "Курсив (Ctrl+I)", self._italic, "italic"),
                ("• Список", "Список с точками", lambda: self._list(QTextListFormat.ListDisc), None),
                ("1. Список", "Нумерованный список", lambda: self._list(QTextListFormat.ListDecimal), None)):
            button = TransparentPushButton(text, self)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            if style:
                font = QFont(button.font())
                font.setBold(style == "bold")
                font.setItalic(style == "italic")
                button.setFont(font)
            tools.addWidget(button)
        tools.addStretch(1)
        self.editor = TextEdit(self)
        self.editor.setMinimumHeight(max(240, min(460, parent.height() - 380)))
        self.editor.textChanged.connect(self._on_changed)
        for keys, handler in (("Ctrl+B", self._bold), ("Ctrl+I", self._italic)):
            shortcut = QShortcut(QKeySequence(keys), self.editor)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(handler)
        self.status = CaptionLabel("", self)
        self.status.setWordWrap(True)
        # Снимки экрана записи — лентой под текстом. Щелчок — снимок в программе просмотра Windows
        self.shots_title = CaptionLabel("", self)
        self.shots_folder = TransparentPushButton("Папка снимков", self, FIF.FOLDER)
        self.shots_folder.setToolTip("Все снимки этой игры — в папке screenshots рядом с программой")
        self.shots_folder.clicked.connect(self.open_shots_folder)
        shots_head = QHBoxLayout()
        shots_head.addWidget(self.shots_title)
        shots_head.addStretch(1)
        shots_head.addWidget(self.shots_folder)
        self.shots_strip = QWidget()
        self.shots_strip.setStyleSheet("background: transparent")
        self.shots_layout = QHBoxLayout(self.shots_strip)
        self.shots_layout.setContentsMargins(0, 0, 0, 0)
        self.shots_layout.setSpacing(8)
        self.shots_layout.addStretch(1)
        self.shots_scroll = ScrollArea(self)
        self.shots_scroll.setWidget(self.shots_strip)
        self.shots_scroll.setWidgetResizable(True)
        self.shots_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.shots_scroll.setFixedHeight(ShotThumb.SIZE.height() + 18)  # и место под полосу прокрутки
        self.shots_scroll.setStyleSheet("QScrollArea {background: transparent; border: none}")
        self._shot_queue = []
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self.entry_title)
        right.addLayout(tools)
        right.addWidget(self.editor, 1)
        right.addLayout(shots_head)
        right.addWidget(self.shots_scroll)
        right.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(16)
        row.addLayout(left)
        row.addLayout(right, 1)
        self.viewLayout.addWidget(title)
        self.viewLayout.addWidget(hint)
        self.viewLayout.addLayout(row)
        width = max(640, min(1000, parent.width() - 160))
        self.widget.setMinimumWidth(width)
        self.widget.setMaximumWidth(width)
        self.yesButton.setText("Готово")
        self.cancelButton.hide()

        self._fill_list()
        if self.note.get("broken"):
            self._set_status(f"Прежний файл заметки был повреждён — он отложен как {self.note['broken']} в папке "
                             "notes, заметка начата заново.", warning=True)
        QTimer.singleShot(0, self.editor.setFocus)

    # --- Список записей ---
    def _sessions(self):
        return sorted(self.note["sessions"], key=lambda s: (s["date"], s["created"]), reverse=True)

    def _preview(self, text):
        """Первая строка записи без разметки Markdown — подпись в списке."""
        for line in text.splitlines():
            line = re.sub(r"^\s*(#+|[-*+]|\d+[.)]|>)\s*", "", line).replace("**", "").replace("*", "").strip()
            if line:
                return line[:self.PREVIEW_CHARS] + ("…" if len(line) > self.PREVIEW_CHARS else "")
        return ""

    def _entry_text(self, entry):
        if entry is None:
            return "Об игре\n" + (self._preview(self.note["text"]) or "пока пусто")
        preview = self._preview(entry["text"])
        if not preview and entry.get("images"):
            preview = f"снимков: {len(entry['images'])}"
        return f"{core.russian_date(entry['date'])}\n{preview or 'пустая запись'}"

    def _fill_list(self, select=None):
        """Список заново; открыть в редакторе select (None — «Об игре»)."""
        self.entries.blockSignals(True)
        self.entries.clear()
        self.rows = [None] + self._sessions()
        height = QFontMetrics(self.entries.font()).height() * 2 + 16  # две строки: дата и начало текста
        for entry in self.rows:
            item = QListWidgetItem(self._entry_text(entry))
            item.setSizeHint(QSize(0, height))
            self.entries.addItem(item)
        self.entries.blockSignals(False)
        self.entries.setCurrentRow(next((i for i, e in enumerate(self.rows) if e is select), 0))

    def _refresh_row(self):
        row = next((i for i, e in enumerate(self.rows) if e is self.current), -1)
        item = self.entries.item(row) if row >= 0 else None
        if item:
            item.setText(self._entry_text(self.current))

    def _on_row(self, row):
        if 0 <= row < len(self.rows):
            self._store()
            self._open(self.rows[row])

    def _open(self, entry):
        self.current = entry
        self.editor.blockSignals(True)
        self.editor.setMarkdown(self.note["text"] if entry is None else entry["text"])
        self.editor.blockSignals(False)
        self._opened = True
        if entry is None:
            self.entry_title.setText("Об игре")
            self.editor.setPlaceholderText("Общие впечатления, что нравится и что нет, план обзора или статьи…")
        else:
            self.entry_title.setText(f"Запись от {core.russian_date(entry['date'])}")
            self.editor.setPlaceholderText("Что было в этой сессии: где остановились, что запомнилось, мысли…")
        self.delete_button.setEnabled(entry is not None)
        self._reset_delete()
        self._show_shots(entry)

    # --- Снимки записи ---
    def _show_shots(self, entry):
        while self.shots_layout.count() > 1:  # последней стоит растяжка
            widget = self.shots_layout.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        images = (entry or {}).get("images") or []
        for widget in (self.shots_title, self.shots_folder, self.shots_scroll):
            widget.setVisible(bool(images))
        self._shot_queue = []
        if not images:
            return
        self.shots_title.setText(f"Снимки экрана: {len(images)}")
        for image in images:
            path = core.screenshot_file(image)
            thumb = ShotThumb(path if path and os.path.exists(path) else None, self.shots_strip)
            if thumb.path:
                thumb.clicked.connect(lambda p=thumb.path: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
                self._shot_queue.append(thumb)
            self.shots_layout.insertWidget(self.shots_layout.count() - 1, thumb)
        QTimer.singleShot(0, self._load_next_shot)

    def _load_next_shot(self):
        """Миниатюры — по одной за проход: два десятка снимков 4K не подвешивают окно."""
        if not self._shot_queue:
            return
        thumb = self._shot_queue.pop(0)
        try:
            thumb.load()
        except RuntimeError:  # запись уже сменили — миниатюра удалена
            pass
        QTimer.singleShot(0, self._load_next_shot)

    def open_shots_folder(self):
        folder = core.screenshot_folder(self.game)
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            self._set_status("Папки со снимками этой игры ещё нет.", warning=True)

    def add_screenshot(self, session_id, image):
        """Снимок, сделанный, пока окно открыто, — сразу в заметку окна: иначе окно, сохраняясь, затёрло бы его."""
        self._store()
        session_id = core.attach_screenshot(self.note, session_id, image)
        self.dirty = True
        entry = next(s for s in self.note["sessions"] if s["id"] == session_id)
        if entry is self.current:
            self._show_shots(entry)
            self._refresh_row()
        else:
            self._fill_list(select=self.current)
        self.save()
        return session_id

    def _store(self):
        """Текст из редактора — в заметку (в «Об игре» или в открытую запись)."""
        if not self._opened:
            return
        text = self.editor.toMarkdown().strip()
        if self.current is None:
            self.note["text"] = text
        else:
            self.current["text"] = text

    # --- Записи ---
    def add_session(self):
        self._store()
        session = core.new_session()
        self.note["sessions"].append(session)
        self._fill_list(select=session)
        self.editor.setFocus()

    def delete_session(self):
        entry = self.current
        if entry is None:
            return
        self._store()
        if (entry["text"].strip() or entry.get("images")) and not self._delete_armed:
            # Запись с текстом или снимками удаляется вторым нажатием — случайно не потерять. Кнопка на это время красная
            self._delete_armed = True
            self.delete_button.setText("Точно удалить?")
            self.delete_button.setIcon(FIF.DELETE.icon(color=QColor("white")))
            self.delete_button.setStyleSheet(self._delete_style + self.DELETE_ARMED_QSS)
            self.delete_timer.start()
            return
        self._reset_delete()
        self.note["sessions"] = [s for s in self.note["sessions"] if s is not entry]
        self._opened = False  # в редакторе ещё текст удалённой записи — обратно его не забираем
        self.dirty = True
        self._fill_list()
        if self.save() and entry.get("images"):
            # Сами файлы программа не удаляет: снимок мог пригодиться и без записи
            self._set_status(f"Запись удалена. Её снимки ({len(entry['images'])}) остались в папке screenshots "
                             "рядом с программой.")

    def _reset_delete(self):
        self.delete_timer.stop()
        self._delete_armed = False
        self.delete_button.setText("Удалить запись")
        self.delete_button.setIcon(FIF.DELETE)
        self.delete_button.setStyleSheet(self._delete_style)

    # --- Оформление ---
    def _bold(self):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Normal if self.editor.currentCharFormat().font().bold() else QFont.Bold)
        self.editor.mergeCurrentCharFormat(fmt)

    def _italic(self):
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.editor.currentCharFormat().fontItalic())
        self.editor.mergeCurrentCharFormat(fmt)

    def _heading(self):
        cursor = self.editor.textCursor()
        block = cursor.blockFormat()
        level = 0 if block.headingLevel() else 2
        block.setHeadingLevel(level)
        cursor.beginEditBlock()
        cursor.setBlockFormat(block)
        cursor.movePosition(QTextCursor.StartOfBlock)
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setProperty(QTextFormat.FontSizeAdjustment, 2 if level else 0)
        cursor.mergeCharFormat(fmt)
        cursor.endEditBlock()

    def _list(self, style):
        cursor = self.editor.textCursor()
        current = cursor.currentList()
        if current is None:
            cursor.createList(style)
        elif current.format().style() == style:  # тот же список ещё раз — строка снова обычная
            current.remove(cursor.block())
            block = cursor.blockFormat()
            block.setIndent(0)
            cursor.setBlockFormat(block)
        else:
            fmt = current.format()
            fmt.setStyle(style)
            current.setFormat(fmt)

    # --- Сохранение ---
    def _on_changed(self):
        self.dirty = True
        self.save_timer.start()

    def _set_status(self, text, warning=False):
        self.status.setText(text)
        self.status.setTextColor(*(WARNING_TEXT if warning else (QColor(0, 0, 0, 160), QColor(255, 255, 255, 170))))

    def save(self):
        self.save_timer.stop()
        if not self.dirty:
            return True
        self._store()
        try:
            core.save_note(self.game, self.note)
        except OSError as e:
            self.save_failed = str(e)
            core.log.warning("Заметка «%s» не сохранилась: %s", self.game["name"], e)
            self._set_status(f"Не сохранилось: {e}", warning=True)
            return False
        self.dirty, self.save_failed = False, ""
        self._set_status("Сохранено в " + time.strftime("%H:%M"))
        self._refresh_row()
        return True

    def export(self):
        self._store()
        self.save()
        name = core.clean_name(self.game["name"]) or "Игра"
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт заметки",
                                              os.path.join(os.path.expanduser("~"), f"{name} — заметки.md"),
                                              "Markdown (*.md);;Текст (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(core.note_markdown(self.game, self.note))
        except OSError as e:
            self._set_status(f"Файл не сохранился: {e}", warning=True)
            return
        core.log.info("Заметка «%s» выгружена в файл %s", self.game["name"], os.path.basename(path))
        self._set_status(f"Выгружено в файл {os.path.basename(path)}")

    def done(self, code):
        self.save()
        self.delete_timer.stop()
        super().done(code)


def open_notes(win, game, button):
    """«Свои заметки» — окном поверх программы; после закрытия на кнопке видно, сколько в заметке записей."""
    dialog = NotesDialog(win, game)
    core.log.info("Открыты заметки «%s»", game["name"])
    win.notes_dialog = dialog  # снимок экрана, сделанный сейчас, пойдёт прямо в это окно
    try:
        dialog.exec()
    finally:
        win.notes_dialog = None
    failed = dialog.save_failed
    dialog.deleteLater()
    if failed:
        notify("error", win, "Заметка не сохранилась", f"{failed}. Подробности — на вкладке «Логи».", 8000)
    show_notes_count(button, game)


def show_notes_count(button, game):
    count = core.note_count(game)
    button.setText(f"Свои заметки · {count}" if count else "Свои заметки")
    button.setToolTip("Общая заметка об игре и записи по сессиям — хранятся только на этом ПК" +
                      (f". Сейчас частей: {count}" if count else ""))


# Enter в окнах поверх программы — как главная кнопка окна («ОК», «Применить», «Сохранить», «Далее»). В окнах
# библиотеки интерфейса главная кнопка не назначена кнопкой по умолчанию, и Enter в поле ввода ничего не делал.
# Поле с несколькими строками (заметки) Enter забирает себе — там он, как и положено, начинает новую строку
def _enter_confirms(cls):
    original = cls.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        button = getattr(self, "yesButton", None)
        if hasattr(button, "setDefault"):
            button.setAutoDefault(True)
            button.setDefault(True)

    cls.__init__ = init


for _dialog_class in (MessageBoxBase, MessageBox):
    _enter_confirms(_dialog_class)


class GamePage(QWidget):
    """Страница выпавшей игры: шапка с фоном и логотипом, кнопки, скриншоты и трейлеры, описание.

    Всё, что качается из интернета, загружается в фоне. Если за это время открыли другую игру,
    опоздавшие ответы отбрасываются по номеру открытия (_token).
    """

    def set_origin(self, collection):
        """Открыли из коллекции — кнопка «К коллекции «…»» видна; из рулетки или списка — нет."""
        self.origin = collection
        self.collection_button.setVisible(collection is not None)
        if collection is not None:
            self.collection_button.setText(f"К коллекции «{collection['name']}»")
        self.buttons.invalidate()

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
        self.origin = None  # коллекция, из которой открыли игру, — для кнопки «К коллекции»
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
        self.header.liveKeepRequested.connect(self._keep_live)
        self.header.artRequested.connect(lambda kind, step: self.game and step_art(self, self.game, kind, step))
        self._live_urls = []  # адреса вариантов живого фона открытой игры

        self.play_button = PrimaryPushButton("Играть", self, FIF.PLAY)
        self.play_button.clicked.connect(self.play)
        self.spin_button = PushButton("Крутить ещё", self, FIF.SYNC)
        self.spin_button.clicked.connect(self.home.spin_again)
        # Убирает игру только из рулетки: в «Играх Steam» и в коллекциях она остаётся
        self.done_button = PushButton("Убрать из рулетки", self, fluent_icon("REMOVE", FIF.DELETE))
        self.done_button.setToolTip("Игра больше не будет выпадать в рулетке, но останется в «Играх Steam» "
                                    "и в коллекциях — там её можно крутить")
        self.done_button.clicked.connect(lambda: self.home.remove_from_roulette(self.game))
        self.win.rouletteChanged.connect(lambda: self.game is not None and self.update_buttons())
        self.cover_button = PushButton("Своя обложка", self, FIF.PHOTO)
        self.cover_button.clicked.connect(self.home.choose_cover)
        self.notes_button = PushButton("Свои заметки", self, fluent_icon("QUICK_NOTE", FIF.EDIT))
        self.notes_button.clicked.connect(lambda: self.game and open_notes(self.win, self.game, self.notes_button))
        self.store_button = PushButton("Магазин Steam", self, FIF.SHOPPING_CART)
        self.store_button.clicked.connect(self.open_store)
        self.link_button = PrimaryPushButton("Найти в Steam", self, FIF.SEARCH)
        self.link_button.setToolTip("Игра добавлена вручную: найдите её в магазине Steam — появятся описание, "
                                    "скриншоты, трейлеры и установка")
        self.link_button.clicked.connect(lambda: self.win.library_page.link_game(self.game))
        self.soundtrack_button = PushButton("Слушать саундтрек", self, FIF.MUSIC)
        self.soundtrack_button.clicked.connect(self.listen_soundtrack)
        self.soundtrack_button.hide()
        back_button = TransparentPushButton("К рулетке", self, FIF.LEFT_ARROW)
        back_button.clicked.connect(self.home.back_to_reel)
        # Карточка одна и та же — выпала игра на рулетке или её открыли из списка, — поэтому и в список
        # можно вернуться всегда: игра в нём будет выделена
        library_button = TransparentPushButton("К играм Steam", self, FIF.LIBRARY)
        library_button.setToolTip("Вернуться на вкладку «Игры Steam» — к этой игре в списке")
        library_button.clicked.connect(self.home.back_to_library)
        # Открыли игру из коллекции — можно вернуться и туда (кнопка видна только тогда)
        self.collection_button = TransparentPushButton("К коллекции", self, fluent_icon("ALBUM", FIF.FOLDER))
        self.collection_button.clicked.connect(self.win.back_to_collection)
        self.collection_button.hide()

        # Кнопки по центру; в узком окне переносятся на вторую строку
        buttons_box = QWidget(self)
        buttons = CenteredFlowLayout(buttons_box, spacing=12)
        for button in (self.play_button, self.link_button, self.spin_button, self.done_button, self.notes_button,
                       self.cover_button, self.store_button, self.soundtrack_button, self.collection_button,
                       back_button, library_button):
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
        self.privacy_button = PushButton("Открыть настройки приватности", self.achievements_box.body, FIF.LINK)
        self.privacy_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(core.STEAM_PRIVACY_PAGE)))
        self.privacy_button.hide()
        self.key_button = PrimaryPushButton("Добавить ключ Steam", self.achievements_box.body, FIF.PEOPLE)
        self.key_button.setToolTip("Мастер подключения к Steam — сразу на шаге ключа, если SteamID запомнен")
        self.key_button.clicked.connect(lambda: self.win.run_steam_setup())
        self.key_button.hide()
        self.achievements_list = QGridLayout()
        self.achievements_list.setHorizontalSpacing(32)
        self.achievements_list.setVerticalSpacing(14)
        self.achievement_rows = []
        self.achievement_columns = 0
        self.achievements_box.body_layout.addWidget(self.achievements_progress, 0, Qt.AlignHCenter)
        self.achievements_box.body_layout.addWidget(self.achievements_note)
        self.achievements_box.body_layout.addWidget(self.privacy_button, 0, Qt.AlignHCenter)
        self.achievements_box.body_layout.addWidget(self.key_button, 0, Qt.AlignHCenter)
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
        previous, self.game = self.game, game
        self._live_urls = []
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
        if previous and previous is not game:
            # Ушли с игры, не выбрав вариант живого фона, — просмотренные варианты больше не нужны.
            # Шапка уже отпустила файл, удаляем в фоне
            Worker(core.discard_animated_variants, dict(previous), parent=self).start()
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
        show_notes_count(self.notes_button, game)
        self.scroll.verticalScrollBar().setValue(0)

        key = core.griddb_key()
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
            self._show_without_details("Игра добавлена вручную, без связи со Steam. Нажмите «Найти в Steam» — "
                                       "программа найдёт её в магазине, и здесь появятся описание, скриншоты "
                                       "и трейлеры.")
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
        pixmap = QPixmap.fromImage(QImage(path)) if path else QPixmap()  # файл мог смениться под тем же именем
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
        self._live_urls = info["urls"]
        if not self.header.show_live(info["path"], len(info["urls"]), info["chosen"]):
            core.log.warning("Живой фон %s не открылся (нет поддержки WebP в Qt?) — значок не показываю",
                             info["path"])

    def _download_live(self, index):
        """Нужен вариант живого фона номер index — качаем его в фоне, ход загрузки рисуется кольцом на значке.
        Уже скачанный вариант откроется сразу."""
        token, game = self._token, dict(self.game)
        url = self._live_urls[index] if 0 <= index < len(self._live_urls) else None
        if not url:
            self.header.live_failed()
            return
        relay = ProgressRelay(self)
        relay.progress.connect(lambda _text, percent: token == self._token and self.header.set_live_progress(percent))
        worker = Worker(core.download_animated_hero, game, index, url, lambda percent: relay.report("", percent),
                        lambda: token != self._token, parent=self)
        worker.finished.connect(lambda path: token == self._token and self._on_live_downloaded(path, index))
        worker.failed.connect(lambda message: token == self._token and self._on_live_failed(message))
        worker.finished.connect(relay.deleteLater)
        worker.failed.connect(relay.deleteLater)
        worker.start()

    def _on_live_downloaded(self, path, index):
        first = self.header.live_path is None  # первый скачанный вариант сразу становится выбранным
        if not path:
            self.header.live_failed()
        elif not self.header.live_ready(path, index, chosen=index if first else None):
            core.log.warning("Живой фон %s не открылся (нет поддержки WebP в Qt?)", path)
            notify("warning", self.win, "Живой фон не открылся", "Остаётся обычный фон.")

    def _on_live_failed(self, message):
        self.header.live_failed()
        notify("warning", self.win, "Не удалось скачать живой фон", message)

    def _keep_live(self, index):
        """✓ — оставить этот вариант живого фона, остальные просмотренные удалить с диска."""
        token, game = self._token, dict(self.game)
        self.header.release_live()  # файл сейчас переименуется — Qt должен его отпустить
        worker = Worker(core.keep_animated_hero, game, index, parent=self)
        worker.finished.connect(lambda result: token == self._token and self._on_live_kept(result, index))
        worker.failed.connect(lambda message: token == self._token and self._on_keep_failed(message))
        worker.start()

    def _on_live_kept(self, result, index):
        self.header.live_ready(result["path"], index, chosen=index)
        freed = f" Освободилось {disk_size_text(result['freed'])}." if result["freed"] >= 1024 ** 2 else ""
        notify("success", self.win, "Живой фон сохранён",
               f"Вариант {index + 1} из {len(self._live_urls)}. Остальные просмотренные удалены.{freed}")

    def _on_keep_failed(self, message):
        self.header.live_ready(self.header.live_path, self.header.live_index)  # остаёмся на том же варианте
        notify("warning", self.win, "Не удалось сохранить живой фон", message)

    # --- Размытая подложка под всей страницей ---
    def _on_backdrop(self, path):
        pixmap = QPixmap.fromImage(QImage(path)) if path else QPixmap()  # файл мог смениться под тем же именем
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
        release_caption = "Дата выхода"
        if not details["release_date"].strip():
            # На странице магазина даты нет — год из сведений, собранных для фильтров рулетки
            year = core.meta_year(self.win.steam_meta.get(str(int(self.game["appid"]))))
            release, release_caption = (str(year), "Год выхода") if year else ("", release_caption)
        values = {
            "genres": ("Жанры", ", ".join(details["genres"]), None),
            "release": (release_caption, release, None),
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
        if core.needs_translation(details) and core.translation_enabled():
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
        if self.chip_values:  # обзоры изданий и время прохождения — только у игр из магазина
            for title, url in self.press_links:
                chips.append(InfoChip("Обзор", title, url, self.chips_box))
            chips.append(InfoChip("How Long To Beat", "Сколько проходить", hltb_url(self.game["name"]),
                                  self.chips_box))
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
        self.privacy_button.hide()
        self.key_button.hide()
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
            self.key_button.setVisible(bool(data.get("no_key")))
            return
        self.achievements = data
        self.privacy_button.setVisible(bool(data.get("private")))
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
        self.link_button.setVisible(bool(self.game) and not appid)
        owned = bool(appid) and self.win.owns(appid)
        self.store_button.setText(OWNED_STORE_TEXT if owned else "Магазин Steam")
        self.store_button.setIcon(FIF.HEART if owned else FIF.SHOPPING_CART)
        self.store_button.setToolTip(OWNED_STORE_TIP if owned else "Страница игры в магазине Steam")
        self.play_button.setText("Играть" if installed else "Установить")
        self.play_button.setIcon(FIF.PLAY if installed else FIF.DOWNLOAD)
        self.done_button.setVisible(bool(self.game) and not self.win.roulette.has(self.game))
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
        self.remember_box.setToolTip("Поменять выбор можно в «Настройках» → «Встроенный браузер»")
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


if QWebEngineView:
    class StorePage(QWebEnginePage):
        """Страница встроенного браузера. Ссылки steam:// («Установить» в магазине) Qt сам отдаёт Windows прямо
        в потоке окна — пока Steam откликается, окно висит до 15–30 секунд. Поэтому такие ссылки перехватываем:
        программа сама передаёт их в Steam в фоне — и только безопасные команды (MainWindow.open_steam_link)."""

        steamLink = Signal(QUrl)

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if url.scheme().lower() == "steam":
                self.steamLink.emit(QUrl(url))
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)


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
    """Встроенный браузер на всю рабочую область: третий экран после рулетки и карточки игры,
    а с standalone — отдельная вкладка «Браузер» со строкой адреса и поиска.

    Движок браузера создаётся при запуске программы (см. preload). Куки и кэш сайтов хранятся
    в cache/web — второй раз страницы открываются быстрее, согласие на куки не спрашивают заново.
    """

    backRequested = Signal()
    MUSIC_SPACE = 286  # справа поверх лежит мини-плеер
    HOME_URL = "https://store.steampowered.com/"

    def __init__(self, parent=None, standalone=False):
        super().__init__(parent)
        self.view = None
        self.standalone = standalone
        self._checkout_warned = False

        self.return_button = PrimaryPushButton("К карточке", self, FIF.RETURN)
        self.return_button.clicked.connect(self.backRequested)
        self.return_button.setVisible(not standalone)
        outside = TransparentPushButton("Во внешнем браузере", self, FIF.GLOBE)
        outside.clicked.connect(self.open_outside)
        self.back_button = TransparentToolButton(FIF.LEFT_ARROW, self)
        self.back_button.setToolTip("Назад")
        self.forward_button = TransparentToolButton(FIF.RIGHT_ARROW, self)
        self.forward_button.setToolTip("Вперёд")
        reload = TransparentToolButton(FIF.SYNC, self)
        reload.setToolTip("Обновить")
        home = TransparentToolButton(FIF.HOME, self)
        home.setToolTip("Магазин Steam")
        home.clicked.connect(lambda: self.open(self.HOME_URL))
        home.setVisible(standalone)
        self.title_label = StrongBodyLabel("", self)
        self.title_label.setMinimumWidth(80)
        # Строка адреса и поиска — только на вкладке «Браузер»: со страницы игры открываются её ссылки
        self.address = LineEdit(self)
        self.address.setClearButtonEnabled(True)
        self.address.setPlaceholderText("Адрес сайта или поиск в Google — и Enter")
        self.address.returnPressed.connect(self._go)
        self.address.setVisible(standalone)
        self.title_label.setVisible(not standalone)
        self.bar = ProgressBar(self)
        self.bar.setFixedHeight(3)
        self.bar.hide()

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(20, 12, self.MUSIC_SPACE, 8)
        toolbar.setSpacing(6)
        toolbar.addWidget(self.return_button)
        if not standalone:  # у карточки — как раньше: возврат и внешний браузер слева
            toolbar.addWidget(outside)
            toolbar.addSpacing(8)
        toolbar.addWidget(self.back_button)
        toolbar.addWidget(self.forward_button)
        toolbar.addWidget(reload)
        toolbar.addWidget(home)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.title_label, 1)
        toolbar.addWidget(self.address, 1)
        if standalone:  # на вкладке слева навигация, по центру адрес, справа внешний браузер
            toolbar.addSpacing(8)
            toolbar.addWidget(outside)

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
        page = StorePage(web_profile(self), self.view)
        page.steamLink.connect(self._on_steam_link)
        self.view.setPage(page)
        self.view.titleChanged.connect(self._on_title)
        self.view.urlChanged.connect(self._update_nav)
        self.view.loadStarted.connect(lambda: (self.bar.setValue(0), self.bar.show()))
        self.view.loadProgress.connect(self.bar.setValue)
        self.view.loadFinished.connect(self._on_loaded)
        self.back_button.clicked.connect(self.view.back)
        self.forward_button.clicked.connect(self.view.forward)
        self._reload.clicked.connect(self.view.reload)

    def _on_steam_link(self, url):
        window = self.window()
        if hasattr(window, "open_steam_link"):
            window.open_steam_link(url)

    def open(self, url, back_text="К карточке"):
        self._ensure_view()
        if self.page_box.indexOf(self.view) < 0:  # заготовка с запуска или новый движок — ставим на страницу
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
        url = self.view.url() if self.view else QUrl()
        if self.standalone and not self.address.hasFocus() and url.scheme() in ("http", "https"):
            self.address.setText(url.toString())
            self.address.setCursorPosition(0)
        self._warn_checkout(url)

    def _warn_checkout(self, url):
        """Дошли до оплаты в магазине Steam — советуем платить не во встроенном браузере.

        Встроенный браузер — часть программы, а программа с открытым кодом может оказаться чужой
        изменённой копией. Данные карты безопаснее вводить там, где программа их точно не видит.
        """
        if self._checkout_warned or not url.host().endswith("steampowered.com"):
            return
        if not url.path().startswith("/checkout"):
            return
        self._checkout_warned = True
        core.log.info("Встроенный браузер: открыта оплата в магазине Steam — советую клиент Steam или внешний браузер")
        notify("warning", self.window(), "Оплату лучше завершить вне программы",
               "Данные карты безопаснее вводить в клиенте Steam или в обычном браузере — нажмите "
               "«Во внешнем браузере». При оплате кошельком Steam данные карты не нужны.", 12000)

    def _go(self):
        """Адрес из строки: похож на сайт — открываем его, иначе ищем в Google."""
        text = self.address.text().strip()
        if not text:
            return
        if " " not in text and "." in text:
            url = QUrl.fromUserInput(text)
        else:
            url = QUrl(f"https://www.google.com/search?q={quote_plus(text)}")
        self.address.clearFocus()
        self.open(url.toString())

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.view:
            # Ушли к карточке — видео и звук на странице не должны играть в фоне
            self.view.page().setAudioMuted(True)

    def showEvent(self, event):
        super().showEvent(event)
        if self.view:
            self.view.page().setAudioMuted(False)


class WebPage(QWidget):
    """Вкладка «Браузер»: встроенный браузер со строкой адреса, чтобы быстро что-то найти, не открывая
    карточку игры. Профиль браузера общий с карточкой: вход в магазин Steam один на оба.

    Свой движок создаётся при первом открытии вкладки — окно к этому времени уже работает через видеокарту
    (браузер карточки создан при запуске), поэтому мигания нет.
    """

    def __init__(self, window):
        super().__init__()
        self.setObjectName("webPage")
        self.win = window
        self.browser = BrowserPage(self, standalone=True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.browser)
        # Мини-плеер поверх, в правом верхнем углу — как на рулетке и в браузере карточки
        self.mini_player = MiniPlayer(window, self)
        self.mini_player.sizeChanged.connect(self._place_music_buttons)
        window.music.trackChanged.connect(lambda: QTimer.singleShot(0, self._place_music_buttons))

    def open(self, url):
        self.browser.open(url)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.browser.view:
            self.browser.open(BrowserPage.HOME_URL)
        QTimer.singleShot(0, self._place_music_buttons)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_music_buttons()

    def _place_music_buttons(self):
        right = HomePage.MARGINS[2]
        size = self.mini_player.sizeHint()
        y = 12 + max(0, (self.browser.address.sizeHint().height() - size.height()) // 2)  # на линии строки адреса
        self.mini_player.setGeometry(self.width() - right - size.width(), y, size.width(), size.height())
        self.mini_player.raise_()


class FilmBackdrop(QWidget):
    """Фон вкладки «Рулетка»: наклонные ленты «фотоплёнки» из скачанных обложек, соседние едут навстречу.

    Фон размыт и притушен, чтобы барабан поверх него читался чётко. Рисуем его в картинку втрое меньше
    окна и растягиваем: так дешевле, а растяжение само добавляет мягкости.

    Кнопкой «Фон» под барабаном ленты можно остановить или поставить вместо них свою картинку — она идёт
    под то же матовое стекло (MODES)."""

    MODES = ("film", "still", "custom")

    SCALE = 3  # во сколько раз картинка фона меньше окна
    ANGLE = 18  # наклон лент в градусах: верх уходит влево, как «\»
    SPEED = 8  # пикселей маленькой картинки в секунду — на экране втрое больше
    FPS = 30
    STRIPS = 8  # разных лент; если на экран нужно больше — они повторяются
    CORNER = 10  # скругление левого верхнего угла области страниц у окна Fluent

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
        self.custom = None  # своя картинка (уже размытая), когда mode == "custom"
        mode = core.load_settings().get("roulette_backdrop", "film")
        self.mode = "film"
        self.set_mode(mode if mode in self.MODES else "film")

    def set_mode(self, mode):
        """Сменить фон: film — ленты едут, still — стоят, custom — своя картинка. Своей нет — ленты."""
        self.custom = None
        if mode == "custom":
            path = core.roulette_backdrop()
            image = QImage(path) if path else QImage()
            if image.isNull():
                mode = "film"
            else:
                self.custom = QPixmap.fromImage(image)
        self.mode = mode
        self.frame = None
        if mode == "custom":
            if self.opacity == 0.0:
                self.fade_anim.start()
        else:
            self.rebuild_if_empty()
        if mode == "film" and self.isVisible():
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _has_picture(self):
        return self.custom is not None if self.mode == "custom" else bool(self.strips)

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
        if self.strips and not had_strips and self.opacity == 0.0:
            self.fade_anim.start()  # первое появление — плавно
        self.frame = None
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
        if self.mode == "film" and self.strips and not self.window().isMinimized():
            self._render()
            self.update()

    def _render(self):
        w = max(1, math.ceil(self.width() / self.SCALE))
        h = max(1, math.ceil(self.height() / self.SCALE))
        if self.frame is None or self.frame.width() != w or self.frame.height() != h:
            self.frame = QImage(w, h, QImage.Format_RGB32)
        self.frame.fill(QColor(*core.FILM_COLOR))
        if not self._has_picture():
            return
        painter = QPainter(self.frame)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)  # плавный сдвиг на доли пикселя

        if self.mode == "custom":  # своя картинка — на весь кадр, лишнее по краям обрезается
            scale = max(w / self.custom.width(), h / self.custom.height())
            cw, ch = self.custom.width() * scale, self.custom.height() * scale
            painter.drawPixmap(QRectF((w - cw) / 2, (h - ch) / 2, cw, ch), self.custom, QRectF(self.custom.rect()))
        else:
            self._draw_strips(painter, w, h)
        self._draw_wash(painter, w, h)
        painter.end()

    def _draw_strips(self, painter, w, h):
        t = time.monotonic() - self.started if self.mode == "film" else 0.0  # «стоят» — один и тот же кадр
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

    @staticmethod
    def _draw_wash(painter, w, h):
        # Вуаль цвета темы, в центре — плотнее: там барабан
        dark = isDarkTheme()
        wash = QColor(22, 22, 26, 165) if dark else QColor(240, 240, 244, 185)
        painter.fillRect(0, 0, w, h, wash)
        glow = QRadialGradient(QPointF(w / 2, h * 0.52), max(w, h) * 0.55)
        glow.setColorAt(0, QColor(wash.red(), wash.green(), wash.blue(), 70))
        glow.setColorAt(1, QColor(wash.red(), wash.green(), wash.blue(), 0))
        painter.fillRect(0, 0, w, h, glow)

    def paintEvent(self, event):
        if not self._has_picture():
            return
        if self.frame is None or getattr(self, "_frame_dark", None) != isDarkTheme():
            self._frame_dark = isDarkTheme()  # сменили тему — вуаль другого цвета, даже если ленты стоят
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
        if self.mode != "custom":
            self.rebuild_if_empty()
        if self.mode == "film":
            self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()  # на другой вкладке и на странице игры фон не крутим


def pil_pixmap(image):
    """Картинка PIL (RGB) → QPixmap. None → None."""
    if image is None:
        return None
    image = image.convert("RGB")
    qimage = QImage(image.tobytes("raw", "RGB"), image.width, image.height, image.width * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())


class SelectionBackdrop(QWidget):
    """Подложка вкладки с играми: выделили игру — за списком её обложка под матовым стеклом, как фон рулетки;
    выделили несколько — коллаж из их обложек. Сменили выделение — картинки плавно сменяют друг друга,
    сняли — подложка уходит. Лежит под всем остальным на вкладке и мышь не ловит."""

    DELAY_MS = 160  # выделяют мышью с Shift — собираем, когда перестали щёлкать
    FADE_MS = 450
    CORNER = FilmBackdrop.CORNER
    instances = []  # все подложки — переключатель в «Настройках» включает и выключает их разом

    def __init__(self, host):
        super().__init__(host)
        SelectionBackdrop.instances.append(self)
        self.host = host
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.pixmap = self.old = None
        self.fade = 1.0
        self.names = ()
        self.wanted = ()  # выделенные игры — даже когда подложка выключена, чтобы сразу показать при включении
        self._token = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.DELAY_MS)
        self.timer.timeout.connect(self._build)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(self.FADE_MS)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._set_fade)
        host.installEventFilter(self)  # вкладка поменяла размер — подложка тоже
        self.setGeometry(host.rect())
        self.lower()

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() == QEvent.Resize:
            self.setGeometry(self.host.rect())
            self.lower()
        return False

    def set_names(self, names):
        """Названия выделенных игр (обложки ищутся по названию)."""
        self.wanted = tuple(dict.fromkeys(n for n in names if n))[:core.SELECTION_MAX]
        settings = getattr(self.window(), "settings", None) or core.load_settings()  # без чтения с диска
        names = self.wanted if settings.get("selection_backdrop", True) else ()
        if names != self.names:
            self.names = names
            self.timer.start()

    def apply_setting(self):
        """Переключили подложку в «Настройках» — показать или убрать её сразу."""
        self.set_names(self.wanted)

    def refresh(self, name=None):
        """Обложка игры сменилась — если она на подложке, собрать подложку заново."""
        if name is None or name in self.names:
            self.timer.start()

    def _build(self):
        self._token += 1
        token = self._token
        paths = [p for p in (core.find_cover(n) for n in self.names) if p]
        if not paths:
            self._show(None)
            return
        worker = Worker(core.selection_backdrop, paths, parent=self)
        worker.finished.connect(lambda image: token == self._token and self._show(pil_pixmap(image)))
        worker.start()

    def _show(self, pixmap):
        if pixmap is None and self.pixmap is None:
            return
        self.old = self.pixmap if self.fade >= 0.5 else self.old  # уходящая картинка — та, что сейчас видна
        self.pixmap = pixmap
        self.fade = 0.0
        self.anim.stop()
        self.anim.start()

    def _set_fade(self, value):
        self.fade = float(value)
        if self.fade >= 1.0:
            self.old = None
        self.update()

    def _draw(self, painter, pixmap, opacity):
        if pixmap is None or opacity <= 0:
            return
        w, h = self.width(), self.height()
        painter.setOpacity(opacity)
        scale = max(w / pixmap.width(), h / pixmap.height())  # во весь фон, лишнее по краям обрезается
        pw, ph = pixmap.width() * scale, pixmap.height() * scale
        painter.drawPixmap(QRectF((w - pw) / 2, (h - ph) / 2, pw, ph), pixmap, QRectF(pixmap.rect()))
        FilmBackdrop._draw_wash(painter, w, h)  # то же матовое стекло, что у фона рулетки

    def paintEvent(self, event):
        if self.pixmap is None and self.old is None:
            return
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        corner = QPainterPath()  # скругляем только левый верхний угол, как у самой области страниц
        corner.addRoundedRect(QRectF(0, 0, self.width() + self.CORNER, self.height() + self.CORNER),
                              self.CORNER, self.CORNER)
        painter.setClipPath(corner)
        self._draw(painter, self.old, 1.0 - self.fade)
        self._draw(painter, self.pixmap, self.fade)


class MetaCollector(QObject):
    """Жанры, год выхода и отзывы игр Steam для фильтров рулетки — собираются в фоне после импорта, синхронизации
    и при запуске (то, что не успели). Один сбор за раз; собранное лежит в win.steam_meta."""

    changed = Signal()  # пришла новая порция сведений или сбор закончился
    _progress = Signal(int, int)  # из фонового потока
    _done = Signal(object)

    DELAY_MS = 4000  # список игр меняется пачкой (импорт) — начинаем, когда он успокоится
    RETRY_MS = 10 * 60 * 1000  # магазин перестал отвечать — пробуем снова через 10 минут
    RELOAD_S = 3  # сведения с диска перечитываются не чаще раза в 3 с
    NOTICE_MIN = 10  # столько игр без сведений — предупреждаем, что фильтры будут позже

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.running = False
        self.state = None  # (готово, всего), пока идёт сбор
        self._again = False
        self._stop = False
        self._loaded_at = 0.0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.start)
        self._progress.connect(self._on_progress)
        self._done.connect(self._on_done)

    def appids(self):
        return sorted({int(g["appid"]) for g in self.win.library.games if g.get("appid")})

    def missing(self, appids=None):
        meta = self.win.steam_meta
        return [a for a in (self.appids() if appids is None else appids) if not core.meta_ready(meta.get(str(a)))]

    def schedule(self, delay=None):
        self.timer.start(self.DELAY_MS if delay is None else delay)

    def stop(self):
        self._stop = True

    def start(self):
        if self.running:
            self._again = True  # список поменялся во время сбора — после него соберём и новые
            return
        appids = self.appids()
        missing = self.missing(appids)
        now = time.time()
        stale = any(now - (self.win.steam_meta.get(str(a)) or {}).get("store_fetched", 0) > core.STEAM_META_STORE_TTL
                    for a in appids)
        if not appids or not (missing or stale):
            return
        if len(missing) >= self.NOTICE_MIN:
            core.log.info("Собираю жанры, годы и оценки для фильтров рулетки: игр без сведений — %d", len(missing))
            notify("info", self.win, "Фильтры рулетки — чуть позже",
                   f"Собираю из магазина Steam жанры, год выхода и отзывы: {game_word(len(missing))}. Это идёт в фоне "
                   "и займёт какое-то время — пока сбор не закончится, фильтры знают не все игры. Ход сбора — "
                   "на «Рулетке», под заголовком.", 9000)
        self.running, self._again, self._stop = True, False, False
        self.state = (len(appids) - len(missing), len(appids))
        self.changed.emit()
        threading.Thread(target=self._run, args=(appids,), daemon=True).start()

    def _run(self, appids):
        try:
            result = core.collect_steam_meta(appids, lambda done, total: self._progress.emit(done, total),
                                             lambda: self._stop)
        except Exception:  # noqa: BLE001 — без сведений фильтры просто не знают игру, программа работает
            core.log.exception("Сбор жанров и оценок для фильтров рулетки сломался")
            result = None
        try:
            self._done.emit(result)
        except RuntimeError:
            pass  # программу закрыли

    def _on_progress(self, done, total):
        self.state = (done, total)
        # Сведения перечитываем и сообщаем о них не на каждую игру: барабан с фильтрами пересобирается по сигналу
        if time.monotonic() - self._loaded_at >= self.RELOAD_S:
            self._loaded_at = time.monotonic()
            self.win.steam_meta = core.load_steam_meta()
            self.changed.emit()

    def _on_done(self, result):
        self.running, self.state = False, None
        self.win.steam_meta = core.load_steam_meta()
        self.changed.emit()
        if self._again:
            self.schedule(1000)
        elif result and result.get("stopped"):
            self.schedule(self.RETRY_MS)


class RouletteFilterDialog(MessageBoxBase):
    """«Фильтры» рулетки: жанры, год выхода и отзывы. Только для игр Steam — по сведениям из магазина Steam;
    игры, о которых сведений ещё нет, под включённый фильтр не попадают."""

    GENRE_COLUMNS = 3
    DIALOG_WIDTH = 640
    DIALOG_PAD = 72  # поля окна по бокам и полоса прокрутки жанров
    DIALOG_SIDE = 60  # от краёв окна программы

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.dialog_width = self.DIALOG_WIDTH
        filters = win.roulette_filters()
        self.games = [g for g in win.library.games if g.get("appid") and not win.roulette.has(g)]
        self.manual = sum(1 for g in win.library.games if not g.get("appid") and not win.roulette.has(g))
        self._load_entries()
        counts = {}
        for entry in self.entries:
            for genre in entry.get("genres") or []:
                counts[genre] = counts.get(genre, 0) + 1
        for genre in filters.get("genres") or []:
            counts.setdefault(genre, 0)  # жанр выбран раньше, а игр с ним сейчас нет — галочку не теряем
        years = sorted({y for y in (core.meta_year(e) for e in self.entries) if y}
                       | {y for y in (filters.get("year_from"), filters.get("year_to")) if isinstance(y, int)})

        self.viewLayout.addWidget(SubtitleLabel("Фильтры рулетки", self))
        self.viewLayout.addWidget(plain_label(
            "Только для игр Steam. Жанры, год выхода и отзывы — из магазина Steam. Игры, о которых сведений ещё нет, "
            "под включённый фильтр не попадают, пока сведения не соберутся.", self, caption=True))

        self.viewLayout.addWidget(StrongBodyLabel("Жанры", self))
        self.genre_boxes = {}
        if counts:
            self.viewLayout.addWidget(plain_label("Подойдёт игра с любым из отмеченных жанров. Ничего не отмечено — "
                                                  "любые.", self, caption=True))
            body = QWidget()
            grid = QGridLayout(body)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(8)
            chosen = set(filters.get("genres") or [])
            for genre, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
                box = CheckBox(f"{genre} ({count})", body)
                box.setChecked(genre in chosen)
                box.stateChanged.connect(self._update)
                self.genre_boxes[genre] = box
            # Окно — по самому длинному жанру («Многопользовательские игры (12)»), чтобы подпись не обрезалась;
            # в три колонки не влезает в окно программы — жанры встают в две
            column = max(box.sizeHint().width() for box in self.genre_boxes.values())
            room = win.width() - 2 * self.DIALOG_SIDE
            columns = self.GENRE_COLUMNS
            while columns > 2 and column * columns + 16 * (columns - 1) + self.DIALOG_PAD > room:
                columns -= 1
            self.dialog_width = max(self.DIALOG_WIDTH, min(room, column * columns + 16 * (columns - 1) +
                                                           self.DIALOG_PAD))
            for i, box in enumerate(self.genre_boxes.values()):
                grid.addWidget(box, i // columns, i % columns)
            scroll = ScrollArea(self)
            scroll.setWidget(body)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")
            rows = math.ceil(len(counts) / columns)
            scroll.setFixedHeight(min(rows * 34 + 4, 240))
            self.viewLayout.addWidget(scroll)
        else:
            self.viewLayout.addWidget(plain_label("Жанры появятся, когда соберутся сведения об играх.", self,
                                                  caption=True))

        self.viewLayout.addWidget(StrongBodyLabel("Год выхода", self))
        self.years = years
        self.year_from = ComboBox(self)
        self.year_from.addItems(["любого"] + [str(y) for y in years])
        self.year_to = ComboBox(self)
        self.year_to.addItems(["любой"] + [str(y) for y in years])
        for box, value in ((self.year_from, filters.get("year_from")), (self.year_to, filters.get("year_to"))):
            box.setCurrentIndex(years.index(value) + 1 if value in years else 0)
            box.currentIndexChanged.connect(self._update)
        year_row = QHBoxLayout()
        year_row.setSpacing(10)
        year_row.addWidget(BodyLabel("с", self))
        year_row.addWidget(self.year_from)
        year_row.addWidget(BodyLabel("по", self))
        year_row.addWidget(self.year_to)
        year_row.addStretch(1)
        self.viewLayout.addLayout(year_row)

        self.viewLayout.addWidget(StrongBodyLabel("Отзывы в Steam", self))
        self.reviews_box = ComboBox(self)
        self.reviews_box.addItems([title for _, title in core.REVIEW_LEVELS])
        levels = [level for level, _ in core.REVIEW_LEVELS]
        minimum = filters.get("reviews") or 0
        self.reviews_box.setCurrentIndex(levels.index(minimum) if minimum in levels else 0)
        self.reviews_box.currentIndexChanged.connect(self._update)
        self.viewLayout.addWidget(self.reviews_box, 0, Qt.AlignLeft)

        self.status = plain_label("", self, caption=True)
        self.viewLayout.addWidget(self.status)
        reset = TransparentPushButton("Сбросить фильтры", self, FIF.CANCEL)
        reset.clicked.connect(self._reset)
        self.counter = StrongBodyLabel("", self)
        self.counter.setTextColor(themeColor(), themeColor())
        bottom = QHBoxLayout()
        bottom.addWidget(self.counter, 1)
        bottom.addWidget(reset)
        self.viewLayout.addLayout(bottom)

        self.yesButton.setText("Применить")
        self.cancelButton.setText("Отмена")
        self.widget.setMinimumWidth(self.dialog_width)
        self.widget.setMaximumWidth(self.dialog_width)
        win.meta.changed.connect(self._on_meta)  # сбор идёт — счётчики оживают прямо в окне
        self._update()

    def _load_entries(self):
        meta = self.win.steam_meta
        self.entries = [meta.get(str(int(g["appid"]))) or {} for g in self.games]

    def _on_meta(self):
        try:
            self._load_entries()
            self._update()
        except RuntimeError:
            pass  # окно уже закрыто

    def filters(self):
        """Выбор в окне — новым словарем (настройки по умолчанию общие, их не трогаем)."""
        year_from = self.years[self.year_from.currentIndex() - 1] if self.year_from.currentIndex() > 0 else None
        year_to = self.years[self.year_to.currentIndex() - 1] if self.year_to.currentIndex() > 0 else None
        if year_from and year_to and year_from > year_to:
            year_from, year_to = year_to, year_from
        return {"genres": sorted(g for g, box in self.genre_boxes.items() if box.isChecked()),
                "year_from": year_from, "year_to": year_to,
                "reviews": core.REVIEW_LEVELS[max(0, self.reviews_box.currentIndex())][0]}

    def _reset(self):
        for box in self.genre_boxes.values():
            box.setChecked(False)
        self.year_from.setCurrentIndex(0)
        self.year_to.setCurrentIndex(0)
        self.reviews_box.setCurrentIndex(0)
        self._update()

    def _update(self, *_):
        filters = self.filters()
        ready = sum(1 for e in self.entries if core.meta_ready(e))
        total = len(self.games)
        status = f"Сведения собраны: {ready} из {game_word(total)} Steam."
        if self.win.meta.running:
            status += " Сбор идёт в фоне — когда закончится, под фильтры попадут и остальные."
        elif ready < total:
            status += " Остальные соберутся позже — программа попробует сама."
        if self.manual:
            status += (f" Добавленные вручную без связи со Steam ({self.manual}) под фильтры не попадают: "
                       "сведений о них нет.")
        self.status.setText(status)
        if not core.filters_active(filters):
            self.counter.setText(f"Фильтры выключены — крутятся все игры Steam: {total + self.manual}")
            return
        matched = sum(1 for e in self.entries if core.meta_matches(e, filters))
        self.counter.setText(f"Подходит: {game_word(matched)}" if matched else "Под эти фильтры не подходит ни одна игра")


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
        self.mini_player.sizeChanged.connect(self._place_music_buttons)  # раскрылся — встаёт у правого края
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
        self.win.portsChanged.connect(self.on_library_changed)
        self.win.collectionsChanged.connect(self.on_library_changed)  # коллекцию могут крутить
        self.win.rouletteChanged.connect(self.on_library_changed)  # игру убрали из рулетки или сменили фильтры
        self.win.meta.changed.connect(self._on_meta_changed)
        self.win.covers.coverReady.connect(self.reel.refresh_cover)
        self.win.covers.progress.connect(self.on_cover_progress)
        self.win.covers.finished.connect(self.on_covers_finished)
        self.reel.show_idle(self.win.roulette_games())
        self.stack.setCurrentWidget(self.reel_view)
        self.update_count()
        self.update_buttons()

    NOTE_MS = 12000  # столько висит итог импорта над загрузкой обложек, потом исчезает

    def _build_loading_view(self):
        view = QWidget()
        # Итог импорта — здесь, а не всплывающим уведомлением: оно легло бы поверх этих надписей.
        # Ширина фиксированная: с переносом строк и выравниванием по центру Qt иначе даёт надписи высоту
        # одной строки, и текст обрезался («Импорт завершён. Подходит под»)
        self.loading_note = BodyLabel("", view)
        self.loading_note.setTextColor(themeColor(), themeColor())
        self.loading_note.setAlignment(Qt.AlignCenter)
        self.loading_note.setWordWrap(True)
        self.loading_note.setFixedWidth(560)
        self.loading_note.hide()
        self.note_timer = QTimer(self)
        self.note_timer.setSingleShot(True)
        self.note_timer.timeout.connect(lambda: self.set_loading_note(""))
        title = SubtitleLabel("Подготавливаем обложки", view)
        text = BodyLabel("Скачиваем картинки для ваших игр, чтобы барабан крутился красиво.\n"
                         "Это нужно один раз — дальше обложки берутся с диска.", view)
        text.setAlignment(Qt.AlignCenter)
        self.progress_bar = ProgressBar(view)
        self.progress_bar.setFixedWidth(420)
        self.progress_label = CaptionLabel("", view)
        # «Пропустить» здесь нет: без обложек барабан и плитка выглядят пустыми, а загрузка идёт один раз.
        # Карточки игр и другие вкладки открываются и во время неё

        layout = QVBoxLayout(view)
        layout.setSpacing(14)
        layout.addStretch(1)
        layout.addWidget(self.loading_note, 0, Qt.AlignHCenter)
        layout.addSpacing(10)
        for widget in (title, text, self.progress_bar, self.progress_label):
            layout.addWidget(widget, 0, Qt.AlignHCenter)
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
        # Что крутить — видно, когда кроме игр Steam есть порты или дисковые игры; варианты — из того, что есть
        self.pool_box = ComboBox(view)
        self.pool_box.setToolTip("Что крутит рулетка: игры Steam, порты, дисковые игры — отдельно или вместе, "
                                 "или одну из ваших коллекций")
        self._pool_keys, self._pool_labels, self._pool_titles = [], [], {}
        self.pool_box.currentIndexChanged.connect(self._on_pool_changed)
        # Фильтры по жанру, году и отзывам — только когда крутятся игры Steam
        self.filter_button = TransparentPushButton("Фильтры", view, FIF.FILTER)
        self.filter_button.clicked.connect(self.edit_filters)
        # Фон рулетки: ленты едут, стоят или своя картинка — если от движения укачивает
        self.backdrop_button = TransparentPushButton("Фон", view, FIF.PHOTO)
        self.backdrop_button.setToolTip("Фон рулетки: ленты из обложек (едут или стоят) или своя картинка")
        self.backdrop_button.clicked.connect(self._backdrop_menu)

        spin_row = QHBoxLayout()
        spin_row.setSpacing(12)
        spin_row.addStretch(1)
        spin_row.addWidget(self.spin_button)
        spin_row.addWidget(self.pool_box)
        spin_row.addWidget(self.filter_button)
        spin_row.addWidget(self.backdrop_button)
        spin_row.addStretch(1)

        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addStretch(1)
        layout.addWidget(self.reel)
        layout.addWidget(self.name_label)
        layout.addSpacing(8)
        layout.addLayout(spin_row)
        layout.addStretch(1)
        return view

    # Варианты в порядке показа; показываются те, для которых все источники не пусты
    POOLS = (("steam", "Игры Steam"), ("steam+ports+discs", "Все игры"), ("steam+ports", "Steam и порты"),
             ("steam+discs", "Steam и дисковые"), ("ports+discs", "Порты и дисковые"), ("ports", "Только порты"),
             ("discs", "Только дисковые"))
    POOL_NAMES = {"steam": "игры Steam", "ports": "порты", "discs": "дисковые"}

    def _on_pool_changed(self, index):
        if not 0 <= index < len(self._pool_keys):
            return
        pool = self._pool_keys[index]
        if pool == self.win.roulette_pool():
            return
        self.win.settings["roulette_pool"] = pool
        core.save_settings(roulette_pool=pool)
        core.log.info("Рулетка крутит: %s", self._pool_titles.get(pool, pool))
        self.current = None
        self.name_label.setText("")
        self.on_library_changed()
        for page in (self.win.native_page, self.win.disc_page):
            page.page.refresh()  # кнопка «Крутить ещё» на странице игры

    # --- Фон рулетки ---
    BACKDROP_TITLES = {"film": "Ленты из обложек — едут", "still": "Ленты из обложек — стоят",
                       "custom": "Своя картинка"}

    def _backdrop_menu(self):
        menu = RoundMenu(parent=self)
        current = self.backdrop.mode
        for mode, icon in (("film", FIF.PLAY), ("still", FIF.PAUSE)):
            action = Action(FIF.ACCEPT if mode == current else icon, self.BACKDROP_TITLES[mode], menu)
            action.triggered.connect(lambda _=False, m=mode: self._set_backdrop(m))
            menu.addAction(action)
        if core.roulette_backdrop():
            mine = Action(FIF.ACCEPT if current == "custom" else FIF.PHOTO, "Своя картинка", menu)
            mine.triggered.connect(lambda: self._set_backdrop("custom"))
            menu.addAction(mine)
        menu.addSeparator()
        pick = Action(FIF.FOLDER, "Выбрать картинку…", menu)
        pick.setToolTip("Программа сделает размытую копию — сам файл не трогается")
        pick.triggered.connect(self._pick_backdrop)
        menu.addAction(pick)
        button = self.backdrop_button
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _set_backdrop(self, mode):
        self.backdrop.set_mode(mode)
        mode = self.backdrop.mode  # своей картинки могло не оказаться
        core.save_settings(roulette_backdrop=mode)
        core.log.info("Фон рулетки: %s", self.BACKDROP_TITLES[mode])

    def _pick_backdrop(self):
        path, _ = QFileDialog.getOpenFileName(self, "Картинка для фона рулетки", "",
                                              "Изображения (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not path:
            return
        show_busy(self.win, "Готовлю фон…", "Делаю размытую копию картинки. Сам файл не трогаю.")
        worker = Worker(core.save_roulette_backdrop, path, parent=self)
        worker.finished.connect(lambda _path: (hide_busy(self.win), self._set_backdrop("custom")))
        worker.failed.connect(lambda message: (hide_busy(self.win), notify(
            "error", self.win, "Фон не поставился", message.split(": ", 1)[-1], 6000)))
        worker.start()

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
        if not self.browser or external_only(url):
            QDesktopServices.openUrl(QUrl(url))
            return
        choice = link_choice(self.win, url)
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

    def set_loading_note(self, text):
        """Строка над «Подготавливаем обложки» — например, итог импорта. Сама уходит через NOTE_MS. Пусто — убрать."""
        self.note_timer.stop()
        self.loading_note.setText(text)
        self.loading_note.setVisible(bool(text))
        if text:
            self.note_timer.start(self.NOTE_MS)

    def show_reel(self):
        self.set_loading_note("")
        if self.stack.currentWidget() is self.reel_view:
            return
        if self.current is None:
            self.reel.show_idle(self.win.roulette_games())
        self._set_view(self.reel_view)
        self.reel.play_intro()

    def back_to_reel(self):
        self.show_reel()

    def back_to_library(self):
        """С карточки — на вкладку «Игры Steam», к этой игре. Карточка остаётся открытой на «Рулетке»."""
        game = self.game_page.game
        self.game_page.stop_video()
        self.win.switchTo(self.win.library_page)
        if game:
            self.win.library_page.reveal(game)

    def open_game(self, game):
        """Страница игры — после прокрутки или из списка игр. Выпал порт или дисковая игра — её страница
        на её вкладке."""
        if self.reel.is_spinning():
            return
        if core.is_port(game):
            self.current = game
            self.reel.show_idle(self.win.roulette_games(), center=game)
            self.show_name(game["name"])
            page = self.win.disc_page if core.is_disc(game) else self.win.native_page
            self.win.switchTo(page)
            page.open_port(game)
            return
        self.current = game
        # При возврате к рулетке барабан стоит на этой игре
        self.reel.show_idle(self.win.roulette_games(), center=game)
        self.show_name(game["name"])
        self.game_page.show_game(game)
        self.game_page.set_origin(self.win.game_origin)
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
        pool = self.win.roulette_pool()
        removed = self.win.roulette.has
        lists = {"steam": self.win.library.games, "ports": self.win.ports.ports, "discs": self.win.discs.ports}
        counts = {key: sum(1 for g in games if not removed(g)) for key, games in lists.items()}
        sources = pool.split("+")
        filtered = self.win.filters_on()
        # Сколько убрано из рулетки, не пишем: убранные игры в неё не возвращаются, число ни к чему
        if pool.startswith("col:"):
            collection = self.win.collections.find(pool[4:])
            text = f"Крутим коллекцию «{collection['name']}»: {game_word(len(self.win.roulette_games()))}"
        elif sources == ["steam"] and filtered:
            text = f"Подходит под фильтры: {len(self.win.roulette_games())} из {counts['steam']}"
        elif sources == ["steam"]:
            text = f"Игр в рулетке: {counts['steam']}"
        else:
            text = "Крутим: " + " + ".join(f"{self.POOL_NAMES[s]} {counts[s]}" for s in sources)
        state = self.win.meta.state
        if state and state[0] < state[1]:
            text += f" · собираю жанры, годы и отзывы для фильтров: {state[0]} из {state[1]}"
        self.count_label.setText(text)
        self._update_filter_button(pool, filtered)
        # Выбор «что крутить» — из тех вариантов, для которых есть игры, и коллекции, в которых есть игры.
        # Кроме Steam ничего нет — не показываем
        available = set(self.win.pool_sources())
        keys = [key for key, _ in self.POOLS if set(key.split("+")) <= available]
        titles = dict(self.POOLS)
        for collection in self.win.collections.collections:
            if self.win.collection_games(collection):
                key = f"col:{collection['id']}"
                keys.append(key)
                titles[key] = f"Коллекция «{collection['name']}»"
        labels = [titles[key] for key in keys]
        self._pool_titles = titles
        if keys != self._pool_keys or labels != self._pool_labels:
            self._pool_keys, self._pool_labels = keys, labels
            self.pool_box.blockSignals(True)
            self.pool_box.clear()
            self.pool_box.addItems(labels)
            self.pool_box.blockSignals(False)
        self.pool_box.setVisible(len(keys) > 1)
        index = keys.index(pool) if pool in keys else 0
        if self.pool_box.currentIndex() != index:
            self.pool_box.blockSignals(True)
            self.pool_box.setCurrentIndex(index)
            self.pool_box.blockSignals(False)

    # --- Фильтры для игр Steam ---
    def _update_filter_button(self, pool, filtered):
        active = core.filters_active(self.win.roulette_filters())
        self.filter_button.setVisible(any(g.get("appid") for g in self.win.library.games))
        self.filter_button.setEnabled(pool == "steam")
        filters = self.win.roulette_filters()
        parts = sum(bool(x) for x in (filters.get("genres"), filters.get("year_from") or filters.get("year_to"),
                                       filters.get("reviews")))
        self.filter_button.setText(f"Фильтры · {parts}" if active else "Фильтры")
        if pool != "steam":
            self.filter_button.setToolTip("Фильтры по жанру, году и отзывам — только для игр Steam: выберите «Игры "
                                          "Steam» в списке рядом" + (". Сейчас они не действуют" if active else ""))
        else:
            self.filter_button.setToolTip("Крутить только игры нужных жанров, лет и с хорошими отзывами"
                                          if not filtered else "Фильтры включены — нажмите, чтобы изменить или сбросить")

    def edit_filters(self):
        if self.reel.is_spinning():
            return
        dialog = RouletteFilterDialog(self.win)
        core.log.info("Открыто окно «Фильтры рулетки»")
        accepted = dialog.exec()
        filters = dialog.filters()
        dialog.deleteLater()
        if not accepted or filters == self.win.roulette_filters():
            return
        self.win.settings["roulette_filters"] = filters
        core.save_settings(roulette_filters=filters)
        core.log.info("Фильтры рулетки: %s", self.filters_text(filters) or "выключены")
        self.current = None
        self.name_label.setText("")
        self.win.rouletteChanged.emit()
        if core.filters_active(filters) and not self.win.roulette_games():
            notify("warning", self.win, "Под фильтры ничего не подходит",
                   "Ослабьте фильтры или подождите, пока соберутся сведения об играх." if self.win.meta.running else
                   "Ослабьте фильтры: «Фильтры» рядом с кнопкой «Крутить».", 6000)

    @staticmethod
    def filters_text(filters):
        parts = []
        if filters.get("genres"):
            parts.append("жанры " + ", ".join(filters["genres"]))
        if filters.get("year_from") or filters.get("year_to"):
            parts.append(f"годы {filters.get('year_from') or '…'}–{filters.get('year_to') or '…'}")
        if filters.get("reviews"):
            parts.append(f"отзывы от {filters['reviews']} %")
        return "; ".join(parts)

    def _on_meta_changed(self):
        """Пришла порция жанров и оценок. С фильтрами под них могли подойти новые игры — тогда барабан пересобираем."""
        if self.win.filters_on():
            ids = [id(g) for g in self.win.roulette_games()]
            if ids != getattr(self, "_filtered_ids", None):
                self._filtered_ids = ids
                self.on_library_changed()
                return
        self.update_count()

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
        games = self.win.roulette_games()
        if self.current is not None and not any(g is self.current for g in games):
            self.current = None
            self.name_label.setText("")
            if self._on_page() and not core.is_port(self.game_page.game or {}):
                game = self.game_page.game
                if not game or not any(g is game for g in self.win.library.games):
                    self.show_reel()  # игру убрали из списка — её страница больше не нужна
        if self.current is None:
            self.reel.show_idle(games)
        self.update_buttons()

    def spin(self):
        games = self.win.roulette_games()
        if self.reel.is_spinning():
            return
        if not games:
            if self.win.filters_on():
                notify("warning", self.win, "Под фильтры ничего не подходит",
                       "Сведения об играх ещё собираются — подождите немного или ослабьте фильтры."
                       if self.win.meta.running else "Ослабьте фильтры: «Фильтры» рядом с кнопкой «Крутить».", 6000)
            elif self.win.roulette.removed and not self.win.roulette_pool().startswith("col:"):
                notify("warning", self.win, "Крутить нечего",
                       "Все игры убраны из рулетки. Их можно крутить в коллекциях: «Коллекции» → «Крутить эту "
                       "коллекцию», — или добавьте новые игры.", 7000)
            else:
                notify("warning", self.win, "Список пуст", "Добавьте игры на вкладке «Игры Steam».")
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
            self.win.game_origin = None  # выпала на рулетке — «назад» к рулетке, а не в коллекцию
            self.open_game(game)

    SECTION_NAMES = {"steam": "«Игры Steam»", "port": "«Игры Native Port's»", "disc": "«Дисковые игры»"}

    def remove_from_roulette(self, game):
        """«Убрать из рулетки»: игра больше не выпадает на барабане, но остаётся в своём разделе и в коллекциях.
        Вернуть её в рулетку нельзя — крутить можно в коллекции."""
        if not game or self.win.roulette.has(game):
            return
        name = game["name"]
        section = self.SECTION_NAMES[game_kind(game)]
        if not confirm(self.win, "Убрать из рулетки?",
                       f"«{name}» больше не будет выпадать в рулетке. Из раздела {section} игра никуда не денется: "
                       "её можно запускать, открывать её страницу и добавлять в коллекции, как раньше.\n\n"
                       "Вернуть игру в рулетку будет нельзя. Но если захотите снова её крутануть — добавьте её "
                       "в коллекцию и нажмите «Крутить эту коллекцию»: в коллекции крутятся все её игры, "
                       "и убранные из рулетки тоже.", "Убрать из рулетки"):
            return
        self.win.roulette.add(game)
        try:
            self.win.roulette.save()
        except OSError as e:
            self.win.roulette.undo(game)
            notify("error", self.win, "Не удалось сохранить список рулетки", str(e))
            return
        core.log.info("«%s» убрана из рулетки (%s), в разделе осталась", name, GAME_KIND_NAMES[game_kind(game)])
        if self.current is game:
            self.current = None
            self.name_label.setText("")
        self.win.rouletteChanged.emit()
        notify("success", self.win, "Убрано из рулетки",
               f"«{name}» осталась в разделе {section}. Крутить её можно в коллекции.", 6000)

    def mark_done(self):
        """До 2.4.5 «Пройдена» убирала игру из списка вместе с обложкой — теперь только из рулетки."""
        self.remove_from_roulette(self.current)

    def choose_cover(self):
        if self.current:
            self.win.library_page.choose_cover(self.current)


# --- Страница «Игры» --------------------------------------------------------
class CoverThumbs(QObject):
    """Уменьшенные обложки для плитки на вкладке «Игры». Читаются с диска в фоновом потоке, по мере того как
    плитки появляются на экране, и хранятся в памяти не больше LIMIT штук: 800 полноразмерных обложек
    заняли бы сотни мегабайт. get() → QPixmap, None (обложки нет) или LOADING (ещё читается)."""

    LOADING = object()
    LIMIT = 500
    QUEUE_LIMIT = 120  # прокрутили далеко — старые просьбы уже не нужны
    _loaded = Signal(str, object)  # название, QImage или None — из фонового потока

    def __init__(self, size, parent=None):
        super().__init__(parent)
        self.size = size  # размер обложки на плитке в точках экрана
        self.ratio = 1.0
        self.updated = None  # что вызвать, когда обложка готова (перерисовать плитку)
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._queue, self._queued = [], set()
        self._running = False
        self._loaded.connect(self._on_loaded)

    def get(self, name):
        if name in self._cache:
            self._cache.move_to_end(name)
            return self._cache[name]
        with self._lock:
            if name not in self._queued:
                self._queued.add(name)
                self._queue.append(name)
                for old in self._queue[:-self.QUEUE_LIMIT]:
                    self._queued.discard(old)
                del self._queue[:-self.QUEUE_LIMIT]
            if self._running:
                return self.LOADING
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()
        return self.LOADING

    def forget(self, name):
        """Обложка игры сменилась — перечитать её с диска."""
        self._cache.pop(name, None)

    def clear(self):
        self._cache.clear()

    def _run(self):
        width, height = round(self.size.width() * self.ratio), round(self.size.height() * self.ratio)
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                name = self._queue.pop()  # последние просьбы — то, что сейчас на экране
                self._queued.discard(name)
            path = core.find_cover(name)
            image = QImage(path) if path else None
            if image is not None and image.isNull():
                image = None
            if image is not None:
                image = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            try:
                self._loaded.emit(name, image)
            except RuntimeError:
                return  # программу закрыли

    def _on_loaded(self, name, image):
        pixmap = None
        if image is not None:
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(self.ratio)
        self._cache[name] = pixmap
        while len(self._cache) > self.LIMIT:
            self._cache.popitem(last=False)
        if self.updated:
            self.updated()


class GridFit(QObject):
    """Плитки в сетке — ровно по ширине: сколько колонок влезает, на столько и делится ширина, а каждая плитка
    стоит по центру своей клетки. Без этого Qt ставит плитки с фиксированным шагом от левого края, и всё, что
    не влезло в ещё одну колонку, остаётся пустой полосой справа."""

    def __init__(self, view, tile, gap=8):
        super().__init__(view)
        self.view, self.tile, self.gap = view, QSize(tile), gap
        self._width = -1
        view.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self.fit()
        return False

    def fit(self, force=False):
        if self.view.viewMode() != QListView.IconMode:
            return
        width = self.view.viewport().width()
        if width <= 0 or (width == self._width and not force):
            return
        self._width = width
        columns = max(1, width // (self.tile.width() + self.gap))
        self.view.setGridSize(QSize(width // columns, self.tile.height() + self.gap))


class CoverTileDelegate(QStyledItemDelegate):
    """Плитка игры: обложка со скруглёнными углами и название под ней. Выбранные — в рамке цвета темы."""

    COVER = QSize(132, 198)
    PAD = 8
    TEXT_H = 38  # две строки названия

    def __init__(self, thumbs, parent=None):
        super().__init__(parent)
        self.thumbs = thumbs
        if isinstance(parent, QListView):  # плитки — ровно по ширине вкладки, без пустой полосы справа
            self.grid_fit = GridFit(parent, self.sizeHint(None, None))

    def sizeHint(self, option, index):
        return QSize(self.COVER.width() + 2 * self.PAD, self.COVER.height() + self.TEXT_H + 2 * self.PAD)

    def paint(self, painter, option, index):
        name = index.data(Qt.UserRole) or ""
        dark = isDarkTheme()
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        rect = QRectF(option.rect)
        painter.save()
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        if selected or hover:
            painter.setPen(Qt.NoPen)
            alpha = (22 if selected else 12) if dark else (14 if selected else 8)
            painter.setBrush(QColor(255, 255, 255, alpha) if dark else QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)

        cover = QRectF(rect.x() + (rect.width() - self.COVER.width()) / 2, rect.y() + self.PAD,
                       self.COVER.width(), self.COVER.height())
        path = QPainterPath()
        path.addRoundedRect(cover, 8, 8)
        pixmap = self.thumbs.get(name)
        painter.save()
        painter.setClipPath(path)
        if isinstance(pixmap, QPixmap):
            painter.drawPixmap(cover, pixmap, QRectF(pixmap.rect()))
        else:
            painter.fillRect(cover, QColor(255, 255, 255, 22) if dark else QColor(0, 0, 0, 14))
            if pixmap is None:  # обложки нет — название на карточке, как на барабане
                painter.setPen(QColor(255, 255, 255, 200) if dark else QColor(0, 0, 0, 180))
                font = QFont(option.font)
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(cover.adjusted(10, 10, -10, -10), Qt.AlignCenter | Qt.TextWordWrap, name)
        painter.restore()
        painter.setBrush(Qt.NoBrush)
        if selected:
            painter.setPen(QPen(themeColor(), 3))
            painter.drawRoundedRect(cover.adjusted(-1.5, -1.5, 1.5, 1.5), 9, 9)
        else:
            painter.setPen(QPen(QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 30), 1))
            painter.drawPath(path)

        # Название — до двух строк под обложкой, не влезло — многоточие
        text_rect = QRectF(rect.x() + 4, cover.bottom() + 6, rect.width() - 8, self.TEXT_H - 6)
        painter.setFont(option.font)
        painter.setPen(QColor(255, 255, 255, 230) if dark else QColor(0, 0, 0, 220))
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                         two_lines(name, QFontMetrics(option.font), round(text_rect.width())))
        painter.restore()


def two_lines(text, metrics, width):
    """Текст в две строки по ширине: что не влезло во вторую — многоточием."""
    words, first = text.split(), ""
    while words and metrics.horizontalAdvance((first + " " + words[0]).strip()) <= width:
        first = (first + " " + words.pop(0)).strip()
    if not first:  # первое слово длиннее строки
        return metrics.elidedText(text, Qt.ElideRight, width)
    rest = " ".join(words)
    return first + ("\n" + metrics.elidedText(rest, Qt.ElideRight, width) if rest else "")


# Порядок игр на вкладках с играми. Новые — по порядку добавления: так работает и для игр, добавленных до 2.4
SORT_TITLES = {"name": "По названию: А–Я", "name_desc": "По названию: Я–А", "new": "Сначала новые",
               "old": "Сначала давние", "played": "Больше наиграно", "recent": "Недавно запускали"}


def sort_games(games, mode):
    """Игры в выбранном порядке (SORT_TITLES). «Сначала новые» — добавленные последними сверху: после
    синхронизации со Steam сразу видно, что добавилось."""
    indexed = list(enumerate(games))

    def by_name(pair):
        return core.natural_key(pair[1]["name"])

    if mode == "name_desc":
        indexed.sort(key=by_name, reverse=True)
    elif mode == "new":
        indexed.reverse()
    elif mode == "old":
        pass
    elif mode == "played":
        indexed.sort(key=lambda p: (-(p[1].get("playtime") or 0), by_name(p)))
    elif mode == "recent":
        indexed.sort(key=lambda p: (-(p[1].get("last_played") or 0), by_name(p)))
    else:
        indexed.sort(key=by_name)
    return [game for _, game in indexed]


def make_sort_box(parent, modes, setting, on_change):
    """Выбор порядка игр; выбор запоминается в настройке setting."""
    box = ComboBox(parent)
    box.addItems([SORT_TITLES[m] for m in modes])
    current = core.load_settings().get(setting, "name")
    box.setCurrentIndex(modes.index(current) if current in modes else 0)
    box.setToolTip("Порядок игр. «Сначала новые» — добавленные последними сверху, например после синхронизации")

    def changed(index):
        if 0 <= index < len(modes):
            core.save_settings(**{setting: modes[index]})
            on_change()

    box.currentIndexChanged.connect(changed)
    box.mode = lambda: modes[box.currentIndex()] if 0 <= box.currentIndex() < len(modes) else "name"
    return box


class MiniPlayerSpot:
    """Мини-плеер в правом верхнем углу вкладки, на линии её заголовка — как на рулетке. Лежит поверх
    раскладки; вкладка зовёт place() из resizeEvent и showEvent."""

    def __init__(self, page, title, margins=(36, 20, 36, 28)):
        self.page, self.title = page, title
        self.top, self.right = margins[1], margins[2]
        self.player = MiniPlayer(page.win, page)
        self.player.sizeChanged.connect(self.place)  # раскрылся — встаёт у правого края
        page.win.music.trackChanged.connect(lambda: QTimer.singleShot(0, self.place))

    def place(self):
        size = self.player.sizeHint()
        y = self.top + max(0, (self.title.sizeHint().height() - size.height()) // 2)
        self.player.setGeometry(self.page.width() - self.right - size.width(), y, size.width(), size.height())
        self.player.raise_()


class LibraryPage(QWidget):
    """Вкладка «Игры»: список игр — строками или плиткой с обложками (выбор запоминается). Поиск, правый
    клик (запуск, магазин, обложка, удаление), выделение нескольких игр и двойной клик — страница игры — в обоих видах."""

    VIEWS = ("list", "grid")
    SORTS = ("name", "name_desc", "new", "old")

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
        self.thumbs = CoverThumbs(CoverTileDelegate.COVER, self)
        self.grid = QListWidget(self)
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)  # плитки переносятся по ширине окна
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setFrameShape(QFrame.NoFrame)
        self.grid.setMouseTracking(True)  # подсветка плитки под мышью
        self.grid.viewport().setMouseTracking(True)
        self.grid.viewport().setAttribute(Qt.WA_Hover)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(30)
        self.grid.setItemDelegate(CoverTileDelegate(self.thumbs, self.grid))
        self.grid.setStyleSheet("QListWidget {background: transparent; border: none; outline: none}")
        if SmoothScrollDelegate:
            self._grid_scroll = SmoothScrollDelegate(self.grid)  # полосы прокрутки как у остальных списков
        self.thumbs.updated = self.grid.viewport().update
        for view in (self.list, self.grid):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self.show_menu(v, pos))
            # Двойной щелчок — страница игры, как в коллекциях; запуск — с неё или из меню по правому клику
            view.itemDoubleClicked.connect(lambda item: self.open_page(self.game_of(item)))
            view.viewport().installEventFilter(self)  # клик мимо игр — снять выделение
        # Esc снимает выделение (а в строке поиска — сначала стирает поиск)
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.setContext(Qt.WidgetWithChildrenShortcut)
        escape.activated.connect(self._on_escape)
        self.views = QStackedWidget(self)
        self.views.addWidget(self.list)
        self.views.addWidget(self.grid)

        # Вид — двумя кнопками справа от поиска
        self.list_button = TransparentToggleToolButton(FIF.MENU, self)
        self.list_button.setToolTip("Списком")
        self.list_button.clicked.connect(lambda: self.set_view("list"))
        self.grid_button = TransparentToggleToolButton(FIF.PHOTO, self)
        self.grid_button.setToolTip("Плиткой с обложками — сразу видно, у каких игр обложки нет или она не та. "
                                    "Правый клик по игре → своя обложка или обложка из SteamGridDB")
        self.grid_button.clicked.connect(lambda: self.set_view("grid"))
        self.sort_box = make_sort_box(self, self.SORTS, "library_sort", self.refresh)
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_row.addWidget(self.search, 1)
        search_row.addSpacing(6)
        search_row.addWidget(self.sort_box)
        search_row.addSpacing(6)
        search_row.addWidget(self.list_button)
        search_row.addWidget(self.grid_button)

        self.import_button = PrimaryPushButton("Импорт из Steam", self, FIF.DOWNLOAD)
        self.import_button.clicked.connect(self.import_from_steam)
        self.sync_button = PushButton("Синхронизировать", self, FIF.UPDATE)
        self.sync_button.setToolTip("Повторить импорт с прошлым выбором коллекций — без окна настроек")
        self.sync_button.clicked.connect(self.quick_sync)
        self.add_button = PushButton("Добавить", self, FIF.ADD)
        self.add_button.clicked.connect(lambda: self.add_games())
        self.delete_button = PushButton("Удалить выбранные", self, FIF.DELETE)
        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button = PushButton("Очистить список", self, FIF.BROOM)
        self.clear_button.clicked.connect(self.clear_library)

        # В узком окне (половина экрана) кнопки переносятся на вторую строку, а не сжимаются
        buttons_box = QWidget(self)
        buttons = CenteredFlowLayout(buttons_box, spacing=12, center=False)
        for button in (self.import_button, self.sync_button, self.add_button, self.delete_button,
                       self.clear_button):
            buttons.addWidget(button)
        self._quick = False
        self._had_token = False  # при последней загрузке был вход в магазин Steam (семейная группа проверялась)
        self.set_import_busy(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addWidget(self.title)
        layout.addWidget(self.count_label)
        layout.addLayout(search_row)
        layout.addWidget(self.views, 1)
        layout.addWidget(buttons_box)
        self.mini = MiniPlayerSpot(self, self.title)
        # Выделили игры — за списком их обложки под матовым стеклом
        self.backdrop = SelectionBackdrop(self)
        for view in (self.list, self.grid):
            view.itemSelectionChanged.connect(self._on_selection)

        self.win.libraryChanged.connect(self.refresh)
        self.win.covers.coverReady.connect(self.cover_changed)
        self.refresh()
        view = core.load_settings().get("library_view", "list")
        self.set_view(view if view in self.VIEWS else "list", save=False)

    def _on_selection(self):
        self.backdrop.set_names([item.data(Qt.UserRole) for item in self.current_view().selectedItems()])

    def set_view(self, view, save=True):
        self.views.setCurrentWidget(self.grid if view == "grid" else self.list)
        self.list_button.setChecked(view == "list")
        self.grid_button.setChecked(view == "grid")
        self._on_selection()
        if save:
            core.save_settings(library_view=view)
            core.log.info("Вкладка «Игры Steam»: вид — %s", "плиткой" if view == "grid" else "списком")

    def current_view(self):
        return self.views.currentWidget()

    # --- Выделение ---
    def clear_selection(self):
        for view in (self.list, self.grid):
            view.clearSelection()

    def _on_escape(self):
        if self.search.hasFocus() and self.search.text():
            self.search.clear()
            return
        self.clear_selection()
        self.current_view().setFocus()

    def eventFilter(self, obj, event):
        # Клик левой кнопкой по пустому месту списка или плитки (без Ctrl и Shift) — выделение снимается
        if (event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton
                and not event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
            view = self.list if obj is self.list.viewport() else self.grid if obj is self.grid.viewport() else None
            if view is not None and view.itemAt(event.position().toPoint()) is None:
                view.clearSelection()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        # Клик по самой вкладке — мимо списка, кнопок и поиска
        if event.button() == Qt.LeftButton:
            self.clear_selection()
        super().mousePressEvent(event)

    def reveal(self, game):
        """Вернулись с карточки игры — игра выделена и видна в списке, откуда её открыли."""
        view = self.current_view()
        for row in range(view.count()):
            item = view.item(row)
            if item.data(Qt.UserRole) == game["name"]:
                view.clearSelection()
                item.setSelected(True)
                view.setCurrentItem(item)
                view.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                break
        view.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.mini.place)
        ratio = self.devicePixelRatioF()
        if ratio != self.thumbs.ratio:  # окно перенесли на экран с другим масштабом — обложки резче
            self.thumbs.ratio = ratio
            self.thumbs.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.mini.place()

    def game_of(self, item):
        return self.win.library.find(item.data(Qt.UserRole) or item.text()) if item else None

    def show_menu(self, view, pos):
        item = view.itemAt(pos)
        game = self.game_of(item)
        if not game:
            return
        if not item.isSelected():
            view.clearSelection()
            item.setSelected(True)

        menu = RoundMenu(parent=self)
        appid = game.get("appid")
        if appid:
            installed = core.is_installed(appid)
            launch = Action(FIF.PLAY if installed else FIF.DOWNLOAD, "Играть" if installed else "Установить", menu)
            launch.triggered.connect(lambda: self.win.launch_game(game))
            owned = self.win.owns(appid)
            store = Action(FIF.HEART if owned else FIF.SHOPPING_CART,
                           OWNED_STORE_TEXT if owned else "Открыть в магазине Steam", menu)
            store.setToolTip(OWNED_STORE_TIP if owned else "")
            store.triggered.connect(lambda: self.win.open_in_steam(f"steam://store/{appid}", game, "store"))
            menu.addActions([launch, store])
        else:
            link = Action(FIF.SEARCH, "Найти в Steam", menu)
            link.setToolTip("Добавлена вручную, без связи со Steam: найдите её в магазине, и появятся описание, "
                            "трейлеры и установка")
            link.triggered.connect(lambda: self.link_game(game))
            menu.addAction(link)

        show = Action(FIF.GAME, "Открыть страницу игры", menu)
        show.triggered.connect(lambda: self.open_page(game))
        menu.addAction(show)
        menu.addSeparator()
        own_cover = Action(FIF.PHOTO, "Своя обложка…", menu)
        own_cover.triggered.connect(lambda: self.choose_cover(game))
        menu.addAction(own_cover)
        if core.griddb_key():
            grid_cover = Action(FIF.DOWNLOAD, "Обложка из SteamGridDB", menu)
            grid_cover.setToolTip("Обложка из SteamGridDB — например, если Steam собрал её из горизонтальной шапки. "
                                  "Выберите ещё раз — будет следующий вариант")
            grid_cover.triggered.connect(lambda: self.cover_from_griddb(game))
            menu.addAction(grid_cover)
        menu.addSeparator()
        add_collection_menu(self.win, menu, [self.game_of(i) for i in view.selectedItems()])
        menu.addSeparator()
        count = len(view.selectedItems())
        delete = Action(FIF.DELETE, "Удалить из списка" if count == 1 else f"Удалить выбранные ({count})", menu)
        delete.triggered.connect(self.delete_selected)
        menu.addAction(delete)
        menu.exec(view.viewport().mapToGlobal(pos))

    def open_page(self, game):
        if not game:
            return
        self.win.game_origin = None  # «назад» со страницы — на «Игры Steam», как обычно
        self.win.switchTo(self.win.home_page)
        self.win.home_page.open_game(game)

    # --- Обложки ---
    def cover_changed(self, name):
        """Обложка игры появилась или сменилась — перерисовать её на плитке, на барабане, у портов и дисковых игр."""
        self.thumbs.forget(name)
        self.grid.viewport().update()
        self.backdrop.refresh(name)
        self.win.home_page.reel.refresh_cover(name)
        for page in (getattr(self.win, "native_page", None), getattr(self.win, "disc_page", None),
                     getattr(self.win, "collections_page", None)):
            if page is not None:  # «Своя обложка» и SteamGridDB для них — те же, что у игр Steam
                page.cover_changed(name)

    def choose_cover(self, game):
        path, _ = QFileDialog.getOpenFileName(self, f"Обложка для «{game['name']}»", "",
                                              "Изображения (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                core.save_cover_bytes(game["name"], f.read())
            core.mark_custom_cover(game["name"])  # выбранную вручную программа не заменит
        except OSError as e:
            notify("error", self.win, "Не удалось сохранить обложку", str(e))
            return
        core.log.info("Своя обложка для «%s»", game["name"])
        self.cover_changed(game["name"])
        notify("success", self.win, "Обложка сохранена", f"«{game['name']}»")

    def cover_from_griddb(self, game):
        """Каждое нажатие — следующая вертикальная обложка 600×900 из SteamGridDB, по кругу."""
        name = game["name"]
        worker = Worker(core.cover_from_griddb, dict(game), core.griddb_key(), parent=self)
        worker.finished.connect(lambda result: self._on_griddb_cover(name, result))
        worker.failed.connect(lambda message: notify("warning", self.win, "SteamGridDB не ответил",
                                                     message.split(": ", 1)[-1], 6000))
        worker.start()
        notify("info", self.win, "Ищу обложку в SteamGridDB", f"«{name}»", 2500)

    def _on_griddb_cover(self, name, result):
        if not result:
            notify("info", self.win, "В SteamGridDB обложки нет",
                   f"«{name}»: вертикальной обложки там пока не выложили. Можно поставить свою.", 6000)
            return
        self.cover_changed(name)
        more = (" Не та — выберите «Обложка из SteamGridDB» ещё раз, будет следующая." if result["count"] > 1
                else " Других вариантов там нет.")
        notify("success", self.win, "Обложка заменена",
               f"«{name}» — из SteamGridDB, вариант {result['index'] + 1} из {result['count']}.{more}", 5000)

    def refresh(self):
        query = self.search.text().casefold().strip()
        self.list.clear()
        self.grid.clear()
        tile = CoverTileDelegate.COVER
        for game in sort_games(self.win.library.games, self.sort_box.mode()):
            if query and query not in game["name"].casefold():
                continue
            for view in (self.list, self.grid):
                item = QListWidgetItem(game["name"] if view is self.list else "")
                item.setData(Qt.UserRole, game["name"])
                if view is self.grid:
                    item.setToolTip(game["name"])
                    item.setSizeHint(QSize(tile.width() + 2 * CoverTileDelegate.PAD,
                                           tile.height() + CoverTileDelegate.TEXT_H + 2 * CoverTileDelegate.PAD))
                view.addItem(item)
        text = f"Всего игр: {len(self.win.library.games)}"
        unlinked = sum(not game.get("appid") for game in self.win.library.games)
        if unlinked:
            text += f" · без связи со Steam: {unlinked} — правый клик по игре → «Найти в Steam»"
        self.count_label.setText(text)

    def add_games(self, term="", synced=False):
        dialog = AddGameDialog(self.win, term=term, synced=synced)
        core.log.info("Открыто окно «Добавить игру»")
        accepted = dialog.exec()
        choice, plain, bulk = dialog.choice, dialog.plain_name, dialog.bulk
        want_owned, term = dialog.want_owned, dialog.search.text().strip()
        dialog.deleteLater()
        if not accepted:
            return
        if want_owned:
            self._sync_owned(lambda: self.add_games(term, synced=True))
        elif bulk:
            self.add_list()
        elif choice:
            self._add_found(choice)
        elif plain:
            if self.win.library.add(plain) != "added":
                notify("info", self.win, "Игра уже в списке", f"«{plain}»")
                return
            if self.win.save_library():
                core.log.info("Добавлена игра без Steam: «%s»", plain)
                self.win.libraryChanged.emit()
                notify("success", self.win, "Игра добавлена", f"«{plain}» — без связи со Steam.")

    def _add_found(self, choice):
        """Игра из поиска Steam — с номером в Steam: у неё сразу будут описание, трейлеры и установка."""
        name, appid = choice["name"], choice["appid"]
        twin = next((g for g in self.win.library.games if g.get("appid") == appid), None)
        if twin:
            notify("info", self.win, "Игра уже в списке", f"«{twin['name']}»")
            return
        result = self.win.library.add(name, appid)
        if not result:
            notify("info", self.win, "Игра с таким названием уже в списке", f"«{name}»")
            return
        if not self.win.save_library():
            return
        core.log.info("%s игра из поиска Steam: «%s» (%s)", "Добавлена" if result == "added" else "Связана",
                      name, appid)
        self.win.covers.forget(name)
        self.win.libraryChanged.emit()
        notify("success", self.win, "Игра добавлена" if result == "added" else "Игра связана со Steam",
               f"«{name}»: описание, трейлеры и обложка — из Steam.")

    def add_list(self):
        """Несколько игр списком — только названия, без поиска в Steam."""
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

    def _sync_owned(self, then):
        """Вход в магазин Steam (если нужен) и список того, что есть на аккаунте, — потом then(), здесь же."""
        self.win.switchTo(self)
        self.win.music_page.sync_steam(after=then, back_text="К играм", return_page=self)

    def offer_owned_games(self):
        """После входа в магазин без ключа: игры с аккаунта, которых ещё нет в списке, — предлагаем добавить.
        Без ключа коллекций Steam не видно, поэтому выбор простой: все игры аккаунта или ни одной."""
        owned = core.owned_apps()
        if not owned:
            return  # вход не прошёл или его отменили — об этом уже сказано
        listed = {g.get("appid") for g in self.win.library.games}
        new = [appid for appid in owned if appid not in listed]
        if not new:
            notify("success", self.win, "Вход в магазин Steam выполнен",
                   "Все игры с вашего аккаунта уже в списке. В «Добавить» у найденных игр видно, какие уже ваши.", 6000)
            return
        worker = Worker(core.owned_games, new, parent=self)
        worker.finished.connect(self._on_owned_games)
        worker.failed.connect(lambda message: notify("warning", self.win, "Не удалось узнать игры аккаунта",
                                                     "Магазин Steam не ответил. Игры можно добавить через «Добавить»."))
        worker.start()

    def _on_owned_games(self, games):
        listed = {g.get("appid") for g in self.win.library.games}
        games = [g for g in games if g["appid"] not in listed]
        if not games:
            notify("success", self.win, "Вход в магазин Steam выполнен",
                   "Новых игр на аккаунте нет — только дополнения, саундтреки или то, что уже в списке.", 6000)
            return
        names = ", ".join(f"«{g['name']}»" for g in games[:5]) + (f" и ещё {len(games) - 5}" if len(games) > 5 else "")
        if not confirm(self.win, "Добавить игры с аккаунта?",
                       f"На вашем аккаунте Steam есть игры, которых нет в списке: {len(games)} — {names}.\n\n"
                       "Добавить их? У каждой будут описание, трейлеры, обложка и установка через Steam. "
                       "Коллекций Steam без ключа не видно — лишнее потом можно убрать из списка.", "Добавить"):
            return
        added = sum(self.win.library.add(g["name"], g["appid"]) == "added" for g in games)
        if not self.win.save_library():
            return
        core.log.info("Игры с аккаунта без ключа: добавлено %d из %d", added, len(games))
        self.win.libraryChanged.emit()
        notify("success", self.win, f"Добавлено игр: {added}", "С вашего аккаунта Steam.", 6000)

    def link_game(self, game, term="", synced=False):
        """«Найти в Steam» для игры, добавленной вручную: найденный номер в Steam привязывается к ней."""
        dialog = AddGameDialog(self.win, game, term=term, synced=synced)
        core.log.info("Открыто окно «Найти в Steam» для «%s»", game["name"])
        accepted = dialog.exec()
        choice, want_owned, term = dialog.choice, dialog.want_owned, dialog.search.text().strip()
        dialog.deleteLater()
        if accepted and want_owned and game in self.win.library.games:
            self._sync_owned(lambda: self.link_game(game, term, synced=True))
            return
        if not accepted or not choice or game not in self.win.library.games:
            return
        twin = next((g for g in self.win.library.games if g.get("appid") == choice["appid"] and g is not game), None)
        if twin:
            notify("warning", self.win, "Эта игра уже в списке",
                   f"С этой страницей Steam уже связана «{twin['name']}».", 6000)
            return
        game["appid"] = choice["appid"]
        if not self.win.save_library():
            game["appid"] = None
            return
        core.log.info("«%s» связана со Steam: «%s» (%s)", game["name"], choice["name"], choice["appid"])
        self.win.covers.forget(game["name"])  # с номером в Steam обложка может найтись
        self.win.libraryChanged.emit()
        game_page = self.win.home_page.game_page
        if game_page.game is game:
            game_page.show_game(game)  # карточка открыта — сразу с описанием и трейлерами
        notify("success", self.win, "Игра связана со Steam",
               f"«{game['name']}» → «{choice['name']}». Теперь у неё есть описание, трейлеры и установка через Steam.",
               7000)

    def delete_selected(self):
        names = [item.data(Qt.UserRole) or item.text() for item in self.current_view().selectedItems()]
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
            self.thumbs.clear()
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
            self.win.run_steam_setup()  # ключа нет — сначала подключаем Steam, импорт мастер запустит сам
            return
        self._quick = quick
        self.set_import_busy(True)
        self._progress_dialog = ImportProgressDialog(
            self.win, "Синхронизация со Steam" if quick else "Загружаю библиотеку Steam")
        self._progress_dialog.show()
        if not QWebEngineView:
            self._start_load(api_key, steam_id, None)
            return
        # Семейную группу Steam показывает только тому, кто вошёл в магазин: сначала берём токен входа
        self._progress_dialog.set_progress("Проверяю вход в магазин Steam…", 2)
        reader = SteamWebToken(self)
        reader.finished.connect(lambda token: self._start_load(api_key, steam_id, token))
        reader.failed.connect(lambda message: (core.log.warning("Токен магазина Steam не получен: %s", message),
                                               self._start_load(api_key, steam_id, None)))
        reader.start()

    def _start_load(self, api_key, steam_id, token):
        self._had_token = bool(token)
        core.log.info("Загрузка библиотеки Steam: %s", "вход в магазин есть — проверяю семейную группу" if token
                      else "входа в магазин нет — семейная группа не проверяется")
        relay = ProgressRelay(self)
        relay.progress.connect(self._on_progress)
        worker = Worker(core.load_steam_library, api_key, steam_id, relay.report, token, parent=self)
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

        library["store_login"] = self._had_token
        settings = core.load_settings()
        if self._quick:
            choice = {key: settings[key] for key in IMPORT_KEYS}
            # Без входа в магазин семейную группу не проверить — игры не с вашего аккаунта из списка не убираем
            self.apply_import(library, choice, keep_unverified=not library["family_checked"])
            return
        listed = {game["appid"] for game in self.win.library.games if game.get("appid")}
        dialog = ImportDialog(self.win, library, settings, listed)
        core.log.info("Открыто окно «Импорт из Steam»")
        accepted = dialog.exec()
        choice, want_login = dialog.selection(), dialog.want_login
        dialog.deleteLater()
        if want_login:
            core.save_settings(**choice)  # окно откроется заново с тем же выбором
            if library["store_login"]:
                core.log.info("Импорт: вход в магазин есть, а Steam не ответил про семейную группу — пробую ещё раз")
                self._load_library(False)
            else:
                core.log.info("Импорт: вход в магазин Steam, чтобы проверить семейную группу")
                self.win.music_page.sync_steam(after=self._after_import_login, back_text="К импорту",
                                               return_page=self, ask=False,
                                               done_text="Проверяю покупки и семейную группу Steam — это до "
                                                         "минуты. Окно импорта появится само.")
            return
        core.log.info("Окно «Импорт из Steam» закрыто: %s", "импорт" if accepted else "отмена")
        if not accepted:
            return
        core.save_settings(import_done=True, **choice)
        self.set_import_busy(False)
        self.apply_import(library, choice)

    def _after_import_login(self):
        """Вернулись из входа в магазин Steam (вошли или нет) — окно импорта открывается снова, само."""
        core.log.info("Импорт: после входа в магазин Steam открываю окно импорта заново")
        self.win.switchTo(self)
        self._load_library(False)

    def apply_import(self, library, choice, keep_unverified=False):
        """keep_unverified — не убирать по «Заменить мой список» игры не с вашего аккаунта: семейную группу
        не проверили, и неизвестно, доступны ли они ещё (так бывает при быстрой синхронизации без входа)."""
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

        removed = kept = 0
        if choice["import_replace"]:
            keep = {game["appid"] for game in selected}
            own = {game["appid"] for game in library["games"] if not game["family"]}
            for game in list(self.win.library.games):
                appid = game.get("appid")
                if not appid or appid in keep:  # без appid — добавлены вручную
                    continue
                if keep_unverified and appid not in own:
                    kept += 1
                    continue
                self.win.library.remove(game)
                removed += 1

        if not self.win.save_library():
            return
        core.log.info("%s: выбор %s, подходит %d, добавлено %d, убрано %d, не убрано без проверки семьи %d",
                      "Синхронизация" if self._quick else "Импорт", choice, len(selected), added, removed, kept)
        self.win.libraryChanged.emit()
        text = f"Подходит под выбор: {len(selected)}. Добавлено новых: {added}."
        if removed:
            text += f" Убрано из списка: {removed}."
        if kept:
            text += (f" Не убрано игр не с вашего аккаунта: {kept} — семейная группа не проверена "
                     "(нет входа в магазин Steam).")
        if missing:
            text += f" Не найдено коллекций из прошлого выбора: {len(missing)}."
        title = "Синхронизация завершена" if self._quick else "Импорт завершён"
        home = self.win.home_page
        if added >= LOADING_THRESHOLD and home.stack.currentWidget() is home.loading_view:
            # Идёт загрузка обложек: итог — на её экране, уведомление закрыло бы надписи
            home.set_loading_note(f"{title}. {text}")
            self.win.switchTo(home)
            return
        notify("success", self.win, title, text, duration=7000)

    def on_import_failed(self, message):
        self.set_import_busy(False)
        self._close_progress()
        notify("error", self.win, "Не удалось получить библиотеку Steam",
               message + "\nПодробности — на вкладке «Логи».", duration=8000)


# --- Страница «Музыка» ------------------------------------------------------
STEAM_LOGIN_URL = "https://store.steampowered.com/login/"
STEAM_SOUNDTRACKS_URL = "https://store.steampowered.com/soundtracks/"


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
        self.timer.timeout.connect(
            lambda: self._finish(error=f"магазин Steam не ответил за {self.TIMEOUT_MS // 1000} секунд"))

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


class SteamWebToken(SteamOwnedApps):
    """Временный токен входа в магазин Steam — с ним Steam отвечает, какие игры семейной группы вам доступны.

    Магазин выдаёт его тому, кто вошёл на сайт (вход хранится в куках встроенного браузера). Токен живёт
    только в памяти на время импорта: программа не пишет его ни в лог, ни в файлы. finished(токен) или
    finished(None) — вход в магазин не выполнен; failed(текст) — магазин не открылся.
    """

    URL = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"
    TIMEOUT_MS = 15000

    def _on_text(self, text):
        if self._done:
            return
        try:
            data = json.loads(text)
        except ValueError:
            self._finish(error="магазин Steam не выдал токен входа")
            return
        token = ((data.get("data") or {}).get("webapi_token") if isinstance(data, dict) else None)
        self._finish(token if isinstance(token, str) and token else None)  # без входа токена нет


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


def disk_size_text(size):
    """Размер на диске по-русски: «1,4 ГБ», «230 МБ»."""
    return (f"{size / 1024 ** 3:.1f} ГБ" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} МБ").replace(".", ",")


class RemoveAlbumDialog(MessageBoxBase):
    """«Удалить с ПК» для скачанного саундтрека. Steam запущен — удаляет сам Steam (он спросит и уберёт всё),
    закрыт — программа кладёт папку альбома в Корзину. choice: "steam" | "trash"."""

    def __init__(self, parent, album, owner_name=None):
        super().__init__(parent)
        self.choice = None
        steam_on = core.steam_running()
        can_steam = bool(album.get("appid")) and bool(core.find_steam_dir())
        self.viewLayout.addWidget(SubtitleLabel("Удалить саундтрек с ПК?", self))
        size = core.folder_size(album["folder"])
        name = BodyLabel(f"«{album['name']}» — треков: {len(album['tracks'])}, освободится {disk_size_text(size)}.", self)
        name.setWordWrap(True)
        self.viewLayout.addWidget(name)
        lines = []
        if owner_name is not None:
            lines.append((f"Этот альбом скачал аккаунт {owner_name}. Вернуть его сможет только этот аккаунт "
                          "(бесплатные саундтреки — любой).", True))
        elif album.get("appid"):
            lines.append(("Он останется в вашей библиотеке Steam: скачать заново можно в любой момент — "
                          "он появится в «Куплены, но не скачаны».", False))
        if steam_on and can_steam:
            lines.append(("Steam запущен — удалит сам Steam: он спросит ещё раз и уберёт и файлы, и свою запись "
                          "об альбоме.", False))
        else:
            lines.append(("Программа переложит папку альбома в Корзину Windows — передумаете, восстановите оттуда. "
                          "Steam для этого не нужен.", False))
        for text, warning in lines:
            label = CaptionLabel(text, self)
            label.setWordWrap(True)
            if warning:
                label.setTextColor(*WARNING_TEXT)
            self.viewLayout.addWidget(label)
        self.widget.setMinimumWidth(480)

        self.yesButton.clicked.disconnect()
        if steam_on and can_steam:
            self.yesButton.setText("Удалить через Steam")
            self.yesButton.clicked.connect(lambda: self._choose("steam"))
        else:
            self.yesButton.setText("Удалить в Корзину")
            self.yesButton.clicked.connect(lambda: self._choose("trash"))
            if can_steam:  # Steam есть, но закрыт — можно и через него: он запустится
                via_steam = PushButton("Через Steam", self.buttonGroup)
                via_steam.setToolTip("Steam запустится и сам удалит альбом")
                via_steam.clicked.connect(lambda: self._choose("steam"))
                self.buttonLayout.insertWidget(1, via_steam, 1, Qt.AlignVCenter)
        self.cancelButton.setText("Отмена")

    def _choose(self, choice):
        self.choice = choice
        self.accept()


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
        # Синхронизацию может попросить и другая вкладка («Добавить игру», мастер): куда вернуться и что сделать потом
        self._after_sync = None
        self._return_page = None
        self._back_text = "К саундтрекам"
        self._ask_login = True
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
        self.source_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.source_list.customContextMenuRequested.connect(self._show_source_menu)
        self.sync_button = PushButton("Синхронизировать со Steam", self, FIF.SYNC)
        self.sync_button.setToolTip("Найти скачанные саундтреки и узнать, какие куплены, но ещё не скачаны")
        self.sync_button.clicked.connect(lambda: self.sync_steam())
        self.buy_button = PushButton("Купить саундтреки", self, FIF.SHOPPING_CART)
        self.buy_button.setToolTip("Раздел саундтреков в магазине Steam — на вкладке «Браузер»")
        self.buy_button.clicked.connect(lambda: self.win.open_web(STEAM_SOUNDTRACKS_URL))
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
        self.remove_button = TransparentPushButton("Удалить с ПК", self, FIF.DELETE)
        self.remove_button.setToolTip("Удалить скачанный альбом с этого компьютера — через Steam или в Корзину")
        self.remove_button.clicked.connect(lambda: self.remove_album(self.shown))
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
        left.addWidget(self.buy_button)

        # В узком окне кнопки альбома переносятся на вторую строку, а не сжимаются
        actions_box = QWidget(self)
        actions = CenteredFlowLayout(actions_box, spacing=8, center=False)
        for button in (self.listen_button, self.game_button, self.remove_button):
            actions.addWidget(button)
        self.album_actions = actions
        info = QVBoxLayout()
        info.setSpacing(4)
        info.addWidget(self.album_title)
        info.addWidget(self.album_caption)
        info.addStretch(1)
        info.addWidget(actions_box)
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
        for title, group in self._album_groups(albums):
            self._add_header(title)
            for source in group:
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
        if source["kind"] == "store":
            item.setToolTip("Двойной клик — скачать в Steam")
        elif self.foreign(source["album"]):
            item.setToolTip("Этот альбом скачал на этот ПК другой аккаунт Steam, не тот, что подключён к программе. "
                            "Файлы лежат на диске, поэтому слушать его можно.\n" + source["path"])
        else:
            item.setToolTip(source["path"])
        self.source_list.addItem(item)
        self.row_sources.append(source)

    def foreign(self, album):
        """Альбом скачан на этот ПК другим аккаунтом Steam (по LastOwner в его appmanifest)."""
        steam_id = self.win.my_steam_id()
        return bool(album.get("owner") and steam_id and album["owner"] != steam_id)

    def _album_groups(self, albums):
        """Скачанные — разделами по аккаунтам Steam, которые их скачали: сначала ваш, потом остальные.
        Имена аккаунтов — из клиента Steam на этом ПК; SteamID неизвестен или чужих нет — один раздел."""
        steam_id = self.win.my_steam_id()
        if not albums or not any(self.foreign(s["album"]) for s in albums):
            return [("Скачанные", albums)]
        names = core.steam_account_names()
        groups = []
        mine = [s for s in albums if not self.foreign(s["album"])]
        if mine:
            groups.append((f"Ваш аккаунт — {names.get(steam_id) or core.short_steam_id(steam_id)}", mine))
        owners = sorted({s["album"]["owner"] for s in albums if self.foreign(s["album"])},
                        key=lambda owner: (names.get(owner) or owner).casefold())
        for owner in owners:
            groups.append((f"Скачаны аккаунтом {names.get(owner) or core.short_steam_id(owner)}",
                           [s for s in albums if s["album"].get("owner") == owner]))
        return groups

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
            pixmap = QPixmap.fromImage(QImage(path)) if path else QPixmap()  # файл мог смениться под тем же именем
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
            self.album_cover.hide()  # пустой значок рядом с надписью ничего не говорит
            self.album_title.setText("Саундтреков пока нет")
            self.album_caption.setText("Скачайте саундтрек в Steam или нажмите «Синхронизировать со Steam» — "
                                       "появятся и купленные, но не скачанные. Встроенная тема и своя музыка — "
                                       "в «Настройках».")
            self.listen_button.hide()
            self.game_button.hide()
            self.remove_button.hide()
            self._update_backdrop()
            return
        self.listen_button.show()
        self.album_cover.show()
        store = source["kind"] == "store"
        self.remove_button.setVisible(not store)
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
        self.album_actions.invalidate()  # кнопки появились или спрятались — перенос по строкам заново
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

    # --- Удаление скачанного альбома ---
    REMOVE_RECHECKS = (5, 15, 30, 60)  # секунд: Steam удаляет альбом после своего вопроса — смотрим, когда исчезнет

    def _show_source_menu(self, pos):
        item = self.source_list.itemAt(pos)
        source = self._source_of_row(self.source_list.row(item)) if item else None
        if not source:
            return
        self.select_source(source)
        menu = RoundMenu(parent=self)
        store = source["kind"] == "store"
        listen = Action(FIF.DOWNLOAD if store else FIF.PLAY, "Скачать в Steam" if store else "Слушать", menu)
        listen.triggered.connect(lambda: self.play_source(source))
        menu.addAction(listen)
        if self._library_game(source):
            game = Action(FIF.GAME, "Страница игры", menu)
            game.triggered.connect(self.open_game)
            menu.addAction(game)
        if not store:
            menu.addSeparator()
            remove = Action(FIF.DELETE, "Удалить с ПК", menu)
            remove.triggered.connect(lambda: self.remove_album(source))
            menu.addAction(remove)
        menu.exec(self.source_list.viewport().mapToGlobal(pos))

    def remove_album(self, source):
        """«Удалить с ПК»: через Steam (он запущен) или в Корзину (Steam закрыт). Купленный альбом остаётся
        на аккаунте — после удаления он появится в «Куплены, но не скачаны»."""
        if not source or source["kind"] != "album":
            return
        album = source["album"]
        owner = None
        if self.foreign(album):
            owner = core.steam_account_names().get(album["owner"]) or core.short_steam_id(album["owner"])
        dialog = RemoveAlbumDialog(self.win, album, owner)
        core.log.info("Открыто окно «Удалить саундтрек» для «%s»", album["name"])
        accepted = dialog.exec()
        choice = dialog.choice
        dialog.deleteLater()
        if not accepted or not choice:
            return
        if choice == "steam":
            self._remove_via_steam(album)
            return
        if core.steam_running() and album.get("appid"):  # Steam успели запустить, пока было открыто окно
            notify("warning", self.win, "Steam запущен",
                   "Пока Steam работает, удалять альбом в обход него нельзя — он может скачать его заново. "
                   "Нажмите «Удалить с ПК» ещё раз: удалит сам Steam.", 7000)
            return
        self._release_album(album)
        worker = Worker(core.remove_soundtrack, dict(album), parent=self)
        worker.finished.connect(lambda _: self._on_album_removed(album))
        worker.failed.connect(lambda message: self._on_remove_failed(album, message))
        worker.start()

    def _release_album(self, album):
        """Играет трек из этого альбома — переключаемся на встроенную тему, иначе Windows не отдаст файл."""
        if self.music.release(album["folder"]):
            if core.same_path(core.load_settings()["music_path"], album["folder"]):
                core.save_settings(music_path="")
            notify("info", self.win, "Музыка переключена на встроенную тему",
                   f"Играл трек из «{album['name']}», а его удаляют.")

    def _remove_via_steam(self, album):
        url = f"steam://uninstall/{album['appid']}"
        core.log.info("Удалить саундтрек «%s» через Steam: %s", album["name"], url)
        self._release_album(album)
        Worker(core.open_url, url, parent=self).start()  # Steam может отвечать несколько секунд
        for seconds in self.REMOVE_RECHECKS:
            QTimer.singleShot(seconds * 1000, lambda: self.reload_soundtracks(quiet=True))
        notify("info", self.win, "Steam спросит, удалить ли альбом",
               f"«{album['name']}». Когда Steam его удалит, альбом сам пропадёт из списка.", 7000)

    def _on_album_removed(self, album):
        self.reload_soundtracks()
        text = "Вернуть его можно из Корзины Windows."
        if album.get("appid") in {item["appid"] for item in self.owned}:
            text += " Он остался на аккаунте — теперь он в «Куплены, но не скачаны», скачать заново можно оттуда."
        notify("success", self.win, "Саундтрек удалён в Корзину", f"«{album['name']}». {text}", 7000)

    def _on_remove_failed(self, album, message):
        core.log.warning("Саундтрек «%s» не удалился: %s", album["name"], message)
        notify("error", self.win, "Не удалось удалить саундтрек",
               f"«{album['name']}»: {message}\nПодробности — на вкладке «Логи».", 8000)
        self.reload_soundtracks(quiet=True)

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
        if not self.win.confirm_steam_account("скачает саундтрек"):
            return
        core.log.info("Скачать саундтрек «%s»: %s", item["name"], url)
        Worker(core.open_url, url, parent=self).start()  # Steam может отвечать несколько секунд
        self._watch[item["appid"]] = time.monotonic()
        self.watch_timer.start()
        notify("info", self.win, "Steam откроет окно скачивания",
               f"«{item['name']}». Когда альбом скачается, он сам появится на вкладке «Саундтреки».", 6000)

    def expect_install(self, appid):
        """Из встроенного браузера в Steam ушла установка. Саундтрек ли это, узнаём у магазина в фоне:
        если да — возвращаемся на эту вкладку и ждём альбом на диске, как при «Скачать в Steam»."""
        worker = Worker(core.store_items, [appid], parent=self)
        worker.finished.connect(lambda items: self._on_install_kind(appid, items))
        worker.start()

    def _on_install_kind(self, appid, items):
        item = items.get(appid) or {}
        if item.get("type") != core.STORE_TYPE_MUSIC:
            return  # игра или дополнение — их ждать здесь незачем
        name = item.get("name") or "Саундтрек"
        core.log.info("Из встроенного браузера скачивается саундтрек «%s» (%s) — возвращаюсь к «Саундтрекам»",
                      name, appid)
        self._watch[appid] = time.monotonic()
        self.watch_timer.start()
        self.win.switchTo(self)
        notify("info", self.win, "Саундтрек скачивается в Steam",
               f"«{name}». Когда Steam его скачает, он сам появится в этом списке.", 7000)

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
        if not albums:
            self._finish_scan()  # до перерисовки: иначе в пустом списке так и висело бы «Ищу…»
            self.build_sources()
            self._refresh_game_page()
            return
        self.build_sources()
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
    def sync_steam(self, after=None, back_text=None, return_page=None, ask=True, done_text=None):
        """Скачанные альбомы — с диска, купленные — из магазина Steam (нужен вход во встроенном браузере).

        after() — что сделать, когда синхронизация закончится (как угодно: успех, отказ, ошибка); return_page —
        куда вернуться после входа в магазин; ask=False — не спрашивать «Войти?», об этом уже спросили;
        done_text — что сказать, когда вход в магазин прошёл (например, «продолжаю импорт»).
        """
        if after or return_page:
            self._after_sync, self._return_page = after, return_page
            self._back_text = back_text or "К саундтрекам"
            self._ask_login = ask
            self._login_text = done_text
        if self._syncing:
            return
        self.reload_soundtracks()
        if not QWebEngineView:
            notify("warning", self.win, "Покупки Steam не узнать",
                   "Для этого нужен встроенный браузер (PySide6 с QtWebEngine). Скачанные саундтреки видны и так.")
            self._finish_sync()
            return
        self._set_syncing(True, "Узнаю, что куплено…")
        reader = SteamOwnedApps(self)
        reader.finished.connect(self._on_owned_apps)
        reader.failed.connect(self._on_sync_failed)
        reader.start()

    def _finish_sync(self):
        """Синхронизация закончилась — возвращаем тому, кто её просил."""
        hide_busy(self.win)
        callback = self._after_sync
        self._after_sync, self._return_page, self._back_text = None, None, "К саундтрекам"
        self._ask_login = True
        self._login_text = None
        self._login_done = self._login_retried = False
        if callback:
            core.log.info("Синхронизация с магазином Steam закончена — возвращаюсь к тому, что её просило")
            QTimer.singleShot(0, callback)

    def _set_syncing(self, syncing, text=""):
        """Пока идёт синхронизация, её ход пишется на самой кнопке."""
        self._syncing = syncing
        self.sync_button.setEnabled(not syncing)
        self.sync_button.setText(text if syncing else "Синхронизировать со Steam")

    def _on_owned_apps(self, owned):
        if owned is None and getattr(self, "_login_done", False) and not getattr(self, "_login_retried", False):
            # Вход только что прошёл, а магазин ещё отвечает «не вошли» (так бывает первые секунды после входа
            # по QR-коду) — спрашиваем ещё раз, а не отправляем входить заново
            self._login_retried = True
            self._set_syncing(False)
            core.log.info("Магазин Steam: вход только что прошёл, а покупки ещё не отдаёт — спрошу через 3 с")
            QTimer.singleShot(3000, self.sync_steam)
            return
        if owned is None:
            hide_busy(self.win)  # дальше вопрос или браузер — ждать больше нечего
            self._set_syncing(False)
            core.log.info("Магазин Steam: вход не выполнен — предлагаю войти во встроенном браузере")
            if not getattr(self, "_ask_login", True):
                self._ask_login = True  # спросили заранее — второй раз только если вход не удастся
                self._await_store_login()
            elif confirm(self.win, "Войдите в магазин Steam",
                         "Что есть на вашем аккаунте — игры и саундтреки, — Steam показывает только тому, кто вошёл "
                         "в магазин. Вход в обычном браузере программе ничего не передаёт, поэтому войти нужно здесь, "
                         "во встроенном браузере.\n\n"
                         "Удобнее и безопаснее — по QR-коду: отсканируйте его в мобильном приложении Steam, и пароль "
                         "в программу не вводится вовсе. Вход запомнится только на этом ПК, выйти можно в «Настройках» "
                         "→ «Выйти из магазина Steam». Как только вход пройдёт, программа сама вернётся обратно.",
                         "Войти"):
                self._await_store_login()
            else:
                self._finish_sync()
            return
        self._set_syncing(True, "Ищу саундтреки среди покупок…")
        worker = Worker(core.owned_soundtracks, owned, parent=self)
        worker.finished.connect(self._on_owned_soundtracks)
        worker.failed.connect(self._on_sync_failed)
        worker.start()

    # --- Вход в магазин во встроенном браузере ---
    STORE_LOGIN_COOKIE = "steamLoginSecure"  # кука входа: Steam ставит её, когда вход прошёл

    def _await_store_login(self):
        hide_busy(self.win)  # снова нужен вход — браузер не должен оказаться под стеклом
        self._awaiting_login = True
        self._store_account = ""
        self._cookie_store = web_profile(self).cookieStore()
        self._cookie_store.cookieAdded.connect(self._on_store_cookie)
        self.win.switchTo(self.win.home_page)
        self.win.home_page.open_browser(STEAM_LOGIN_URL, self._back_text, self._back_from_login)

    def _stop_cookie_watch(self):
        store, self._cookie_store = getattr(self, "_cookie_store", None), None
        if store:
            try:
                store.cookieAdded.disconnect(self._on_store_cookie)
            except (RuntimeError, TypeError):
                pass

    def _on_store_cookie(self, cookie):
        """Появилась кука входа в магазин — вход прошёл, возвращаемся к саундтрекам сами.

        Из куки берём только номер аккаунта в её начале (чтобы сверить с подключённым). Саму куку
        программа не хранит, не пишет в лог и никуда не отправляет — она остаётся в браузере.
        """
        if not self._awaiting_login or cookie.name().data().decode("ascii", "ignore") != self.STORE_LOGIN_COOKIE:
            return
        if not cookie.domain().lstrip(".").endswith("steampowered.com"):
            return
        match = re.match(r"(\d{17})", cookie.value().data().decode("ascii", "ignore"))
        self._store_account = match.group(1) if match else ""
        self._login_done = True
        self._stop_cookie_watch()
        core.log.info("Магазин Steam: вход выполнен (SteamID %s) — возвращаюсь к саундтрекам",
                      core.short_steam_id(self._store_account))
        QTimer.singleShot(1200, self._finish_store_login)  # даём магазину дорисовать страницу после входа

    def _finish_store_login(self):
        home = self.win.home_page
        if home.browser and home.stack.currentWidget() is home.browser and home._browser_return == self._back_from_login:
            home._leave_browser()  # само вызовет _back_from_login
        elif self._awaiting_login:
            self._back_from_login()
        steam_id = self.win.my_steam_id()
        if self._store_account and steam_id and self._store_account != steam_id:
            notify("warning", self.win, "В магазин вошёл другой аккаунт",
                   f"В магазине Steam вы вошли аккаунтом {core.short_steam_id(self._store_account)}, а к программе "
                   f"подключён {core.short_steam_id(steam_id)}. Покупки будут показаны для аккаунта из магазина. "
                   f"Сменить — «Настройки» → «Выйти из магазина Steam».", 10000)
        elif self._after_sync:
            # Дальше Steam разбирает покупки (это до минуты), а на экране ничего бы не происходило —
            # показываем кружок ожидания под матовым стеклом, пока не вернём управление тому, кто просил
            show_busy(self.win, "Вход в магазин Steam выполнен",
                      getattr(self, "_login_text", None) or "Узнаю у Steam, что есть на вашем аккаунте. "
                                                            "Это может занять до минуты.")

    def _on_owned_soundtracks(self, items):
        self.owned = items
        self._synced = True
        self._set_syncing(False)
        self.build_sources()
        self._refresh_game_page()
        downloaded = {a["appid"] for a in self.albums if a["appid"]}
        missing = sum(item["appid"] not in downloaded for item in items)
        if not self._after_sync:  # синхронизацию попросила другая вкладка — итог покажет она сама
            notify("success", self.win, "Синхронизировано со Steam",
                   f"На аккаунте: {len(core.owned_apps())}. Купленных саундтреков: {len(items)}, "
                   f"из них не скачано: {missing}.")
        self._finish_sync()

    def _on_sync_failed(self, message):
        core.log.warning("Синхронизация саундтреков со Steam не удалась: %s", message)
        self._set_syncing(False)
        notify("warning", self.win, "Не удалось узнать покупки в Steam", message)
        self._finish_sync()

    def _back_from_login(self):
        self._stop_cookie_watch()  # вернулись кнопкой, не войдя, — больше не ждём
        self.win.switchTo(self._return_page or self)
        if self._awaiting_login:
            self._awaiting_login = False
            QTimer.singleShot(0, self.sync_steam)
        else:
            if not self._syncing:
                self._finish_sync()

    def forget_store_login(self):
        """«Выйти из магазина Steam»: стираем куки и кэш встроенного браузера и список покупок с прошлой синхронизации."""
        profile = web_profile(self)
        profile.cookieStore().deleteAllCookies()
        profile.clearHttpCache()
        core.forget_owned_soundtracks()
        self.owned, self._synced = [], False
        self.build_sources()
        self._refresh_game_page()
        core.log.info("Встроенный браузер: вход в магазин Steam и куки сайтов удалены")

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
class SettingsSection(QWidget):
    """Раздел «Настроек» карточкой: значок и заголовок сверху, под ними — сами настройки. Так видно, где
    кончается один раздел и начинается другой."""

    PAD = 20
    ICON = 20
    GAP = 12  # между значком и заголовком
    RADIUS = 8

    def __init__(self, icon, title, parent):
        super().__init__(parent)
        self.icon = icon
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(self.PAD, self.PAD - 4, self.PAD, self.PAD)
        self.body.setSpacing(10)
        self.title_label = SubtitleLabel(title, self)
        head = QHBoxLayout()
        head.setSpacing(0)
        # Место под значок вместе с промежутком: у пустого места раскладка свой промежуток не ставит
        head.addSpacing(self.ICON + self.GAP)
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.body.addLayout(head)
        self.body.addSpacing(2)

    def add(self, item, stretch=0, alignment=None):
        if isinstance(item, QLayout):
            self.body.addLayout(item, stretch)
        elif alignment is not None:
            self.body.addWidget(item, stretch, alignment)
        else:
            self.body.addWidget(item, stretch)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        dark = isDarkTheme()
        painter.setPen(QPen(QColor(255, 255, 255, 24) if dark else QColor(0, 0, 0, 20), 1))
        painter.setBrush(QColor(255, 255, 255, 13) if dark else QColor(255, 255, 255, 170))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        title = self.title_label.geometry()
        rect = QRectF(self.PAD, title.center().y() - self.ICON / 2 + 1, self.ICON, self.ICON)
        self.icon.render(painter, rect)


class SettingsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("settingsPage")
        self.win = window
        settings = self.win.settings

        self.griddb_edit = PasswordLineEdit(self)
        self.griddb_edit.setText(settings["griddb_api_key"])
        self.steam_key_edit = PasswordLineEdit(self)
        self.steam_key_edit.setText(settings["steam_api_key"])
        self.steam_id_edit = LineEdit(self)
        self.steam_id_edit.setText(settings["steam_id"])
        self.save_button = PrimaryPushButton("Сохранить ключи", self, FIF.SAVE)
        self.save_button.clicked.connect(self.save_keys)
        login_button = PushButton("Войти через Steam", self, FIF.PEOPLE)
        login_button.setToolTip("Вход на сайте Steam в вашем браузере и ключ Steam Web API — по шагам")
        login_button.clicked.connect(self.win.run_steam_setup)
        forget_button = PushButton("Удалить ключ и выйти", self, FIF.DELETE)
        forget_button.setToolTip("Стереть ключ Steam Web API и SteamID с этого компьютера")
        forget_button.clicked.connect(self.forget_steam)
        grid_button = PushButton("Ключ SteamGridDB", self, FIF.PHOTO)
        grid_button.setToolTip("Что даёт ключ SteamGridDB и как его получить — по шагам, с проверкой ключа")
        grid_button.clicked.connect(self.win.run_griddb_setup)
        # Кнопки — с переносом строк: в окне на половину экрана они не сжимаются и не обрезают надписи
        key_buttons = QWidget(self)
        key_flow = CenteredFlowLayout(key_buttons, spacing=8, center=False)
        for button in (self.save_button, login_button, grid_button, forget_button):
            key_flow.addWidget(button)

        keys = QGridLayout()
        keys.setHorizontalSpacing(16)
        keys.setVerticalSpacing(4)
        rows = [
            ("SteamGridDB API key", self.griddb_edit,
             "Обложки, фоны и живые фоны — необязательно; как получить — кнопка «Ключ SteamGridDB». "
             "Хранится зашифрованным, только на этом ПК"),
            ("Steam Web API key", self.steam_key_edit,
             "steamcommunity.com/dev/apikey (импорт библиотеки). Хранится зашифрованным, только на этом ПК"),
            ("SteamID64", self.steam_id_edit, "17 цифр, например 7656119…; проще — «Войти через Steam»"),
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
        music_buttons = QWidget(self)
        music_flow = CenteredFlowLayout(music_buttons, spacing=8, center=False)
        for button in (builtin_button, self.custom_button, choose_file, choose_folder):
            music_flow.addWidget(button)

        self.sounds_switch = SwitchButton(self)
        self.sounds_switch.setOnText("Вкл")
        self.sounds_switch.setOffText("Выкл")
        self.sounds_switch.setChecked(settings["sounds_enabled"])
        self.sounds_switch.checkedChanged.connect(self.set_sounds_enabled)
        # Фоновая музыка целиком: включена — при запуске играет то, что играло в прошлый раз
        self.music_switch = SwitchButton(self)
        self.music_switch.setOnText("Вкл")
        self.music_switch.setOffText("Выкл")
        self.music_switch.setChecked(bool(settings["music_enabled"]))
        self.music_switch.checkedChanged.connect(self.set_music_enabled)
        self.win.music.stateChanged.connect(self._show_music_enabled)  # и кнопка динамика в мини-плеере

        sound = QGridLayout()
        sound.setHorizontalSpacing(16)
        sound.setVerticalSpacing(8)
        sound.addWidget(BodyLabel("Фоновая музыка", self), 0, 0)
        sound.addWidget(self.music_switch, 0, 1)
        music_hint = CaptionLabel("Включена — при запуске программы играет то же, что и в прошлый раз: встроенная "
                                  "тема, саундтрек Steam или своя музыка. Выключить и включить можно и кнопкой "
                                  "с динамиком в мини-плеере.", self)
        music_hint.setWordWrap(True)
        sound.addWidget(music_hint, 1, 1, 1, 2)
        sound.addWidget(BodyLabel("Музыка", self), 2, 0)
        sound.addWidget(self.music_edit, 2, 1)
        sound.addWidget(music_buttons, 3, 1, 1, 2)
        sound.addWidget(CaptionLabel("Саундтреки Steam, громкость и плеер — на вкладке «Саундтреки». "
                                     "Пауза, треки и «вперемешку» — кнопками справа вверху на рулетке.", self),
                        4, 1)
        sound.addWidget(BodyLabel("Звуки барабана", self), 5, 0)
        sound.addWidget(self.sounds_switch, 5, 1)
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
        # Подложка из выделенных игр — кому-то она мешает, поэтому выключается
        self.selection_switch = SwitchButton(self)
        self.selection_switch.setOnText("Вкл")
        self.selection_switch.setOffText("Выкл")
        self.selection_switch.setChecked(bool(settings.get("selection_backdrop", True)))
        self.selection_switch.checkedChanged.connect(self.set_selection_backdrop)
        selection_row = QHBoxLayout()
        selection_row.addWidget(BodyLabel("Подложка из выделенных игр", self))
        selection_row.addSpacing(16)
        selection_row.addWidget(self.selection_switch)
        selection_row.addStretch(1)
        selection_hint = CaptionLabel("Выделили игру на вкладке с играми — за списком её обложка под матовым "
                                      "стеклом, несколько — коллаж из их обложек", self)
        selection_hint.setWordWrap(True)

        # Перевод описаний и отзывов: Google, а когда он не отвечает — модулем на этом ПК
        self.translate_box = ComboBox(self)
        self.translate_box.addItems(TRANSLATE_MODE_TITLES)
        self.translate_box.setCurrentIndex(core.TRANSLATE_MODES.index(core.translate_mode()))
        self.translate_box.currentIndexChanged.connect(self.change_translate_mode)
        translate_row = QHBoxLayout()
        translate_row.addWidget(BodyLabel("Переводить", self))
        translate_row.addSpacing(16)
        translate_row.addWidget(self.translate_box)
        translate_row.addStretch(1)
        self.model_button = PushButton("Скачать модуль перевода", self, FIF.DOWNLOAD)
        self.model_button.clicked.connect(self.toggle_local_model)
        self.model_bar = ProgressBar(self)
        self.model_bar.setFixedWidth(320)
        self.model_bar.hide()
        model_row = QHBoxLayout()
        model_row.setSpacing(12)
        model_row.addWidget(self.model_button)
        model_row.addWidget(self.model_bar)
        model_row.addStretch(1)
        self.model_status = CaptionLabel("", self)
        self.model_status.setWordWrap(True)
        self._model_busy = False
        self.update_translator()

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

        # Встроенный браузер помнит вход в магазин Steam — даём его стереть
        store_logout = PushButton("Выйти из магазина Steam", self, FIF.CLOSE)
        store_logout.clicked.connect(self.forget_store_login)
        store_logout.setEnabled(bool(QWebEngineView))
        store_hint = CaptionLabel("Встроенный браузер помнит вход в магазин Steam: по нему программа узнаёт, какие "
                                  "саундтреки вы купили. Кнопка стирает этот вход и куки всех сайтов браузера.", self)
        store_hint.setWordWrap(True)

        # Обновление программы: о новой версии сообщает GitHub, ставится она только по кнопке «Обновить»
        self.update_status = BodyLabel("", self)
        self.update_status.setWordWrap(True)
        self.update_notes = CaptionLabel("", self)
        self.update_notes.setWordWrap(True)
        self.update_notes.setTextFormat(Qt.MarkdownText)  # заметка к релизу на GitHub написана в Markdown
        self.update_notes.setTextInteractionFlags(Qt.TextSelectableByMouse)  # ссылки из заметки не открываются
        self.update_button = PrimaryPushButton("Обновить", self, FIF.DOWNLOAD)
        self.update_button.clicked.connect(self.install_update)
        self.update_page_button = PushButton("Страница релиза", self, FIF.GITHUB)
        self.update_page_button.clicked.connect(self.open_release_page)
        self.update_skip_button = PushButton("Пропустить эту версию", self, FIF.CLOSE)
        self.update_skip_button.setToolTip("Точка на «Настройках» пропадёт до следующей версии. Обновиться всё "
                                           "равно можно здесь")
        self.update_skip_button.clicked.connect(self.win.updater.skip)
        self.update_check_button = PushButton("Проверить сейчас", self, FIF.SYNC)
        self.update_check_button.clicked.connect(lambda: self.win.updater.check(force=True))
        update_buttons = QWidget(self)
        update_flow = CenteredFlowLayout(update_buttons, spacing=8, center=False)
        for button in (self.update_button, self.update_page_button, self.update_skip_button, self.update_check_button):
            update_flow.addWidget(button)
        self.update_flow = update_flow
        self.update_bar = ProgressBar(self)
        self.update_bar.setFixedWidth(320)
        self.update_bar.hide()
        self.update_progress = CaptionLabel("", self)
        self.update_progress.hide()
        self.update_switch = SwitchButton(self)
        self.update_switch.setOnText("Вкл")
        self.update_switch.setOffText("Выкл")
        self.update_switch.setChecked(bool(settings.get("update_auto", True)))
        self.update_switch.checkedChanged.connect(self.win.updater.set_auto)
        update_row = QHBoxLayout()
        update_row.addWidget(BodyLabel("Проверять обновление автоматически", self))
        update_row.addSpacing(16)
        update_row.addWidget(self.update_switch)
        update_row.addStretch(1)
        update_hint = CaptionLabel(
            "При каждом запуске программа спрашивает GitHub о последнем релизе проекта — без ключа и без входа. Новая "
            "версия ставится только по кнопке «Обновить»: программа скачивает BacklogRoulette.exe со страницы "
            "релиза, сверяет его контрольную сумму SHA-256 с той, что посчитал GitHub, и только тогда заменяет "
            "себя и перезапускается. Игры, настройки и обложки остаются на месте.", self)
        update_hint.setWordWrap(True)
        self.win.updater.changed.connect(self.update_updates)
        self.update_updates()

        # Снимки экрана в портах и дисковых играх: клавиша или сочетание — своё
        settings_hotkey = core.screenshot_hotkey(settings)
        self.shots_switch = SwitchButton(self)
        self.shots_switch.setOnText("Вкл")
        self.shots_switch.setOffText("Выкл")
        self.shots_switch.setChecked(bool(settings.get("screenshots_enabled", True)))
        self.shots_switch.checkedChanged.connect(self.set_screenshots_enabled)
        shots_row = QHBoxLayout()
        shots_row.addWidget(BodyLabel("Снимки по клавише", self))
        shots_row.addSpacing(16)
        shots_row.addWidget(self.shots_switch)
        shots_row.addStretch(1)
        self.hotkey_button = HotkeyButton(settings_hotkey["text"], self)
        self.hotkey_button.picked.connect(self.set_screenshot_hotkey)
        self.hotkey_button.rejected.connect(lambda text: self._shots_status(text, warning=True))
        hotkey_default = TransparentPushButton("По умолчанию", self, FIF.CANCEL)
        hotkey_default.setToolTip(f"Вернуть {core.DEFAULT_SETTINGS['screenshot_hotkey']['text']}")
        hotkey_default.clicked.connect(lambda: self.set_screenshot_hotkey(dict(core.DEFAULT_SETTINGS["screenshot_hotkey"])))
        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(BodyLabel("Клавиша снимка", self))
        hotkey_row.addSpacing(16)
        hotkey_row.addWidget(self.hotkey_button)
        hotkey_row.addWidget(hotkey_default)
        hotkey_row.addStretch(1)
        self.shot_sound_switch = SwitchButton(self)
        self.shot_sound_switch.setOnText("Вкл")
        self.shot_sound_switch.setOffText("Выкл")
        self.shot_sound_switch.setChecked(bool(settings.get("screenshot_sound", True)))
        self.shot_sound_switch.checkedChanged.connect(lambda on: core.save_settings(screenshot_sound=on))
        listen = TransparentPushButton("Послушать", self, FIF.VOLUME)
        listen.clicked.connect(self._play_shutter)
        shot_sound_row = QHBoxLayout()
        shot_sound_row.addWidget(BodyLabel("Щелчок затвора", self))
        shot_sound_row.addSpacing(16)
        shot_sound_row.addWidget(self.shot_sound_switch)
        shot_sound_row.addWidget(listen)
        shot_sound_row.addStretch(1)
        shots_hint = CaptionLabel(
            "Работает, пока идёт порт или дисковая игра, запущенная из программы: снимается окно игры (без рамки), "
            "если оно сейчас впереди. Снимок — PNG в папке screenshots рядом с программой и в запись этой сессии "
            "в «Своих заметках». Поверх игры ничего не появляется — только щелчок затвора; не получилось — два "
            "низких «бип». Клавиша нужна программе только на время игры: потом она снова свободна. Чёрные снимки — "
            "у игр в исключительном полноэкранном режиме: включите в игре оконный или «безрамочный».", self)
        shots_hint.setWordWrap(True)
        self.shots_status = CaptionLabel("", self)
        self.shots_status.setWordWrap(True)
        self.shots_status.hide()
        shots_folder = PushButton("Открыть папку снимков", self, FIF.FOLDER)
        shots_folder.clicked.connect(self.open_screenshots_folder)

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
        layout.setSpacing(16)
        layout.addWidget(TitleLabel("Настройки", self))
        # Каждый раздел — своей карточкой со значком. Обновление — первым: на него указывает точка на «Настройках»
        update = SettingsSection(FIF.UPDATE, "Обновление программы", content)
        for item in (self.update_status, self.update_notes, update_buttons, self.update_bar, self.update_progress,
                     update_row, update_hint):
            update.add(item)
        keys_section = SettingsSection(fluent_icon("FINGERPRINT", FIF.PEOPLE), "Ключи", content)
        keys_section.add(keys)
        keys_section.add(key_buttons)
        sound_section = SettingsSection(FIF.MUSIC, "Звук", content)
        sound_section.add(sound)
        sound_section.add(self.credit_label)
        shots_section = SettingsSection(fluent_icon("CAMERA", FIF.PHOTO), "Снимки экрана в портах и дисковых играх",
                                        content)
        for item in (shots_row, hotkey_row, shot_sound_row, shots_hint, self.shots_status):
            shots_section.add(item)
        shots_section.add(shots_folder, 0, Qt.AlignLeft)
        look = SettingsSection(fluent_icon("PALETTE", FIF.PHOTO), "Оформление", content)
        for item in (appearance, selection_row, selection_hint):
            look.add(item)
        translate = SettingsSection(fluent_icon("LANGUAGE", FIF.GLOBE), "Перевод описаний", content)
        for item in (translate_row, model_row, self.model_status):
            translate.add(item)
        browser = SettingsSection(FIF.GLOBE, "Встроенный браузер", content)
        browser.add(links)
        browser.add(links_hint)
        browser.add(store_logout, 0, Qt.AlignLeft)
        browser.add(store_hint)
        for section in (update, keys_section, sound_section, shots_section, look, translate, browser):
            layout.addWidget(section)
        layout.addStretch(1)

    # --- Снимки экрана ---
    def _shots_status(self, text, warning=False):
        self.shots_status.setText(text)
        self.shots_status.setTextColor(*(WARNING_TEXT if warning else (QColor(0, 0, 0, 160),
                                                                        QColor(255, 255, 255, 170))))
        self.shots_status.setVisible(bool(text))

    def set_screenshots_enabled(self, on):
        core.save_settings(screenshots_enabled=on)
        core.log.info("Снимки экрана по клавише: %s", "включены" if on else "выключены")
        self.win.play_tracker.shots.sync()

    def set_screenshot_hotkey(self, hotkey):
        problem = core.hotkey_problem(hotkey["vk"], hotkey["mods"])
        if problem:
            self._shots_status(f"{hotkey['text']}: {problem}.", warning=True)
            return
        if not self.win.play_tracker.shots.check_free(hotkey["vk"], hotkey["mods"]):
            self._shots_status(f"{hotkey['text']} уже забрала другая программа — выберите другое сочетание.",
                               warning=True)
            return
        core.save_settings(screenshot_hotkey=hotkey)
        self.hotkey_button.setText(hotkey["text"])
        core.log.info("Клавиша снимка: %s", hotkey["text"])
        self._shots_status(f"Снимок — {hotkey['text']}.")
        self.win.play_tracker.shots.sync()  # игра уже идёт — новое сочетание сразу

    def _play_shutter(self):
        shots = self.win.play_tracker.shots
        if not core.load_settings().get("screenshot_sound", True):
            self._shots_status("Щелчок затвора выключен — включите его, чтобы послушать.")
            return
        shots.play("shot")

    def open_screenshots_folder(self):
        os.makedirs(core.SCREENSHOTS_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(core.SCREENSHOTS_DIR))

    def forget_store_login(self):
        if not confirm(self.win, "Выйти из магазина Steam?",
                       "Встроенный браузер забудет вход в магазин Steam и куки всех сайтов. Список купленных, но "
                       "не скачанных саундтреков исчезнет до следующей синхронизации. Скачанные саундтреки, игры "
                       "и ключи останутся.", "Выйти"):
            return
        self.win.music_page.forget_store_login()
        notify("success", self.win, "Вы вышли из магазина Steam",
               "Чтобы снова увидеть покупки, нажмите «Синхронизировать со Steam» на вкладке «Саундтреки».")

    # --- Обновление программы ---
    UPDATE_NOTES_SHOWN = 1500  # длинную заметку к релизу целиком — на странице релиза

    def update_updates(self):
        updater = self.win.updater
        release = updater.release
        available = updater.available()
        version = f"Установлена версия {updater.current}" + (" (для проверки обновления)" if updater.fake else "")
        if updater.installing:
            status = version + ". Ставлю обновление…"
        elif updater.checking:
            status = version + ". Проверяю GitHub…"
        elif available:
            status = f"{version}. Вышла версия {release['tag'].lstrip('vV')}"
            if release["tag"] == self.win.settings.get("update_skip"):
                status += " — вы её пропустили, но обновиться можно и сейчас"
            status += "."
        elif release:
            status = version + " — это последняя."
        else:
            status = version + "."
        checked = self.win.settings.get("update_checked") or 0
        if checked and not updater.checking and not updater.installing:
            status += " Проверено " + time.strftime("%d.%m.%Y в %H:%M", time.localtime(checked)) + "."
        self.update_status.setText(status)
        notes = (release or {}).get("notes") or ""
        if len(notes) > self.UPDATE_NOTES_SHOWN:
            notes = notes[:self.UPDATE_NOTES_SHOWN].rsplit("\n", 1)[0] + "\n\n… целиком — на странице релиза"
        self.update_notes.setText(notes if available else "")
        self.update_notes.setVisible(available and bool(notes))
        busy = updater.checking or updater.installing
        self.update_button.setVisible(available)
        self.update_button.setEnabled(not busy)
        self.update_page_button.setVisible(available)
        self.update_skip_button.setVisible(available and release["tag"] != self.win.settings.get("update_skip"))
        self.update_check_button.setEnabled(not busy)
        self.update_flow.invalidate()

    def open_release_page(self):
        release = self.win.updater.release
        url = (release or {}).get("url") or f"{REPO_URL}/releases/latest"
        QDesktopServices.openUrl(QUrl(url))

    def install_update(self):
        updater = self.win.updater
        release = updater.release
        if not updater.available() or updater.installing:
            return
        tag = release["tag"].lstrip("vV")
        if not core.can_self_update():
            if not getattr(sys, "frozen", False):
                text = ("Программа запущена из исходников (python MySteamRandomApp.py) — она не заменяет свои файлы "
                        "сама. Заберите новую версию из репозитория: git pull — или скачайте .exe со страницы релиза.")
            else:
                text = ("Эта сборка лежит папкой (рядом с .exe есть _internal) — одним файлом её не обновить. "
                        "Скачайте новую версию со страницы релиза.")
            if confirm(self.win, f"Обновить до {tag}?", text, "Открыть страницу релиза"):
                self.open_release_page()
            return
        asset = release.get("asset")
        if not asset:
            if confirm(self.win, f"Обновить до {tag}?",
                       f"В релизе нет файла {core.UPDATE_ASSET} с контрольной суммой, поэтому программа не может "
                       "проверить его сама и ставить не будет. Скачайте обновление со страницы релиза вручную.",
                       "Открыть страницу релиза"):
                self.open_release_page()
            return
        size = f" ({disk_size_text(asset['size'])})" if asset.get("size") else ""
        if not confirm(self.win, f"Обновить до версии {tag}?",
                       f"Программа скачает {core.UPDATE_ASSET}{size} со страницы релиза {release['tag']} на GitHub "
                       "и сверит его контрольную сумму SHA-256 с той, что посчитал GitHub. Сойдётся — закроется и "
                       "откроется уже новой версией. Не сойдётся — ничего не изменится.\n\n"
                       "Игры, коллекции, настройки, ключи и обложки останутся на месте. Если играет порт или "
                       "дисковая игра, время в ней до этой минуты засчитается.", "Обновить"):
            return
        core.log.info("Обновление программы: ставлю %s (сейчас %s)", release["tag"], updater.current)
        updater.installing = True
        updater.changed.emit()
        self.update_bar.setRange(0, 0)
        self.update_bar.show()
        self.update_progress.setText("Подключаюсь к GitHub…")
        self.update_progress.show()
        relay = ProgressRelay(self)
        relay.progress.connect(self._on_update_progress)
        worker = Worker(core.download_update, release, relay.report, parent=self)
        worker.finished.connect(self._on_update_downloaded)
        worker.failed.connect(self._on_update_failed)
        worker.finished.connect(relay.deleteLater)
        worker.failed.connect(relay.deleteLater)
        worker.start()

    def _on_update_progress(self, text, percent):
        self.update_progress.setText(text)
        if percent >= 0:
            self.update_bar.setRange(0, 100)
            self.update_bar.setValue(percent)

    def _on_update_downloaded(self, new_path):
        self.update_progress.setText("Файл проверен. Перезапускаюсь…")
        try:
            exe = core.apply_update(new_path)
            core.restart_program(exe, self.win.updater.current)
        except OSError as e:
            core.log.exception("Обновление программы: новый файл не встал на место")
            self._on_update_failed(f"OSError: не удалось заменить файл программы: {e}. Ничего не изменено — "
                                   "можно скачать обновление со страницы релиза вручную")
            return
        QApplication.quit()  # новая версия дождётся, пока эта закроется (core.finish_update)

    def _on_update_failed(self, message):
        updater = self.win.updater
        updater.installing = False
        self.update_bar.hide()
        self.update_progress.hide()
        updater.changed.emit()
        text = message.split(": ", 1)[-1]
        core.log.warning("Обновление программы не поставлено: %s", text)
        notify("error", self.win, "Обновление не поставлено", text + "\nПодробности — на вкладке «Логи».", 10000)

    def set_sounds_enabled(self, enabled):
        self.win.sounds.enabled = enabled
        core.save_settings(sounds_enabled=enabled)
        if enabled:
            self.win.sounds.win()  # пример звука

    def set_music_enabled(self, enabled):
        if enabled == self.win.music.enabled:
            return
        self.win.set_music_enabled(enabled)  # сохраняется: при следующем запуске будет так же
        core.log.info("Фоновая музыка %s в «Настройках»", "включена" if enabled else "выключена")

    def _show_music_enabled(self):
        """Музыку включили или выключили кнопкой в плеере — переключатель показывает то же самое. Пока окно
        только открывается и музыка ещё не включилась, показываем то, что сохранено."""
        enabled = self.win.music.enabled or (not self.win.isVisible() and bool(self.win.settings["music_enabled"]))
        if self.music_switch.isChecked() != enabled:
            self.music_switch.blockSignals(True)
            self.music_switch.setChecked(enabled)
            self.music_switch.blockSignals(False)

    def set_selection_backdrop(self, enabled):
        self.win.settings["selection_backdrop"] = enabled
        core.save_settings(selection_backdrop=enabled)
        core.log.info("Подложка из выделенных игр: %s", "включена" if enabled else "выключена")
        for backdrop in SelectionBackdrop.instances:
            backdrop.apply_setting()

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
        if steam_key and not core.STEAM_KEY_RE.fullmatch(steam_key):
            notify("warning", self.win, "Неверный ключ Steam Web API", "Это 32 знака из цифр и букв A–F.")
            return
        changes = {"steam_api_key": steam_key, "steam_id": steam_id, "griddb_api_key": griddb_key}
        if steam_key and steam_id:
            changes["steam_setup"] = "done"  # ключи введены вручную — мастер при запуске не нужен
        grid_changed = griddb_key != core.griddb_key()
        try:
            core.save_settings(**changes)
        except OSError as e:
            notify("error", self.win, "Не удалось сохранить настройки", str(e))
            return
        self.win.settings = core.load_settings()
        notify("success", self.win, "Ключи сохранены")
        if grid_changed:
            self.win.on_griddb_key_changed()

    def show_keys(self):
        """Ключи и SteamID в полях — заново из settings.json (например, после мастера подключения)."""
        settings = core.load_settings()
        self.steam_key_edit.setText(settings["steam_api_key"])
        self.steam_id_edit.setText(settings["steam_id"])
        self.griddb_edit.setText(settings["griddb_api_key"])

    def forget_steam(self):
        if not confirm(self.win, "Удалить ключ и выйти?",
                       "Ключ Steam Web API и SteamID будут стёрты с этого компьютера. Список игр, обложки и "
                       "остальные настройки останутся. В самом Steam ключ продолжит действовать — чтобы отключить "
                       "его совсем, на странице ключа нажмите «Revoke My Steam Web API Key».", "Удалить"):
            return
        try:
            core.save_settings(steam_api_key="", steam_id="", steam_setup="")
        except OSError as e:
            notify("error", self.win, "Не удалось удалить ключ", str(e))
            return
        core.log.info("Ключ Steam Web API и SteamID удалены по кнопке «Удалить ключ и выйти»")
        self.win.session_steam_id = ""  # и тот, что помнился только до закрытия программы
        self.win.settings = core.load_settings()
        self.show_keys()
        self.win.music_page.build_sources()
        notify("success", self.win, "Ключ и SteamID удалены",
               "Подключить Steam снова — кнопка «Войти через Steam».")

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

    # --- Перевод описаний ---
    def change_translate_mode(self, index):
        mode = core.TRANSLATE_MODES[index]
        core.save_settings(translate_mode=mode)
        core.log.info("Перевод описаний: %s", TRANSLATE_MODE_TITLES[index])
        self.update_translator()

    def update_translator(self):
        """Что с модулем перевода на ПК: есть ли движок в программе, скачана ли модель — и что делает кнопка."""
        if self._model_busy:
            return
        info = core.local_model_info()
        local_only = core.translate_mode() == "local"
        if not core.local_engine_available():
            self.model_button.setEnabled(False)
            text = ("Перевод на ПК недоступен: в этой сборке нет его движка (библиотек ctranslate2 и sentencepiece). "
                    "Описания переводит Google.")
            if local_only:
                text += " Пока выбрано «Только на этом ПК», описания не переводятся."
        elif info:
            self.model_button.setEnabled(True)
            self.model_button.setText("Удалить модуль перевода")
            self.model_button.setIcon(FIF.DELETE)
            text = (f"Модуль перевода скачан ({disk_size_text(info['size'])}). Он переводит прямо на этом ПК, без "
                    "интернета: текст описаний никуда не отправляется. Качество чуть ниже, чем у Google.")
        else:
            self.model_button.setEnabled(True)
            self.model_button.setText("Скачать модуль перевода")
            self.model_button.setIcon(FIF.DOWNLOAD)
            text = ("Google Переводчик иногда отказывает — тогда описание остаётся по-английски. Модуль перевода "
                    "подстрахует его: переводит с английского прямо на этом ПК, без интернета. Скачивается один "
                    "раз, около 190 МБ, с сайта открытого проекта Argos Translate.")
            if local_only:
                text += " Пока модуль не скачан, при «Только на этом ПК» описания не переводятся."
        self.model_status.setText(text)

    def toggle_local_model(self):
        if core.local_model_info():
            if not confirm(self.win, "Удалить модуль перевода?",
                           "Модель перевода удалится с этого ПК. Уже переведённые описания останутся, а новые "
                           "будет переводить только Google. Скачать модуль снова можно здесь же.", "Удалить"):
                return
            try:
                core.remove_local_model()
            except OSError as e:
                notify("error", self.win, "Не удалось удалить модуль перевода",
                       f"{e}. Закройте программу и удалите папку cache\\translate вручную.")
                return
            self.update_translator()
            notify("success", self.win, "Модуль перевода удалён")
            return
        if not confirm(self.win, "Скачать модуль перевода?",
                       "Около 190 МБ с argos-net.com — сайта открытого проекта Argos Translate. Это модель для "
                       "перевода с английского на русский: только данные, не программа. Программа сверит её "
                       "контрольную сумму, положит в папку cache\\translate рядом с собой и будет переводить "
                       "описания и отзывы без интернета.",
                       "Скачать"):
            return
        self._model_busy = True
        self.model_button.setEnabled(False)
        self.model_bar.setRange(0, 100)
        self.model_bar.setValue(0)
        self.model_bar.show()
        self.model_status.setText("Подключаюсь…")
        relay = ProgressRelay(self)
        relay.progress.connect(self._on_model_progress)
        worker = Worker(core.download_local_model, relay.report, parent=self)
        worker.finished.connect(self._on_model_installed)
        worker.failed.connect(self._on_model_failed)
        worker.finished.connect(relay.deleteLater)
        worker.failed.connect(relay.deleteLater)
        worker.start()

    def _on_model_progress(self, text, percent):
        self.model_status.setText(text)
        if percent >= 0:
            self.model_bar.setValue(percent)

    def _finish_model(self):
        self._model_busy = False
        self.model_bar.hide()
        self.update_translator()

    def _on_model_installed(self, info):
        self._finish_model()
        notify("success", self.win, "Модуль перевода установлен",
               f"Теперь описания переводятся и без Google. Пробная фраза: «{info['sample']}»", 9000)

    def _on_model_failed(self, message):
        self._finish_model()
        notify("error", self.win, "Модуль перевода не установлен",
               message.split(": ", 1)[-1] + "\nПодробности — на вкладке «Логи».", 9000)


# Перевод описаний — в том же порядке, что core.TRANSLATE_MODES
TRANSLATE_MODE_TITLES = ("Google, при сбое — на этом ПК", "Только на этом ПК, без интернета", "Не переводить")


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


# --- Страница «Купить игры» ---------------------------------------------------
class ShopItemDelegate(QStyledItemDelegate):
    """Лот Plati.Market в списке «Найти игры»: картинка, название, крупная цена, продажи, продавец и метки.
    Два вида — строкой во всю ширину (list) и карточкой в сетке (grid), как на сайте площадки."""

    ROW_H = 112
    CARD_W, CARD_H = 216, 318
    THUMB = 84
    PRICE_W = 170

    def __init__(self, page):
        super().__init__(page.list)
        self.page = page
        self.mode = "list"
        self.hover_row = -1
        self.pressed_row = -1

    # Список программы (ListWidget библиотеки интерфейса) сообщает своему рисовальщику, над какой строкой мышь,
    # какую нажали и какие выбраны. Без этих трёх методов он падает при каждом движении мыши
    def setHoverRow(self, row):
        self.hover_row = row

    def setPressedRow(self, row):
        self.pressed_row = row

    def setSelectedRows(self, indexes):
        pass  # выбранный лот видно по option.state — отдельно его хранить не нужно

    def sizeHint(self, option, index):
        if self.mode == "grid":
            return QSize(self.CARD_W, self.CARD_H)
        return QSize(max(200, self.page.list.viewport().width() - 4), self.ROW_H)

    @staticmethod
    def _colors(dark):
        if dark:
            return QColor(255, 255, 255), QColor(255, 255, 255, 165), QColor(255, 255, 255, 14)
        return QColor(0, 0, 0, 228), QColor(0, 0, 0, 150), QColor(0, 0, 0, 10)

    @staticmethod
    def _font(base, pixels=None, bold=False, delta=0):
        """Шрифт от base: ровно pixels пикселей или на delta пунктов крупнее. Шрифты программы бывают заданы
        в пикселях (тогда размера в пунктах у них нет, -1) — их увеличиваем в пикселях."""
        font = QFont(base)
        if pixels:
            font.setPixelSize(pixels)
        elif delta:
            if font.pointSizeF() > 0:
                font.setPointSizeF(font.pointSizeF() + delta)
            elif font.pixelSize() > 0:
                font.setPixelSize(font.pixelSize() + round(delta * 4 / 3))
        font.setBold(bold)
        return font

    def _back(self, painter, rect, dark, selected, hover, back):
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(back)
        painter.drawPath(path)
        if hover or selected:
            painter.setBrush(QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 8))
            painter.drawPath(path)
        if selected:
            accent = QColor(themeColor())
            painter.setPen(QPen(accent, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)

    def _thumb(self, painter, rect, item, dark):
        """Картинка лота по центру rect, с подложкой, пока картинка не пришла."""
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(255, 255, 255, 10) if dark else QColor(0, 0, 0, 8))
        pixmap = self.page.images.get(item["image"])
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(rect.size().toSize(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            target = QRectF(0, 0, scaled.width(), scaled.height())
            target.moveCenter(rect.center())
            painter.drawPixmap(target.topLeft(), scaled)
        painter.restore()

    def _price(self, item):
        currency = self.page._currency()
        return ShopPage._price_text(item["prices"].get(currency), currency)

    @staticmethod
    def _sold(item):
        return "продажи скрыты" if item["sold"] is None else f"продано {number_text(item['sold'])}"

    def paint(self, painter, option, index):
        item = index.data(Qt.UserRole)
        if not isinstance(item, dict):
            return
        painter.save()
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        dark = isDarkTheme()
        text, caption, back = self._colors(dark)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver) or index.row() == self.hover_row
        if self.mode == "grid":
            self._paint_card(painter, option, item, dark, text, caption, back, selected, hover)
        else:
            self._paint_row(painter, option, item, dark, text, caption, back, selected, hover)
        painter.restore()

    def _paint_row(self, painter, option, item, dark, text, caption, back, selected, hover):
        rect = QRectF(option.rect).adjusted(4, 4, -4, -4)
        self._back(painter, rect, dark, selected, hover, back)
        pad = 12
        thumb = QRectF(rect.x() + pad, rect.center().y() - self.THUMB / 2, self.THUMB, self.THUMB)
        self._thumb(painter, thumb, item, dark)
        # Цена — справа, крупно; под ней скидка и продажи
        price_box = QRectF(rect.right() - pad - self.PRICE_W, rect.y() + pad, self.PRICE_W, rect.height() - 2 * pad)
        price_font = self._font(option.font, pixels=22, bold=True)
        painter.setFont(price_font)
        painter.setPen(text)
        price_h = QFontMetrics(price_font).height()
        painter.drawText(QRectF(price_box.x(), price_box.y(), price_box.width(), price_h),
                         Qt.AlignRight | Qt.AlignVCenter, self._price(item))
        small = self._font(option.font)
        painter.setFont(small)
        line = QFontMetrics(small).height()
        y = price_box.y() + price_h + 4
        if item["discount"]:
            painter.setPen(QColor(16, 160, 80) if not dark else QColor(108, 203, 95))
            painter.drawText(QRectF(price_box.x(), y, price_box.width(), line), Qt.AlignRight | Qt.AlignVCenter,
                             f"скидка {item['discount']}%")
            y += line + 2
        painter.setPen(caption)
        painter.drawText(QRectF(price_box.x(), y, price_box.width(), line), Qt.AlignRight | Qt.AlignVCenter,
                         self._sold(item))
        # Середина: название в две строки, продавец с отзывами, метки
        left = thumb.right() + 14
        width = price_box.x() - 16 - left
        name_font = self._font(option.font, bold=True, delta=1)
        name_metrics = QFontMetrics(name_font)
        name = two_lines(item["name"], name_metrics, round(width))
        name_h = name_metrics.lineSpacing() * (name.count("\n") + 1)
        painter.setFont(name_font)
        painter.setPen(text)
        y = rect.y() + pad
        painter.drawText(QRectF(left, y, width, name_h), Qt.AlignLeft | Qt.AlignTop, name)
        painter.setFont(small)
        metrics = QFontMetrics(small)
        y += name_h + 6
        painter.setPen(caption)
        painter.drawText(QRectF(left, y, width, line), Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(self.page._seller_text(item), Qt.ElideRight, round(width)))
        y += line + 4
        self._chips(painter, QRectF(left, y, width, line + 6), self.page._labels(item), small, dark)

    def _paint_card(self, painter, option, item, dark, text, caption, back, selected, hover):
        rect = QRectF(option.rect).adjusted(6, 6, -6, -6)
        self._back(painter, rect, dark, selected, hover, back)
        pad = 10
        width = rect.width() - 2 * pad
        thumb = QRectF(rect.x() + pad, rect.y() + pad, width, 128)
        self._thumb(painter, thumb, item, dark)
        y = thumb.bottom() + 8
        name_font = self._font(option.font, bold=True)
        name_metrics = QFontMetrics(name_font)
        painter.setFont(name_font)
        painter.setPen(text)
        name = two_lines(item["name"], name_metrics, round(width))
        name_h = name_metrics.lineSpacing() * 2
        painter.drawText(QRectF(rect.x() + pad, y, width, name_h), Qt.AlignLeft | Qt.AlignTop, name)
        y += name_h + 6
        price_font = self._font(option.font, pixels=24, bold=True)
        price_h = QFontMetrics(price_font).height()
        painter.setFont(price_font)
        painter.drawText(QRectF(rect.x() + pad, y, width, price_h), Qt.AlignLeft | Qt.AlignVCenter, self._price(item))
        small = self._font(option.font)
        line = QFontMetrics(small).height()
        if item["discount"]:
            painter.setFont(small)
            painter.setPen(QColor(16, 160, 80) if not dark else QColor(108, 203, 95))
            painter.drawText(QRectF(rect.x() + pad, y, width, price_h), Qt.AlignRight | Qt.AlignVCenter,
                             f"−{item['discount']}%")
        y += price_h + 4
        painter.setFont(small)
        painter.setPen(caption)
        metrics = QFontMetrics(small)
        painter.drawText(QRectF(rect.x() + pad, y, width, line), Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(f"{self._sold(item)} · 👍 {number_text(item['good'])}",
                                            Qt.ElideRight, round(width)))
        y += line + 6
        self._chips(painter, QRectF(rect.x() + pad, y, width, line + 6), self.page._labels(item), small, dark)

    @staticmethod
    def _chips(painter, rect, labels, font, dark):
        """Метки лота — плашками в одну строку; что не влезло, не рисуется."""
        painter.setFont(font)
        metrics = QFontMetrics(font)
        x = rect.x()
        for label in labels:
            w = metrics.horizontalAdvance(label) + 14
            if x + w > rect.right() + 0.5:
                break
            chip = QRectF(x, rect.y(), w, rect.height())
            warn = label == "не игра?"
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(196, 43, 28, 60) if warn else
                             (QColor(255, 255, 255, 22) if dark else QColor(0, 0, 0, 14)))
            painter.drawRoundedRect(chip, rect.height() / 2, rect.height() / 2)
            painter.setPen(QColor(255, 255, 255, 215) if dark else QColor(0, 0, 0, 190))
            painter.drawText(chip, Qt.AlignCenter, label)
            x += w + 6


class ShopPage(QWidget):
    """Вкладка «Найти игры на Plati.Market»: поиск ключей, гифтов и аккаунтов Steam и Xbox.

    Ищет открытый поиск площадки (core.search_plati) — без ключа и без входа. Найденное показывается после
    «Показать» списком или сеткой: с крупной ценой в выбранной валюте, продажами, продавцом и отзывами, его можно
    сортировать и отбирать. Покупка — только на сайте: лот открывается во внешнем браузере."""

    SORTS = (("relevance", "Сначала подходящие"), ("price_asc", "Сначала дешёвые"), ("price_desc", "Сначала дорогие"),
             ("sold", "Больше продаж"))
    PLATFORMS = (("all", "Все платформы"), ("steam", "Steam"), ("xbox", "Xbox"), ("other", "Другие"))
    VIEWS = (("list", "Списком"), ("grid", "Сеткой"))
    KIND_TITLES = {key: title for key, title, _pattern in core.SHOP_KINDS}
    PLATFORM_TITLES = {key: title for key, title, _pattern in core.SHOP_PLATFORMS}
    ITEM_PREFIX = "https://plati.market/itm/"

    def __init__(self, window):
        super().__init__()
        self.setObjectName("shopPage")
        self.win = window
        self.result = None  # последний ответ поиска: {"query", "total", "items"}
        self.shown = []  # лоты в списке сейчас — по строкам
        self.images = {}  # картинки лотов: адрес → QPixmap
        self._token = 0
        self.loader = ImageLoader(self)
        self.loader.loaded.connect(self._on_image)
        settings = core.load_settings()

        self.title = TitleLabel("Найти игры на Plati.Market", self)
        heading = SubtitleLabel("Ключи, гифты и аккаунты Steam и Xbox", self)
        self.search = SearchLineEdit(self)
        # Подсказки в поле нет — она всплывает рядом с мышкой при наведении
        self.search.setToolTip("Название игры, например Doom — и Enter")
        self.search.setFont(resized_font(self.search.font(), 2))
        # Лупа в поле — searchSignal; Enter библиотека в него не передаёт, поэтому — отдельно
        if hasattr(self.search, "searchSignal"):
            self.search.searchSignal.connect(lambda _text: self.find())
        self.search.returnPressed.connect(self.find)
        self.search.setFixedHeight(40)
        enlarge_line_edit_buttons(self.search)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self.search, 1)

        self.bar = IndeterminateProgressBar(self)
        self.bar.hide()
        # Под полем — итог поиска и «Показать», по центру. «Показать» после нажатия исчезает, а вместо неё
        # встают фильтры — выдача под ними не прыгает
        self.status = StrongBodyLabel("", self)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        self.show_button = PrimaryPushButton("Показать", self, FIF.VIEW if hasattr(FIF, "VIEW") else FIF.SEARCH)
        self.show_button.setMinimumWidth(180)
        self.show_button.clicked.connect(self.show_results)
        self.show_button.hide()

        # Валюта, порядок, платформа и «Только игры» — запоминаются в настройках
        self.currency_box = ComboBox(self)
        self.currency_box.addItems([f"{code} {sign}" for code, (_f, sign) in core.SHOP_CURRENCIES.items()])
        codes = list(core.SHOP_CURRENCIES)
        self.currency_box.setCurrentIndex(codes.index(settings["shop_currency"])
                                          if settings["shop_currency"] in codes else 0)
        self.sort_box = ComboBox(self)
        self.sort_box.addItems([title for _key, title in self.SORTS])
        self.sort_box.setCurrentIndex(next((i for i, (k, _t) in enumerate(self.SORTS) if k == settings["shop_sort"]), 0))
        self.platform_box = ComboBox(self)
        self.platform_box.addItems([title for _key, title in self.PLATFORMS])
        self.platform_box.setCurrentIndex(next((i for i, (k, _t) in enumerate(self.PLATFORMS)
                                                if k == settings["shop_platform"]), 0))
        self.games_only = CheckBox("Только игры", self)
        self.games_only.setToolTip("Прятать читы, игровую валюту, пополнения, книги и прочие лоты без платформы "
                                   "(Steam, Xbox, EA app…), а также лоты, в названии которых нет всех слов из поиска "
                                   "(по «Battlefield 4» не покажет «Battlefield 2042»)")
        self.games_only.setChecked(bool(settings["shop_games_only"]))
        self.view_box = ComboBox(self)
        self.view_box.addItems([title for _key, title in self.VIEWS])
        self.view_box.setCurrentIndex(next((i for i, (k, _t) in enumerate(self.VIEWS)
                                            if k == settings["shop_view"]), 0))
        for box in (self.currency_box, self.sort_box, self.platform_box):
            box.currentIndexChanged.connect(self._on_filters)
        self.games_only.stateChanged.connect(self._on_filters)
        self.view_box.currentIndexChanged.connect(self._on_view)
        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(8)
        filters.addWidget(self.currency_box)
        filters.addWidget(self.sort_box)
        filters.addWidget(self.platform_box)
        filters.addWidget(self.games_only)
        filters.addStretch(1)
        filters.addWidget(self.view_box)
        self.filters_box = QWidget(self)
        self.filters_box.setLayout(filters)
        self.filters_box.hide()

        self.list = ListWidget(self)
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setMovement(QListView.Static)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.delegate = ShopItemDelegate(self)
        self.list.setItemDelegate(self.delegate)
        self.grid_fit = GridFit(self.list, QSize(ShopItemDelegate.CARD_W, ShopItemDelegate.CARD_H), gap=4)
        self.list.viewport().installEventFilter(self)  # колесо в сетке — см. eventFilter
        self.list.itemDoubleClicked.connect(lambda _item: self.open_selected())
        self.list.currentRowChanged.connect(lambda row: self.open_button.setEnabled(0 <= row < len(self.shown)))
        self.list.hide()
        self._apply_view()
        self.open_button = PushButton("Открыть на Plati.Market", self, FIF.LINK)
        self.open_button.setToolTip("Страница лота — во внешнем браузере. Двойной щелчок по лоту — то же самое")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.hide()

        note = plain_label("Plati.Market — площадка частных продавцов. Перед покупкой проверьте отзывы о продавце, "
                           "регион и способ активации. Покупка проходит на сайте: программа ничего не покупает, "
                           "не входит в ваши аккаунты и не знает о покупках.", self, caption=True)
        note.setTextColor(*WARNING_TEXT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 20, 36, 28)
        layout.setSpacing(12)
        layout.addWidget(self.title)
        layout.addWidget(heading)
        # Заголовки всегда на месте, сверху. Пока выдачи нет, поле поиска стоит посередине окна — пустые места
        # сверху и снизу поровну. Нажали «Показать» — поле поднимается под заголовки, выдача занимает остальное
        self.top_spacer = QWidget(self)
        self.top_spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout.addWidget(self.top_spacer, 1)
        layout.addLayout(search_row)
        layout.addWidget(self.bar)
        layout.addWidget(self.status)
        layout.addWidget(self.show_button, 0, Qt.AlignHCenter)
        layout.addWidget(self.filters_box)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.open_button, 0, Qt.AlignLeft)
        self.spacer = QWidget(self)  # пока списка нет — пустое место, чтобы подсказка стояла внизу
        self.spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout.addWidget(self.spacer, 1)
        layout.addWidget(note)
        self.mini = MiniPlayerSpot(self, self.title)
        self._set_status("Введите название игры и нажмите Enter или лупу в поле. Цены, продажи и отзывы — "
                         "с Plati.Market.")

    # --- Поиск ---
    def find(self, query=None):
        """Искать query (или то, что в поле поиска)."""
        if isinstance(query, str):
            self.search.setText(query)
        query = re.sub(r"\s+", " ", self.search.text()).strip()
        if self.bar.isVisible() and query == getattr(self, "_query", None):
            return  # этот поиск уже идёт (Enter и лупа подряд)
        if len(query) < core.PLATI_MIN_QUERY:
            self._set_status(f"Введите хотя бы {core.PLATI_MIN_QUERY} буквы названия.")
            return
        self._token += 1
        token = self._token
        self._query = query
        self.result = None
        self._hide_results()
        self.bar.show()
        self._set_status(f"Ищу «{query}» на Plati.Market…")
        worker = Worker(core.search_plati, query, parent=self)
        worker.finished.connect(lambda result: token == self._token and self._on_found(result))
        worker.failed.connect(lambda message: token == self._token and self._on_failed(message))
        worker.start()

    def _on_found(self, result):
        self.bar.hide()
        self.result = result
        if not result["items"]:
            self._set_status(f"По «{result['query']}» на Plati.Market ничего не нашлось. Попробуйте название "
                             "по-английски или короче.")
            return
        self._update_status()
        self.show_button.show()

    def _on_failed(self, message):
        self.bar.hide()
        self._set_status("Plati.Market не ответил: " + message.split(": ", 1)[-1] + ". Попробуйте ещё раз через "
                         "минуту.", warning=True)

    def _update_status(self):
        result = self.result
        if not result:
            return
        fitting = len(self._filtered())
        text = f"Найдено {offers_word(result['total'])}"
        if result["total"] > len(result["items"]):
            text += f", загружены {len(result['items'])} самых подходящих"
        if fitting != len(result["items"]):
            text += f" · под фильтры подходит {fitting}"
        self._set_status(text + ".")

    def _set_status(self, text, warning=False):
        self.status.setText(text)
        if warning:
            self.status.setTextColor(*WARNING_TEXT)
        else:
            self.status.setTextColor(QColor(0, 0, 0), QColor(255, 255, 255))

    # --- Список ---
    def show_results(self):
        self.show_button.hide()
        self.top_spacer.hide()
        self.filters_box.show()
        self.list.show()
        self.open_button.show()
        self.spacer.hide()
        self._fill()

    def _hide_results(self):
        self.show_button.hide()
        self.filters_box.hide()
        self.list.hide()
        self.list.clear()
        self.shown, self.images = [], {}
        self.loader.clear()
        self.open_button.hide()
        self.spacer.show()

    def _currency(self):
        return list(core.SHOP_CURRENCIES)[max(0, self.currency_box.currentIndex())]

    def _filtered(self):
        """Лоты под выбранные платформу и «Только игры» — в выбранном порядке."""
        items = (self.result or {}).get("items") or []
        platform = self.PLATFORMS[max(0, self.platform_box.currentIndex())][0]
        if platform in ("steam", "xbox"):
            items = [i for i in items if platform in i["platforms"]]
        elif platform == "other":
            items = [i for i in items if "steam" not in i["platforms"] and "xbox" not in i["platforms"]]
        if self.games_only.isChecked():
            # Только игры: лот с платформой (книги «Контрольная…» по запросу «Control» её не называют), не чит
            # и не валюта, и в названии — все слова из поиска
            wanted = core.shop_words(self.result["query"])
            items = [i for i in items if i["platforms"] and not i["junk"] and wanted <= set(i["words"])]
        sort = self.SORTS[max(0, self.sort_box.currentIndex())][0]
        currency = self._currency()
        if sort in ("price_asc", "price_desc"):
            priced = [i for i in items if i["prices"].get(currency) is not None]
            priced.sort(key=lambda i: i["prices"][currency], reverse=sort == "price_desc")
            items = priced + [i for i in items if i["prices"].get(currency) is None]  # без цены — в конце
        elif sort == "sold":
            # Скрытые продажи — не ноль, а «неизвестно»: такие лоты после тех, где число видно
            items = sorted(items, key=lambda i: (i["sold"] is None, -(i["sold"] or 0)))
        return items

    def _fill(self):
        self.list.clear()
        self.shown = self._filtered()
        for item in self.shown:
            entry = QListWidgetItem()
            entry.setData(Qt.UserRole, item)
            entry.setToolTip(item["name"] if not item["name_eng"] or item["name_eng"] == item["name"]
                             else f"{item['name']}\n{item['name_eng']}")
            self.list.addItem(entry)
            if item["image"] not in self.images:
                self.loader.request(item["image"])
        self.open_button.setEnabled(False)
        self._update_status()
        if not self.shown and self.result and self.result["items"]:
            self._set_status(self.status.text() + " Смените платформу или снимите «Только игры».")

    def _apply_view(self):
        """Список строками во всю ширину или сеткой карточек — как выбрано."""
        mode = self.VIEWS[max(0, self.view_box.currentIndex())][0]
        self.delegate.mode = mode
        if mode == "grid":
            self.list.setViewMode(QListView.IconMode)
            self.list.setFlow(QListView.LeftToRight)
            self.list.setWrapping(True)
            self.grid_fit.fit(force=True)  # карточки — ровно по ширине, без пустой полосы справа
        else:
            self.list.setViewMode(QListView.ListMode)
            self.list.setFlow(QListView.TopToBottom)
            self.list.setWrapping(False)
            self.list.setGridSize(QSize())
        self.list.setMovement(QListView.Static)  # карточки не перетаскиваются
        self.list.setResizeMode(QListView.Adjust)
        self.list.setSpacing(0)
        # Смена вида возвращает прокрутку «по строке» — тогда ползунок в сетке перескакивает через десятки рядов
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.doItemsLayout()

    def _on_view(self, *_):
        core.save_settings(shop_view=self.VIEWS[max(0, self.view_box.currentIndex())][0])
        self._apply_view()

    @staticmethod
    def _price_text(value, currency):
        sign = core.SHOP_CURRENCIES[currency][1]
        if value is None:
            return "цена не указана"
        text = f"{value:,.0f}" if currency == "RUB" or value >= 1000 else f"{value:,.2f}"
        return text.replace(",", " ").replace(".", ",") + f" {sign}"

    @staticmethod
    def _seller_text(item):
        return f"{item['seller'] or 'Продавец'} · 👍 {number_text(item['good'])}  👎 {number_text(item['bad'])}"

    def _labels(self, item):
        """Метки лота: платформа, что это за товар, DLC, регион; «не игра?» — если похоже на чит или валюту."""
        labels = [self.PLATFORM_TITLES[p] for p in item["platforms"]]
        if item["kind"]:
            labels.append(self.KIND_TITLES[item["kind"]])
        if item["dlc"]:
            labels.append("DLC")
        labels += item["regions"]
        if item["junk"]:
            labels.append("не игра?")
        return labels or ["платформа не указана"]

    def _on_image(self, url, content):
        pixmap = QPixmap()
        if not content or not pixmap.loadFromData(content):
            return
        if max(pixmap.width(), pixmap.height()) > 320:  # на карточку больше не нужно — меньше памяти
            pixmap = pixmap.scaled(320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.images[url] = pixmap
        self.list.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.mini.place()
        if self.delegate.mode == "list":
            self.list.doItemsLayout()  # строки — во всю новую ширину

    def _on_filters(self, *_):
        sort = self.SORTS[max(0, self.sort_box.currentIndex())][0]
        platform = self.PLATFORMS[max(0, self.platform_box.currentIndex())][0]
        core.save_settings(shop_currency=self._currency(), shop_sort=sort, shop_platform=platform,
                           shop_games_only=self.games_only.isChecked())
        if self.list.isVisible():
            self._fill()
        else:
            self._update_status()

    # --- Ссылки ---
    def open_selected(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self.shown):
            return
        item = self.shown[row]
        url = item["url"]
        if not url.startswith(self.ITEM_PREFIX):  # открываем только страницы лотов на plati.market
            return
        core.log.info("Найти игры: открываю лот %s на Plati.Market", item["id"])
        QDesktopServices.openUrl(QUrl(url))

    WHEEL_STEP = 110  # пикселей за щелчок колеса в сетке — треть карточки

    def eventFilter(self, obj, event):
        if (obj is self.list.viewport() and event.type() == QEvent.Wheel and self.delegate.mode == "grid"
              and not event.modifiers() & Qt.ControlModifier):
            # В сетке Qt крутит по целому ряду карточек на каждую «строку» колеса — три ряда за щелчок.
            # Крутим сами, по пикселям; тачпад присылает точные пиксели — их и берём
            bar = self.list.verticalScrollBar()
            pixels = event.pixelDelta().y()
            delta = pixels if pixels else round(event.angleDelta().y() / 120 * self.WHEEL_STEP)
            bar.setValue(bar.value() - delta)
            return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.mini.place)
        QTimer.singleShot(0, self.search.setFocus)


def number_text(value):
    """12345 → «12 345»."""
    return f"{int(value):,}".replace(",", " ")


def enlarge_line_edit_buttons(edit, icon=15, size=QSize(38, 32)):
    """Лупа и крестик внутри поля поиска крупнее: у библиотеки они 10 пикселей — в высоком поле едва видны."""
    for name in ("searchButton", "clearButton"):
        button = getattr(edit, name, None)
        if button is not None:
            button.setFixedSize(size)
            button.setIconSize(QSize(icon, icon))


def offers_word(count):
    """«1 предложение», «3 предложения», «45 предложений»."""
    n = abs(int(count)) % 100
    word = "предложений" if 11 <= n <= 14 else {1: "предложение", 2: "предложения", 3: "предложения",
                                                 4: "предложения"}.get(n % 10, "предложений")
    return f"{number_text(count)} {word}"


# --- Страница «Игры Native Port's» ------------------------------------------
def _port_fingerprints(items):
    """Отпечатки файлов запуска для новых портов: [отпечаток или {"error": текст}]. В фоновом потоке."""
    result = []
    for item in items:
        try:
            result.append(core.port_fingerprint(item))
        except OSError as e:
            result.append({"error": str(e)})
    return result


def text_key(text):
    """Короткий ключ текста — под ним хранится его перевод."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _translate_texts(texts):
    """Перевод описаний с GitHub на русский: {"done": {ключ: перевод}, "tried": [ключи]}. В фоновом потоке."""
    done = {}
    for text in texts:
        translated = core.translate_port_text(text)
        if translated:
            done[text_key(text)] = translated
    return {"done": done, "tried": [text_key(t) for t in texts]}


def plain_label(text, parent, caption=False, selectable=False):
    """Подпись с переносом строк и только обычным текстом: тексты с GitHub — не разметка (QLabel иначе сам
    угадывает HTML и показал бы чужие ссылки и картинки)."""
    label = (CaptionLabel if caption else BodyLabel)(text, parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    if selectable:
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class PortScanDialog(MessageBoxBase):
    """Игры, найденные в папке (порты или дисковые): какие добавить и каким файлом каждая запускается."""

    WIDTH = 620

    def __init__(self, parent, scan, title="Найдены порты", github=True):
        super().__init__(parent)
        self.rows = []
        found, empty = scan["found"], scan["empty"]
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        parts = [f"Найдено: {len(found)}"]
        if scan["known"]:
            parts.append(f"уже в списке: {scan['known']}")
        if empty:
            parts.append(f"без файла запуска: {len(empty)} ({', '.join(empty[:4])}{'…' if len(empty) > 4 else ''})")
        self.viewLayout.addWidget(plain_label(
            " · ".join(parts) + ". Отметьте, какие добавить, и проверьте файл запуска. Программа ничего "
            "не запускала — только посмотрела, какие .exe лежат в папках.", self, caption=True))

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 14, 0)  # справа — место под полосу прокрутки
        layout.setSpacing(4)
        for port in found:
            box = CheckBox(port["name"], body)
            box.setChecked(True)
            box.stateChanged.connect(self._update)
            exe = ComboBox(body)
            exe.addItems(port["exes"])
            exe.setFixedWidth(260)
            exe.setToolTip("Каким файлом запускается игра. Первым стоит самый вероятный")
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(box, 1)
            row.addWidget(exe)
            layout.addLayout(row)
            note = os.path.basename(port["folder"]) + (f" · GitHub: {port['repo']}" if port["repo"] else "")
            caption = plain_label(note, body, caption=True)
            caption.setContentsMargins(28, 0, 0, 8)
            layout.addWidget(caption)
            self.rows.append((port, box, exe))
        layout.addStretch(1)
        scroll = ScrollArea(self)
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background: transparent; border: none}")
        scroll.setFixedHeight(min(layout.totalHeightForWidth(self.WIDTH) + 8, max(240, parent.height() - 320)))
        self.viewLayout.addWidget(scroll)
        if github:
            self.viewLayout.addWidget(plain_label(
                "Ссылку на проект порта на GitHub — для проверки обновлений — можно добавить потом: правый клик по "
                "порту → «Изменить…».", self, caption=True))
        self.widget.setMinimumWidth(self.WIDTH + 48)
        self.widget.setMaximumWidth(self.WIDTH + 48)
        self.cancelButton.setText("Отмена")
        self._update()

    def _update(self, *_):
        count = len(self.selected())
        self.yesButton.setText(f"Добавить ({count})" if count else "Добавить")
        self.yesButton.setEnabled(bool(count))

    def selected(self):
        return [{"name": port["name"], "folder": port["folder"], "exe": exe.currentText(), "repo": port["repo"] or ""}
                for port, box, exe in self.rows if box.isChecked()]


class PortEditDialog(MessageBoxBase):
    """Порт: название, папка, файл запуска и ссылка на его проект на GitHub — для проверки обновлений.

    Новую ссылку окно проверяет у GitHub, прежде чем сохранить: с опечаткой в ссылке порт потерял бы сведения
    о версиях. «Найти на GitHub» — поиск проектов по названию; выбирает из них пользователь."""

    def __init__(self, parent, port, title, find_repo=False, github=True):
        super().__init__(parent)
        self.win = parent
        self.github = github
        self.folder = port["folder"]
        self.initial_repo = port.get("repo") or ""
        self.result_port = None
        self._closed = False
        self._checking = False
        self.viewLayout.addWidget(SubtitleLabel(title, self))

        self.name_edit = LineEdit(self)
        self.name_edit.setText(port["name"])
        self.folder_label = plain_label(self.folder, self, caption=True)
        folder_button = TransparentPushButton("Другая папка…", self, FIF.FOLDER)
        folder_button.clicked.connect(self._choose_folder)
        self.exe_box = ComboBox(self)
        self.exe_box.setMinimumWidth(300)
        exes = list(port.get("exes") or [])
        if port.get("exe") and port["exe"] not in exes:
            exes.insert(0, port["exe"])
        self.exe_box.addItems(exes)
        if port.get("exe"):
            self.exe_box.setCurrentText(port["exe"])
        exe_button = TransparentPushButton("Другой файл…", self, FIF.APPLICATION)
        exe_button.clicked.connect(self._choose_exe)
        self.repo_edit = LineEdit(self)
        self.repo_edit.setPlaceholderText("github.com/автор/проект — страница проекта или его релизов")
        self.repo_edit.setClearButtonEnabled(True)
        if port.get("repo"):
            self.repo_edit.setText(f"https://github.com/{port['repo']}")
        self.search_button = TransparentPushButton("Найти на GitHub", self, FIF.SEARCH)
        self.search_button.setToolTip("Поиск проектов на GitHub по названию игры — выберите подходящий из списка")
        self.search_button.clicked.connect(self._search_github)

        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.addWidget(BodyLabel("Название", self), 0, 0)
        form.addWidget(self.name_edit, 0, 1, 1, 2)
        form.addWidget(BodyLabel("Папка", self), 1, 0)
        form.addWidget(self.folder_label, 1, 1)
        form.addWidget(folder_button, 1, 2)
        form.addWidget(BodyLabel("Запуск", self), 2, 0)
        form.addWidget(self.exe_box, 2, 1)
        form.addWidget(exe_button, 2, 2)
        if github:
            form.addWidget(BodyLabel("GitHub", self), 3, 0)
            form.addWidget(self.repo_edit, 3, 1)
            form.addWidget(self.search_button, 3, 2)
        else:  # у дисковых игр GitHub нет — поле не показываем
            self.repo_edit.hide()
            self.search_button.hide()
        form.setColumnStretch(1, 1)
        self.viewLayout.addLayout(form)
        self.viewLayout.addWidget(plain_label(
            "Обложка, фон и логотип ищутся в SteamGridDB по названию. По ссылке на GitHub программа раз в несколько "
            "часов смотрит, вышла ли новая версия, и сообщает о ней. Скачиваете и ставите обновление вы сами — "
            "программа ничего не качает и не запускает. Ссылку можно не указывать." if github else
            "Обложка, фон и логотип ищутся в SteamGridDB по названию — если игра нашлась не та, поправьте "
            "название. Запускается только выбранный файл из папки игры.", self, caption=True))
        self.status = plain_label("", self, caption=True)
        self.status.setTextColor(*WARNING_TEXT)
        self.status.hide()
        self.viewLayout.addWidget(self.status)

        self.widget.setMinimumWidth(640)
        self.widget.setMaximumWidth(640)
        self.yesButton.setText("Сохранить")
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._save)
        self.cancelButton.setText("Отмена")
        if find_repo and not self.initial_repo and os.path.isdir(self.folder):
            # Ссылки нет — пока окно открыто, ищем её в файлах порта (настройки, сами .exe)
            self.status.setText("Ищу ссылку на GitHub в файлах порта…")
            self.status.setTextColor(QColor(0, 0, 0, 160), QColor(255, 255, 255, 170))
            self.status.show()
            worker = Worker(core.scan_port_folder, self.folder, parent=self.win)
            worker.finished.connect(self._on_repo_hint)
            worker.failed.connect(lambda _message: not self._closed and self.status.hide())
            worker.start()

    def _on_repo_hint(self, found):
        if self._closed:
            return
        self.status.setTextColor(*WARNING_TEXT)
        repo = (found or {}).get("repo")
        if repo and not self.repo_edit.text().strip():
            self.repo_edit.setText(f"https://github.com/{repo}")
            self.status.setText("Ссылка найдена в файлах порта — проверьте, что это его проект, и сохраните.")
        elif not self.repo_edit.text().strip():
            self.status.setText("В файлах порта ссылки нет. Нажмите «Найти на GitHub» или вставьте её сами.")
        else:
            self.status.hide()

    def _warn(self, text):
        self.status.setText(text)
        self.status.show()

    def done(self, code):
        self._closed = True  # опоздавшие ответы GitHub больше не нужны
        super().done(code)

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка порта", self.folder)
        if not folder:
            return
        found = core.scan_port_folder(folder, hint=False)
        if not found:
            self._warn("В этой папке нет файлов .exe — выберите папку, где лежит сама игра.")
            return
        self.folder = found["folder"]
        self.folder_label.setText(self.folder)
        self.exe_box.clear()
        self.exe_box.addItems(found["exes"])
        self.status.hide()

    def _choose_exe(self):
        path, _ = QFileDialog.getOpenFileName(self, "Файл запуска игры", self.folder, "Программы (*.exe)")
        if not path:
            return
        try:
            rel = os.path.relpath(os.path.normpath(path), self.folder)
        except ValueError:  # другой диск
            rel = ".."
        if rel.startswith("..") or os.path.isabs(rel):
            self._warn("Файл запуска должен лежать в папке порта (или в её подпапке).")
            return
        if self.exe_box.findText(rel) < 0:
            self.exe_box.addItem(rel)
        self.exe_box.setCurrentText(rel)
        self.status.hide()

    # --- Поиск проекта на GitHub ---
    def _search_github(self):
        name = core.clean_name(self.name_edit.text())
        if not name:
            self._warn("Сначала впишите название игры — по нему и ищем.")
            return
        self.search_button.setEnabled(False)
        self.search_button.setText("Ищу…")
        worker = Worker(core.search_port_repos, name, parent=self.win)  # окно могут закрыть раньше ответа
        worker.finished.connect(self._on_found)
        worker.failed.connect(self._on_search_failed)
        worker.start()

    def _end_search(self):
        self.search_button.setEnabled(True)
        self.search_button.setText("Найти на GitHub")

    def _on_found(self, found):
        if self._closed:
            return
        self._end_search()
        if not found:
            self._warn("На GitHub ничего похожего не нашлось. Попробуйте название по-английски или вставьте "
                       "ссылку сами.")
            return
        self.status.hide()
        menu = RoundMenu(parent=self)
        for item in found:
            text = f"{item['repo']}  ★ {item['stars']}" + (f" — {item['description'][:70]}" if item["description"] else "")
            action = Action(FIF.GITHUB, text, menu)
            action.triggered.connect(lambda _=False, repo=item["repo"]: self.repo_edit.setText(f"https://github.com/{repo}"))
            menu.addAction(action)
        menu.exec(self.search_button.mapToGlobal(self.search_button.rect().bottomLeft()))

    def _on_search_failed(self, message):
        if self._closed:
            return
        self._end_search()
        self._warn("GitHub не ответил: " + message.split(": ", 1)[-1])

    # --- Сохранение ---
    def _save(self):
        if self._checking:
            return
        name = core.title_name(self.name_edit.text())  # двоеточие и знаки остаются: по ним точнее поиск картинок
        exe = self.exe_box.currentText()
        repo_text = self.repo_edit.text().strip() if self.github else ""
        repo = core.parse_github_repo(repo_text) if repo_text else ""
        if not name:
            self._warn("Впишите название игры.")
        elif not exe:
            self._warn("Выберите файл, которым запускается игра.")
        elif repo_text and not repo:
            self._warn("Это не похоже на ссылку на проект GitHub. Нужна ссылка вида github.com/автор/проект.")
        elif repo and repo.casefold() != self.initial_repo.casefold():
            self._check_repo(name, exe, repo)  # новая ссылка — сначала спросим GitHub, есть ли такой проект
        else:
            self.result_port = {"name": name, "folder": self.folder, "exe": exe, "repo": repo}
            self.accept()

    def _check_repo(self, name, exe, repo):
        self._checking = True
        self.yesButton.setEnabled(False)
        self.yesButton.setText("Проверяю ссылку…")
        self.status.hide()
        worker = Worker(core.fetch_port_about, repo, parent=self.win)
        worker.finished.connect(lambda about: self._on_repo_checked(name, exe, about))
        worker.failed.connect(lambda message: self._on_repo_failed(name, exe, repo, message))
        worker.start()

    def _end_check(self):
        self._checking = False
        self.yesButton.setEnabled(True)
        self.yesButton.setText("Сохранить")

    def _on_repo_checked(self, name, exe, about):
        if self._closed:
            return
        self._end_check()
        self.result_port = {"name": name, "folder": self.folder, "exe": exe, "repo": about["repo"], "about": about}
        self.accept()

    def _on_repo_failed(self, name, exe, repo, message):
        if self._closed:
            return
        self._end_check()
        text = message.split(": ", 1)[-1]
        if message.startswith("GitHubMissing"):
            self._warn(f"{text[:1].upper()}{text[1:]}. Ссылка не сохранена — исправьте её или сотрите.")
            return
        # GitHub не ответил или просит подождать — ссылку сохраняем, проверка обновлений покажет, если она неверна
        core.log.info("Ссылку на GitHub для «%s» не проверить сейчас (%s) — сохраняю как есть", name, text)
        self.result_port = {"name": name, "folder": self.folder, "exe": exe, "repo": repo}
        self.accept()


TROPHY_DARK = QColor(43, 52, 64)


def draw_trophy(painter, center, size, color):
    """Кубок высотой size точек с центром в center: чаша с ручками, ножка и подставка."""
    x, y, k = center.x(), center.y(), size / 16
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    cup = QPainterPath()
    cup.moveTo(x - 5 * k, y - 8 * k)
    cup.lineTo(x + 5 * k, y - 8 * k)
    cup.cubicTo(x + 5 * k, y - 1 * k, x + 2.5 * k, y + 1.5 * k, x, y + 1.5 * k)
    cup.cubicTo(x - 2.5 * k, y + 1.5 * k, x - 5 * k, y - 1 * k, x - 5 * k, y - 8 * k)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawPath(cup)
    painter.drawRect(QRectF(x - 1.1 * k, y + 1.4 * k, 2.2 * k, 3.4 * k))  # ножка
    painter.drawRoundedRect(QRectF(x - 4.5 * k, y + 4.6 * k, 9 * k, 2.6 * k), 0.8 * k, 0.8 * k)  # подставка
    painter.setPen(QPen(color, 1.5 * k))
    painter.setBrush(Qt.NoBrush)
    for side in (-1, 1):  # ручки
        painter.drawArc(QRectF(x + side * 5.2 * k - 2.6 * k, y - 7.2 * k, 5.2 * k, 5.6 * k),
                        (-90 if side > 0 else 90) * 16, 180 * 16)
    painter.restore()


def trophy_icon(color=None):
    """Значок кубка для кнопок. Без цвета — цвета текста темы: кнопка сама сделает его бледным, если выключена."""
    color = color or (WHITE if isDarkTheme() else QColor(30, 30, 30))
    icon = QIcon()
    for side in (16, 32, 48):
        pixmap = QPixmap(side, side)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        draw_trophy(painter, QPointF(side / 2, side / 2), side, color)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def completed_text(port):
    """«пройдена 07.11.2026» — для плашек и списка."""
    return "пройдена " + time.strftime("%d.%m.%Y", time.localtime(port["completed_at"]))


class PortTileDelegate(CoverTileDelegate):
    """Плитка порта — как у игр Steam, а если на GitHub вышла новая версия, в углу обложки кружок со стрелкой.
    Пройденная игра — с платиновым кубком в левом верхнем углу, как трофей в PlayStation."""

    UPDATE_ROLE = Qt.UserRole + 2
    COMPLETED_ROLE = Qt.UserRole + 12

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(self.COMPLETED_ROLE):
            self._paint_trophy(painter, option)
        if not index.data(self.UPDATE_ROLE):
            return
        rect = QRectF(option.rect)
        right = rect.x() + (rect.width() + self.COVER.width()) / 2
        center = QPointF(right - 16, rect.y() + self.PAD + 16)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(WHITE, 2))
        painter.setBrush(themeColor())
        painter.drawEllipse(center, 12, 12)
        painter.setPen(Qt.NoPen)
        painter.setBrush(WHITE)
        x, y = center.x(), center.y()
        painter.drawPolygon(QPolygonF([QPointF(x, y - 7), QPointF(x + 6, y), QPointF(x + 2.2, y),
                                       QPointF(x + 2.2, y + 6), QPointF(x - 2.2, y + 6), QPointF(x - 2.2, y),
                                       QPointF(x - 6, y)]))
        painter.restore()

    def _paint_trophy(self, painter, option):
        rect = QRectF(option.rect)
        left = rect.x() + (rect.width() - self.COVER.width()) / 2
        center = QPointF(left + 20, rect.y() + self.PAD + 20)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # Платина: светлый серебристо-голубой металл с бликом сверху слева
        metal = QLinearGradient(center.x() - 15, center.y() - 15, center.x() + 15, center.y() + 15)
        metal.setColorAt(0, QColor(246, 249, 252))
        metal.setColorAt(0.5, QColor(196, 208, 222))
        metal.setColorAt(1, QColor(132, 148, 168))
        painter.setPen(QPen(QColor(255, 255, 255, 230), 2))
        painter.setBrush(metal)
        painter.drawEllipse(center, 15, 15)
        painter.restore()
        draw_trophy(painter, center, 18, TROPHY_DARK)


def hltb_url(name):
    """Поиск игры на How Long To Beat — сколько её проходить."""
    return f"https://howlongtobeat.com/?q={quote_plus(name)}"


# Сайты, которые во встроенном браузере ломаются: How Long To Beat при прокрутке становится чёрным (из-за его
# рекламы — флаги отрисовки Chromium не помогают). Их программа всегда открывает во внешнем браузере.
EXTERNAL_ONLY_HOSTS = ("howlongtobeat.com",)


def external_only(url):
    host = QUrl(url).host().lower()
    return any(host == h or host.endswith("." + h) for h in EXTERNAL_ONLY_HOSTS)


def link_choice(win, url):
    """Где открыть ссылку: «inside» | «outside» — как выбрано в «Настройках», а если там «Спрашивать» —
    спрашиваем окном. None — окно закрыли без выбора."""
    choice = core.load_settings().get("link_open", "ask")
    if choice in ("inside", "outside"):
        return choice
    dialog = LinkDialog(win, url)
    core.log.info("Открыто окно «Открыть ссылку»: %s", url)
    dialog.exec()
    choice = dialog.choice
    core.log.info("Окно «Открыть ссылку» закрыто: %s%s", choice or "отмена",
                  ", больше не спрашивать" if choice and dialog.remember_box.isChecked() else "")
    if choice and dialog.remember_box.isChecked():
        core.save_settings(link_open=choice)
        win.settings_page.show_link_mode(choice)
    return choice


def pcgamingwiki_url(name):
    """Статья об игре на PCGamingWiki: как запустить старую игру на современной системе, патчи, широкий экран.
    Поиск по названию: если статья с таким названием есть, сайт сразу открывает её."""
    return f"https://www.pcgamingwiki.com/w/index.php?search={quote_plus(name)}&title=Special%3ASearch&go=Go"


class PortPage(QWidget):
    """Страница порта: шапка с фоном и логотипом (из SteamGridDB, по названию), кнопки, версии и что нового
    в последнем релизе на GitHub.

    У дисковой игры вместо GitHub — описание: из магазина Steam, если игру связали с ним («Найти в Steam»; там же
    оценки, отзывы и обзоры изданий), а если её в Steam нет — начало статьи из Википедии."""

    SIDE_MARGIN, CONTENT_WIDTH, TEXT_WIDTH = GamePage.SIDE_MARGIN, GamePage.CONTENT_WIDTH, GamePage.TEXT_WIDTH
    ABOUT_TTL = 7 * 24 * 3600  # описание проекта с GitHub — раз в неделю
    WIKI_TTL = 30 * 24 * 3600  # статья из Википедии — раз в месяц (и сразу, если игру переименовали)
    WIKI_VERSION = 3  # 3: «007» — не номер части; статьи, найденные (и не найденные) раньше, ищутся заново

    def __init__(self, ports_page):
        super().__init__()
        self.ports_page, self.win = ports_page, ports_page.win
        self.port = None
        self.origin = None  # коллекция, из которой открыли игру, — для кнопки «К коллекции»
        self._token = 0
        # Дисковая игра, связанная со Steam: карточка магазина (None — ещё не пришла, {} — у Steam её нет)
        # и отзывы игроков. Хранятся только пока открыта страница — у Steam свой кэш карточек
        self.store, self.store_reviews = None, None
        self._store_translating = self._wiki_loading = False
        self.press_links = []
        self.backdrop = None
        self.backdrop_opacity = 0.0
        self.backdrop_anim = QVariantAnimation(self)
        self.backdrop_anim.setDuration(900)
        self.backdrop_anim.setStartValue(0.0)
        self.backdrop_anim.setEndValue(1.0)
        self.backdrop_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.backdrop_anim.valueChanged.connect(self._set_backdrop_opacity)

        self.header = HeroHeader(self)
        self.header.artRequested.connect(lambda kind, step: self.port and step_art(self, self.port, kind, step))
        self.play_button = PrimaryPushButton("Играть", self, FIF.PLAY)
        self.play_button.clicked.connect(lambda: self.ports_page.launch(self.port))
        # Прошли игру и удалили с диска — она остаётся здесь с наигранным временем и заметками, но запускать
        # её нечем: вместо «Играть» — выключенная «Пройдена». Снова поставили — «Вернуть в игру»
        self.complete_button = PushButton("Отметить пройденной", self, trophy_icon())
        self.complete_button.setToolTip("Игра останется здесь — с наигранным временем и заметками, но из рулетки уйдёт. "
                                        "Удалить её с диска можно и потом: программа сама ничего не удаляет")
        self.complete_button.clicked.connect(lambda: self.ports_page.mark_completed(self.port))
        self.replay_button = PushButton("Вернуть в игру", self, FIF.SYNC)
        self.replay_button.setToolTip("Снова «Играть» — например, если вы поставили игру ещё раз")
        self.replay_button.clicked.connect(lambda: self.ports_page.unmark_completed(self.port))
        self.update_button = PrimaryPushButton("Скачать обновление", self, FIF.DOWNLOAD)
        self.update_button.setToolTip("Страница нового релиза на GitHub — в браузере. Скачиваете и ставите вы сами")
        self.update_button.clicked.connect(lambda: self.ports_page.open_release(self.port))
        self.updated_button = PushButton("Я обновил", self, FIF.ACCEPT)
        self.updated_button.setToolTip("Отметить, что у вас уже стоит последняя версия")
        self.updated_button.clicked.connect(lambda: self.ports_page.mark_updated(self.port))
        self.repo_button = PushButton("Проект на GitHub", self, FIF.GITHUB)
        self.repo_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(f"https://github.com/{self.port['repo']}")))
        self.folder_button = PushButton("Открыть папку", self, FIF.FOLDER)
        self.folder_button.clicked.connect(lambda: self.ports_page.open_folder(self.port))
        self.edit_button = PushButton("Изменить…", self, fluent_icon("EDIT", FIF.SETTING))
        self.edit_button.clicked.connect(lambda: self.ports_page.edit(self.port))
        self.cover_button = PushButton("Своя обложка", self, FIF.PHOTO)
        self.cover_button.clicked.connect(lambda: self.win.library_page.choose_cover(self.port))
        self.notes_button = PushButton("Свои заметки", self, fluent_icon("QUICK_NOTE", FIF.EDIT))
        self.notes_button.clicked.connect(lambda: self.port and open_notes(self.win, self.port, self.notes_button))
        self.spin_button = PushButton("Крутить ещё", self, FIF.SYNC)
        self.spin_button.clicked.connect(self._spin_again)
        # Как у игр Steam: только из рулетки, в своём разделе и в коллекциях игра остаётся
        self.roulette_button = PushButton("Убрать из рулетки", self, fluent_icon("REMOVE", FIF.DELETE))
        self.roulette_button.setToolTip("Игра больше не будет выпадать в рулетке, но останется здесь и в коллекциях — "
                                        "там её можно крутить")
        self.roulette_button.clicked.connect(lambda: self.win.home_page.remove_from_roulette(self.port))
        self.win.rouletteChanged.connect(self.refresh)
        self.steam_button = PushButton("Найти в Steam", self, FIF.SEARCH)
        self.steam_button.setToolTip("Найдите игру в магазине Steam — здесь появятся её описание, оценки и отзывы. "
                                     "Запускаться она будет по-прежнему своим файлом")
        self.steam_button.clicked.connect(lambda: self.ports_page.link_steam(self.port))
        back_button = TransparentPushButton(ports_page.T["back"], self, FIF.LEFT_ARROW)
        back_button.clicked.connect(self.ports_page.show_list)
        # Открыли из коллекции — можно вернуться и туда
        self.collection_button = TransparentPushButton("К коллекции", self, fluent_icon("ALBUM", FIF.FOLDER))
        self.collection_button.clicked.connect(self.win.back_to_collection)
        self.collection_button.hide()
        buttons_box = QWidget(self)
        self.buttons = CenteredFlowLayout(buttons_box, spacing=12)
        for button in (self.play_button, self.update_button, self.updated_button, self.notes_button, self.repo_button,
                       self.steam_button, self.folder_button, self.edit_button, self.cover_button, self.spin_button,
                       self.roulette_button, self.complete_button, self.replay_button, self.collection_button,
                       back_button):
            self.buttons.addWidget(button)

        self.chips_box = QWidget(self)
        self.chips = CenteredFlowLayout(self.chips_box, spacing=10)

        self.about_box = QWidget(self)
        about = QVBoxLayout(self.about_box)
        about.setContentsMargins(0, 0, 0, 0)
        about.setSpacing(8)
        self.about_title = SubtitleLabel("О проекте", self.about_box)
        self.description = plain_label("", self.about_box, selectable=True)
        self.description.setFont(resized_font(self.description.font(), 2))
        self.notes_title = SubtitleLabel("", self.about_box)
        self.notes = plain_label("", self.about_box, selectable=True)
        # Тексты с GitHub обычно по-английски — переводятся теми же средствами, что описания игр Steam
        self.translated_note = CaptionLabel("", self.about_box)
        self.original_button = TransparentPushButton("Показать оригинал", self.about_box,
                                                     fluent_icon("LANGUAGE", FIF.GLOBE))
        self.original_button.clicked.connect(self._toggle_original)
        translate_row = QHBoxLayout()
        translate_row.setContentsMargins(0, 0, 0, 0)
        translate_row.addWidget(self.translated_note)
        translate_row.addWidget(self.original_button)
        translate_row.addStretch(1)
        self.hint = plain_label("", self.about_box, caption=True)
        self.source_note = plain_label("", self.about_box, caption=True)  # откуда описание дисковой игры
        # Что пишут игроки — у дисковой игры, связанной со Steam, как на странице игры Steam
        self.players_title = SubtitleLabel("Что пишут игроки", self.about_box)
        self.players_title.hide()
        self.players_box = QVBoxLayout()
        self.players_box.setSpacing(6)
        for widget in (self.about_title, self.description, self.notes_title, self.notes):
            about.addWidget(widget)
        about.addLayout(translate_row)
        about.addWidget(self.source_note)
        about.addWidget(self.hint)
        about.addSpacing(12)
        about.addWidget(self.players_title)
        about.addLayout(self.players_box)
        # История версий — под спойлером; описания старых версий переводятся, когда спойлер раскрыли
        self.history_box = Spoiler("История версий", self.about_box)
        self.history_box.toggled.connect(lambda expanded: expanded and self._build_history())
        about.addSpacing(8)
        about.addWidget(self.history_box)
        self._show_original = False
        self._translating = set()  # ключи текстов, перевод которых уже просили за это открытие страницы
        self._pending = set()  # и которые переводятся прямо сейчас
        self._history_dirty = True

        column = QWidget(self)
        inner = QVBoxLayout(column)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(16)
        inner.addWidget(buttons_box)
        inner.addWidget(self.chips_box)
        inner.addSpacing(8)
        inner.addLayout(centered(self.about_box, self.TEXT_WIDTH))

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 24)
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

    def show_port(self, port, keep_origin=False):
        if not keep_origin:  # «Изменить…» и «Найти в Steam» перерисовывают ту же страницу — откуда открыли, не меняем
            self.origin = self.win.game_origin
        self._token += 1
        self.port = port
        self._translating = set()
        self._pending = set()
        self._history_dirty = True
        self.history_box.set_expanded(False)
        self.header.set_game(port["name"])
        self.backdrop = None
        self.backdrop_anim.stop()
        self.update()
        self.scroll.verticalScrollBar().setValue(0)
        key = core.griddb_key()
        for kind in core.ART_KINDS:
            self._run(core.get_artwork, (dict(port), kind, key), lambda path, k=kind: self._on_art(k, path))
        about = port.get("about") or {}
        if port.get("repo") and time.time() - (about.get("fetched") or 0) > self.ABOUT_TTL:
            self._run(core.fetch_port_about, (port["repo"],), self._on_about,
                      lambda message: core.log.info("Описание порта «%s» с GitHub не загрузилось: %s",
                                                    port["name"], message))
        self.store, self.store_reviews, self.press_links = None, None, []
        self._store_translating = self._wiki_loading = False
        clear_layout(self.players_box)
        self.players_title.hide()
        if not self.ports_page.GITHUB:
            if port.get("appid"):  # связана со Steam — описание, оценки и отзывы оттуда
                self._run(core.get_game_details, (port["appid"],), self._on_store_details,
                          lambda message: self._on_store_details(None, message))
                self._run(core.get_reviews, (port["appid"],), self._on_store_reviews,
                          lambda message: core.log.warning("Отзывы Steam не загрузились: %s", message))
            else:
                self._load_wiki()
        show_notes_count(self.notes_button, port)
        self.refresh()

    def _run(self, fn, args, on_done, on_fail=None):
        token = self._token
        worker = Worker(fn, *args, parent=self)
        worker.finished.connect(lambda result: token == self._token and on_done(result))
        if on_fail:
            worker.failed.connect(lambda message: token == self._token and on_fail(message))
        worker.start()

    def _on_art(self, kind, path):
        pixmap = QPixmap.fromImage(QImage(path)) if path else QPixmap()  # файл мог смениться под тем же именем
        if pixmap.isNull():
            return
        if kind == "hero":
            self.header.set_hero(pixmap)
            self._run(core.make_backdrop, (path,), self._on_backdrop)
        else:
            self.header.set_logo(pixmap)

    def _on_about(self, about):
        if self.port is None:
            return
        self.port["about"] = about
        self.ports_page.save()
        self.refresh()

    # --- Дисковая игра: описание из Steam или из Википедии ---
    def _on_store_details(self, details, error=None):
        """Карточка магазина Steam пришла. Нет её (сняли с продажи) или Steam не ответил — описание из Википедии."""
        if error:
            core.log.warning("Страница Steam для «%s» не загрузилась: %s", self.port["name"], error)
        self.store = details or {}
        if details:
            name = self.port["name"]
            self.press_links = [(title, f"https://www.google.com/search?q={quote_plus(f'site:{site} {name} обзор')}")
                                for title, site in REVIEW_SITES]
            if core.needs_translation(details) and core.translation_enabled():
                self._store_translating = True  # пока показываем оригинал, перевод подставим, когда придёт
                self._run(core.translate_details, (details,), self._on_store_translated,
                          lambda message: self._on_store_translated(None))
        else:
            self._load_wiki()
        self.refresh()

    def _on_store_translated(self, details):
        self._store_translating = False
        if details:
            self.store = dict(details, translated=True)
        self.refresh()

    def _on_store_reviews(self, reviews):
        self.store_reviews = reviews or None
        clear_layout(self.players_box)
        excerpts = (reviews or {}).get("excerpts") or []
        self.players_title.setVisible(bool(excerpts))
        for review in excerpts:
            verdict = "👍 Рекомендует" if review["up"] else "👎 Не рекомендует"
            meta = f"{verdict} · {review['hours']:g} ч в игре".replace(".", ",")
            if review.get("translated"):
                meta += " · переведено"
            self.players_box.addWidget(plain_label(meta, self.about_box, caption=True))
            self.players_box.addWidget(plain_label(f"«{review['text']}»", self.about_box))
            self.players_box.addSpacing(10)
        self.refresh()

    def _wiki(self):
        """Статья из Википедии для этого названия игры, если нашлась."""
        wiki = self.port.get("wiki") or {}
        fresh = wiki.get("v") == self.WIKI_VERSION and wiki.get("query") == self.port["name"]
        return wiki if fresh and wiki.get("found") else None

    def _load_wiki(self):
        """Описание из Википедии — раз в WIKI_TTL; переименовали игру — сразу заново, по новому названию."""
        wiki = self.port.get("wiki") or {}
        if (wiki.get("v") == self.WIKI_VERSION and wiki.get("query") == self.port["name"]
                and time.time() - (wiki.get("fetched") or 0) < self.WIKI_TTL):
            return
        name = self.port["name"]
        self._wiki_loading = True
        self._run(core.fetch_wiki_summary, (name,), lambda found: self._on_wiki(name, found),
                  lambda message: self._on_wiki_failed(name, message))

    def _on_wiki(self, name, found):
        self._wiki_loading = False
        self.port["wiki"] = dict(found or {}, query=name, found=bool(found), fetched=int(time.time()),
                                 v=self.WIKI_VERSION)
        self.ports_page.save()
        self.refresh()

    def _on_wiki_failed(self, name, message):
        self._wiki_loading = False
        core.log.info("Описание «%s» из Википедии не загрузилось: %s", name, message)
        self.refresh()

    def refresh(self):
        """Кнопки, плашки и тексты — по тому, что известно о порте сейчас."""
        port = self.port
        if port is None:
            return
        disc = not self.ports_page.GITHUB
        latest = port.get("latest") or {}
        completed = bool(port.get("completed_at"))
        update = core.port_has_update(port) and not completed  # пройденную обновлять незачем
        self.update_button.setVisible(update)
        self.updated_button.setVisible(update)
        self.play_button.setEnabled(not completed)
        self.play_button.setText("Пройдена" if completed else "Играть")
        self.play_button.setIcon(trophy_icon() if completed else FIF.PLAY)
        self.play_button.setToolTip(completed_text(port)[:1].upper() + completed_text(port)[1:]
                                    + ". Снова играть — «Вернуть в игру»" if completed else "")
        self.complete_button.setIcon(trophy_icon())  # цвет — под тему, её могли сменить
        self.complete_button.setVisible(not completed)
        self.replay_button.setVisible(completed)
        self.repo_button.setVisible(bool(port.get("repo")))
        self.steam_button.setVisible(disc and not port.get("appid"))
        origin = self.origin
        self.collection_button.setVisible(origin is not None)
        if origin is not None:
            self.collection_button.setText(f"К коллекции «{origin['name']}»")
        # «Крутить ещё» — когда эта игра сама крутится в рулетке
        self.spin_button.setVisible(any(g is port for g in self.win.roulette_games()))
        self.roulette_button.setVisible(not completed and not self.win.roulette.has(port))
        self.buttons.invalidate()

        # Дисковая игра: карточка Steam (если связана и Steam её отдал), иначе статья из Википедии
        store = (self.store or None) if disc and port.get("appid") else None
        wiki = self._wiki() if disc and not store else None
        loading = bool(disc and not store and not wiki and (
            (port.get("appid") and self.store is None) or self._wiki_loading))

        clear_layout(self.chips)
        chips = [InfoChip("Запуск", os.path.basename(port["exe"]), None, self.chips_box)]
        if self.win.play_tracker.playing(port):
            chips.append(InfoChip("Наиграно", "играете сейчас", None, self.chips_box))
        elif port.get("playtime"):
            played = core.playtime_text(port["playtime"])
            if port.get("last_played"):
                played += " · последний раз " + time.strftime("%d.%m.%Y", time.localtime(port["last_played"]))
            chips.append(InfoChip("Наиграно", played, None, self.chips_box))
        if completed:
            chips.append(InfoChip("Пройдена", time.strftime("%d.%m.%Y", time.localtime(port["completed_at"])), None,
                                  self.chips_box))
        if port.get("installed_tag"):
            chips.append(InfoChip("Версия у вас", port["installed_tag"], None, self.chips_box))
        if latest.get("tag"):
            value = latest["tag"] + (f" · {latest['published']}" if latest.get("published") else "")
            chips.append(InfoChip("Последняя на GitHub", value, latest.get("url"), self.chips_box))
        if store:
            release = (store.get("release_date") or "") + (" (ещё не вышла)" if store.get("coming_soon") else "")
            for caption, value in (("Жанры", ", ".join(store.get("genres") or [])), ("Дата выхода", release),
                                   ("Разработчик", ", ".join(store.get("developers") or []))):
                if value.strip():
                    chips.append(InfoChip(caption, value, None, self.chips_box))
            reviews = self.store_reviews or {}
            if reviews.get("total"):
                percent = round(100 * reviews["positive"] / reviews["total"])
                count = f"{reviews['total']:,}".replace(",", " ")
                value = f"{reviews['score']} · {percent}% из {count}" if reviews.get("score") else f"{percent}% из {count}"
                chips.append(InfoChip("Отзывы в Steam", value,
                                      f"https://store.steampowered.com/app/{port['appid']}/#app_reviews_hash",
                                      self.chips_box))
            if store.get("metacritic"):
                chips.append(InfoChip("Metacritic", f"{store['metacritic']} из 100", store.get("metacritic_url"),
                                      self.chips_box))
            chips.append(InfoChip("Магазин Steam", "Страница игры",
                                  f"https://store.steampowered.com/app/{port['appid']}/", self.chips_box))
            for title, url in self.press_links:
                chips.append(InfoChip("Обзор", title, url, self.chips_box))
        chips.append(InfoChip("How Long To Beat", "Сколько проходить", hltb_url(port["name"]), self.chips_box))
        if disc:  # как запустить старую игру на современной системе
            chips.append(InfoChip("PCGamingWiki", "Исправления и улучшения", pcgamingwiki_url(port["name"]),
                                  self.chips_box))
        if wiki:
            chips.append(InfoChip("Википедия", wiki.get("title") or "Статья", wiki.get("url"), self.chips_box))
        for chip in chips:
            chip.linkClicked.connect(self._open_link)
            self.chips.addWidget(chip)
        self.chips.invalidate()

        if disc:
            if store:
                description, notes = store.get("description") or "", store.get("about") or ""
            else:
                description, notes = (wiki or {}).get("text") or "", ""
            own = [] if store else [description]  # Википедию переводим сами; карточку Steam переводит core
            self.about_title.setText("Об игре")
            self.notes_title.setText("")
        else:
            description = (port.get("about") or {}).get("description", "")
            notes = latest.get("notes") or ""
            own = [description, notes]
            title = latest.get("title") or latest.get("tag") or ""
            self.about_title.setText("О проекте")
            self.notes_title.setText(f"Что нового в {title}" if title else "")
        shown = description if store else self._shown(description)
        self.about_title.setVisible(bool(description) or loading)
        self.description.setText(shown or ("Загружаю описание…" if loading else ""))
        self.description.setVisible(bool(description) or loading)
        self.notes_title.setVisible(bool(notes) and bool(self.notes_title.text()))
        self.notes.setText(notes if store else self._shown(notes))
        self.notes.setVisible(bool(notes))
        self._translate(own)
        self._update_translate_row(own)
        if store:  # перевод карточки Steam — как на странице игры Steam, без «Показать оригинал»
            note = "Перевожу описание на русский…" if self._store_translating else (
                "Описание переведено автоматически" if store.get("translated") else "")
            self.translated_note.setText(note)
            self.translated_note.setVisible(bool(note))
            self.original_button.hide()
        source = ""
        if store and description:
            source = "Описание, оценки и отзывы — из магазина Steam."
        elif wiki:
            source = f"Описание — из Википедии, статья «{wiki.get('title', '')}»."
        self.source_note.setText(source)
        self.source_note.setVisible(bool(source))

        history = self._history()
        self.history_box.setVisible(len(history) > 1)
        self.history_box.set_title(f"История версий ({len(history)})")
        self._history_dirty = True
        if self.history_box.expanded:
            self._build_history()
        if disc:  # обновлений с GitHub у дисковых игр нет — подсказка только о том, почему нет описания
            asked = (port.get("wiki") or {}).get("query") == port["name"]
            if description or loading:
                hint = ""
            elif not asked:
                hint = "Описание не загрузилось: Википедия не ответила. Откройте страницу игры чуть позже."
            elif port.get("appid"):
                hint = ("Магазин Steam не отдаёт страницу этой игры, а в Википедии статьи о ней не нашлось. "
                        "Связать с другой игрой Steam — правый клик по игре → «Найти в Steam».")
            else:
                hint = ("Описания нет: игра не связана с магазином Steam, а в Википедии статьи о ней не нашлось. "
                        "Есть в Steam — нажмите «Найти в Steam». Нашлось не то или ничего — поправьте название: "
                        "«Изменить…».")
        elif update:
            hint = (f"Вышла новая версия {latest['tag']} (у вас {port['installed_tag']}). Обновление скачиваете "
                    "и ставите вы сами — со страницы релиза. Когда обновите, нажмите «Я обновил» или просто "
                    "запустите игру: программа заметит новый файл запуска.")
        elif not port.get("repo"):
            hint = ("Ссылки на проект на GitHub нет — проверять обновления не по чему. Добавить: «Изменить…».")
        elif port.get("check_error"):
            hint = f"Обновления не проверились: {port['check_error']}"
        elif latest.get("tag"):
            hint = "У вас последняя версия."
        else:
            hint = "Обновления ещё не проверялись — это случится само, в течение нескольких минут."
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    def _open_link(self, url):
        # Релизы GitHub качают в обычном браузере, How Long To Beat ломается во встроенном; остальное
        # (PCGamingWiki) — как выбрано в «Настройках» → «Открывать ссылки»
        if url.startswith("https://github.com/") or external_only(url) or not self.win.web_page:
            QDesktopServices.openUrl(QUrl(url))
            return
        choice = link_choice(self.win, url)
        if choice == "outside":
            QDesktopServices.openUrl(QUrl(url))
        elif choice == "inside":
            self.win.open_web(url)

    def _spin_again(self):
        self.win.switchTo(self.win.home_page)
        self.win.home_page.spin_again()

    # --- Перевод ---
    # Переводы хранятся в порте ("translations": {ключ текста: перевод}): второй раз страница открывается
    # сразу на русском. Ключ — от самого текста, поэтому сменилось описание на GitHub — переводится заново.
    def _translation(self, text):
        return (self.port.get("translations") or {}).get(text_key(text)) if text and self.port else None

    def _shown(self, text):
        translated = None if self._show_original else self._translation(text)
        return translated or text

    def _translate(self, texts):
        """Перевести тексты, которых ещё нет на русском, — в фоне, по одному разу за открытие страницы."""
        if self.port is None or not core.translation_enabled():
            return
        wanted = []
        for text in texts:
            key = text_key(text) if text else None
            if key and key not in self._translating and core.needs_russian(text) and not self._translation(text):
                self._translating.add(key)
                wanted.append(text)
        if wanted:
            self._pending |= {text_key(t) for t in wanted}
            self._run(_translate_texts, (wanted,), self._on_translated)

    def _on_translated(self, result):
        self._pending -= set(result["tried"])
        if self.port is None:
            return
        if result["done"]:
            translations = dict(self.port.get("translations") or {})
            translations.update(result["done"])
            keep = {text_key(t) for t in self._port_texts() if t}
            self.port["translations"] = {k: v for k, v in translations.items() if k in keep}
            self.ports_page.save()
        self.refresh()

    def _port_texts(self):
        port = self.port
        return [(port.get("about") or {}).get("description", ""), (port.get("latest") or {}).get("notes", ""),
                (port.get("wiki") or {}).get("text", "")] + [item.get("notes", "") for item in port.get("history") or []]

    def _update_translate_row(self, texts):
        texts = list(texts) + ([item.get("notes", "") for item in self._history()] if self.history_box.expanded
                               else [])
        translated = any(self._translation(t) for t in texts if t)
        waiting = bool(self._pending & {text_key(t) for t in texts if t})
        if waiting and not translated:
            self.translated_note.setText("Перевожу описание на русский…")
        elif self._show_original:
            self.translated_note.setText("Оригинал с GitHub" if self.ports_page.GITHUB else "Оригинал из Википедии")
        else:
            self.translated_note.setText("Переведено автоматически" if translated else "")
        self.translated_note.setVisible(bool(self.translated_note.text()))
        self.original_button.setVisible(translated)
        self.original_button.setText("Показать перевод" if self._show_original else "Показать оригинал")

    def _toggle_original(self):
        self._show_original = not self._show_original
        self.refresh()

    # --- История версий ---
    def _history(self):
        if self.port is None:
            return []
        history = list(self.port.get("history") or [])
        latest = self.port.get("latest") or {}
        if latest.get("tag") and not any(item.get("tag") == latest["tag"] for item in history):
            history.insert(0, latest)
        return history

    HISTORY_TRANSLATE = 10  # переводим описания стольких последних версий — старые редко кому нужны

    def _build_history(self):
        if not self._history_dirty or self.port is None:
            return
        self._history_dirty = False
        body, layout = self.history_box.body, self.history_box.body_layout
        clear_layout(layout)
        history = self._history()
        installed = self.port.get("installed_tag")
        for item in history:
            title = item.get("title") or ""
            head = plain_label(item["tag"] + (f" — {title}" if title and title != item["tag"] else ""), body)
            head.setFont(resized_font(head.font(), 1))
            head_font = head.font()
            head_font.setBold(True)
            head.setFont(head_font)
            meta = [item["published"]] if item.get("published") else []
            if item["tag"] == installed:
                meta.append("у вас эта версия")
            if item.get("prerelease"):
                meta.append("предварительная")
            layout.addWidget(head)
            if meta:
                layout.addWidget(plain_label(" · ".join(meta), body, caption=True))
            layout.addWidget(plain_label(self._shown(item.get("notes") or "") or "Описания у этой версии нет.", body,
                                         selectable=True))
            layout.addSpacing(10)
        self._translate([item.get("notes") for item in history[:self.HISTORY_TRANSLATE]])
        self._update_translate_row([(self.port.get("about") or {}).get("description", ""),
                                    (self.port.get("latest") or {}).get("notes", "")])

    # --- Подложка, как у страницы игры ---
    def _on_backdrop(self, path):
        pixmap = QPixmap.fromImage(QImage(path)) if path else QPixmap()  # файл мог смениться под тем же именем
        if pixmap.isNull():
            return
        self.backdrop = pixmap
        self.backdrop_anim.stop()
        self.backdrop_anim.start()

    def _set_backdrop_opacity(self, value):
        self.backdrop_opacity = value
        self.update()

    def paintEvent(self, event):
        GamePage.paintEvent(self, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, lambda: self.header.fit_width(self.scroll.viewport().width()))


class HotkeyButton(PushButton):
    """Кнопка выбора клавиши снимка: нажали — «Нажмите клавишу…», следующая клавиша или сочетание с Ctrl, Alt,
    Shift и есть выбор. Esc — отмена. Код клавиши берётся у Windows (nativeVirtualKey) — его и регистрируем."""

    picked = Signal(dict)  # {"vk", "mods", "text"}
    rejected = Signal(str)
    MODIFIER_KEYS = (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr)

    def __init__(self, text="", parent=None):
        # Кнопки библиотеки, созданные с текстом и значком, заново зовут __init__ только с parent — поэтому
        # создаём кнопку с одним parent, а текст и значок ставим потом
        super().__init__(parent=parent)
        self.capturing = False
        self.swallow_release = False
        self.shown = text
        self.setText(text)
        self.setIcon(fluent_icon("KEYBOARD", FIF.EDIT))
        self.setToolTip("Нажмите и затем — клавишу или сочетание для снимка")
        self.setMinimumWidth(200)
        self.clicked.connect(self.start)

    def setText(self, text):
        if not getattr(self, "capturing", False):
            self.shown = text
        super().setText(text)

    def start(self):
        self.capturing = True
        super().setText("Нажмите клавишу… (Esc — отмена)")
        self.setFocus()
        self.grabKeyboard()

    def stop(self):
        if self.capturing:
            self.capturing = False
            self.releaseKeyboard()
            super().setText(self.shown)

    def focusOutEvent(self, event):
        self.stop()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if not self.capturing:
            return super().keyPressEvent(event)
        if event.key() in self.MODIFIER_KEYS or event.key() == Qt.Key_Print:
            return  # ждём основную клавишу; Print Screen Windows присылает только при отпускании
        if event.key() == Qt.Key_Escape and not event.modifiers():
            self.stop()
            return
        self._pick(event)

    def keyReleaseEvent(self, event):
        if self.capturing and event.key() == Qt.Key_Print:
            self._pick(event)
            return
        if self.swallow_release:  # отпустили выбранную клавишу (пробел, Enter) — это не новое нажатие кнопки
            self.swallow_release = False
            return
        super().keyReleaseEvent(event)

    def _pick(self, event):
        self.stop()
        self.swallow_release = event.type() == QEvent.KeyPress
        modifiers = event.modifiers()
        if modifiers & Qt.MetaModifier:
            self.rejected.emit("Сочетания с клавишей Win заняты самой Windows — возьмите Ctrl, Alt или Shift.")
            return
        mods, names = 0, []
        for flag, mod, name in ((Qt.ControlModifier, core.MOD_CONTROL, "Ctrl"), (Qt.AltModifier, core.MOD_ALT, "Alt"),
                                (Qt.ShiftModifier, core.MOD_SHIFT, "Shift")):
            if modifiers & flag:
                mods |= mod
                names.append(name)
        vk = int(event.nativeVirtualKey())
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            name = chr(vk)  # цифры и латинские буквы — по самой клавише, а не по знаку с Shift или раскладкой
        else:
            name = (core.HOTKEY_NAMES.get(vk) or QKeySequence(event.key()).toString(QKeySequence.NativeText)
                    or f"код {vk}")
        self.picked.emit({"vk": vk, "mods": mods, "text": "+".join(names + [name])})


class HotkeyFilter(QAbstractNativeEventFilter):
    """Ловит сообщение Windows «нажата клавиша снимка» (WM_HOTKEY) — и только его, остальные идут дальше как были."""

    WM_HOTKEY = 0x0312

    def __init__(self, hotkey_id, callback):
        super().__init__()
        self.hotkey_id, self.callback = hotkey_id, callback
        self.warned = False

    def nativeEventFilter(self, event_type, message):
        try:
            kind = event_type.data() if hasattr(event_type, "data") else event_type
            if kind in (b"windows_generic_MSG", "windows_generic_MSG"):
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == self.WM_HOTKEY and msg.wParam == self.hotkey_id:
                    QTimer.singleShot(0, self.callback)  # снимаем после — не внутри обработки сообщения Windows
                    return True, 0
        except Exception as e:  # повторилась бы на каждом сообщении Windows — в журнал только один раз
            if not self.warned:
                self.warned = True
                core.log.warning("Клавиша снимка: сообщение Windows не разобралось: %s", e)
        return False, 0


class Screenshots(QObject):
    """Снимки экрана в портах и дисковых играх. Клавиша зарегистрирована, только пока идёт игра, запущенная из
    программы (PlayTracker), и снимается только окно этой игры — если оно впереди. Снимок — PNG в папку
    screenshots/<игра> и в запись сессии в «Своих заметках» (одна запись на запуск игры). Поверх игры ничего
    не показывается: снимок получился — щелчок затвора, не получился — два низких «бип»."""

    HOTKEY_ID = 0x5B01

    def __init__(self, window, tracker):
        super().__init__(window)
        self.win, self.tracker = window, tracker
        self.registered = None  # (vk, mods) — какое сочетание сейчас за программой
        self.hwnd = None
        self.failed = ""  # сочетание, которое Windows не дала (занято другой программой)
        self.filter = None
        self.saving = 0
        self.effects = {}

    # --- Клавиша ---
    def sync(self):
        """Клавиша — за программой, только если снимки включены и идёт хотя бы одна игра."""
        settings = core.load_settings()
        hotkey = core.screenshot_hotkey(settings)
        want = (sys.platform == "win32" and bool(settings.get("screenshots_enabled", True))
                and bool(self.tracker.sessions))
        key = (hotkey["vk"], hotkey["mods"]) if want else None
        if key == self.registered and (key or not self.failed):
            return
        self._unregister()
        self.failed = ""
        if key:
            self._register(key, hotkey["text"])

    def _register(self, key, text):
        if self.filter is None:
            self.filter = HotkeyFilter(self.HOTKEY_ID, self.take)
            QApplication.instance().installNativeEventFilter(self.filter)
        self.hwnd = int(self.win.winId())
        if core.register_hotkey(self.hwnd, self.HOTKEY_ID, key[0], key[1]):
            self.registered = key
            for name in ("shot", "shot_fail"):  # звуки — заранее: первый щелчок прозвучит сразу
                self._effect(name)
            core.log.info("Снимки экрана: клавиша %s — за программой, пока идёт игра", text)
            return
        self.failed = text
        core.log.warning("Снимки экрана: %s занято другой программой", text)
        notify("warning", self.win, "Клавиша снимка занята",
               f"{text} уже забрала другая программа — снимков в этой игре не будет. Выберите другое сочетание: "
               "«Настройки» → «Снимки экрана».", 8000)

    def _unregister(self):
        if self.registered:
            core.unregister_hotkey(self.hwnd, self.HOTKEY_ID)
            core.log.info("Снимки экрана: клавиша отпущена — игр не идёт")
        self.registered = None

    def check_free(self, vk, mods):
        """Свободно ли сочетание (для настроек): занимаем его на миг и сразу отпускаем. Своё — свободно."""
        if self.registered == (vk, mods):
            return True
        hwnd = int(self.win.winId())
        if not core.register_hotkey(hwnd, self.HOTKEY_ID + 1, vk, mods):
            return False
        core.unregister_hotkey(hwnd, self.HOTKEY_ID + 1)
        return True

    # --- Снимок ---
    def _effect(self, name):
        effect = self.effects.get(name)
        if effect is None:
            path = core.generate_sounds().get(name)
            if not path:
                return None
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(path))
            effect.setVolume(0.8)
            self.effects[name] = effect
        return effect

    def play(self, name):
        if not core.load_settings().get("screenshot_sound", True):
            return
        effect = self._effect(name)
        if effect is not None:
            effect.play()

    def take(self):
        hwnd, program = core.foreground_window()
        session = next((s for s in self.tracker.sessions.values()
                        if program and core.folder_running(s["port"]["folder"], [program])), None)
        if session is None:
            self.play("shot_fail")
            core.log.info("Снимок не сделан: впереди не окно запущенной игры")
            return
        port = session["port"]
        if self.saving >= core.SCREENSHOT_SAVING_MAX:
            self.play("shot_fail")
            core.log.info("Снимок «%s» не сделан: прошлые ещё записываются на диск", port["name"])
            return
        try:
            width, height, data = core.capture_window(hwnd)
            digest = core.frame_digest(data)
            if digest == session.get("last_frame"):
                # Ровно тот же кадр, что в прошлый раз, — экран отдал застывшую картинку (игра в исключительном
                # полноэкранном режиме рисует мимо Windows). Пробуем снять само окно
                width, height, data = core.capture_window(hwnd, window_first=True)
                digest = core.frame_digest(data)
                if digest == session.get("last_frame"):
                    raise core.ScreenshotError(
                        "кадр не обновился — снимок получился бы таким же, как прошлый. Так бывает у игр в "
                        "исключительном полноэкранном режиме: Windows видит только старую картинку. Включите в "
                        "настройках игры оконный режим (в старых играх на движке Quake 3 — консоль, r_fullscreen 0)")
            path = core.new_screenshot_path(port)
        except (core.ScreenshotError, OSError) as e:
            self.play("shot_fail")
            core.log.warning("Снимок «%s» не получился: %s", port["name"], e)
            notify("warning", self.win, "Снимок не получился", f"«{port['name']}»: {e}.", 8000)
            return
        session["last_frame"] = digest
        self.play("shot")
        self.saving += 1
        worker = Worker(core.save_screenshot, path, width, height, data, parent=self)
        worker.finished.connect(lambda image: self._on_saved(session, port, image))
        worker.failed.connect(lambda message: self._on_failed(port, message))
        worker.start()

    def _on_failed(self, port, message):
        self.saving -= 1
        core.log.warning("Снимок «%s» не записался: %s", port["name"], message)
        notify("error", self.win, "Снимок не записался", f"«{port['name']}»: {message.split(': ', 1)[-1]}", 8000)

    def _on_saved(self, session, port, image):
        self.saving -= 1
        dialog = getattr(self.win, "notes_dialog", None)
        try:
            if dialog is not None and dialog.game is port:
                session["note_session"] = dialog.add_screenshot(session.get("note_session"), image)
            else:
                note = core.load_note(port)
                session["note_session"] = core.attach_screenshot(note, session.get("note_session"), image)
                core.save_note(port, note)
        except OSError as e:
            core.log.warning("Снимок «%s» сохранён, но в заметку не попал: %s", port["name"], e)
            notify("warning", self.win, "Снимок не попал в заметку",
                   f"Файл сохранён в папке screenshots, а заметка «{port['name']}» не записалась: {e}", 8000)
            return
        page = getattr(session.get("page"), "page", None)  # страница игры на вкладке портов или дисковых
        if page is not None and getattr(page, "port", None) is port and hasattr(page, "notes_button"):
            show_notes_count(page.notes_button, port)  # на открытой странице игры — новое число записей


class PlayTracker(QObject):
    """Время в портах и дисковых играх: после запуска программа раз в несколько секунд смотрит, работает ли
    что-нибудь из папки игры. Так считается и игра, которую запустил её лаунчер (Call of Duty: Launcher.exe →
    CoDSP.exe), и запущенная с правами администратора. Закрылась — время сессии прибавляется к игре.

    Программа только читает список запущенных программ Windows — ничего в них не трогает."""

    POLL_MS = 5000
    START_WAIT = 120  # с: столько ждём, пока игра появится (окно прав администратора, долгий лаунчер)
    GAP = 20  # с: из папки игры ничего не видно дольше — сессия закончилась (лаунчер передал запуск не мгновенно)
    MIN_SESSION = 10  # с: короче — это не игра, а, например, лаунчер, который сразу закрыли

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.sessions = {}  # id игры → {"port", "page", "launched", "started", "last_seen", "seen"}
        self._polling = False
        self.timer = QTimer(self)
        self.timer.setInterval(self.POLL_MS)
        self.timer.timeout.connect(self._poll)
        self.shots = Screenshots(window, self)  # снимки экрана — пока идёт игра

    def playing(self, port):
        session = self.sessions.get(port["id"])
        return bool(session and session["seen"])

    def start(self, port, page):
        if port["id"] in self.sessions:
            return  # эту игру уже считаем: второй запуск — то же время
        now = time.time()
        self.sessions[port["id"]] = {"port": port, "page": page, "launched": now, "started": now,
                                     "last_seen": now, "seen": False}
        if not self.timer.isActive():
            self.timer.start()
        self.shots.sync()
        QTimer.singleShot(2000, self._poll)

    def _poll(self):
        if self._polling or not self.sessions:
            return
        self._polling = True
        worker = Worker(core.running_programs, parent=self)
        worker.finished.connect(self._on_programs)
        worker.failed.connect(self._on_failed)
        worker.start()

    def _on_failed(self, message):
        self._polling = False
        core.log.warning("Время в игре: список запущенных программ не прочитался: %s", message)

    def _on_programs(self, programs):
        self._polling = False
        now, changed = time.time(), False
        for port_id, session in list(self.sessions.items()):
            if core.folder_running(session["port"]["folder"], programs):
                if not session["seen"]:
                    session["seen"], session["started"] = True, now
                    changed = True  # на плитке и странице — «играете сейчас»
                    core.log.info("Время в «%s»: игра запущена — считаю", session["port"]["name"])
                session["last_seen"] = now
            elif session["seen"] and now - session["last_seen"] > self.GAP:
                self._finish(port_id)
            elif not session["seen"] and now - session["launched"] > self.START_WAIT:
                del self.sessions[port_id]
                core.log.info("Время в «%s» не считается: игра так и не появилась среди запущенных программ "
                              "(возможно, её лаунчер запускает программу из другой папки)", session["port"]["name"])
        if not self.sessions:
            self.timer.stop()
            self.shots.sync()  # игр не идёт — клавиша снимка снова свободна для других программ
        if changed:
            self.win.portsChanged.emit()

    def _finish(self, port_id, closing=False):
        session = self.sessions.pop(port_id)
        port = session["port"]
        seconds = session["last_seen"] - session["started"]
        if seconds < self.MIN_SESSION:
            core.log.info("Время в «%s»: игра закрылась через %d с — слишком коротко, не засчитано",
                          port["name"], int(seconds))
            if not closing:
                self.win.portsChanged.emit()
            return
        port["playtime"] = int((port.get("playtime") or 0) + seconds)
        port["last_played"] = int(session["started"])
        port["sessions"] = int(port.get("sessions") or 0) + 1
        session["page"].save()
        core.log.info("Время в «%s»: сессия %s, всего %s", port["name"], core.playtime_text(seconds),
                      core.playtime_text(port["playtime"]))
        if not closing:
            self.win.portsChanged.emit()
            notify("info", self.win, "Игра закрыта", f"«{port['name']}»: сыграно {core.playtime_text(seconds)}. "
                                                     f"Всего — {core.playtime_text(port['playtime'])}.", 5000)

    def flush(self):
        """Программу закрывают, а игра ещё идёт — время до этой минуты всё равно засчитывается."""
        now = time.time()
        for port_id, session in list(self.sessions.items()):
            if session["seen"]:
                session["last_seen"] = now
                self._finish(port_id, closing=True)
        self.sessions.clear()
        self.timer.stop()
        self.shots.sync()


class NativePortsPage(QWidget):
    """Вкладка «Игры Native Port's»: порты и другие игры, которые уже стоят на ПК и запускаются своим .exe.

    Добавить — папкой, где у каждого порта своя папка, или по одному. Плитка с обложками, правый клик, двойной
    клик — запуск. Страница порта — по клику «Открыть страницу». Обновления проверяются на GitHub сами.
    Вкладка «Дисковые игры» — та же самая, только со своим списком, своей папкой и без GitHub (DiscGamesPage).
    """

    OBJECT_NAME = "nativePortsPage"
    LIBRARY = "ports"  # список на главном окне: win.ports
    ROOT_SETTING = "ports_root"
    GITHUB = True
    T = {
        "title": "Игры Native Port's",
        "empty_title": "Порты и игры, которые уже стоят на ПК",
        "empty_text": "Фанатские порты (Ship of Harkinian, OpenMW и другие) и любые игры, которые запускаются своим "
                      ".exe. Укажите папку, в которой у каждой игры своя папка, — программа найдёт файлы запуска. "
                      "Обложки, фоны и логотипы — из SteamGridDB, а по ссылке на проект на GitHub программа "
                      "сообщит о новой версии.",
        "root_button": "Добавить папку с портами",
        "add_button": "Добавить порт",
        "add_title": "Добавить порт",
        "one_folder": "Папка порта — где лежит сама игра",
        "count": "Портов: {n}",
        "none": "Портов пока нет",
        "scan_busy": "Ищу порты…",
        "scan_title": "Найдены порты",
        "nothing_new": "Новых портов не нашлось",
        "all_known": "Все порты из этой папки уже в списке ({n}).",
        "add_busy": "Добавляю порты…",
        "added": "Добавлено портов: {n}",
        "not_added": "Порты не добавлены",
        "already": "Этот порт уже в списке",
        "no_folder": "Папки порта нет",
        "no_folder_text": "Порт перенесли или удалили. Укажите новую: «Изменить…».",
        "root_missing": "Папки с портами нет",
        "root_missing_text": "Выберите её заново: «Добавить папку с портами».",
        "saved": "Порт сохранён",
        "removed": "Убрано портов: {n}",
        "remove_confirm": "{names} — портов: {n}. Сами файлы на диске останутся, программа их не трогает.",
        "remove_tip": "Сами файлы порта на диске останутся",
        "edit_tip": "Название, папка, файл запуска и ссылка на GitHub",
        "exe_missing": "«{name}»: {exe} не найден — порт перенесли или удалили. Укажите новое место: «Изменить…».",
        "exe_changed": "«{name}»: {exe} — уже не тот файл, что был при добавлении порта. Если вы обновили порт — "
                       "всё в порядке. Если нет — файл мог заменить кто-то другой, и запускать его не стоит."
                       "\n\nЗапустить?",
        "not_launched": "Порт не запустился",
        "back": "К портам",
    }

    def __init__(self, window):
        super().__init__()
        self.setObjectName(self.OBJECT_NAME)
        self.win = window
        self._checking = False
        T = self.T

        # Список
        self.list_view = QWidget(self)
        self.title = TitleLabel(T["title"], self.list_view)
        self.count_label = CaptionLabel("", self.list_view)
        self.search = SearchLineEdit(self.list_view)
        self.search.setPlaceholderText("Поиск по названию")
        self.search.textChanged.connect(self.refresh)
        self.thumbs = CoverThumbs(CoverTileDelegate.COVER, self)
        self.grid = QListWidget(self.list_view)
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setFrameShape(QFrame.NoFrame)
        self.grid.setMouseTracking(True)
        self.grid.viewport().setMouseTracking(True)
        self.grid.viewport().setAttribute(Qt.WA_Hover)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(30)
        self.grid.setItemDelegate(PortTileDelegate(self.thumbs, self.grid))
        self.grid.setStyleSheet("QListWidget {background: transparent; border: none; outline: none}")
        if SmoothScrollDelegate:
            self._grid_scroll = SmoothScrollDelegate(self.grid)
        self.thumbs.updated = self.grid.viewport().update
        # Как на вкладке «Игры Steam»: списком или плиткой, выбор запоминается
        self.list = ListWidget(self.list_view)
        for view in (self.list, self.grid):
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self.show_menu(v, pos))
            # Двойной щелчок — страница игры (как на «Играх Steam» и в коллекциях); запуск — с неё или из меню
            view.itemDoubleClicked.connect(lambda item: self.open_from_list(self.port_of(item)))
        self.views = QStackedWidget(self.list_view)
        self.views.addWidget(self.list)
        self.views.addWidget(self.grid)
        self.list_button = TransparentToggleToolButton(FIF.MENU, self.list_view)
        self.list_button.setToolTip("Списком")
        self.list_button.clicked.connect(lambda: self.set_view("list"))
        self.grid_button = TransparentToggleToolButton(FIF.PHOTO, self.list_view)
        self.grid_button.setToolTip("Плиткой с обложками")
        self.grid_button.clicked.connect(lambda: self.set_view("grid"))
        self.sort_box = make_sort_box(self.list_view, self.SORTS, f"{self.LIBRARY}_sort", self.refresh)
        self.search_box = QWidget(self.list_view)
        search_row = QHBoxLayout(self.search_box)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self.search, 1)
        search_row.addSpacing(6)
        search_row.addWidget(self.sort_box)
        search_row.addSpacing(6)
        search_row.addWidget(self.list_button)
        search_row.addWidget(self.grid_button)

        # Пусто — объясняем, что это за раздел
        self.empty_box = QWidget(self.list_view)
        empty = QVBoxLayout(self.empty_box)
        empty.addStretch(1)
        empty_title = SubtitleLabel(T["empty_title"], self.empty_box)
        empty_text = plain_label(T["empty_text"], self.empty_box)
        empty_text.setAlignment(Qt.AlignCenter)
        # С переносом строк и выравниванием по центру Qt даёт надписи высоту по своей прикидке, и конец текста
        # обрезался. Поэтому ширину и высоту считаем сами — под окно (_fit_empty_text)
        self.empty_text = empty_text
        empty.addWidget(empty_title, 0, Qt.AlignHCenter)
        empty.addWidget(empty_text, 0, Qt.AlignHCenter)
        empty.addStretch(1)

        self.root_button = PrimaryPushButton(T["root_button"], self.list_view,
                                             fluent_icon("FOLDER_ADD", FIF.FOLDER))
        self.root_button.setToolTip("Папка, в которой у каждой игры своя папка. Ничего не запускается — "
                                    "программа только смотрит, какие .exe там лежат")
        self.root_button.clicked.connect(self.add_root)
        self.rescan_button = PushButton("Найти новые", self.list_view, FIF.SYNC)
        self.rescan_button.clicked.connect(self.find_new)
        self.add_button = PushButton(T["add_button"], self.list_view, FIF.ADD)
        self.add_button.clicked.connect(self.add_one)
        self.updates_button = PushButton("Проверить обновления", self.list_view, FIF.UPDATE)
        self.updates_button.setToolTip("Спросить GitHub о новых версиях всех портов со ссылкой на проект")
        self.updates_button.clicked.connect(lambda: self.check_updates(force=True))
        self.delete_button = PushButton("Убрать выбранные", self.list_view, FIF.DELETE)
        self.delete_button.setToolTip("Убрать из списка — " + T["remove_tip"][:1].lower() + T["remove_tip"][1:])
        self.delete_button.clicked.connect(self.remove_selected)
        # Кнопки — по левому краю; в узком окне (половина экрана) переносятся на вторую строку, а не сжимаются
        buttons_box = QWidget(self.list_view)
        buttons = CenteredFlowLayout(buttons_box, spacing=12, center=False)
        for button in (self.root_button, self.rescan_button, self.add_button, self.updates_button,
                       self.delete_button):
            buttons.addWidget(button)
        self.buttons = buttons

        list_layout = QVBoxLayout(self.list_view)
        list_layout.setContentsMargins(36, 20, 36, 28)
        list_layout.setSpacing(12)
        list_layout.addWidget(self.title)
        list_layout.addWidget(self.count_label)
        list_layout.addWidget(self.search_box)
        list_layout.addWidget(self.views, 1)
        list_layout.addWidget(self.empty_box, 1)
        list_layout.addWidget(buttons_box)

        # Страница порта
        self.page = PortPage(self)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.mini = MiniPlayerSpot(self, self.title)
        # Выделили игры — за списком их обложки под матовым стеклом (у страницы игры своя подложка)
        self.backdrop = SelectionBackdrop(self.list_view)
        for view in (self.list, self.grid):
            view.itemSelectionChanged.connect(self._on_selection)

        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.setContext(Qt.WidgetWithChildrenShortcut)
        escape.activated.connect(self._on_escape)
        self.win.portsChanged.connect(self.refresh)
        self.win.covers.coverReady.connect(self.cover_changed)
        view = core.load_settings().get(f"{self.LIBRARY}_view", "grid")
        self.set_view(view if view in ("list", "grid") else "grid", save=False)
        self.refresh()

    SORTS = ("name", "name_desc", "new", "old", "played", "recent")

    def _on_selection(self):
        self.backdrop.set_names([item.data(Qt.UserRole) for item in self.current_view().selectedItems()])

    def set_view(self, view, save=True):
        self.views.setCurrentWidget(self.grid if view == "grid" else self.list)
        self.list_button.setChecked(view == "list")
        self.grid_button.setChecked(view == "grid")
        self._on_selection()
        if save:
            core.save_settings(**{f"{self.LIBRARY}_view": view})
            core.log.info("Вкладка «%s»: вид — %s", self.T["title"], "плиткой" if view == "grid" else "списком")

    def current_view(self):
        return self.views.currentWidget()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.mini.place()
        self._fit_empty_text()

    def _fit_empty_text(self):
        """Пояснение на пустой вкладке: до 560 точек в ширину (уже — если окно узкое) и ровно той высоты,
        сколько строк получилось, — чтобы конец текста не обрезался ни на весь экран, ни на половину."""
        label = self.empty_text
        width = max(240, min(560, self.width() - 2 * 36 - 24))
        rect = QFontMetrics(label.font()).boundingRect(QRect(0, 0, width, 10000),
                                                       int(Qt.AlignCenter | Qt.TextWordWrap), label.text())
        label.setFixedSize(width, rect.height() + 6)

    # --- Список ---
    @property
    def lib(self):
        """Свой список: порты или дисковые игры (core.PortLibrary)."""
        return getattr(self.win, self.LIBRARY)

    def save(self):
        return self.win.save_ports(self.lib)

    def refresh(self):
        ports = self.lib.ports
        query = self.search.text().casefold().strip()
        selected = {item.data(Qt.UserRole + 1) for item in self.current_view().selectedItems()}
        self.list.clear()
        self.grid.clear()
        tile = CoverTileDelegate.COVER
        tracker = self.win.play_tracker
        for port in sort_games(ports, self.sort_box.mode()):
            if query and query not in port["name"].casefold():
                continue
            update = core.port_has_update(port) and not port.get("completed_at")
            details = []
            if tracker.playing(port):
                details.append("играете сейчас")
            elif port.get("playtime"):
                details.append("наиграно " + core.playtime_text(port["playtime"]))
            if port.get("completed_at"):
                details.append(completed_text(port))
            if update:
                details.append(f"новая версия {port['latest']['tag']}")
            for view in (self.list, self.grid):
                text = port["name"] + (f"   ·   {', '.join(details)}" if details else "")
                item = QListWidgetItem(text if view is self.list else "")
                item.setData(Qt.UserRole, port["name"])
                item.setData(Qt.UserRole + 1, port["id"])
                item.setToolTip(port["name"] + "".join(f"\n{d[:1].upper()}{d[1:]}" for d in details))
                if view is self.grid:
                    item.setData(PortTileDelegate.UPDATE_ROLE, update)
                    item.setData(PortTileDelegate.COMPLETED_ROLE, bool(port.get("completed_at")))
                    item.setSizeHint(QSize(tile.width() + 2 * CoverTileDelegate.PAD,
                                           tile.height() + CoverTileDelegate.TEXT_H + 2 * CoverTileDelegate.PAD))
                view.addItem(item)
                item.setSelected(port["id"] in selected)
        updates = sum(core.port_has_update(p) and not p.get("completed_at") for p in ports)
        completed = sum(bool(p.get("completed_at")) for p in ports)
        text = self.T["count"].format(n=len(ports)) if ports else self.T["none"]
        total = sum(p.get("playtime") or 0 for p in ports)
        if total >= 60:
            text += f" · наиграно всего: {core.playtime_text(total)}"
        if completed:
            text += f" · пройдено: {completed}"
        if updates:
            text += f" · с новой версией на GitHub: {updates} — отмечены стрелкой"
        self.count_label.setText(text)
        self.views.setVisible(bool(ports))
        self.search_box.setVisible(bool(ports))
        self.empty_box.setVisible(not ports)
        self.delete_button.setVisible(bool(ports))
        self.updates_button.setVisible(self.GITHUB and any(p.get("repo") for p in ports))
        self.rescan_button.setVisible(bool(core.load_settings().get(self.ROOT_SETTING)))
        self.buttons.invalidate()
        if self.page.port is not None:
            if any(p is self.page.port for p in ports):
                self.page.refresh()
            elif self.stack.currentWidget() is self.page:
                self.show_list()  # игру убрали из списка

    def port_of(self, item):
        return self.lib.find(item.data(Qt.UserRole + 1)) if item else None

    def cover_changed(self, name):
        self.thumbs.forget(name)
        self.grid.viewport().update()
        self.backdrop.refresh(name)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.mini.place)
        self._fit_empty_text()
        ratio = self.devicePixelRatioF()
        if ratio != self.thumbs.ratio:
            self.thumbs.ratio = ratio
            self.thumbs.clear()

    def _on_escape(self):
        if self.stack.currentWidget() is self.page:
            self.show_list()
        elif self.search.hasFocus() and self.search.text():
            self.search.clear()
        else:
            for view in (self.list, self.grid):
                view.clearSelection()

    def show_menu(self, view, pos):
        item = view.itemAt(pos)
        port = self.port_of(item)
        if not port:
            return
        if not item.isSelected():
            view.clearSelection()
            item.setSelected(True)
        menu = RoundMenu(parent=self)
        completed = bool(port.get("completed_at"))
        play = Action(FIF.PLAY, "Играть", menu)
        play.triggered.connect(lambda: self.launch(port))
        page = Action(FIF.GAME, "Открыть страницу", menu)
        page.triggered.connect(lambda: self.open_from_list(port))
        folder = Action(FIF.FOLDER, "Открыть папку", menu)
        folder.triggered.connect(lambda: self.open_folder(port))
        menu.addActions([page, folder] if completed else [play, page, folder])
        if core.port_has_update(port) and not completed:
            release = Action(FIF.DOWNLOAD, f"Скачать обновление {port['latest']['tag']}", menu)
            release.triggered.connect(lambda: self.open_release(port))
            done = Action(FIF.ACCEPT, "Я обновил", menu)
            done.triggered.connect(lambda: self.mark_updated(port))
            menu.addActions([release, done])
        if completed:
            replay = Action(FIF.SYNC, "Вернуть в игру", menu)
            replay.triggered.connect(lambda: self.unmark_completed(port))
            menu.addAction(replay)
        else:
            complete = Action(trophy_icon(), "Отметить пройденной", menu)
            complete.triggered.connect(lambda: self.mark_completed(port))
            menu.addAction(complete)
        menu.addSeparator()
        edit = Action(fluent_icon("EDIT", FIF.SETTING), "Изменить…", menu)
        edit.setToolTip(self.T["edit_tip"])
        edit.triggered.connect(lambda: self.edit(port))
        own_cover = Action(FIF.PHOTO, "Своя обложка…", menu)
        own_cover.triggered.connect(lambda: self.win.library_page.choose_cover(port))
        menu.addActions([edit, own_cover])
        if not self.GITHUB:  # дисковая игра: описание, оценки и отзывы — из её страницы в Steam
            link = Action(FIF.SEARCH, "Связать с другой игрой Steam…" if port.get("appid") else "Найти в Steam…", menu)
            link.triggered.connect(lambda: self.link_steam(port))
            menu.addAction(link)
            if port.get("appid"):
                unlink = Action(FIF.CLOSE, "Отвязать от Steam", menu)
                unlink.setToolTip("Описание будет из Википедии")
                unlink.triggered.connect(lambda: self.unlink_steam(port))
                menu.addAction(unlink)
        if core.griddb_key():
            grid_cover = Action(FIF.DOWNLOAD, "Обложка из SteamGridDB", menu)
            grid_cover.triggered.connect(lambda: self.win.library_page.cover_from_griddb(port))
            menu.addAction(grid_cover)
        menu.addSeparator()
        add_collection_menu(self.win, menu, [self.port_of(i) for i in view.selectedItems()])
        menu.addSeparator()
        count = len(view.selectedItems())
        remove = Action(FIF.DELETE, "Убрать из списка" if count == 1 else f"Убрать выбранные ({count})", menu)
        remove.setToolTip(self.T["remove_tip"])
        remove.triggered.connect(self.remove_selected)
        menu.addAction(remove)
        menu.exec(view.viewport().mapToGlobal(pos))

    def open_from_list(self, port):
        """Страница игры, открытая из своего списка, — «назад» ведёт в этот список, а не в коллекцию."""
        if port:
            self.win.game_origin = None
            self.open_port(port)

    def open_port(self, port):
        self.page.show_port(port)
        self.stack.setCurrentWidget(self.page)

    def show_list(self):
        self.stack.setCurrentWidget(self.list_view)
        if self.page.port is not None:
            view = self.current_view()
            for row in range(view.count()):
                item = view.item(row)
                if item.data(Qt.UserRole + 1) == self.page.port["id"]:
                    view.clearSelection()
                    item.setSelected(True)
                    view.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                    break

    def open_folder(self, port):
        if port and os.path.isdir(port["folder"]):
            QDesktopServices.openUrl(QUrl.fromLocalFile(port["folder"]))
        elif port:
            notify("warning", self.win, self.T["no_folder"], self.T["no_folder_text"])

    # --- Добавление ---
    def add_root(self):
        start = core.load_settings().get(self.ROOT_SETTING) or ""
        root = QFileDialog.getExistingDirectory(self, "Папка, в которой у каждой игры своя папка", start)
        if not root:
            return
        core.save_settings(**{self.ROOT_SETTING: os.path.normpath(root)})
        self._scan_root(root)

    def find_new(self):
        root = core.load_settings().get(self.ROOT_SETTING)
        if not root or not os.path.isdir(root):
            notify("warning", self.win, self.T["root_missing"], self.T["root_missing_text"])
            return
        self._scan_root(root)

    def _scan_root(self, root):
        show_busy(self.win, self.T["scan_busy"], "Смотрю, какие файлы .exe лежат в папках игр. Ничего не запускаю.")
        worker = Worker(core.scan_ports_root, root, self.lib.folders(), self.GITHUB, parent=self)
        worker.finished.connect(self._on_scanned)
        worker.failed.connect(lambda message: (hide_busy(self.win), notify(
            "error", self.win, "Папку не удалось просмотреть", message.split(": ", 1)[-1])))
        worker.start()

    def _on_scanned(self, scan):
        hide_busy(self.win)
        if not scan["found"]:
            text = (self.T["all_known"].format(n=scan["known"]) if scan["known"] else
                    "В папках нет файлов .exe. Нужна папка, в которой у каждой игры своя папка.")
            notify("info", self.win, self.T["nothing_new"], text, 6000)
            return
        dialog = PortScanDialog(self.win, scan, self.T["scan_title"], github=self.GITHUB)
        core.log.info("Открыто окно «%s»: %d", self.T["scan_title"], len(scan["found"]))
        accepted = dialog.exec()
        items = dialog.selected()
        dialog.deleteLater()
        if accepted and items:
            self._add_ports(items)

    def add_one(self):
        folder = QFileDialog.getExistingDirectory(self, self.T["one_folder"],
                                                  core.load_settings().get(self.ROOT_SETTING) or "")
        if not folder:
            return
        if any(core.same_path(folder, f) for f in self.lib.folders()):
            notify("info", self.win, self.T["already"], os.path.basename(folder))
            return
        # В фоне: поиск ссылки на GitHub читает и сами .exe — большие файлы это пара секунд
        show_busy(self.win, "Смотрю папку игры…", "Ищу файлы запуска" + (" и ссылку на проект на GitHub"
                                                                          if self.GITHUB else "") + ". Ничего не запускаю.")
        worker = Worker(core.scan_port_folder, folder, self.GITHUB, parent=self)
        worker.finished.connect(self._on_one_scanned)
        worker.failed.connect(lambda message: (hide_busy(self.win), notify(
            "error", self.win, "Папку не удалось просмотреть", message.split(": ", 1)[-1])))
        worker.start()

    def _on_one_scanned(self, found):
        hide_busy(self.win)
        if not found:
            notify("warning", self.win, "В папке нет файлов .exe",
                   "Выберите папку, где лежит сама игра (или её подпапку bin).", 6000)
            return
        dialog = PortEditDialog(self.win, found, self.T["add_title"], github=self.GITHUB)
        accepted = dialog.exec()
        result = dialog.result_port
        dialog.deleteLater()
        if accepted and result:
            self._add_ports([result])

    def _add_ports(self, items):
        """Отпечатки файлов запуска — в фоне (большие .exe считаются пару секунд), потом в список."""
        show_busy(self.win, self.T["add_busy"], "Запоминаю контрольные суммы файлов запуска: по ним программа "
                                                "заметит, если файл поменяется.")
        worker = Worker(_port_fingerprints, items, parent=self)
        worker.finished.connect(lambda prints: self._on_fingerprints(items, prints))
        worker.failed.connect(lambda message: (hide_busy(self.win), notify("error", self.win,
                                                                            self.T["not_added"], message)))
        worker.start()

    def _on_fingerprints(self, items, prints):
        hide_busy(self.win)
        added, failed = [], []
        for item, fingerprint in zip(items, prints):
            if fingerprint.get("error"):
                failed.append(f"«{item['name']}»: {fingerprint['error']}")
                continue
            added.append(self.lib.add(item["name"], item["folder"], item["exe"], item.get("repo") or "",
                                      fingerprint))
        if added and not self.save():
            return
        for port in added:
            core.log.info("%s: добавлена «%s», запуск %s%s", self.T["title"], port["name"],
                          os.path.basename(port["exe"]), f", GitHub {port['repo']}" if port["repo"] else "")
        self.win.portsChanged.emit()
        if failed:
            notify("warning", self.win, f"Не добавлено: {len(failed)}", "\n".join(failed[:4]), 8000)
        if added:
            names = ", ".join(f"«{p['name']}»" for p in added[:4]) + ("…" if len(added) > 4 else "")
            notify("success", self.win, self.T["added"].format(n=len(added)), names + ". Обложки скачиваются.", 6000)
            if any(p["repo"] for p in added):
                self.check_updates(force=True, quiet=True, only=[p["id"] for p in added])

    # --- Изменение и удаление ---
    def edit(self, port):
        if not port:
            return
        found = core.scan_port_folder(port["folder"], hint=False) if os.path.isdir(port["folder"]) else None
        dialog = PortEditDialog(self.win, dict(port, exes=(found or {}).get("exes") or []), f"«{port['name']}»",
                                find_repo=self.GITHUB and not port.get("repo"), github=self.GITHUB)
        accepted = dialog.exec()
        result = dialog.result_port
        dialog.deleteLater()
        if not accepted or not result or not any(p is port for p in self.lib.ports):
            return
        old_name = port["name"]
        name = self.lib.unique_name(result["name"], skip=port)
        if name != old_name:
            self._move_cover(old_name, name, port)
            port["name"] = name
        if result["repo"].casefold() != (port.get("repo") or "").casefold():
            # Другой проект — прежние версии, история и переводы к нему не относятся
            port.update(repo=result["repo"], latest=None, history=[], installed_tag="", checked=0,
                        about=result.get("about"), translations={})
            port.pop("check_error", None)
            port.pop("notified_tag", None)
        exe_changed = not core.same_path(result["folder"], port["folder"]) or result["exe"] != port["exe"]
        port.update(folder=result["folder"], exe=result["exe"])
        if exe_changed:
            port["fingerprint"] = {}  # пересчитаем: новый файл запуска выбрал сам пользователь
            worker = Worker(core.port_fingerprint, dict(port), parent=self)
            worker.finished.connect(lambda fingerprint: (port.update(fingerprint=fingerprint), self.save()))
            worker.start()
        if not self.save():
            return
        core.log.info("%s: «%s» изменена", self.T["title"], port["name"])
        self.win.portsChanged.emit()
        if port.get("repo") and not port.get("latest"):
            self.check_updates(force=True, quiet=True, only=[port["id"]])
        if self.page.port is port:
            self.page.show_port(port, keep_origin=True)
        notify("success", self.win, self.T["saved"], f"«{port['name']}»")

    # --- Связь дисковой игры со Steam ---
    def link_steam(self, port):
        """«Найти в Steam» для дисковой игры: из её страницы в магазине — описание, оценки и отзывы. Запуск не
        меняется: игра по-прежнему запускается своим файлом из своей папки, Steam для этого не нужен."""
        if not port:
            return
        dialog = AddGameDialog(self.win, port, for_disc=True)
        core.log.info("Открыто окно «Найти в Steam» для дисковой игры «%s»", port["name"])
        accepted = dialog.exec()
        choice = dialog.choice
        dialog.deleteLater()
        if not accepted or not choice or not any(p is port for p in self.lib.ports):
            return
        old = port.get("appid")
        port["appid"] = choice["appid"]
        if not self.save():
            port["appid"] = old
            return
        core.log.info("Дисковая игра «%s» связана со Steam: «%s» (%s)", port["name"], choice["name"], choice["appid"])
        self.win.covers.forget(port["name"])  # с номером в Steam найдутся и картинки Steam
        self.win.portsChanged.emit()
        if self.page.port is port:
            self.page.show_port(port, keep_origin=True)
        notify("success", self.win, "Игра связана со Steam",
               f"«{port['name']}» → «{choice['name']}». Описание, оценки и отзывы — оттуда; запускается игра "
               "по-прежнему своим файлом.", 7000)

    def unlink_steam(self, port):
        if not port or not port.get("appid"):
            return
        old = port.pop("appid")
        if not self.save():
            port["appid"] = old
            return
        core.log.info("Дисковая игра «%s» отвязана от Steam", port["name"])
        self.win.portsChanged.emit()
        if self.page.port is port:
            self.page.show_port(port, keep_origin=True)
        notify("success", self.win, "Игра отвязана от Steam", f"«{port['name']}»: описание теперь из Википедии.")

    def _move_cover(self, old, new, port):
        """Обложки хранятся по названию — переименовали игру, переносим и обложку (если она только её)."""
        path = core.find_cover(old)
        if not path or self.win.name_in_use(old, skip=port) or core.find_cover(new):
            return
        try:
            os.replace(path, os.path.join(core.COVERS_DIR, core.clean_name(new) + os.path.splitext(path)[1]))
        except OSError as e:
            core.log.warning("Обложка «%s» не переименовалась: %s", old, e)

    def remove_selected(self):
        ports = [self.port_of(item) for item in self.current_view().selectedItems()]
        ports = [p for p in ports if p]
        if not ports:
            notify("info", self.win, "Ничего не выбрано", "Выделите игры в списке (Ctrl или Shift — несколько).")
            return
        names = ", ".join(f"«{p['name']}»" for p in ports[:3]) + ("…" if len(ports) > 3 else "")
        if not confirm(self.win, "Убрать из списка?", self.T["remove_confirm"].format(names=names, n=len(ports)),
                       "Убрать"):
            return
        for port in ports:
            self.lib.remove(port)
            if not self.win.name_in_use(port["name"]):  # у игры с тем же названием обложка общая
                try:
                    core.delete_cover(port["name"])
                except OSError:
                    pass
        if self.save():
            self.win.portsChanged.emit()
            notify("success", self.win, self.T["removed"].format(n=len(ports)), "Файлы на диске не тронуты.")

    # --- Запуск ---
    def launch(self, port):
        if not port:
            return
        if port.get("completed_at"):  # кнопка «Пройдена» выключена, но запуск мог прийти и из рулетки или коллекции
            notify("info", self.win, "Игра пройдена",
                   f"«{port['name']}»: {completed_text(port)}. Снова играть — «Вернуть в игру» на её странице.")
            return
        worker = Worker(core.check_port_exe, dict(port), parent=self)
        worker.finished.connect(lambda result: self._on_exe_checked(port, result))
        worker.failed.connect(lambda message: notify("error", self.win, self.T["not_launched"],
                                                     message.split(": ", 1)[-1]))
        worker.start()

    def _on_exe_checked(self, port, result):
        exe = os.path.basename(port["exe"])
        if result["state"] == "missing":
            notify("error", self.win, "Файла запуска нет", self.T["exe_missing"].format(name=port["name"], exe=exe),
                   8000)
            return
        if result["state"] == "changed":
            latest = (port.get("latest") or {}).get("tag")
            update = bool(latest and latest != port.get("installed_tag"))
            core.log.info("«%s»: файл запуска изменился с добавления", port["name"])
            if not confirm(self.win, "Файл запуска изменился", self.T["exe_changed"].format(name=port["name"], exe=exe),
                           "Запустить"):
                return
            port["fingerprint"] = result["fingerprint"]
            if update:
                port["installed_tag"] = latest  # файл новый — значит, обновили до последней версии
            self.save()
            self.win.portsChanged.emit()
        elif result["fingerprint"] != port.get("fingerprint"):
            port["fingerprint"] = result["fingerprint"]  # тот же файл, просто сменилось время — запоминаем
            self.save()
        try:
            core.launch_port(port)
        except OSError as e:
            core.log.warning("«%s» не запустилась: %s", port["name"], e)
            notify("error", self.win, self.T["not_launched"], f"«{port['name']}»: {e}", 8000)
            return
        self.win.play_tracker.start(port, self)  # время в игре — пока она запущена
        notify("success", self.win, "Запускаю", f"«{port['name']}». Приятной игры 🎮", 3000)

    # --- Обновления ---
    def open_release(self, port):
        url = (port.get("latest") or {}).get("url") if port else None
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def mark_updated(self, port):
        latest = (port.get("latest") or {}).get("tag") if port else None
        if not latest:
            return
        port["installed_tag"] = latest
        if self.save():
            core.log.info("Порт «%s»: отмечен как обновлённый до %s", port["name"], latest)
            self.win.portsChanged.emit()

    # --- Пройдена ---
    # Пройденная игра остаётся в списке: наигранное время, заметки, обложка и коллекции — при ней. Меняется только
    # то, что её больше не запустить: «Играть» становится выключенной «Пройдена», рулетка её не крутит, обновлений
    # не ищем. Сама программа с диска ничего не удаляет. «Вернуть в игру» — всё как было, в том числе рулетка
    # (если игру не убирали из неё вручную — тогда она так и остаётся убранной)
    def mark_completed(self, port):
        if not port or port.get("completed_at"):
            return
        if not confirm(self.win, "Отметить пройденной?",
                       f"«{port['name']}» останется в списке — с наигранным временем и заметками, но из рулетки уйдёт, "
                       "а вместо «Играть» будет «Пройдена». Файлы на диске программа не трогает: удалить игру можно "
                       "самим, когда захотите.\n\nСнова сыграть — «Вернуть в игру».", "Пройдена"):
            return
        port["completed_at"] = int(time.time())
        if not self.save():
            port.pop("completed_at", None)
            return
        core.log.info("«%s»: отмечена пройденной", port["name"])
        self._completed_changed()
        notify("success", self.win, "Игра пройдена 🏆", f"«{port['name']}». Поздравляю!", 5000)

    def unmark_completed(self, port):
        if not port or not port.get("completed_at"):
            return
        old = port.pop("completed_at")
        if not self.save():
            port["completed_at"] = old
            return
        core.log.info("«%s»: возвращена в игру", port["name"])
        self._completed_changed()
        notify("success", self.win, "Игра снова в игре", f"«{port['name']}»: «Играть» и рулетка — как раньше. Если "
               "игры уже нет на диске — поставьте её в ту же папку или укажите новую через «Изменить…».", 7000)

    def _completed_changed(self):
        self.win.portsChanged.emit()
        self.win.rouletteChanged.emit()  # барабан, «Крутить ещё» и счётчик игр рулетки

    def check_updates(self, force=False, quiet=False, only=None):
        """Спросить GitHub о новых версиях. quiet — без уведомления «всё актуально» (при запуске программы)."""
        if self._checking or not self.GITHUB:
            return
        ports = [p for p in self.lib.ports if p.get("repo") and not p.get("completed_at")
                 and (only is None or p["id"] in only)]
        if not ports:
            return
        self._checking = True
        self.updates_button.setEnabled(False)
        self.updates_button.setText("Проверяю…")
        worker = Worker(core.check_ports_updates, [dict(p) for p in ports], force, parent=self)
        worker.finished.connect(lambda result: self._on_updates(result, quiet))
        worker.failed.connect(lambda message: self._on_updates({"results": {}, "limit": None, "error": message}, quiet))
        worker.start()

    def _on_updates(self, result, quiet):
        self._checking = False
        self.updates_button.setEnabled(True)
        self.updates_button.setText("Проверить обновления")
        now, fresh = int(time.time()), []
        for port_id, found in result["results"].items():
            port = self.lib.find(port_id)
            if not port:
                continue
            port["checked"] = now
            if found.get("error"):
                port["check_error"] = found["error"]
                continue
            port.pop("check_error", None)
            release = found.get("latest")
            port["latest"] = release
            port["history"] = core.merge_port_history(port.get("history"), found.get("history"))
            if release and not port.get("installed_tag"):
                port["installed_tag"] = release["tag"]  # первая проверка: считаем, что стоит последняя версия
            if core.port_has_update(port) and port.get("notified_tag") != release["tag"]:
                port["notified_tag"] = release["tag"]
                fresh.append(port)
        if result["results"]:
            self.save()
            self.win.portsChanged.emit()
        if fresh:
            names = ", ".join(f"«{p['name']}» {p['latest']['tag']}" for p in fresh[:4]) + ("…" if len(fresh) > 4 else "")
            notify("info", self.win, "Вышли новые версии портов",
                   f"{names}. Скачать — правый клик по порту → «Скачать обновление».", 9000)
        elif result.get("limit"):
            if not quiet:
                notify("warning", self.win, "GitHub просит подождать", result["limit"], 8000)
        elif result.get("error"):
            if not quiet:
                notify("warning", self.win, "Обновления не проверились", result["error"].split(": ", 1)[-1], 7000)
        elif not quiet:
            errors = [self.lib.find(i) for i, r in result["results"].items() if r.get("error")]
            if errors:
                notify("warning", self.win, f"Не проверились: {len(errors)}",
                       "; ".join(f"«{p['name']}»: {p['check_error']}" for p in errors[:3] if p), 8000)
            else:
                notify("success", self.win, "Обновлений нет", "У всех портов со ссылкой на GitHub последние версии.")


class DiscGamesPage(NativePortsPage):
    """Вкладка «Дисковые игры»: игры с дисков (и любые другие), установленные каждая в свою папку.

    Всё как у портов — папка с играми или по одной, плитка, страница игры с фоном из SteamGridDB, запуск
    с проверкой файла, — только без GitHub: обновлений у таких игр нет. Свой список — discs.json.
    """

    OBJECT_NAME = "discGamesPage"
    LIBRARY = "discs"
    ROOT_SETTING = "discs_root"
    GITHUB = False
    T = {
        "title": "Дисковые игры",
        "empty_title": "Игры с дисков, которые уже стоят на ПК",
        "empty_text": "Игры, установленные с дисков, и любые другие, у которых своя папка и свой .exe. Укажите "
                      "папку, в которой у каждой игры своя папка, — программа найдёт файлы запуска. Обложки, фоны "
                      "и логотипы — из SteamGridDB, описание — из Steam («Найти в Steam») или из Википедии. "
                      "Игры можно крутить в рулетке вместе с остальными.",
        "root_button": "Добавить папку с играми",
        "add_button": "Добавить игру",
        "add_title": "Добавить игру",
        "one_folder": "Папка игры — где она установлена",
        "count": "Игр: {n}",
        "none": "Игр пока нет",
        "scan_busy": "Ищу игры…",
        "scan_title": "Найдены игры",
        "nothing_new": "Новых игр не нашлось",
        "all_known": "Все игры из этой папки уже в списке ({n}).",
        "add_busy": "Добавляю игры…",
        "added": "Добавлено игр: {n}",
        "not_added": "Игры не добавлены",
        "already": "Эта игра уже в списке",
        "no_folder": "Папки игры нет",
        "no_folder_text": "Игру перенесли или удалили. Укажите новую папку: «Изменить…».",
        "root_missing": "Папки с играми нет",
        "root_missing_text": "Выберите её заново: «Добавить папку с играми».",
        "saved": "Игра сохранена",
        "removed": "Убрано игр: {n}",
        "remove_confirm": "{names} — игр: {n}. Сами игры на диске останутся, программа их не трогает.",
        "remove_tip": "Сама игра на диске останется",
        "edit_tip": "Название, папка и файл запуска",
        "exe_missing": "«{name}»: {exe} не найден — игру перенесли или удалили. Укажите новое место: «Изменить…».",
        "exe_changed": "«{name}»: {exe} — уже не тот файл, что был при добавлении игры. Если вы поставили патч "
                       "или переустановили игру — всё в порядке. Если нет — файл мог заменить кто-то другой, "
                       "и запускать его не стоит.\n\nЗапустить?",
        "not_launched": "Игра не запустилась",
        "back": "К дисковым играм",
    }


# --- Коллекции ------------------------------------------------------------------
COLLECTION_ICON = fluent_icon("ALBUM", FIF.FOLDER)
GAME_KIND_NAMES = {"steam": "Игра Steam", "port": "Порт", "disc": "Дисковая игра"}


def game_word(n):
    """«1 игра», «3 игры», «12 игр»."""
    if n % 10 == 1 and n % 100 != 11:
        word = "игра"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "игры"
    else:
        word = "игр"
    return f"{n} {word}"


def game_kind(game):
    return ("disc" if core.is_disc(game) else "port") if core.is_port(game) else "steam"


def add_collection_menu(win, menu, games):
    """«Добавить в коллекцию» в меню игры на любой вкладке. Коллекций нет — сразу окно с названием новой;
    есть — подменю со списком (где игры уже есть — галочка, выбрать — убрать оттуда) и «Новая коллекция…»."""
    games = [g for g in games if g]
    page = getattr(win, "collections_page", None)
    if not games or page is None:
        return
    refs = [core.collection_ref(g) for g in games]
    collections = win.collections.collections
    if not collections:
        action = Action(COLLECTION_ICON, "Добавить в коллекцию…", menu)
        action.setToolTip("Коллекций пока нет — назовите первую, и " + ("игра попадёт" if len(games) == 1
                                                                        else "игры попадут") + " в неё")
        action.triggered.connect(lambda: page.new_collection(games))
        menu.addAction(action)
        return
    sub = RoundMenu("Добавить в коллекцию", menu)
    if hasattr(sub, "setIcon"):
        sub.setIcon(COLLECTION_ICON)
    for collection in collections:
        inside = all(win.collections.has(collection, ref) for ref in refs)
        action = Action(FIF.ACCEPT if inside else COLLECTION_ICON, collection["name"], sub)
        action.setToolTip("Уже в этой коллекции — выберите, чтобы убрать оттуда" if inside else
                          f"В коллекции: {game_word(len(win.collection_games(collection)))}")
        action.triggered.connect(lambda _=False, c=collection, i=inside: page.toggle_games(c, games, remove=i))
        sub.addAction(action)
    sub.addSeparator()
    new = Action(FIF.ADD, "Новая коллекция…", sub)
    new.triggered.connect(lambda: page.new_collection(games))
    sub.addAction(new)
    menu.addMenu(sub)


class CollectionNameDialog(MessageBoxBase):
    """Название новой коллекции или новое название для старой."""

    def __init__(self, parent, title, name="", skip=None):
        super().__init__(parent)
        self.win, self.skip = parent, skip
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        self.edit = LineEdit(self)
        self.edit.setText(name)
        self.edit.setPlaceholderText("Например: «На выходные», «Хорроры», «С друзьями»")
        self.edit.setMaxLength(core.COLLECTION_NAME_MAX)
        self.viewLayout.addWidget(self.edit)
        self.error = CaptionLabel("", self)
        self.error.setTextColor(*WARNING_TEXT)
        self.error.hide()
        self.viewLayout.addWidget(self.error)
        self.yesButton.setText("Сохранить")
        self.cancelButton.setText("Отмена")
        self.widget.setMinimumWidth(440)
        QTimer.singleShot(0, self.edit.setFocus)

    def name(self):
        return core.title_name(self.edit.text())

    def validate(self):
        name = self.name()
        message = ("Введите название коллекции." if not name else
                   "Коллекция с таким названием уже есть." if self.win.collections.name_taken(name, self.skip) else "")
        self.error.setText(message)
        self.error.setVisible(bool(message))
        return not message


class CollectionTileDelegate(QStyledItemDelegate):
    """Плитка коллекции — размером с обложку игры, вид на выбор («Вид плиток» на вкладке):

    - «Стекло» — коллаж из обложек её игр под матовым стеклом с бликом, название по центру на стеклянной плашке;
    - «Коробка от диска» — пластиковая коробка с корешком: на лицевой стороне обложки игр, на корешке и на
      ленте снизу — название.

    Под плиткой — сколько в ней игр."""

    COVER, PAD, TEXT_H = CoverTileDelegate.COVER, CoverTileDelegate.PAD, CoverTileDelegate.TEXT_H
    SPINE = 16  # ширина корешка коробки

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page

    def sizeHint(self, option, index):
        return QSize(self.COVER.width() + 2 * self.PAD, self.COVER.height() + self.TEXT_H + 2 * self.PAD)

    def paint(self, painter, option, index):
        collection = self.page.win.collections.find(index.data(Qt.UserRole))
        if collection is None:
            return
        dark = isDarkTheme()
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        rect = QRectF(option.rect)
        games = self.page.win.collection_games(collection)
        tile = QRectF(rect.x() + (rect.width() - self.COVER.width()) / 2, rect.y() + self.PAD,
                      self.COVER.width(), self.COVER.height())
        painter.save()
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        style = self.page.tile_style()
        if style == "case":
            self._paint_case(painter, option, collection, games, tile, dark, selected, hover)
        else:
            self._paint_collage(painter, option, collection, tile, dark, selected, hover)
        # Под плиткой — только сколько игр
        text_rect = QRectF(rect.x() + 4, tile.bottom() + 6, rect.width() - 8, self.TEXT_H - 6)
        painter.setFont(option.font)
        painter.setPen(QColor(255, 255, 255, 190) if dark else QColor(0, 0, 0, 170))
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, game_word(len(games)) if games else "Пока пусто")
        painter.restore()

    @staticmethod
    def _border(dark):
        return QColor(255, 255, 255, 46) if dark else QColor(0, 0, 0, 30)

    def _hover_back(self, painter, tile, dark, selected, hover):
        if selected or hover:
            painter.setPen(Qt.NoPen)
            alpha = (22 if selected else 12) if dark else (14 if selected else 8)
            painter.setBrush(QColor(255, 255, 255, alpha) if dark else QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(tile.adjusted(-6, -6, 6, 6), 12, 12)

    def _name_plate(self, painter, option, name, area, dark, lines=4):
        """Название по центру area — на стеклянной плашке, до lines строк."""
        font = QFont(option.font)
        font.setPixelSize(15)
        font.setBold(True)
        painter.setFont(font)
        flags = Qt.AlignCenter | Qt.TextWordWrap
        used = painter.boundingRect(QRectF(0, 0, area.width() - 32, 1000), flags, name)
        height = min(used.height(), QFontMetrics(font).lineSpacing() * lines)
        band = QRectF(area.x() + 8, area.center().y() - height / 2 - 10, area.width() - 16, height + 20)
        painter.setPen(QPen(QColor(255, 255, 255, 50) if dark else QColor(0, 0, 0, 25), 1))
        painter.setBrush(QColor(22, 22, 26, 165) if dark else QColor(255, 255, 255, 190))
        painter.drawRoundedRect(band, 8, 8)
        painter.setPen(WHITE if dark else QColor(0, 0, 0, 225))
        painter.drawText(band.adjusted(8, 10, -8, -10), flags, name)

    def _paint_collage(self, painter, option, collection, tile, dark, selected, hover):
        """«Стекло»: коллаж из обложек под матовым стеклом с бликом сверху, название по центру."""
        self._hover_back(painter, tile, dark, selected, hover)
        cover = QRectF(tile)
        radius = 12
        path = QPainterPath()
        path.addRoundedRect(cover, radius, radius)
        painter.save()
        painter.setClipPath(path)
        pixmap = self.page.tile_pixmap(collection["id"])
        if pixmap is not None:
            painter.drawPixmap(cover, pixmap, QRectF(pixmap.rect()))
        else:  # обложек пока нет — стекло цвета темы
            gradient = QLinearGradient(cover.topLeft(), cover.bottomRight())
            accent = QColor(themeColor())
            accent.setAlpha(90)
            gradient.setColorAt(0, accent)
            gradient.setColorAt(1, QColor(255, 255, 255, 20) if dark else QColor(0, 0, 0, 12))
            painter.fillRect(cover, gradient)
        painter.fillRect(cover, QColor(16, 16, 20, 70) if dark else QColor(255, 255, 255, 60))  # матовое стекло
        shine = QLinearGradient(cover.topLeft(), cover.center())  # блик сверху, как на стекле
        shine.setColorAt(0, QColor(255, 255, 255, 46))
        shine.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(cover, shine)
        self._name_plate(painter, option, collection["name"], cover, dark)
        painter.restore()
        painter.setBrush(Qt.NoBrush)
        if selected:
            painter.setPen(QPen(themeColor(), 3))
            painter.drawRoundedRect(cover.adjusted(-1.5, -1.5, 1.5, 1.5), radius + 1, radius + 1)
        else:
            painter.setPen(QPen(self._border(dark), 1))
            painter.drawPath(path)

    def _paint_case(self, painter, option, collection, games, tile, dark, selected, hover):
        """«Коробка от диска»: тёмный пластик, корешок слева с названием, на лицевой стороне — до четырёх обложек
        за прозрачной плёнкой, снизу — лента с названием."""
        self._hover_back(painter, tile, dark, selected, hover)
        box = tile.adjusted(4, 2, -4, -2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawRoundedRect(box.translated(2, 4), 6, 6)  # тень
        plastic = QLinearGradient(box.topLeft(), box.topRight())
        plastic.setColorAt(0, QColor(28, 28, 32))
        plastic.setColorAt(1, QColor(46, 46, 52))
        painter.setPen(QPen(themeColor() if selected else QColor(255, 255, 255, 40), 3 if selected else 1))
        painter.setBrush(plastic)
        painter.drawRoundedRect(box, 6, 6)

        # Корешок: рёбра и название сверху вниз
        spine = QRectF(box.x(), box.y(), self.SPINE, box.height())
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        for x in (spine.right() - 3, spine.right()):
            painter.drawLine(QPointF(x, box.y() + 6), QPointF(x, box.bottom() - 6))
        painter.save()
        painter.translate(spine.center())
        painter.rotate(90)
        spine_font = QFont(option.font)
        spine_font.setPixelSize(10)
        spine_font.setBold(True)
        painter.setFont(spine_font)
        painter.setPen(QColor(255, 255, 255, 200))
        length = spine.height() - 20
        spine_text = QFontMetrics(spine_font).elidedText(collection["name"], Qt.ElideRight, round(length))
        painter.drawText(QRectF(-length / 2, -spine.width() / 2, length, spine.width()), Qt.AlignCenter, spine_text)
        painter.restore()

        # Лицевая сторона — обложки игр (одна целиком, две-три рядом, четыре — 2×2)
        front = QRectF(spine.right() + 3, box.y() + 5, box.right() - spine.right() - 8, box.height() - 10)
        path = QPainterPath()
        path.addRoundedRect(front, 3, 3)
        painter.save()
        painter.setClipPath(path)
        names = [g["name"] for g in games[:4]]
        if names:
            rows = 2 if len(names) == 4 else 1
            per_row = math.ceil(len(names) / rows)
            for i, name in enumerate(names):
                row, col = divmod(i, per_row)
                cell = QRectF(front.x() + col * front.width() / per_row, front.y() + row * front.height() / rows,
                              front.width() / per_row, front.height() / rows)
                pixmap = self.page.thumbs.get(name)
                if isinstance(pixmap, QPixmap):
                    scale = max(cell.width() / pixmap.width(), cell.height() / pixmap.height())
                    source = QRectF(0, 0, cell.width() / scale, cell.height() / scale)
                    source.moveCenter(QPointF(pixmap.rect().center()))
                    painter.drawPixmap(cell, pixmap, source)
                else:
                    painter.fillRect(cell, QColor(60, 60, 68))
        else:
            accent = QColor(themeColor())
            accent.setAlpha(70)
            painter.fillRect(front, accent)
        # Прозрачная плёнка: блик по диагонали
        shine = QLinearGradient(front.topLeft(), front.bottomRight())
        shine.setColorAt(0, QColor(255, 255, 255, 60))
        shine.setColorAt(0.35, QColor(255, 255, 255, 8))
        shine.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(front, shine)
        # Лента с названием снизу
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = two_lines(collection["name"], metrics, round(front.width() - 16))
        band_h = metrics.lineSpacing() * (text.count("\n") + 1) + 12
        band = QRectF(front.x(), front.bottom() - band_h, front.width(), band_h)
        painter.fillRect(band, QColor(12, 12, 16, 200))
        painter.setPen(WHITE)
        painter.drawText(band.adjusted(8, 0, -8, 0), Qt.AlignCenter, text)
        painter.restore()


class CollectionGameDelegate(CoverTileDelegate):
    """Игра в открытой коллекции — как на других вкладках. Пока обложки «раздаются» (DealOverlay), плитки
    не рисуются: на их места как раз ложатся летящие обложки."""

    def __init__(self, thumbs, parent=None):
        super().__init__(thumbs, parent)
        self.hidden = False

    def paint(self, painter, option, index):
        if not self.hidden:
            super().paint(painter, option, index)


class DealOverlay(QWidget):
    """Открытие коллекции: обложки её игр «раздаются» из плитки коллекции, как карты из колоды, — одна за другой
    слетают со стопки, на лету выпрямляются и ложатся на свои места в сетке."""

    finished = Signal()
    CARD_MS, STAGGER_MS, MAX_CARDS = 440, 30, 24

    def __init__(self, host, thumbs, start, cards):
        super().__init__(host)
        self.thumbs, self.start = thumbs, start
        self.cards = [(name, end, random.uniform(-10, 10), i * self.STAGGER_MS)
                      for i, (name, end) in enumerate(cards[:self.MAX_CARDS])]
        self.elapsed = 0.0
        self.setGeometry(host.rect())
        self.show()
        self.raise_()
        total = self.CARD_MS + self.STAGGER_MS * max(0, len(self.cards) - 1)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(total)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(float(total))
        self.anim.valueChanged.connect(self._set_elapsed)
        self.anim.finished.connect(self._done)
        self.anim.start()

    def mousePressEvent(self, event):
        event.accept()  # пока раздаются — щелчки не проходят к сетке под ними

    def _set_elapsed(self, value):
        self.elapsed = float(value)
        self.update()

    def _done(self):
        self.finished.emit()
        self.deleteLater()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        waiting = [card for card in self.cards if self.elapsed < card[3]]
        for name, _end, angle, _delay in waiting[:4][::-1]:  # колода: верхние карты чуть веером
            self._card(painter, name, self.start, angle * 0.5)
        for name, end, angle, delay in self.cards:
            if self.elapsed < delay:
                continue
            t = min(1.0, (self.elapsed - delay) / self.CARD_MS)
            e = 1 - (1 - t) ** 3
            rect = QRectF(self.start.x() + (end.x() - self.start.x()) * e,
                          self.start.y() + (end.y() - self.start.y()) * e - math.sin(math.pi * t) * 28,  # дуга
                          self.start.width() + (end.width() - self.start.width()) * e,
                          self.start.height() + (end.height() - self.start.height()) * e)
            self._card(painter, name, rect, angle * (1 - e))

    def _card(self, painter, name, rect, angle):
        dark = isDarkTheme()
        painter.save()
        painter.translate(rect.center())
        painter.rotate(angle)
        card = QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 60))
        painter.drawRoundedRect(card.translated(0, 4), 8, 8)  # тень
        path = QPainterPath()
        path.addRoundedRect(card, 8, 8)
        painter.setClipPath(path)
        pixmap = self.thumbs.get(name)
        if isinstance(pixmap, QPixmap):
            painter.drawPixmap(card, pixmap, QRectF(pixmap.rect()))
        else:
            painter.fillRect(card, QColor(60, 60, 66) if dark else QColor(225, 225, 230))
        painter.setClipping(False)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 50) if dark else QColor(0, 0, 0, 35), 1))
        painter.drawPath(path)
        painter.restore()


class CollectionsPage(QWidget):
    """Вкладка «Коллекции»: свои подборки игр из Steam, портов и дисковых. Вид плиток — на выбор (STYLES).
    Двойной щелчок — коллекция открывается: обложки «раздаются» из её плитки по местам. Добавить игру —
    правый клик по ней на любой вкладке → «Добавить в коллекцию». Коллекцию можно крутить в рулетке."""

    TILE_DELAY_MS = 600  # скачиваются обложки — коллажи плиток пересобираются не на каждую

    def __init__(self, window):
        super().__init__()
        self.setObjectName("collectionsPage")
        self.win = window
        self.current = None  # открытая коллекция
        self.detail_games = []
        self._overlay = None
        self.tiles = {}  # номер коллекции → (обложки, из которых собран коллаж, QPixmap или None)
        self._tile_pending = {}  # номер коллекции → обложки, из которых коллаж собирается сейчас
        self._dirty = set()
        self.tile_timer = QTimer(self)
        self.tile_timer.setSingleShot(True)
        self.tile_timer.setInterval(self.TILE_DELAY_MS)
        self.tile_timer.timeout.connect(self._update_tiles)
        # Обложки — и для вееров на плитках, и для игр внутри коллекции
        self.thumbs = CoverThumbs(CoverTileDelegate.COVER, self)

        # Список коллекций
        self.list_view = QWidget(self)
        self.title = TitleLabel("Коллекции", self.list_view)
        self.count_label = CaptionLabel("", self.list_view)
        self.grid = self._make_grid(self.list_view)
        self.grid.setItemDelegate(CollectionTileDelegate(self, self.grid))
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.itemDoubleClicked.connect(lambda item: self.open_collection(self._collection_of(item), item))
        self.grid.customContextMenuRequested.connect(self._tile_menu)
        self.grid.itemSelectionChanged.connect(self._on_tile_selection)

        self.empty_box = QWidget(self.list_view)
        empty = QVBoxLayout(self.empty_box)
        empty.addStretch(1)
        empty.addWidget(SubtitleLabel("Свои подборки игр", self.empty_box), 0, Qt.AlignHCenter)
        self.empty_text = plain_label(
            "Соберите игры из Steam, портов и дисковых в свои коллекции: «На выходные», «Хорроры», «С друзьями». "
            "Добавить игру — правый клик по ней на любой вкладке с играми → «Добавить в коллекцию». Любую "
            "коллекцию можно крутить в рулетке.", self.empty_box)
        self.empty_text.setAlignment(Qt.AlignCenter)
        empty.addWidget(self.empty_text, 0, Qt.AlignHCenter)
        empty.addStretch(1)

        new_button = PrimaryPushButton("Новая коллекция", self.list_view, FIF.ADD)
        new_button.clicked.connect(lambda: self.new_collection([]))
        list_buttons_box = QWidget(self.list_view)
        list_buttons = CenteredFlowLayout(list_buttons_box, spacing=12, center=False)
        list_buttons.addWidget(new_button)
        self.style_button = PushButton("Вид плиток", self.list_view, fluent_icon("PALETTE", FIF.VIEW))
        self.style_button.setToolTip("Как выглядят плитки коллекций: стекло или коробка от диска")
        self.style_button.clicked.connect(self._style_menu)
        list_buttons.addWidget(self.style_button)

        list_layout = QVBoxLayout(self.list_view)
        list_layout.setContentsMargins(36, 20, 36, 28)
        list_layout.setSpacing(12)
        list_layout.addWidget(self.title)
        list_layout.addWidget(self.count_label)
        list_layout.addWidget(self.grid, 1)
        list_layout.addWidget(self.empty_box, 1)
        list_layout.addWidget(list_buttons_box)
        self.list_backdrop = SelectionBackdrop(self.list_view)  # выделили коллекцию — за ней обложки её игр

        # Открытая коллекция
        self.detail_view = QWidget(self)
        back_button = TransparentPushButton("К коллекциям", self.detail_view, FIF.LEFT_ARROW)
        back_button.clicked.connect(self.show_list)
        self.detail_title = TitleLabel("", self.detail_view)
        self.detail_count = CaptionLabel("", self.detail_view)
        self.games = self._make_grid(self.detail_view)
        self.games_delegate = CollectionGameDelegate(self.thumbs, self.games)
        self.games.setItemDelegate(self.games_delegate)
        self.thumbs.updated = lambda: (self.games.viewport().update(), self.grid.viewport().update())
        self.games.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.games.itemDoubleClicked.connect(lambda item: self.open_game(self._game_of(item)))
        self.games.customContextMenuRequested.connect(self._game_menu)
        self.games.itemSelectionChanged.connect(self._on_game_selection)
        self.detail_empty = plain_label("В коллекции пока нет игр. Добавить — правый клик по игре на любой вкладке "
                                        "с играми → «Добавить в коллекцию».", self.detail_view, caption=True)
        self.spin_button = PrimaryPushButton("Крутить эту коллекцию", self.detail_view, FIF.SYNC)
        self.spin_button.setToolTip("Рулетка будет крутить только игры этой коллекции. Вернуть — «Что крутить» "
                                    "рядом с кнопкой «Крутить»")
        self.spin_button.clicked.connect(lambda: self.spin_collection(self.current))
        rename_button = PushButton("Переименовать…", self.detail_view, fluent_icon("EDIT", FIF.SETTING))
        rename_button.clicked.connect(lambda: self.rename(self.current))
        self.remove_button = PushButton("Убрать выбранные", self.detail_view, fluent_icon("REMOVE", FIF.DELETE))
        self.remove_button.setToolTip("Убрать из коллекции — сами игры останутся в своих списках")
        self.remove_button.clicked.connect(self.remove_selected)
        detail_buttons_box = QWidget(self.detail_view)
        detail_buttons = CenteredFlowLayout(detail_buttons_box, spacing=12, center=False)
        for button in (self.spin_button, rename_button, self.remove_button, back_button):
            detail_buttons.addWidget(button)
        self.detail_buttons = detail_buttons

        detail_layout = QVBoxLayout(self.detail_view)
        detail_layout.setContentsMargins(36, 20, 36, 28)
        detail_layout.setSpacing(12)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_count)
        detail_layout.addWidget(self.games, 1)
        detail_layout.addWidget(self.detail_empty)
        detail_layout.addWidget(detail_buttons_box)
        self.detail_backdrop = SelectionBackdrop(self.detail_view)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.detail_view)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.mini = MiniPlayerSpot(self, self.title)

        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.setContext(Qt.WidgetWithChildrenShortcut)
        escape.activated.connect(self._on_escape)
        for signal in (self.win.libraryChanged, self.win.portsChanged, self.win.collectionsChanged):
            signal.connect(self.refresh)
        self.win.covers.coverReady.connect(self.cover_changed)
        self.refresh()

    @staticmethod
    def _make_grid(parent):
        grid = QListWidget(parent)
        grid.setViewMode(QListView.IconMode)
        grid.setResizeMode(QListView.Adjust)
        grid.setMovement(QListView.Static)
        grid.setUniformItemSizes(True)
        grid.setSpacing(4)
        grid.setFrameShape(QFrame.NoFrame)
        grid.setMouseTracking(True)
        grid.viewport().setMouseTracking(True)
        grid.viewport().setAttribute(Qt.WA_Hover)
        grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        grid.verticalScrollBar().setSingleStep(30)
        grid.setStyleSheet("QListWidget {background: transparent; border: none; outline: none}")
        grid.setContextMenuPolicy(Qt.CustomContextMenu)
        if SmoothScrollDelegate:
            grid._smooth_scroll = SmoothScrollDelegate(grid)
        return grid

    @staticmethod
    def _tile_size():
        cover = CoverTileDelegate.COVER
        return QSize(cover.width() + 2 * CoverTileDelegate.PAD,
                     cover.height() + CoverTileDelegate.TEXT_H + 2 * CoverTileDelegate.PAD)

    # --- Список коллекций ---
    def refresh(self):
        collections = self.win.collections.collections
        selected = {item.data(Qt.UserRole) for item in self.grid.selectedItems()}
        self.grid.clear()
        for collection in collections:
            item = QListWidgetItem("")
            item.setData(Qt.UserRole, collection["id"])
            item.setToolTip(f"{collection['name']}\n{game_word(len(self.win.collection_games(collection)))}"
                            " · двойной щелчок — открыть")
            item.setSizeHint(self._tile_size())
            self.grid.addItem(item)
            item.setSelected(collection["id"] in selected)
        self.count_label.setText(f"Коллекций: {len(collections)}" if collections else "Коллекций пока нет")
        self.grid.setVisible(bool(collections))
        self.empty_box.setVisible(not collections)
        self.style_button.setVisible(bool(collections))
        self._update_tiles()
        self.grid.viewport().update()
        if self.current is not None:
            if any(c is self.current for c in collections):
                self.refresh_detail()
            else:
                self.current = None
                self.show_list()  # коллекцию удалили

    def _collection_of(self, item):
        return self.win.collections.find(item.data(Qt.UserRole)) if item else None

    def _on_tile_selection(self):
        names = []
        for item in self.grid.selectedItems():
            collection = self._collection_of(item)
            if collection:
                names += [g["name"] for g in self.win.collection_games(collection)]
        self.list_backdrop.set_names(names)

    def cover_changed(self, name):
        self.thumbs.forget(name)
        self.games.viewport().update()
        self.grid.viewport().update()  # обложка могла быть на плитке
        self.detail_backdrop.refresh(name)
        self.list_backdrop.refresh(name)
        for collection in self.win.collections.collections:
            if any(g["name"] == name for g in self.win.collection_games(collection)):
                self._dirty.add(collection["id"])
        if self._dirty:
            self.tile_timer.start()

    # --- Вид плиток ---
    STYLES = (("glass", "Стекло", "Коллаж из обложек под матовым стеклом, название по центру"),
              ("case", "Коробка от диска", "Пластиковая коробка с корешком, на лицевой стороне — обложки игр"))

    def tile_style(self):
        style = self.win.settings.get("collections_style", "glass")
        return style if style in dict((k, t) for k, t, _ in self.STYLES) else "glass"  # «Колоды» и «Веера» больше нет

    def _style_menu(self):
        menu = RoundMenu(parent=self)
        current = self.tile_style()
        for key, title, tip in self.STYLES:
            action = Action(FIF.ACCEPT if key == current else COLLECTION_ICON, title, menu)
            action.setToolTip(tip)
            action.triggered.connect(lambda _=False, k=key: self.set_tile_style(k))
            menu.addAction(action)
        button = self.style_button
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def set_tile_style(self, style):
        self.win.settings["collections_style"] = style
        core.save_settings(collections_style=style)
        core.log.info("Вид плиток коллекций: %s", dict((k, t) for k, t, _ in self.STYLES)[style])
        self._update_tiles()
        self.grid.viewport().update()

    # --- Картинки плиток «Колода» и «Стекло»: коллаж из обложек, собирается в фоне ---
    def tile_pixmap(self, collection_id):
        tile = self.tiles.get(collection_id)
        return tile[1] if tile else None

    def _tile_paths(self, collection):
        paths = []
        for game in self.win.collection_games(collection):
            path = core.find_cover(game["name"])
            if path:
                paths.append(path)
                if len(paths) == 4:
                    break
        return tuple(paths)

    def _update_tiles(self):
        if self.tile_style() != "glass":
            return  # коробке коллаж не нужен — обложки рисуются как есть
        for collection in self.win.collections.collections:
            cid, paths = collection["id"], self._tile_paths(collection)
            tile = self.tiles.get(cid)
            fresh = tile is not None and tile[0] == paths and cid not in self._dirty
            if fresh or self._tile_pending.get(cid) == paths:
                continue
            self._dirty.discard(cid)
            if not paths:
                self.tiles[cid] = (paths, None)
                self._tile_pending.pop(cid, None)
                continue
            self._tile_pending[cid] = paths
            worker = Worker(core.collection_cover, list(paths), parent=self)
            worker.finished.connect(lambda image, c=cid, p=paths: self._on_tile(c, p, image))
            worker.failed.connect(lambda message, c=cid: (self._tile_pending.pop(c, None), core.log.warning(
                "Картинка коллекции не собралась: %s", message)))
            worker.start()
        self.grid.viewport().update()

    def _on_tile(self, cid, paths, image):
        if self._tile_pending.get(cid) != paths:
            return  # пока собиралась, обложки сменились — соберётся заново
        self._tile_pending.pop(cid, None)
        self.tiles[cid] = (paths, pil_pixmap(image))
        self.grid.viewport().update()

    # --- Открытая коллекция ---
    def open_collection(self, collection, item=None):
        if collection is None:
            return
        start = None
        if item is not None:  # обложки полетят из этой плитки
            cover = CoverTileDelegate.COVER
            rect = self.grid.visualItemRect(item)
            corner = self.grid.viewport().mapTo(self, QPoint(rect.x() + (rect.width() - cover.width()) // 2,
                                                             rect.y() + CoverTileDelegate.PAD))
            start = QRectF(corner.x(), corner.y(), cover.width(), cover.height())
        self.current = collection
        self.games.clearSelection()
        self.refresh_detail()
        self.stack.setCurrentWidget(self.detail_view)
        core.log.info("Коллекция «%s» открыта: %s", collection["name"], game_word(len(self.detail_games)))
        if start is not None and self.detail_games:
            self.games_delegate.hidden = True
            QTimer.singleShot(0, lambda: self._deal(start))

    def _deal(self, start):
        self.games.doItemsLayout()  # сетка раскладывается с задержкой — места обложек нужны сейчас
        viewport, cover = self.games.viewport(), CoverTileDelegate.COVER
        cards = []
        for row in range(self.games.count()):
            item = self.games.item(row)
            rect = self.games.visualItemRect(item)
            if rect.top() > viewport.height():
                break
            corner = viewport.mapTo(self, QPoint(rect.x() + (rect.width() - cover.width()) // 2,
                                                 rect.y() + CoverTileDelegate.PAD))
            cards.append((item.data(Qt.UserRole), QRectF(corner.x(), corner.y(), cover.width(), cover.height())))
            if len(cards) >= DealOverlay.MAX_CARDS:
                break
        for name, _rect in cards:
            self.thumbs.get(name)  # обложки читаются, пока карты летят
        if not cards or self.stack.currentWidget() is not self.detail_view:
            self._dealt()
            return
        self._overlay = DealOverlay(self, self.thumbs, start, cards)
        self._overlay.finished.connect(self._dealt)

    def _dealt(self):
        self._overlay = None
        self.games_delegate.hidden = False
        self.games.viewport().update()

    def refresh_detail(self):
        collection = self.current
        if collection is None:
            return
        games = self.win.collection_games(collection)
        selected = {id(g) for g in (self._game_of(item) for item in self.games.selectedItems()) if g}
        self.detail_games = games
        self.games.clear()
        for row, game in enumerate(games):
            item = QListWidgetItem("")
            item.setData(Qt.UserRole, game["name"])
            item.setData(Qt.UserRole + 1, row)
            item.setToolTip(f"{game['name']}\n{GAME_KIND_NAMES[game_kind(game)]}")
            item.setSizeHint(self._tile_size())
            self.games.addItem(item)
            item.setSelected(id(game) in selected)
        self.detail_title.setText(collection["name"])
        kinds = {"steam": 0, "port": 0, "disc": 0}
        for game in games:
            kinds[game_kind(game)] += 1
        parts = [f"{title} {kinds[k]}" for k, title in (("steam", "Steam"), ("port", "порты"), ("disc", "дисковые"))
                 if kinds[k]]
        missing = len(collection["items"]) - len(games)
        text = game_word(len(games)) + (f" · {', '.join(parts)}" if len(parts) > 1 else "")
        if missing:
            text += f" · ещё {game_word(missing)} сейчас нет в списках — вернутся, если их добавить снова"
        self.detail_count.setText(text)
        self.games.setVisible(bool(games))
        self.detail_empty.setVisible(not games)
        self.spin_button.setEnabled(bool(games))
        self.remove_button.setVisible(bool(games))
        self.detail_buttons.invalidate()

    def _game_of(self, item):
        row = item.data(Qt.UserRole + 1) if item else None
        return self.detail_games[row] if isinstance(row, int) and 0 <= row < len(self.detail_games) else None

    def reveal(self, game):
        """Вернулись со страницы игры — она выделена в открытой коллекции."""
        if not game:
            return
        for row in range(self.games.count()):
            item = self.games.item(row)
            if self._game_of(item) is game:
                self.games.clearSelection()
                item.setSelected(True)
                self.games.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                break

    def _on_game_selection(self):
        self.detail_backdrop.set_names([item.data(Qt.UserRole) for item in self.games.selectedItems()])

    def show_list(self):
        if self._overlay is not None:
            self._overlay.deleteLater()
            self._dealt()
        self.stack.setCurrentWidget(self.list_view)
        if self.current is not None:
            for row in range(self.grid.count()):
                item = self.grid.item(row)
                if item.data(Qt.UserRole) == self.current["id"]:
                    self.grid.clearSelection()
                    item.setSelected(True)
                    self.grid.scrollToItem(item)
                    break
        self.current = None

    def open_game(self, game):
        """Страница игры: Steam — на «Рулетке», порт и дисковая — на своей вкладке. С неё можно вернуться сюда."""
        if not game:
            return
        self.win.game_origin = self.current
        self.win.switchTo(self.win.home_page)
        self.win.home_page.open_game(game)
        self.win.game_origin = None  # страница запомнила, откуда её открыли; дальше — обычный путь

    def play(self, game):
        if not game:
            return
        if core.is_port(game):
            (self.win.disc_page if core.is_disc(game) else self.win.native_page).launch(game)
        else:
            self.win.launch_game(game)

    # --- Меню ---
    def _tile_menu(self, pos):
        item = self.grid.itemAt(pos)
        collection = self._collection_of(item)
        if not collection:
            return
        self.grid.clearSelection()
        item.setSelected(True)
        has_games = bool(self.win.collection_games(collection))
        menu = RoundMenu(parent=self)
        open_action = Action(COLLECTION_ICON, "Открыть", menu)
        open_action.triggered.connect(lambda: self.open_collection(collection, item))
        spin = Action(FIF.SYNC, "Крутить в рулетке", menu)
        spin.setEnabled(has_games)
        spin.triggered.connect(lambda: self.spin_collection(collection))
        rename = Action(fluent_icon("EDIT", FIF.SETTING), "Переименовать…", menu)
        rename.triggered.connect(lambda: self.rename(collection))
        delete = Action(FIF.DELETE, "Удалить коллекцию", menu)
        delete.setToolTip("Сами игры останутся в своих списках")
        delete.triggered.connect(lambda: self.delete(collection))
        menu.addActions([open_action, spin, rename])
        menu.addSeparator()
        menu.addAction(delete)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _game_menu(self, pos):
        item = self.games.itemAt(pos)
        game = self._game_of(item)
        if not game:
            return
        if not item.isSelected():
            self.games.clearSelection()
            item.setSelected(True)
        menu = RoundMenu(parent=self)
        if core.is_port(game) or game.get("appid"):
            installed = core.is_port(game) or core.is_installed(game["appid"])
            play = Action(FIF.PLAY if installed else FIF.DOWNLOAD, "Играть" if installed else "Установить", menu)
            play.triggered.connect(lambda: self.play(game))
            menu.addAction(play)
        page = Action(FIF.GAME, "Открыть страницу игры", menu)
        page.triggered.connect(lambda: self.open_game(game))
        menu.addAction(page)
        menu.addSeparator()
        selected = [self._game_of(i) for i in self.games.selectedItems()]
        add_collection_menu(self.win, menu, selected)
        menu.addSeparator()
        count = len([g for g in selected if g])
        remove = Action(fluent_icon("REMOVE", FIF.DELETE),
                        "Убрать из коллекции" if count == 1 else f"Убрать из коллекции ({count})", menu)
        remove.setToolTip("Сама игра останется в своём списке")
        remove.triggered.connect(self.remove_selected)
        menu.addAction(remove)
        menu.exec(self.games.viewport().mapToGlobal(pos))

    def _on_escape(self):
        if self.stack.currentWidget() is self.detail_view:
            self.show_list()
        else:
            self.grid.clearSelection()

    # --- Изменения ---
    def _save(self):
        if not self.win.save_collections():
            return False
        self.win.collectionsChanged.emit()
        return True

    def new_collection(self, games):
        dialog = CollectionNameDialog(self.win, "Новая коллекция")
        core.log.info("Открыто окно «Новая коллекция»")
        accepted = dialog.exec()
        name = dialog.name()
        dialog.deleteLater()
        if not accepted or not name:
            return
        collection = self.win.collections.create(name)
        added = self.win.collections.add(collection, [core.collection_ref(g) for g in games if g])
        if not self.win.save_collections():
            self.win.collections.delete(collection)
            return
        core.log.info("Коллекция «%s» создана: %s", name, game_word(added))
        self.win.collectionsChanged.emit()
        notify("success", self.win, "Коллекция создана", f"«{name}»: {game_word(added)}." if added else
               f"«{name}». Добавить игры — правый клик по игре на любой вкладке → «Добавить в коллекцию».", 6000)

    def toggle_games(self, collection, games, remove=False):
        refs = [core.collection_ref(g) for g in games if g]
        lib = self.win.collections
        count = lib.remove(collection, refs) if remove else lib.add(collection, refs)
        if not count:
            notify("info", self.win, "Уже в коллекции", f"«{collection['name']}»: эти игры уже там.")
            return
        if not self._save():
            return
        core.log.info("Коллекция «%s»: %s %s", collection["name"], "убрано" if remove else "добавлено",
                      game_word(count))
        notify("success", self.win, ("Убрано из коллекции" if remove else "Добавлено в коллекцию"),
               f"«{collection['name']}»: {game_word(count)}.", 3500)

    def remove_selected(self):
        games = [g for g in (self._game_of(i) for i in self.games.selectedItems()) if g]
        if not games or self.current is None:
            notify("info", self.win, "Ничего не выбрано", "Выделите игры в коллекции (Ctrl или Shift — несколько).")
            return
        self.toggle_games(self.current, games, remove=True)

    def rename(self, collection):
        if collection is None:
            return
        dialog = CollectionNameDialog(self.win, "Переименовать коллекцию", collection["name"], skip=collection)
        accepted = dialog.exec()
        name = dialog.name()
        dialog.deleteLater()
        if not accepted or not name or name == collection["name"]:
            return
        old, collection["name"] = collection["name"], name
        if not self._save():
            collection["name"] = old
            return
        core.log.info("Коллекция «%s» переименована в «%s»", old, name)

    def delete(self, collection):
        if not confirm(self.win, "Удалить коллекцию?", f"«{collection['name']}» — сами игры останутся в своих "
                                                         "списках, удалится только подборка.", "Удалить"):
            return
        self.win.collections.delete(collection)
        if not self.win.save_collections():
            self.win.collections.collections.append(collection)
            return
        self.tiles.pop(collection["id"], None)
        core.log.info("Коллекция «%s» удалена", collection["name"])
        self.win.collectionsChanged.emit()
        notify("success", self.win, "Коллекция удалена", f"«{collection['name']}». Игры остались на своих местах.")

    def spin_collection(self, collection):
        """«Крутить эту коллекцию»: рулетка крутит только её игры — и сразу запускается."""
        if collection is None:
            return
        if not self.win.collection_games(collection):
            notify("info", self.win, "Крутить нечего", f"В «{collection['name']}» пока нет игр.")
            return
        pool = f"col:{collection['id']}"
        self.win.settings["roulette_pool"] = pool
        core.save_settings(roulette_pool=pool)
        core.log.info("Рулетка крутит: коллекция «%s»", collection["name"])
        home = self.win.home_page
        home.current = None
        home.name_label.setText("")
        home.on_library_changed()
        self.win.switchTo(home)
        home.show_reel()
        QTimer.singleShot(350, home.spin)

    # --- Окно ---
    def _fit_empty_text(self):
        label = self.empty_text
        width = max(240, min(560, self.width() - 2 * 36 - 24))
        rect = QFontMetrics(label.font()).boundingRect(QRect(0, 0, width, 10000),
                                                       int(Qt.AlignCenter | Qt.TextWordWrap), label.text())
        label.setFixedSize(width, rect.height() + 6)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.mini.place()
        self._fit_empty_text()
        if self._overlay is not None:
            self._overlay.deleteLater()  # сетка переложилась — места карт уже другие
            self._dealt()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.mini.place)
        self._fit_empty_text()
        ratio = self.devicePixelRatioF()
        if ratio != self.thumbs.ratio:
            self.thumbs.ratio = ratio
            self.thumbs.clear()


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
def _paint_disc(painter, rect, color):
    """Значок «компакт-диск» контуром, как значки Fluent: обод, отверстие в центре и блик на дорожках."""
    rect = QRectF(rect)
    side = min(rect.width(), rect.height())
    unit = side / 16  # значки Fluent рисуются в сетке 16×16
    center = rect.center()
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, 1.2 * unit)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(center, 6.9 * unit, 6.9 * unit)  # обод диска
    painter.drawEllipse(center, 2.0 * unit, 2.0 * unit)  # отверстие
    for radius, start in ((4.6 * unit, 100), (4.6 * unit, 280)):  # блик на дорожках — две дуги друг против друга
        arc = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
        painter.drawArc(arc, start * 16, 55 * 16)
    painter.restore()


class DiscIcon(FluentIconBase):
    """Значок «Дисковых игр» в боковом меню: круглый CD-диск, в цвет текста меню своей темы."""

    def path(self, theme=Theme.AUTO):
        return ""  # не из файла — рисуется в render

    def render(self, painter, rect, theme=Theme.AUTO, indexes=None, **attributes):
        dark = isDarkTheme() if theme == Theme.AUTO else theme == Theme.DARK
        _paint_disc(painter, rect, QColor(255, 255, 255) if dark else QColor(0, 0, 0, 228))

    def icon(self, theme=Theme.AUTO, color=None):
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        self.render(painter, QRectF(0, 0, 64, 64), theme)
        painter.end()
        return QIcon(pixmap)


DISC_ICON = DiscIcon()


class AppUpdater(QObject):
    """Новая версия программы на GitHub: проверка (сама — при каждом запуске, и по кнопке), точка на «Настройках»
    и установка по кнопке «Обновить» (core: скачать, сверить SHA-256, заменить .exe, перезапуститься).

    Для проверки обновления без нового релиза: $env:BR_FAKE_VERSION="2.4" — программа считает себя версией 2.4,
    и последний настоящий релиз покажется ей обновлением. Ставится он так же, со сверкой суммы."""

    changed = Signal()
    CHECK_DELAY_MS = 1500  # при каждом запуске — как только окно открылось

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.checking = False
        self.installing = False
        self._notified = None  # о какой версии уже сказали в этом запуске
        fake = os.environ.get("BR_FAKE_VERSION", "").strip()
        self.fake = bool(core.version_tuple(fake))
        self.current = fake if self.fake else __version__
        release = win.settings.get("update_release")
        self.release = release if isinstance(release, dict) and release.get("tag") else None
        if self.fake:
            core.log.info("Обновление программы: для проверки программа считает себя версией %s (BR_FAKE_VERSION)",
                          self.current)

    def available(self):
        return bool(self.release) and core.newer_version(self.release["tag"], self.current)

    def pending(self):
        """Есть обновление, которое не попросили пропустить, — для точки на «Настройках»."""
        return self.available() and self.release["tag"] != self.win.settings.get("update_skip")

    def schedule(self):
        """Проверка при запуске программы — каждый раз, если она не выключена в «Настройках»."""
        if self.win.settings.get("update_auto", True):
            QTimer.singleShot(self.CHECK_DELAY_MS, self.check)

    def check(self, force=False):
        if self.checking or self.installing:
            return
        self.checking = True
        self.changed.emit()
        worker = Worker(core.latest_release, parent=self)
        worker.finished.connect(lambda release: self._on_checked(release, force))
        worker.failed.connect(lambda message: self._on_failed(message, force))
        worker.start()

    def _on_checked(self, release, force):
        self.checking = False
        self.release = release
        self.win.settings.update(update_checked=time.time(), update_release=release)
        core.save_settings(update_checked=self.win.settings["update_checked"], update_release=release)
        self.changed.emit()
        if self.available():
            # При запуске — раз за запуск и только если версию не пропустили; по «Проверить сейчас» — всегда
            if force or (self.pending() and self._notified != release["tag"]):
                self._notified = release["tag"]
                notify("info", self.win, f"Вышла новая версия {release['tag'].lstrip('vV')}",
                       f"У вас {self.current}" + (f" — так задано для проверки (BR_FAKE_VERSION), на самом деле "
                                                  f"{__version__}" if self.fake else "")
                       + ". Что нового и кнопка «Обновить» — в «Настройках», раздел «Обновление программы».",
                       12000, action=("Посмотреть обновление", self.open_settings))
        elif force:
            notify("success", self.win, "У вас последняя версия", f"{APP_NAME} {self.current}.")

    def open_settings(self):
        """Открыть «Настройки» на разделе «Обновление программы» — он там первый, прокручиваем к началу."""
        page = self.win.settings_page
        self.win.switchTo(page)
        areas = ([page] if isinstance(page, ScrollArea) else []) + page.findChildren(ScrollArea)
        for area in areas:
            area.verticalScrollBar().setValue(0)
        core.log.info("Обновление программы: открыты «Настройки» из уведомления")

    def _on_failed(self, message, force):
        self.checking = False
        self.changed.emit()
        text = message.split(": ", 1)[-1]
        core.log.warning("Обновление программы: GitHub не ответил: %s", text)
        if force:
            notify("warning", self.win, "Не удалось проверить обновления", text, 7000)

    def skip(self):
        if not self.release:
            return
        tag = self.release["tag"]
        self.win.settings["update_skip"] = tag
        core.save_settings(update_skip=tag)
        core.log.info("Обновление программы: версию %s попросили пропустить", tag)
        self.changed.emit()

    def set_auto(self, enabled):
        self.win.settings["update_auto"] = enabled
        core.save_settings(update_auto=enabled)
        core.log.info("Обновление программы: сама проверка %s", "включена" if enabled else "выключена")
        if enabled:
            self.check()


class NavHover(QObject):
    """Боковое меню — узкая полоска значков. Навели на неё мышь — меню плавно выезжает вправо поверх страницы:
    значок и подпись в одну строку. Увели мышь или выбрали пункт — прячется обратно. Страница под ним
    не сдвигается и не сжимается.

    Выбрали пункт — меню спряталось, а мышь всё ещё на полоске значков. Чтобы оно тут же не выехало снова,
    после выбора оно ждёт, пока мышь уйдёт с полоски, и только потом снова открывается при наведении."""

    OPEN_MS, CLOSE_MS = 150, 250  # мышь просто пролетела мимо — меню не дёргается
    BLOCK_MIN_MS = 500  # после выбора пункта меню не открывается хотя бы полсекунды, даже если мышь ушла и вернулась

    def __init__(self, panel, strip):
        super().__init__(panel)
        self.panel = panel
        self.strip = strip  # место меню в окне: шириной в полоску значков, не двигается и не прячется
        self.blocked = False  # меню закрылось выбором пункта — не открывать, пока мышь не уйдёт с полоски
        self.blocked_at = 0.0
        self._closing = False  # меню закрываем мы сами, потому что мышь ушла
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._check)
        # Пока меню «ждёт ухода мыши», раз в 120 мс смотрим, где курсор: события «мышь ушла» у панели
        # приходят не всегда — она во время закрытия меняет размер и родителя
        self.unblock_timer = QTimer(self)
        self.unblock_timer.setInterval(120)
        self.unblock_timer.timeout.connect(self._try_unblock)
        panel.installEventFilter(self)
        # Пункт выбрали — панель закрывает себя сама (self.collapse()). Подменяем методы у этой панели: так
        # видно, что её закрыл щелчок, а открыть её снова, пока мышь не ушла, не получится никаким путём
        self._collapse, self._expand = panel.collapse, panel.expand
        panel.collapse, panel.expand = self._on_collapse, self._on_expand

    def block(self, *_):
        """Выбрали пункт меню (или сменилась вкладка), а мышь на меню — не открывать его снова, пока мышь
        не уйдёт с полоски значков."""
        if self._hovered():
            if not self.blocked:
                core.log.info("Меню: пункт выбран, мышь на полоске — не открываю, пока мышь не уйдёт")
            self.blocked = True
            self.blocked_at = time.monotonic()
            self.timer.stop()
            self.unblock_timer.start()

    def _try_unblock(self):
        if not self.blocked:
            self.unblock_timer.stop()
        elif time.monotonic() - self.blocked_at >= self.BLOCK_MIN_MS / 1000 and not self._hovered():
            self.blocked = False
            self.unblock_timer.stop()

    def _on_collapse(self, *args):
        if not self._closing:
            self.block()
        self._collapse(*args)

    def _on_expand(self, *args):
        if self.blocked:
            core.log.info("Меню: пункт только что выбран, мышь ещё на полоске — снова не открываю")
            return
        self._expand(*args)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Enter:
            if not self.blocked:
                self.timer.start(self.OPEN_MS)
        elif event.type() == QEvent.Leave:
            self.timer.start(self.CLOSE_MS)
        return False

    def _hovered(self):
        """Курсор над полоской значков или над раскрытым меню. Проверяем по месту на экране, а не по событиям
        мыши: панель во время закрытия ненадолго прячется и меняет родителя. Полоска с запасом в 8 точек
        влево и 4 вправо — щелчок у самого края окна тоже считается."""
        pos = QCursor.pos()
        strip = QRect(self.strip.mapToGlobal(QPoint(0, 0)), self.strip.size()).adjusted(-8, 0, 4, 0)
        if self.strip.isVisible() and strip.contains(pos):
            return True
        if not self.panel.isVisible():
            return False
        return self.panel.rect().adjusted(-8, 0, 4, 0).contains(self.panel.mapFromGlobal(pos))

    def _check(self):
        animation = getattr(self.panel, "expandAni", None)
        if animation is not None and animation.state() == QAbstractAnimation.Running:
            self.timer.start(80)  # меню ещё выезжает или уезжает — решим, когда остановится
            return
        mode = getattr(self.panel, "displayMode", None)
        if self._hovered():
            if mode == NavigationDisplayMode.COMPACT and not self.blocked:
                self.panel.expand()
            return
        self._try_unblock()
        if mode == NavigationDisplayMode.MENU:
            self._closing = True
            try:
                self.panel.collapse()
            finally:
                self._closing = False


class MainWindow(FluentWindow):
    libraryChanged = Signal()
    portsChanged = Signal()  # порты добавили, убрали, изменили или пришли сведения об их обновлениях
    collectionsChanged = Signal()  # коллекцию создали, переименовали, удалили или поменяли в ней игры
    rouletteChanged = Signal()  # игру убрали из рулетки или сменили фильтры рулетки

    def __init__(self, startup=None, updated_from=""):
        super().__init__()
        startup = startup or Timing("окно")
        self.updated_from = updated_from  # программу только что обновили кнопкой «Обновить» — с этой версии
        if QWebEngineView:
            # Встроенный браузер переводит окно на отрисовку через видеокарту, и тогда прозрачные места
            # под эффектом Mica становятся чёрными: в светлой теме чернеют меню и фон страниц.
            # Поэтому с браузером окно рисует обычный сплошной фон своей темы.
            self.setMicaEffectEnabled(False)
        self.settings = core.load_settings()
        self.session_steam_id = ""  # SteamID из входа, когда запоминать его отказались: только до закрытия программы
        self.library = core.GameLibrary()
        self.load_message = self.library.load()
        self.ports = core.PortLibrary()
        self.ports_message = self.ports.load()
        self.discs = core.PortLibrary(core.DISCS_FILE, kind="disc")
        self.discs_message = self.discs.load()
        self.collections = core.CollectionLibrary()
        self.collections_message = self.collections.load()
        self.roulette = core.RouletteList()  # игры, убранные из рулетки (в своих разделах они остаются)
        self.roulette_message = self.roulette.load()
        self.steam_meta = core.load_steam_meta()  # жанры, годы и оценки игр Steam — для фильтров рулетки
        self.meta = MetaCollector(self)
        self.updater = AppUpdater(self)  # новая версия программы на GitHub
        self._update_dot = None
        self.game_origin = None  # коллекция, из которой открыли страницу игры (для кнопки «К коллекции»)

        self.music = MusicPlayer(self.settings["music_path"], self.settings["music_shuffle_mode"],
                                 self.settings["music_repeat"] == "one", self)
        self.music.set_volume(int(self.settings["music_volume"]))
        self.sounds = ReelSounds(bool(self.settings["sounds_enabled"]), self)
        self.covers = CoverPrefetcher(self)
        self.play_tracker = PlayTracker(self)  # время в портах и дисковых играх
        startup.mark("список игр и музыка")

        self.home_page = HomePage(self)
        self.library_page = LibraryPage(self)
        self.native_page = NativePortsPage(self)
        self.disc_page = DiscGamesPage(self)
        self.collections_page = CollectionsPage(self)
        self.music_page = MusicPage(self)
        self.web_page = WebPage(self) if QWebEngineView else None
        self.shop_page = ShopPage(self)  # «Найти игры на Plati.Market»
        self.settings_page = SettingsPage(self)
        self.logs_page = LogsPage(self)
        self.about_page = AboutPage(self)
        startup.mark("вкладки")
        self.addSubInterface(self.home_page, FIF.GAME, "Рулетка")
        self.addSubInterface(self.library_page, FIF.LIBRARY, "Игры Steam")
        self.addSubInterface(self.native_page, FIF.APPLICATION, "Игры Native Port's")
        self.addSubInterface(self.disc_page, DISC_ICON, "Дисковые игры")
        self.addSubInterface(self.collections_page, COLLECTION_ICON, "Коллекции")
        self.addSubInterface(self.music_page, FIF.MUSIC, "Саундтреки")
        if self.web_page:
            self.addSubInterface(self.web_page, FIF.GLOBE, "Браузер")
        self.addSubInterface(self.shop_page, FIF.SHOPPING_CART, "Найти игры на Plati.Market")
        self.addSubInterface(self.logs_page, FIF.DOCUMENT, "Логи",
                             position=NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.settings_page, FIF.SETTING, "Настройки",
                             position=NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.about_page, FIF.INFO, "О программе",
                             position=NavigationItemPosition.BOTTOM)
        self._fit_navigation()
        self._connect_section_clicks()
        self.updater.changed.connect(self._update_badge)
        self._update_badge()  # обновление нашли в прошлый раз и ещё не поставили — точка сразу
        if self.home_page.browser:
            self.home_page.browser.preload(self)  # до показа окна — иначе оно мигнёт при первой ссылке
            startup.mark("браузер")
        self.libraryChanged.connect(self.on_library_changed)
        self.libraryChanged.connect(lambda: self.meta.schedule())  # импорт и синхронизация: новым играм — сведения
        self.portsChanged.connect(self.on_library_changed)
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
        if self.covers.enqueue(self.all_games()):
            self.home_page.show_loading()
        self.music_page.reload_soundtracks()
        QTimer.singleShot(300, self._after_show)

    def _time_tab_switch(self, index):
        """Замер переключения вкладки: от смены до момента, когда окно снова свободно (после её showEvent)."""
        page = self.stackedWidget.widget(index)
        key = page.objectName() if page else str(index)
        panel = getattr(self.navigationInterface, "panel", self.navigationInterface)
        item = getattr(panel, "items", {}).get(key)
        widget = getattr(item, "widget", item)  # подпись в меню: «Саундтреки», …
        timing = Timing(f"вкладка «{widget.text() if hasattr(widget, 'text') else key}»")
        QTimer.singleShot(0, lambda: timing.done("показана"))

    def _fit_navigation(self):
        """Боковое меню — полоской значков; при наведении раскрывается поверх страницы (NavHover). Ширина
        раскрытого меню — по самой длинной подписи, чтобы «Игры Native Port's» помещались в одну строку."""
        nav = self.navigationInterface
        panel = getattr(nav, "panel", None)
        if panel is None or NavigationDisplayMode is None:
            return
        if hasattr(nav, "setReturnButtonVisible"):
            nav.setReturnButtonVisible(False)  # «Назад» в меню не нужен: у страниц свои кнопки возврата
        if hasattr(nav, "setMinimumExpandWidth"):
            nav.setMinimumExpandWidth(100000)  # при любой ширине окна меню выезжает поверх, а не раздвигает страницу
        widgets = [getattr(item, "widget", None) for item in getattr(panel, "items", {}).values()]
        # Подпись в меню рисуется чуть шире шрифта самой кнопки — к ширине самой длинной подписи («Найти игры
        # на Plati.Market») запас на значок, поля и эту разницу: меньше — последняя буква уходит за край
        texts = [QFontMetrics(w.font()).horizontalAdvance(w.text()) for w in widgets if hasattr(w, "text")]
        if hasattr(nav, "setExpandWidth"):
            nav.setExpandWidth(max([150] + texts) + 94)
        if getattr(self, "_nav_hover", None) is None:  # второй раз не вешаем — иначе два «наблюдателя» спорят
            self._nav_hover = NavHover(panel, nav)
            # Сменилась вкладка щелчком по меню — оно закрывается и не выезжает снова, пока мышь на полоске
            self.stackedWidget.currentChanged.connect(self._nav_hover.block)

    def _connect_section_clicks(self):
        """Щелчок по разделу в боковом меню — его главная страница. Открыта страница игры — раздел возвращается
        к своему списку, как по кнопкам «К портам», «К коллекциям», «К рулетке». И когда раздел уже выбран."""
        for page in (self.home_page, self.native_page, self.disc_page, self.collections_page):
            item = self.navigationInterface.widget(page.objectName())
            if item is None or not hasattr(item, "clicked"):
                continue
            # Меню само переключает вкладку по тому же щелчку — сбрасываем раздел сразу после этого
            item.clicked.connect(lambda *_, p=page: QTimer.singleShot(0, lambda: self.show_section_main(p)))

    def show_section_main(self, page):
        if not page.isVisible():
            return  # вкладку так и не открыли (например, поверх было окно)
        if page is self.home_page:
            if page.stack.currentWidget() is page.game_page:
                page.game_page.stop_video()
                page.show_reel()
            elif page.browser is not None and page.stack.currentWidget() is page.browser \
                    and page._browser_return is None:
                page.show_reel()  # ссылка с карточки игры; вход в магазин Steam так не прерываем
            else:
                return
        elif page is self.collections_page:
            if page.stack.currentWidget() is not page.detail_view:
                return
            page.show_list()
        else:
            if page.stack.currentWidget() is not page.page:
                return
            page.show_list()
        core.log.info("Меню: «%s» — к главной странице раздела", page.objectName())

    def _update_badge(self):
        """Точка на значке «Настройки», пока есть обновление, которое не поставили и не пропустили."""
        want = self.updater.pending()
        if want == (self._update_dot is not None):
            return
        if not want:
            self._update_dot.deleteLater()
            self._update_dot = None
            return
        if DotInfoBadge is None:
            return  # в этой версии библиотеки интерфейса значков нет — обновление видно в «Настройках»
        item = self.navigationInterface.widget(self.settings_page.objectName())
        if item is None:
            return
        try:
            self._update_dot = DotInfoBadge.attension(parent=item.parent(), target=item,
                                                      position=InfoBadgePosition.NAVIGATION_ITEM)
            self._update_dot.setFixedSize(8, 8)  # родная точка в 4 точки на полоске значков почти не видна
        except (TypeError, AttributeError) as e:
            core.log.warning("Точка «есть обновление» на меню не нарисовалась: %s", e)
            self._update_dot = None

    def _after_show(self):
        if self.load_message:
            notify("info", self, "Список игр", self.load_message, duration=8000)
        if self.ports_message:
            notify("warning", self, "Список портов", self.ports_message, duration=8000)
        if self.discs_message:
            notify("warning", self, "Список дисковых игр", self.discs_message, duration=8000)
        if self.collections_message:
            notify("warning", self, "Коллекции", self.collections_message, duration=8000)
        if self.roulette_message:
            notify("warning", self, "Список рулетки", self.roulette_message, duration=8000)
        # Жанры, годы и отзывы, которые не успели собрать в прошлый раз, — когда окно уже открылось и обложки пошли
        self.meta.schedule(8000)
        self.set_music_enabled(bool(self.settings["music_enabled"]), save=False)
        if self.home_page.stack.currentWidget() is self.home_page.reel_view:
            self.home_page.reel.play_intro()
        if core.griddb_key():
            self.covers.upgrade(self.library.games)  # каждая обложка проверяется один раз, потом это мгновенно
        # Новые версии портов на GitHub — не чаще раза в несколько часов, молча, если нового нет
        QTimer.singleShot(5000, lambda: self.native_page.check_updates(quiet=True))
        self.updater.schedule()  # и новая версия самой программы — при каждом запуске
        if self.updated_from:
            notify("success", self, f"Обновлено до версии {__version__}",
                   f"Было: {self.updated_from}. Что нового — на странице релиза («Настройки» → «Обновление "
                   "программы»).", 8000)
        if self.needs_steam_setup():
            QTimer.singleShot(400, self.run_steam_setup)

    @staticmethod
    def needs_steam_setup():
        """Первый запуск: Steam ещё не подключали, не нажимали «Пропустить» и не выбрали «Без ключа».
        Кто обновился с ключом и SteamID, мастер не видит."""
        settings = core.load_settings()
        if settings["steam_api_key"] and settings["steam_id"]:
            return False
        return settings.get("steam_setup") not in ("done", "skipped", "no_key")

    def run_steam_setup(self):
        """Мастер подключения к Steam: при первом запуске, из «Настроек» и из «Импорт из Steam» без ключа."""
        dialog = SteamSetupDialog(self)
        core.log.info("Открыто окно «Подключение к Steam»")
        dialog.exec()
        connected, want_import, no_key = dialog.connected, dialog.want_import, dialog.no_key
        repeated, session_id, grid_saved = dialog.repeated, dialog.session_id, dialog.grid_saved
        skipped = dialog.skipped
        core.log.info("Окно «Подключение к Steam» закрыто: %s",
                      "подключено" if connected else "без ключа" if no_key else "«Пропустить»" if skipped
                      else "закрыто без выбора")
        dialog.deleteLater()
        self.settings = core.load_settings()
        if grid_saved:
            self.on_griddb_key_changed()
        if connected:
            self.session_steam_id = ""
        elif no_key:
            self.session_steam_id = session_id  # SteamID не запомнили — помним только до закрытия программы
        if no_key:
            if repeated:
                return  # «Без ключа» уже выбирали — всё как было, лишних уведомлений не нужно
            self.settings_page.show_keys()
            self.music_page.build_sources()  # разделы саундтреков по аккаунтам — по запомненному SteamID
            if QWebEngineView and not core.owned_apps():
                self.offer_store_login()
            else:
                notify("success", self, "Работаем без ключа",
                       "Игры добавляются на вкладке «Игры Steam» → «Добавить» — с поиском по магазину Steam.", 7000)
            return
        if not connected:
            # Отказ запоминаем только по «Пропустить». Закрыли мастер иначе — при следующем запуске он снова
            if skipped and not (self.settings["steam_api_key"] and self.settings["steam_id"]) \
                    and self.settings.get("steam_setup") != "no_key":  # «Без ключа» с прошлого раза остаётся
                core.save_settings(steam_setup="skipped")  # при следующем запуске не навязываемся
                self.show_steam_later()
            return
        self.settings_page.show_keys()
        self.library_page.set_import_busy(False)
        self.music_page.build_sources()  # пометки «скачан другим аккаунтом» — по новому SteamID
        if want_import:
            self.switchTo(self.library_page)
            self.library_page.import_from_steam()
        elif self.home_page.game_page.game and self.home_page.game_page.isVisible():
            self.home_page.game_page.show_game(self.home_page.game_page.game)  # с ключом появятся достижения

    def run_griddb_setup(self):
        """«Настройки» → «Ключ SteamGridDB»: что даёт ключ, как его получить, проверка и сохранение."""
        dialog = SteamGridDialog(self)
        core.log.info("Открыто окно «Ключ SteamGridDB»")
        dialog.exec()
        saved = dialog.saved
        dialog.deleteLater()
        core.log.info("Окно «Ключ SteamGridDB» закрыто: %s", "ключ сохранён" if saved else "без изменений")
        if saved:
            self.on_griddb_key_changed()
            notify("success", self, "Ключ SteamGridDB сохранён",
                   "Обложки, фоны и живые фоны, которых нет в Steam, программа теперь возьмёт из SteamGridDB.", 6000)

    def on_griddb_key_changed(self):
        """Ключ SteamGridDB добавили, сменили или стёрли: игры без картинок ищутся заново."""
        self.settings = core.load_settings()
        self.settings_page.show_keys()
        try:
            core.clear_artwork_misses()
            core.forget_cover_checks()  # обложки, собранные Steam из шапки, ищем в SteamGridDB заново
        except OSError as e:
            core.log.warning("Не удалось забыть ненайденные фоны и логотипы: %s", e)
        if self.covers.retry(self.library.games) >= LOADING_THRESHOLD:
            self.home_page.show_loading()
        self.covers.upgrade(self.library.games)
        game_page = self.home_page.game_page
        if game_page.game and game_page.isVisible():
            game_page.show_game(game_page.game)  # фон, логотип и живой фон — уже с новым ключом

    def offer_store_login(self):
        """После «Без ключа»: что есть на аккаунте, Steam без ключа показывает только после входа в магазин.
        Шаг необязательный — можно войти позже прямо из окна «Добавить игру»."""
        if not confirm(self, "Показать, что есть на вашем аккаунте?",
                       "Без ключа Steam показывает ваши игры и саундтреки только тому, кто вошёл в магазин Steam — "
                       "здесь, во встроенном браузере. Тогда при добавлении игр будет видно, какие уже есть на "
                       "аккаунте, а в «Саундтреках» — купленные альбомы.\n\n"
                       "Удобнее и безопаснее — по QR-коду в мобильном приложении Steam: пароль в программу не "
                       "вводится вовсе. Вход запомнится только на этом ПК, стереть — «Настройки» → «Выйти из "
                       "магазина Steam». Можно и позже: «Игры Steam» → «Добавить» → «Показать, что есть на моём аккаунте».",
                       "Войти в магазин"):
            notify("success", self, "Работаем без ключа",
                   "Игры добавляются на вкладке «Игры Steam» → «Добавить» — с поиском по магазину Steam.", 7000)
            return
        self.switchTo(self.library_page)
        self.music_page.sync_steam(after=self.library_page.offer_owned_games, back_text="К играм Steam",
                                   return_page=self.library_page, ask=False)

    def show_steam_later(self):
        """Окно после «Пропустить»: что работает без Steam. «Добавить игру» — сразу к поиску игры."""
        dialog = SteamLaterDialog(self)
        core.log.info("Открыто окно «Steam можно подключить позже»")
        add = bool(dialog.exec())
        dialog.deleteLater()
        if add:
            self.switchTo(self.library_page)
            self.library_page.add_games()

    def on_library_changed(self):
        if self.covers.enqueue(self.all_games()) >= LOADING_THRESHOLD:
            self.home_page.show_loading()

    def all_games(self):
        """Игры Steam, порты и дисковые игры — для обложек."""
        return self.library.games + self.ports.ports + self.discs.ports

    def name_in_use(self, name, skip=None):
        """Название есть у какой-то игры в программе (кроме skip): тогда её обложку не трогаем — она общая."""
        if self.library.find(name):
            return True
        key = name.casefold()
        return any(p is not skip and p["name"].casefold() == key for p in self.ports.ports + self.discs.ports)

    # Что крутит рулетка: игры Steam, порты, дисковые — по отдельности и вместе. Ключ — источники через «+»
    POOL_SOURCES = ("steam", "ports", "discs")

    def pool_sources(self):
        """Источники, в которых есть игры, — из них и можно выбирать."""
        return [s for s, games in zip(self.POOL_SOURCES, (self.library.games, self.ports.ports, self.discs.ports))
                if games]

    def roulette_pool(self):
        """Что крутит рулетка сейчас: «steam», «steam+ports», «discs», «col:<номер коллекции>» и т. п. Выбранного
        источника больше нет (порты убрали, коллекцию удалили или опустошили) — остаётся то, что есть из
        выбранного, а если ничего — всё, где есть игры."""
        pool = self.settings.get("roulette_pool") or "steam"
        pool = {"mix": "steam+ports"}.get(pool, pool)  # так «Steam и порты» назывались в первых сборках 2.4
        if pool.startswith("col:"):
            collection = self.collections.find(pool[4:])
            if collection and self.collection_games(collection):
                return pool
            pool = "steam"
        available = self.pool_sources()
        chosen = [s for s in self.POOL_SOURCES if s in pool.split("+") and s in available]
        return "+".join(chosen or available) or "steam"

    def roulette_games(self):
        """Игры для барабана — по выбору рядом с «Крутить». В списке те же словари, что в играх и портах.
        Убранные из рулетки сюда не попадают (кроме коллекций: там крутятся все их игры), а игры Steam — ещё
        и по фильтрам, если они включены. Пройденные порты и дисковые игры не крутятся нигде: запускать их нечем —
        вернули в игру, снова крутятся (а убранные вручную так и остаются убранными)."""
        pool = self.roulette_pool()
        if pool.startswith("col:"):
            return [g for g in self.collection_games(self.collections.find(pool[4:])) if not g.get("completed_at")]
        lists = {"steam": self.library.games, "ports": self.ports.ports, "discs": self.discs.ports}
        games = []
        for source in pool.split("+"):
            games += [g for g in lists[source] if not self.roulette.has(g) and not g.get("completed_at")]
        if self.filters_on():
            filters, meta = self.roulette_filters(), self.steam_meta
            games = [g for g in games if g.get("appid") and core.meta_matches(meta.get(str(int(g["appid"]))), filters)]
        return games

    def roulette_filters(self):
        filters = self.settings.get("roulette_filters")
        return filters if isinstance(filters, dict) else {}

    def filters_on(self):
        """Фильтры включены и действуют: они только для игр Steam — когда рулетка крутит только их."""
        return self.roulette_pool() == "steam" and core.filters_active(self.roulette_filters())

    def back_to_collection(self):
        """«К коллекции» со страницы игры: на вкладку «Коллекции», в ту коллекцию, где эта игра выделена."""
        page = self.collections_page
        home = self.home_page
        collection, game = None, None
        if home.game_page.isVisible():
            collection, game = home.game_page.origin, home.game_page.game
            home.game_page.stop_video()
        for ports_page in (self.native_page, self.disc_page):
            if ports_page.page.isVisible():
                collection, game = ports_page.page.origin, ports_page.page.port
                ports_page.show_list()  # страница игры не остаётся висеть на её вкладке
        self.switchTo(page)
        if collection is not None and any(c is collection for c in self.collections.collections):
            page.open_collection(collection)
            page.reveal(game)
        else:
            page.show_list()

    def collection_games(self, collection):
        """Игры коллекции, которые сейчас есть в списках, — те же словари, что в играх, портах и дисковых."""
        if not collection:
            return []
        by_appid = {int(g["appid"]): g for g in self.library.games if g.get("appid")}
        games, seen = [], set()
        for ref in collection["items"]:
            if ref["kind"] == "steam":
                game = by_appid.get(ref["appid"]) if "appid" in ref else self.library.find(ref["name"])
            else:
                game = (self.discs if ref["kind"] == "disc" else self.ports).find(ref["id"])
            if game is not None and id(game) not in seen:
                seen.add(id(game))
                games.append(game)
        return games

    def save_collections(self):
        try:
            self.collections.save()
            return True
        except OSError as e:
            notify("error", self, "Не удалось сохранить коллекции", str(e))
            return False

    def save_ports(self, lib=None):
        lib = lib or self.ports
        try:
            lib.save()
            return True
        except OSError as e:
            notify("error", self, "Не удалось сохранить список " +
                   ("дисковых игр" if lib is self.discs else "портов"), str(e))
            return False

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
        if not installed and not self.confirm_steam_account("установит игру"):
            return
        core.log.info("%s «%s» (%s)", "Запуск" if installed else "Установка", game["name"], appid)
        if installed:
            self.open_in_steam(f"steam://run/{appid}", game, "run")
        else:
            self.open_in_steam(f"steam://install/{appid}", game, "install")

    # Команды Steam, которые страница во встроенном браузере может передать: установка, запуск и страница
    # в магазине — только с номером приложения и без параметров. Остальное (параметры запуска, удаление,
    # прочие команды) программа не передаёт: так страница не заставит Steam сделать что-то за спиной пользователя.
    STEAM_LINK_RE = re.compile(r"steam://(install|run|store|nav/games/details)/(\d+)/?", re.IGNORECASE)

    def open_steam_link(self, url):
        """Ссылка steam:// со страницы во встроенном браузере — например, «Установить» в магазине Steam."""
        text = url.toString()
        match = self.STEAM_LINK_RE.fullmatch(text)
        if not match:
            core.log.warning("Встроенный браузер: страница попросила Steam выполнить «%s» — не передаю", text[:120])
            notify("warning", self, "Команда для Steam не передана",
                   "Страница попросила Steam выполнить необычную команду. Из встроенного браузера программа "
                   "передаёт Steam только установку, запуск и страницу в магазине. Если команда нужна — откройте "
                   "страницу «Во внешнем браузере».", 9000)
            return
        action, appid = match.group(1).lower(), int(match.group(2))
        if action == "install" and not self.confirm_steam_account("установит выбранное"):
            return
        core.log.info("Встроенный браузер: передаю в Steam %s", text)
        if action == "install":
            self.music_page.expect_install(appid)  # если это саундтрек — вернёмся к «Саундтрекам» и дождёмся его
        worker = Worker(core.open_url, text, parent=self)  # в фоне: Steam может откликаться долго
        worker.failed.connect(lambda message: notify("error", self, "Не удалось открыть Steam",
                                                     "Проверьте, что Steam установлен."))
        worker.start()

    def my_steam_id(self):
        """Ваш SteamID: запомненный в настройках или — если запоминать отказались — из входа в этом запуске."""
        return self.settings.get("steam_id") or getattr(self, "session_steam_id", "")

    def owns(self, appid):
        """Игра есть на вашем аккаунте: по последней синхронизации покупок или потому, что её установил
        на этот ПК ваш аккаунт (по файлам Steam — без ключа и без входа)."""
        if appid in core.owned_apps():
            return True
        owner, mine = core.installed_owner(appid), self.my_steam_id()
        return bool(owner and mine and owner == mine)

    def confirm_steam_account(self, what):
        """В клиент Steam на ПК вошёл не тот аккаунт, что подключён к программе, — спрашиваем, прежде чем
        передавать ему команду: он выполнит её для себя. True — продолжать."""
        other = core.other_steam_account(core.load_settings()["steam_id"] or getattr(self, "session_steam_id", ""))
        if not other:
            return True
        core.log.info("В клиенте Steam вошёл другой аккаунт (SteamID %s), чем подключён к программе",
                      core.short_steam_id(other))
        name = core.steam_account_names().get(other)
        who = f"«{name}», SteamID {core.short_steam_id(other)}" if name else f"SteamID {core.short_steam_id(other)}"
        return confirm(self, "В Steam вошёл другой аккаунт",
                       f"В Steam на этом ПК сейчас вошёл не тот аккаунт, что подключён к программе "
                       f"(в Steam — {who}). Steam {what} для него, а если "
                       f"у него этого нет — предложит купить. Чтобы получить в свой аккаунт, сначала войдите "
                       f"в Steam им.", "Всё равно открыть Steam")

    def set_music_enabled(self, enabled, save=True):
        self.music.set_enabled(enabled)  # кнопки плееров и переключатель в «Настройках» — по сигналу stateChanged
        if save:
            self.settings["music_enabled"] = enabled
            core.save_settings(music_enabled=enabled)

    def toggle_music(self):
        self.set_music_enabled(not self.music.enabled)

    def show_playing_music(self):
        """Нота в мини-плеере: вкладка «Саундтреки», открытая на альбоме, который играет."""
        playing = self.music_page.playing_source()
        if playing:
            self.music_page.select_source(playing)
        self.switchTo(self.music_page)

    def open_web(self, url):
        """Открыть адрес на вкладке «Браузер»; без встроенного браузера — во внешнем."""
        if not self.web_page or external_only(url):
            QDesktopServices.openUrl(QUrl(url))
            return
        self.web_page.open(url)
        self.switchTo(self.web_page)

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
    """Любая непойманная ошибка пишется в лог, а пользователь видит уведомление вместо молчаливого сбоя.

    Уведомление само может задеть то место, где случилась ошибка (затемнение уводит мышь со списка — и снова та же
    ошибка). Поэтому пока одно уведомление об ошибке показывается, новое не открывается, а одинаковые ошибки
    подряд уведомляют не чаще раза в ERROR_NOTICE_PAUSE секунд — иначе программа зацикливается и не отвечает."""
    ERROR_NOTICE_PAUSE = 30
    state = {"busy": False, "last": "", "at": 0.0}

    def on_error(exc_type, exc, tb):
        core.log.critical("Непредвиденная ошибка", exc_info=(exc_type, exc, tb))
        if threading.current_thread() is not threading.main_thread() or state["busy"]:
            return
        text = f"{exc_type.__name__}: {core.mask_secrets(exc)}"
        now = time.monotonic()
        if text == state["last"] and now - state["at"] < ERROR_NOTICE_PAUSE:
            return
        state.update(busy=True, last=text, at=now)
        try:
            notify("error", window, "Что-то пошло не так", f"{text}\nПодробности — на вкладке «Логи».",
                   duration=10000)
        except Exception:  # уведомление не открылось — ошибка уже в логе, главное не упасть здесь
            core.log.exception("Уведомление об ошибке не показалось")
        finally:
            state["busy"] = False

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
    # После «Обновить»: ждём, пока закроется прежняя версия, и убираем её файл
    updated_from = core.finish_update()
    if updated_from:
        core.log.info("Программа обновлена: %s → %s", updated_from, __version__)
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
    window = MainWindow(startup, updated_from)
    install_error_hooks(window)
    app.aboutToQuit.connect(window.play_tracker.flush)  # игра ещё идёт — время до закрытия программы засчитывается
    window.show()
    QTimer.singleShot(0, lambda: startup.done("окно на экране"))  # сработает, когда окно уже нарисовано
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
