"""Логика без интерфейса: список игр, обложки, Steam, настройки."""

import base64
import functools
import hashlib
import hmac
import html
import http.server
import io
import json
import logging
import math
import os
import random
import re
import secrets
import struct
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

# --- Пути -----------------------------------------------------------------
# Данные лежат рядом со скриптом (или рядом с .exe после сборки PyInstaller)
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ресурсы, вшитые в .exe, распаковываются во временную папку _MEIPASS
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)

GAMES_FILE = os.path.join(BASE_DIR, "games.json")
LEGACY_EXCEL_FILE = os.path.join(BASE_DIR, "SteamGames.xlsx")
COVERS_DIR = os.path.join(BASE_DIR, "covers")
API_KEY_FILE = os.path.join(BASE_DIR, "api_key.txt")  # ключ SteamGridDB
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")  # ключ Steam, SteamID, звук, тема
NOT_FOUND_FILE = os.path.join(BASE_DIR, "covers_not_found.json")  # игры, для которых обложку не нашли
LOGS_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOGS_DIR, "app.log")
HANG_LOG_FILE = os.path.join(LOGS_DIR, "hang.log")  # стеки потоков, если окно перестало отвечать
NAMES_CACHE_FILE = os.path.join(BASE_DIR, "steam_app_names.json")  # названия семейных игр
NAMES_CACHE_TTL = 7 * 24 * 3600  # неделя
DETAILS_CACHE_FILE = os.path.join(BASE_DIR, "steam_details.json")  # карточки игр из магазина Steam
DETAILS_CACHE_TTL = 14 * 24 * 3600  # две недели
CACHE_DIR = os.path.join(BASE_DIR, "cache")  # фоны и логотипы для страниц игр
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus")
ICON_NAME = "icon.ico"  # рисуется скриптом make_icon.py
MUSIC_NAME = "apptheme.mp3"

COVER_SIZE = (300, 450)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")

STEAMGRIDDB_API_URL = "https://www.steamgriddb.com/api/v2"
STEAM_OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
STEAM_APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
STEAM_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_COVER_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
STEAM_HERO_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_hero.jpg"
STEAM_LOGO_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/logo.png"
STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STEAM_SCHEMA_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
STEAM_PLAYER_ACHIEVEMENTS_URL = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
STEAM_GLOBAL_ACHIEVEMENTS_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
REQUEST_TIMEOUT = 10  # секунд

DEFAULT_SETTINGS = {
    "steam_api_key": "",
    "griddb_api_key": "",  # ключ SteamGridDB (обложки, фоны, логотипы); до 2.3 лежал открытым в api_key.txt
    "steam_id": "",
    "music_enabled": True,
    "music_volume": 40,  # 0–100
    "music_path": "",  # что играет: свой трек, папка или саундтрек Steam; пусто — встроенный apptheme.mp3
    "music_custom": "",  # своя папка или трек — остаётся в списке, даже когда играет саундтрек
    "music_shuffle_mode": "off",  # вперемешку: off, all — вся музыка (кроме встроенной), soundtracks — саундтреки
    "music_repeat": "all",  # all — папка по кругу, one — повторять один трек
    "sounds_enabled": True,  # щелчки барабана и звук остановки
    "theme": "auto",  # auto | light | dark
    # Подключение к Steam при первом запуске: "" — ещё не было, done — подключено, skipped — «Пропустить»,
    # no_key — вошли, но Steam не выдал ключ, и выбрали «Без ключа» (SteamID — только если разрешили запомнить)
    "steam_setup": "",
    # Последний выбор в окне импорта — по нему работает быстрая синхронизация
    "import_done": False,
    "import_include": [],  # id коллекций Steam
    "import_skip_hidden": True,
    "import_unplayed": False,
    "import_replace": False,
    "import_family": True,  # брать игры семейной группы, подтверждённые Steam
    "import_unverified": False,  # и «семейные» из своих коллекций, которые Steam не подтвердил (без входа в магазин)
    "library_view": "list",  # вкладка «Игры»: list — списком, grid — плиткой с обложками
    # Перевод описаний и отзывов: auto — Google, а когда он не отвечает — модулем на этом ПК;
    # local — только модулем на ПК (текст не уходит в интернет); off — не переводить
    "translate_mode": "auto",
}


def find_resource(name):
    """Файл рядом с программой важнее вшитого в .exe — так его легко заменить."""
    for folder in (BASE_DIR, RESOURCE_DIR):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            return path
    return None


def list_audio_files(folder):
    """Все аудиофайлы в папке и её подпапках."""
    files = []
    for root, _, names in os.walk(folder):
        files += [os.path.join(root, n) for n in names if n.lower().endswith(AUDIO_EXTENSIONS)]
    return files


def natural_key(text):
    """Сортировка «как в проводнике»: трек 2 идёт раньше трека 10."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def folder_tracks(folder):
    """Треки папки по порядку. Альбомы часто лежат сразу в MP3 и FLAC — одинаковые треки берём один раз."""
    best = {}
    for path in list_audio_files(folder):
        stem, ext = os.path.splitext(os.path.basename(path))
        rank = AUDIO_EXTENSIONS.index(ext.lower())  # mp3 первым: его открывают оба проигрывателя Qt
        if stem.casefold() not in best or rank < best[stem.casefold()][0]:
            best[stem.casefold()] = (rank, path)
    return sorted((path for _, path in best.values()), key=lambda p: natural_key(os.path.relpath(p, folder)))


def track_titles(paths):
    """Названия треков для плеера: без общего начала («The Medium Original Soundtrack - ») и номера трека."""
    stems = [os.path.splitext(os.path.basename(p))[0] for p in paths]
    prefix = os.path.commonprefix(stems) if len(stems) > 1 else ""
    cut = prefix.rfind(" - ")
    prefix = prefix[:cut + 3] if cut >= 0 else ""
    titles = []
    for stem in stems:
        title = re.sub(r"^\d{1,3}(?:\s*[-._)]\s*|\s+)(?=\S)", "", stem[len(prefix):])
        titles.append(title.strip() or stem)
    return titles


def same_path(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def is_steam_music(path):
    """Лежит ли папка в steamapps\\music — там Steam хранит скачанные саундтреки."""
    sep = os.sep
    return f"{sep}steamapps{sep}music{sep}" in os.path.normcase(os.path.normpath(path)) + sep


# --- Логи -------------------------------------------------------------------
log = logging.getLogger("MySteamRandom")

# Ключи API и временный токен входа попадают в адреса запросов (…?key=…, …?access_token=…) и тексты ошибок —
# в логах их прячем
_SECRET_RE = re.compile(r"(key=|access_token=|webapi_token\"?:\s*\"?|Bearer\s+)[^&\s'\"]+", re.IGNORECASE)


def mask_secrets(text):
    return _SECRET_RE.sub(r"\1***", str(text))


class _MaskSecretsFilter(logging.Filter):
    def filter(self, record):
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = mask_secrets(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging():
    """Пишет логи в logs/app.log рядом с программой (до 3 файлов по 1 МБ)."""
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        return  # нет прав на запись — работаем без логов
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(_MaskSecretsFilter())
    log.addHandler(handler)


def read_log(max_lines=3000):
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            text = "".join(f.readlines()[-max_lines:])
    except OSError:
        text = ""
    try:
        with open(HANG_LOG_FILE, encoding="utf-8", errors="replace") as f:
            hangs = f.read().strip()
    except OSError:
        hangs = ""
    if hangs:  # где застревала программа — в конце, чтобы попадало в «Копировать всё»
        text += f"\n===== Зависания (logs\\{os.path.basename(HANG_LOG_FILE)}) =====\n{hangs}\n"
    return text


def clear_log():
    """Начать журнал заново: текущий уходит в app.log.1 (прошлый app.log.1 — в app.log.2), ничего не стирается."""
    handlers = [h for h in log.handlers if isinstance(h, RotatingFileHandler)]
    if handlers:
        for handler in handlers:
            handler.doRollover()
    elif os.path.exists(LOG_FILE):
        os.replace(LOG_FILE, LOG_FILE + ".1")
    if os.path.exists(HANG_LOG_FILE):
        # Файл держит открытым сторож зависаний, поэтому не переименовываем, а дописываем в архив и обнуляем
        with open(HANG_LOG_FILE, encoding="utf-8", errors="replace") as f:
            hangs = f.read()
        if hangs.strip():
            with open(HANG_LOG_FILE + ".1", "a", encoding="utf-8") as f:
                f.write(hangs)
        with open(HANG_LOG_FILE, "w", encoding="utf-8"):
            pass


# --- Работа с файлами -------------------------------------------------------
def read_text(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def save_json(path, data):
    # Пишем во временный файл и подменяем, чтобы не испортить данные при сбое
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


# --- Шифрование ключей ------------------------------------------------------
# Ключи Steam Web API и SteamGridDB хранятся в settings.json не открытым текстом, а зашифрованными средствами
# Windows (DPAPI): расшифровать их может только эта учётная запись Windows на этом ПК. Скопированный на другой
# компьютер settings.json ключей не выдаст. Остальной код ничего об этом не знает: load_settings отдаёт ключи уже
# расшифрованными, а save_settings сам шифрует их перед записью.
SECRET_FIELDS = {"steam_api_key": "steam_api_key_protected", "griddb_api_key": "griddb_api_key_protected"}
_DPAPI_ENTROPY = b"Backlog Roulette: Steam Web API key"
_unprotected = {}  # расшифрованные ключи: load_settings вызывается часто, DPAPI каждый раз не дёргаем
_settings_lock = threading.RLock()


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    data_in, keep_in = blob(data)
    entropy, keep_entropy = blob(_DPAPI_ENTROPY)
    data_out = DataBlob()
    crypt32 = ctypes.windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ui_forbidden = 0x1  # никаких окон Windows: ошибка — значит ошибка
    if not function(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None, ui_forbidden,
                    ctypes.byref(data_out)):
        raise OSError(ctypes.WinError())
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(data_out.pbData, ctypes.c_void_p))


def can_protect_secrets():
    return sys.platform == "win32"


def protect_secret(text):
    return base64.b64encode(_dpapi(text.encode("utf-8"), protect=True)).decode("ascii")


def unprotect_secret(stored):
    """Расшифрованный ключ или "" — если он зашифрован на другом ПК или другой учётной записью Windows."""
    if stored in _unprotected:
        return _unprotected[stored]
    try:
        text = _dpapi(base64.b64decode(stored), protect=False).decode("utf-8")
    except (OSError, ValueError) as e:
        log.warning("Ключ из settings.json не расшифровался (%s): похоже, файл перенесли с другого ПК. "
                    "Введите ключи заново: «Настройки» → «Войти через Steam» и поле SteamGridDB", e)
        text = ""
    _unprotected[stored] = text
    return text


def _read_settings_file():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_settings(settings):
    data = dict(settings)
    for field, protected in SECRET_FIELDS.items():
        key = data.pop(field, "")
        data.pop(protected, None)
        if key and can_protect_secrets():
            data[protected] = protect_secret(key)
            data[field] = ""
        else:
            data[field] = key
    save_json(SETTINGS_FILE, data)


def load_settings():
    with _settings_lock:  # обложки качаются в нескольких потоках — переносить ключ должен кто-то один
        return _load_settings()


def _load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings.update(_read_settings_file())
    rewrite = False
    for field, protected in SECRET_FIELDS.items():
        stored = settings.pop(protected, "")
        if stored:
            settings[field] = unprotect_secret(stored)
        elif settings[field] and can_protect_secrets():
            rewrite = True  # до 2.3 ключ Steam лежал открытым текстом — перешифровываем на месте
    # До 2.3 ключ SteamGridDB лежал открытым текстом в api_key.txt — переносим его в settings.json зашифрованным
    legacy = read_text(API_KEY_FILE) if not settings["griddb_api_key"] and can_protect_secrets() else ""
    if legacy:
        settings["griddb_api_key"] = legacy
        rewrite = True
    if rewrite:
        try:
            _write_settings(settings)
            if legacy:
                os.remove(API_KEY_FILE)
            log.info("Ключи теперь хранятся в settings.json зашифрованными%s",
                     " (api_key.txt с открытым ключом SteamGridDB удалён)" if legacy else "")
        except OSError as e:
            log.warning("Не удалось зашифровать ключи: %s", e)
    return settings


def griddb_key():
    """Ключ SteamGridDB (пусто — не задан). Можно вызывать из фоновых потоков."""
    return load_settings()["griddb_api_key"]


def save_settings(**changes):
    with _settings_lock:
        settings = _load_settings()
        settings.update(changes)
        _write_settings(settings)


# Очистка названия: Steam иногда присылает его с HTML-кодами («Assassin&apos;s Creed») — раскрываем их,
# затем убираем символы, недопустимые в имени файла (и значки ™ ®)
def clean_name(text):
    text = re.sub(r'[<>:"/\\|?*™®©]', "", html.unescape(str(text)))
    return re.sub(r"\s+", " ", text).strip()


# --- Список игр -------------------------------------------------------------
def read_legacy_excel():
    """Читает названия из старого SteamGames.xlsx (столбец «Игры»)."""
    from openpyxl import load_workbook

    workbook = load_workbook(LEGACY_EXCEL_FILE, read_only=True)
    rows = workbook.active.iter_rows(values_only=True)
    header = next(rows, ())
    column = header.index("Игры") if "Игры" in header else 0
    names = [row[column] for row in rows if len(row) > column and row[column]]
    workbook.close()
    return names


class GameLibrary:
    """Список игр в games.json: [{"name": "...", "appid": 123 или null}, ...]"""

    def __init__(self, path=GAMES_FILE):
        self.path = path
        self.games = []

    def load(self):
        """Загружает список. Возвращает сообщение для пользователя или None."""
        if not os.path.exists(self.path):
            return self._migrate_from_excel()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            # Не перезаписываем повреждённый файл — откладываем его в сторону
            broken_path = self.path + ".broken"
            os.replace(self.path, broken_path)
            self.games = []
            return (f"Файл games.json повреждён ({e}). Он сохранён как "
                    f"{os.path.basename(broken_path)}, список начат заново.")
        self.games = [g for g in data if isinstance(g, dict) and g.get("name")]
        if self._fix_escaped_names():
            try:
                self.save()
            except OSError:
                log.exception("Не удалось сохранить исправленные названия игр")
        return None

    def _fix_escaped_names(self):
        """До 2.2 названия попадали в список с HTML-кодами: «&apos;» вместо апострофа. Чиним их вместе
        с файлами обложек. Если такая игра уже есть с правильным названием — оставляем одну."""
        fixed_count = 0
        for game in list(self.games):
            old, new = game["name"], clean_name(game["name"])
            if new == old:
                continue
            fixed_count += 1
            twin = self.find(new)
            if twin and twin is not game:
                if game.get("appid") and not twin.get("appid"):
                    twin["appid"] = game["appid"]
                self.games.remove(game)
            else:
                game["name"] = new
            for ext in IMAGE_EXTENSIONS:  # обложка лежит под старым названием
                old_path, new_path = (os.path.join(COVERS_DIR, base + ext) for base in (old, new))
                try:
                    if os.path.exists(old_path):
                        if os.path.exists(new_path) or find_cover(new):
                            os.remove(old_path)
                        else:
                            os.replace(old_path, new_path)
                except OSError as e:
                    log.warning("Не удалось переименовать обложку «%s»: %s", old, e)
            log.info("Название исправлено: «%s» → «%s»", old, new)
        return fixed_count

    def _migrate_from_excel(self):
        if not os.path.exists(LEGACY_EXCEL_FILE):
            return None
        try:
            names = read_legacy_excel()
        except ImportError:
            return "Найден SteamGames.xlsx. Для переноса выполните pip install openpyxl и перезапустите программу."
        for name in names:
            self.add(name)
        self.save()
        return f"Из SteamGames.xlsx перенесено игр: {len(self.games)}."

    def save(self):
        save_json(self.path, self.games)

    def find(self, name):
        key = name.casefold()
        for game in self.games:
            if game["name"].casefold() == key:
                return game
        return None

    def add(self, name, appid=None):
        """Возвращает "added", "updated" (дописан appid) или None (пропущено)."""
        name = clean_name(name)
        if not name:
            return None
        existing = self.find(name)
        if existing:
            if appid and not existing.get("appid"):
                existing["appid"] = appid
                return "updated"
            return None
        self.games.append({"name": name, "appid": appid})
        return "added"

    def remove(self, game):
        self.games.remove(game)

    def clear(self):
        self.games.clear()


# --- Обложки ----------------------------------------------------------------
def find_cover(name):
    base = clean_name(name)
    for ext in IMAGE_EXTENSIONS:
        path = os.path.join(COVERS_DIR, base + ext)
        if os.path.exists(path):
            return path
    return None


def clear_covers():
    """Удаляет все скачанные и свои обложки. Возвращает, сколько файлов удалено."""
    removed = 0
    if os.path.isdir(COVERS_DIR):
        for file_name in os.listdir(COVERS_DIR):
            if os.path.splitext(file_name)[1].lower() in IMAGE_EXTENSIONS:
                os.remove(os.path.join(COVERS_DIR, file_name))
                removed += 1
    save_not_found(set())
    forget_cover_checks(keep_custom=False)
    return removed


NOT_FOUND_VERSION = 2  # 2: ищем и по настоящим адресам картинок Steam — прежний список ненайденных забываем


def load_not_found():
    try:
        with open(NOT_FOUND_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version") == NOT_FOUND_VERSION:
            return set(data.get("names") or [])
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return set()


def save_not_found(names):
    save_json(NOT_FOUND_FILE, {"version": NOT_FOUND_VERSION, "names": sorted(names)})


def delete_cover(name):
    base = clean_name(name)
    for ext in IMAGE_EXTENSIONS:
        path = os.path.join(COVERS_DIR, base + ext)
        if os.path.exists(path):
            os.remove(path)


def save_cover_bytes(name, content):
    image = Image.open(io.BytesIO(content)).convert("RGB")
    image = image.resize(COVER_SIZE, Image.Resampling.LANCZOS)
    os.makedirs(COVERS_DIR, exist_ok=True)
    delete_cover(name)  # иначе старая .png найдётся раньше новой .jpg
    path = os.path.join(COVERS_DIR, clean_name(name) + ".jpg")
    image.save(path, quality=92)
    return path


def fetch(url, **kwargs):
    response = requests.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
    response.raise_for_status()
    return response


def download_from_steamgriddb(game, api_key):
    """Вертикальная обложка игры из SteamGridDB (байты) или None. Игру ищем по номеру в Steam, иначе по названию."""
    headers = {"Authorization": f"Bearer {api_key}"}
    game_id = _griddb_game_id(game, api_key)
    if not game_id:
        return None

    grids_url = f"{STEAMGRIDDB_API_URL}/grids/game/{game_id}"
    # Сначала просим вертикальные обложки 600x900, затем любые
    grids = fetch(grids_url, headers=headers, params={"dimensions": "600x900"}).json().get("data", [])
    if not grids:
        grids = fetch(grids_url, headers=headers).json().get("data", [])
    if not grids:
        return None
    return fetch(grids[0]["url"]).content


def cover_detail(image):
    """Сколько мелких деталей в верхней четверти, в середине и в нижней четверти обложки: (верх, середина, низ)."""
    edges = image.convert("L").resize((60, 90)).filter(ImageFilter.FIND_EDGES)

    def detail(top, bottom):
        values = list(edges.crop((2, top, 58, bottom)).getdata())  # у самых краёв FIND_EDGES даёт ложные линии
        return sum(values) / len(values)

    return detail(2, 22), detail(36, 54), detail(68, 88)


# Пороги для looks_generated. Steam собирает обложку тремя способами:
# 1) шапка по центру, сверху и снизу размытое продолжение — верх и низ гладкие, середина детальная
#    (Final Fantasy IV: верх 0.1, середина 48.8, низ 0.0);
# 2) шапка вверху, ниже — её размытое продолжение: верх детальный, середина уже наполовину размыта, низ совсем
#    гладкий (Back to the Future Ep 2–5: верх ≈20, середина ≈15, низ ≈1.5; Beyond Good & Evil: 13.7 / 5.9 / 1.2;
#    F.E.A.R.: 13.7 / 18.4 / 1.3; Singularity: 6.0 / 7.9 / 1.1; Murdered: Soul Suspect: 6.8 / 7.6 / 1.3).
#    У настоящих обложек с тёмным низом середина детальнее верха в разы (Borderlands 4: 5.6 / 73.0 / 0.3,
#    Watch_Dogs: 6.8 / 27.7 / 1.6) — их второе правило не трогает;
# 3) шапка внизу, выше — гладкая заливка: верх пустой, середина и низ детальные, низ заметно спокойнее середины
#    (Alone in the Dark: 0.0 / 36.5 / 7.1; Dino Crisis: 0.4 / 44.3 / 14.1).
GENERATED_EDGE_MAX = 5  # способ 1: верх и низ — размытое продолжение шапки, деталей почти нет
GENERATED_MIDDLE_MIN = 3.5
GENERATED_RATIO = 3  # середина хотя бы втрое детальнее верха и низа
GENERATED_TOP_BOTTOM_MAX = 2.5  # способ 2: низ гладкий…
GENERATED_TOP_MIN = 5  # …верх детальный…
GENERATED_TOP_MIDDLE = 1.4  # …и середина детальнее верха не больше чем на 40 %
GENERATED_EMPTY_TOP = 1  # способ 3: верх почти пустой…
GENERATED_BOTTOM_RATIO = 3  # …а середина хотя бы втрое детальнее низа


def looks_generated(image):
    """Обложку собрал сам Steam из горизонтальной шапки игры: шапка по центру, вверху или внизу, а остальное —
    её размытое продолжение или заливка. Так Steam делает обложки старым играм, у которых настоящей вертикальной
    нет. Узнаём их по гладкой (без мелких деталей) верхней и нижней четверти при обычной середине, по гладкому
    низу при детальном верхе — или по пустому верху при детальной середине."""
    top, middle, bottom = cover_detail(image)
    edge = max(top, bottom)
    if edge < GENERATED_EDGE_MAX and middle >= max(GENERATED_MIDDLE_MIN, edge * GENERATED_RATIO):
        return True
    if bottom < GENERATED_TOP_BOTTOM_MAX and top >= GENERATED_TOP_MIN and middle <= top * GENERATED_TOP_MIDDLE:
        return True
    return (top < GENERATED_EMPTY_TOP and middle >= GENERATED_MIDDLE_MIN
            and middle >= bottom * GENERATED_BOTTOM_RATIO)


def _content_generated(content):
    try:
        with Image.open(io.BytesIO(content)) as image:
            return looks_generated(image)
    except (OSError, ValueError):
        return False


def download_cover(game, steamgriddb_key):
    """Возвращает (путь к обложке или None, список ошибок)."""
    errors = []

    # 1. Официальная обложка Steam — не требует ключа, нужен только appid. Сначала по старому адресу,
    # а у новых игр (и демоверсий) — по настоящему адресу из магазина
    steam_content = None
    if game.get("appid"):
        try:
            steam_content = fetch(STEAM_COVER_URL.format(appid=game["appid"])).content
        except requests.RequestException as e:
            errors.append(f"Steam: {e}")
            steam_content = _steam_asset(game["appid"], "cover")
    # Обложку собрал Steam из шапки — с ключом сначала ищем настоящую в SteamGridDB
    if steam_content and not (steamgriddb_key and _content_generated(steam_content)):
        try:
            return save_cover_bytes(game["name"], steam_content), errors
        except OSError as e:
            errors.append(f"Steam: {e}")
            steam_content = None

    # 2. SteamGridDB
    if not steamgriddb_key:
        errors.append("SteamGridDB: не задан API-ключ")
        return None, errors
    try:
        content = download_from_steamgriddb(game, steamgriddb_key)
        if content:
            return save_cover_bytes(game["name"], content), errors
        errors.append("SteamGridDB: игра не найдена")
    except (requests.RequestException, OSError, ValueError, KeyError) as e:
        errors.append(f"SteamGridDB: {mask_secrets(e)}")
    if steam_content:  # в SteamGridDB ничего — остаётся обложка, собранная Steam
        try:
            return save_cover_bytes(game["name"], steam_content), errors
        except OSError as e:
            errors.append(f"Steam: {e}")
    return None, errors


# Обложки, скачанные раньше: какие уже проверены на «собрана Steam из шапки» и какие выбраны вручную
# (их программа не заменяет никогда). После смены ключа SteamGridDB проверка начинается заново.
COVER_CHECKS_FILE = os.path.join(BASE_DIR, "covers_checked.json")
COVER_CHECKS_VERSION = 4  # 4: мягче пороги и шапка внизу — все проверяются заново (выбранные вручную — нет)
_cover_checks_lock = threading.Lock()


def _load_cover_checks():
    try:
        with open(COVER_CHECKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            fresh = data.get("version") == COVER_CHECKS_VERSION
            return {"checked": set(data.get("checked") or []) if fresh else set(),
                    "custom": set(data.get("custom") or [])}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    return {"checked": set(), "custom": set()}


def _save_cover_checks(checks):
    save_json(COVER_CHECKS_FILE, {"version": COVER_CHECKS_VERSION, "checked": sorted(checks["checked"]),
                                  "custom": sorted(checks["custom"])})


def mark_custom_cover(name):
    """Обложку выбрали вручную — её программа больше не трогает."""
    with _cover_checks_lock:
        checks = _load_cover_checks()
        checks["custom"].add(clean_name(name))
        _save_cover_checks(checks)


def forget_cover_checks(keep_custom=True):
    """Ключ SteamGridDB сменился — собранные Steam обложки проверяем заново (выбранные вручную — нет)."""
    with _cover_checks_lock:
        checks = _load_cover_checks()
        _save_cover_checks({"checked": set(), "custom": checks["custom"] if keep_custom else set()})


def upgrade_generated_covers(games, api_key, on_replaced=None):
    """Уже скачанные обложки, которые Steam собрал из шапки, — заменить настоящими из SteamGridDB.

    Каждая обложка проверяется один раз (список — в covers_checked.json). on_replaced(название) — обложка
    заменена. Вызывается в фоновом потоке. Возвращает, сколько заменено.
    """
    if not api_key:
        return 0
    with _cover_checks_lock:
        checks = _load_cover_checks()
    found, missing, replaced, failed, checked = 0, [], 0, 0, 0
    near = []  # не прошли проверку, но ближе всех к порогу — в лог, чтобы было видно, как работает проверка
    try:
        for game in games:
            name = clean_name(game["name"])
            if name in checks["checked"] or name in checks["custom"]:
                continue
            path = find_cover(name)
            if not path:
                continue
            checks["checked"].add(name)
            checked += 1
            try:
                with Image.open(path) as image:
                    top, middle, bottom = cover_detail(image)
                    generated = looks_generated(image)
            except (OSError, ValueError):
                continue
            if not generated:
                near.append((middle / max(top, bottom, 0.5), name, top, middle, bottom))
                continue
            found += 1
            try:
                content = download_from_steamgriddb(game, api_key)
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("Обложка «%s» из SteamGridDB не скачалась: %s", name, mask_secrets(e))
                checks["checked"].discard(name)  # нет связи — попробуем при следующем запуске
                failed += 1
                continue
            if not content:
                missing.append(name)
                continue
            save_cover_bytes(name, content)
            replaced += 1
            log.info("Обложка «%s» была собрана Steam из шапки (верх %.1f, середина %.1f, низ %.1f) — "
                     "заменена из SteamGridDB", name, top, middle, bottom)
            if on_replaced:
                on_replaced(game["name"])
    finally:
        with _cover_checks_lock:
            custom = _load_cover_checks()["custom"]  # пока мы работали, обложку могли выбрать вручную
            _save_cover_checks({"checked": checks["checked"], "custom": custom | checks["custom"]})
    if checked:
        near.sort(reverse=True)
        log.info("Обложки, собранные Steam из шапки игры: проверено %d, похожих %d, заменено из SteamGridDB %d, "
                 "не скачалось %d, в SteamGridDB нет: %s. Ближе всех к порогу, но не заменены: %s", checked, found,
                 replaced, failed, (", ".join(missing[:30]) + (" …" if len(missing) > 30 else "")) or "—",
                 "; ".join(f"«{n}» (верх {t:.1f}, середина {m:.1f}, низ {b:.1f})" for _, n, t, m, b in near[:8])
                 or "—")
    return replaced


class ServiceBusy(Exception):
    """Сайт (SteamGridDB, Google) сейчас не отвечает или отвечает пустотой. Текст — для пользователя."""


GRIDDB_RETRY_PAUSE = 3  # секунд: SteamGridDB порой минуту отвечает пустыми страницами, потом снова работает


def cover_from_griddb(game, api_key):
    """Заменить обложку игры лучшей вертикальной из SteamGridDB — по просьбе пользователя. Путь или None — там нет.
    Такую обложку программа потом не трогает, как и выбранную вручную. Сайт не ответил и со второго раза —
    ServiceBusy."""
    name = clean_name(game["name"])
    old = find_cover(name)
    if old:
        # Цифры проверки «собрана Steam из шапки» для старой обложки — по ним подстраивается порог
        try:
            with Image.open(old) as image:
                top, middle, bottom = cover_detail(image)
                generated = looks_generated(image)
            log.info("Обложка «%s» до замены: верх %.1f, середина %.1f, низ %.1f — %s", name, top, middle, bottom,
                     "похожа на собранную Steam" if generated else "проверка не сочла её собранной Steam")
        except (OSError, ValueError):
            pass
    content, error = None, None
    for attempt in range(2):
        if attempt:
            time.sleep(GRIDDB_RETRY_PAUSE)
        try:
            content = download_from_steamgriddb(game, api_key)
            error = None
            break
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            error = e
    if error is not None:
        log.warning("Обложка «%s»: SteamGridDB не ответил и со второго раза: %s", name, mask_secrets(error))
        raise ServiceBusy("SteamGridDB сейчас не отвечает. Так бывает минуту-другую — попробуйте чуть позже.")
    if not content:
        log.info("Обложка «%s»: в SteamGridDB вертикальной обложки нет", game["name"])
        return None
    path = save_cover_bytes(game["name"], content)
    mark_custom_cover(game["name"])
    log.info("Обложка «%s» заменена из SteamGridDB по просьбе пользователя", game["name"])
    return path


# --- Steam ------------------------------------------------------------------
def fetch_owned_games(api_key, steam_id, free=True):
    """Игры аккаунта из Web API. free — и бесплатные, в которые играли (без них Steam отдаёт только купленное)."""
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1 if free else 0,
        "format": "json",
    }
    data = fetch(STEAM_OWNED_GAMES_URL, params=params).json()
    return data.get("response", {}).get("games", [])


def _drop_free_non_games(games, api_key, steam_id):
    """Среди бесплатных, в которые играли, Steam присылает и моды (Killing Floor Mod: Defence Alliance 2 и т. п.)
    — это не игры из библиотеки. Бесплатные оставляем, только если магазин Steam знает их как игру.
    Магазин не ответил — оставляем всё как есть. Возвращает appid убранных."""
    try:
        paid = {g.get("appid") for g in fetch_owned_games(api_key, steam_id, free=False)}
        free = [appid for appid in games if appid not in paid]
        if not free:
            return []
        items = store_items(free)
    except (requests.RequestException, ValueError) as e:
        log.warning("Бесплатные игры не проверены (моды могут попасть в список): %s", e)
        return []
    dropped = [appid for appid in free
               if appid not in items or items[appid]["type"] not in (None, STORE_TYPE_GAME)]
    for appid in dropped:
        log.info("Не игра, убрана из своих: «%s» (%s) — %s", games[appid]["name"], appid,
                 "магазин её не знает" if appid not in items else f"тип в магазине {items[appid]['type']}")
        del games[appid]
    return dropped


# --- Вход через Steam -------------------------------------------------------
# Вход идёт через официальный вход Steam (OpenID) в обычном браузере пользователя — не во встроенном:
# программа, внутри которой открыта страница входа, технически может прочитать всё, что туда вводят.
# Во внешнем браузере пароль, коды Steam Guard и сессия программе недоступны вовсе. Она получает
# только SteamID, а чтобы его нельзя было подделать, ответ перепроверяется прямым запросом к Steam.
STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_OPENID_ID_RE = re.compile(r"https://steamcommunity\.com/openid/id/(\d{17})")
OPENID_NS = "http://specs.openid.net/auth/2.0"
OPENID_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"
STEAM_API_KEY_PAGE = "https://steamcommunity.com/dev/apikey"
STEAM_PRIVACY_PAGE = "https://steamcommunity.com/my/edit/settings"
STEAM_PLAYER_SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
STEAM_KEY_RE = re.compile(r"[0-9A-Fa-f]{32}")
LOGIN_TIMEOUT = 300  # секунд на вход в браузере


class SteamLoginError(Exception):
    pass


def short_steam_id(steam_id):
    """SteamID для логов: только последние цифры — логи пользователи присылают другим людям."""
    return f"…{str(steam_id)[-4:]}" if steam_id else "нет"


_LOGIN_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8"><title>Backlog Roulette</title>
<body style="font-family:Segoe UI,sans-serif;background:#202020;color:#fff;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0"><div style="text-align:center;max-width:460px">
<h2>{title}</h2><p style="color:#ccc">{text}</p></div></body></html>"""


