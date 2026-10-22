"""
Интеграция с Битрикс24 CRM через входящий вебхук.

Модуль ничего не знает про FastAPI и AI — только про вебхук. Это делает его
простым для проверки: достаточно подставить свой сервер вместо Битрикс24.

Актуальная документация: https://apidocs.bitrix24.ru/
Методы:
    crm.item.add                 — создать лид (актуальный; crm.lead.add объявлен устаревшим)
    crm.timeline.comment.add     — комментарий в таймлайн лида
    tasks.task.add               — задача менеджеру
    im.message.add               — уведомление в чат
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

import logbuf
from anonymizer import is_known_first_name, is_patronymic

# Таймауты и повторы
REQUEST_TIMEOUT = 30.0
CONNECT_TIMEOUT = 10.0
MAX_ATTEMPTS = 3

# Тип объекта CRM: 1 — лид
ENTITY_TYPE_LEAD = 1

# Стадия лида в зависимости от типа
STAGE_BY_LEAD_TYPE = {
    "целевой": "NEW",
    "неясно": "NEW",
    "нецелевой": "JUNK",
}

# Подписи полей известных данных — используются в тексте комментария
KEY_LABELS: dict[str, str] = {
    "company": "Компания",
    "name": "Имя",
    "contact": "Контакт",
    "product": "Продукт",
    "quantity": "Количество",
    "budget": "Бюджет",
    "deadline": "Сроки",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

LEAD_TYPE_TITLE = {
    "целевой": "Целевой лид",
    "неясно": "Лид требует уточнения",
    "нецелевой": "Нецелевой лид",
}


class BitrixError(Exception):
    """Ошибка обращения к Битрикс24 с понятным для пользователя текстом."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def get_base_url() -> str:
    """Адрес вебхука с завершающим слэшем: https://portal/rest/1/token/"""
    raw = (os.getenv("B24_URL") or "").strip()
    if not raw:
        return ""
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw.rstrip("/") + "/"


def get_portal_url() -> str:
    """Адрес портала без /rest/ — нужен для ссылок на карточку лида."""
    raw = (os.getenv("B24_URL") or "").strip()
    if not raw:
        return ""
    raw = raw.split("/rest/")[0]
    return raw.rstrip("/")


def get_manager_id() -> str:
    """ID ответственного. Если не задан — берём ID владельца вебхука из адреса."""
    configured = (os.getenv("B24_MANAGER_ID") or "").strip()
    if configured:
        return configured
    # Адрес вебхука имеет вид .../rest/<user_id>/<token>/
    match = re.search(r"/rest/(\d+)/", get_base_url())
    return match.group(1) if match else ""


def get_chat_id() -> str:
    return (os.getenv("B24_CHAT_ID") or "").strip()


def get_source_id() -> str:
    return (os.getenv("B24_SOURCE_ID") or "WEB").strip()


def get_currency_id() -> str:
    return (os.getenv("B24_CURRENCY_ID") or "RUB").strip()


def get_task_days() -> int:
    try:
        return max(1, int(os.getenv("B24_TASK_DAYS") or 1))
    except ValueError:
        return 1


def lead_url(lead_id: int | str) -> str:
    portal = get_portal_url()
    if not portal:
        return ""
    return f"{portal}/crm/lead/details/{lead_id}/"


# ---------------------------------------------------------------------------
# Разбор данных карточки
# ---------------------------------------------------------------------------

def split_person_name(full: str) -> tuple[str, str, str]:
    """Делит «Иван Петров» на (имя, отчество, фамилию).

    Учитывает оба порядка: «Иван Петров» и «Петров Иван».
    """
    parts = [part for part in re.split(r"\s+", (full or "").strip()) if part]
    if not parts:
        return ("", "", "")
    if len(parts) == 1:
        return (parts[0], "", "")

    first, second = parts[0], parts[1]
    if is_known_first_name(first):
        name, rest = first, parts[1:]
    elif is_known_first_name(second):
        name, rest = second, [first] + parts[2:]
    else:
        # Разобрать не удалось — считаем первую часть именем
        return (first, "", " ".join(parts[1:]))

    second_name = ""
    last_name = ""
    for token in rest:
        if not second_name and is_patronymic(token):
            second_name = token
        elif not last_name:
            last_name = token
        else:
            last_name = f"{last_name} {token}"
    return (name, second_name, last_name)


