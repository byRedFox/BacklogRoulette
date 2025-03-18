import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog
from PIL import Image, ImageTk
import pandas as pd
import random
import os
import re
import pyperclip
import requests  # Для выполнения HTTP-запросов

# Константы для настройки приложения
EXCEL_FILE_PATH = r'D:\SteamLibrary\steamrandom\SteamGames.xlsx'
COVERS_DIR = r'D:\SteamLibrary\steamrandom\covers'
INITIAL_WINDOW_SIZE = "500x250"
FINAL_WINDOW_SIZE = "600x900"
FONT = ("Comic Sans", 12)
SUPPORTED_IMAGE_FORMATS = ['.png', '.jpg', '.jpeg']
STEAMGRIDDB_API_URL = "https://www.steamgriddb.com/api/v2"
API_KEY_FILE = "api_key.txt"  # Файл для хранения API ключа

# Функция для загрузки API ключа из файла
def load_api_key():
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.read().strip()
    return None

# Функция для сохранения API ключа в файл
def save_api_key(api_key):
    with open(API_KEY_FILE, "w") as f:
        f.write(api_key)

# Функция для очистки текста от недопустимых символов
def clean_filename(text):
    return re.sub(r'[<>:"/\\|?*]', '', text)

# Функция для загрузки данных из Excel-файла
def load_games():
    try:
        df = pd.read_excel(EXCEL_FILE_PATH)
        if df.empty:
            messagebox.showinfo("Ошибка", "Файл пуст или не содержит данных.")
            return None
        return df['Игры'].tolist()
    except FileNotFoundError:
        messagebox.showinfo("Ошибка", "Файл не найден.")
        return None
    except Exception as e:
        messagebox.showinfo("Ошибка", f"Произошла ошибка при загрузке файла: {e}")
        return None

# Функция для удаления игры из списка и файла обложки
def delete_game(game_name):
    try:
        df = pd.read_excel(EXCEL_FILE_PATH)
        df = df[df['Игры'] != game_name]
        df.to_excel(EXCEL_FILE_PATH, index=False)

        for ext in SUPPORTED_IMAGE_FORMATS:
            icon_path = os.path.join(COVERS_DIR, f"{game_name}{ext}")
            if os.path.exists(icon_path):
                os.remove(icon_path)
                break

        messagebox.showinfo("Успех", f"Игра '{game_name}' удалена.")
    except Exception as e:
        messagebox.showinfo("Ошибка", f"Не удалось удалить игру: {e}")

# Функция для ручной загрузки обложки игры
def upload_game_cover(game_name):
    try:
        file_path = filedialog.askopenfilename(
            title="Выберите обложку для игры",
            filetypes=[("Изображения", "*.png;*.jpg;*.jpeg")]
        )
        if file_path:
            cover_path = os.path.join(COVERS_DIR, f"{game_name}{os.path.splitext(file_path)[1]}")
            with open(file_path, "rb") as src_file, open(cover_path, "wb") as dest_file:
                dest_file.write(src_file.read())
            return cover_path
        else:
            return None
    except Exception as e:
        messagebox.showinfo("Ошибка", f"Не удалось загрузить обложку: {e}")
        return None

# Функция для скачивания обложки с SteamGridDB
def download_game_cover(game_name, size=(300, 450)):
    api_key = load_api_key()
    if not api_key:
        messagebox.showinfo("Ошибка", "API ключ не найден. Пожалуйста, введите API ключ.")
        return None

    try:
        headers = {
            "Authorization": f"Bearer {api_key}"  # Используем загруженный API ключ
        }
        # Ищем игру по названию
        response = requests.get(f"{STEAMGRIDDB_API_URL }/search/autocomplete/{game_name}", headers=headers)
        if response.status_code == 200:
            games = response.json().get('data', [])
            if games:
                game_id = games[0]['id']  # Берем первый результат
                # Ищем обложки для игры
                response = requests.get(f"{STEAMGRIDDB_API_URL}/grids/game/{game_id}", headers=headers)
                if response.status_code == 200:
                    grids = response.json().get('data', [])
                    if grids:
                        # Ищем обложку с нужным разрешением (если есть)
                        for grid in grids:
                            if grid['width'] == size[0] and grid['height'] == size[1]:
                                image_url = grid['url']
                                break
                        else:
                            # Если обложка с нужным разрешением не найдена, берем первую
                            image_url = grids[0]['url']

                        # Скачиваем обложку
                        image_response = requests.get(image_url)
                        if image_response.status_code == 200:
                            cover_path = os.path.join(COVERS_DIR, f"{game_name}.png")
                            with open(cover_path, "wb") as f:
                                f.write(image_response.content)

                            # Изменяем размер обложки, если она не соответствует нужному разрешению
                            image = Image.open(cover_path)
                            if image.size != size:
                                image = image.resize(size, Image.Resampling.LANCZOS)
                                image.save(cover_path)

                            return cover_path
    except Exception as e:
        messagebox.showinfo("Ошибка", f"Не удалось скачать обложку: {e}")
    return None

