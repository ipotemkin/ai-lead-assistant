"""
Обезличивание персональных данных перед отправкой в AI.

Схема работы:
    результат = anonymize(текст, known_values=[...])
    результат.text      — текст с токенами вида [ТЕЛЕФОН_1], его видит модель
    результат.mapping   — {"[ТЕЛЕФОН_1]": "+7 900 123-45-67"}
    результат.findings  — [{"token": "[ТЕЛЕФОН_1]", "type": "телефон"}] без значений

Соответствие живёт только в памяти текущего запроса: оно лежит в локальной
переменной и исчезает вместе с ответом. Ничего не пишется на диск, в модульные
переменные, в логи и в базу — при обезличивании это принципиально.

Что НЕ обезличивается (это не персональные данные, и без них модель не сможет
определить тип лида): названия компаний, суммы, количества, сроки, названия товаров.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Типы сущностей
# ---------------------------------------------------------------------------

TYPE_LABELS: dict[str, str] = {
    "ИМЯ": "имя",
    "ТЕЛЕФОН": "телефон",
    "ПОЧТА": "почта",
    "АДРЕС": "адрес",
    "ИНН": "ИНН",
    "СНИЛС": "СНИЛС",
    "ПАСПОРТ": "паспорт",
    "КАРТА": "банковская карта",
    "ДАТА_РОЖДЕНИЯ": "дата рождения",
}

# Приоритет: чем меньше, тем важнее. Используется при разрешении пересечений.
PRIORITY: dict[str, int] = {
    "ИМЯ": 0,
    "ПОЧТА": 1,
    "ИНН": 2,
    "СНИЛС": 3,
    "ПАСПОРТ": 4,
    "КАРТА": 5,
    "ТЕЛЕФОН": 6,
    "ДАТА_РОЖДЕНИЯ": 7,
    "АДРЕС": 8,
}

# Значения из полей формы — заведомо персональные данные, важнее любого детектора.
PRIORITY_KNOWN = -1


@dataclass
class AnonymizationResult:
    """Результат обезличивания."""

    text: str
    mapping: dict[str, str] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Человекочитаемая сводка: «имя (2), телефон (1)»."""
        counts: dict[str, int] = {}
        for item in self.findings:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
        return ", ".join(f"{name} ({count})" for name, count in counts.items())


@dataclass
class _Span:
    """Найденный фрагмент персональных данных в исходном тексте."""

    start: int
    end: int
    kind: str
    value: str
    priority: int = 0


# ---------------------------------------------------------------------------
# Словари
# ---------------------------------------------------------------------------

MALE_NAMES: frozenset[str] = frozenset(
    """
    александр алексей анатолий андрей антон аркадий артём арсений борис вадим
    валентин валерий василий виктор виталий владислав владимир вячеслав геннадий
    георгий григорий глеб даниил денис дмитрий евгений егор иван игорь илья
    кирилл константин леонид максим марк михаил никита николай олег павел пётр
    петр роман руслан сергей станислав степан тимофей фёдор юрий ярослав савелий
    тимур мирон матвей филипп ян эдуард давид лев семён артур макар николай
    дмитрий александр илья кирилл игорь лев
    """.split()
)

FEMALE_NAMES: frozenset[str] = frozenset(
    """
    александра алина алла анастасия ангелина анна валентина валерия вера
    вероника виктория галина дарья диана екатерина елена елизавета ирина
    кристина ксения лариса любовь людмила маргарита мария марина надёжа
    наталья нина оксана ольга полина раиса светлана софия тамара татьяна ульяна
    юлия яна алёна алиса бэлла василиса дарина евгения инесса карина кира
    лилия майя наиля ольга рита снежана ульяна
    """.split()
)


