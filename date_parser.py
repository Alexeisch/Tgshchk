"""
Разбор текста задачи на русском языке.

Пример: "6 числа встреча по адресу ул. Ленина 5 с Иваном Петровым в 15:00"
  -> название: "Встреча"
     дата:     6-е число текущего месяца (или следующего, если 6-е уже прошло)
     время:    15:00
     место:    "ул. Ленина 5"
     человек:  "с Иваном Петровым"
"""

import re
from datetime import date, datetime, time, timedelta

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
MONTH_RE = (r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[яй]|июн[ья]|июл[ья]|августа?|"
            r"сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])")
# Номер месяца по первым трём буквам ("ноября" -> "ноя" -> 11); у мая две формы
MONTH_NUM = {m[:3]: i for i, m in enumerate(MONTHS_GEN, 1)} | {"май": 5}

# Дни недели во всех нужных формах: "в пятницу", "к пятнице", "до пятницы", "по пятницам"
_WEEKDAY_FORMS = [
    ("понедельник", "понедельнику", "понедельника", "понедельникам"),
    ("вторник", "вторнику", "вторника", "вторникам"),
    ("среду|среда", "среде", "среды", "средам"),
    ("четверг", "четвергу", "четверга", "четвергам"),
    ("пятницу|пятница", "пятнице", "пятницы", "пятницам"),
    ("субботу|суббота", "субботе", "субботы", "субботам"),
    ("воскресенье", "воскресенью", "воскресенья", "воскресеньям"),
]
WEEKDAY_NUM = {form: i for i, forms in enumerate(_WEEKDAY_FORMS) for group in forms for form in group.split("|")}
_ACC = "|".join(f[0] for f in _WEEKDAY_FORMS)    # понедельник|вторник|среду|среда|...
_DAT = "|".join(f[1] for f in _WEEKDAY_FORMS)    # понедельнику|вторнику|среде|...
_GEN = "|".join(f[2] for f in _WEEKDAY_FORMS)    # понедельника|вторника|среды|...
_PLURAL = "|".join(f[3] for f in _WEEKDAY_FORMS)  # понедельникам|вторникам|...


def _safe_date(year: int, month: int, day: int):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add_month(d: date) -> tuple:
    return (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)


def _month_day(year: int, month: int, day: int) -> date:
    """Дата с поправкой на короткие месяцы: 31 число в феврале -> 28 (29)."""
    while not _safe_date(year, month, day):
        day -= 1
    return date(year, month, day)


def _cut(text: str, match) -> str:
    """Вырезает найденный фрагмент из текста."""
    return text[: match.start()] + " " + text[match.end():]


def _weekday_ahead(today: date, weekday: int, allow_today: bool) -> date:
    """Ближайший такой день недели: начиная с сегодня (allow_today) или строго после."""
    days = (weekday - today.weekday()) % 7
    return today + timedelta(days=days or (0 if allow_today else 7))


# ---------- Время ----------

def extract_time(text: str):
    """Ищет время. Возвращает (time или None, текст без найденного фрагмента)."""
    # "в 15:00", "к 9:30", "15:00", "в 15.30"
    for pattern in (r"(?:\b(?:в|к|на)\s+)?\b(\d{1,2}):(\d{2})\b", r"\b(?:в|к)\s+(\d{1,2})\.(\d{2})\b"):
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            hour, minute = int(m.group(1)), int(m.group(2))
            if hour <= 23 and minute <= 59:
                return time(hour, minute), _cut(text, m)

    # "в 8 вечера", "в 3 часа дня", "в 10 утра", "в 15 часов", "в 15ч", "в 15"
    # (только с "в"/"к": "на 3 дня" — это не время)
    for m in re.finditer(r"\b(?:в|к)\s+(\d{1,2})(?!\d)\s*(ч\b|час(?:а|ов)?\b)?\s*(утра|дня|вечера|ночи)?",
                         text, flags=re.IGNORECASE):
        if not (m.group(2) or m.group(3)):
            # "в 15" без уточнения — время, только если дальше не идёт месяц/"числа" ("в 15 ноября")
            rest = text[m.end():].lstrip().lower()
            if re.match(MONTH_RE + r"|числа|-?го\b|\.|/", rest):
                continue
        hour = int(m.group(1))
        part = (m.group(3) or "").lower()
        if part in ("дня", "вечера") and hour < 12:
            hour += 12
        elif part == "ночи" and hour == 12:
            hour = 0
        if hour <= 23:
            return time(hour, 0), _cut(text, m)

    for pattern, value in ((r"\bв\s+полдень\b", time(12, 0)), (r"\bв\s+полночь\b", time(0, 0))):
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return value, _cut(text, m)
    return None, text


