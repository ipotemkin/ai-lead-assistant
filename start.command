#!/bin/bash
# ---------------------------------------------------------------------------
# Запуск приложения в один клик (macOS).
# Двойной клик по этому файлу: создаст виртуальное окружение, поставит
# библиотеки, запустит сервер и откроет браузер.
# ---------------------------------------------------------------------------

cd "$(dirname "$0")" || exit 1

echo ""
echo "  AI-помощник по обработке заявок"
echo "  --------------------------------"

PYTHON_BIN="python3"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo ""
  echo "  Не найден Python 3."
  echo "  Установите его: https://www.python.org/downloads/"
  echo "  Затем запустите этот файл ещё раз."
  echo ""
  read -r -p "  Нажмите Enter, чтобы закрыть окно..."
  exit 1
fi

# --- 1. Виртуальное окружение -------------------------------------------
if [ ! -d ".venv" ]; then
  echo ""
  echo "  1/3 Создаю виртуальное окружение (.venv)..."
  "$PYTHON_BIN" -m venv .venv
  if [ $? -ne 0 ]; then
    echo "  Не удалось создать .venv. Проверьте, что Python установлен с официального сайта."
    read -r -p "  Нажмите Enter, чтобы закрыть окно..."
    exit 1
  fi
else
  echo ""
  echo "  1/3 Виртуальное окружение уже есть."
fi

# --- 2. Библиотеки -------------------------------------------------------
NEEDS_INSTALL=0
if [ ! -f ".venv/.deps_ok" ]; then
  NEEDS_INSTALL=1
elif [ requirements.txt -nt ".venv/.deps_ok" ]; then
  NEEDS_INSTALL=1
fi

if [ "$NEEDS_INSTALL" = "1" ]; then
  echo "  2/3 Устанавливаю библиотеки (это делается один раз)..."
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  if ! ./.venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo "  Не удалось установить библиотеки. Проверьте подключение к интернету."
    read -r -p "  Нажмите Enter, чтобы закрыть окно..."
    exit 1
  fi
  touch .venv/.deps_ok
else
  echo "  2/3 Библиотеки уже установлены."
fi

# --- 3. Запуск -----------------------------------------------------------
echo "  3/3 Запускаю сервер..."
echo ""

# Удаляем файл со старым портом, чтобы затем выбрать актуальный
rm -f .port

# Открываем браузер на реальном порте, на котором поднялось приложение.
# Приложение само выбирает свободный порт, если указанный в .env занят.
(
  for _ in $(seq 1 60); do
    if [ -f ".port" ]; then
      REAL_PORT=$(tr -d '[:space:]' < .port 2>/dev/null)
      if [ -n "$REAL_PORT" ] && curl -s -o /dev/null "http://127.0.0.1:$REAL_PORT/health" 2>/dev/null; then
        open "http://127.0.0.1:$REAL_PORT"
        break
      fi
    fi
    sleep 0.5
  done
) &

./.venv/bin/python app.py

echo ""
echo "  Приложение остановлено."
read -r -p "  Нажмите Enter, чтобы закрыть окно..."
