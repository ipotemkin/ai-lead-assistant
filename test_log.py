"""
Проверки журнала операций.

Запуск:
    python test_log.py
    make test
"""

from __future__ import annotations

import sys

import logbuf

PASSED = 0
FAILED: list[str] = []


def check(condition: bool, message: str) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(message)


def main() -> int:
    logbuf.clear()

    # Запись содержит всё, что нужно для анализа
    logbuf.set_request_id("0001")
    record = logbuf.log(
        "test.step",
        "Проверка",
        duration_ms=12.5,
        model="gpt-5-mini",
        tokens=42,
    )
    for field in ("ts", "time", "request_id", "step", "level", "duration_ms", "message", "data"):
        check(field in record, f"В записи нет поля {field}")
    check_equal(record["request_id"], "0001", "Идентификатор запроса проставлен")
    check_equal(record["step"], "test.step", "Имя операции")
    check_equal(record["duration_ms"], 12.5, "Длительность")
    check_equal(record["data"].get("tokens"), 42, "Дополнительные данные")

    # Записи накапливаются
    logbuf.log("test.second", "Ещё одна")
    check(len(logbuf.records()) >= 2, "Записи накапливаются")

    # Ограничение выборки
    last = logbuf.records(limit=1)
    check_equal(len(last), 1, "Выборка последних записей")
    check_equal(last[0]["step"], "test.second", "Возвращается самая свежая запись")

    # Идентификатор запроса меняется
    first_id = logbuf.new_request_id()
    second_id = logbuf.new_request_id()
    check(first_id != second_id, f"Идентификаторы запросов разные: {first_id} и {second_id}")
    logbuf.set_request_id(second_id)
    check_equal(logbuf.current_request_id(), second_id, "Текущий идентификатор установлен")

    # Таймер измеряет время
    timer = logbuf.Timer()
    check(timer.ms >= 0, "Таймер отдаёт длительность")
    check(timer.seconds >= 0, "Таймер отдаёт секунды")

    # Очистка
    logbuf.clear()
    check_equal(len(logbuf.records()), 0, "Журнал очищается")

    return 0


def check_equal(actual, expected, message: str) -> None:
    check(actual == expected, f"{message}: получено {actual!r}, ожидалось {expected!r}")


if __name__ == "__main__":
    main()
    print()
    print(f"Пройдено проверок: {PASSED}")
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)}")
        for item in FAILED:
            print(f"  ✗ {item}")
        sys.exit(1)
    print("Все проверки пройдены.")