def split_contact(contact: str) -> tuple[str, str]:
    """Делит «+7 900 123-45-67, ivan@company.ru» на (телефон, почта)."""
    if not contact or contact == "не указано":
        return ("", "")
    emails = _EMAIL_RE.findall(contact)
    email = emails[0] if emails else ""
    phone = _EMAIL_RE.sub("", contact).strip(" ,;·")
    return (phone, email)


def parse_money(text: str) -> float | None:
    """Разбирает «500 000 рублей» -> 500000, «1,5 млн» -> 1500000.

    Если в строке несколько чисел (вилка диапазона) — возвращает None:
    выбирать одну из границ значило бы выдумать данные.
    """
    if not text or text == "не указано":
        return None

    groups = re.findall(r"\d[\d\s\u00a0]*(?:[.,]\d{1,3})?", text)
    groups = [group for group in groups if re.search(r"\d", group)]
    if not groups:
        return None
    if len(groups) > 1:
        return None  # диапазон — не выбираем

    raw = groups[0].replace("\u00a0", " ").strip()
    if re.search(r"[.,]\d{1,2}$", raw):
        number = float(re.sub(r"[^\d.,]", "", raw).replace(",", "."))
    else:
        digits = re.sub(r"\D", "", raw)
        number = float(digits) if digits else None
    if number is None:
        return None

    lowered = text.lower()
    if re.search(r"млн|миллион", lowered):
        number *= 1_000_000
    elif re.search(r"тыс|тысяч", lowered):
        number *= 1_000
    return number


def lead_title(card: dict[str, Any]) -> str:
    """Заголовок карточки лида: «ООО ОфисЛайн — офисные столы»."""
    key_data = card.get("key_data") or {}
    company = str(key_data.get("company") or "").strip()
    product = str(key_data.get("product") or "").strip()
    name = str(key_data.get("name") or "").strip()

    if company and company != "не указано" and product and product != "не указано":
        return f"{company} — {product}"[:100]
    for candidate in (company, product, name):
        if candidate and candidate != "не указано":
            return candidate[:100]
    return "Заявка с сайта"


