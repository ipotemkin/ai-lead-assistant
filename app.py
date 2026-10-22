"""
AI-помощник для первичной обработки заявок.

Запуск:
    python app.py
Затем откройте http://127.0.0.1:3000

Настройки (ключ API, модель, описание компании) читаются из файла .env.
Сам ключ в коде не хранится и в браузер не попадает.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

import logbuf
from ai import AIError, analyze_lead, get_api_key, get_base_url, get_model
from anonymizer import (
    address_form,
    anonymize,
    canonical_name,
    capitalize_names_in_text,
    capitalize_word,
    natural_name_order,
    person_parts,
    restore,
)
from bitrix import (
    BitrixError,
    add_comment_and_notify,
    call as bitrix_call,
    call_batch as bitrix_batch,
    create_lead,
    create_task,
    get_chat_id,
    get_manager_id,
    get_portal_url,
    get_source_id,
    lead_url,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PORT_FILE = BASE_DIR / ".port"

# Порт по умолчанию. 3000 на этой машине занят контейнером Docker,
# поэтому по умолчанию используем 8000.
DEFAULT_PORT = 8000

# Читаем .env один раз при старте. Переменные, заданные в системе, имеют приоритет.
load_dotenv(BASE_DIR / ".env")

NOT_SPECIFIED = "не указано"

# Слова, по которым заведомо нужен живой человек.
CONFLICT_MARKERS: list[tuple[str, str]] = [
    ("жалоб", "жалоба в тексте заявки"),
    ("недоволен", "недовольство в тексте заявки"),
    ("недовольств", "недовольство в тексте заявки"),
    ("претензи", "претензия в тексте заявки"),
    ("вернуть деньги", "требование возврата денег"),
    ("возврат денег", "требование возврата денег"),
    ("вернуть оплату", "требование возврата оплаты"),
    ("возврат товара", "требование возврата товара"),
    ("возврат средств", "требование возврата средств"),
    ("требование возврата", "требование возврата"),
    ("отказаться от заказ", "угроза отказаться от заказа"),
    ("откажемся", "угроза отказаться от сотрудничества"),
    ("откажемся от", "угроза отказаться от сотрудничества"),
    ("уйдем к", "угроза уйти к конкурентам"),
    ("уйдём к", "угроза уйти к конкурентам"),
    ("уйдем от вас", "угроза уйти к конкурентам"),
    ("уйдём от вас", "угроза уйти к конкурентам"),
    ("уйдем к конкурент", "угроза уйти к конкурентам"),
    ("уйдём к конкурент", "угроза уйти к конкурентам"),
    ("подадим в суд", "юридическая угроза"),
    ("в суд", "юридическая угроза"),
    ("юрист", "юридические требования"),
    ("юридическ", "юридические требования"),
    ("компенсац", "требование компенсации"),
    ("недопостав", "недопоставка"),
    ("недопоставк", "недопоставка"),
    ("некачественн", "недовольство качеством"),
    ("брак", "недовольство качеством"),
    ("сорван срок", "сорванные сроки"),
    ("сорвали срок", "сорванные сроки"),
    ("обман", "обвинение в обмане"),
    ("мошенник", "обвинение в мошенничестве"),
    ("отказываемся", "отказ от дальнейшей работы"),
]

# Данные, без которых целевой лид нельзя нормально обработать.
IMPORTANT_FIELDS = ("product", "contact")

KEY_FIELD_ALIASES: dict[str, str] = {
    "company": "company",
    "компания": "company",
    "организация": "company",
    "заказчик": "company",
    "клиент": "company",
    "name": "name",
    "имя": "name",
    "имя клиента": "name",
    "контактное лицо": "name",
    "contact_name": "name",
    "contact person": "name",
    "менеджер": "name",
    "contact": "contact",
    "контакт": "contact",
    "телефон": "contact",
    "email": "contact",
    "e-mail": "contact",
    "почта": "contact",
    "product": "product",
    "продукт": "product",
    "услуга": "product",
    "товар": "product",
    "quantity": "quantity",
    "количество": "quantity",
    "кол-во": "quantity",
    "объем": "quantity",
    "объём": "quantity",
    "qty": "quantity",
    "budget": "budget",
    "бюджет": "budget",
    "бюджетная рамка": "budget",
    "deadline": "deadline",
    "сроки": "deadline",
    "срок": "deadline",
    "дедлайн": "deadline",
    "term": "deadline",
}

EMPTY_VALUES = {
    "",
    "-",
    "--",
    "—",
    "n/a",
    "na",
    "нет данных",
    "нет",
    "нету",
    "unknown",
    "null",
    "none",
    "неизвестно",
    "не указано",
    "не указана",
    "не указан",
    "без понятия",
    "пока не знаю",
    "пока не известно",
}

LEAD_TYPES = ("целевой", "нецелевой", "неясно")

# Сочетания слов: срабатывают, только когда найдены ВСЕ слова из набора.
# Так ловим разные формы: «вернуть деньги», «верните деньги», «вернули деньги».
CONFLICT_COMBOS: list[tuple[tuple[str, ...], str]] = [
    (("верн", "деньг"), "требование возврата денег"),
    (("верн", "оплат"), "требование возврата оплаты"),
    (("верн", "товар"), "требование возврата товара"),
    (("возврат", "деньг"), "требование возврата денег"),
    (("возврат", "средств"), "требование возврата средств"),
    (("возврат", "товар"), "требование возврата товара"),
    (("вернуть", "деньг"), "требование возврата денег"),
]


# ---------------------------------------------------------------------------
# Приведение ответа AI к единому виду
# ---------------------------------------------------------------------------

def _as_text(value: Any) -> str:
    """Любое значение -> строка. Пустое и «мусорное» -> «не указано»."""
    if value is None:
        return NOT_SPECIFIED
    if isinstance(value, bool):
        return NOT_SPECIFIED
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = [_as_text(item) for item in value]
        parts = [part for part in parts if part != NOT_SPECIFIED]
        return ", ".join(parts) if parts else NOT_SPECIFIED
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _as_text(item)
            if text != NOT_SPECIFIED:
                parts.append(f"{key}: {text}")
        return "; ".join(parts) if parts else NOT_SPECIFIED

    text = str(value).strip()
    if text.lower() in EMPTY_VALUES:
        return NOT_SPECIFIED
    return text


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "да", "yes", "y", "1", "нужно", "требуется"}:
        return True
    return False


def _as_list(value: Any) -> list[str]:
    """Строка или список -> список строк без пустых и повторов."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items: list[str] = []
        for line in value.splitlines():
            line = line.strip().lstrip("-•·*").strip()
            if line:
                raw_items.append(line)
        if len(raw_items) == 1:
            single = raw_items[0]
            if ";" in single:
                raw_items = [part.strip() for part in single.split(";") if part.strip()]
            elif ", " in single and ":" not in single:
                raw_items = [part.strip() for part in single.split(",") if part.strip()]
        raw_items = raw_items or ([] if value is None else [value])
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            if isinstance(item, str):
                cleaned = item.strip().lstrip("-•·*").strip()
                if cleaned:
                    raw_items.append(cleaned)
            else:
                text = _as_text(item)
                if text != NOT_SPECIFIED:
                    raw_items.append(text)
    else:
        raw_items = [str(value)]

    result: list[str] = []
    for item in raw_items:
        text = _as_text(item)
        if text == NOT_SPECIFIED:
            continue
        if text not in result:
            result.append(text)
    return result


