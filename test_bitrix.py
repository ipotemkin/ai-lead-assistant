"""
Проверки интеграции с Битрикс24.

Запуск:
    python test_bitrix.py
    make test
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import bitrix
from bitrix import (
    BitrixError,
    add_comment_and_notify,
    batch_command,
    card_to_text,
    create_lead,
    create_task,
    notification_text,
    parse_money,
    split_contact,
    split_person_name,
)

PASSED = 0
FAILED: list[str] = []

PORT = 4020
RECEIVED: list[dict] = []
SCENARIO = {"mode": "ok"}


def check(condition: bool, message: str) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(message)


def check_equal(actual, expected, message: str) -> None:
    check(actual == expected, f"{message}\n     получено: {actual!r}\n     ожидалось: {expected!r}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        request = json.loads(raw or "{}")
        method = self.path.rsplit("/", 1)[-1].replace(".json", "")
        RECEIVED.append({"method": method, "payload": request})

        mode = SCENARIO["mode"]
        if mode == "unauthorized":
            return self._send(401, {"error": "INVALID_CREDENTIALS", "error_description": "Invalid request credentials"})
        if mode == "limit" and len(RECEIVED) < 2:
            return self._send(503, {"error": "QUERY_LIMIT_EXCEEDED", "error_description": "Too many requests"})
        if mode == "no_scope":
            return self._send(401, {"error": "insufficient_scope", "error_description": "The request requires higher privileges"})
        if mode == "no_task_scope" and method.startswith("tasks."):
            return self._send(401, {"error": "insufficient_scope", "error_description": "The request requires higher privileges"})

        if method == "crm.item.add":
            # Реальный формат ответа: {"result": {"item": {"id": 342}}}
            return self._send(200, {"result": {"item": {"id": 4242, "title": "тест"}}})
        if method == "crm.timeline.comment.add":
            return self._send(200, {"result": 777})
        if method == "tasks.task.add":
            return self._send(200, {"result": {"task": {"id": "888"}}})
        if method == "im.message.add":
            return self._send(200, {"result": 999})
        if method == "crm.activity.add":
            return self._send(200, {"result": 555})
        if method == "batch":
            return self._send(200, {"result": {"result": {
                "comment": {"result": 777},
                "message": {"result": 999},
                "stages": [{"STATUS_ID": "NEW", "NAME": "Не обработан"}],
                "sources": [{"STATUS_ID": "WEB", "NAME": "Веб-сайт"}],
                "fields": [{"ID": "1", "TITLE": "Название"}],
                "user": [{"ID": "1", "NAME": "Иван", "LAST_NAME": "Петров"}],
                "chats": {"items": [{"id": 1, "chat_id": 55, "title": "Отдел продаж", "type": "chat"}]},
            }, "result_error": {}}})
        if method == "crm.status.list":
            return self._send(200, {"result": [{"STATUS_ID": "NEW", "NAME": "Не обработан"}]})
        if method == "user.get":
            # Реальный формат: user.get возвращает МАССИВ пользователей
            return self._send(200, {"result": [{"ID": "1", "NAME": "Иван", "LAST_NAME": "Петров"}]})
        if method == "im.recent.list":
            return self._send(200, {"result": {"items": [{"id": 1, "chat_id": 55, "title": "Отдел продаж", "type": "chat"}]}})
        return self._send(404, {"error": "ERROR_METHOD_NOT_FOUND", "error_description": "Method not found!"})


def start_server() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


CARD = {
    "company": "ООО ОфисЛайн",
    "summary": "Нужны 20 офисных столов и 40 кресел",
    "lead_type": "целевой",
    "lead_type_reason": "Назван наш товар с количеством и бюджетом",
    "key_data": {
        "company": "ООО ОфисЛайн",
        "name": "Иван Петров",
        "contact": "+7 900 123-45-67, ivanov@officeline.ru",
        "product": "офисные столы и кресла",
        "quantity": "20 столов и 40 кресел",
        "budget": "500 000 рублей",
        "deadline": "до конца месяца",
    },
    "missing_data": ["точные модели"],
    "next_step": "Позвонить по +7 900 123-45-67 и уточнить модели.",
    "reply_draft": "Здравствуйте! Готовы обсудить условия.",
    "needs_human": False,
    "human_reason": "не требуется",
    "clarifying_questions": ["Какие модели предпочитаете?"],
}


async def main() -> int:
    server = start_server()
    bitrix.os.environ["B24_URL"] = f"http://127.0.0.1:{PORT}/rest/1/testtoken/"
    bitrix.os.environ["B24_MANAGER_ID"] = "1"
    bitrix.os.environ["B24_CHAT_ID"] = "chat55"
    bitrix.os.environ["B24_SOURCE_ID"] = "WEB"
    bitrix.os.environ["B24_CURRENCY_ID"] = "RUB"

    # ---------------------------------------------------------------- разбор
    print("Проверка 1. Разбор данных карточки")
    check_equal(split_person_name("Иван Петров"), ("Иван", "", "Петров"), "Имя и фамилия")
    check_equal(split_person_name("Петров Иван"), ("Иван", "", "Петров"), "Фамилия и имя")
    check_equal(split_person_name("Анна Сергеевна"), ("Анна", "Сергеевна", ""), "Имя и отчество")
    check_equal(split_contact("+7 900 123-45-67, ivanov@company.ru"), ("+7 900 123-45-67", "ivanov@company.ru"), "Телефон и почта")
    check_equal(parse_money("500 000 рублей"), 500000.0, "Сумма")
    check_equal(parse_money("500 000 - 800 000"), None, "Диапазон не разбираем")
    check("Тип лида: целевой" in card_to_text(CARD), "Текст карточки содержит тип лида")
    check("Позвонить по +7 900 123-45-67" in card_to_text(CARD), "Текст карточки содержит следующий шаг")

    # ---------------------------------------------------------------- лид
    print("Проверка 2. Создание лида")
    RECEIVED.clear()
    lead_id = await create_lead(CARD)
    check_equal(lead_id, 4242, "Возвращён id лида (ответ приходит объектом item)")
    call = RECEIVED[0]
    check_equal(call["method"], "crm.item.add", "Вызван crm.item.add")
    payload = call["payload"]
    check_equal(payload.get("entityTypeId"), 1, "entityTypeId = 1 (лид)")
    fields = payload.get("fields") or {}
    check_equal(fields.get("title"), "ООО ОфисЛайн — офисные столы и кресла", "Заголовок лида")
    check_equal(fields.get("stageId"), "NEW", "Стадия целевого лида")
    check_equal(fields.get("name"), "Иван", "Имя")
    check_equal(fields.get("lastName"), "Петров", "Фамилия")
    check_equal(fields.get("companyTitle"), "ООО ОфисЛайн", "Компания")
    check_equal(fields.get("assignedById"), 1, "Ответственный")
    check_equal(fields.get("opportunity"), 500000.0, "Сумма лида")
    check_equal(fields.get("currencyId"), "RUB", "Валюта")
    check_equal(fields.get("isManualOpportunity"), "Y", "Ручная сумма")
    check_equal(fields.get("sourceId"), "WEB", "Источник")
    check("Тип лида" in (fields.get("comments") or ""), "Комментарий с карточкой")
    fm = fields.get("fm") or []
    check_equal(fm[0].get("typeId"), "PHONE", "Телефон в мультиполе")
    check_equal(fm[1].get("typeId"), "EMAIL", "Почта в мультиполе")

    # нецелевой лид уходит в JUNK
    RECEIVED.clear()
    junk = dict(CARD, lead_type="нецелевой")
    await create_lead(junk)
    check_equal((RECEIVED[0]["payload"]["fields"]).get("stageId"), "JUNK", "Стадия нецелевого лида")

    # ---------------------------------------------------------------- прочее
    print("Проверка 3. Комментарий, задача, уведомление")
    RECEIVED.clear()
    comment_id, message_id, problems = await add_comment_and_notify(4242, CARD)
    check_equal(comment_id, 777, "Возвращён id комментария")
    check_equal(message_id, 999, "Возвращён id сообщения")
    check_equal(problems, [], "Ошибок нет")
    check_equal(RECEIVED[0]["method"], "batch", "Комментарий и уведомление ушли одним пакетом")
    commands = RECEIVED[0]["payload"]["cmd"]
    check("comment" in commands and "message" in commands, "В пакете оба подзапроса")
    check("fields[ENTITY_ID]=4242" in commands["comment"], "Комментарий привязан к лиду")
    check("fields[ENTITY_TYPE]=lead" in commands["comment"], "Тип объекта комментария")
    check("fields[COMMENT]=" in commands["comment"], "Текст комментария внутри fields")
    check("DIALOG_ID=chat55" in commands["message"], "Чат уведомления")
    check("%D0%A2%D0%B8%D0%BF" in commands["comment"] or "Тип" in commands["comment"],
          "Текст комментария передан")

    RECEIVED.clear()
    task = await create_task(4242, CARD)
    check_equal(task["kind"], "task", "Создана настоящая задача (есть скоуп task)")
    check_equal(task["id"], "888", "Возвращён id задачи")
    fields = RECEIVED[0]["payload"]["fields"]
    check_equal(RECEIVED[0]["method"], "tasks.task.add", "Вызван tasks.task.add")
    check_equal(fields.get("RESPONSIBLE_ID"), 1, "Исполнитель задачи")
    check("Позвонить" in fields.get("TITLE", ""), "Название задачи — следующий шаг")
    check_equal(fields.get("UF_CRM_TASK"), ["L_4242"], "Задача привязана к лиду")
    check("DEADLINE" in fields, "У задачи есть срок")

    check(
        "/crm/lead/details/4242/" in notification_text(4242, CARD),
        "Ссылка на лид в уведомлении",
    )
    check("[B]Новая заявка" in notification_text(4242, CARD), "Текст уведомления")

    # ---------------------------------------------------------------- ошибки
    print("Проверка 4. Ошибки")
    SCENARIO["mode"] = "unauthorized"
    try:
        await create_lead(CARD)
        check(False, "При 401 должна выбрасываться ошибка")
    except BitrixError as exc:
        check("вебхук" in exc.message.lower(), f"Понятное сообщение о вебхуке: {exc.message}")

    SCENARIO["mode"] = "no_scope"
    try:
        await create_lead(CARD)
        check(False, "При insufficient_scope должна выбрасываться ошибка")
    except BitrixError as exc:
        check("прав" in exc.message.lower(), f"Понятное сообщение о правах: {exc.message}")

    # нет скоупа task -> задача не создаётся, но дело в таймлайне создаётся
    SCENARIO["mode"] = "no_task_scope"
    RECEIVED.clear()
    task = await create_task(4242, CARD)
    check_equal(task["kind"], "activity", "Без скоупа task создаётся дело в таймлайне")
    check(RECEIVED[-1]["method"] == "crm.activity.add", "Вызван crm.activity.add")
    fields = RECEIVED[-1]["payload"]["fields"]
    check_equal(fields.get("OWNER_ID"), 4242, "Дело привязано к лиду")
    check_equal(fields.get("TYPE_ID"), 2, "Тип дела — звонок")
    check(bool(fields.get("COMMUNICATIONS")), "У дела есть коммуникация")
    SCENARIO["mode"] = "ok"

    # повтор при превышении лимита
    SCENARIO["mode"] = "limit"
    RECEIVED.clear()
    lead_id = await create_lead(CARD)
    check_equal(lead_id, 4242, "После 503 запрос повторяется и проходит")
    check(len(RECEIVED) >= 2, f"Было несколько попыток: {len(RECEIVED)}")

    SCENARIO["mode"] = "ok"

    # ---------------------------------------------------------------- настройки
    print("Проверка 5. Настройки")

    # Диагностика /api/b24/check не должна падать ни при каком ответе Битрикса
    from app import b24_check

    report = await b24_check()
    check(isinstance(report.get("checks"), list), "Диагностика вернула отчёт")
    check(len(report["checks"]) >= 5, f"В отчёте все проверки: {len(report['checks'])}")
    check(
        all("detail" in item and "name" in item for item in report["checks"]),
        "У каждой проверки есть название и пояснение",
    )
    manager_check = next(
        (item for item in report["checks"] if item["name"] == "Ответственный"), {}
    )
    check(
        "Иван Петров" in manager_check.get("detail", ""),
        f"Имя ответственного разобрано из массива: {manager_check.get('detail')}",
    )

    # Битрикс недоступен — отчёт всё равно должен собраться
    bitrix.os.environ["B24_URL"] = "http://127.0.0.1:4999/rest/1/x/"
    report = await b24_check()
    check(isinstance(report.get("checks"), list), "Отчёт собран даже при недоступном Битриксе")
    check(
        any(item["level"] == "fail" for item in report["checks"]),
        "Есть строка об ошибке подключения",
    )
    bitrix.os.environ["B24_URL"] = f"http://127.0.0.1:{PORT}/rest/1/testtoken/"

    bitrix.os.environ.pop("B24_MANAGER_ID", None)
    check_equal(bitrix.get_manager_id(), "1", "ID менеджера берётся из адреса вебхука")
    bitrix.os.environ["B24_MANAGER_ID"] = "5"
    check_equal(bitrix.get_manager_id(), "5", "ID менеджера из .env приоритетнее")
    check("crm/lead/details/7/" in bitrix.lead_url(7), "Ссылка на лид")
    bitrix.os.environ["B24_URL"] = ""
    check_equal(bitrix.get_base_url(), "", "Пустой B24_URL даёт пустой адрес")
    check_equal(bitrix.get_portal_url(), "", "Пустой портал")

    server.shutdown()
    return 0


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print(f"Пройдено проверок: {PASSED}")
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)}")
        for item in FAILED:
            print(f"  ✗ {item}")
        sys.exit(1)
    print("Все проверки пройдены.")
