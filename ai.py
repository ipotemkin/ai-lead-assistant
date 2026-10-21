"""
Модуль работы с AI: собирает промпт, отправляет запрос в ProxyAPI (OpenAI-совместимый API)
и разбирает ответ в словарь.

Намеренно написан на "голом" httpx и стандартной библиотеке — без обёрток OpenAI SDK,
чтобы было видно, что именно происходит и куда уходит ключ.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

# Значения по умолчанию. Реальные значения приходят из файла .env (см. app.py).
DEFAULT_BASE_URL = "https://api.proxyapi.ru/v1"
DEFAULT_MODEL = "openai/gpt-5-mini"

# Таймаут запроса в секундах
TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 15.0

# Схема ответа. По ней модель обязана вернуть все поля карточки лида.
LEAD_CARD_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "summary": {"type": "string"},
        "lead_type": {"type": "string", "enum": ["целевой", "нецелевой", "неясно"]},
        "subject_match": {"type": "string", "enum": ["да", "нет", "неясно"]},
        "offer_clarity": {"type": "string", "enum": ["да", "нет"]},
        "lead_type_reason": {"type": "string"},
        "key_data": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "name": {"type": "string"},
                "contact": {"type": "string"},
                "product": {"type": "string"},
                "quantity": {"type": "string"},
                "budget": {"type": "string"},
                "deadline": {"type": "string"},
            },
            "required": [
                "company",
                "name",
                "contact",
                "product",
                "quantity",
                "budget",
                "deadline",
            ],
            "additionalProperties": False,
        },
        "missing_data": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
        "reply_draft": {"type": "string"},
        "needs_human": {"type": "boolean"},
        "human_reason": {"type": "string"},
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "company",
        "summary",
        "lead_type",
        "subject_match",
        "offer_clarity",
        "lead_type_reason",
        "key_data",
        "missing_data",
        "next_step",
        "reply_draft",
        "needs_human",
        "human_reason",
        "clarifying_questions",
    ],
    "additionalProperties": False,
}

# Пример ответа, который мы показываем модели в тексте промпта.
# Нужен для режима json_object и как подсказка формата.
OUTPUT_EXAMPLE = {
    "company": "ООО «Ромашка»",
    "summary": "Хотят 50 лицензий CRM, бюджет обсуждается.",
    "lead_type": "целевой",
    "subject_match": "да",
    "offer_clarity": "да",
    "lead_type_reason": "Назван наш продукт с количеством и бюджетом — предмет покупки понятен.",
    "key_data": {
        "company": "ООО «Ромашка»",
        "name": "Иван Петров",
        "contact": "+7 900 000-00-00",
        "product": "CRM-система",
        "quantity": "50 лицензий",
        "budget": "800 000 ₽",
        "deadline": "до 1 декабря",
    },
    "missing_data": ["бюджет подтверждён"],
    "next_step": "Позвонить по номеру +7 900 000-00-00 и уточнить бюджет.",
    "reply_draft": "Здравствуйте, Иван! Спасибо за обращение. Готовы обсудить условия по CRM-системе — уточните, пожалуйста, бюджет.",
    "needs_human": False,
    "human_reason": "не требуется",
    "clarifying_questions": ["Какой бюджет на покупку вы закладываете?"],
}

RULES = """ТВОЯ ЗАДАЧА
Разобрать текст заявки и вернуть ТОЛЬКО JSON-объект по схеме ниже.
Ничего кроме JSON не пиши: ни пояснений, ни markdown, ни ```-скелетов.

ГДЕ ИСКАТЬ ДАННЫЕ
В тексте заявки могут быть: компания, имя, контакт, продукт, количество, бюджет, сроки.
Любого из этих данных может не быть вообще.

ФОРМАТ ВХОДЯЩЕЙ ЗАЯВКИ
Текст заявки может приходить в таком виде:
Имя: <имя контактного лица>
Телефон: <телефон>
Почта: <электронная почта>

<текст обращения клиента>

Строки «Имя», «Телефон», «Почта» — это контактные данные автора обращения.
«Имя» записывай в key_data.name. «Телефон» и «Почта» — в key_data.contact
(если заполнены оба, перечисли их через запятую, например «+7 900 123-45-67, ivan@company.ru»;
если заполнен только один — впиши только его). Если эти строки отсутствуют —
в key_data.name и key_data.contact пиши «не указано».
Компания клиента бери из текста обращения, а не из строки «Имя».

ПРАВИЛА (нарушать нельзя)
1. НЕ ВЫДУМЫВАЙ. Бери только то, что реально написано в тексте заявки.
   Нельзя додумывать, достраивать по смыслу или брать данные из своих знаний о мире.
2. Если данных нет — пиши строго «не указано». Не пиши «-», «нет данных», «N/A»,
   пустые строки и не выбрасывай поле из JSON.
3. Все важные данные, которых не хватает, перечисли в missing_data.
   Каждый пункт — короткая фраза без знаков препинания в конце: «бюджет», «сроки», «контакт».
4. Если информации не хватает, чтобы разобраться, задай 1–2 самых важных уточняющих
   вопроса в clarifying_questions. Больше двух вопросов не задавай. Если всё ясно —
   массив пустой.
5. НЕ ОБЕЩАЙ цены, сроки поставки, скидки, бонусы и любые другие коммерческие условия,
   которых нет в тексте заявки. Нельзя писать «отправим за 3 дня», «скидка 20%»,
   «цена 50 000 ₽», если этого нет во входных данных.
6. Определи lead_type — строго по процедуре «КАК ОПРЕДЕЛИТЬ ТИП ЛИДА» ниже.
   Поля subject_match и offer_clarity заполняй ВСЕГДА, даже если ответ очевидный.
7. НУЖЕН ЧЕЛОВЕК (needs_human = true), если в заявке есть: жалоба, конфликт, претензия,
   угроза отказаться или уйти к конкурентам, требование возврата денег или товара,
   недовольство качеством или сроками, юридические или угрожающие формулировки,
   а также любой нестандартный запрос, который нельзя обработать по стандартному сценарию.
8. Когда needs_human = true:
   - human_reason — кратко назови причину, например «жалоба на недопоставку и требование возврата»;
   - reply_draft — НЕЙТРАЛЬНЫЙ черновик ответа: подтверждение получения обращения и
     готовность разобраться. Без извинений за несуществующие проблемы и без обещаний цены,
     сроков, скидок и компенсаций. Нельзя писать «вернём деньги», «исправим за 2 дня»,
     «компенсируем ущерб».
9. next_step — одно конкретное действие менеджера, коротко.
10. reply_draft — короткий черновик ответа клиенту на русском языке, 1–3 предложения,
    без неподтверждённых обещаний.
11. summary — суть запроса в 1–2 предложениях.
12. Тон — деловой и нейтральный. Язык — русский.
13. Не упоминай внутренние отделы, должности и процессы компании (отдел кадров, склад,
    отдел продаж, договорной отдел), если их нет во входных данных. Нельзя обещать
    «передадим в отдел кадров» или «оформим договор», если в заявке этого не было.

КАК ОПРЕДЕЛИТЬ ТИП ЛИДА (lead_type)
Ответь на два вопроса СЕПАРАТНО, в отдельных полях, и выведи их ответы явно.
Не пытайся сразу придумать тип лида — сначала ответь на вопросы, потом примени правило.

Вопрос 1 — ПРЕДМЕТ (поле subject_match).
Относится ли запрос к нашим товарам и услугам?
  «да»     — в тексте назван наш товар или наша услуга, либо то, что мы продаём
              (учитывай описание нашей компании выше);
  «нет»    — запрос точно не про наш товар: резюме, поиск работы, спам, вопросы про
              чужие товары, предложение о сотрудничестве, бытовые вопросы;
  «неясно» — предмет вообще не назван или непонятно, наш это товар или нет
              (например, «хотим обсудить сотрудничество»).

Вопрос 2 — ОПРЕДЕЛЁННОСТЬ (поле offer_clarity).
Понятно ли, ЧТО ИМЕННО мы можем предложить клиенту?
  «да»  — назван конкретный товар, модель, наименование или артикул;
          ЛИБО назван общий класс товара, но дополнен конкретными параметрами
          (количество, бюджет, сроки, назначение, размер, цвет, комплектация) —
          и менеджеру понятно, что именно предлагать;
  «нет» — назван только общий класс без конкретики, и непонятно, какой именно товар
          нужен и в каком объёме. Товар, названный «вообще», а не конкретно —
          это «нет»: «кресла», «стулья», «мебель», «техника», «несколько штук».
          Считай конкретным и то, что прямо перечислено в описании наших товаров.

Правило вывода lead_type:
  subject_match = «нет»                     → нецелевой
  subject_match = «неясно»                  → неясно
  subject_match = «да» и offer_clarity = «нет» → неясно
  subject_match = «да» и offer_clarity = «да» → целевой

Если lead_type = «неясно», в missing_data обязательно укажи, чего не хватает для
определения (например: «конкретная модель или название товара», «количество», «бюджет»),
и задай 1–2 уточняющих вопроса в clarifying_questions.

Поле lead_type_reason — одно короткое предложение с объяснением, почему выбран
именно этот тип лида.

ПРИМЕРЫ РАЗБОРА
Заявка: «Добрый день, сколько у вас стоят офисные кресла? Нужно несколько штук»
  subject_match = «да», offer_clarity = «нет», lead_type = «неясно».
  Причина: назван только общий класс мебели — непонятно, какая именно модель нужна
  и в каком количестве, предлагать нечего.
  missing_data: «конкретная модель или название товара», «количество», «бюджет».
  clarifying_questions: «Какая именно модель кресла вас интересует?»,
  «Сколько штук нужно и в какой бюджет укладываетесь?»

Заявка: «Нужно 20 офисных столов и 40 кресел, бюджет 500 000 ₽, запуск до конца месяца»
  subject_match = «да», offer_clarity = «да», lead_type = «целевой».
  Причина: категория названа вместе с количеством, бюджетом и сроком — предмет
  покупки понятен.

Заявка: «Кресло Атлант, нужно 10 штук»
  subject_match = «да», offer_clarity = «да», lead_type = «целевой».
  Причина: названа конкретная модель и количество.

Заявка: «Ищу работу в вашей компании»
  subject_match = «нет», lead_type = «нецелевой».
  Причина: запрос не про наш товар.

Заявка: «Хотим обсудить сотрудничество»
  subject_match = «неясно», lead_type = «неясно».
  Причина: предмет не назван, непонятно, что именно предлагают."""


class AIError(Exception):
    """Ошибка, которую мы показываем пользователю понятным текстом."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_api_key() -> str:
    """Ключ берётся только из переменных окружения (файл .env). В коде его нет."""
    return (os.getenv("PROXY_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def get_base_url() -> str:
    return (os.getenv("PROXY_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def get_model() -> str:
    return (os.getenv("OPENAI_MODEL") or DEFAULT_MODEL).strip()


def build_system_prompt(company_description: str) -> str:
    """Собирает системный промпт: описание компании + правила + формат ответа."""
    description = (company_description or "").strip()

    if description:
        about_company = (
            "О НАС: мы продаём следующие продукты и услуги:\n"
            f"{description}\n\n"
            "Запрос считается ЦЕЛЕВЫМ, если он явно относится к перечисленным продуктам "
            "или услугам И в нём видна потребность в покупке.\n"
            "Если по описанию нельзя понять, относится ли запрос к нам, и в тексте нет прямого "
            "упоминания наших продуктов — ставь «неясно», а не «нецелевой»."
        )
    else:
        about_company = (
            "О НАС: описание наших продуктов и услуг НЕ УКАЗАНО.\n"
            "В таком случае: если в заявке явно просят купить конкретный товар или услугу — "
            "считай «целевой»; если запрос точно не про покупку (резюме, поиск работы, спам, "
            "бытовые вопросы) — «нецелевой»; иначе — «неясно»."
        )

    return f"""Ты — AI-ассистент отдела продаж. Обрабатываешь входящие заявки и заполняешь
карточку лида для менеджера.

{about_company}

{RULES}

ФОРМАТ ОТВЕТА
Верни только JSON со следующими полями:
{json.dumps(OUTPUT_EXAMPLE, ensure_ascii=False, indent=2)}

Требования к JSON: ровно эти поля, все поля заполнены (пустой массив — это [], а не null),
тип lead_type — одно из: целевой, нецелевой, неясно."""


def _response_format_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "lead_card",
            "strict": True,
            "schema": LEAD_CARD_JSON_SCHEMA,
        },
    }


def _extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели. Терпит markdown-скелеты и лишний текст."""
    cleaned = (text or "").strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIError(
            "AI ответил не в формате JSON. Попробуйте обработать заявку ещё раз "
            "или обратитесь к менеджеру.",
            status_code=502,
        ) from exc

    if not isinstance(parsed, dict):
        raise AIError(
            "AI вернул ответ в неожиданном формате. Попробуйте обработать заявку ещё раз.",
            status_code=502,
        )

    return parsed


def _friendly_error(status_code: int, body: str) -> tuple[str, int]:
    """Превращает ошибку ProxyAPI в понятное сообщение на русском.

    Возвращает (текст для пользователя, HTTP-код для нашего ответа).
    """
    detail = ""
    try:
        payload = json.loads(body)
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
        if not detail:
            detail = str(payload.get("detail") or payload.get("message") or "")
    except (json.JSONDecodeError, AttributeError, TypeError):
        detail = body[:300]

    if status_code == 401:
        return "Ключ API неверный или истёк. Проверьте PROXY_API_KEY в файле .env.", 401
    if status_code == 402:
        return "На балансе ProxyAPI недостаточно средств. Пополните баланс в кабинете.", 402
    if status_code == 403:
        return "Доступ к API запрещён. Проверьте ключ и ограничения ключа в кабинете.", 403
    if status_code == 404:
        return (
            "Модель не найдена на сервере ProxyAPI. "
            f"Проверьте OPENAI_MODEL в файле .env (сейчас указано: {get_model()}).",
            404,
        )
    if status_code == 429:
        return "Слишком много запросов к API. Подождите минуту и попробуйте снова.", 429
    if 500 <= status_code < 600:
        return "Сервис ProxyAPI временно недоступен. Попробуйте через минуту.", 502

    extra = f" Ответ сервера: {detail}" if detail else ""
    return f"Не удалось обработать заявку (HTTP {status_code}).{extra}", 502


async def analyze_lead(text: str, company_description: str) -> dict[str, Any]:
    """
    Отправляет заявку в AI и возвращает распарсенный словарь с полями карточки.
    При любой проблеме бросает AIError с понятным сообщением.
    """
    api_key = get_api_key()
    if not api_key:
        raise AIError(
            "Не найден ключ API. Откройте файл .env рядом с app.py "
            "и впишите ключ в строку PROXY_API_KEY.",
            status_code=500,
        )

    url = f"{get_base_url()}/chat/completions"
    model = get_model()

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(company_description)},
            {"role": "user", "content": f"Текст заявки:\n\n{text}"},
        ],
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            # 1) Просим строгий JSON по схеме — модель обязана вернуть все поля.
            response = await client.post(
                url, headers=headers, json={**payload, "response_format": _response_format_schema()}
            )

            # 2) Не каждая модель умеет строгий режим — пробуем облегчённый.
            if response.status_code == 400 and _mentions_response_format(response.text):
                response = await client.post(
                    url, headers=headers, json={**payload, "response_format": {"type": "json_object"}}
                )

            if response.status_code >= 400:
                message, error_status = _friendly_error(response.status_code, response.text)
                raise AIError(message, status_code=error_status)

            data = response.json()
        except httpx.TimeoutException as exc:
            raise AIError(
                f"AI-сервис не ответил за {int(TIMEOUT_SECONDS)} секунд. Попробуйте ещё раз.",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise AIError(
                "Нет связи с AI-сервисом. Проверьте подключение к интернету.",
                status_code=502,
            ) from exc
        except json.JSONDecodeError as exc:
            raise AIError(
                "AI-сервис вернул неожиданный ответ. Попробуйте ещё раз.",
                status_code=502,
            ) from exc

    content = _extract_content(data)
    if not content:
        raise AIError("AI вернул пустой ответ. Попробуйте ещё раз.", status_code=502)

    return _extract_json(content)


def _mentions_response_format(body: str) -> bool:
    lowered = (body or "").lower()
    return "response_format" in lowered or "json_schema" in lowered


def _extract_content(data: dict[str, Any]) -> str:
    """Достаёт текст ответа из разных форматов, которые встречаются у прокси."""
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Некоторые прокси отдают content как список кусочков.
            parts = []
            for chunk in content:
                if isinstance(chunk, dict):
                    parts.append(str(chunk.get("text") or ""))
                else:
                    parts.append(str(chunk))
            return "".join(parts)
        if isinstance(choices[0].get("text"), str):
            return choices[0]["text"]
        return ""
    except (AttributeError, IndexError, TypeError):
        return ""