def _female_declensions(name: str) -> set[str]:
    """Падежные формы женских имён: Мария -> Марии, Анна -> Анны."""
    forms: set[str] = set()
    if name.endswith("а"):
        forms.update({name[:-1] + "ы", name[:-1] + "е"})
    elif name.endswith("я"):
        forms.update({name[:-1] + "и", name[:-1] + "е"})
    elif name.endswith("ия"):
        forms.update({name[:-2] + "ии", name[:-2] + "ие"})
    return forms


def _build_name_set() -> frozenset[str]:
    names: set[str] = set(MALE_NAMES) | set(FEMALE_NAMES)
    for name in FEMALE_NAMES:
        names |= _female_declensions(name)
    return frozenset(names)


FIRST_NAMES: frozenset[str] = _build_name_set()

# Слова, которые не могут быть фамилией в составе ФИО
NOT_A_SURNAME: frozenset[str] = frozenset(
    """
    ооо оао зао пао нпо нпк ип гк тд тпк рф ао оао нпо ооо
    руб рублей коп копеек тел телефон имя компания фирма отдел закупок
    маркетинг логистика склад офис директор менеджер менеджеры
    добрый день здравствуйте спасибо пожалуйста привет
    январь февраль март апрель май июнь июль август сентябрь октябрь
    ноябрь декабрь понедельник вторник среда четверг пятница суббота воскресенье
    """.split()
)

# Организационные формы — перед ними фамилия не ищется
ORG_PREFIXES: frozenset[str] = frozenset("ооо оао зао пао нпо нпк ип гк тд тпк ао".split())

CITIES: tuple[str, ...] = tuple(
    """
    Санкт-Петербург Санкт Петербург СанктПетербург
    Новосибирск Екатеринбург Нижний Новгород Казань Челябинск Красноярск
    Самара Уфа Ростов-на-Дону Омск Краснодар Воронеж Пермь Волгоград Саратов
    Тюмень Тольятти Барнаул Ижевск Ульяновск Иркутск Хабаровск Ярославль
    Владивосток Махачкала Томск Оренбург Кемерово Новокузнецк Рязань
    Астрахань Набережные Челны Пенза Липецк Киров Чебоксары Тула
    Калининград Балашиха Курск Ставрополь Улан-Удэ Магнитогорск Сочи Тверь
    Иваново Брянск Белгород Сургут Владимир Нижний Тагил Архангельск Чита
    Симферополь Калуга Смоленск Курган Орёл Орел Череповец Вологда
    Владикавказ Мурманск Саранск Якутск Тамбов Грозный Стерлитамак Кострома
    Нижневартовск Новомосковск Комсомольск-на-Амуре Йошкар-Ола Таганрог
    Сыктывкар Нальчик Нижнекамск Шахты Дзержинск Орск Братск Ангарск Энгельс
    Благовещенск Старый Оскол Великий Новгород Псков Бийск Прокопьевск
    Балаково Армавир Рыбинск Северодвинск Абакан Норильск Сызрань Волгодонск
    Уссурийск Каменск-Уральский Новочеркасск Златоуст Электросталь Салават
    Миасс Керчь Пятигорск Коломна Майкоп Ковров Домодедово Камышин
    Нефтеюганск Серпухов Москва Питер
    """.split("\n")
)

# Падежные формы самых крупных городов
CITY_FORMS: tuple[str, ...] = (
    "Москва", "Москву", "Москве", "Москвы",
    "СПб", "Санкт-Петербург", "Санкт Петербург", "СанктПетербург",
    "Петербург", "Петербурге", "Петербурга",
)


def _city_names() -> frozenset[str]:
    return frozenset(name.lower() for name in set(CITIES) | set(CITY_FORMS))


# ---------------------------------------------------------------------------
# Регулярные выражения
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-.]*[A-Za-z0-9])?\.[A-Za-z]{2,}"
)

# Кандидат в телефон: последовательность цифр с разделителями
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\d\-])\+?\d(?:[\s\-().]{0,2}\d){6,18}(?![\d\-])")

