"""
Журнал операций приложения.

Каждая операция попадает в журнал с меткой времени, идентификатором запроса
и затраченным временем. Журнал нужен, чтобы видеть, где именно тормозит
обработка заявки: в модели, в разборе ответа или в отправке в CRM.

Два места, где смотреть журнал:
  - окно Терминала, где запущено приложение;
  - GET /api/log — журнал последних записей в формате JSON.

В журнал НЕ попадают персональные данные: только длины текстов, количества
найденных сущностей, идентификаторы и время.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from typing import Any

# Сколько последних записей держим в памяти
MAX_RECORDS = 1000

_lock = threading.Lock()
_records: list[dict[str, Any]] = []
_context = threading.local()
_counter = 0


def new_request_id() -> str:
    """Новый короткий идентификатор запроса."""
    global _counter
    with _lock:
        _counter += 1
        number = _counter
    return f"{number:04d}"


def set_request_id(value: str) -> None:
    _context.request_id = value


def current_request_id() -> str:
    return getattr(_context, "request_id", "-")


class Timer:
    """Замер времени операции."""

    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((time.perf_counter() - self.start) * 1000, 1)

    @property
    def seconds(self) -> float:
        return round((time.perf_counter() - self.start) / 1000, 2)


def log(
    step: str,
    message: str = "",
    *,
    duration_ms: float | None = None,
    level: str = "info",
    **data: Any,
) -> dict[str, Any]:
    """Добавляет запись в журнал и печатает её в Терминал."""
    record: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
        "request_id": current_request_id(),
        "step": step,
        "level": level,
        "duration_ms": duration_ms,
        "message": message,
        "data": data,
    }

    with _lock:
        _records.append(record)
        if len(_records) > MAX_RECORDS:
            del _records[: len(_records) - MAX_RECORDS]

    print(_format(record), file=sys.stderr, flush=True)
    return record


def _format(record: dict[str, Any]) -> str:
    duration = record.get("duration_ms")
    duration_text = f"{duration:>9.1f} ms" if isinstance(duration, (int, float)) else " " * 14
    parts = [
        f"[{record['time']}]",
        f"{record['request_id']:>4}",
        f"{record['step']:<22}",
        duration_text,
        record.get("message") or "",
    ]
    line = " ".join(part for part in parts if part)

    data = record.get("data") or {}
    if data:
        details = ", ".join(f"{key}={value}" for key, value in data.items())
        line = f"{line} | {details}" if details else line
    return line


def records(limit: int | None = None) -> list[dict[str, Any]]:
    """Последние записи журнала."""
    with _lock:
        items = list(_records)
    if limit is not None and limit > 0:
        items = items[-limit:]
    return items


def clear() -> None:
    """Очищает журнал."""
    with _lock:
        _records.clear()
