"""
Проверка качества классификации и скорости обработки.

Прогоняет эталонные заявки через текущую модель и показывает, что получилось
и сколько времени заняло. Нужен, чтобы перед любым изменением промпта или модели
убедиться, что точность не просела.

Запуск:
    python check_quality.py
    make check-quality
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv('.env')

import ai  # noqa: E402
import bitrix  # noqa: E402
from app import apply_guards, normalize_card  # noqa: E402

# Эталонные заявки: (название, текст, ожидаемый тип лида, нужен ли человек)
CASES: list[tuple[str, str, str, bool]] = [
    (
        "Полная заявка",
        "Здравствуйте! Меня зовут Иван Петров, компания ООО ОфисЛайн, отдел закупок. "
        "Нужны 20 офисных столов и 40 кресел, бюджет 500 000 рублей, нужен запуск до конца месяца.",
        "целевой",
        False,
    ),
    (
        "Только категория",
        "Добрый день! Меня зовут иван. Сколько у вас стоят офисные кресла? Нужно несколько штук.",
        "неясно",
        False,
    ),
    (
        "Неполная",
        "Хотим обсудить сотрудничество. Свяжитесь с нами, пожалуйста.",
        "неясно",
        False,
    ),
    (
        "Конфликтная",
        "Здравствуйте. Вы привезли 10 столов не того цвета. Требуем вернуть деньги, "
        "иначе уйдём к конкурентам.",
        "целевой",
        True,
    ),
    (
        "Нецелевая",
        "Всем привет! Ищу работу в вашей компании, у меня 6 лет опыта в продажах.",
        "нецелевой",
        False,
    ),
    (
        "Цена без деталей",
        "Добрый день, сколько стоит офисный стол?",
        "неясно",
        False,
    ),
]

DESCRIPTION = os.getenv("COMPANY_DESCRIPTION") or "Продаем офисную мебель B2B клиентам."


async def main() -> int:
    model = ai.get_model()
    reasoning = ai.get_reasoning_effort() or "по умолчанию"

    print()
    print("=" * 78)
    print(f"  Модель: {model}   |   reasoning_effort: {reasoning}")
    print("=" * 78)

    total = 0.0
    failures: list[str] = []
    prompt_tokens = completion_tokens = 0

    for name, text, expected_lead, expected_human in CASES:
        start = time.perf_counter()
        try:
            raw = await ai.analyze_lead(text, DESCRIPTION)
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - start
            total += elapsed
            failures.append(f"{name}: ошибка {exc}")
            print(f"  ✗  {name:<20} {elapsed:>6.1f}s  ОШИБКА: {str(exc)[:50]}")
            continue

        elapsed = time.perf_counter() - start
        total += elapsed

        card = apply_guards(normalize_card(raw), text)
        lead = card.get("lead_type")
        human = bool(card.get("needs_human"))

        ok_lead = lead == expected_lead
        ok_human = human == expected_human
        if not (ok_lead and ok_human):
            failures.append(
                f"{name}: ожидали тип {expected_lead}/человек {expected_human}, "
                f"получили {lead}/{human}"
            )

        mark = "✓" if (ok_lead and ok_human) else "✗"
        print(
            f"  {mark}  {name:<20} {elapsed:>6.1f}s  "
            f"тип={lead:<10} человек={str(human):<5}"
        )

    # Сколько токенов ушло — по последней записи журнала
    for record in reversed(ai.logbuf.records()):
        if record.get("step") == "ai.request":
            data = record.get("data") or {}
            prompt_tokens = data.get("prompt_tokens") or 0
            completion_tokens = data.get("completion_tokens") or 0
            break

    print("-" * 78)
    print(
        f"  Заявок: {len(CASES)}   |   общее время: {total:.1f}s   |   "
        f"в среднем: {total / len(CASES):.1f}s на заявку"
    )
    if prompt_tokens:
        print(
            f"  Токены на последний вызов: вход {prompt_tokens}, "
            f"выход {completion_tokens}"
        )
    if failures:
        print()
        print(f"  ПРОВАЛЕНО: {len(failures)}")
        for item in failures:
            print(f"    ✗ {item}")
        return 1

    print()
    print(f"  Все {len(CASES)} эталонных заявок классифицированы верно.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