# Маркеры перед телефоном
_PHONE_MARKER_RE = re.compile(
    r"(?i)(?:тел(?:ефон|а)?|моб(?:айл|ильный)?|сотовый|связаться|звоните|позвоните|"
    r"пишите|whatsapp|телеграм|tel|phone|моб\.|с\.)"
)

# Группировки цифр, характерные для телефонов
_PHONE_GROUP_PATTERNS = (
    (3, 3, 2, 2),
    (3, 2, 2, 2),
    (3, 3, 3, 2),
    (3, 3, 4),
    (3, 7),
    (4, 3, 3),
    (2, 3, 2, 2),
)

_INN_RE = re.compile(r"(?i:инн)\s*№?\s*(\d{10}(?:\d{2})?)")
_SNILS_RE = re.compile(r"(?i:снилс)\s*№?\s*(\d[\d\- ]{8,14})")
_PASSPORT_RE = re.compile(r"(?i:паспорт\w*|серия\w*\s+паспорта)\s*№?\s*(\d[\d\- ]{5,12})")
_CARD_RE = re.compile(r"(?<![\d\-])(?:\d{4}[ \-]?){3}\d{4}(?![\d\-])")
_DOB_RE = re.compile(
    r"(?i:род\w*|дата\s+рождения|д\.?\s?р\.?|рождени\w*)\s*[:\-—]?\s*"
    r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})"
)

_CITY_PATTERN = "|".join(
    re.escape(name) for name in sorted(set(CITIES) | set(CITY_FORMS), key=len, reverse=True)
)
_CITY_RE = re.compile(
    r"(?<![\w-])(?i:в|во|из|до|г\.|гор\.|город|города|городе)\s+(" + _CITY_PATTERN + r")(?![\w-])"
)

# Адрес целиком: улица + дом + квартира + город
_ADDRESS_TAIL_RE = re.compile(
    r"(?:\s*,\s*(?:г\.\s*)?(?:" + _CITY_PATTERN + r"))?"
)
_STREET_RE = re.compile(
    r"(?<![\w-])"
    r"(?i:ул(?:иц[аы]|ице|ицу)?|улица|проспект(?:а|е|у)?|пр-т|пр-те|переулок(?:а|е|ке)?|"
    r"пер\.|набережн(?:ая|ой|ую)?|наб\.|шоссе|бульвар(?:а|е)?|площад(?:ь|и)|пл\.|тракт|проезд|тупик)"
    r"\.?\s+"
    r"[А-ЯЁ][а-яёA-Za-z0-9\-]*(?:\s+[А-ЯЁ][а-яёA-Za-z0-9\-]*)?"
    r"(?:\s*,\s*(?:(?:д|дом)\.?\s*)?\d{1,5})?"                       # , д. 12  или  , 12
    r"(?:\s*,\s*(?:к|корп|стр)\.?\s*\d{1,5})*"                        # , корп. 2
    r"(?:\s*,\s*(?:кв|квартира|офис)\.?\s*\d{1,5})?"                  # , кв. 5
    + _ADDRESS_TAIL_RE.pattern                                        # , Санкт-Петербург
)

_HOUSE_RE = re.compile(
    r"(?:\s*,\s*(?:д|дом)\.?\s*\d{1,5}(?:\s*,\s*(?:кв|квартира)\.?\s*\d{1,5})?)"
)
_ADDRESS_HINT_RE = re.compile(r"(?i:адрес\w*)")

# Название адреса без типа улицы: «Новый Арбат», «Большая Дмитровка»
_TOPONYM_NAME_RE = re.compile(
    r"(?<![\w-])[А-ЯЁ][а-яёA-Za-z0-9\-]*(?:\s+[А-ЯЁ][а-яёA-Za-z0-9\-]*){0,2}"
)

# Полный адрес: название + номер дома (+ квартира, офис)
_TOPONYM_RE = re.compile(
    r"[А-ЯЁ][а-яёA-Za-z0-9\-]*(?:\s+[А-ЯЁ][а-яёA-Za-z0-9\-]*){0,2}"
    r"(?:\s*[,\-–]\s*\d{1,5})"
    r"(?:\s*,\s*(?:кв|квартира|офис|стр|корп)\.?\s*\d{1,5})*"
)

