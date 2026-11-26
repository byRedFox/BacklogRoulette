"""Backlog Roulette — чтение кнопки Share геймпада Xbox через SDL в отдельном процессе.

Запускает его программа (core.GamepadShareWatcher), пока идёт игра: из сборки — сама BacklogRoulette.exe с ключом
--gamepad-share, из исходников — python -I gamepad_share.py. Отдельный процесс нужен потому, что Raw Input Windows
принимает в одном процессе только одно окно на вид устройств: внутри программы геймпад видели, а нажатия до SDL
не доходили (похоже, их забирает Qt или встроенный браузер). Свой процесс — только SDL, делить не с кем.

Только чтение: геймпад открывается вместе с игрой, вибрацию, подсветку и настройки Windows процесс не трогает.
Своих файлов не пишет. О себе сообщает строками в stdout: «вид<TAB>текст» — ready, log, press, no_sdl, failed.
Закрывается, когда программа закрывает его stdin или сама закрывается. Модули программы не импортирует —
только стандартную библиотеку и pysdl2 (SDL2.dll — из папки, которую передала программа в PYSDL2_DLL_PATH).
"""

import os
import sys
import threading
import time

POLL = 0.03  # с какой паузой спрашивать SDL о кнопках (30 мс — нажатие не пропустить)
REPEAT = 0.5  # повторное нажатие быстрее — это дребезг или удержание, не новый снимок
SILENT = 5  # с: геймпад так и не появился — строка в журнал (для разбора)
CONTROLLER_TYPE_XBOXONE = 2  # Xbox One и Xbox Series: Share у них — кнопка MISC1
XBOX_SERIES_PRODUCTS = {0x0B12, 0x0B13}  # Xbox Series по кабелю и по Bluetooth (Microsoft, 045E)
XBOX_SERIES_SHARE_BUTTON = 11  # Share у Xbox Series в Raw Input — если SDL не назвала её MISC1 сама
# Xbox — через Raw Input (по Bluetooth на Windows только он и отдаёт Share) и HIDAPI (по кабелю). XInput включён
# лишь для сверки: SDL берёт из него курки и кнопку Xbox к тому же геймпаду, отдельно его не открывает. Прочие
# драйверы HIDAPI выключены: геймпады Switch и PlayStation SDL при открытии переводит в свой режим отчётов
HINTS = {
    "SDL_JOYSTICK_HIDAPI": "1",
    "SDL_JOYSTICK_HIDAPI_XBOX": "1",
    "SDL_JOYSTICK_HIDAPI_XBOX_ONE": "1",
    "SDL_JOYSTICK_HIDAPI_XBOX_360": "0",
    **{f"SDL_JOYSTICK_HIDAPI_{name}": "0" for name in (
        "PS3", "PS4", "PS5", "SWITCH", "JOY_CONS", "COMBINE_JOY_CONS", "GAMECUBE", "LUNA", "SHIELD", "STADIA",
        "STEAM", "STEAMDECK", "WII", "NINTENDO_CLASSIC")},
    "SDL_JOYSTICK_RAWINPUT": "1",
    "SDL_JOYSTICK_WGI": "0",
    "SDL_XINPUT_ENABLED": "1",
    "SDL_DIRECTINPUT_ENABLED": "0",
    "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS": "1",
}
DRIVERS = {ord("h"): "HIDAPI", ord("r"): "Raw Input", ord("x"): "XInput", ord("w"): "WGI"}  # буква в GUID

_out_lock = threading.Lock()


def say(kind, text=""):
    """Строка программе. Не вышло — программы уже нет: закрываемся."""
    line = f"{kind}\t{text}".replace("\r", " ").replace("\n", " ")[:400] + "\n"
    try:
        with _out_lock:
            os.write(1, line.encode("utf-8", "replace"))
    except OSError:
        os._exit(0)


def watch_parent(stop, parent_pid):
    """Программа закрыла stdin или закрылась сама — пора и нам."""
    def stdin_closed():
        try:
            while os.read(0, 64):
                pass
        except OSError:
            return  # stdin не дали — остаётся проверка процесса программы
        stop.set()

    threading.Thread(target=stdin_closed, name="stdin", daemon=True).start()
    if not parent_pid or sys.platform != "win32":
        return None
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    handle = kernel32.OpenProcess(0x00100000, 0, parent_pid)  # SYNCHRONIZE — только ждать завершения
    if not handle:
        return None
    return lambda: kernel32.WaitForSingleObject(handle, 0) == 0  # 0 — процесс программы завершился


def message_pump():
    """Разбор сообщений Windows этого потока. Нажатия Raw Input приходят сообщениями WM_INPUT в скрытое окно, которое
    SDL создаёт в потоке SDL_Init, — а без видео SDL сама их не разбирает, и кнопки остаются «отпущенными».
    Возвращает функцию: разобрать всё, что накопилось; ответ — сколько среди них было WM_INPUT."""
    if sys.platform != "win32":
        return lambda: 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                    wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    msg, pm_remove, wm_input = wintypes.MSG(), 0x0001, 0x00FF

    def pump():
        inputs = 0
        for _ in range(500):  # за один раз — не больше 500, чтобы не застрять
            if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, pm_remove):
                break
            inputs += msg.message == wm_input
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return inputs

    return pump