def _normalize_lead_type(value: Any) -> str:
    text = _as_text(value).lower().replace("ё", "е")
    if "нецелевой" in text or "не целевой" in text or "не-целевой" in text:
        return "нецелевой"
    if "целевой" in text or "целевой" in text:
        return "целевой"
    return "неясно"


def _raw_text(value: Any) -> str:
    """Текст без замены «пустых» значений.

    Нужно для разбора ответов «да»/«нет»: обычный _as_text считает «нет»
    отсутствующим значением, и ответ модели превратился бы в «не указано».
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value).strip().lower().replace("ё", "е")


def _normalize_subject_match(value: Any) -> str | None:
    """Ответ модели на вопрос «относится ли запрос к нашим товарам»."""
    text = _raw_text(value)
    if text in {"да", "yes", "истина", "true", "да, наш", "наш"}:
        return "да"
    if text in {"нет", "no", "ложь", "false"}:
        return "нет"
    if text in {"неясно", "непонятно", "unknown", "частично", "сложно сказать"}:
        return "неясно"
    return None


def _normalize_offer_clarity(value: Any) -> bool | None:
    """Ответ модели на вопрос «понятно ли, что именно предлагать»."""
    text = _raw_text(value)
    if text in {"да", "yes", "истина", "true", "понятно", "определённо", "определенно"}:
        return True
    if text in {"нет", "no", "ложь", "false", "непонятно", "не определено", "не определён"}:
        return False
    return None


def _derive_lead_type(ai_type: str, subject_match: str | None, offer_clarity: bool | None) -> str:
    """
    Определяет тип лида по структурированным ответам модели.

    Модель отвечает на два вопроса по отдельности, а решение принимает сервер —
    так правило квалификации одинаково работает во всех случаях и его легко
    изменить в одном месте.

      предмет не наш              -> нецелевой
      предмет неизвестен          -> неясно
      предмет наш, но непонятно
      что именно предлагать      -> неясно
      предмет наш и всё понятно   -> целевой
    """
    if subject_match is None:
        # Модель не ответила на вспомогательные вопросы — доверяем её общему выводу.
        return ai_type

    if subject_match == "нет":
        return "нецелевой"
    if subject_match == "неясно":
        return "неясно"

    # subject_match == "да"
    if offer_clarity is False:
        return "неясно"
    if offer_clarity is True:
        # Если модель сама сомневается в общем выводе — не даём завысить тип лида.
        return ai_type if ai_type in {"неясно", "нецелевой"} else "целевой"
    return ai_type


def _normalize_key_data(value: Any) -> dict[str, str]:
    """Собирает 7 известных полей, понимая и русские, и английские названия."""
    collected: dict[str, str] = {}

    def put(canonical: str, raw_value: Any) -> None:
        text = _as_text(raw_value)
        if text != NOT_SPECIFIED:
            collected[canonical] = text

    if isinstance(value, dict):
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip().lower().replace("ё", "е")
            canonical = KEY_FIELD_ALIASES.get(key)
            if canonical:
                put(canonical, raw_value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            text = _as_text(item)
            if text == NOT_SPECIFIED or ":" not in text:
                continue
            raw_key, _, raw_value = text.partition(":")
            canonical = KEY_FIELD_ALIASES.get(raw_key.strip().lower().replace("ё", "е"))
            if canonical:
                put(canonical, raw_value)
    elif isinstance(value, str) and ":" in value:
        for chunk in value.split(";"):
            raw_key, _, raw_value = chunk.partition(":")
            canonical = KEY_FIELD_ALIASES.get(raw_key.strip().lower().replace("ё", "е"))
            if canonical:
                put(canonical, raw_value)

    return {
        "company": collected.get("company", NOT_SPECIFIED),
        "name": collected.get("name", NOT_SPECIFIED),
        "contact": collected.get("contact", NOT_SPECIFIED),
        "product": collected.get("product", NOT_SPECIFIED),
        "quantity": collected.get("quantity", NOT_SPECIFIED),
        "budget": collected.get("budget", NOT_SPECIFIED),
        "deadline": collected.get("deadline", NOT_SPECIFIED),
    }


def normalize_card(raw: Any) -> dict[str, Any]:
    """Приводит любой ответ AI к полному набору полей карточки."""
    if not isinstance(raw, dict):
        raw = {}

    needs_human = _as_bool(raw.get("needs_human"))
    reason = _as_text(raw.get("human_reason"))

    # Причина передачи человеку должна быть всегда осмысленной.
    if not needs_human:
        reason = "не требуется"
    elif reason == NOT_SPECIFIED:
        reason = "Требуется решение менеджера"

    # Тип лида выводим по правилу в _derive_lead_type, а не берём как есть.
    ai_lead_type = _normalize_lead_type(raw.get("lead_type"))
    lead_type = _derive_lead_type(
        ai_lead_type,
        _normalize_subject_match(raw.get("subject_match")),
        _normalize_offer_clarity(raw.get("offer_clarity")),
    )

    return {
        "company": _as_text(raw.get("company")),
        "summary": _as_text(raw.get("summary")),
        "lead_type": lead_type,
        "lead_type_reason": _as_text(raw.get("lead_type_reason")),
        "key_data": _normalize_key_data(raw.get("key_data")),
        "missing_data": _as_list(raw.get("missing_data")),
        "next_step": _as_text(raw.get("next_step")),
        "reply_draft": _as_text(raw.get("reply_draft")),
        "needs_human": needs_human,
        "human_reason": reason,
        "clarifying_questions": _as_list(raw.get("clarifying_questions"))[:2],
        # Служебные поля: заполняются на сервере, модель их не возвращает
        "llm_request": raw.get("llm_request") or "",
        "entities": raw.get("entities") or [],
        "crm": raw.get("crm") or {},
        "timing": raw.get("timing") or {},
    }


def restore_card(card: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Подставляет настоящие значения вместо токенов во всех строковых полях карточки."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return restore(value, mapping)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(card)


# Приветствие и имя после него. Имя — до трёх слов: «Иван», «Иван Петрович»,
# «Иван Петрович Нервный» — чтобы заменить ФИО целиком, вместе с фамилией.
_GREETING_RE = re.compile(
    r"\b((?i:здравствуй\w*|добрый\s+день|доброе\s+утро|добрый\s+вечер|привет\w*))\s*,\s*"
    r"([А-ЯЁA-Z][а-яёA-Za-z-]+(?:\s+[А-ЯЁA-Z][а-яёA-Za-z-]+){0,2})"
)


def fix_greeting_name(text: str, raw_name: str) -> str:
    """
    В приветствии обращаемся по имени и отчеству, без фамилии.

    «Здравствуйте, Савельева Мария!» -> «Здравствуйте, Мария!»
    «Здравствуйте, Петр Иванович Нервный!» -> «Здравствуйте, Петр Иванович!»
    """
    if not text or not raw_name:
        return text

    first, patronymic, surname = person_parts(raw_name)
    if not first:
        return text

    # Форма обращения — с большой буквы: «Иван», «Мария Сергеевна».
    first = capitalize_word(first)
    patronymic = capitalize_word(patronymic)
    address = f"{first} {patronymic}".strip()
    # Все разумные порядки ФИО, которые встречаются в приветствии:
    # «Иван», «Иван Петрович», «Иван Петрович Нервный», «Нервный Иван Петрович».
    parts = [part for part in (first, patronymic, surname) if part]
    variants: set[str] = set()
    if parts:
        variants.add(" ".join(parts).lower())
    if first and surname:
        variants.add(" ".join(part for part in (surname, first) if part).lower())
        if patronymic:
            variants.add(" ".join(part for part in (surname, first, patronymic) if part).lower())
    if first and patronymic:
        variants.add(" ".join(part for part in (first, patronymic) if part).lower())
    variants.add(first.lower())
    variants.add(address.lower())
    variants.discard("")

    def replace(match: re.Match[str]) -> str:
        matched = match.group(2).strip().lower()
        if matched in variants:
            return f"{match.group(1)}, {address}"
        return match.group(0)

    return _GREETING_RE.sub(replace, text)


def fix_name_capitalization(card: dict[str, Any]) -> dict[str, Any]:
    """
    Приводит имена к правильному регистру: «иван» -> «Иван».

    Клиент часто пишет имя с маленькой буквы. Ориентиром служит поле
    «Имя» в известных данных — по нему понимаем, как имя должно выглядеть,
    и приводим к этому виду все упоминания в текстах карточки.
    """

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return fix_text(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    key_data = dict(card.get("key_data") or {})
    raw_name = str(key_data.get("name") or "").strip()
    # «Савельева Мария» -> «Мария Савельева»: привычный порядок для карточки
    canonical = canonical_name(natural_name_order(raw_name))

    first, patronymic, surname = person_parts(raw_name)
    if canonical and canonical != raw_name:
        key_data["name"] = canonical

    # Слова имени -> правильная форма. Сопоставление по самому слову, а не по
    # позиции: иначе после перестановки «Савельева Мария» превратилось бы
    # в «Мария Савельева» -> «Мария Савельева» с перепутанными словами.
    name_parts: list[tuple[str, str]] = []
    for raw_part, canonical_part in (
        (first, first),
        (patronymic, patronymic),
        (surname, surname),
    ):
        if not raw_part:
            continue
        target = capitalize_word(canonical_part)
        if target != raw_part:
            name_parts.append((raw_part, target))

    # Полные формы имени в любом порядке -> привычный порядок.
    order_variants: list[str] = []
    if first and surname:
        if patronymic:
            order_variants = [
                f"{first} {patronymic} {surname}",
                f"{surname} {first} {patronymic}",
            ]
        else:
            order_variants = [f"{first} {surname}", f"{surname} {first}"]

    def fix_text(text: str) -> str:
        if not text:
            return text
        # Один проход: иначе замены мешают друг другу.
        if name_parts:
            pattern = re.compile(
                "|".join(
                    r"(?<![\w-])" + re.escape(source) + r"(?![\w-])"
                    for source, _ in name_parts
                ),
                re.IGNORECASE,
            )
            targets = {source.lower(): target for source, target in name_parts}
            text = pattern.sub(lambda m: targets.get(m.group(0).lower(), m.group(0)), text)
        if order_variants and canonical:
            pattern = re.compile(
                "|".join(
                    r"(?<![\w-])" + re.escape(variant) + r"(?![\w-])"
                    for variant in order_variants
                ),
                re.IGNORECASE,
            )
            text = pattern.sub(lambda _: canonical, text)
        return text

    result = walk(card)
    if isinstance(result, dict):
        # В текстах для менеджера дополнительно поднимаем имена с маленькой буквы:
        # там имя может стоять отдельно («Здравствуйте, иван!»).
        for field in ("reply_draft", "next_step"):
            if isinstance(result.get(field), str):
                result[field] = capitalize_names_in_text(result[field])
        # В приветствии обращаемся по имени, а не «Фамилия Имя».
        if isinstance(result.get("reply_draft"), str):
            result["reply_draft"] = fix_greeting_name(result["reply_draft"], raw_name)
        if isinstance(result.get("key_data"), dict):
            result["key_data"]["name"] = canonical or key_data.get("name", NOT_SPECIFIED)
    return result


def fill_contacts_from_form(card: dict[str, Any], payload: "LeadCardRequest") -> dict[str, Any]:
    """
    Подстраховка: если модель не разглядела токены и вернула «не указано»,
    берём контактные данные из формы — там они точные.
    """
    key_data = dict(card.get("key_data") or {})

    if payload.name and key_data.get("name") in (NOT_SPECIFIED, "", None):
        key_data["name"] = payload.name

    contacts = [value for value in (payload.phone, payload.email) if value]
    if contacts and key_data.get("contact") in (NOT_SPECIFIED, "", None):
        key_data["contact"] = ", ".join(contacts)

    card["key_data"] = key_data
    return card


def detect_conflicts(text: str) -> list[str]:
    """Находит в тексте заявки признаки конфликта — их нужно передавать человеку."""
    lowered = (text or "").lower().replace("ё", "е")
    reasons: list[str] = []
    for marker, reason in CONFLICT_MARKERS:
        if marker in lowered and reason not in reasons:
            reasons.append(reason)
    for words, reason in CONFLICT_COMBOS:
        if all(word in lowered for word in words) and reason not in reasons:
            reasons.append(reason)
    return reasons


def apply_guards(card: dict[str, Any], text: str) -> dict[str, Any]:
    """
    Страховка на сервере: правила из ТЗ проверяются кодом, а не только промптом.
    """
    card = dict(card)
    key_data = dict(card.get("key_data") or {})
    missing = list(card.get("missing_data") or [])
    reasons = detect_conflicts(text)

    # 1. Конфликтные формулировки -> человек, даже если AI ошибся.
    if reasons:
        already_flagged = card.get("needs_human") and card.get("human_reason") not in (
            "",
            NOT_SPECIFIED,
            "не требуется",
        )
        if not already_flagged:
            # AI не заметил конфликт — описываем причину сами.
            card["needs_human"] = True
            card["human_reason"] = "Требуется участие человека: " + "; ".join(reasons)

    # 2. Целевой лид, у которого нет вообще никаких данных -> «неясно».
    if card.get("lead_type") == "целевой":
        meaningful = [
            key for key in ("product", "quantity", "budget", "deadline", "contact", "company")
            if key_data.get(key, NOT_SPECIFIED) != NOT_SPECIFIED
        ]
        if not meaningful:
            card["lead_type"] = "неясно"
            for field in ("company", "name", "contact", "product", "quantity", "budget", "deadline"):
                item = f"не указано: {field}"
                if item not in missing:
                    missing.append(item)

    # 3. Целевой лид без важных данных -> добавляем их в missing_data.
    if card.get("lead_type") == "целевой":
        for field in IMPORTANT_FIELDS:
            if key_data.get(field, NOT_SPECIFIED) == NOT_SPECIFIED:
                label = "продукт" if field == "product" else "контакт"
                if label not in missing:
                    missing.append(label)

    # 4. Нецелевой лид: уточнять нечего — заявка не про наш продукт.
    if card.get("lead_type") == "нецелевой":
        missing = []
        card["clarifying_questions"] = []

    # 5. Неясный лид: обязательно понятно, чего не хватает и что спросить.
    if card.get("lead_type") == "неясно":
        if not missing:
            missing = ["предмет покупки", "контакт"]
        if not card.get("clarifying_questions"):
            card["clarifying_questions"] = [
                "Уточните, пожалуйста, какой товар или услуга вас интересует и как с вами связаться."
            ]

    # 6. Человек не нужен -> причина не требуется.
    if not card.get("needs_human"):
        card["human_reason"] = "не требуется"
    elif card.get("human_reason", NOT_SPECIFIED) == NOT_SPECIFIED:
        card["human_reason"] = "Требуется решение менеджера"

    card["missing_data"] = missing
    card["key_data"] = key_data
    return card


# ---------------------------------------------------------------------------
# Модели API
# ---------------------------------------------------------------------------

class LeadCardRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000, description="Текст заявки")
    name: str = Field(default="", max_length=200, description="Имя контактного лица")
    phone: str = Field(default="", max_length=100, description="Телефон")
    email: str = Field(default="", max_length=200, description="Электронная почта")
    company_description: str = Field(
        default="", max_length=5000, description="Описание продуктов компании"
    )
    send_to_crm: bool = Field(default=True, description="Отправить карточку в Битрикс24")

    @field_validator("text")
    @classmethod
    def _text_must_have_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Текст заявки не может быть пустым")
        return cleaned

    @field_validator("name", "phone", "email", "company_description")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()


def compose_request_text(text: str, name: str, phone: str, email: str) -> str:
    """
    Собирает из полей формы понятный для AI текст заявки.

    Контактные данные подставляются сверху отдельными строками, чтобы AI
    надёжно забирал их в key_data.name и key_data.contact.
    """
    header: list[str] = []
    if name:
        header.append(f"Имя: {name}")
    if phone:
        header.append(f"Телефон: {phone}")
    if email:
        header.append(f"Почта: {email}")

    body = text.strip()
    if not header:
        return body
    return "\n".join(header) + "\n\n" + body


class KeyDataModel(BaseModel):
    company: str = NOT_SPECIFIED
    name: str = NOT_SPECIFIED
    contact: str = NOT_SPECIFIED
    product: str = NOT_SPECIFIED
    quantity: str = NOT_SPECIFIED
    budget: str = NOT_SPECIFIED
    deadline: str = NOT_SPECIFIED


class EntityFinding(BaseModel):
    """Найденный фрагмент персональных данных — без самого значения."""

    token: str
    type: str


class CrmResult(BaseModel):
    """Что получилось при отправке карточки в Битрикс24."""

    sent: bool = False
    lead_id: int | None = None
    lead_url: str = ""
    comment_id: int | None = None
    task_id: str = ""
    task_kind: str = ""
    message_id: int | None = None
    errors: list[str] = Field(default_factory=list)


class Timing(BaseModel):
    """Сколько времени заняла обработка, в секундах."""

    ai: float = 0.0
    crm: float = 0.0
    total: float = 0.0


class LeadCardResponse(BaseModel):
    company: str
    summary: str
    lead_type: Literal["целевой", "нецелевой", "неясно"]
    lead_type_reason: str
    key_data: KeyDataModel
    missing_data: list[str]
    next_step: str
    reply_draft: str
    needs_human: bool
    human_reason: str
    clarifying_questions: list[str]
    llm_request: str = ""
    entities: list[EntityFinding] = []
    crm: CrmResult = Field(default_factory=CrmResult)
    timing: Timing = Field(default_factory=Timing)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, raw: Any) -> Any:
        return normalize_card(raw)


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI-помощник по обработке заявок",
    description="Принимает текст заявки и возвращает структурированную карточку лида.",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    """Проверка готовности: видно, есть ли ключ и какая модель выбрана."""
    return {
        "status": "ok",
        "api_key_present": bool(get_api_key()),
        "model": get_model(),
        "base_url": get_base_url(),
        "company_description_present": bool(
            (os.getenv("COMPANY_DESCRIPTION") or "").strip()
        ),
    }


class PreviewRequest(BaseModel):
    """Поля формы для предпросмотра обезличивания (без обработки AI)."""

    text: str = Field(default="", max_length=10000)
    name: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=200)


def _dialog_id(item: dict[str, Any]) -> str:
    """Идентификатор чата для im.message.add.

    Для группового чата — «chat<id>», для личной переписки — ID пользователя.
    """
    if str(item.get("type") or "").lower() == "chat":
        return f"chat{item.get('chat_id')}"
    return str(item.get("id") or "")


async def push_to_crm(card: dict[str, Any]) -> dict[str, Any]:
    """
    Отправляет карточку в Битрикс24: лид, комментарий, задача, уведомление.

    Каждый шаг выполняется независимо: если задача не создалась, лид и комментарий
    уже в CRM, а проблема попадает в errors. Карточка при этом не теряется.
    """
    result: dict[str, Any] = {
        "sent": False,
        "lead_id": None,
        "lead_url": "",
        "comment_id": None,
        "task_id": "",
        "task_kind": "",
        "message_id": None,
        "errors": [],
    }

    try:
        lead_id = await create_lead(card)
    except BitrixError as exc:
        result["errors"].append(f"Лид не создан: {exc.message}")
        return result
    except Exception as exc:  # noqa: BLE001 — CRM не должен ломать обработку
        result["errors"].append(f"Лид не создан: {exc}")
        return result

    result["sent"] = True
    result["lead_id"] = lead_id
    result["lead_url"] = lead_url(lead_id)

    # Комментарий с уведомлением уходят одним пакетом, задача создаётся
    # параллельно — ждать по очереди не нужно.
    async def make_task() -> tuple[str, str, str | None]:
        try:
            task = await create_task(lead_id, card)
            return ("task", task.get("id") or "", task.get("kind") or "task")
        except BitrixError as exc:
            return ("error", "", exc.message)

    async def make_comment_and_notify() -> tuple[int | None, int | None, list[str]]:
        try:
            return await add_comment_and_notify(lead_id, card)
        except BitrixError as exc:
            return (None, None, [f"Комментарий и уведомление не отправлены: {exc.message}"])

    (task_kind, task_id, task_error), (comment_id, message_id, problems) = await asyncio.gather(
        make_task(), make_comment_and_notify()
    )

    result["comment_id"] = comment_id
    result["message_id"] = message_id
    result["task_id"] = task_id
    result["task_kind"] = task_kind if task_kind == "task" else ""

    if task_kind == "error":
        result["errors"].append(f"Задача не создана: {task_error}")
    elif task_kind == "activity":
        result["errors"].append(
            "У вебхука нет прав на раздел «Задачи» — вместо задачи создано дело «Звонок» в таймлайне лида."
        )

    result["errors"].extend(problems)
    return result


@app.post("/api/preview")
async def preview(payload: PreviewRequest) -> dict[str, Any]:
    """Показывает текст, который уйдёт в LLM. К AI не обращается."""
    request_text = compose_request_text(
        payload.text.strip(), payload.name.strip(), payload.phone.strip(), payload.email.strip()
    )
    result = anonymize(
        request_text, known_values=[payload.name, payload.phone, payload.email]
    )
    return {
        "llm_request": result.text,
        "entities": result.findings,
        "summary": result.summary,
    }


@app.post("/api/crm/send")
async def crm_send(payload: dict[str, Any]) -> dict[str, Any]:
    """Отправляет уже готовую карточку в CRM — без повторного вызова AI."""
    card = normalize_card(payload)
    return await push_to_crm(card)


@app.get("/api/b24/check")
async def b24_check() -> dict[str, Any]:
    """Диагностика настройки Битрикс24: что работает, что нет и что подправить.

    Отчёт только читает — ничего не создаёт. Ни одна отдельная проверка не может
    уронить весь эндпоинт: сбой превращается в строку отчёта.
    """
    report: dict[str, Any] = {
        "configured": bool(get_base_url()),
        "portal": get_portal_url(),
        "manager_id": get_manager_id(),
        "chat_id": get_chat_id(),
        "source_id": get_source_id(),
        "checks": [],
        "lead_stages": [],
        "sources": [],
        "chats": [],
    }

    def add_check(name: str, ok: bool, level: str, detail: str) -> None:
        report["checks"].append(
            {"name": name, "ok": ok, "level": level, "detail": detail}
        )

    async def probe(name: str, factory, detail_builder, level: str = "fail"):
        """Выполняет проверку. Любая ошибка становится строкой отчёта, а не 500."""
        try:
            result = await factory()
        except BitrixError as exc:
            add_check(name, False, level, exc.message)
            return None
        except Exception as exc:  # noqa: BLE001 — отчёт не должен падать
            add_check(name, False, "fail", f"ошибка проверки: {type(exc).__name__}: {exc}")
            return None
        detail = detail_builder(result) if detail_builder else ""
        add_check(name, True, "ok", detail)
        return result

    if not report["configured"]:
        add_check("Адрес вебхука", False, "fail", "Не задан B24_URL в файле .env")
        return report

    # Все проверки — одним пакетом: пять обращений к порталу занимают около
    # двух секунд, один пакет — около четырёхсот миллисекунд.
    async def batch_probe() -> dict[str, Any]:
        results, errors = await bitrix_batch(
            {
                "stages": "crm.status.list?filter[ENTITY_ID]=STATUS",
                "sources": "crm.status.list?filter[ENTITY_ID]=SOURCE",
                "fields": "tasks.task.getFields",
                "user": f"user.get?ID={get_manager_id() or 1}",
                "chats": "im.recent.list?LIMIT=10&SKIP_OPENLINES=Y&UNREAD_ONLY=N&PARSE_TEXT=N",
            }
        )
        return {"results": results, "errors": errors}

    try:
        probe_data = await batch_probe()
    except BitrixError as exc:
        add_check("Портал Битрикс24", False, "fail", exc.message)
        return report
    except Exception as exc:  # noqa: BLE001
        add_check(
            "Портал Битрикс24", False, "fail", f"ошибка проверки: {type(exc).__name__}: {exc}"
        )
        return report

    batch_results = probe_data.get("results") or {}
    batch_errors = probe_data.get("errors") or {}

    def batch_ok(name: str) -> bool:
        return name in batch_results and name not in batch_errors

    stages = batch_results.get("stages") or []
    report["lead_stages"] = [
        {"id": item.get("STATUS_ID"), "name": item.get("NAME")} for item in stages
    ]
    add_check(
        "Вебхук и доступ к CRM",
        batch_ok("stages"),
        "ok" if batch_ok("stages") else "fail",
        f"доступно стадий лида: {len(stages)}",
    )
    if batch_errors:
        add_check(
            "Ошибки проверки",
            False,
            "warn",
            "; ".join(f"{name}: {error.get('error_description') or error.get('error')}"
                      for name, error in batch_errors.items()),
        )

    sources = batch_results.get("sources") or []
    report["sources"] = [
        {"id": item.get("STATUS_ID"), "name": item.get("NAME")} for item in sources
    ]
    add_check(
        "Источники лидов",
        batch_ok("sources"),
        "ok" if batch_ok("sources") else "fail",
        f"доступно источников: {len(sources)}",
    )

    add_check(
        "Права на задачи",
        batch_ok("fields"),
        "ok" if batch_ok("fields") else "fail",
        (
            "раздел «Задачи» доступен, задачи будут создаваться"
            if batch_ok("fields")
            else "у вебхука нет прав на задачи — нужен скоуп «Задачи (task)», "
            "а не устаревший «Задачи (tasks)». Лид, комментарий и дело "
            "«Звонок» в таймлайне при этом создаются."
        ),
    )

    # 4. Ответственный
    manager_id = get_manager_id()
    if not manager_id:
        add_check(
            "Ответственный",
            False,
            "fail",
            "Не удалось определить ID: задайте B24_MANAGER_ID в файле .env",
        )
    else:
        user = batch_results.get("user")
        if isinstance(user, list):
            user = user[0] if user else {}
        if isinstance(user, dict) and user:
            full_name = " ".join(
                part
                for part in [user.get("NAME"), user.get("LAST_NAME")]
                if isinstance(part, str) and part
            ).strip()
            add_check(
                "Ответственный",
                True,
                "ok",
                f"ID {manager_id} — {full_name or 'имя в профиле не заполнено'}",
            )
        else:
            add_check(
                "Ответственный",
                True,
                "warn",
                f"ID {manager_id} — у вебхука нет прав на просмотр пользователей. "
                "Созданию задач это не мешает.",
            )

    # 5. Чаты для уведомлений
    chats = batch_results.get("chats")
    items = chats.get("items", []) if isinstance(chats, dict) else []
    report["chats"] = [
        {
            "id": str(item.get("chat_id") or item.get("id") or ""),
            "dialog_id": _dialog_id(item),
            "title": item.get("title") or "",
            "type": item.get("type") or "",
        }
        for item in items
    ]
    add_check(
        "Список чатов",
        bool(items),
        "ok" if items else "warn",
        f"доступно чатов: {len(items)}",
    )

    configured_chat = get_chat_id()
    if configured_chat:
        found = any(chat["dialog_id"] == configured_chat for chat in report["chats"])
        add_check(
            "Чат для уведомлений",
            found,
            "ok" if found else "fail",
            f"B24_CHAT_ID = {configured_chat}"
            if found
            else f"чат {configured_chat} не найден в списке чатов",
        )
    else:
        add_check(
            "Чат для уведомлений",
            False,
            "warn",
            "Не задан B24_CHAT_ID: уведомления отключены, выберите чат из списка",
        )

    return report


@app.get("/api/log")
def get_log(limit: int = 200) -> dict[str, Any]:
    """Журнал операций приложения: что, сколько времени и с каким результатом.

    Персональных данных в журнале нет — только длины, количества и время.
    """
    items = logbuf.records(limit=limit)
    return {
        "count": len(items),
        "records": items,
    }


@app.delete("/api/log")
def clear_log() -> dict[str, Any]:
    """Очищает журнал операций."""
    logbuf.clear()
    return {"status": "ok"}


@app.post("/api/analyze", response_model=LeadCardResponse)
async def analyze(payload: LeadCardRequest) -> Any:
    """Главный сценарий: поля формы -> карточка лида -> Битрикс24."""
    request_id = logbuf.new_request_id()
    logbuf.set_request_id(request_id)
    total_timer = logbuf.Timer()

    description = payload.company_description.strip() or (
        os.getenv("COMPANY_DESCRIPTION") or ""
    ).strip()

    request_text = compose_request_text(
        payload.text, payload.name, payload.phone, payload.email
    )

    logbuf.log(
        "request.received",
        "Заявка принята",
        text_len=len(request_text),
        has_name=bool(payload.name),
        has_phone=bool(payload.phone),
        has_email=bool(payload.email),
        send_to_crm=payload.send_to_crm,
    )

    # Персональные данные заменяются токенами. Соответствие живёт только
    # в памяти этого запроса и исчезает после формирования ответа.
    timer = logbuf.Timer()
    anonymized = anonymize(
        request_text, known_values=[payload.name, payload.phone, payload.email]
    )
    logbuf.log(
        "anonymize",
        "Персональные данные обезличены",
        duration_ms=timer.ms,
        entities=len(anonymized.findings),
        text_len=len(anonymized.text),
    )

    started = time.perf_counter()
    raw = await analyze_lead(anonymized.text, description)
    ai_seconds = time.perf_counter() - started

    timer = logbuf.Timer()
    card = normalize_card(raw)
    logbuf.log(
        "card.normalize",
        "Ответ приведён к единому виду",
        duration_ms=timer.ms,
        lead_type=card.get("lead_type"),
    )

    timer = logbuf.Timer()
    card = apply_guards(card, request_text)
    logbuf.log(
        "card.guards",
        "Проверены правила квалификации",
        duration_ms=timer.ms,
        lead_type=card.get("lead_type"),
        needs_human=card.get("needs_human"),
        missing=len(card.get("missing_data") or []),
    )

    timer = logbuf.Timer()
    card = restore_card(card, anonymized.mapping)
    logbuf.log(
        "card.restore",
        "Возвращены настоящие значения",
        duration_ms=timer.ms,
        tokens=len(anonymized.mapping),
    )

    timer = logbuf.Timer()
    card = fill_contacts_from_form(card, payload)
    card = fix_name_capitalization(card)
    logbuf.log(
        "card.fields",
        "Уточнены контакты и имена",
        duration_ms=timer.ms,
        # само имя в журнал не пишем — только факт правки
        name_normalized=bool(card.get("key_data", {}).get("name")),
    )

    # Текст, который ушёл в модель, показываем обезличенным
    card["llm_request"] = anonymized.text
    card["entities"] = anonymized.findings

    if payload.send_to_crm:
        card["crm"] = await push_to_crm(card)
    else:
        card["crm"] = {
            "sent": False,
            "lead_id": None,
            "lead_url": "",
            "comment_id": None,
            "task_id": "",
            "task_kind": "",
            "message_id": None,
            "errors": [],
        }
        logbuf.log("crm.skipped", "Отправка в CRM отключена", duration_ms=0.0)

    crm_seconds = time.perf_counter() - started - ai_seconds
    card["timing"] = {
        "ai": round(ai_seconds, 2),
        "crm": round(crm_seconds, 2),
        "total": round(time.perf_counter() - started, 2),
    }

    logbuf.log(
        "request.done",
        "Обработка завершена",
        duration_ms=total_timer.ms,
        ai_s=card["timing"]["ai"],
        crm_s=card["timing"]["crm"],
        lead_type=card.get("lead_type"),
        crm_sent=bool((card.get("crm") or {}).get("sent")),
    )
    return card


@app.exception_handler(AIError)
async def ai_error_handler(_: Request, exc: AIError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    message = "Проверьте форму: текст заявки обязателен."
    for error in exc.errors():
        error_type = str(error.get("type") or "")
        if "too_long" in error_type:
            message = "Текст слишком длинный. Максимум 10 000 символов."
            break
        if error_type.startswith("missing"):
            message = "Проверьте форму: текст заявки обязателен."
    return JSONResponse(status_code=400, content={"error": message})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Логируем запросы к несуществующим адресам — так видно, кто стучится."""
    if exc.status_code == 404:
        # /v1/* — это автоопределение локальных OpenAI-совместимых серверов
        # (opencode, расширения браузера). Такие пробы шумят в логе, пропускаем.
        if not request.url.path.startswith("/v1/"):
            agent = request.headers.get("user-agent") or "не указан"
            print(
                f"[404] Неизвестный адрес: {request.method} {request.url.path} "
                f"(User-Agent: {agent})",
                file=sys.stderr,
                flush=True,
            )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "Адрес не найден. Приложение доступно на /, /api/analyze и /health."},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    print(f"[ОШИБКА] {type(exc).__name__}: {exc}", file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"error": "Внутренняя ошибка приложения. Подробности — в окне терминала."},
    )