# ---------- Дата ----------

# Относительные даты: (шаблон, функция(найденное, сегодня) -> дата)
_RELATIVE = [
    (r"послезавтра", lambda m, t: t + timedelta(days=2)),
    (r"завтра", lambda m, t: t + timedelta(days=1)),
    (r"сегодня", lambda m, t: t),
    (r"через\s+(\d+)\s+(?:день|дня|дней)", lambda m, t: t + timedelta(days=int(m.group(1)))),
    (r"через\s+(\d+)\s+(?:неделю|недели|недель)", lambda m, t: t + timedelta(weeks=int(m.group(1)))),
    (r"через\s+неделю", lambda m, t: t + timedelta(weeks=1)),
    (r"через\s+месяц", lambda m, t: _month_day(*_add_month(t), t.day)),
]


def _day_month(today: date, day: int, month: int, year):
    """Дата из числа и месяца. Если год не указан и дата уже прошла — берём следующий год."""
    d = _safe_date(year or today.year, month, day)
    if d and not year and d < today:
        d = _safe_date(today.year + 1, month, day) or d
    return d


def extract_date(text: str, today: date):
    """Ищет дату. Возвращает (date или None, текст без найденного фрагмента)."""
    low = text.lower()

    for pattern, make in _RELATIVE:
        m = re.search(rf"\b{pattern}\b", low)
        if m:
            return make(m, today), _cut(text, m)

    # "06.11.2026", "6.11", "6/11"
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", low)
    if m:
        year = int(m.group(3)) if m.group(3) else None
        d = _day_month(today, int(m.group(1)), int(m.group(2)), year + 2000 if year and year < 100 else year)
        if d:
            return d, _cut(text, m)

    # "6 ноября", "6-го ноября 2027"
    m = re.search(r"\b(\d{1,2})(?:-?го|-?е)?\s+" + MONTH_RE + r"(?:\s+(\d{4}))?(?:\s*года?)?", low)
    if m:
        d = _day_month(today, int(m.group(1)), MONTH_NUM[m.group(2)[:3]], int(m.group(3)) if m.group(3) else None)
        if d:
            return d, _cut(text, m)

    # "6 числа", "6-го числа" (+ необязательно "этого/следующего месяца")
    m = re.search(r"\b(\d{1,2})(?:-?го|-?е)?\s+числа(?:\s+(этого|текущего|следующего)\s+месяца)?", low)
    if m:
        day, which = int(m.group(1)), m.group(2)
        d = None if which == "следующего" else _safe_date(today.year, today.month, day)
        if d is None or (d < today and which is None):
            d = _safe_date(*_add_month(today), day)
        if d:
            return d, _cut(text, m)

    # "в пятницу", "во вторник", "к пятнице", "до понедельника" — ближайший такой день впереди
    m = re.search(rf"\b(?:(?:в|во)\s+({_ACC})|(?:к|ко)\s+({_DAT})|до\s+({_GEN}))\b", low)
    if m:
        weekday = WEEKDAY_NUM[next(g for g in m.groups() if g)]
        return _weekday_ahead(today, weekday, allow_today=False), _cut(text, m)

    return None, text


# ---------- Место и человек ----------