class SteamLogin:
    """Один вход через Steam. Создаётся в главном потоке, wait() ждёт ответа в фоновом.

    Steam возвращает пользователя на адрес программы на этом же ПК: маленький сервер слушает только
    127.0.0.1 (из сети к нему не подключиться) на случайном порту и принимает ответ, только если в нём
    случайная метка этого входа. Затем ответ проверяется у самого Steam (check_authentication).
    """

    def __init__(self):
        self.state = secrets.token_urlsafe(24)
        self.steam_id = None
        self.error = None
        login = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 5  # браузер может открыть соединение заранее и молчать — не ждём его вечно

            def do_GET(self):
                login._on_request(self)

            def log_message(self, *args):
                pass  # адреса с ответом Steam в лог не пишем

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.5
        self.return_to = f"http://127.0.0.1:{self.server.server_address[1]}/steam-login?state={self.state}"

    def url(self):
        params = {
            "openid.ns": OPENID_NS,
            "openid.mode": "checkid_setup",
            "openid.return_to": self.return_to,
            "openid.realm": f"http://127.0.0.1:{self.server.server_address[1]}/",
            "openid.identity": OPENID_SELECT,
            "openid.claimed_id": OPENID_SELECT,
        }
        return f"{STEAM_OPENID_URL}?{urlencode(params)}"

    def wait(self, cancelled):
        """SteamID после входа; None — окно закрыли. Не вошли за LOGIN_TIMEOUT или Steam не подтвердил — ошибка."""
        deadline = time.monotonic() + LOGIN_TIMEOUT
        try:
            while self.steam_id is None and self.error is None:
                if cancelled():
                    return None
                if time.monotonic() > deadline:
                    raise SteamLoginError("вход не завершён за 5 минут. Нажмите «Войти через Steam» ещё раз")
                self.server.handle_request()
        finally:
            self.server.server_close()
        if self.error:
            raise SteamLoginError(self.error)
        log.info("Вход через Steam: подтверждён, SteamID %s", short_steam_id(self.steam_id))
        return self.steam_id

    def _on_request(self, request):
        parts = urlsplit(request.path)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if parts.path != "/steam-login" or not hmac.compare_digest(query.get("state", ""), self.state):
            self._reply(request, 404, "Страница не найдена", "")
            return
        if query.get("openid.mode") == "cancel":
            self.error = "вход отменён на странице Steam"
            self._reply(request, 200, "Вход отменён", "Вернитесь в Backlog Roulette.")
            return
        try:
            self.steam_id = verify_steam_login(query, self.return_to)
        except (SteamLoginError, requests.RequestException) as e:
            self.error = mask_secrets(e) if isinstance(e, SteamLoginError) else f"Steam не ответил: {mask_secrets(e)}"
            log.warning("Вход через Steam не подтверждён: %s", self.error)
            self._reply(request, 200, "Вход не удался", "Вернитесь в Backlog Roulette и попробуйте ещё раз.")
            return
        self._reply(request, 200, "Готово!", "Вход подтверждён. Эту вкладку можно закрыть и вернуться в Backlog Roulette.")

    @staticmethod
    def _reply(request, status, title, text):
        body = _LOGIN_PAGE.format(title=html.escape(title), text=html.escape(text)).encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        request.wfile.write(body)