def _can_bind(family: int, address: str, port: int) -> bool:
    """Можно ли занять этот адрес и порт (True — можно)."""
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
        return True
    except OSError:
        return False


def _ipv6_port_is_free(port: int) -> bool:
    """Свободен ли порт на IPv6.

    Пробуем занять «::» в режиме v6only: так обнаруживается любой IPv6-слушатель
    на этом порту, включая wildcard вида [::]:3000, который публикуют контейнеры
    Docker. Такой слушатель перехватывает обращения к «localhost» раньше нас.
    """
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(("::", port))
        return True
    except OSError:
        return False


def _port_in_use(port: int) -> bool:
    """Занят ли порт кем-либо: проверяем IPv4 и IPv6.

    Иначе может повториться неприятная ситуация: программа (например, контейнер
    Docker) слушает [::]:3000, браузер открывает «localhost:3000», попадает
    туда — и показывает чужое приложение вместо нашего.
    """
    if not _can_bind(socket.AF_INET, "127.0.0.1", port):
        return True
    return not _ipv6_port_is_free(port)


def _find_free_port(preferred: int, attempts: int = 20) -> int:
    """Возвращает preferred, если он свободен, иначе — следующий свободный порт."""
    for offset in range(attempts):
        candidate = preferred + offset
        if 1024 < candidate < 65536 and not _port_in_use(candidate):
            return candidate
    return preferred