# Функция для добавления нескольких игр
def add_multiple_games(game_names):
    games_added = []
    for game_name in game_names:
        cleaned_game = clean_filename(game_name.strip())
        if cleaned_game:
            try:
                # Загружаем текущий список игр
                df = pd.read_excel(EXCEL_FILE_PATH)
                # Добавляем новую игру
                df = df._append({'Игры': cleaned_game}, ignore_index=True)
                # Сохраняем изменения в файл
                df.to_excel(EXCEL_FILE_PATH, index=False)
                # Обновляем список игр в памяти
                games.append(cleaned_game)
                games_added.append(cleaned_game)

                # Пробуем скачать обложку
                cover_path = download_game_cover(cleaned_game)
                if cover_path:
                    messagebox.showinfo("Успех", f"Обложка для игры '{cleaned_game}' скачана.")
                else:
                    # Если не удалось скачать, предлагаем загрузить с компьютера
                    response = messagebox.askyesno("Ошибка", f"Не удалось скачать обложку для '{cleaned_game}'. Хотите загрузить с компьютера?")
                    if response:
                        cover_path = upload_game_cover(cleaned_game)
                        if cover_path:
                            messagebox.showinfo("Успех", f"Обложка для игры '{cleaned_game}' загружена.")
                        else:
                            messagebox.showinfo("Ошибка", f"Обложка для игры '{cleaned_game}' не была загружена.")
            except Exception as e:
                messagebox.showinfo("Ошибка", f"Не удалось добавить игру '{cleaned_game}': {e}")
    
    if games_added:
        messagebox.showinfo("Успех", f"Добавлены игры: {', '.join(games_added)}")
        # После добавления игр предлагаем добавить ещё или выбрать случайную игру
        choice = messagebox.askyesno("Добавить ещё?", "Хотите добавить ещё одну игру?")
        if choice:
            custom_add_game_dialog()  # Открываем диалог снова
        else:
            # Если пользователь не хочет добавлять ещё, предлагаем выбрать случайную игру
            choice = messagebox.askyesno("Выбрать случайную игру", "Хотите выбрать случайную игру?")
            if choice:
                choose_random_game()
            else:
                # Если пользователь отказывается, выводим диалог "Покеда"
                messagebox.showinfo("Покеда", "До встречи в следующий раз!")
                root.destroy()  # Закрываем программу

# Функция для выбора случайной игры
def choose_random_game():
    if not games:
        messagebox.showinfo("Ошибка", "Нет доступных игр.")
        return
    
    # Выбор случайной игры
    global selected_game
    selected_game = random.choice(games)
    game_label.config(text=selected_game)  # Обновляем текст метки с названием игры

    # Поиск изображения игры
    icon_path = find_game_image(selected_game)

    # Загрузка и отображение иконки
    try:
        if icon_path:
            image = Image.open(icon_path)
            image = image.resize((300, 450), Image.Resampling.LANCZOS)  # Изменяем размер обложки на 300x450
            photo = ImageTk.PhotoImage(image)
            icon_label.config(image=photo)
            icon_label.image = photo  # Сохраняем ссылку, чтобы изображение не удалялось сборщиком мусора
        else:
            icon_label.config(image='')  # Если иконка не найдена, очищаем поле
            messagebox.showinfo("Ошибка", f"Иконка для игры '{selected_game}' не найдена.")
    except Exception as e:
        messagebox.showinfo("Ошибка", f"Не удалось загрузить иконку: {e}")

    # Изменение размера окна после выбора игры
    root.geometry(FINAL_WINDOW_SIZE)

    # Центрирование окна
    center_window(root)

    # Показ диалогового окна с вопросом об удалении игры
    response = messagebox.askyesno("Удалить игру", f"Удалить игру '{selected_game}' из списка?")
    if response:  # Если пользователь выбрал "Да"
        delete_game(selected_game)
        games.remove(selected_game)  # Удаляем игру из списка в памяти
        # Выводим окно "Покеда" после удаления игры
        messagebox.showinfo("Покеда", "До встречи в следующий раз!")
        root.destroy()  # Закрываем программу
    else:  # Если пользователь выбрал "Нет"
        # Предлагаем добавить игру в список
        choice = messagebox.askyesno("Добавить игру в список", "Хотите добавить игру в список?")
        if choice:
            custom_add_game_dialog()  # Открываем диалог добавления игры
        else:
            # Если пользователь отказывается, выводим окно "Покеда"
            messagebox.showinfo("Покеда", "До встречи в следующий раз!")
            root.destroy()  # Закрываем программу