def extract_place(text: str):
    m = re.search(
        # Запятая не обрывает адрес, если дальше идёт "кв", "д.", "корп", "офис", цифра и т.п.
        r"\b(?:по\s+адресу|адрес:?)\s+(.+?)"
        r"(?=\s+с\s+[А-ЯЁA-Z]|\s+со\s+[А-ЯЁA-Z]|[;!]|,(?!\s*(?:кв|д\b|д\.|дом|корп|стр|офис|подъезд|этаж|\d))|$)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(" .,"), _cut(text, m)
    return None, text


def extract_person(text: str):
    # "с Иваном", "с Иваном Петровым", "со Светланой" — имя с заглавной буквы
    m = re.search(r"\b(?:с|со)\s+([А-ЯЁA-Z][а-яёa-z\-]+(?:\s+[А-ЯЁA-Z][а-яёa-z\-]+)?)", text)
    if m:
        return m.group(0).strip(), _cut(text, m)  # сохраняем вместе с "с": "с Иваном"
    return None, text


def _clean_title(text: str) -> str:
    text = re.sub(r"(^|\s)(у\s+меня|запланирована?|запланировано|запланированы)(?=\s|$)", " ", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.!?;:])", r"\1", re.sub(r"\s+", " ", text)).strip(" ,.;:-—")
    return text[:1].upper() + text[1:]


# ---------- Повторяющиеся задачи ----------
#
# Правило повтора хранится строкой:
#   "daily"          — каждый день
#   "weekdays"       — по будням (пн–пт)
#   "weekly:0"       — каждую неделю, по понедельникам (0 = пн ... 6 = вс)
#   "monthly:5"      — каждый месяц, 5 числа
#   "yearly:11-06"   — каждый год, 6 ноября

REPEAT_KINDS = {
    "daily": "Каждый день",
    "weekdays": "По будням",
    "weekly": "Каждую неделю",
    "monthly": "Каждый месяц",
    "yearly": "Каждый год",
}

WEEKDAY_EVERY = ["каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг",
                 "каждую пятницу", "каждую субботу", "каждое воскресенье"]


def make_repeat(kind: str, start: date):
    """Превращает вид повтора ("weekly") и дату первого раза в правило ("weekly:2")."""
    return {
        "daily": "daily",
        "weekdays": "weekdays",
        "weekly": f"weekly:{start.weekday()}",
        "monthly": f"monthly:{start.day}",
        "yearly": f"yearly:{start.month:02d}-{start.day:02d}",
    }.get(kind)


def repeat_label(repeat) -> str:
    """Человекочитаемое описание правила: "каждую среду", "каждый месяц, 5 числа"."""
    kind, _, arg = (repeat or "").partition(":")
    if kind == "weekly":
        return WEEKDAY_EVERY[int(arg)]
    if kind == "monthly":
        return f"каждый месяц, {arg} числа"
    if kind == "yearly":
        month, day = (int(x) for x in arg.split("-"))
        return f"каждый год, {day} {MONTHS_GEN[month - 1]}"
    return {"daily": "каждый день", "weekdays": "по будням"}.get(kind, "")


def next_date(repeat: str, after: date) -> date:
    """Первая дата по правилу повтора строго после даты after."""
    kind, _, arg = repeat.partition(":")
    if kind == "daily":
        return after + timedelta(days=1)
    if kind == "weekdays":
        return after + timedelta(days={4: 3, 5: 2}.get(after.weekday(), 1))  # пт и сб -> понедельник
    if kind == "weekly":
        return after + timedelta(days=(int(arg) - after.weekday() - 1) % 7 + 1)
    if kind == "monthly":
        d = _month_day(after.year, after.month, int(arg))
        return d if d > after else _month_day(*_add_month(after), int(arg))
    if kind == "yearly":
        month, day = (int(x) for x in arg.split("-"))
        d = _month_day(after.year, month, day)
        return d if d > after else _month_day(after.year + 1, month, day)
    raise ValueError(f"Неизвестное правило повтора: {repeat}")