# Сколько символов после маркера «адрес» просматриваем в поисках начала адреса
ADDRESS_WINDOW = 24

_NAME_PATTERN = "|".join(sorted(FIRST_NAMES, key=len, reverse=True))
_FIRST_NAME_RE = re.compile(
    r"(?<![\w-])(?:" + _NAME_PATTERN + r")(?![\w-])", re.IGNORECASE
)
_PATRONYMIC_RE = re.compile(
    r"(?<![\w-])[А-ЯЁ][а-яё]{2,}(?:ович|евич|овна|евна|ична|инична|ишна)(?![\w-])"
)
_SURNAME_RE = re.compile(r"(?<![\w-])[А-ЯЁ][а-яё]{2,}(?![\w-])")

# Фамилия с инициалами: «Иванов И.И.», «И. И. Иванов»
_SURNAME_INITIALS_RE = re.compile(
    r"(?<![\w-])[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.(?![\w-])"
)
_INITIALS_SURNAME_RE = re.compile(
    r"(?<![\w-])[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]{2,}(?![\w-])"
)

# Токены при обратной подстановке
_BRACKET_TOKEN_RE = re.compile(r"\[\s*([^\[\]]+?)\s*\]")
_BARE_TOKEN_RE = re.compile(r"\b([A-ZА-ЯЁ][A-ZА-ЯЁа-яё_]*_\d+)\b")


# ---------------------------------------------------------------------------
# Детекторы
# ---------------------------------------------------------------------------

def _guess_kind(value: str) -> str:
    """Определяет тип точно известного значения по его виду."""
    if "@" in value:
        return "ПОЧТА"
    digits = [char for char in value if char.isdigit()]
    if len(digits) >= 6:
        return "ТЕЛЕФОН"
    return "ИМЯ"


def _find_all_known_values(text: str, known_values: Iterable[str]) -> list[_Span]:
    """Точные совпадения значений из полей формы — во всех вхождениях по всему тексту."""
    spans: list[_Span] = []
    for raw in known_values:
        value = (raw or "").strip()
        if len(value) < 2:
            continue
        kind = _guess_kind(value)
        for match in re.finditer(re.escape(value), text, re.IGNORECASE):
            spans.append(
                _Span(match.start(), match.end(), kind, match.group(0), PRIORITY_KNOWN)
            )
    return spans


def _find_emails(text: str) -> list[_Span]:
    return [
        _Span(m.start(), m.end(), "ПОЧТА", m.group(0), PRIORITY["ПОЧТА"])
        for m in _EMAIL_RE.finditer(text)
    ]


def _looks_like_phone(digits: str, raw: str, has_marker: bool) -> bool:
    count = len(digits)
    if not 10 <= count <= 15:
        return False
    if has_marker or "+" in raw:
        return True
    groups = [group for group in re.split(r"\D+", raw) if group]
    sizes = [len(group) for group in groups]
    if sizes and sizes[0] == 1 and groups[0] in ("7", "8") and count in (10, 11):
        sizes = sizes[1:]
    if not sizes:
        return False
    if tuple(sizes) in _PHONE_GROUP_PATTERNS:
        return True
    # Слитный номер: 79001234567, 84951234567
    if len(sizes) == 1 and count in (11, 12) and groups[0][:1] in ("7", "8"):
        return True
    return False


def _find_phones(text: str) -> list[_Span]:
    spans: list[_Span] = []
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        digits = "".join(char for char in raw if char.isdigit())
        # Маркер может стоять прямо перед номером
        prefix = text[max(0, match.start() - 25) : match.start()]
        has_marker = bool(_PHONE_MARKER_RE.search(prefix))
        if _looks_like_phone(digits, raw, has_marker):
            spans.append(_Span(match.start(), match.end(), "ТЕЛЕФОН", raw, PRIORITY["ТЕЛЕФОН"]))
    return spans