def open_pad(sdl2, index, controllers):
    """Открыть геймпад, если это Xbox с кнопкой Share; программе — что за геймпад (для разбора)."""
    pad = sdl2.SDL_GameControllerOpen(index)
    if not pad:
        return
    joystick = sdl2.SDL_GameControllerGetJoystick(pad)
    name = (sdl2.SDL_GameControllerName(pad) or b"").decode("utf-8", "replace")[:60] or "геймпад"
    kind = sdl2.SDL_GameControllerGetType(pad)
    product = sdl2.SDL_JoystickGetProduct(joystick)
    buttons = max(0, sdl2.SDL_JoystickNumButtons(joystick))
    driver = DRIVERS.get(sdl2.SDL_JoystickGetGUID(joystick).data[14], "?")
    instance = sdl2.SDL_JoystickInstanceID(joystick)
    raw = None
    share = bool(sdl2.SDL_GameControllerHasButton(pad, sdl2.SDL_CONTROLLER_BUTTON_MISC1))
    if not share and product in XBOX_SERIES_PRODUCTS and buttons > XBOX_SERIES_SHARE_BUTTON:
        share, raw = True, XBOX_SERIES_SHARE_BUTTON
    details = f"тип SDL {kind}, {driver}, кнопок {buttons}, PID {product:04X}"
    if kind != CONTROLLER_TYPE_XBOXONE or not share or instance in controllers:
        say("log", f"Геймпад «{name}» ({details}): кнопки Share нет — не слушаю")
        sdl2.SDL_GameControllerClose(pad)
        return
    mapping = (sdl2.SDL_GameControllerMapping(pad) or b"").decode("utf-8", "replace")
    misc1 = next((part for part in mapping.split(",") if part.startswith("misc1:")), "misc1: нет")
    controllers[instance] = {"pad": pad, "joystick": joystick, "name": name, "raw": raw,
                             "buttons": [False] * buttons, "share": False}
    say("log", f"Геймпад «{name}» ({details}, {misc1}): кнопка Share снимает экран")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parent_pid = int(argv[argv.index("--parent") + 1]) if "--parent" in argv[:-1] else 0
    stop = threading.Event()
    parent_gone = watch_parent(stop, parent_pid)
    try:
        import ctypes
        import sdl2
    except Exception as e:  # noqa: BLE001 — pysdl2 бросает и ImportError, и RuntimeError
        say("no_sdl", f"SDL не загрузилась ({e})")
        return 1
    controllers = {}  # номер геймпада в SDL → геймпад, имя и прошлое состояние кнопок
    started = False
    try:
        for name, value in HINTS.items():
            sdl2.SDL_SetHint(name.encode(), value.encode())
        if sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER) != 0:
            say("no_sdl", "SDL не запустилась — " + (sdl2.SDL_GetError() or b"").decode("utf-8", "replace"))
            return 1
        started = True
        version = sdl2.SDL_version()
        sdl2.SDL_GetVersion(ctypes.byref(version))
        say("ready", f"{version.major}.{version.minor}.{version.patch}")
        pump = message_pump()
        event, last = sdl2.SDL_Event(), 0.0
        silent_since, seen_any, logged = time.monotonic(), False, set()
        inputs, inputs_said = 0, False
        while not stop.wait(POLL):
            if parent_gone and parent_gone():
                break
            inputs += pump()
            if inputs and not inputs_said:
                inputs_said = True  # для разбора: Windows присылает нажатия геймпада этому процессу
                say("log", "Windows присылает данные геймпада (Raw Input)")
            elif not inputs_said and controllers and time.monotonic() - silent_since > 30:
                inputs_said = True
                say("log", "за 30 с Windows не прислала ни одного сообщения Raw Input от геймпада")
            if not seen_any and time.monotonic() - silent_since > SILENT:
                seen_any = True
                names = [(sdl2.SDL_JoystickNameForIndex(i) or b"?").decode("utf-8", "replace")[:60]
                         for i in range(max(0, sdl2.SDL_NumJoysticks()))]
                say("log", f"SDL не видит ни одного геймпада Xbox (устройств ввода: {len(names)}"
                           + (" — " + ", ".join(names) if names else "") + ")")
            # События — только о подключении; нажатия читаются состоянием кнопок
            while sdl2.SDL_PollEvent(ctypes.byref(event)):
                if event.type == sdl2.SDL_CONTROLLERDEVICEADDED:
                    seen_any = True
                    open_pad(sdl2, event.cdevice.which, controllers)
                elif event.type == sdl2.SDL_CONTROLLERDEVICEREMOVED:
                    pad = controllers.pop(event.cdevice.which, None)
                    if pad:
                        sdl2.SDL_GameControllerClose(pad["pad"])
                        say("log", f"Геймпад отключён: {pad['name']}")
            sdl2.SDL_GameControllerUpdate()
            for pad in controllers.values():
                joystick, pressed = pad["joystick"], False
                for button in range(len(pad["buttons"])):
                    down = bool(sdl2.SDL_JoystickGetButton(joystick, button))
                    if down and not pad["buttons"][button]:
                        if (pad["name"], button) not in logged:  # для разбора: что вообще доходит — по разу
                            logged.add((pad["name"], button))
                            say("log", f"Геймпад «{pad['name']}»: нажата кнопка {button}")
                        pressed |= button == pad["raw"]
                    pad["buttons"][button] = down
                share = bool(sdl2.SDL_GameControllerGetButton(pad["pad"], sdl2.SDL_CONTROLLER_BUTTON_MISC1))
                if share and not pad["share"]:
                    if ("misc1", pad["name"]) not in logged:
                        logged.add(("misc1", pad["name"]))
                        say("log", f"Геймпад «{pad['name']}»: SDL увидела кнопку Share")
                    pressed = True
                pad["share"] = share
                if pressed and time.monotonic() - last > REPEAT:
                    last = time.monotonic()
                    say("press", pad["name"])
    except Exception as e:  # noqa: BLE001
        say("failed", f"{type(e).__name__}: {e}")
        return 1
    finally:
        for pad in controllers.values():
            sdl2.SDL_GameControllerClose(pad["pad"])
        if started:
            sdl2.SDL_Quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