def next_occurrence(repeat: str, due: date, today: date) -> date:
    """Следующий раз после due, но не раньше сегодняшнего дня (если задачу закрыли с опозданием)."""
    d = next_date(repeat, due)
    while d < today:
        d = next_date(repeat, d)
    return d


def first_occurrence(repeat: str, today: date) -> date:
    """Ближайший подходящий день начиная с сегодняшнего."""
    return next_date(repeat, today - timedelta(days=1))


_SIMPLE_REPEATS = [
    (r"каждый\s+день|ежедневно", "daily"),
    (r"по\s+будням|каждый\s+будний\s+день|в\s+будни", "weekdays"),
    (r"каждую\s+неделю|еженедельно", "weekly"),
    (r"каждый\s+месяц|ежемесячно", "monthly"),
    (r"каждый\s+год|ежегодно", "yearly"),
]


def extract_repeat(text: str, today: date):
    """
    Ищет слова о повторе: "каждый день", "по будням", "каждую среду", "каждое 5 число",
    "ежемесячно" и т.п. Возвращает (вид повтора, подсказка даты или None, текст без фрагмента).
    """
    low = text.lower()
    for pattern, kind in _SIMPLE_REPEATS:
        m = re.search(rf"\b(?:{pattern})\b", low)
        if m:
            return kind, None, _cut(text, m)

    # "каждый понедельник", "каждую среду", "по пятницам"
    m = re.search(rf"\b(?:(?:каждый|каждую|каждое)\s+({_ACC})|по\s+({_PLURAL}))\b", low)
    if m:
        start = _weekday_ahead(today, WEEKDAY_NUM[m.group(1) or m.group(2)], allow_today=True)
        return "weekly", start, _cut(text, m)

    # "каждое 5 число", "каждого 5-го числа"
    m = re.search(r"\b(?:каждое|каждого)\s+(\d{1,2})(?:-?го|-?е)?\s+числ[оа]\b", low)
    if m and 1 <= int(m.group(1)) <= 31:
        day = int(m.group(1))
        hint = _month_day(today.year, today.month, day)
        return "monthly", hint if hint >= today else _month_day(*_add_month(today), day), _cut(text, m)

    return None, None, text


# ---------- Главная функция ----------

def parse_task(text: str, now: datetime) -> dict:
    """Разбирает сообщение пользователя на части."""
    original = text.strip()
    naive_now = now.replace(tzinfo=None)

    due_time, rest = extract_time(original)
    repeat_kind, repeat_hint, rest = extract_repeat(rest, now.date())
    due_date, rest = extract_date(rest, now.date())
    place, rest = extract_place(rest)
    person, rest = extract_person(rest)

    repeat = None
    if repeat_kind:
        explicit_date = due_date is not None
        if not due_date:
            # "каждую неделю"/"каждый месяц"/"каждый год" без даты — начинаем с сегодняшнего дня
            due_date = repeat_hint or (
                now.date() if repeat_kind in ("weekly", "monthly", "yearly")
                else first_occurrence(repeat_kind, now.date())
            )
        repeat = make_repeat(repeat_kind, due_date)
        # Если дата не названа явно, а время сегодня уже прошло — первый раз будет в следующий подходящий день
        if not explicit_date and due_time and datetime.combine(due_date, due_time) <= naive_now:
            due_date = next_date(repeat, due_date)

    # Если указано только время — считаем, что это сегодня (или завтра, если время уже прошло)
    if due_time and not due_date:
        due_date = now.date()
        if datetime.combine(due_date, due_time) <= naive_now:
            due_date += timedelta(days=1)

    return {
        "title": _clean_title(rest) or original,
        "due_date": due_date,
        "due_time": due_time,
        "place": place,
        "person": person,
        "repeat": repeat,
    }


def parse_when(text: str, now: datetime):
    """Разбирает только дату/время (используется при переносе задачи)."""
    due_time, rest = extract_time(text)
    due_date, _ = extract_date(rest, now.date())
    return due_date, due_time
