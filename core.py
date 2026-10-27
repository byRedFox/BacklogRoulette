"""Логика без интерфейса: список игр, обложки, Steam, настройки."""

import functools
import hashlib
import html
import io
import json
import logging
import math
import os
import random
import re
import struct
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, urljoin

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
    "steam_id": "",
    "music_enabled": True,
    "music_volume": 40,  # 0–100
    "music_path": "",  # свой трек или папка с музыкой; пусто — встроенный apptheme.mp3
    "sounds_enabled": True,  # щелчки барабана и звук остановки
    "theme": "auto",  # auto | light | dark
    # Последний выбор в окне импорта — по нему работает быстрая синхронизация
    "import_done": False,
    "import_include": [],  # id коллекций Steam
    "import_skip_hidden": True,
    "import_unplayed": False,
    "import_replace": False,
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


# --- Логи -------------------------------------------------------------------
log = logging.getLogger("MySteamRandom")

# Ключи API попадают в адреса запросов (…?key=…) и тексты ошибок — в логах их прячем
_SECRET_RE = re.compile(r"(key=|Bearer\s+)[^&\s'\"]+", re.IGNORECASE)


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


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return settings


def save_settings(**changes):
    settings = load_settings()
    settings.update(changes)
    save_json(SETTINGS_FILE, settings)


# Очистка названия от символов, недопустимых в имени файла (и значков ™ ®)
def clean_name(text):
    text = re.sub(r'[<>:"/\\|?*™®©]', "", str(text))
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
        return None

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
    return removed


