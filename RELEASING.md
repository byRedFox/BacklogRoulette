# Как выпускать новую версию

Релиз ПК-версии — из ветки `master`, через GitHub CLI (`gh`). Команды — для PowerShell 7 (или терминала VS Code).
Как вообще идёт разработка, правила и история — в `DEVELOPMENT.md`.

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

6. **APK для телефона** — один релиз на обе платформы (exe и apk вместе). APK собирает GitHub Actions в ветке
   `android` (подпись — ключом из Secrets). Скачать последнюю успешную сборку и сверить сумму:
   ```powershell
   $run = gh run list --workflow Android --branch android --status success --limit 1 --json databaseId --jq '.[0].databaseId'
   gh run download $run -n BacklogRoulette-android -D "$env:TEMP\br-apk"
   Get-Content "$env:TEMP\br-apk\SHA256SUMS.txt"
   Get-FileHash "$env:TEMP\br-apk\BacklogRoulette-android.apk" -Algorithm SHA256
   ```
   Суммы должны совпасть. Если папка `br-apk` осталась от прошлого раза — сначала удалите её. Проверить APK
   на телефоне по кабелю: `adb install -r "$env:TEMP\br-apk\BacklogRoulette-android.apk"`. Если в Android-версии
   в этот раз ничего не менялось — шаг можно пропустить и выложить релиз только с exe.

7. **Релиз**
   ```powershell
   Get-FileHash dist\BacklogRoulette.exe -Algorithm SHA256
   gh release create vX.Y.Z dist\BacklogRoulette.exe "$env:TEMP\br-apk\BacklogRoulette-android.apk" --title "Backlog Roulette X.Y.Z" --notes-file "$env:TEMP\BacklogRoulette-X.Y.Z.md" --verify-tag --latest
   ```
   `--verify-tag` не даст создать релиз без уже отправленного тега. ПК-программа ищет в релизе ровно
   `BacklogRoulette.exe` и на APK не смотрит. Отдельный релиз только с APK **не делаем**: став Latest, он спрятал бы
   обновление ПК-версии.

8. **Проверка**
   ```powershell
   gh release view vX.Y.Z --web
   ```
   На странице — заметка, `BacklogRoulette.exe`, `BacklogRoulette-android.apk` (если был) и пометка **Latest**.

## Если менялась только Android-версия

Новый релиз **не создаём**: в нём пришлось бы выложить тот же exe с новым номером, и ПК-программа без конца
предлагала бы «обновиться» на саму себя. APK докладываем в текущий Latest-релиз (заменяет прежний APK, exe и
пометку Latest не трогает):
```powershell
$tag = gh release view --json tagName --jq .tagName
gh release upload $tag "$env:TEMP\br-apk\BacklogRoulette-android.apk" --clobber
gh release view $tag --web
```
APK перед этим скачать и сверить, как в шаге 6.

## Если что-то пошло не так

- Ошибка в exe, а релиз ещё не опубликован — убрать тег и поставить заново после исправления:
  ```powershell
  git tag -d vX.Y.Z
  git push origin :refs/tags/vX.Y.Z
  ```
- Релиз уже опубликован — тег не трогаем, выпускаем следующую версию (X.Y.Z+1).