def main() -> None:
    try:
        wanted_port = int(os.getenv("PORT") or DEFAULT_PORT)
    except ValueError:
        wanted_port = DEFAULT_PORT

    port = wanted_port
    if _port_in_use(wanted_port):
        port = _find_free_port(wanted_port)
        print(
            f"\nПорт {wanted_port} занят другой программой "
            f"(это может быть контейнер Docker или другое приложение).\n"
            f"Приложение запущено на свободном порту {port}.\n"
            f"Чтобы вернуть порт {wanted_port}, остановите программу, которая его\n"
            f"занимает, или укажите другой порт в файле .env (PORT={port}).\n",
            flush=True,
        )

    if not get_api_key():
        print(
            "\nВнимание: не найден PROXY_API_KEY.\n"
            "Откройте файл .env рядом с app.py и впишите ключ в строку PROXY_API_KEY.\n"
            "Приложение запустится, но обработка заявки будет возвращать подсказку об ошибке.\n",
            flush=True,
        )

    # Записываем реальный порт, чтобы скрипт запуска открыл нужный адрес в браузере.
    try:
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass

    print(f"\n  Приложение запущено: http://127.0.0.1:{port}")
    print(f"  Модель: {get_model()}")
    print("  Остановить: Ctrl+C\n", flush=True)

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