def _find_marked_ids(text: str) -> list[_Span]:
    spans: list[_Span] = []
    for pattern, kind in (
        (_INN_RE, "ИНН"),
        (_SNILS_RE, "СНИЛС"),
        (_PASSPORT_RE, "ПАСПОРТ"),
    ):
        for match in pattern.finditer(text):
            spans.append(
                _Span(match.start(1), match.end(1), kind, match.group(1), PRIORITY[kind])
            )
    return spans


def _find_cards(text: str) -> list[_Span]:
    spans: list[_Span] = []
    for match in _CARD_RE.finditer(text):
        digits = "".join(char for char in match.group(0) if char.isdigit())
        if len(digits) == 16:
            spans.append(
                _Span(match.start(), match.end(), "КАРТА", match.group(0), PRIORITY["КАРТА"])
            )
    return spans


def _find_dates_of_birth(text: str) -> list[_Span]:
    return [
        _Span(m.start(1), m.end(1), "ДАТА_РОЖДЕНИЯ", m.group(1), PRIORITY["ДАТА_РОЖДЕНИЯ"])
        for m in _DOB_RE.finditer(text)
    ]


def _find_addresses(text: str) -> list[_Span]:
    spans: list[_Span] = []

    for match in _STREET_RE.finditer(text):
        start = match.start()
        end = match.end()
        # Город перед адресом: «Москва, ул. Тверская, 1»
        prefix = text[max(0, start - 30) : start]
        city = re.search(
            r"(?i:г\.\s*)?(" + _CITY_PATTERN + r")\s*,\s*$", prefix
        )
        if city:
            start = max(0, start - 30) + city.start()
        spans.append(_Span(start, end, "АДРЕС", text[start:end], PRIORITY["АДРЕС"]))

    for match in _CITY_RE.finditer(text):
        spans.append(
            _Span(match.start(), match.end(), "АДРЕС", match.group(0), PRIORITY["АДРЕС"])
        )

    # Дом и квартира без улицы — только если рядом есть слово «адрес»
    if _ADDRESS_HINT_RE.search(text):
        for match in _HOUSE_RE.finditer(text):
            prefix = text[max(0, match.start() - 40) : match.start()]
            if _ADDRESS_HINT_RE.search(prefix):
                spans.append(
                    _Span(match.start(), match.end(), "АДРЕС", match.group(0), PRIORITY["АДРЕС"])
                )

    # Топоним без типа улицы рядом с маркером «по адресу»: «Новый Арбат, 10».
    # Окно ограничивает, ГДЕ искать начало адреса; сам адрес захватывается целиком.
    for marker in _ADDRESS_HINT_RE.finditer(text):
        window = text[marker.end() : marker.end() + ADDRESS_WINDOW]
        name = _TOPONYM_NAME_RE.search(window)
        if not name:
            continue
        start = marker.end() + name.start()
        toponym = _TOPONYM_RE.match(text[start:])
        if toponym:
            end = start + toponym.end()
            spans.append(
                _Span(start, end, "АДРЕС", text[start:end], PRIORITY["АДРЕС"])
            )

    return spans


def _find_names(text: str) -> list[_Span]:
    """Имена по лексикону: имя + необязательное отчество + необязательная фамилия."""
    spans: list[_Span] = []
    for match in _FIRST_NAME_RE.finditer(text):
        start = match.start()
        end = match.end()

        # Отчество
        tail = text[end : end + 30]
        stripped = tail.lstrip()
        patronymic = _PATRONYMIC_RE.match(stripped)
        if patronymic:
            end += (len(tail) - len(stripped)) + patronymic.end()

        # Фамилия
        tail = text[end : end + 30]
        stripped = tail.lstrip()
        surname = _SURNAME_RE.match(stripped)
        if surname:
            candidate = surname.group(0).lower()
            if candidate not in NOT_A_SURNAME and candidate not in _city_names():
                end += (len(tail) - len(stripped)) + surname.end()

        spans.append(_Span(start, end, "ИМЯ", text[start:end], PRIORITY["ИМЯ"]))
    return spans