def card_to_text(card: dict[str, Any]) -> str:
    """Карточка лида обычным текстом — для комментария и описания задачи."""
    key_data = card.get("key_data") or {}
    lines: list[str] = []

    lines.append(f"Тип лида: {card.get('lead_type', 'неясно')}")
    reason = str(card.get("lead_type_reason") or "").strip()
    if reason and reason != "не указано":
        lines.append(f"Почему: {reason}")
    lines.append(f"Компания: {card.get('company', 'не указано')}")
    lines.append(f"Суть запроса: {card.get('summary', 'не указано')}")

    lines.append("Известные данные:")
    for key, label in KEY_LABELS.items():
        lines.append(f"  {label}: {key_data.get(key, 'не указано')}")

    missing = card.get("missing_data") or []
    if missing:
        lines.append("Чего не хватает: " + ", ".join(missing))

    questions = card.get("clarifying_questions") or []
    if questions:
        lines.append("Уточняющие вопросы: " + "; ".join(questions))

    lines.append(f"Следующий шаг: {card.get('next_step', 'не указано')}")

    if card.get("needs_human"):
        lines.append("Нужен человек: да")
        lines.append(f"Причина: {card.get('human_reason', 'не указано')}")

    lines.append("Черновик ответа:")
    lines.append(str(card.get("reply_draft") or "не указано"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Обращение к API
# ---------------------------------------------------------------------------

def _friendly_error(code: str, description: str) -> str:
    """Переводит код ошибки Битрикс24 в понятный текст."""
    text = (description or "").strip()
    lowered = text.lower()

    if "invalid_credentials" in code.lower() or "wrong authorization" in lowered:
        return "Ключ вебхука неверный или истёк. Проверьте B24_URL в файле .env."
    if "insufficient_scope" in code.lower():
        return (
            "Вебхуку не хватает прав. Откройте его в Битрикс24 "
            "(CRM → Настройки → Разработчикам → Другое → Входящий вебхук) "
            "и включите разделы CRM, Задачи и Чаты, затем сохраните."
        )
    if "access denied" in lowered:
        return "Недостаточно прав. Проверьте права вебхука в Битрикс24."
    if "commercial plans" in lowered or "only by subscription" in lowered:
        return "REST API доступен только на коммерческих тарифах Битрикс24."
    if "method_not_found" in code.lower():
        return "Метод недоступен с текущими правами вебхука."
    if "query_limit_exceeded" in code.lower():
        return "Слишком много запросов к Битрикс24. Попробуйте через минуту."
    if "operation_time_limit" in code.lower():
        return "Битрикс24 временно перегружен. Повторите позже."
    if "not found" in lowered:
        return "Запрошенный элемент в Битрикс24 не найден."
    if "not specified" in lowered or "not found" in lowered:
        return "Битрикс24 не принял запрос."
    if text:
        return text[:300]
    return "Неизвестная ошибка Битрикс24" + (f" ({code})" if code else "")


def _summarize_result(result: Any) -> str:
    """Короткое представление результата для журнала — без лишних данных."""
    if result is None:
        return "нет"
    if isinstance(result, (int, str)):
        return str(result)
    if isinstance(result, list):
        return f"список[{len(result)}]"
    if isinstance(result, dict):
        for key in ("item", "task"):
            nested = result.get(key)
            if isinstance(nested, dict) and nested.get("id") is not None:
                return f"{key}#{nested['id']}"
        if result.get("id") is not None:
            return f"#{result['id']}"
        return "объект"
    return type(result).__name__


def _extract_id(result: Any, *paths: str) -> str:
    """Достаёт идентификатор из ответа метода.

    Формат ответа зависит от метода: crm.item.add возвращает
    {"result": {"item": {"id": 342}}}, crm.timeline.comment.add — {"result": 999},
    tasks.task.add — {"result": {"task": {"id": "888"}}}.
    """
    if result is None:
        return ""
    if isinstance(result, (int, str)):
        return str(result)
    if isinstance(result, dict):
        for path in paths:
            node: Any = result
            for key in path.split("."):
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(key)
            if node is not None and not isinstance(node, (dict, list)):
                return str(node)
        for key in ("id", "ID"):
            if key in result:
                return str(result[key])
    return ""


def batch_command(method: str, params: dict[str, Any] | None = None) -> str:
    """
    Собирает подзапрос для batch: «метод?параметр=значение».

    Вложенные объекты кодируются как fields[ENTITY_ID]=... — именно так их ждёт
    Битрикс24, когда параметры передаются строкой, а не телом запроса.
    """
    if not params:
        return method

    parts: list[str] = []
    for key, value in params.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                parts.append(f"{key}[{sub_key}]={quote(str(sub_value), safe='')}")
        else:
            parts.append(f"{key}={quote(str(value), safe='')}")
    return f"{method}?{'&'.join(parts)}"


async def call_batch(commands: dict[str, str]) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """
    Выполняет несколько методов Битрикс24 за один HTTP-запрос.

    commands — словарь {имя: "метод?параметры"}.
    Возвращает (результаты, ошибки): ошибка одного подзапроса не мешает остальным.

    Экономит время: четыре отдельных обращения к порталу занимают около двух секунд,
    один пакет — около четырёхсот миллисекунд.
    """
    base = get_base_url()
    if not base:
        raise BitrixError("Битрикс24 не настроен: не задан B24_URL в файле .env.")

    timer = logbuf.Timer()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
    ) as client:
        try:
            response = await client.post(
                f"{base}batch.json",
                json={"halt": 0, "cmd": commands},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.TimeoutException:
            logbuf.log(
                "b24.error",
                f"Битрикс24 не ответил за {int(REQUEST_TIMEOUT)} секунд.",
                duration_ms=timer.ms,
                level="error",
                method="batch",
            )
            raise BitrixError(
                f"Битрикс24 не ответил за {int(REQUEST_TIMEOUT)} секунд."
            ) from None
        except httpx.HTTPError:
            logbuf.log(
                "b24.error",
                "Нет связи с Битрикс24",
                duration_ms=timer.ms,
                level="error",
                method="batch",
            )
            raise BitrixError("Нет связи с Битрикс24. Проверьте интернет.") from None

    try:
        data = response.json()
    except ValueError:
        raise BitrixError(
            f"Битрикс24 вернул неожиданный ответ (HTTP {response.status_code})."
        ) from None

    if isinstance(data, dict) and "error" in data:
        code = str(data.get("error") or "").strip()
        description = str(data.get("error_description") or "").strip()
        logbuf.log(
            "b24.error",
            _friendly_error(code, description),
            duration_ms=timer.ms,
            level="error",
            method="batch",
        )
        raise BitrixError(_friendly_error(code, description), code=code)

    payload = (data.get("result") or {}) if isinstance(data, dict) else {}
    results = payload.get("result") or {}
    errors = payload.get("result_error") or {}

    logbuf.log(
        "b24.batch",
        f"Пакет из {len(commands)} методов выполнен",
        duration_ms=timer.ms,
        method="batch",
        calls=len(commands),
        ok=len(results),
        errors=len(errors) or 0,
    )
    return results, errors


async def call(method: str, params: dict[str, Any] | None = None) -> Any:
    """Вызывает метод вебхука и возвращает поле result.

    При превышении лимита интенсивности (503) повторяет запрос с задержкой.
    """
    base = get_base_url()
    if not base:
        raise BitrixError("Битрикс24 не настроен: не задан B24_URL в файле .env.")

    url = f"{base}{method}.json"
    payload = dict(params or {})
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
    last_message = ""
    timer = logbuf.Timer()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            last_message = f"Битрикс24 не ответил за {int(REQUEST_TIMEOUT)} секунд."
            logbuf.log(
                "b24.error",
                last_message,
                duration_ms=timer.ms,
                level="error",
                method=method,
                attempt=attempt,
            )
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(attempt)
                timer = logbuf.Timer()
                continue
            raise BitrixError(last_message) from None
        except httpx.HTTPError:
            logbuf.log(
                "b24.error",
                "Нет связи с Битрикс24",
                duration_ms=timer.ms,
                level="error",
                method=method,
            )
            raise BitrixError("Нет связи с Битрикс24. Проверьте интернет.") from None

        try:
            data = response.json()
        except ValueError:
            raise BitrixError(
                f"Битрикс24 вернул неожиданный ответ (HTTP {response.status_code})."
            ) from None

        if isinstance(data, dict) and "error" in data:
            code = str(data.get("error") or "").strip()
            description = str(data.get("error_description") or "").strip()
            if code == "QUERY_LIMIT_EXCEEDED" and attempt < MAX_ATTEMPTS:
                logbuf.log(
                    "b24.retry",
                    "Превышен лимит запросов, повторяем",
                    duration_ms=timer.ms,
                    level="warn",
                    method=method,
                    attempt=attempt,
                )
                await asyncio.sleep(attempt)
                timer = logbuf.Timer()
                continue
            logbuf.log(
                "b24.error",
                _friendly_error(code, description),
                duration_ms=timer.ms,
                level="error",
                method=method,
                code=code,
            )
            raise BitrixError(_friendly_error(code, description), code=code)

        if response.status_code >= 400:
            logbuf.log(
                "b24.error",
                f"Битрикс24 вернул ошибку HTTP {response.status_code}",
                duration_ms=timer.ms,
                level="error",
                method=method,
            )
            raise BitrixError(
                f"Битрикс24 вернул ошибку HTTP {response.status_code}."
            )

        result = data.get("result") if isinstance(data, dict) else data
        logbuf.log(
            "b24.call",
            f"Метод {method} выполнен",
            duration_ms=timer.ms,
            method=method,
            http=response.status_code,
            result=_summarize_result(result),
        )
        return result

    raise BitrixError(last_message or "Битрикс24 не ответил.")


# ---------------------------------------------------------------------------
# Действия в CRM
# ---------------------------------------------------------------------------

async def create_lead(card: dict[str, Any]) -> int:
    """Создаёт лид в CRM и возвращает его идентификатор."""
    key_data = card.get("key_data") or {}
    lead_type = str(card.get("lead_type") or "неясно").strip().lower()
    stage_id = STAGE_BY_LEAD_TYPE.get(lead_type, "NEW")

    name, second_name, last_name = split_person_name(str(key_data.get("name") or ""))
    phone, email = split_contact(str(key_data.get("contact") or ""))

    fields: dict[str, Any] = {
        "title": lead_title(card),
        "stageId": stage_id,
        "sourceId": get_source_id(),
        "opened": "Y",
        "comments": card_to_text(card),
    }

    if name and name != "не указано":
        fields["name"] = name
    if second_name:
        fields["secondName"] = second_name
    if last_name and last_name != "не указано":
        fields["lastName"] = last_name

    company = str(key_data.get("company") or "").strip()
    if company and company != "не указано":
        fields["companyTitle"] = company

    reason = str(card.get("lead_type_reason") or "").strip()
    if reason and reason != "не указано":
        fields["statusDescription"] = reason

    manager_id = get_manager_id()
    if manager_id:
        fields["assignedById"] = int(manager_id)

    amount = parse_money(str(key_data.get("budget") or ""))
    if amount:
        fields["opportunity"] = amount
        fields["currencyId"] = get_currency_id()
        fields["isManualOpportunity"] = "Y"

    multifield: list[dict[str, str]] = []
    if phone:
        multifield.append({"typeId": "PHONE", "valueType": "WORK", "value": phone})
    if email:
        multifield.append({"typeId": "EMAIL", "valueType": "WORK", "value": email})
    if multifield:
        fields["fm"] = multifield

    result = await call("crm.item.add", {"entityTypeId": ENTITY_TYPE_LEAD, "fields": fields})
    lead_id = _extract_id(result, "item.id")
    if not lead_id:
        raise BitrixError("Битрикс24 не вернул идентификатор созданного лида.")
    return int(lead_id)


def notification_text(lead_id: int, card: dict[str, Any]) -> str:
    """Текст уведомления в чат."""
    key_data = card.get("key_data") or {}
    lines = [
        "[B]Новая заявка с сайта[/B]",
        f"Тип: {LEAD_TYPE_TITLE.get(str(card.get('lead_type') or ''), 'заявка')}",
        f"Компания: {key_data.get('company', 'не указано')}",
        f"Контакт: {key_data.get('contact', 'не указано')}",
        f"Суть: {card.get('summary', 'не указано')}",
    ]
    if card.get("needs_human"):
        lines.append(f"[B]Нужен человек:[/B] {card.get('human_reason', '')}")
    lines.append(f"Следующий шаг: {card.get('next_step', 'не указано')}")

    url = lead_url(lead_id)
    if url:
        lines.append(f"[URL={url}]Открыть лид[/URL]")
    return "\n".join(lines)


def _batch_value(result: Any) -> Any:
    """В пакете результат подзапроса лежит внутри поля result."""
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return result


def _error_text(error: Any) -> str:
    """Человекочитаемый текст ошибки подзапроса из пакета."""
    if isinstance(error, dict):
        return str(error.get("error_description") or error.get("error") or "неизвестная ошибка")
    return str(error)


async def add_comment_and_notify(
    lead_id: int, card: dict[str, Any]
) -> tuple[int | None, int | None, list[str]]:
    """
    Добавляет комментарий в таймлайн и отправляет уведомление в чат.

    Обе операции уходят одним пакетом batch — это вчетверо быстрее двух
    отдельных обращений к порталу.

    Возвращает (id комментария, id сообщения, список ошибок).
    """
    commands: dict[str, str] = {
        "comment": batch_command(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": lead_id,
                    "ENTITY_TYPE": "lead",
                    "COMMENT": card_to_text(card),
                }
            },
        )
    }

    chat_id = get_chat_id()
    if chat_id:
        commands["message"] = batch_command(
            "im.message.add",
            {
                "DIALOG_ID": chat_id,
                "MESSAGE": notification_text(lead_id, card),
                "SYSTEM": "N",
                "URL_PREVIEW": "N",
            },
        )

    results, errors = await call_batch(commands)

    comment_id: int | None = None
    message_id: int | None = None
    problems: list[str] = []

    if results.get("comment") is not None:
        comment_id = int(_extract_id(_batch_value(results["comment"]), "item.id") or 0) or None
    if "comment" in errors:
        problems.append(f"Комментарий не добавлен: {_error_text(errors['comment'])}")

    if results.get("message") is not None:
        message_id = int(_extract_id(_batch_value(results["message"]), "item.id") or 0) or None
    if "message" in errors:
        problems.append(f"Уведомление не отправлено: {_error_text(errors['message'])}")

    return comment_id, message_id, problems


