"""
AI-помощник для первичной обработки заявок.

Запуск:
    python app.py
Затем откройте http://127.0.0.1:3000

Настройки (ключ API, модель, описание компании) читаются из файла .env.
Сам ключ в коде не хранится и в браузер не попадает.
"""

from __future__ import annotations

import os
import socket
import sys
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

from ai import AIError, analyze_lead, get_api_key, get_base_url, get_model
from anonymizer import anonymize, restore

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


@app.post("/api/analyze", response_model=LeadCardResponse)
async def analyze(payload: LeadCardRequest) -> Any:
    """Главный сценарий: поля формы -> карточка лида."""
    description = payload.company_description.strip() or (
        os.getenv("COMPANY_DESCRIPTION") or ""
    ).strip()

    request_text = compose_request_text(
        payload.text, payload.name, payload.phone, payload.email
    )

    # Персональные данные заменяются токенами. Соответствие живёт только
    # в памяти этого запроса и исчезает после формирования ответа.
    anonymized = anonymize(
        request_text, known_values=[payload.name, payload.phone, payload.email]
    )

    raw = await analyze_lead(anonymized.text, description)
    card = apply_guards(normalize_card(raw), request_text)
    card = restore_card(card, anonymized.mapping)
    card = fill_contacts_from_form(card, payload)

    # Текст, который ушёл в модель, показываем обезличенным
    card["llm_request"] = anonymized.text
    card["entities"] = anonymized.findings
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