def _find_surname_with_initials(text: str) -> list[_Span]:
    """Фамилия с инициалами: «Иванов И.И.», «И. И. Иванов»."""
    spans: list[_Span] = []
    for pattern in (_SURNAME_INITIALS_RE, _INITIALS_SURNAME_RE):
        for match in pattern.finditer(text):
            surname = match.group(0).split()[0]
            if surname.lower() in NOT_A_SURNAME:
                continue
            # Перед фамилией не должно стоять обозначение организации
            prefix = text[max(0, match.start() - 15) : match.start()].split()
            if prefix and prefix[-1].lower() in ORG_PREFIXES:
                continue
            spans.append(
                _Span(match.start(), match.end(), "ИМЯ", match.group(0), PRIORITY["ИМЯ"])
            )
    return spans


# ---------------------------------------------------------------------------
# Сборка результата
# ---------------------------------------------------------------------------

def _resolve_overlaps(spans: list[_Span]) -> list[_Span]:
    """Если фрагменты пересекаются, оставляем более приоритетный."""
    ordered = sorted(spans, key=lambda s: (s.priority, -(s.end - s.start), s.start))
    accepted: list[_Span] = []
    for span in ordered:
        if any(span.start < other.end and other.start < span.end for other in accepted):
            continue
        accepted.append(span)
    accepted.sort(key=lambda s: s.start)
    return accepted


def anonymize(text: str, known_values: Iterable[str] = ()) -> AnonymizationResult:
    """
    Заменяет персональные данные в тексте временными токенами.

    known_values — значения из полей формы (имя, телефон, почта). Они известные
    персональные данные, поэтому заменяются в первую очередь и во всех
    вхождениях по всему тексту, включая тело заявки.
    """
    if not text:
        return AnonymizationResult(text="", mapping={}, findings=[])

    known = [value for value in known_values if (value or "").strip()]

    spans: list[_Span] = []
    spans += _find_all_known_values(text, known)
    spans += _find_emails(text)
    spans += _find_marked_ids(text)
    spans += _find_cards(text)
    spans += _find_dates_of_birth(text)
    spans += _find_phones(text)
    spans += _find_addresses(text)
    spans += _find_names(text)
    spans += _find_surname_with_initials(text)

    accepted = _resolve_overlaps(spans)

    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}
    by_value: dict[tuple[str, str], str] = {}
    findings: list[dict[str, str]] = []

    pieces: list[str] = []
    position = 0

    for span in accepted:
        original = text[span.start : span.end]
        key = (span.kind, original.lower())
        if key in by_value:
            token = by_value[key]
        else:
            counters[span.kind] = counters.get(span.kind, 0) + 1
            token = f"[{span.kind}_{counters[span.kind]}]"
            by_value[key] = token
            mapping[token] = original
            findings.append({"token": token, "type": TYPE_LABELS.get(span.kind, span.kind.lower())})

        pieces.append(text[position : span.start])
        pieces.append(token)
        position = span.end

    pieces.append(text[position:])
    return AnonymizationResult(text="".join(pieces), mapping=mapping, findings=findings)


def restore(text: str, mapping: dict[str, str]) -> str:
    """Подставляет настоящие значения вместо токенов."""
    if not text or not mapping:
        return text

    lookup = {token.strip("[] ").upper(): value for token, value in mapping.items()}

    def replace_bracketed(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return lookup.get(key.upper(), match.group(0))

    def replace_bare(match: re.Match[str]) -> str:
        key = match.group(1)
        return lookup.get(key.upper(), match.group(0))

    result = _BRACKET_TOKEN_RE.sub(replace_bracketed, text)
    result = _BARE_TOKEN_RE.sub(replace_bare, result)
    return result
