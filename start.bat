@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   AI-помощник по обработке заявок
echo   --------------------------------

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Не найден Python.
  echo   Установите его: https://www.python.org/downloads/
  echo   Затем запустите этот файл ещё раз.
  echo.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo.
  echo   1-3 Создаю виртуальное окружение (.venv)...
  python -m venv .venv
  if errorlevel 1 (
    echo   Не удалось создать .venv.
    pause
    exit /b 1
  )
) else (
  echo.
  echo   1-3 Виртуальное окружение уже есть.
)

set NEEDS_INSTALL=0
if not exist ".venv\.deps_ok" set NEEDS_INSTALL=1
for /f %%i in ('powershell -Command "(Get-Item requirements.txt).LastWriteTime -gt (Get-Item .venv\.deps_ok).LastWriteTime" 2^>nul') do if "%%i"=="True" set NEEDS_INSTALL=1

if "%NEEDS_INSTALL%"=="1" (
  echo   Устанавливаю библиотеки ^(это делается один раз^)....
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo   Не удалось установить библиотеки. Проверьте подключение к интернету.
    pause
    exit /b 1
  )
  echo ok > .venv\.deps_ok
) else (
  echo   Библиотеки уже установлены.
)

set PORT=8000
if exist ".env" (
  for /f "tokens=1,* delims==" %%a in ('findstr /b "PORT=" .env') do set PORT=%%b
)

del /q .port 2>nul

echo   Запускаю сервер на порту %PORT% (если он занят, приложение выберет свободный)...
echo.

start "" cmd /c "for /l %%i in (1,1,60) do @(if exist .port (for /f %%p in (.port) do start "" http://127.0.0.1:%%p & exit /b) else (timeout /t 1 >nul))"

.venv\Scripts\python.exe app.py

echo.
echo   Приложение остановлено.
pause
