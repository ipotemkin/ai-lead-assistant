# ---------------------------------------------------------------------------
# Команды для запуска проекта.
#
#   make run     — запустить приложение (главная команда)
#   make setup   — только подготовить окружение и библиотеки
#   make clean   — убрать кэши Python
#   make help    — список всех команд
#
# Остановить работающее приложение: Ctrl+C в этом же окне.
# ---------------------------------------------------------------------------

PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
STAMP  := $(VENV)/.deps_ok

.DEFAULT_GOAL := help

.PHONY: help setup run test clean

help: ## Показать список команд
	@echo ""
	@echo "  Команды:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "    \033[36m%-8s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  После запуска приложение будет доступно на http://127.0.0.1:8000"

setup: $(STAMP) ## Создать виртуальное окружение и поставить библиотеки

# Библиотеки ставятся только если их нет или изменился requirements.txt
$(STAMP): requirements.txt
	@echo "  Готовлю виртуальное окружение (.venv)..."
	@$(PYTHON) -m venv $(VENV)
	@$(BIN)/python -m pip install --quiet --upgrade pip
	@$(BIN)/python -m pip install --quiet -r requirements.txt
	@touch $@
	@echo "  Готово."

run: setup ## Запустить приложение
	@PORT=$$(grep -E '^[[:space:]]*PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' \r"'); \
		echo ""; \
		echo "  Приложение запускается: http://127.0.0.1:$${PORT:-8000}"; \
		echo "  Остановить: Ctrl+C"; \
		echo ""
	@$(BIN)/python app.py

test: setup ## Проверить обезличивание, интеграцию с Битрикс24 и журнал
	@$(BIN)/python test_anonymizer.py
	@$(BIN)/python test_bitrix.py
	@$(BIN)/python test_log.py

clean: ## Удалить кэши Python
	@rm -rf __pycache__ */__pycache__
	@echo "  Кэши удалены."
