@echo off
rem Build MySteamRandomApp.exe and copy it to the project folder.
rem Keep this file ASCII-only: cmd.exe misreads non-ASCII characters in .bat files.
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto error

python -m PyInstaller --noconfirm MySteamRandomApp.spec
if errorlevel 1 goto error

rem The app looks for games.json, covers and api_key.txt next to itself
copy /Y "dist\MySteamRandomApp.exe" "MySteamRandomApp.exe" >nul
if errorlevel 1 goto error

echo.
echo Done: MySteamRandomApp.exe is in the project folder.
pause
exit /b 0

:error
echo.
echo Build failed, see the messages above.
pause
exit /b 1