def load_not_found():
    try:
        with open(NOT_FOUND_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return set()


def save_not_found(names):
    save_json(NOT_FOUND_FILE, sorted(names))


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


def download_from_steamgriddb(name, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    search_url = f"{STEAMGRIDDB_API_URL}/search/autocomplete/{quote(name, safe='')}"
    found = fetch(search_url, headers=headers).json().get("data", [])
    if not found:
        return None

    grids_url = f"{STEAMGRIDDB_API_URL}/grids/game/{found[0]['id']}"
    # Сначала просим вертикальные обложки 600x900, затем любые
    grids = fetch(grids_url, headers=headers, params={"dimensions": "600x900"}).json().get("data", [])
    if not grids:
        grids = fetch(grids_url, headers=headers).json().get("data", [])
    if not grids:
        return None
    return fetch(grids[0]["url"]).content


def download_cover(game, steamgriddb_key):
    """Возвращает (путь к обложке или None, список ошибок)."""
    errors = []

    # 1. Официальная обложка Steam — не требует ключа, нужен только appid
    if game.get("appid"):
        try:
            content = fetch(STEAM_COVER_URL.format(appid=game["appid"])).content
            return save_cover_bytes(game["name"], content), errors
        except (requests.RequestException, OSError) as e:
            errors.append(f"Steam: {e}")

    # 2. SteamGridDB — поиск по названию
    if not steamgriddb_key:
        errors.append("SteamGridDB: не задан API-ключ")
        return None, errors
    try:
        content = download_from_steamgriddb(game["name"], steamgriddb_key)
        if content:
            return save_cover_bytes(game["name"], content), errors
        errors.append("SteamGridDB: игра не найдена")
    except (requests.RequestException, OSError, ValueError, KeyError) as e:
        errors.append(f"SteamGridDB: {e}")
    return None, errors


# --- Steam ------------------------------------------------------------------
def fetch_owned_games(api_key, steam_id):
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    }
    data = fetch(STEAM_OWNED_GAMES_URL, params=params).json()
    return data.get("response", {}).get("games", [])


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


# --- Библиотека целиком: свои игры + семейные из коллекций ---------------------
# GetOwnedGames отдаёт только купленные вами игры. Игры семейной библиотеки Steam
# через обычный ключ API не получить, но их appid лежат в ваших коллекциях —
# названия для них берём из общего списка игр магазина Steam.
SHORTCUT_APPID_MIN = 2 ** 31  # у сторонних игр, добавленных в Steam вручную, appid начинаются отсюда
APP_DETAILS_LIMIT = 40  # сколько игр, которых нет в списке магазина, проверяем по одной
MAX_STORE_APPID = 4_500_000  # примерный самый большой appid в магазине — для полосы прогресса


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


def load_steam_library(api_key, steam_id, progress=None):
    """Собирает всё для окна импорта: игры, коллекции и ошибку чтения коллекций (если была).

    Игры: [{"appid", "name", "playtime" (минуты или None — неизвестно), "family": bool}, ...]
    progress(текст, проценты) — необязательная функция для окна прогресса; вызывается из фонового потока.
    """
    _report(progress, "Получаю ваши игры из Steam…", 5)
    games = {}
    for game in fetch_owned_games(api_key, steam_id):
        if game.get("appid") and game.get("name"):
            games[game["appid"]] = {"appid": game["appid"], "name": game["name"],
                                    "playtime": game.get("playtime_forever", 0), "family": False}

    _report(progress, f"Ваших игр: {len(games)}. Читаю коллекции Steam…", 15)
    try:
        collections = read_steam_collections(steam_id)
        collections_error = None
    except (OSError, ValueError) as e:
        collections, collections_error = None, str(e)

    extra = set()
    for collection in collections or []:
        extra |= {a for a in collection["apps"] if isinstance(a, int) and 0 < a < SHORTCUT_APPID_MIN}
    extra -= games.keys()

    if extra:
        _report(progress, f"Ищу названия семейных игр: {len(extra)}…", 20)
        names = resolve_app_names(api_key, extra, progress)
        for appid, name in names.items():
            # Время игры в семейных играх через API не узнать
            games[appid] = {"appid": appid, "name": name, "playtime": None, "family": True}
    _report(progress, "Готово!", 100)

    log.info("Библиотека Steam: своих игр %d, семейных найдено %d из %d, коллекций %s",
             sum(not g["family"] for g in games.values()), sum(g["family"] for g in games.values()),
             len(extra), "не найдено" if collections is None else len(collections))
    if collections_error:
        log.warning("Не удалось прочитать коллекции Steam: %s", collections_error)
    return {"games": list(games.values()), "collections": collections, "collections_error": collections_error}


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

    selected = [
        game for game in library["games"]
        if (not include_ids or game["appid"] in include)
        and game["appid"] not in skip
        and not (choice["import_unplayed"] and (game["playtime"] or 0) > 0)
    ]
    return selected, missing


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
    """Перевод описания через бесплатный адрес Google Translate. None — не получилось.

    Официального бесплатного API у переводчиков нет, а этот адрес без ключа используют многие программы.
    Если Google его закроет, описание просто останется на языке оригинала.
    """
    try:
        data = fetch(TRANSLATE_URL, params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text}).json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        return translated.strip() or None
    except (requests.RequestException, ValueError, TypeError, IndexError) as e:
        log.warning("Не удалось перевести описание: %s", e)
        return None


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

    # Русского описания у игры нет — Steam присылает английское. Переводим только описания,
    # названия, разработчиков и жанры оставляем как в Steam.
    description, short_translated = _to_russian(_strip_html(d.get("short_description")))
    about = _strip_html(d.get("about_the_game") or d.get("detailed_description"))
    if about == description or len(about) <= len(description) + 40:
        about, about_translated = "", False  # полное описание ничего не добавляет к краткому
    else:
        about, about_translated = _to_russian(about)
    return {
        "version": DETAILS_VERSION,
        "appid": appid,
        "name": d.get("name", ""),
        "description": description,
        "about": about,
        "translated": short_translated or about_translated,
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
    texts = (details.get("description"), details.get("about"))
    if any(text and not _is_russian(text) for text in texts):
        return UNTRANSLATED_TTL
    return DETAILS_CACHE_TTL


def get_game_details(appid):
    """Карточка из кэша steam_details.json, а если её нет или она старше двух недель — из магазина."""
    key = str(appid)
    with _details_lock:
        cached = _load_details_cache().get(key)
    if (cached and cached.get("version") == DETAILS_VERSION
            and time.time() - cached.get("fetched", 0) < _details_ttl(cached)):
        return cached
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
    return details


def get_achievements(appid, api_key, steam_id):
    """Достижения игры: названия и описания на русском, доля игроков, получено ли у вас.

    Нужен ключ Steam Web API. Ваш прогресс Steam отдаёт только для игр на вашем аккаунте
    и при открытой игровой информации профиля — иначе "unlocked" = None.
    Возвращает None, если у игры нет достижений.
    """
    if not api_key:
        return {"error": "Чтобы показать достижения, введите ключ Steam Web API в настройках."}
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

    mine = None
    if steam_id:
        try:
            data = fetch(STEAM_PLAYER_ACHIEVEMENTS_URL,
                         params={"key": api_key, "steamid": steam_id, "appid": appid}).json()
            stats = data.get("playerstats") or {}
            if stats.get("success"):
                mine = {a["apiname"]: a.get("unlocktime", 0) for a in stats.get("achievements") or []
                        if a.get("achieved")}
        except (requests.RequestException, ValueError) as e:
            # Семейная игра, не на этом аккаунте или закрытый профиль — показываем без прогресса
            log.info("Прогресс достижений для %s недоступен: %s", appid, mask_secrets(e))

    result = []
    for a in items:
        api_name = a.get("name", "")
        unlocked_at = mine.get(api_name) if mine is not None else None
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
    return {"items": result, "unlocked": len(mine) if mine is not None else None}


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
    """Байты фона или логотипа: из Steam, иначе из SteamGridDB. None — нигде нет."""
    if game.get("appid"):
        url = (STEAM_HERO_URL if kind == "hero" else STEAM_LOGO_URL).format(appid=game["appid"])
        try:
            return fetch(url).content
        except requests.RequestException:
            pass  # у новых игр Steam хранит картинки по другим адресам — идём в SteamGridDB
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
    if not os.path.exists(missing):
        try:
            content = _download_art(game, kind, api_key)
            if content:
                image = Image.open(io.BytesIO(content))
                image.load()
            else:
                os.makedirs(os.path.dirname(missing), exist_ok=True)
                write_text(missing, "")
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
TRAILER_THREADS = 6  # кусочков качаем одновременно


class TrailerCancelled(Exception):
    """Трейлер закрыли, пока он скачивался."""


def _playlist_lines(url):
    text = fetch(url).text
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("по адресу не плейлист HLS")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _hls_attributes(line):
    """Атрибуты тега HLS: '#EXT-X-MEDIA:TYPE=AUDIO,URI="a.m3u8"' → {"TYPE": "AUDIO", "URI": "a.m3u8"}."""
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1])}