def verify_steam_login(params, return_to):
    """SteamID из ответа Steam — только если ответ наш, пришёл от Steam и Steam его подтвердил."""
    if params.get("openid.ns") != OPENID_NS or params.get("openid.mode") != "id_res":
        raise SteamLoginError("Steam прислал непонятный ответ")
    if params.get("openid.op_endpoint") != STEAM_OPENID_URL or params.get("openid.return_to") != return_to:
        raise SteamLoginError("ответ пришёл не от Steam или не на этот вход")
    match = STEAM_OPENID_ID_RE.fullmatch(params.get("openid.claimed_id", ""))
    if not match or params.get("openid.identity") != params.get("openid.claimed_id"):
        raise SteamLoginError("в ответе Steam нет номера аккаунта")
    # Главная проверка: спрашиваем сам Steam, выдавал ли он этот ответ. Подделку он не подтвердит
    check = dict(params, **{"openid.mode": "check_authentication"})
    check.pop("state", None)
    response = requests.post(STEAM_OPENID_URL, data=check, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if "is_valid:true" not in response.text.splitlines():
        raise SteamLoginError("Steam не подтвердил вход")
    return match.group(1)


def check_steam_key(api_key, steam_id):
    """Проверка ключа одним запросом: {"name": имя в Steam, "games": сколько игр, "private": игры скрыты}."""
    if not STEAM_KEY_RE.fullmatch(api_key or ""):
        raise SteamLoginError("ключ — это 32 знака из цифр и букв A–F. Скопируйте его со страницы целиком")
    try:
        response = requests.get(STEAM_OWNED_GAMES_URL, timeout=REQUEST_TIMEOUT,
                                params={"key": api_key, "steamid": steam_id, "format": "json",
                                        "include_played_free_games": 1})  # считаем так же, как импорт
    except requests.RequestException as e:
        raise SteamLoginError(f"Steam не ответил: {mask_secrets(e)}") from None
    if response.status_code in (401, 403):
        raise SteamLoginError("Steam не принял ключ. Проверьте, что скопировали его целиком")
    if not response.ok:
        raise SteamLoginError(f"Steam ответил ошибкой {response.status_code}, попробуйте через минуту")
    data = (response.json() or {}).get("response") or {}
    name = ""
    try:
        players = fetch(STEAM_PLAYER_SUMMARIES_URL, params={"key": api_key, "steamids": steam_id}).json()
        name = ((players.get("response") or {}).get("players") or [{}])[0].get("personaname", "")
    except (requests.RequestException, ValueError, IndexError, AttributeError):
        pass  # имя только для красоты
    private = "game_count" not in data  # игры скрыты в приватности профиля — Steam присылает пустой ответ
    log.info("Ключ Steam Web API проверен: SteamID %s, игр %s", short_steam_id(steam_id),
             "скрыты" if private else data.get("game_count", 0))
    return {"name": name, "games": data.get("game_count", 0), "private": private}


STEAMGRIDDB_KEY_PAGE = "https://www.steamgriddb.com/profile/preferences/api"
STEAMGRIDDB_KEY_RE = re.compile(r"[0-9A-Za-z]{20,64}")


def check_griddb_key(api_key):
    """Проверка ключа SteamGridDB одним поиском. Ошибка — SteamLoginError с понятной причиной."""
    key = (api_key or "").strip()
    if not STEAMGRIDDB_KEY_RE.fullmatch(key):
        raise SteamLoginError("ключ SteamGridDB — строка из латинских букв и цифр без пробелов. "
                              "Скопируйте его со страницы целиком")
    try:
        response = requests.get(f"{STEAMGRIDDB_API_URL}/search/autocomplete/portal",
                                headers={"Authorization": f"Bearer {key}"}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise SteamLoginError(f"SteamGridDB не ответил: {mask_secrets(e)}") from None
    if response.status_code in (401, 403):
        raise SteamLoginError("SteamGridDB не принял ключ. Проверьте, что скопировали его целиком")
    if not response.ok:
        raise SteamLoginError(f"SteamGridDB ответил ошибкой {response.status_code}, попробуйте через минуту")
    log.info("Ключ SteamGridDB проверен")
    return key


# --- Коллекции Steam --------------------------------------------------------
# Клиент Steam хранит коллекции в userdata/<account id>/config/cloudstorage/cloud-storage-namespace-1.json:
# список пар [ключ, {"value": "<json-строка>", "is_deleted": ...}], где у коллекций ключ
# "user-collections.<id>", а в value лежат {"name": ..., "added": [appid, ...], "removed": [...]}.
STEAMID64_BASE = 76561197960265728  # SteamID64 = это число + account id
SPECIAL_COLLECTIONS = {"hidden": "Скрытые", "favorite": "Избранное"}


def find_steam_dir():
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                path, _ = winreg.QueryValueEx(key, "SteamPath")
            if os.path.isdir(path):
                return os.path.normpath(path)
        except OSError:
            pass
    for path in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(path):
            return path
    return None


@functools.lru_cache(maxsize=1)  # папки библиотек за время работы программы не меняются
def steam_library_dirs():
    """Все папки библиотек Steam на ПК (игры можно ставить на разные диски)."""
    steam_dir = find_steam_dir()
    if not steam_dir:
        return []
    dirs = [steam_dir]
    try:
        with open(os.path.join(steam_dir, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return dirs
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        path = os.path.normpath(match.group(1).replace("\\\\", "\\"))
        # Без учёта регистра: Steam пишет и «d:\steam», и «D:\Steam» — это одна папка
        if os.path.normcase(path) not in {os.path.normcase(d) for d in dirs}:
            dirs.append(path)
    log.info("Папки библиотек Steam: %s", ", ".join(dirs))
    return dirs


def is_installed(appid):
    """Установлена ли игра: у установленных игр Steam хранит файл appmanifest_<appid>.acf."""
    return any(os.path.exists(os.path.join(d, "steamapps", f"appmanifest_{appid}.acf"))
               for d in steam_library_dirs())


def installed_owner(appid):
    """Кем установлена игра на этом ПК: SteamID64 аккаунта (LastOwner в appmanifest), "" — неизвестно кем,
    None — не установлена. Ключ и вход не нужны — это файлы Steam на диске."""
    for library in steam_library_dirs():
        path = os.path.join(library, "steamapps", f"appmanifest_{appid}.acf")
        if os.path.exists(path):
            return _acf_values(path).get("lastowner", "")
    return None


def _steam_registry_value(name, subkey=""):
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam" + subkey) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def steam_running():
    """Запущен ли клиент Steam: пока он работает, в реестре записан его pid."""
    return bool(_steam_registry_value("pid", r"\ActiveProcess"))


def running_app_id():
    """appid игры, которая сейчас запущена через Steam (0 — ни одной)."""
    return _steam_registry_value("RunningAppID") or 0


def steam_client_account():
    """SteamID64 аккаунта, который вошёл в клиент Steam на этом ПК, или None — узнать не вышло.

    Пока Steam запущен, реестр хранит номер вошедшего аккаунта (ActiveUser). Steam закрыт — берём
    последний вошедший аккаунт из config/loginusers.vdf (отметка MostRecent).
    """
    active = _steam_registry_value("ActiveUser", r"\ActiveProcess")
    if active:
        return str(STEAMID64_BASE + int(active))
    for steam_id, block in _login_users():
        if re.search(r'"MostRecent"\s+"1"', block, re.IGNORECASE):
            return steam_id
    return None


def _login_users():
    """Аккаунты, которые входили в клиент Steam на этом ПК (config/loginusers.vdf): [(SteamID64, блок), ...]."""
    steam_dir = find_steam_dir()
    if not steam_dir:
        return []
    try:
        with open(os.path.join(steam_dir, "config", "loginusers.vdf"), encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return []
    return [(m.group(1), m.group(2)) for m in re.finditer(r'"(\d{17})"\s*\{([^}]*)\}', text)]


def steam_account_names():
    """Имена в Steam аккаунтов, которые входили в клиент Steam на этом ПК: {SteamID64: имя}.

    Берём только видимое всем имя профиля (PersonaName) — логин аккаунта из файла не читаем.
    """
    names = {}
    for steam_id, block in _login_users():
        persona = re.search(r'"PersonaName"\s+"([^"]*)"', block, re.IGNORECASE)
        if persona and persona.group(1).strip():
            names[steam_id] = persona.group(1).strip()
    return names


def steam_persona_name(steam_id):
    """Имя профиля Steam по SteamID — без ключа. Сначала из клиента Steam на этом ПК, иначе с открытой
    страницы профиля в Steam (её XML-версия; имя видно всем, даже у закрытого профиля). None — не узнали."""
    if not re.fullmatch(r"\d{17}", steam_id or ""):
        return None
    name = steam_account_names().get(steam_id)
    if name:
        return name
    try:
        text = fetch(f"https://steamcommunity.com/profiles/{steam_id}/", params={"xml": 1}).text
    except Exception as e:  # noqa: BLE001 — без имени покажем только SteamID
        log.warning("Имя профиля Steam (SteamID %s) не узнать: %s", short_steam_id(steam_id), e)
        return None
    match = re.search(r"<steamID>\s*(?:<!\[CDATA\[(.*?)\]\]>|([^<]*))\s*</steamID>", text, re.DOTALL)
    name = ((match.group(1) or match.group(2) or "").strip()) if match else ""
    return name or None


def other_steam_account(steam_id):
    """SteamID64 аккаунта в клиенте Steam, если это не тот, что подключён к программе. None — тот же или не узнать.

    Клиент Steam ставит игры, качает саундтреки и хранит коллекции для того, кто в нём вошёл,
    а программа может быть подключена к другому аккаунту.
    """
    client = steam_client_account()
    return client if client and steam_id and client != str(steam_id) else None


def collections_hint(steam_id):
    """Почему коллекций не нашлось — подсказка для окна импорта."""
    if not find_steam_dir():
        return "Коллекции программа читает из клиента Steam, а он на этом ПК не найден."
    other = other_steam_account(steam_id)
    if other:
        return (f"Коллекции программа читает из клиента Steam на этом ПК, а в нём вошёл другой аккаунт "
                f"(SteamID {short_steam_id(other)}). Войдите в Steam аккаунтом, подключённым к программе, "
                f"и повторите импорт.")
    return ("Коллекции программа читает из клиента Steam на этом ПК, а этим аккаунтом в него ещё не входили. "
            "Войдите в Steam этим аккаунтом и повторите импорт.")


def open_url(url):
    """Открывает steam://… ссылку. Может занять время, поэтому вызывается в фоновом потоке."""
    if sys.platform == "win32":
        os.startfile(url)
    else:
        import webbrowser

        webbrowser.open(url)


def read_steam_collections(steam_id):
    """Коллекции из клиента Steam на этом ПК: [{"id", "name", "apps": set(appid)}, ...].

    Возвращает None, если Steam или файл коллекций не найден. Динамические коллекции
    (собранные по фильтрам) пропускаются: их состав Steam вычисляет на лету и в файл не пишет.
    """
    steam_dir = find_steam_dir()
    if not steam_dir or not str(steam_id).isdigit():
        return None
    account_id = int(steam_id) - STEAMID64_BASE
    path = os.path.join(steam_dir, "userdata", str(account_id), "config", "cloudstorage",
                        "cloud-storage-namespace-1.json")
    if not os.path.exists(path):
        return None

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    collections = []
    for entry in data:
        if not isinstance(entry, list) or len(entry) < 2 or not isinstance(entry[1], dict):
            continue
        key, item = str(entry[0]), entry[1]
        if not key.startswith("user-collections.") or item.get("is_deleted") or not item.get("value"):
            continue
        try:
            value = json.loads(item["value"])
        except (TypeError, json.JSONDecodeError):
            continue
        added = value.get("added")
        if not isinstance(added, list):
            continue
        collection_id = key.split(".", 1)[1]
        name = SPECIAL_COLLECTIONS.get(collection_id) or value.get("name") or collection_id
        apps = set(added) - set(value.get("removed") or [])
        collections.append({"id": collection_id, "name": name, "apps": apps})

    collections.sort(key=lambda c: (c["id"] not in SPECIAL_COLLECTIONS, c["name"].casefold()))
    return collections


# --- Библиотека целиком: свои игры + семейная группа ---------------------------
# GetOwnedGames отдаёт только купленные вами игры. Игры семейной группы Steam отдаёт IFamilyGroupsService —
# ему нужен токен входа в магазин Steam (его выдаёт сам магазин во встроенном браузере, ключ тут не годится).
# Без входа семейные игры не проверить. Раньше программа считала семейными все игры из коллекций, которых нет
# среди покупок, — туда попадали и «Скрытые» (тысячи бесплатных лицензий и демо), и игры, доступ к которым
# давно пропал. Теперь без входа «семейными» считаются только игры из ваших собственных коллекций, и они
# помечаются как неподтверждённые: в импорт по умолчанию не идут.
SHORTCUT_APPID_MIN = 2 ** 31  # у сторонних игр, добавленных в Steam вручную, appid начинаются отсюда
APP_DETAILS_LIMIT = 40  # сколько игр, которых нет в списке магазина, проверяем по одной
MAX_STORE_APPID = 4_500_000  # примерный самый большой appid в магазине — для полосы прогресса
STEAM_FAMILY_GROUP_URL = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
STEAM_FAMILY_APPS_URL = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
FAMILY_APP_TYPE_GAME = 1  # тип «игра» в ответе семейной библиотеки (программы, инструменты и видео — другие)
LOG_APPIDS = 40  # сколько номеров приложений перечислять в логе в одной строке


def fetch_family_games(token, steam_id):
    """Игры семейной группы, доступные вам (без ваших собственных): [{"appid", "name", "owners"}].

    None — вы не состоите в семейной группе. token — временный токен входа в магазин Steam: живёт только
    в памяти на время этого запроса, в лог и в файлы не попадает (в текстах ошибок он скрывается).
    """
    group = (fetch(STEAM_FAMILY_GROUP_URL, params={"access_token": token, "steamid": steam_id}).json()
             .get("response") or {})
    group_id = str(group.get("family_groupid") or "0")
    if group.get("is_not_member_of_any_group") or group_id == "0":
        return None
    data = (fetch(STEAM_FAMILY_APPS_URL, params={
        "access_token": token, "family_groupid": group_id, "steamid": steam_id, "include_own": "false",
        "include_excluded": "false", "include_free": "false", "include_non_games": "false",
        "language": "russian", "max_apps": 50000}).json().get("response") or {})
    games, kinds, excluded = [], {}, 0
    for app in data.get("apps") or []:
        appid = int(app.get("appid") or 0)
        owners = [str(o) for o in app.get("owner_steamids") or []]
        kind = app.get("app_type", FAMILY_APP_TYPE_GAME)
        kinds[kind] = kinds.get(kind, 0) + 1
        if not appid or str(steam_id) in owners:
            continue
        if app.get("exclude_reason"):  # владелец закрыл игру для семьи или её нельзя давать другим
            excluded += 1
            continue
        if kind != FAMILY_APP_TYPE_GAME:
            continue
        games.append({"appid": appid, "name": clean_name(app.get("name") or ""), "owners": owners})
    unnamed = [g["appid"] for g in games if not g["name"]]
    if unnamed:  # имя иногда не приходит — берём его из магазина
        try:
            items = store_items(unnamed)
            for game in games:
                if not game["name"]:
                    game["name"] = clean_name((items.get(game["appid"]) or {}).get("name") or "")
        except (requests.RequestException, ValueError) as e:
            log.warning("Названия %d семейных игр не узнать: %s", len(unnamed), e)
    games = [g for g in games if g["name"]]
    log.info("Семейная группа Steam: игр доступно %d, недоступно (закрыто владельцем) %d, все типы в ответе: %s",
             len(games), excluded, kinds)
    return games


def _report(progress, text, percent):
    if progress:
        progress(text, percent)


def fetch_store_app_names(api_key, appids, progress=None):
    """Названия игр по appid из списка магазина Steam (только игры, без DLC и саундтреков)."""
    wanted = set(appids)
    names = {}
    last_appid = 0
    while wanted - names.keys():
        # Список идёт по возрастанию appid, так что по последнему appid можно оценить, сколько осталось
        share = min(last_appid / MAX_STORE_APPID, 1.0)
        _report(progress, f"Ищу названия семейных игр в магазине Steam… найдено {len(names)} из {len(wanted)}",
                20 + int(share * 65))
        params = {"key": api_key, "include_games": "true", "max_results": 50000, "last_appid": last_appid}
        response = fetch(STEAM_APP_LIST_URL, params=params).json().get("response", {})
        for app in response.get("apps", []):
            if app.get("appid") in wanted and app.get("name"):
                names[app["appid"]] = app["name"]
        if not response.get("have_more_results") or not response.get("last_appid"):
            break
        last_appid = response["last_appid"]
    return names


def fetch_app_detail_name(appid):
    """Название одной игры со страницы магазина — для снятых с продажи игр. None, если это не игра."""
    data = fetch(STEAM_APP_DETAILS_URL, params={"appids": appid, "filters": "basic"}).json()
    info = (data or {}).get(str(appid)) or {}
    details = info.get("data") or {}
    if info.get("success") and details.get("type") == "game":
        return details.get("name")
    return None


def _load_names_cache():
    """Кэш названий: {"updated": время, "names": {appid: название}, "not_games": [appid, ...]}.

    "not_games" — appid, которых нет в списке игр магазина (DLC, саундтреки, программы).
    Раз в неделю кэш сбрасывается, чтобы подхватить новые игры и переименования.
    """
    try:
        with open(NAMES_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("updated", 0) < NAMES_CACHE_TTL:
            return {int(k): v for k, v in data.get("names", {}).items()}, set(data.get("not_games", []))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        pass
    return {}, set()


def _save_names_cache(names, not_games, keep_date):
    try:
        updated = time.time()
        if keep_date and os.path.exists(NAMES_CACHE_FILE):
            with open(NAMES_CACHE_FILE, encoding="utf-8") as f:
                updated = json.load(f).get("updated", updated)
        save_json(NAMES_CACHE_FILE, {"updated": updated, "names": {str(k): v for k, v in names.items()},
                                     "not_games": sorted(not_games)})
    except (OSError, json.JSONDecodeError, ValueError):
        log.exception("Не удалось сохранить кэш названий игр")


def resolve_app_names(api_key, appids, progress=None):
    """Названия для appid: из кэша, а для новых — из списка магазина Steam."""
    cached, not_games = _load_names_cache()
    unknown = set(appids) - cached.keys() - not_games
    if not unknown:
        log.info("Названия семейных игр взяты из кэша")
        return {a: cached[a] for a in appids if a in cached}

    log.info("Ищу названия %d игр в списке магазина Steam", len(unknown))
    found = fetch_store_app_names(api_key, unknown, progress)
    rest = sorted(unknown - found.keys())[:APP_DETAILS_LIMIT]
    for i, appid in enumerate(rest):
        _report(progress, f"Проверяю игры, снятые с продажи… {i + 1} из {len(rest)}", 85 + int(10 * i / len(rest)))
        try:
            name = fetch_app_detail_name(appid)
        except (requests.RequestException, ValueError):
            continue
        if name:
            found[appid] = name
    cached.update(found)
    not_games |= unknown - found.keys()
    _save_names_cache(cached, not_games, keep_date=bool(cached.keys() - found.keys()))
    return {a: cached[a] for a in appids if a in cached}


def load_steam_library(api_key, steam_id, progress=None, family_token=None):
    """Собирает всё для окна импорта: игры, коллекции и сведения о семейной группе.

    Игры: [{"appid", "name", "playtime" (минуты или None — неизвестно), "family": bool, "verified": bool}, ...]
    family_token — токен входа в магазин Steam (из встроенного браузера); без него семейная группа не проверяется.
    progress(текст, проценты) — необязательная функция для окна прогресса; вызывается из фонового потока.
    """
    _report(progress, "Получаю ваши игры из Steam…", 5)
    games = {}
    for game in fetch_owned_games(api_key, steam_id):
        if game.get("appid") and game.get("name"):
            games[game["appid"]] = {"appid": game["appid"], "name": clean_name(game["name"]),
                                    "playtime": game.get("playtime_forever", 0), "family": False, "verified": True}
    _drop_free_non_games(games, api_key, steam_id)
    own_count = len(games)

    # family: None — не проверяли (нет входа в магазин) или Steam не ответил; [] — не в семейной группе
    family, family_member, family_error = None, None, ""
    if family_token:
        _report(progress, f"Ваших игр: {own_count}. Проверяю семейную группу Steam…", 12)
        try:
            family = fetch_family_games(family_token, steam_id)
            family_member = family is not None
            family = family or []
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            family_error = mask_secrets(e)
            log.warning("Семейную группу Steam проверить не удалось: %s", family_error)
            family = None
    for game in family or []:
        games.setdefault(game["appid"], {"appid": game["appid"], "name": game["name"], "playtime": None,
                                         "family": True, "verified": True})

    _report(progress, "Читаю коллекции Steam…", 20)
    try:
        collections = read_steam_collections(steam_id)
        collections_error = None
    except (OSError, ValueError) as e:
        collections, collections_error = None, str(e)

    unverified = set()
    if family is None:
        # Семейную группу не проверили — «семейными» считаем только игры из своих коллекций (не из служебных
        # «Скрытых» и «Избранного»): их Steam не подтвердил, и в импорт они по умолчанию не идут
        for collection in collections or []:
            if collection["id"] not in SPECIAL_COLLECTIONS:
                unverified |= {a for a in collection["apps"] if isinstance(a, int) and 0 < a < SHORTCUT_APPID_MIN}
        unverified -= games.keys()
    if unverified:
        _report(progress, f"Ищу названия игр из ваших коллекций: {len(unverified)}…", 25)
        names = resolve_app_names(api_key, unverified, progress)
        for appid, name in names.items():
            games[appid] = {"appid": appid, "name": clean_name(name), "playtime": None, "family": True,
                            "verified": False}
    _report(progress, "Готово!", 100)

    _log_library(games, own_count, family, family_member, collections, unverified)
    twins = {}
    for game in games.values():
        twins.setdefault(game["name"].casefold(), []).append(game["appid"])
    twins = {name: ids for name, ids in twins.items() if len(ids) > 1}
    if twins:
        log.info("Одно название у нескольких игр Steam (в список попадёт одна, своя): %s",
                 "; ".join(f"«{name}» — {', '.join(map(str, ids))}" for name, ids in list(twins.items())[:20]))
    if collections_error:
        log.warning("Не удалось прочитать коллекции Steam: %s", collections_error)
    hint = collections_hint(steam_id) if collections is None and not collections_error else None
    return {"games": list(games.values()), "collections": collections, "collections_error": collections_error,
            "collections_hint": hint, "family_checked": family is not None, "family_member": family_member,
            "family_error": family_error}


def _appid_list(appids):
    appids = sorted(appids)
    text = ", ".join(map(str, appids[:LOG_APPIDS]))
    return text + (f" … и ещё {len(appids) - LOG_APPIDS}" if len(appids) > LOG_APPIDS else "") if appids else "—"


def _log_library(games, own_count, family, family_member, collections, unverified):
    """Подробно в лог: что программа нашла и почему. Номера приложений не секретные — по ним в магазине Steam
    видно, что это за игра (store.steampowered.com/app/<номер>), и так сверяются цифры с клиентом Steam."""
    family_count = sum(g["family"] and g["verified"] for g in games.values())
    if family is None:
        family_text = "не проверена (нет входа в магазин Steam во встроенном браузере)"
    elif not family_member:
        family_text = "вы не состоите в семейной группе"
    else:
        family_text = f"{family_count} игр подтверждено Steam"
    log.info("Библиотека Steam: ваших игр %d, семейная группа — %s, неподтверждённых из коллекций %d, коллекций %s",
             own_count, family_text, sum(not g["verified"] for g in games.values()),
             "не найдено" if collections is None else len(collections))
    for collection in collections or []:
        apps = {a for a in collection["apps"] if isinstance(a, int)}
        own = {a for a in apps if a in games and not games[a]["family"]}
        shared = {a for a in apps if a in games and games[a]["family"] and games[a]["verified"]}
        guessed = {a for a in apps if a in games and not games[a]["verified"]}
        other = apps - own - shared - guessed
        log.info("Коллекция «%s»: номеров %d — ваших игр %d, семейных %d, неподтверждённых %d, "
                 "нет среди доступных игр %d (дополнения, программы, демо, бывшие семейные или чужие лицензии): %s",
                 collection["name"], len(apps), len(own), len(shared), len(guessed), len(other), _appid_list(other))
    if unverified:
        log.info("Неподтверждённые «семейные» (из ваших коллекций): %s", _appid_list(unverified))


def select_games(library, choice):
    """Отбирает игры для импорта по сохранённому выбору (см. ключи import_* в настройках).

    Возвращает (список игр, id коллекций из выбора, которых больше нет в Steam).
    """
    known = {game["appid"] for game in library["games"]}
    collections = {c["id"]: c for c in library["collections"] or []}

    include_ids = choice["import_include"]
    include = set()
    for collection_id in include_ids:
        if collection_id in collections:
            include |= collections[collection_id]["apps"] & known
    missing = [cid for cid in include_ids if cid not in collections]

    skip = set()
    if choice["import_skip_hidden"] and "hidden" in collections:
        skip = collections["hidden"]["apps"]
    # У одной игры бывает два номера в Steam (Civilization IV: 3900 и 34440 — старое и новое издание). Скрыли
    # одно — второе с тем же названием тоже не берём, иначе скрытая игра всё равно попадёт в список
    hidden_names = {clean_name(g["name"]).casefold() for g in library["games"] if g["appid"] in skip}

    selected = [
        game for game in library["games"]
        if (not include_ids or game["appid"] in include)
        and game["appid"] not in skip
        and clean_name(game["name"]).casefold() not in hidden_names
        and not (choice["import_unplayed"] and (game["playtime"] or 0) > 0)
        and (not game["family"] or (choice.get("import_unverified", False) if not game.get("verified", True)
                                    else choice.get("import_family", True)))
    ]
    # Две игры Steam с одним названием (своя и семейная, переиздание под тем же именем) в списке игр — одна
    # строка, поэтому и здесь оставляем одну, свою: иначе «подходит» на одну больше, чем добавится
    unique, names = [], set()
    for game in sorted(selected, key=lambda g: g["family"]):
        name = clean_name(game["name"]).casefold()
        if name not in names:
            names.add(name)
            unique.append(game)
    return unique, missing


# --- Карточка игры ----------------------------------------------------------
MAX_SCREENSHOTS = 10
MAX_MOVIES = 2
DETAILS_VERSION = 4  # 4: трейлеры HLS раньше маленьких mp4; старые записи перезапрашиваются
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_details_lock = threading.Lock()  # кэш карточек читают и пишут фоновые потоки


def _strip_html(text):
    """HTML из магазина Steam в обычный текст с абзацами и пунктами списков."""
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|<h\d[^>]*>", "\n", text or "", flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _movie_sources(movie):
    """Адреса трейлера по порядку попыток — от лучшего качества к худшему.

    С 2025 года Steam у новых трейлеров отдаёт только потоки HLS/DASH (до 1080p), обычных mp4 в ответе нет.
    HLS открывает проигрыватель FFmpeg. Если его нет, остаются старые mp4 на CDN Steam — у новых
    трейлеров там обычно только маленькая версия.
    """
    sources = []
    for key in ("mp4", "webm"):
        if (movie.get(key) or {}).get("max"):
            sources.append(movie[key]["max"])
    if movie.get("hls_h264"):
        sources.append(movie["hls_h264"])
    base = f"https://cdn.akamai.steamstatic.com/steam/apps/{movie['id']}" if movie.get("id") else None
    if base:
        sources.append(f"{base}/movie_max.mp4")
    for key in ("mp4", "webm"):
        if (movie.get(key) or {}).get("480"):
            sources.append(movie[key]["480"])
    if base:
        sources.append(f"{base}/movie480.mp4")
    return list(dict.fromkeys(sources))  # без повторов, порядок сохраняется


def _is_russian(text):
    cyrillic = sum(1 for c in text if "а" <= c.lower() <= "я" or c in "ёЁ")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return cyrillic >= latin


def translate_to_russian(text):
    """Перевод на русский как выбрано в «Настройках»: Google, а если он не ответил — модулем на этом ПК
    (или только модулем). None — не получилось, текст остаётся на языке оригинала."""
    mode = translate_mode()
    if mode == "off":
        return None
    if mode == "auto":
        started = time.monotonic()
        translated = _google_translate(text)
        if translated:
            log.info("Переведено Google: %d знаков за %.1f с", len(text), time.monotonic() - started)
            return translated
    return local_translate(text)


TRANSLATE_MODES = ("auto", "local", "off")


def translate_mode():
    mode = load_settings().get("translate_mode", "auto")
    return mode if mode in TRANSLATE_MODES else "auto"


def translation_enabled():
    """Переводить ли вообще: выключено в «Настройках» или «только на ПК» без скачанного модуля — нет."""
    mode = translate_mode()
    return mode == "auto" or (mode == "local" and local_model_info() is not None and local_engine_available())


# Бесплатные адреса Google Translate без ключа: официального бесплатного API у переводчиков нет, а этими
# пользуются многие программы. Google время от времени отказывает («слишком много запросов») — тогда
# повторяем через полторы секунды, потом пробуем второй адрес, а если молчат оба — пару минут не ждём
# Google вовсе и сразу переводим модулем на ПК.
TRANSLATE_BACKUP_URL = "https://clients5.google.com/translate_a/t"
TRANSLATE_RETRY_PAUSE = 1.5
GOOGLE_REST = 120  # секунд
_google_rest_until = 0.0
_google_first = None  # адрес Google, который ответил последним (None — основной)


def _gtx(text):
    data = fetch(TRANSLATE_URL, params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text}).json()
    return "".join(part[0] for part in data[0] if part and part[0]).strip()


def _google_backup(text):
    data = fetch(TRANSLATE_BACKUP_URL, params={"client": "dict-chrome-ex", "sl": "auto", "tl": "ru", "q": text}).json()
    while isinstance(data, list) and data and isinstance(data[0], list):
        data = data[0]  # [["перевод", "en"]] → ["перевод", "en"]
    return data[0].strip() if isinstance(data, list) and data and isinstance(data[0], str) else ""


def _google_translate(text):
    global _google_rest_until, _google_first
    if time.monotonic() < _google_rest_until:
        return None
    # Первым — адрес, который ответил в прошлый раз: если основной адрес Google сейчас отказывает,
    # каждое описание не ждёт его дважды
    first, second = (_google_backup, _gtx) if _google_first is _google_backup else (_gtx, _google_backup)
    errors = []
    for method, pause in ((first, 0), (first, TRANSLATE_RETRY_PAUSE), (second, 0)):
        time.sleep(pause)
        try:
            translated = method(text)
            if translated:
                if method is not _google_first:
                    log.info("Google Переводчик: отвечает %s адрес%s", "запасной" if method is _google_backup
                             else "основной", f" (до этого: {'; '.join(errors)})" if errors else "")
                    _google_first = method
                return translated
            errors.append("пустой ответ")
        except (requests.RequestException, ValueError, TypeError, IndexError, AttributeError) as e:
            errors.append(str(e).split("?", 1)[0][:160])  # в адресе ошибки — весь текст описания, в лог он не нужен
    local = local_model_info() is not None
    if local:  # есть чем подменить Google — пару минут его не ждём; нет — пусть пробует при каждом описании
        _google_rest_until = time.monotonic() + GOOGLE_REST
    log.warning("Google Переводчик не ответил (%s)%s", "; ".join(errors),
                " — перевожу модулем на этом ПК" if local else "")
    return None


# --- Перевод на этом ПК ---------------------------------------------------------
# Модель перевода с английского на русский из открытого проекта Argos Translate (лицензии MIT / CC0),
# движок — CTranslate2 от OpenNMT. Всё считается на этом ПК: текст описаний никуда не отправляется.
# Модель скачивается один раз по кнопке в «Настройках» — только с адреса, зашитого здесь, только по HTTPS,
# с проверкой размера и состава архива. Из архива берутся только файлы модели — это данные, не программы.
LOCAL_MODEL_URL = "https://argos-net.com/v1/translate-en_ru-1_9.argosmodel"
# Контрольная сумма SHA-256 файла модели — снята с файла, скачанного автором 30.10.2026 (187 МБ). Файл с другой
# суммой (подменённый, битый или выложенный заново) программа не поставит. Сменится модель — сменить и сумму
LOCAL_MODEL_SHA256 = "591d743ae103752b88ffc38785c50421320f4eff93c8967e0d3d2e14d4e27811"
LOCAL_MODEL_DIR = os.path.join(CACHE_DIR, "translate", "en_ru")
LOCAL_MODEL_MAX_BYTES = 600 * 1024 * 1024  # больше — значит, это не та модель
LOCAL_MODEL_MAX_FILES = 300
LOCAL_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=[\"«“(\[A-ZА-ЯЁ0-9])")
_local_engine = {}  # загруженная модель: {"translator", "tokenizer", "prefix"}
_local_lock = threading.Lock()


def local_engine_available():
    """Есть ли в программе движок перевода (библиотеки ctranslate2 и sentencepiece). Сами библиотеки
    при этом не загружаются — это долго, а нужно только при первом переводе."""
    import importlib.util
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("ctranslate2", "sentencepiece"))
    except (ImportError, ValueError):
        return False


def local_model_info():
    """Модуль перевода скачан: {"size": байт на диске, "sha256", "installed": когда}. None — не скачан."""
    if not os.path.exists(os.path.join(LOCAL_MODEL_DIR, "model", "model.bin")):
        return None
    try:
        with open(os.path.join(LOCAL_MODEL_DIR, "source.json"), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError):
        info = {}
    return {"size": folder_size(LOCAL_MODEL_DIR), "sha256": info.get("sha256", ""),
            "installed": info.get("installed", 0)}


def _model_members(archive):
    """Какие файлы взять из архива модели: {путь внутри папки модуля: запись архива}. Странный архив — ValueError."""
    members = archive.infolist()
    if len(members) > LOCAL_MODEL_MAX_FILES or sum(m.file_size for m in members) > LOCAL_MODEL_MAX_BYTES * 2:
        raise ValueError("архив модели больше ожидаемого")
    wanted = {}
    for member in members:
        name = member.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p]
        if name.startswith("/") or ":" in name or ".." in parts:
            raise ValueError("в архиве модели странные пути — не распаковываю")
        if member.is_dir() or len(parts) < 2:
            continue
        inner = parts[1:]  # всё лежит в папке вида translate-en_ru-1_9/
        if len(inner) == 2 and inner[0] == "model" and re.fullmatch(r"[\w.-]+", inner[1]):
            wanted["model/" + inner[1]] = member
        elif len(inner) == 1 and inner[0] in ("sentencepiece.model", "metadata.json"):
            wanted[inner[0]] = member
    if "model/model.bin" not in wanted or "sentencepiece.model" not in wanted:
        raise ValueError("в архиве нет модели перевода — формат не тот")
    return wanted


def download_local_model(progress=None):
    """Скачать и поставить модуль перевода. progress(текст, проценты) — по ходу дела. Вызывается в фоновом потоке.
    Возвращает {"size", "sha256", "sample"} — sample: пробный перевод, которым проверено, что модуль работает."""
    import shutil
    import zipfile

    os.makedirs(os.path.dirname(LOCAL_MODEL_DIR), exist_ok=True)
    archive_path = LOCAL_MODEL_DIR + ".download"
    staging = LOCAL_MODEL_DIR + ".new"
    digest, done = hashlib.sha256(), 0
    try:
        with requests.get(LOCAL_MODEL_URL, timeout=REQUEST_TIMEOUT * 3, stream=True) as response:
            response.raise_for_status()
            if not response.url.startswith("https://"):
                raise ValueError("модель пришла не по защищённому адресу — не ставлю")
            total = int(response.headers.get("Content-Length") or 0)
            if total > LOCAL_MODEL_MAX_BYTES:
                raise ValueError("файл модели больше ожидаемого — не ставлю")
            with open(archive_path, "wb") as f:
                for chunk in response.iter_content(512 * 1024):
                    done += len(chunk)
                    if done > LOCAL_MODEL_MAX_BYTES:
                        raise ValueError("файл модели больше ожидаемого — не ставлю")
                    digest.update(chunk)
                    f.write(chunk)
                    if progress:
                        mb = f"{done / 2 ** 20:.0f}" + (f" из {total / 2 ** 20:.0f}" if total else "")
                        progress(f"Скачиваю модуль перевода… {mb} МБ", done * 90 // total if total else -1)
        sha256 = digest.hexdigest()
        if LOCAL_MODEL_SHA256 and not hmac.compare_digest(sha256, LOCAL_MODEL_SHA256):
            raise ValueError("контрольная сумма файла не совпала: он подменён или повреждён — модуль не поставлен")
        if progress:
            progress("Распаковываю модуль перевода…", 92)
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(archive_path) as archive:
            for inner, member in _model_members(archive).items():
                target = os.path.join(staging, *inner.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
        save_json(os.path.join(staging, "source.json"),
                  {"url": LOCAL_MODEL_URL, "sha256": sha256, "bytes": done, "installed": time.time()})
        _unload_local_engine()
        shutil.rmtree(LOCAL_MODEL_DIR, ignore_errors=True)
        os.replace(staging, LOCAL_MODEL_DIR)
    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)
        shutil.rmtree(staging, ignore_errors=True)
    log.info("Модуль перевода скачан: %s, %.0f МБ, SHA-256 %s%s", LOCAL_MODEL_URL, done / 2 ** 20, sha256,
             " — совпала с зашитой в программу" if LOCAL_MODEL_SHA256
             else " (сумма ещё не зашита в программу — пришлите эту строку автору)")
    if progress:
        progress("Проверяю перевод…", 96)
    sample = local_translate("The adventure begins. Explore the world and save your friends.")
    if not sample or not _is_russian(sample):
        raise ValueError("модуль скачан, но перевести пробную фразу не смог — подробности в логе")
    log.info("Модуль перевода работает: «%s»", sample)
    return {"size": folder_size(LOCAL_MODEL_DIR), "sha256": sha256, "sample": sample}


def remove_local_model():
    import shutil

    _unload_local_engine()
    shutil.rmtree(LOCAL_MODEL_DIR)
    log.info("Модуль перевода удалён")


def _unload_local_engine():
    with _local_lock:
        translator = _local_engine.pop("translator", None)
        _local_engine.clear()
    if translator is not None:
        try:
            translator.unload_model()  # иначе Windows не даст удалить файл модели
        except (AttributeError, RuntimeError):
            pass


def _load_local_engine():
    with _local_lock:
        if _local_engine:
            return _local_engine
        import ctranslate2
        import sentencepiece

        started = time.monotonic()
        threads = max(1, min(4, (os.cpu_count() or 2) - 1))
        translator = ctranslate2.Translator(os.path.join(LOCAL_MODEL_DIR, "model"), device="cpu",
                                            inter_threads=1, intra_threads=threads)
        tokenizer = sentencepiece.SentencePieceProcessor(
            model_file=os.path.join(LOCAL_MODEL_DIR, "sentencepiece.model"))
        try:
            with open(os.path.join(LOCAL_MODEL_DIR, "metadata.json"), encoding="utf-8") as f:
                prefix = (json.load(f) or {}).get("target_prefix") or ""
        except (OSError, json.JSONDecodeError, AttributeError):
            prefix = ""
        _local_engine.update(translator=translator, tokenizer=tokenizer, prefix=prefix)
        log.info("Модуль перевода загружен за %.1f с (потоков: %d)", time.monotonic() - started, threads)
        return _local_engine


def local_translate(text):
    """Перевод с английского на русский модулем на этом ПК. None — модуль не скачан или не справился."""
    if not text.strip() or local_model_info() is None or not local_engine_available():
        return None
    started = time.monotonic()
    try:
        engine = _load_local_engine()
        translator, tokenizer, prefix = engine["translator"], engine["tokenizer"], engine["prefix"]
        lines = []
        for paragraph in text.split("\n"):
            sentences = [s for s in LOCAL_SENTENCE_RE.split(paragraph.strip()) if s.strip()]
            if not sentences:
                lines.append("")
                continue
            batch = [tokenizer.encode(sentence, out_type=str) for sentence in sentences]
            results = translator.translate_batch(
                batch, beam_size=2, max_batch_size=16, replace_unknowns=True, length_penalty=0.2,
                max_decoding_length=300, target_prefix=[[prefix]] * len(batch) if prefix else None)
            words = []
            for result in results:
                tokens = list(result.hypotheses[0])
                if prefix and tokens and tokens[0] == prefix:
                    tokens = tokens[1:]
                words.append(tokenizer.decode(tokens))
            lines.append(" ".join(words))
    except Exception as e:  # noqa: BLE001 — движок на C++ бросает разное; без перевода программа работает
        log.warning("Модуль перевода не справился: %s", e)
        return None
    translated = "\n".join(lines).strip()
    log.info("Переведено на этом ПК: %d знаков за %.1f с", len(text), time.monotonic() - started)
    return translated or None


TRANSLATE_CHUNK = 1500  # символов за один запрос: длинный адрес переводчик не принимает


def translate_long(text):
    """Перевод длинного текста по абзацам, кусками. None — хотя бы один кусок не перевёлся."""
    chunks, current = [], ""
    for paragraph in text.split("\n"):
        while len(paragraph) > TRANSLATE_CHUNK:  # огромный абзац — режем по длине
            chunks.append(paragraph[:TRANSLATE_CHUNK])
            paragraph = paragraph[TRANSLATE_CHUNK:]
        if current and len(current) + len(paragraph) + 1 > TRANSLATE_CHUNK:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    parts = []
    for chunk in chunks:
        if not chunk.strip():
            parts.append(chunk)
            continue
        translated = translate_to_russian(chunk)
        if translated is None:
            return None
        parts.append(translated)
    return "\n".join(parts)


def _to_russian(text):
    """(текст, переведён ли). Русский текст и неудачный перевод возвращаются как есть."""
    if not text or _is_russian(text):
        return text, False
    translated = translate_long(text)
    return (translated, True) if translated else (text, False)


REVIEW_EXCERPTS = 3
REVIEW_MAX_CHARS = 450
# 22 октября 2026 Steam отключил старый адрес store.steampowered.com/appreviews (теперь он ведёт
# на главную магазина). Новый метод Web API работает без ключа, параметры передаются в input_json.
STEAM_REVIEWS_API_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
REVIEWS_FILTER_HELPFUL = 0  # самые полезные
REVIEWS_PURCHASE_ALL = 1  # и купившие в Steam, и получившие игру иначе
STEAMSPY_URL = "https://steamspy.com/api.php"


def _reviews_json(appid, **params):
    """Страница отзывов из нового метода Steam. Ответ лежит внутри "response"."""
    query = {"appid": int(appid), "purchase_type": REVIEWS_PURCHASE_ALL, "display_language": "russian", **params}
    response = fetch(STEAM_REVIEWS_API_URL, params={"input_json": json.dumps(query)})
    data = response.json().get("response")
    if not isinstance(data, dict):
        raise ValueError("Steam прислал отзывы в непонятном виде")
    return data


def _steamspy_summary(appid):
    """Запасной источник: число положительных и отрицательных отзывов Steam по данным SteamSpy."""
    data = fetch(STEAMSPY_URL, params={"request": "appdetails", "appid": appid}).json()
    positive, negative = int(data.get("positive") or 0), int(data.get("negative") or 0)
    if not positive + negative:
        return None
    total = positive + negative
    return {"review_score_desc": score_description(positive, total), "total_positive": positive,
            "total_reviews": total}


def score_description(positive, total):
    """Подпись оценки по правилам Steam: «Очень положительные», «Смешанные» и т. д."""
    if not total:
        return ""
    share = positive / total * 100
    if share >= 80:
        if share >= 95 and total >= 500:
            return "Крайне положительные"
        return "Очень положительные" if total >= 50 else "Положительные"
    if share >= 70:
        return "В основном положительные"
    if share >= 40:
        return "Смешанные"
    if share >= 20:
        return "В основном отрицательные"
    if total >= 500:
        return "Крайне отрицательные"
    return "Очень отрицательные" if total >= 50 else "Отрицательные"


def fetch_reviews(appid):
    """Отзывы игроков из Steam: общая оценка и пара самых полезных отзывов на русском.

    Нет русских — берём английские и переводим. None — Steam не ответил.
    """
    try:
        # Общая оценка по всем языкам приходит в query_summary вместе с первой страницей отзывов
        summary = _reviews_json(appid, languages=["all"], num_per_page=1).get("query_summary") or {}
    except (requests.RequestException, ValueError) as e:
        log.warning("Не удалось загрузить оценку Steam для %s: %s — спрашиваем SteamSpy", appid, e)
        try:
            summary = _steamspy_summary(appid)
        except (requests.RequestException, ValueError) as e:
            log.warning("SteamSpy тоже не ответил для %s: %s", appid, e)
            summary = None
        if not summary:
            return None
        # Тексты отзывов SteamSpy не отдаёт, а Steam только что отказал — не мучаем его второй раз
        return {"version": REVIEWS_VERSION, "score": summary["review_score_desc"],
                "positive": summary["total_positive"], "total": summary["total_reviews"],
                "excerpts": [], "fetched": time.time()}

    excerpts = []
    try:
        for language in ("russian", "english"):
            data = _reviews_json(appid, languages=[language], filter=REVIEWS_FILTER_HELPFUL, day_range=365,
                                 num_per_page=REVIEW_EXCERPTS * 3)
            for review in data.get("reviews") or []:
                if len(excerpts) >= REVIEW_EXCERPTS:
                    break
                text = re.sub(r"\[/?[a-z0-9*=]+\]", "", review.get("review", ""))  # разметка BBCode
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) < 40:
                    continue  # «10/10», «топ» и подобное нам не интересно
                if len(text) > REVIEW_MAX_CHARS:
                    text = text[:REVIEW_MAX_CHARS].rsplit(" ", 1)[0] + "…"
                translated = False
                if language != "russian":
                    russian = translate_to_russian(text)
                    if russian:
                        text, translated = russian, True
                hours = (review.get("author") or {}).get("playtime_forever", 0) / 60
                excerpts.append({"text": text, "up": bool(review.get("voted_up")),
                                 "hours": round(hours, 1), "translated": translated})
            if excerpts:
                break
    except (requests.RequestException, ValueError) as e:
        log.warning("Не удалось загрузить тексты отзывов Steam для %s: %s", appid, e)  # оценку всё равно покажем
    positive, total = summary.get("total_positive", 0), summary.get("total_reviews", 0)
    return {
        "version": REVIEWS_VERSION,
        "score": summary.get("review_score_desc") or score_description(positive, total),
        "positive": positive,
        "total": total,
        "excerpts": excerpts,
        "fetched": time.time(),
    }


REVIEWS_CACHE_FILE = os.path.join(BASE_DIR, "steam_reviews.json")
REVIEWS_CACHE_TTL = 24 * 3600  # отзывы меняются часто — обновляем раз в сутки
REVIEWS_VERSION = 2  # 2: новый метод Steam; записи, собранные через SteamSpy без цитат, перезапрашиваются
_reviews_lock = threading.Lock()


def get_reviews(appid):
    """Отзывы из кэша steam_reviews.json, а если их нет или они старше суток — из Steam."""
    key = str(appid)
    with _reviews_lock:
        try:
            with open(REVIEWS_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
    cached = cache.get(key) if isinstance(cache, dict) else None
    if (cached and cached.get("version") == REVIEWS_VERSION
            and time.time() - cached.get("fetched", 0) < REVIEWS_CACHE_TTL):
        return cached
    reviews = fetch_reviews(appid)
    if reviews is None:
        return cached  # Steam не ответил — покажем старые, если были
    with _reviews_lock:
        try:
            with open(REVIEWS_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                cache = {}
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        cache[key] = reviews
        try:
            save_json(REVIEWS_CACHE_FILE, cache)
        except OSError:
            log.exception("Не удалось сохранить кэш отзывов")
    return reviews


def fetch_game_details(appid):
    """Карточка игры со страницы магазина Steam на русском. None — магазин эту игру не отдаёт."""
    info = {}
    # Часть игр недоступна в магазине вашего региона — тогда спрашиваем американский
    for region in ({}, {"cc": "us"}):
        data = fetch(STEAM_APP_DETAILS_URL, params={"appids": appid, "l": "russian", **region}).json()
        info = (data or {}).get(str(appid)) or {}
        if info.get("success"):
            break
    if not info.get("success"):
        return None

    d = info.get("data") or {}
    release = d.get("release_date") or {}
    movies = []
    for movie in d.get("movies") or []:
        sources = _movie_sources(movie)
        if sources:
            movies.append({"name": movie.get("name", ""), "thumb": movie.get("thumbnail"), "sources": sources})

    # Русского описания у игры нет — Steam присылает английское. Переводит его translate_details отдельно,
    # уже после показа страницы: переводчик бывает медленным, и страница не должна его ждать.
    # Названия, разработчиков и жанры оставляем как в Steam.
    description = _strip_html(d.get("short_description"))
    about = _strip_html(d.get("about_the_game") or d.get("detailed_description"))
    if about == description or len(about) <= len(description) + 40:
        about = ""  # полное описание ничего не добавляет к краткому
    return {
        "version": DETAILS_VERSION,
        "appid": appid,
        "name": d.get("name", ""),
        "description": description,
        "about": about,
        "translated": False,
        "metacritic_url": (d.get("metacritic") or {}).get("url"),
        "achievements_total": (d.get("achievements") or {}).get("total", 0),
        "genres": [g["description"] for g in d.get("genres") or [] if g.get("description")],
        "developers": d.get("developers") or [],
        "release_date": release.get("date", ""),
        "coming_soon": bool(release.get("coming_soon")),
        "metacritic": (d.get("metacritic") or {}).get("score"),
        "header": d.get("header_image"),
        "screenshots": [{"thumb": s.get("path_thumbnail"), "full": s["path_full"]}
                        for s in (d.get("screenshots") or [])[:MAX_SCREENSHOTS] if s.get("path_full")],
        "movies": movies[:MAX_MOVIES],
        # У демоверсии — appid полной игры: если у демо нет своих скриншотов, показываем её
        "fullgame": int((d.get("fullgame") or {}).get("appid") or 0) or None,
        "fetched": time.time(),
    }


def _load_details_cache():
    try:
        with open(DETAILS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


UNTRANSLATED_TTL = 3600  # переводчик отказал (часто «слишком много запросов») — пробуем снова через час


def _details_ttl(details):
    """Сколько хранить карточку: две недели, но час — если описание осталось непереведённым."""
    if needs_translation(details):
        return UNTRANSLATED_TTL
    return DETAILS_CACHE_TTL


def needs_translation(details):
    """Есть ли в карточке описание не на русском — тогда его переводит translate_details."""
    return any(text and not _is_russian(text) for text in (details.get("description"), details.get("about")))


def translate_details(details):
    """Описание карточки на русском: карточка с переводом (она же сохраняется в кэш) или None —
    переводить нечего или переводчик не ответил (тогда остаётся оригинал, попробуем при следующем открытии)."""
    if not needs_translation(details):
        return None
    result, changed = dict(details), False
    for key in ("description", "about"):
        text, translated = _to_russian(details.get(key) or "")
        if translated:
            result[key], changed = text, True
    if not changed:
        return None
    result["translated"] = True
    with _details_lock:
        cache = _load_details_cache()
        # В кэш — только перевод: скриншоты полной игры у демо (media_from) туда не попадают
        entry = dict(cache.get(str(details["appid"])) or details)
        entry.pop("media_from", None)
        entry.update(description=result["description"], about=result["about"], translated=True)
        cache[str(details["appid"])] = entry
        try:
            save_json(DETAILS_CACHE_FILE, cache)
        except OSError:
            log.exception("Не удалось сохранить кэш карточек игр")
    return result


def get_game_details(appid, _parent=True):
    """Карточка из кэша steam_details.json, а если её нет или она старше двух недель — из магазина."""
    key = str(appid)
    with _details_lock:
        cached = _load_details_cache().get(key)
    # До 2.3 у карточек не было "fullgame" — карточку без скриншотов (обычно демо) перезапрашиваем
    stale = cached and "fullgame" not in cached and not cached.get("screenshots")
    if (cached and not stale and cached.get("version") == DETAILS_VERSION
            and time.time() - cached.get("fetched", 0) < _details_ttl(cached)):
        return _with_parent_media(cached) if _parent else cached
    try:
        details = fetch_game_details(appid)
    except (requests.RequestException, ValueError):
        if cached:  # нет сети — показываем старую карточку
            log.warning("Не удалось обновить карточку %s, показываю сохранённую", appid)
            return cached
        raise
    if details:
        with _details_lock:
            cache = _load_details_cache()
            cache[key] = details
            try:
                save_json(DETAILS_CACHE_FILE, cache)
            except OSError:
                log.exception("Не удалось сохранить кэш карточек игр")
    return _with_parent_media(details) if details and _parent else details


def _with_parent_media(details):
    """У демоверсии нет своих скриншотов и трейлеров — берём их у полной игры. В кэш это не пишется."""
    parent = details.get("fullgame")
    if details.get("screenshots") or not parent or parent == details.get("appid"):
        return details
    try:
        full = get_game_details(parent, _parent=False)
    except (requests.RequestException, ValueError) as e:
        log.info("Скриншоты полной игры %s для демо %s не загрузились: %s", parent, details.get("appid"), e)
        return details
    if not full or not full.get("screenshots"):
        return details
    log.info("У демо %s нет своих скриншотов — показываю скриншоты полной игры %s", details.get("appid"), parent)
    return dict(details, screenshots=full["screenshots"], movies=details.get("movies") or full.get("movies") or [],
                media_from=full.get("name") or "")


def get_achievements(appid, api_key, steam_id):
    """Достижения игры: названия и описания на русском, доля игроков, получено ли у вас.

    Нужен ключ Steam Web API. Свой прогресс берётся из Web API, а если он отказал — со страницы профиля;
    не вышло и там — "unlocked" = None, а в "progress_note" — почему.
    Возвращает None, если у игры нет достижений.
    """
    if not api_key:
        return {"error": "Достижения Steam отдаёт только с ключом Steam Web API.", "no_key": True}
    schema = fetch(STEAM_SCHEMA_URL, params={"key": api_key, "appid": appid, "l": "russian"}).json()
    items = ((schema.get("game") or {}).get("availableGameStats") or {}).get("achievements") or []
    if not items:
        return None

    percents = {}
    try:
        data = fetch(STEAM_GLOBAL_ACHIEVEMENTS_URL, params={"gameid": appid}).json()
        for a in (data.get("achievementpercentages") or {}).get("achievements") or []:
            percents[a["name"]] = float(a.get("percent") or 0)
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("Нет статистики достижений для %s: %s", appid, e)

    mine, progress_note, private = None, "", False
    if steam_id:
        mine, progress_note, private = _player_achievements(appid, api_key, steam_id)
    else:
        progress_note = "Чтобы видеть свой прогресс, укажите SteamID64 в настройках."

    result = []
    for a in items:
        api_name = a.get("name", "")
        unlocked_at = mine.get(api_name.casefold()) if mine is not None else None
        result.append({
            "name": a.get("displayName") or api_name,
            "description": a.get("description", ""),
            "hidden": bool(a.get("hidden")),
            "icon": a.get("icon"),
            "icon_gray": a.get("icongray") or a.get("icon"),
            "percent": percents.get(api_name),
            "unlocked": unlocked_at is not None,
            "unlocked_at": unlocked_at or 0,
        })
    # Сначала полученные (свежие выше), потом остальные — от самых частых к редким
    result.sort(key=lambda a: (not a["unlocked"], -a["unlocked_at"], -(a["percent"] or 0)))
    unlocked = sum(a["unlocked"] for a in result) if mine is not None else None
    return {"items": result, "unlocked": unlocked, "progress_note": progress_note, "private": private}


def _player_achievements(appid, api_key, steam_id):
    """Полученные достижения: ({api-имя в нижнем регистре: время получения}, "", False) или
    (None, почему не узнали, закрыт ли профиль).

    Web API отказывает (403) в играх не с этого аккаунта — например, из семейной библиотеки, — хотя
    достижения в них ваши и видны на странице профиля. Тогда берём их оттуда: страница отдаёт XML
    без ключа, если игровая информация профиля открыта. Причину отказа Steam пишет только в теле ответа.
    """
    reason = ""
    try:
        data = fetch(STEAM_PLAYER_ACHIEVEMENTS_URL,
                     params={"key": api_key, "steamid": steam_id, "appid": appid, "l": "english"}).json()
        stats = data.get("playerstats") or {}
        if stats.get("success"):
            return {a["apiname"].casefold(): a.get("unlocktime", 0) for a in stats.get("achievements") or []
                    if a.get("achieved") and a.get("apiname")}, "", False
        reason = stats.get("error") or "Steam не прислал прогресс"
    except requests.HTTPError as e:
        try:
            reason = (e.response.json().get("playerstats") or {}).get("error") or ""
        except ValueError:
            pass
        reason = f"{e.response.status_code}" + (f" — {reason}" if reason else "")
    except (requests.RequestException, ValueError) as e:
        reason = mask_secrets(e)

    mine = _profile_achievements(appid, steam_id)
    private = "not public" in reason.casefold()
    # Web API сказал «профиль закрыт», а страница профиля вернула пустой список — это не «ничего не получено»,
    # а тоже закрытый профиль: не показываем ложное «0 из N»
    if mine is not None and not (private and not mine):
        log.info("Прогресс достижений для %s: Web API отказал (%s), взял со страницы профиля — получено %d",
                 appid, reason, len(mine))
        return mine, "", False
    log.info("Прогресс достижений для %s недоступен: Web API — %s, страница профиля тоже не отдала", appid, reason)
    if private:
        return None, ("Steam не отдаёт ваш прогресс: в настройках приватности профиля закрыта "
                      "«Игровая информация». Откройте её — и прогресс появится."), True
    return None, ("Steam не отдаёт ваш прогресс в этой игре — обычно так бывает с играми из семейной "
                  "библиотеки или не с этого аккаунта."), False


STEAM_PROFILE_ACHIEVEMENTS_URL = "https://steamcommunity.com/profiles/{steam_id}/stats/{appid}/achievements/"


def _profile_achievements(appid, steam_id):
    """Полученные достижения со страницы профиля Steam (XML). None — страница их не отдала."""
    try:
        response = fetch(STEAM_PROFILE_ACHIEVEMENTS_URL.format(steam_id=steam_id, appid=appid),
                         params={"xml": 1, "l": "english"})
    except requests.RequestException as e:
        log.info("Страница достижений профиля для %s не открылась: %s", appid, e)
        return None
    text = response.text
    if "<achievements>" not in text:
        return None  # закрытый профиль, нет игры или Steam прислал HTML вместо XML
    mine = {}
    for block in re.findall(r'<achievement closed="1">(.*?)</achievement>', text, re.DOTALL):
        name = re.search(r"<apiname>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</apiname>", block, re.DOTALL)
        when = re.search(r"<unlockTimestamp>(\d+)</unlockTimestamp>", block)
        if name:
            mine[html.unescape(name.group(1)).strip().casefold()] = int(when.group(1)) if when else 0
    return mine


def fetch_image(url):
    """Картинка для карточки (скриншот, превью трейлера). None — не скачалась."""
    try:
        return fetch(url).content
    except requests.RequestException as e:
        log.warning("Не удалось скачать картинку %s: %s", url, e)
        return None


# --- Фон и логотип для страницы игры -------------------------------------------
# Сначала официальные картинки Steam, затем SteamGridDB (разделы Heroes и Logos).
# Скачанное хранится в cache/hero и cache/logo; файл «.none» рядом значит «нигде нет, не искать снова».
HERO_WIDTH = 1920
LOGO_SIZE = (800, 320)
ART_KINDS = ("hero", "logo")
# Отметка «картинки нет» помнит, по каким правилам искали. До 2.3 настоящие адреса картинок Steam
# не спрашивались, и у новых игр фон с логотипом считались ненайденными — такие отметки ищем заново.
MISS_VERSION = "2"


def _known_missing(marker):
    return os.path.exists(marker) and read_text(marker) == MISS_VERSION


def _art_path(game, kind):
    key = str(game["appid"]) if game.get("appid") else "name_" + clean_name(game["name"])
    return os.path.join(CACHE_DIR, kind, key + (".png" if kind == "logo" else ".jpg"))


def _trimmed_logo(path):
    """Логотип без прозрачных полей вокруг: у одних картинок поля огромные, у других их нет,
    и из-за этого логотипы выглядели то крупными, то мелкими. Обрезанная копия — рядом, «_t.png»."""
    trimmed = path[:-4] + "_t.png"
    if os.path.exists(trimmed):
        return trimmed
    try:
        image = Image.open(path).convert("RGBA")
        # Почти прозрачные пиксели (тени, сглаживание) за границу не считаем
        bbox = image.getchannel("A").point(lambda a: 255 if a > 12 else 0).getbbox()
        if bbox:
            image = image.crop(bbox)
        image.save(trimmed)
        return trimmed
    except OSError as e:
        log.warning("Не удалось обрезать логотип %s: %s", path, e)
        return path


def _griddb_game_id(game, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    if game.get("appid"):
        try:
            data = fetch(f"{STEAMGRIDDB_API_URL}/games/steam/{game['appid']}", headers=headers).json()
            if data.get("data"):
                return data["data"]["id"]
        except requests.RequestException:
            pass  # по appid не нашлось — ищем по названию
    search_url = f"{STEAMGRIDDB_API_URL}/search/autocomplete/{quote(game['name'], safe='')}"
    found = fetch(search_url, headers=headers).json().get("data", [])
    return found[0]["id"] if found else None


def _download_art(game, kind, api_key):
    """Байты фона или логотипа: из Steam (по старому адресу, затем по настоящему из магазина),
    иначе из SteamGridDB. None — нигде нет."""
    if game.get("appid"):
        url = (STEAM_HERO_URL if kind == "hero" else STEAM_LOGO_URL).format(appid=game["appid"])
        try:
            return fetch(url).content
        except requests.RequestException:
            pass  # у новых игр Steam хранит картинки по другим адресам — спрашиваем их у магазина
        content = _steam_asset(game["appid"], kind)
        if content:
            return content
    if not api_key:
        return None
    game_id = _griddb_game_id(game, api_key)
    if not game_id:
        return None
    section = "heroes" if kind == "hero" else "logos"
    headers = {"Authorization": f"Bearer {api_key}"}
    items = fetch(f"{STEAMGRIDDB_API_URL}/{section}/game/{game_id}", headers=headers).json().get("data", [])
    return fetch(items[0]["url"]).content if items else None


def _blurred_cover_hero(game):
    """Запасной фон: середина обложки, размытая и чуть затемнённая."""
    cover = find_cover(game["name"])
    if not cover:
        return None
    image = Image.open(cover).convert("RGB")
    width = HERO_WIDTH // 2
    height = round(width * 620 / 1920)  # пропорции фонов Steam
    image = image.resize((width, round(image.height * width / image.width)), Image.Resampling.LANCZOS)
    top = max(0, (image.height - height) // 2)
    image = image.crop((0, top, width, top + height)).filter(ImageFilter.GaussianBlur(22))
    image = ImageEnhance.Brightness(image).enhance(0.8)
    path = _art_path(game, "hero")[:-4] + "_blur.jpg"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(path, quality=88)
    return path


def get_artwork(game, kind, api_key=""):
    """Путь к фону (kind="hero") или логотипу ("logo") для страницы игры. None — картинки нет.

    Вызывается в фоновом потоке: может скачивать из интернета.
    """
    path = _art_path(game, kind)
    if os.path.exists(path):
        return _trimmed_logo(path) if kind == "logo" else path
    missing = path + ".none"
    image = None
    if not _known_missing(missing):
        try:
            content = _download_art(game, kind, api_key)
            if content:
                image = Image.open(io.BytesIO(content))
                image.load()
            else:
                os.makedirs(os.path.dirname(missing), exist_ok=True)
                write_text(missing, MISS_VERSION)
        except (requests.RequestException, OSError, ValueError, KeyError) as e:
            log.warning("Не удалось получить %s для «%s»: %s", kind, game["name"], mask_secrets(e))

    if image is not None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if kind == "hero":
            image = image.convert("RGB")
            if image.width > HERO_WIDTH:
                image = image.resize((HERO_WIDTH, round(image.height * HERO_WIDTH / image.width)),
                                     Image.Resampling.LANCZOS)
            image.save(path, quality=90)
        else:
            image = image.convert("RGBA")
            image.thumbnail(LOGO_SIZE, Image.Resampling.LANCZOS)
            image.save(path)
            return _trimmed_logo(path)
        return path
    if kind == "hero":
        return _blurred_cover_hero(game)
    return None


def _animated_path(game, index=None):
    """Выбранный живой фон игры (index None) или просмотренный вариант номер index — пока его не выбрали."""
    base = _art_path(game, "hero")[:-4] + "_anim"
    return base + (".webp" if index is None else f"_{index}.webp")


def _animated_list_path(game):
    return _animated_path(game)[:-5] + ".json"


ANIMATED_MAX = 30  # больше вариантов живого фона не показываем


def _load_animated_list(game):
    """{"urls": [адреса вариантов], "chosen": номер выбранного или None} или None — список ещё не спрашивали."""
    try:
        with open(_animated_list_path(game), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("urls"), list):
            chosen = data.get("chosen")
            return {"urls": [u for u in data["urls"] if isinstance(u, str)],
                    "chosen": chosen if isinstance(chosen, int) else None}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _save_animated_list(game, data):
    os.makedirs(os.path.dirname(_animated_list_path(game)), exist_ok=True)
    save_json(_animated_list_path(game), data)


def discard_animated_variants(game):
    """Удалить просмотренные, но не выбранные варианты живого фона. Возвращает, сколько байт освободилось."""
    base = os.path.basename(_animated_path(game))[:-5] + "_"
    folder = os.path.dirname(_animated_path(game))
    freed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if name.startswith(base) and (name.endswith(".webp") or name.endswith(".webp.tmp")):
            path = os.path.join(folder, name)
            try:
                size = os.path.getsize(path)
                os.remove(path)
                freed += size
            except OSError:
                pass  # файл ещё открыт — удалится в следующий раз
    return freed


def find_animated_hero(game, api_key):
    """Есть ли у игры живые фоны в SteamGridDB (анимированный WebP): {"path": выбранный и скачанный файл
    или None, "urls": адреса всех вариантов, "chosen": номер выбранного или None} или None — ключа нет или
    живого фона у игры нет.

    Сами файлы здесь не качаются — только по кнопке на странице игры (download_animated_hero): живые фоны
    весят до 50 МБ. Список вариантов запоминается рядом с фонами («_anim.json»), «.none» — живого фона нет
    (забывается после смены ключа SteamGridDB, как и для обычных фонов). Просмотренные, но не выбранные
    варианты, оставшиеся с прошлого раза, здесь же удаляются.
    """
    if not api_key:
        return None
    discard_animated_variants(game)
    path = _animated_path(game)
    kept = path if os.path.exists(path) else None
    data = _load_animated_list(game)
    if data and data["urls"]:
        return {"path": kept, "urls": data["urls"], "chosen": data["chosen"] if kept else None}
    url_file, missing = path + ".url", path + ".none"
    if os.path.exists(missing) and not kept:
        return None
    try:
        game_id = _griddb_game_id(game, api_key)
        items = []
        if game_id:
            # Qt проигрывает анимированный WebP; анимированный PNG он показал бы неподвижной картинкой
            items = fetch(f"{STEAMGRIDDB_API_URL}/heroes/game/{game_id}",
                          headers={"Authorization": f"Bearer {api_key}"},
                          params={"types": "animated", "mimes": "image/webp"}).json().get("data", [])
        urls = list(dict.fromkeys(item["url"] for item in items if item.get("url")))[:ANIMATED_MAX]
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as e:
        log.warning("Не удалось узнать про живой фон для «%s»: %s", game["name"], mask_secrets(e))
        return {"path": kept, "urls": [], "chosen": None} if kept else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not urls:
        if not kept:
            write_text(missing, "")
            return None
        return {"path": kept, "urls": [], "chosen": None}
    chosen = None
    if kept:  # до 2.3 скачивался только первый вариант, его адрес лежал в «.url»
        old = read_text(url_file)
        chosen = urls.index(old) if old in urls else 0
    _save_animated_list(game, {"urls": urls, "chosen": chosen})
    for stale in (url_file, missing):
        if os.path.exists(stale):
            os.remove(stale)
    log.info("Живой фон для «%s»: вариантов в SteamGridDB — %d", game["name"], len(urls))
    return {"path": kept, "urls": urls, "chosen": chosen}


def download_animated_hero(game, index, url, progress=None, cancelled=None):
    """Скачать вариант живого фона номер index. Первый скачанный сразу становится выбранным, остальные лежат
    отдельными файлами, пока пользователь не выберет (keep_animated_hero) или не уйдёт со страницы игры.
    progress(проценты или -1, если сервер не сказал размер) — по ходу загрузки, cancelled() → True — бросить
    (открыли другую игру). Путь к файлу или None, если загрузку бросили."""
    data = _load_animated_list(game) or {"urls": [url], "chosen": None}
    kept = _animated_path(game)
    if os.path.exists(kept) and data["chosen"] == index:
        return kept
    first = not os.path.exists(kept)
    path = kept if first else _animated_path(game, index)
    if os.path.exists(path):
        return path
    tmp_path = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        total, done = int(response.headers.get("Content-Length") or 0), 0
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(256 * 1024):
                if cancelled and cancelled():
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(min(99, done * 100 // total) if total else -1)
    if cancelled and cancelled():
        os.remove(tmp_path)
        return None
    os.replace(tmp_path, path)
    if first:
        data["chosen"] = index
        _save_animated_list(game, data)
    log.info("Живой фон для «%s», вариант %d: %d КБ%s", game["name"], index + 1, done // 1024,
             " — выбран" if first else "")
    return path


def keep_animated_hero(game, index):
    """Оставить вариант живого фона номер index, остальные просмотренные удалить.
    Возвращает {"path": выбранный файл, "freed": сколько байт освободилось}."""
    data = _load_animated_list(game) or {"urls": [], "chosen": None}
    kept = _animated_path(game)
    variant = _animated_path(game, index)
    freed = 0
    if data["chosen"] != index and os.path.exists(variant):
        if os.path.exists(kept):
            freed += os.path.getsize(kept)
        os.replace(variant, kept)
        data["chosen"] = index
        _save_animated_list(game, data)
    freed += discard_animated_variants(game)
    log.info("Живой фон для «%s»: оставлен вариант %d, освобождено %d КБ", game["name"], index + 1, freed // 1024)
    return {"path": kept, "freed": freed}


def make_backdrop(hero_path):
    """Подложка для всей страницы игры: фон, сильно размытый и затемнённый. Маленькая — её растянут."""
    path = os.path.splitext(hero_path)[0] + "_backdrop.jpg"
    if os.path.exists(path):
        return path
    image = Image.open(hero_path).convert("RGB")
    image = image.resize((480, max(1, round(image.height * 480 / image.width))), Image.Resampling.LANCZOS)
    image = image.filter(ImageFilter.GaussianBlur(18))
    image = ImageEnhance.Brightness(image).enhance(0.75)
    image.save(path, quality=85)
    return path


def clear_artwork_misses():
    """Забыть, для каких игр не нашлось фона и логотипа, — после смены ключа SteamGridDB."""
    for kind in ART_KINDS:
        folder = os.path.join(CACHE_DIR, kind)
        if os.path.isdir(folder):
            for file_name in os.listdir(folder):
                if file_name.endswith(".none"):
                    os.remove(os.path.join(folder, file_name))


# --- Саундтреки Steam -------------------------------------------------------
# Купленный саундтрек Steam качает в <библиотека>\steamapps\music\<папка альбома>, а рядом с играми кладёт
# appmanifest_<appid>.acf: в нём appid альбома, его название и имя папки (installdir). К какой игре относится
# альбом, узнаём у магазина: открытый IStoreBrowseService/GetItems пачкой отвечает, какие приложения —
# саундтреки (тип 11) и чей это саундтрек (parent_appid). Ключ API для него не нужен.
# Какие саундтреки куплены, Web API не отдаёт — это знает только магазин после входа на сайт
# (store.steampowered.com/dynamicstore/userdata), его читает встроенный браузер программы.
SOUNDTRACKS_FILE = os.path.join(BASE_DIR, "steam_soundtracks.json")
SOUNDTRACKS_RETRY = 24 * 3600  # магазин не ответил — спросим снова через сутки
SOUNDTRACK_IMAGES_DIR = os.path.join(CACHE_DIR, "soundtrack")
COVER_WORDS = ("cover", "folder", "front", "album", "art")  # так обычно называют обложку в папке альбома
COVER_RATIO = (0.6, 1.4)  # обложка альбома — квадратная или чуть вытянутая; шапки и логотипы не подходят
STEAM_STORE_ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
STEAM_ASSETS_URL = "https://shared.akamai.steamstatic.com/store_item_assets/"
STORE_TYPE_MUSIC = 11  # тип «саундтрек» в ответе магазина
STORE_TYPE_GAME = 0  # тип «игра»; нулевое значение магазин иногда не присылает вовсе
STORE_ITEMS_BATCH = 100  # столько appid в одном запросе — адрес не выходит слишком длинным
STATE_FULLY_INSTALLED = 4  # флаг StateFlags в appmanifest: скачано целиком
_soundtracks_lock = threading.Lock()


def _acf_values(path):
    """appid, name, installdir, StateFlags и LastOwner (SteamID64 аккаунта, который скачал приложение)
    из appmanifest_*.acf — первые встреченные, они в начале файла."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return {}
    values = {}
    for key, value in re.findall(r'^\s*"(appid|name|installdir|stateflags|lastowner)"\s+"([^"]*)"', text,
                                 re.MULTILINE | re.IGNORECASE):
        values.setdefault(key.lower(), value)
    return values


def find_soundtracks(log_result=True):
    """Скачанные саундтреки Steam: [{"appid" (или None), "name", "folder", "tracks": [путь, ...],
    "owner": SteamID64 скачавшего аккаунта или ""}, ...]."""
    albums, seen = [], set()
    for library in steam_library_dirs():
        steamapps = os.path.join(library, "steamapps")
        music = os.path.join(steamapps, "music")
        if not os.path.isdir(music):
            continue
        manifests = {}
        try:
            for file_name in os.listdir(steamapps):
                if file_name.startswith("appmanifest_") and file_name.endswith(".acf"):
                    values = _acf_values(os.path.join(steamapps, file_name))
                    if values.get("installdir"):
                        manifests[os.path.normcase(values["installdir"])] = values
            entries = os.listdir(music)
        except OSError as e:
            log.warning("Не удалось прочитать папку саундтреков %s: %s", music, e)
            continue
        for entry in entries:
            folder = os.path.join(music, entry)
            key = os.path.normcase(os.path.normpath(folder))
            if key in seen or not os.path.isdir(folder):
                continue
            seen.add(key)
            manifest = manifests.get(os.path.normcase(entry), {})
            flags = manifest.get("stateflags", "")
            if flags.isdigit() and not int(flags) & STATE_FULLY_INSTALLED:
                continue  # Steam ещё качает альбом
            tracks = folder_tracks(folder)
            if not tracks:
                continue  # папка пустая
            appid = manifest.get("appid", "")
            albums.append({"appid": int(appid) if appid.isdigit() else None,
                           "name": manifest.get("name") or entry, "folder": folder, "tracks": tracks,
                           "owner": manifest.get("lastowner", "")})
    albums.sort(key=lambda a: natural_key(a["name"]))
    if log_result:
        log.info("Саундтреки Steam: %d%s", len(albums),
                 "".join(f"\n  {a['name']} ({a['appid'] or 'без appid'}): {len(a['tracks'])} треков" for a in albums))
    return albums


def store_items(appids):
    """Сведения магазина о приложениях: {appid: {"type", "name", "parent", "header", "assets"}}.
    Чего нет в магазине — нет и тут. "assets" — адреса картинок {"cover", "hero", "logo"}, какие есть."""
    ids = sorted(set(appids))
    items = {}
    for start in range(0, len(ids), STORE_ITEMS_BATCH):
        query = {"ids": [{"appid": appid} for appid in ids[start:start + STORE_ITEMS_BATCH]],
                 "context": {"language": "russian", "country_code": "US"},
                 "data_request": {"include_assets": True}}
        data = fetch(STEAM_STORE_ITEMS_URL, params={"input_json": json.dumps(query, separators=(",", ":"))}).json()
        for item in (data.get("response") or {}).get("store_items") or []:
            appid = item.get("appid") or item.get("id")
            if not appid or item.get("success") != 1:
                continue
            assets = item.get("assets") or {}
            urls = {}
            if assets.get("asset_url_format"):
                for kind, names in STORE_ASSET_NAMES.items():
                    name = next((assets[n] for n in names if assets.get(n)), None)
                    if name:
                        urls[kind] = STEAM_ASSETS_URL + assets["asset_url_format"].replace("${FILENAME}", name)
            items[appid] = {"type": item.get("type"), "name": item.get("name") or "",
                            "parent": (item.get("related_items") or {}).get("parent_appid"),
                            "header": urls.get("header", ""), "assets": urls}
    return items


def owned_games(appids):
    """Игры среди приложений аккаунта — без дополнений, саундтреков, демо и программ: [{"appid", "name"}].
    Для импорта без ключа: что есть на аккаунте, известно после входа в магазин во встроенном браузере."""
    games = [{"appid": appid, "name": clean_name(item["name"])} for appid, item in store_items(appids).items()
             if item["type"] in (None, STORE_TYPE_GAME) and clean_name(item["name"])]
    return sorted(games, key=lambda g: natural_key(g["name"]))


# Какие картинки магазина нам нужны и как они называются в ответе GetItems (сначала крупные)
STORE_ASSET_NAMES = {
    "header": ("header",),
    "cover": ("library_capsule_2x", "library_capsule"),
    "hero": ("library_hero_2x", "library_hero"),
    "logo": ("library_logo_2x", "library_logo"),
}
_assets_cache = {}  # appid → адреса картинок: за время работы программы они не меняются
_assets_lock = threading.Lock()


def steam_assets(appid, _depth=0):
    """Официальные картинки Steam для игры: {"cover", "hero", "logo"} — какие нашлись.

    У игр последних лет картинки лежат по адресам с хэшем, и старые адреса вида
    steam/apps/<appid>/library_hero.jpg для них не работают. Настоящие адреса отдаёт магазин
    (IStoreBrowseService/GetItems, ключ не нужен). У демоверсии своих картинок часто нет —
    тогда недостающие берём у основной игры (parent_appid).
    """
    with _assets_lock:
        if appid in _assets_cache:
            return _assets_cache[appid]
    item = store_items([appid]).get(appid) or {}
    urls = dict(item.get("assets") or {})
    parent = item.get("parent")
    if parent and parent != appid and _depth == 0 and not all(k in urls for k in ("cover", "hero", "logo")):
        for kind, url in steam_assets(parent, _depth=1).items():
            urls.setdefault(kind, url)
    with _assets_lock:
        _assets_cache[appid] = urls
    return urls


def _steam_asset(appid, kind):
    """Байты картинки kind из магазина Steam по настоящему адресу или None."""
    try:
        url = steam_assets(appid).get(kind)
        return fetch(url).content if url else None
    except (requests.RequestException, ValueError) as e:
        log.info("Магазин Steam не отдал %s для %s: %s", kind, appid, e)
        return None


def _load_soundtrack_cache():
    """steam_soundtracks.json: {"albums": {appid: сведения о саундтреке}, "not_music": [appid, ...],
    "owned": [appid купленных саундтреков], "synced": когда последний раз сверялись со Steam}."""
    with _soundtracks_lock:
        try:
            with open(SOUNDTRACKS_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
    if not isinstance(cache, dict):
        cache = {}
    if "albums" not in cache:  # в первых сборках 2.2 в файле лежали только альбомы
        cache = {"albums": {k: v for k, v in cache.items() if isinstance(v, dict)}}
    cache.setdefault("not_music", [])
    cache.setdefault("owned", [])
    return cache


def _save_soundtrack_cache(cache):
    with _soundtracks_lock:
        try:
            save_json(SOUNDTRACKS_FILE, cache)
        except OSError:
            log.exception("Не удалось сохранить кэш саундтреков")


def _describe_music(appids, cache):
    """Спрашивает магазин о приложениях, про которые ещё ничего не знаем, и запоминает саундтреки среди них.

    Тип приложения не меняется, поэтому каждое приложение спрашиваем один раз. Нет связи — исключение requests.
    """
    albums, not_music, now = cache["albums"], set(cache["not_music"]), time.time()
    wanted = [appid for appid in appids
              if appid not in not_music and (str(appid) not in albums or (
                  albums[str(appid)].get("failed") and now - albums[str(appid)].get("fetched", 0) > SOUNDTRACKS_RETRY))]
    if not wanted:
        return
    items = store_items(wanted)
    parents = {}
    for appid in wanted:
        item = items.get(appid)
        if not item:
            continue  # магазин не знает приложение (сняли с продажи) — спросим при следующей синхронизации
        if item["type"] != STORE_TYPE_MUSIC:
            not_music.add(appid)
            continue
        albums[str(appid)] = {"name": item["name"], "game": item["parent"], "game_name": "",
                              "header": item["header"], "fetched": now}
        if item["parent"]:
            parents.setdefault(item["parent"], []).append(str(appid))
    if parents:
        try:
            names = store_items(parents)
        except (requests.RequestException, ValueError):
            names = {}
        for parent, keys in parents.items():
            for key in keys:
                albums[key]["game_name"] = (names.get(parent) or {}).get("name", "")
    cache["not_music"] = sorted(not_music)


def _store_image(appid, url, download=True):
    """Шапка страницы альбома в магазине — когда другой картинки нет."""
    path = os.path.join(SOUNDTRACK_IMAGES_DIR, f"{appid}.jpg")
    if os.path.exists(path):
        return path
    if not (download and url):
        return None
    try:
        content = fetch(url).content
        os.makedirs(SOUNDTRACK_IMAGES_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
    except (requests.RequestException, OSError) as e:
        log.warning("Не удалось скачать картинку саундтрека %s: %s", appid, e)
        return None
    return path


def _steam_cover(appid):
    """Обложка альбома из кэша клиента Steam — та, что видна у саундтрека в библиотеке Steam."""
    steam_dir = find_steam_dir()
    if not (steam_dir and appid):
        return None
    library_cache = os.path.join(steam_dir, "appcache", "librarycache")
    paths = []
    for root, _, names in os.walk(os.path.join(library_cache, str(appid))):  # новый клиент: папка на приложение
        paths += [os.path.join(root, n) for n in names if n.lower().endswith(IMAGE_EXTENSIONS)]
    for folder in (library_cache, os.path.join(steam_dir, "music", "_database", "covers")):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        paths += [os.path.join(folder, n) for n in names  # старый клиент: <appid>_library_600x900.jpg
                  if n.lower().endswith(IMAGE_EXTENSIONS) and re.split(r"[_.]", n)[0] == str(appid)]
    best, best_score = None, None
    for path in paths:
        try:
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, ValueError):
            continue
        ratio = width / height
        if not COVER_RATIO[0] <= ratio <= COVER_RATIO[1] or min(width, height) < 150:
            continue  # шапка, логотип или значок
        score = (abs(math.log(ratio)), -width * height)  # самая квадратная, из равных — самая крупная
        if best_score is None or score < best_score:
            best, best_score = path, score
    return best


def _syncsafe(data):
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _id3_picture(body, major):
    """(тип картинки, байты) из кадра APIC (PIC в ID3v2.2). Тип 3 — обложка альбома."""
    encoding = body[0]
    if major == 2:
        kind, rest = body[4], body[5:]  # 3 буквы формата вместо MIME
    else:
        end = body.index(b"\x00", 1)
        kind, rest = body[end + 1], body[end + 2:]
    if encoding in (1, 2):  # UTF-16: описание кончается двумя нулями
        i = 0
        while i + 1 < len(rest) and rest[i:i + 2] != b"\x00\x00":
            i += 2
        return kind, rest[i + 2:]
    return kind, rest[rest.index(b"\x00") + 1:]


def _embedded_art(path):
    """Обложка, вшитая в трек: кадр APIC в MP3 (ID3v2) или блок PICTURE во FLAC. None — её нет."""
    best = None
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] == b"ID3" and len(head) == 10:
                major, flags = head[3], head[5]
                data = f.read(_syncsafe(head[6:10]))
                if flags & 0x80 and major < 4:
                    data = data.replace(b"\xff\x00", b"\xff")  # снимаем «рассинхронизацию» ID3
                pos = 0
                if flags & 0x40:  # расширенный заголовок
                    pos = 4 + struct.unpack(">I", data[:4])[0] if major == 3 else _syncsafe(data[:4])
                id_size, header_size = (3, 6) if major == 2 else (4, 10)
                while pos + header_size <= len(data):
                    frame_id = data[pos:pos + id_size]
                    if not frame_id.strip(b"\x00"):
                        break  # дальше — пустое место в теге
                    if major == 2:
                        size = int.from_bytes(data[pos + 3:pos + 6], "big")
                    elif major == 4:
                        size = _syncsafe(data[pos + 4:pos + 8])
                    else:
                        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
                    body = data[pos + header_size:pos + header_size + size]
                    if major == 4 and body:
                        frame_flags = data[pos + 9]
                        if frame_flags & 0x01:
                            body = body[4:]
                        if frame_flags & 0x02:
                            body = body.replace(b"\xff\x00", b"\xff")
                    pos += header_size + size
                    if frame_id in (b"APIC", b"PIC") and body:
                        kind, image = _id3_picture(body, major)
                        if best is None or kind == 3:
                            best = image
                        if kind == 3:
                            break
            elif head[:4] == b"fLaC":
                f.seek(4)
                while True:
                    block = f.read(4)
                    if len(block) < 4:
                        break
                    last, kind, size = block[0] & 0x80, block[0] & 0x7F, int.from_bytes(block[1:4], "big")
                    if kind != 6:
                        f.seek(size, 1)
                    else:
                        data = f.read(size)
                        picture_type = int.from_bytes(data[0:4], "big")
                        p = 8 + int.from_bytes(data[4:8], "big")  # MIME
                        p += 4 + int.from_bytes(data[p:p + 4], "big") + 16  # описание, размеры и цвета
                        image = data[p + 4:p + 4 + int.from_bytes(data[p:p + 4], "big")]
                        if best is None or picture_type == 3:
                            best = image
                        if picture_type == 3:
                            break
                    if last:
                        break
    except (OSError, ValueError, IndexError, struct.error):
        return best
    return best or None


def _embedded_image(album):
    """Обложка из первых треков альбома — сохраняется в cache\\soundtrack, чтобы не читать треки каждый раз."""
    key = album["appid"] or hashlib.md5(os.path.normcase(album["folder"]).encode("utf-8")).hexdigest()[:12]
    path = os.path.join(SOUNDTRACK_IMAGES_DIR, f"{key}_cover.jpg")
    if os.path.exists(path):
        return path
    for track in album["tracks"][:3]:
        data = _embedded_art(track)
        if not data:
            continue
        try:
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
            image.thumbnail((600, 600), Image.Resampling.LANCZOS)
            os.makedirs(SOUNDTRACK_IMAGES_DIR, exist_ok=True)
            image.save(path, quality=92)
            return path
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            log.warning("Не удалось прочитать обложку из %s: %s", track, e)
    return None


def blurred_cover(path, size=160, radius=14):
    """Сильно размытая уменьшенная обложка для подложки плеера: (ширина, высота, байты RGB) или None.

    Картинка маленькая — растянутая на всё окно, она остаётся гладкой, а размывается быстро.
    """
    from PIL import ImageFilter
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            # Края размытия подтягивают чёрный фон — сначала расширяем картинку её же краями
            pad = radius * 2
            image = _pad_edges(image, pad)
            image = image.filter(ImageFilter.GaussianBlur(radius))
            image = image.crop((pad, pad, image.width - pad, image.height - pad))
            return image.width, image.height, image.tobytes("raw", "RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        log.warning("Не удалось размыть обложку %s: %s", path, e)
        return None


def _pad_edges(image, pad):
    """Расширяет картинку на pad точек, повторяя крайние ряды (чтобы края размытия не темнели)."""
    width, height = image.size
    canvas = Image.new("RGB", (width + pad * 2, height + pad * 2))
    canvas.paste(image.resize((width + pad * 2, height + pad * 2)), (0, 0))  # грубо заполняем всё
    canvas.paste(image, (pad, pad))
    return canvas


def _album_image(album, header_url):
    """Картинка альбома: обложка из его папки, из кэша клиента Steam или из самих треков,
    а если её нигде нет — шапка страницы альбома в магазине."""
    images = []
    for root, _, names in os.walk(album["folder"]):
        images += [os.path.join(root, n) for n in names if n.lower().endswith(IMAGE_EXTENSIONS)]
    if images:
        named = [p for p in images if any(word in os.path.basename(p).lower() for word in COVER_WORDS)]

        def size(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0

        return max(named or images, key=size)  # самая крупная — обычно и есть обложка
    return (_steam_cover(album["appid"]) or _embedded_image(album)
            or (_store_image(album["appid"], header_url) if album["appid"] else None))


def soundtrack_details(albums):
    """Дополняет скачанные альбомы полями "game" (appid игры), "game_name" и "image" (путь к картинке).

    Ответы магазина хранятся в steam_soundtracks.json — к какой игре относится альбом, со временем не меняется.
    Вызывается в фоновом потоке.
    """
    cache = _load_soundtrack_cache()
    ids = [album["appid"] for album in albums if album["appid"]]
    before = json.dumps(cache, sort_keys=True)
    try:
        _describe_music(ids, cache)
    except (requests.RequestException, ValueError) as e:
        log.warning("Магазин Steam не ответил про саундтреки: %s", e)
        for appid in ids:
            info = cache["albums"].setdefault(str(appid), {"failed": True})
            if info.get("failed"):
                info["fetched"] = time.time()
    for album in albums:
        info = (cache["albums"].get(str(album["appid"])) or {}) if album["appid"] else {}
        album["game"] = info.get("game")
        album["game_name"] = info.get("game_name") or ""
        album["image"] = _album_image(album, info.get("header"))
    if json.dumps(cache, sort_keys=True) != before:
        _save_soundtrack_cache(cache)
    return albums


def _owned_items(cache, download):
    items = []
    for appid in cache["owned"]:
        info = cache["albums"].get(str(appid)) or {}
        if info.get("failed"):
            continue
        items.append({"appid": appid, "name": info.get("name") or str(appid), "game": info.get("game"),
                      "game_name": info.get("game_name") or "",
                      "image": _steam_cover(appid) or _store_image(appid, info.get("header"), download)})
    items.sort(key=lambda item: natural_key(item["name"]))
    return items


def owned_soundtracks(owned_appids):
    """Какие из купленных в Steam приложений — саундтреки: [{"appid", "name", "game", "game_name", "image"}, ...].

    owned_appids — всё купленное (игры, DLC, саундтреки). Тип каждого приложения запоминается, так что
    следующая синхронизация спрашивает магазин только о новых покупках. Вызывается в фоновом потоке.
    """
    cache = _load_soundtrack_cache()
    owned = sorted({appid for appid in owned_appids if isinstance(appid, int) and 0 < appid < SHORTCUT_APPID_MIN})
    _describe_music(owned, cache)
    cache["owned"] = [appid for appid in owned
                      if str(appid) in cache["albums"] and not cache["albums"][str(appid)].get("failed")]
    cache["owned_apps"] = owned  # всё, что есть на аккаунте: по нему поиск игр отмечает уже купленные
    cache["synced"] = time.time()
    _save_soundtrack_cache(cache)
    log.info("Куплено в Steam: %d, из них саундтреков: %d", len(owned), len(cache["owned"]))
    return _owned_items(cache, download=True)


def cached_owned_soundtracks():
    """Купленные саундтреки с прошлой синхронизации — без запросов в сеть."""
    return _owned_items(_load_soundtrack_cache(), download=False)


def soundtracks_synced():
    return bool(_load_soundtrack_cache().get("synced"))


def forget_owned_soundtracks():
    """Выход из магазина: список покупок был того, кто вошёл, — забываем его (сведения об альбомах остаются)."""
    cache = _load_soundtrack_cache()
    cache["owned"] = []
    cache["owned_apps"] = []
    cache.pop("synced", None)
    _save_soundtrack_cache(cache)


def owned_apps():
    """appid всего, что есть на аккаунте Steam, — с прошлой синхронизации покупок (вход в магазин
    во встроенном браузере). Пусто — синхронизации ещё не было."""
    return set(_load_soundtrack_cache().get("owned_apps") or [])


# --- Удаление скачанного саундтрека -------------------------------------------
# Скачанный саундтрек — обычное приложение Steam: папка в steamapps\music и appmanifest_<appid>.acf рядом
# с играми. Удалить его можно через сам Steam (steam://uninstall — Steam спросит и уберёт всё сам) или без Steam:
# программа кладёт папку и appmanifest в Корзину Windows, откуда их можно вернуть. Без Steam — только когда он
# закрыт: запущенный Steam держит сведения об альбоме в памяти и может начать качать его заново.
def soundtrack_paths(album):
    """Что удалить вместе с альбомом: [папка альбома, appmanifest (если есть)]. Только папки прямо внутри
    <библиотека>\\steamapps\\music — всё остальное программа не трогает (OSError)."""
    folder = os.path.normpath(album["folder"])
    music = os.path.dirname(folder)
    steamapps = os.path.dirname(music)
    if (os.path.basename(music).lower() != "music" or os.path.basename(steamapps).lower() != "steamapps"
            or not os.path.isdir(folder)):
        raise OSError(f"это не папка саундтрека Steam: {folder}")
    paths = [folder]
    manifest = os.path.join(steamapps, f"appmanifest_{album['appid']}.acf") if album.get("appid") else ""
    if manifest and os.path.isfile(manifest):
        paths.append(manifest)
    return paths


def folder_size(folder):
    total = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _to_recycle_bin(paths):
    """Файлы и папки — в Корзину Windows (SHFileOperation с «можно отменить»), без окон Windows."""
    import ctypes
    from ctypes import wintypes

    class FileOp(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_void_p)]

    fo_delete, allow_undo, no_confirmation, silent, no_error_ui = 3, 0x40, 0x10, 0x4, 0x400
    names = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")  # список путей кончается двумя нулями
    op = FileOp(None, fo_delete, ctypes.cast(names, ctypes.c_void_p), None,
                allow_undo | no_confirmation | silent | no_error_ui, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result or op.fAnyOperationsAborted:
        raise OSError(f"Windows не переместила файлы в Корзину (код {result}). Возможно, трек сейчас открыт")


def remove_soundtrack(album):
    """Удалить скачанный альбом без Steam — в Корзину. Возвращает, сколько файлов и папок убрано."""
    if sys.platform != "win32":
        raise OSError("удаление в Корзину работает только в Windows")
    paths = soundtrack_paths(album)
    _to_recycle_bin(paths)
    log.info("Саундтрек «%s» (%s) удалён в Корзину: %s", album["name"], album.get("appid") or "без appid",
             ", ".join(paths))
    return len(paths)


# --- Поиск игры в магазине Steam ----------------------------------------------
# Открытый поиск магазина: ключ и вход не нужны. Найденный номер игры (appid) — это всё, что нужно
# для описания, скриншотов, трейлеров, обложек и установки через Steam.
STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STORE_SEARCH_LIMIT = 10


def search_store(term):
    """Игры магазина Steam по названию: [{"appid", "name", "image"}, ...]. Вместо названия можно вставить
    номер игры или ссылку на её страницу в магазине."""
    term = term.strip()
    if not term:
        return []
    match = re.search(r"store\.steampowered\.com/app/(\d+)", term) or re.fullmatch(r"(\d{1,8})", term)
    if match:
        appid = int(match.group(1))
        item = store_items([appid]).get(appid)
        found = [{"appid": appid, "name": clean_name(item["name"]), "image": item.get("header") or ""}] \
            if item and item.get("name") else []
    else:
        data = fetch(STEAM_STORE_SEARCH_URL, params={"term": term, "l": "russian", "cc": "US"}).json()
        found = [{"appid": int(item["id"]), "name": clean_name(item["name"]), "image": item.get("tiny_image") or ""}
                 for item in (data or {}).get("items") or []
                 if item.get("type") == "app" and item.get("id") and item.get("name")][:STORE_SEARCH_LIMIT]
    log.info("Поиск в магазине Steam «%s»: найдено %d", term, len(found))
    return found


# --- Фон рулетки: обложки на «фотоплёнке» -----------------------------------
# Размеры — в пикселях уменьшенной копии фона: на экране она растягивается втрое
FILM_FRAME = (52, 78)  # кадр с обложкой
FILM_BORDER = 11  # поле с перфорацией по бокам ленты
FILM_GAP = 6  # перемычка между кадрами
FILM_HOLES = 4  # отверстий перфорации на кадр с каждой стороны
FILM_BLUR = 2.5
FILM_COLOR = (16, 16, 19)
FILM_HOLE_COLOR = (92, 92, 100)
FILM_FRAMES = 12  # обложек на одной ленте


def film_strips(count):
    """Ленты «фотоплёнки» для фона рулетки из уже скачанных обложек.

    Каждая лента — вертикальная полоса тёмной плёнки с перфорацией, в кадрах — обложки в случайном порядке.
    Лента уже размыта и зациклена по высоте: низ без шва переходит в верх. [] — обложек ещё нет.
    """
    try:
        paths = [os.path.join(COVERS_DIR, f) for f in os.listdir(COVERS_DIR) if f.lower().endswith(IMAGE_EXTENSIONS)]
    except FileNotFoundError:
        return []
    random.shuffle(paths)
    paths = paths[:count * FILM_FRAMES]  # больше одновременно не видно — остальные не читаем
    fw, fh = FILM_FRAME
    thumbs = []
    for path in paths:
        try:
            with Image.open(path) as image:
                thumbs.append(ImageOps.fit(image.convert("RGB"), (fw, fh), Image.Resampling.LANCZOS))
        except (OSError, ValueError) as e:
            log.warning("Обложка %s не открылась для фона: %s", os.path.basename(path), e)
    if not thumbs:
        return []

    width, step = fw + 2 * FILM_BORDER, fh + FILM_GAP
    hole_w, hole_h = round(FILM_BORDER * 0.4), 4
    hole_x = (round(FILM_BORDER * 0.3), width - round(FILM_BORDER * 0.3) - hole_w)
    strips = []
    for s in range(count):
        # Обложек мало — ленты повторяют их, но каждая в своём порядке
        frames = thumbs[s * FILM_FRAMES:(s + 1) * FILM_FRAMES] or random.sample(thumbs, min(FILM_FRAMES, len(thumbs)))
        height = step * len(frames)
        strip = Image.new("RGB", (width, height), FILM_COLOR)
        draw = ImageDraw.Draw(strip)
        for i, thumb in enumerate(frames):
            top = i * step
            strip.paste(thumb, (FILM_BORDER, top + FILM_GAP // 2))
            for j in range(FILM_HOLES):
                y = round(top + (j + 0.5) * step / FILM_HOLES - hole_h / 2)
                for x in hole_x:
                    draw.rounded_rectangle([x, y, x + hole_w, y + hole_h], radius=1, fill=FILM_HOLE_COLOR)
        # Размываем три копии подряд и берём среднюю — так на стыке витков ленты нет шва
        tall = Image.new("RGB", (width, height * 3))
        for k in range(3):
            tall.paste(strip, (0, k * height))
        tall = tall.filter(ImageFilter.GaussianBlur(FILM_BLUR))
        strips.append(tall.crop((0, height, width, height * 2)))
    return strips


# --- Трейлеры из потоков Steam ------------------------------------------------
# Новые трейлеры Steam отдаёт только потоком HLS: видео и звук отдельно, кусочками по 3 секунды
# во фрагментах mp4 (.m4s). Сборка FFmpeg внутри Qt такой поток не открывает («Unsupported media type»),
# поэтому скачиваем кусочки сами и склеиваем в обычный mp4 — его открывает любой проигрыватель.
TRAILERS_DIR = os.path.join(CACHE_DIR, "trailers")
TRAILER_MAX_HEIGHT = 1080
TRAILER_CACHE_FILES = 6  # столько последних трейлеров храним, более старые удаляются
TRAILER_PREVIEW_FILES = 2  # и столько «быстрых стартов» в низком качестве — они нужны, только пока качается 1080p
TRAILER_PREVIEW_MIN_HEIGHT = 360  # быстрый старт — самый лёгкий вариант потока, но не мельче этого
TRAILER_THREADS = 6  # кусочков качаем одновременно


class TrailerCancelled(Exception):
    """Трейлер закрыли, пока он скачивался."""


_previews = 0  # сколько «быстрых стартов» качается прямо сейчас
_previews_lock = threading.Lock()


def _previews_running():
    with _previews_lock:
        return _previews > 0


def _playlist_lines(url):
    text = fetch(url).text
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("по адресу не плейлист HLS")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _hls_attributes(line):
    """Атрибуты тега HLS: '#EXT-X-MEDIA:TYPE=AUDIO,URI="a.m3u8"' → {"TYPE": "AUDIO", "URI": "a.m3u8"}."""
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1])}


def _hls_variant(master_url, max_height, lowest=False):
    """Высота кадра, плейлист видео и плейлист звука (или None) — лучший вариант не выше max_height,
    а с lowest — самый лёгкий не мельче TRAILER_PREVIEW_MIN_HEIGHT (для быстрого старта)."""
    lines = _playlist_lines(master_url)
    audio_groups, variants = {}, []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _hls_attributes(line)
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                audio_groups.setdefault(attrs.get("GROUP-ID"), urljoin(master_url, attrs["URI"]))
        elif line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
            attrs = _hls_attributes(line)
            height = int(attrs.get("RESOLUTION", "0x0").lower().split("x")[-1] or 0)
            variants.append((height, int(attrs.get("BANDWIDTH") or 0), urljoin(master_url, lines[i + 1]),
                             attrs.get("AUDIO")))
    if not variants:
        raise ValueError("в плейлисте нет вариантов видео")
    if lowest:
        height, _, video_url, group = min([v for v in variants if v[0] >= TRAILER_PREVIEW_MIN_HEIGHT] or variants)
        return height, video_url, audio_groups.get(group)
    fitting = [v for v in variants if v[0] <= max_height] or [min(variants)]
    height, _, video_url, group = max(fitting)
    return height, video_url, audio_groups.get(group)


def _hls_segments(url):
    """Адрес начального фрагмента (#EXT-X-MAP) и кусочков потока по порядку."""
    init, segments = None, []
    for line in _playlist_lines(url):
        if line.startswith("#EXT-X-BYTERANGE") or (line.startswith("#EXT-X-MAP") and "BYTERANGE" in line):
            raise ValueError("кусочки заданы диапазонами байтов — такой поток не поддерживается")
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise ValueError("поток зашифрован")
        if line.startswith("#EXT-X-MAP:"):
            init = urljoin(url, _hls_attributes(line)["URI"])
        elif not line.startswith("#"):
            segments.append(urljoin(url, line))
    if not init or not segments:
        raise ValueError("в плейлисте нет фрагментов mp4")
    return init, segments


# Разбор mp4. Файл — это «боксы»: 4 байта размера, 4 буквы типа, внутри данные или другие боксы.
def _boxes(data, start=0, end=None):
    """Боксы подряд в data[start:end]: (тип, начало, длина заголовка, полный размер)."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size, header = struct.unpack_from(">Q", data, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError("повреждённый фрагмент mp4")
        yield kind.decode("latin-1"), pos, header, size
        pos += size


def _find(data, start, end, *path):
    """Бокс по пути, например _find(data, 0, n, "moov", "trak"): (начало, заголовок, размер) или None."""
    found = None
    for kind in path:
        for k, pos, header, size in _boxes(data, start, end):
            if k == kind:
                start, end, found = pos + header, pos + size, (pos, header, size)
                break
        else:
            return None
    return found


def _box(kind, *parts):
    payload = b"".join(parts)
    return struct.pack(">I4s", 8 + len(payload), kind.encode("latin-1")) + payload


def _full_box(kind, version, *parts):
    return _box(kind, struct.pack(">I", version << 24), *parts)


def _box_bytes(data, found):
    pos, _, size = found
    return bytes(data[pos:pos + size])


def _timescale(data, found):
    """Единиц времени в секунде из mvhd или mdhd."""
    pos, header, _ = found
    return struct.unpack_from(">I", data, pos + header + (20 if data[pos + header] == 1 else 12))[0]


def _parse_init(data):
    """Всё нужное из начального фрагмента дорожки: описание кадров, шкала времени, значения по умолчанию."""
    end = len(data)
    trak = _find(data, 0, end, "moov", "trak")
    mvhd = _find(data, 0, end, "moov", "mvhd")
    mdhd = trak and _find(data, trak[0] + trak[1], trak[0] + trak[2], "mdia", "mdhd")
    stsd = trak and _find(data, trak[0] + trak[1], trak[0] + trak[2], "mdia", "minf", "stbl", "stsd")
    if not (trak and mvhd and mdhd and stsd):
        raise ValueError("в начальном фрагменте нет описания дорожки")
    trex = _find(data, 0, end, "moov", "mvex", "trex")
    # trex: версия и флаги, номер дорожки, описание, длительность, размер, флаги сэмпла
    defaults = struct.unpack_from(">III", data, trex[0] + trex[1] + 12) if trex else (0, 0, 0)
    return {"data": data, "trak": trak, "mvhd": mvhd, "stsd": _box_bytes(data, stsd),
            "timescale": _timescale(data, mdhd), "movie_timescale": _timescale(data, mvhd), "defaults": defaults}


def _fragment_samples(data, track):
    """Кадры (сэмплы) из кусочка потока: [(смещение, размер, длительность, ключевой, сдвиг показа)]."""
    default_duration, default_size, default_flags = track["defaults"]
    samples = []
    for kind, pos, header, size in _boxes(data):
        if kind != "moof":
            continue
        for tkind, tpos, theader, tsize in _boxes(data, pos + header, pos + size):
            if tkind != "traf":
                continue
            duration, sample_size, flags_default = default_duration, default_size, default_flags
            base, next_offset = pos, None  # смещения данных считаются от начала moof
            for bkind, bpos, bheader, _ in _boxes(data, tpos + theader, tpos + tsize):
                p = bpos + bheader
                if bkind == "tfhd":
                    flags = struct.unpack_from(">I", data, p)[0] & 0xFFFFFF
                    q = p + 8  # версия с флагами и номер дорожки
                    if flags & 0x01:
                        base = struct.unpack_from(">Q", data, q)[0]
                        q += 8
                    if flags & 0x02:
                        q += 4
                    if flags & 0x08:
                        duration = struct.unpack_from(">I", data, q)[0]
                        q += 4
                    if flags & 0x10:
                        sample_size = struct.unpack_from(">I", data, q)[0]
                        q += 4
                    if flags & 0x20:
                        flags_default = struct.unpack_from(">I", data, q)[0]
                elif bkind == "trun":
                    version_flags, count = struct.unpack_from(">II", data, p)
                    version, flags = version_flags >> 24, version_flags & 0xFFFFFF
                    q = p + 8
                    offset = base if next_offset is None else next_offset
                    if flags & 0x01:
                        offset = base + struct.unpack_from(">i", data, q)[0]
                        q += 4
                    first_flags = None
                    if flags & 0x04:
                        first_flags = struct.unpack_from(">I", data, q)[0]
                        q += 4
                    for i in range(count):
                        s_duration, s_size, s_flags, shift = duration, sample_size, flags_default, 0
                        if flags & 0x100:
                            s_duration = struct.unpack_from(">I", data, q)[0]
                            q += 4
                        if flags & 0x200:
                            s_size = struct.unpack_from(">I", data, q)[0]
                            q += 4
                        if flags & 0x400:
                            s_flags = struct.unpack_from(">I", data, q)[0]
                            q += 4
                        if i == 0 and first_flags is not None:
                            s_flags = first_flags
                        if flags & 0x800:
                            shift = struct.unpack_from(">i" if version else ">I", data, q)[0]
                            q += 4
                        if offset + s_size > len(data):
                            raise ValueError("кусочек потока обрезан")
                        # Бит 0x10000 — «не ключевой кадр»
                        samples.append((offset, s_size, s_duration, not s_flags & 0x10000, shift))
                        offset += s_size
                    next_offset = offset
    return samples


def _runs(values):
    """[5, 5, 5, 7] → [[3, 5], [1, 7]]: подряд идущие одинаковые значения."""
    runs = []
    for value in values:
        if runs and runs[-1][1] == value:
            runs[-1][0] += 1
        else:
            runs.append([1, value])
    return runs


def _sample_table(track, samples, chunk_sizes, chunk_offsets):
    """Таблица кадров обычного mp4 (stbl): длительности, размеры, ключевые кадры, где лежат куски."""
    durations = _runs([s[2] for s in samples])
    parts = [track["stsd"],
             _full_box("stts", 0, struct.pack(">I", len(durations)),
                       b"".join(struct.pack(">II", n, v) for n, v in durations))]
    shifts = [s[4] for s in samples]
    if any(shifts):
        runs = _runs(shifts)
        parts.append(_full_box("ctts", 1 if min(shifts) < 0 else 0, struct.pack(">I", len(runs)),
                               b"".join(struct.pack(">Ii", n, v) for n, v in runs)))
    if not all(s[3] for s in samples):
        keys = [i + 1 for i, s in enumerate(samples) if s[3]]
        parts.append(_full_box("stss", 0, struct.pack(">I", len(keys)), b"".join(struct.pack(">I", k) for k in keys)))
    per_chunk, first = [], 1
    for count, value in _runs(chunk_sizes):
        per_chunk.append((first, value))
        first += count
    parts.append(_full_box("stsc", 0, struct.pack(">I", len(per_chunk)),
                           b"".join(struct.pack(">III", c, n, 1) for c, n in per_chunk)))
    parts.append(_full_box("stsz", 0, struct.pack(">II", 0, len(samples)),
                           b"".join(struct.pack(">I", s[1]) for s in samples)))
    parts.append(_full_box("co64", 0, struct.pack(">I", len(chunk_offsets)),
                           b"".join(struct.pack(">Q", o) for o in chunk_offsets)))
    return _box("stbl", *parts)


def _rebuild(data, found, replace):
    """Копия бокса-контейнера, где дочерние боксы заменены: replace[тип](байты) → байты или None (убрать)."""
    pos, header, size = found
    parts = []
    for kind, p, h, s in _boxes(data, pos + header, pos + size):
        box = bytes(data[p:p + s])
        if kind in replace:
            box = replace[kind](box, (p, h, s))
        if box is not None:
            parts.append(box)
    return _box(bytes(data[pos + 4:pos + 8]).decode("latin-1"), *parts)


def _patched(box, header, fields):
    """Копия бокса с новыми числами: fields — [(смещение от начала данных, формат, значение)]."""
    box = bytearray(box)
    for offset, fmt, value in fields:
        struct.pack_into(fmt, box, header + offset, value)
    return bytes(box)


def _trak(track, track_id, media_duration, track_duration, stbl):
    data = track["data"]

    def tkhd(box, found):
        h = found[1]
        if box[h] == 1:
            return _patched(box, h, [(20, ">I", track_id), (28, ">Q", track_duration)])
        return _patched(box, h, [(12, ">I", track_id), (20, ">I", track_duration)])

    def mdhd(box, found):
        h = found[1]
        return _patched(box, h, [(24, ">Q", media_duration)] if box[h] == 1 else [(16, ">I", media_duration)])

    def mdia(box, found):
        return _rebuild(data, found, {"mdhd": mdhd, "minf": lambda b, f: _rebuild(data, f, {"stbl": lambda *_: stbl})})

    # Списки монтажа (edts) у фрагментов рассчитаны на поток — в обычном файле они не нужны
    return _rebuild(data, track["trak"], {"tkhd": tkhd, "mdia": mdia, "edts": lambda *_: None})


def _remux(tracks, path):
    """Склеивает дорожки фрагментированного mp4 (видео и звук отдельно) в обычный mp4.

    tracks — [(начальный фрагмент, [кусочки потока])]. Кусочки с одним номером у всех дорожек кладутся
    рядом — картинка и звук идут в файле вперемешку, как в обычном видео.
    """
    parsed = []
    for init, fragments in tracks:
        track = _parse_init(init)
        track["chunks"] = [(fragment, _fragment_samples(fragment, track)) for fragment in fragments]
        track["chunks"] = [(fragment, samples) for fragment, samples in track["chunks"] if samples]
        if not track["chunks"]:
            raise ValueError("в потоке нет кадров")
        parsed.append(track)
    movie_timescale = parsed[0]["movie_timescale"]
    order = [(t, i) for i in range(max(len(track["chunks"]) for track in parsed))
             for t, track in enumerate(parsed) if i < len(track["chunks"])]

    def moov(offsets):
        traks, longest = [], 0
        for t, track in enumerate(parsed):
            samples = [s for _, chunk in track["chunks"] for s in chunk]
            media_duration = sum(s[2] for s in samples)
            track_duration = media_duration * movie_timescale // track["timescale"]
            longest = max(longest, track_duration)
            stbl = _sample_table(track, samples, [len(chunk) for _, chunk in track["chunks"]],
                                 [offsets.get((t, i), 0) for i in range(len(track["chunks"]))])
            traks.append(_trak(track, t + 1, media_duration, track_duration, stbl))
        first = parsed[0]
        mvhd = _box_bytes(first["data"], first["mvhd"])
        h = first["mvhd"][1]
        mvhd = _patched(mvhd, h, [(24, ">Q", longest) if mvhd[h] == 1 else (16, ">I", longest)])
        mvhd = _patched(mvhd, 0, [(len(mvhd) - 4, ">I", len(parsed) + 1)])  # номер для следующей дорожки
        return _box("moov", mvhd, *traks)

    ftyp = _box("ftyp", b"isom", struct.pack(">I", 512), b"isomiso2avc1mp41")
    # Адреса кусков записаны 8-байтными числами, поэтому длина moov от них не зависит:
    # считаем её с нулями, потом подставляем настоящие адреса
    position = len(ftyp) + len(moov({})) + 16  # 16 — заголовок mdat с 64-битным размером
    offsets = {}
    for key in order:
        t, i = key
        offsets[key] = position
        position += sum(s[1] for s in parsed[t]["chunks"][i][1])
    header = len(ftyp) + len(moov({}))
    with open(path, "wb") as f:
        f.write(ftyp)
        f.write(moov(offsets))
        f.write(struct.pack(">I4sQ", 1, b"mdat", position - header))
        for t, i in order:
            fragment, samples = parsed[t]["chunks"][i]
            view = memoryview(fragment)
            f.write(b"".join(view[s[0]:s[0] + s[1]] for s in samples))


def _prune_trailers():
    try:
        files = [os.path.join(TRAILERS_DIR, f) for f in os.listdir(TRAILERS_DIR) if f.endswith(".mp4")]
    except FileNotFoundError:
        return
    for preview, limit in ((False, TRAILER_CACHE_FILES), (True, TRAILER_PREVIEW_FILES)):
        group = sorted((f for f in files if f.endswith("_low.mp4") == preview), key=os.path.getmtime, reverse=True)
        for path in group[limit:]:
            try:
                os.remove(path)
            except OSError:
                pass  # трейлер сейчас открыт — удалим в следующий раз


def _trailer_path(master_url, preview=False):
    key = hashlib.md5(master_url.split("?")[0].encode("utf-8")).hexdigest()[:16]
    return os.path.join(TRAILERS_DIR, key + ("_low.mp4" if preview else ".mp4"))


def cached_trailer(master_url, preview=False):
    """Путь к уже собранному трейлеру этого потока (preview — быстрый старт в низком качестве) или None."""
    path = _trailer_path(master_url, preview)
    if os.path.exists(path):
        os.utime(path)  # смотрели недавно — удалится последним
        return path
    return None


def trailer_file(master_url, progress=None, cancelled=None, preview=False):
    """Трейлер из потока HLS одним файлом mp4 в лучшем качестве до 1080p. Возвращает путь к файлу.

    preview — самый лёгкий вариант потока: качается за пару секунд, им трейлер начинается, пока
    готовится 1080p. progress(высота кадра, проценты) — по ходу загрузки; cancelled() → True — бросить.
    Готовые файлы хранятся в cache/trailers, повторный просмотр открывается сразу.
    """
    os.makedirs(TRAILERS_DIR, exist_ok=True)
    cached = cached_trailer(master_url, preview)
    if cached:
        return cached
    path = _trailer_path(master_url, preview)

    height, video_url, audio_url = _hls_variant(master_url, TRAILER_MAX_HEIGHT, lowest=preview)
    playlists = [_hls_segments(video_url)] + ([_hls_segments(audio_url)] if audio_url else [])
    urls = [url for init, segments in playlists for url in (init, *segments)]

    def download(url):
        # Пока качается быстрый старт, 1080p ждёт: вместе они делят скорость интернета, и быстрый старт
        # переставал быть быстрым — оба трейлера приходили одновременно
        while not preview and _previews_running() and not (cancelled and cancelled()):
            time.sleep(0.05)
        if cancelled and cancelled():
            raise TrailerCancelled()
        return fetch(url).content

    global _previews
    if preview:
        with _previews_lock:
            _previews += 1
    parts = {}
    pool = ThreadPoolExecutor(TRAILER_THREADS)
    try:
        futures = {pool.submit(download, url): url for url in urls}
        for future in as_completed(futures):
            parts[futures[future]] = future.result()
            if cancelled and cancelled():
                raise TrailerCancelled()
            if progress:
                progress(height, len(parts) * 100 // len(urls))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if preview:
            with _previews_lock:
                _previews -= 1

    temp = path + ".tmp"
    try:
        _remux([(parts[init], [parts[s] for s in segments]) for init, segments in playlists], temp)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    log.info("Трейлер собран из потока%s: %sp, %d кусочков, %.1f МБ", " (быстрый старт)" if preview else "",
             height, len(urls), os.path.getsize(path) / 1024 / 1024)
    _prune_trailers()
    return path


# --- Звуки барабана ---------------------------------------------------------
# Звуки синтезируются в коде — никаких чужих файлов и лицензий.
SAMPLE_RATE = 44100


def _write_wav(path, samples):
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples))


def _tick_samples():
    """Короткий деревянный щелчок, как у колеса фортуны."""
    rng = random.Random(7)
    samples = []
    for i in range(int(SAMPLE_RATE * 0.03)):
        t = i / SAMPLE_RATE
        tone = math.sin(2 * math.pi * 1900 * t) * math.exp(-t * 260)
        noise = (rng.random() * 2 - 1) * math.exp(-t * 600)
        samples.append(0.55 * tone + 0.25 * noise)
    return samples


def _win_samples():
    """Мягкий «дзынь» из трёх нот — когда барабан остановился."""
    samples = []
    notes = [(784.0, 0.0), (988.0, 0.07), (1319.0, 0.14)]  # соль, си, ми
    duration = 1.1
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        value = 0.0
        for freq, start in notes:
            if t >= start:
                local = t - start
                attack = min(1.0, local / 0.01)
                value += attack * math.exp(-local * 4.5) * (
                    math.sin(2 * math.pi * freq * local) + 0.3 * math.sin(2 * math.pi * freq * 2 * local))
        samples.append(0.28 * value)
    return samples


def generate_sounds():
    """Создаёт WAV-файлы звуков во временной папке. Возвращает {"tick": путь, "win": путь}."""
    folder = os.path.join(tempfile.gettempdir(), "MySteamRandom")
    os.makedirs(folder, exist_ok=True)
    paths = {"tick": os.path.join(folder, "tick.wav"), "win": os.path.join(folder, "win.wav")}
    for name, make in (("tick", _tick_samples), ("win", _win_samples)):
        if not os.path.exists(paths[name]):
            _write_wav(paths[name], make())
    return paths