# Функция для поиска изображения игры
def find_game_image(game_name):
    for ext in SUPPORTED_IMAGE_FORMATS:
        icon_path = os.path.join(COVERS_DIR, f"{game_name}{ext}")
        if os.path.exists(icon_path):
            return icon_path
    return None  # Если изображение не найдено

# Функция для центрирования окна
def center_window(window):
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    x = (window.winfo_screenwidth() // 2) - (width // 2)
    y = (window.winfo_screenheight() // 2) - (height // 2)
    window.geometry(f'{width}x{height}+{x}+{y}')

# Собственное диалоговое окно для добавления игры
def custom_add_game_dialog():
    def on_submit():
        input_text = entry.get().strip()
        if input_text:
            # Разделяем игры по символу "|"
            game_names = input_text.split('|')
            add_multiple_games(game_names)
            dialog.destroy()
        else:
            messagebox.showwarning("Ошибка", "Название игры не может быть пустым.")

    # Создаем собственное диалоговое окно
    dialog = tk.Toplevel(root)
    dialog.title("Добавить игру")
    dialog.geometry("500x250")  # Увеличиваем размер окна
    dialog.resizable(False, False)
    dialog.grab_set()  # Делаем окно модальным (поверх всех окон)
    dialog.transient(root)  # Связываем диалоговое окно с главным окном
    dialog.lift()  # Поднимаем окно поверх всех других окон

    # Заголовок
    label = tk.Label(dialog, text="Введите названия игр через '|':", font=FONT)
    label.pack(pady=10)

    # Поле ввода
    entry = tk.Entry(dialog, font=FONT, width=40)
    entry.pack(pady=10)
    entry.focus_set()  # Устанавливаем фокус на поле ввода

    # Кнопка "Вставить" (используем pyperclip для вставки из буфера)
    def paste_text():
        try:
            text = pyperclip.paste()  # Получаем текст из буфера обмена
            if text:
                entry.insert(tk.INSERT, text)
        except Exception as e:
            messagebox.showinfo("Ошибка", f"Не удалось вставить текст: {e}")

    paste_button = tk.Button(dialog, text="Вставить", command=paste_text, font=FONT)
    paste_button.pack(pady=5)

    # Кнопка "Добавить"
    submit_button = tk.Button(dialog, text="Добавить", command=on_submit, font=FONT)
    submit_button.pack(pady=10)

# Функция для добавления новой игры в список
def add_game():
    custom_add_game_dialog()

# Функция для ввода и сохранения API ключа
def set_api_key():
    api_key = simpledialog.askstring("Ввод API ключа", "Введите ваш API ключ для SteamGridDB:")
    if api_key:
        save_api_key(api_key)
        messagebox.showinfo("Успех", "API ключ сохранён.")

# Загрузка списка игр при запуске программы
games = load_games()

# Создание основного окна
root = tk.Tk()
root.title("My Steam Random")
root.geometry(INITIAL_WINDOW_SIZE)  # Устанавливаем начальный размер окна

# Центрирование окна
center_window(root)

# Создание метки для отображения выбранной игры
game_label = tk.Label(root, text="Choose Your Next Game!", font=FONT)
game_label.pack(pady=10)

# Создание метки для отображения иконки
icon_label = tk.Label(root)
icon_label.pack()

# Создание кнопки для выбора игры
button = tk.Button(root, text="Play Next...", command=choose_random_game)
button.pack(pady=10)

# Создание кнопки для добавления новой игры
add_button = tk.Button(root, text="Add Play Next", command=add_game)
add_button.pack(pady=10)

# Создание кнопки для ввода API ключа
api_key_button = tk.Button(root, text="Ввести API ключ", command=set_api_key)
api_key_button.pack(pady=10)

# Запуск основного цикла обработки событий
root.mainloop()