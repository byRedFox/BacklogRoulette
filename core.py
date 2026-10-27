"""Логика без интерфейса: список игр, обложки, Steam, настройки."""

import functools
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
import time
import wave
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

import requests
from PIL import Image

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
NAMES_CACHE_FILE = os.path.join(BASE_DIR, "steam_app_names.json")  # названия семейных игр
NAMES_CACHE_TTL = 7 * 24 * 3600  # неделя
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus")
ICON_NAME = "1.ico"
MUSIC_NAME = "apptheme.mp3"

COVER_SIZE = (300, 450)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")

STEAMGRIDDB_API_URL = "https://www.steamgriddb.com/api/v2"
STEAM_OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
STEAM_APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
STEAM_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_COVER_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
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
            return "".join(f.readlines()[-max_lines:])
    except OSError:
        return ""


def clear_log():
    with open(LOG_FILE, "w", encoding="utf-8"):
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