def _hls_variant(master_url, max_height):
    """Высота кадра, плейлист видео и плейлист звука (или None) — лучший вариант не выше max_height."""
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
    files.sort(key=os.path.getmtime, reverse=True)
    for path in files[TRAILER_CACHE_FILES:]:
        try:
            os.remove(path)
        except OSError:
            pass  # трейлер сейчас открыт — удалим в следующий раз


def _trailer_path(master_url):
    key = hashlib.md5(master_url.split("?")[0].encode("utf-8")).hexdigest()[:16]
    return os.path.join(TRAILERS_DIR, key + ".mp4")


def cached_trailer(master_url):
    """Путь к уже собранному трейлеру этого потока или None."""
    path = _trailer_path(master_url)
    if os.path.exists(path):
        os.utime(path)  # смотрели недавно — удалится последним
        return path
    return None


def trailer_file(master_url, progress=None, cancelled=None):
    """Трейлер из потока HLS одним файлом mp4 в лучшем качестве до 1080p. Возвращает путь к файлу.

    progress(высота кадра, проценты) — по ходу загрузки; cancelled() → True — бросить загрузку.
    Готовые файлы хранятся в cache/trailers, повторный просмотр открывается сразу.
    """
    os.makedirs(TRAILERS_DIR, exist_ok=True)
    cached = cached_trailer(master_url)
    if cached:
        return cached
    path = _trailer_path(master_url)

    height, video_url, audio_url = _hls_variant(master_url, TRAILER_MAX_HEIGHT)
    playlists = [_hls_segments(video_url)] + ([_hls_segments(audio_url)] if audio_url else [])
    urls = [url for init, segments in playlists for url in (init, *segments)]

    def download(url):
        if cancelled and cancelled():
            raise TrailerCancelled()
        return fetch(url).content

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

    temp = path + ".tmp"
    try:
        _remux([(parts[init], [parts[s] for s in segments]) for init, segments in playlists], temp)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    log.info("Трейлер собран из потока: %sp, %d кусочков, %.1f МБ", height, len(urls),
             os.path.getsize(path) / 1024 / 1024)
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