async def create_task(lead_id: int, card: dict[str, Any]) -> dict[str, str]:
    """
    Создаёт задачу менеджеру и привязывает её к лиду.

    Возвращает {"id": ..., "kind": "task"}.
    Если вебхуку не хватает скоупа task, пробует запасной путь: дело «Звонок»
    в таймлайне лида — для этого достаточно скоупа crm.
    """
    manager_id = get_manager_id()
    if not manager_id:
        raise BitrixError("Не указан ответственный: задайте B24_MANAGER_ID в файле .env.")

    next_step = str(card.get("next_step") or "").strip()
    if not next_step or next_step == "не указано":
        next_step = "Обработать заявку"

    deadline = datetime.now() + timedelta(days=get_task_days())

    fields: dict[str, Any] = {
        "TITLE": next_step[:100],
        "DESCRIPTION": card_to_text(card),
        "RESPONSIBLE_ID": int(manager_id),
        "DEADLINE": deadline.strftime("%Y-%m-%dT%H:%M:%S"),
        # Заявка с «нужен человек» — повышенный приоритет
        "PRIORITY": "3" if card.get("needs_human") else "2",
        "UF_CRM_TASK": [f"L_{lead_id}"],
    }

    try:
        result = await call("tasks.task.add", {"fields": fields})
    except BitrixError as exc:
        if "прав" not in exc.message.lower():
            raise
        # Нет скоупа task — делаем дело в таймлайне лида
        activity_id = await create_activity_task(lead_id, card)
        return {"id": activity_id, "kind": "activity"}

    return {"id": _extract_id(result, "task.id"), "kind": "task"}


