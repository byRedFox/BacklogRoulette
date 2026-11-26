# Как выпускать новую версию

Релиз ПК-версии — из ветки `master`, через GitHub CLI (`gh`). Команды — для PowerShell 7 (или терминала VS Code).

## Правила

- Тег и релиз — **только после проверки собранного exe**.
- Файл в релизе всегда называется ровно `BacklogRoulette.exe`: по нему программа находит обновление и сверяет SHA-256.
- `git push --force` не используем никогда.
- Ключи и личные файлы (`api_key.txt`, `settings.json`, `covers/`, `notes/`, `screenshots/` и т. п.) в git не попадают — перед `git add` смотрим `git status`.
- Слияние — только `master` → `android`.

## Один раз: установка и вход в gh

```powershell
winget install --id GitHub.cli -e
gh auth login      # GitHub.com → HTTPS → Yes → Login with a web browser
gh auth status
```

Вход — через браузер, пароль и ключи в терминал не вводятся.

## Порядок выпуска (вместо X.Y.Z — номер версии)

1. **Версия** — в `__version__` (`MySteamRandomApp.py`) и в `version_info.txt` (filevers, prodvers и строки версии). «Что нового» — в `README.md`.

2. **Коммит и ветки**
   ```powershell
   cd D:\Projects\BacklogRoulette
   .\.venv\Scripts\Activate.ps1
   git status
   git add <изменённые файлы>
   git commit -m "X.Y.Z: кратко, что нового"
   git push origin master
   git checkout android
   git merge master --no-edit
   git push origin android
   git checkout master
   ```

3. **Сборка и проверка** — `build.bat`, exe появится в `dist\BacklogRoulette.exe`. Проверить в нём то, что менялось.

4. **Тег** — на `master`:
   ```powershell
   git pull origin master
   git tag -a vX.Y.Z -m "Backlog Roulette X.Y.Z"
   git push origin vX.Y.Z
   ```

5. **Заметка к выпуску** (на русском и английском) — во временную папку Windows, не в проект:
   ```powershell
   @'
   **Backlog Roulette X.Y.Z**

   - …

   ---

   - …
   '@ | Out-File -Encoding utf8 "$env:TEMP\BacklogRoulette-X.Y.Z.md"
   ```
   Строка `'@` — в самом начале строки, без пробелов перед ней.

6. **Релиз**
   ```powershell
   Get-FileHash dist\BacklogRoulette.exe -Algorithm SHA256
   gh release create vX.Y.Z dist\BacklogRoulette.exe --title "Backlog Roulette X.Y.Z" --notes-file "$env:TEMP\BacklogRoulette-X.Y.Z.md" --verify-tag --latest
   ```
   `--verify-tag` не даст создать релиз без уже отправленного тега.

7. **Проверка**
   ```powershell
   gh release view vX.Y.Z --web
   ```
   На странице — заметка, `BacklogRoulette.exe` и пометка **Latest**.

## Если что-то пошло не так

- Ошибка в exe, а релиз ещё не опубликован — убрать тег и поставить заново после исправления:
  ```powershell
  git tag -d vX.Y.Z
  git push origin :refs/tags/vX.Y.Z
  ```
- Релиз уже опубликован — тег не трогаем, выпускаем следующую версию (X.Y.Z+1).