async def create_activity_task(lead_id: int, card: dict[str, Any]) -> str:
    """
    Запасной путь вместо задачи: дело «Звонок» в таймлайне лида.

    Нужен только скоуп crm, который есть у большинства вебхуков.
    """
    manager_id = get_manager_id()
    if not manager_id:
        raise BitrixError("Не указан ответственный: задайте B24_MANAGER_ID в файле .env.")

    key_data = card.get("key_data") or {}
    phone, _ = split_contact(str(key_data.get("contact") or ""))

    next_step = str(card.get("next_step") or "").strip()
    if not next_step or next_step == "не указано":
        next_step = "Обработать заявку"

    fields: dict[str, Any] = {
        "OWNER_TYPE_ID": 1,   # 1 — лид
        "OWNER_ID": lead_id,
        "TYPE_ID": 2,         # 2 — звонок
        "SUBJECT": next_step[:100],
        "DESCRIPTION": card_to_text(card),
        "DESCRIPTION_TYPE": 3,  # HTML
        "RESPONSIBLE_ID": int(manager_id),
        "PRIORITY": 3 if card.get("needs_human") else 2,
        "COMPLETED": "N",
        "START_TIME": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "COMMUNICATIONS": [
            {
                "VALUE": phone or "номер не указан",
                "ENTITY_ID": lead_id,
                "ENTITY_TYPE_ID": 1,
            }
        ],
    }

    result = await call("crm.activity.add", {"fields": fields})
    return _extract_id(result, "item.id")
