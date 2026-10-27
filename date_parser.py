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

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
MONTH_RE = (
    r"(января|январь|февраля|февраль|марта|март|апреля|апрель|мая|май|июня|июнь|"
    r"июля|июль|августа|август|сентября|сентябрь|октября|октябрь|ноября|ноябрь|"
    r"декабря|декабрь)"
)

WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3,
    "пятницу": 4, "пятница": 4, "субботу": 5, "суббота": 5,
    "воскресенье": 6,
}
WEEKDAY_RE = r"(?:в|во)\s+(понедельник|вторник|среду|среда|четверг|пятницу|пятница|субботу|суббота|воскресенье)"


def _month_number(word: str) -> int:
    word = word.lower()
    # "мая"/"май" проверяем отдельно, чтобы не спутать с "март"
    if word in ("мая", "май"):
        return 5
    for stem, num in MONTHS.items():
        if stem != "ма" and word.startswith(stem):
            return num
    return 0


def _safe_date(year: int, month: int, day: int):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add_month(d: date) -> tuple:
    return (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)


def _cut(text: str, match) -> str:
    """Вырезает найденный фрагмент из текста."""
    return text[: match.start()] + " " + text[match.end():]


def extract_time(text: str):
    """Ищет время. Возвращает (time или None, текст без найденного фрагмента)."""
    patterns = [
        # "в 15:00", "к 9:30", "15:00", "в 15.30"
        (r"(?:\b(?:в|к|на)\s+)?\b(\d{1,2})[:](\d{2})\b", None),
        (r"\b(?:в|к|на)\s+(\d{1,2})\.(\d{2})\b", None),
        # "в 8 вечера", "в 3 часа дня", "в 10 утра", "в 15 часов", "в 15ч"
        (r"\b(?:в|к|на)\s+(\d{1,2})\s*(?:ч\b|час(?:а|ов)?\b)?\s*(утра|дня|вечера|ночи)?", "hours"),
        (r"\bв\s+полдень\b", "noon"),
        (r"\bв\s+полночь\b", "midnight"),
    ]
    for pattern, kind in patterns:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            if kind == "noon":
                return time(12, 0), _cut(text, m)
            if kind == "midnight":
                return time(0, 0), _cut(text, m)
            if kind == "hours":
                # Считаем это временем, только если явно сказано "час/ч" или "утра/вечера"
                whole = m.group(0).lower()
                has_word = re.search(r"ч|утра|дня|вечера|ночи", whole[2:])
                if not has_word:
                    # "в 15" без уточнения — тоже время, если дальше не идёт название месяца/"числа"
                    rest = text[m.end():].lstrip().lower()
                    if re.match(MONTH_RE + r"|числа|-?го\b|\.|/", rest):
                        continue
                hour = int(m.group(1))
                part = (m.group(2) or "").lower()
                if part in ("дня", "вечера") and hour < 12:
                    hour += 12
                if part == "ночи" and hour == 12:
                    hour = 0
                if 0 <= hour <= 23:
                    return time(hour, 0), _cut(text, m)
                continue
            hour, minute = int(m.group(1)), int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour, minute), _cut(text, m)
    return None, text


def extract_date(text: str, today: date):
    """Ищет дату. Возвращает (date или None, текст без найденного фрагмента)."""
    low = text.lower()

    m = re.search(r"\bпослезавтра\b", low)
    if m:
        return today + timedelta(days=2), _cut(text, m)
    m = re.search(r"\bзавтра\b", low)
    if m:
        return today + timedelta(days=1), _cut(text, m)
    m = re.search(r"\bсегодня\b", low)
    if m:
        return today, _cut(text, m)

    # "через 3 дня", "через неделю", "через месяц"
    m = re.search(r"\bчерез\s+(\d+)\s+(день|дня|дней)\b", low)
    if m:
        return today + timedelta(days=int(m.group(1))), _cut(text, m)
    m = re.search(r"\bчерез\s+(\d+)\s+(неделю|недели|недель)\b", low)
    if m:
        return today + timedelta(weeks=int(m.group(1))), _cut(text, m)
    m = re.search(r"\bчерез\s+неделю\b", low)
    if m:
        return today + timedelta(weeks=1), _cut(text, m)
    m = re.search(r"\bчерез\s+месяц\b", low)
    if m:
        y, mo = _add_month(today)
        d = _safe_date(y, mo, today.day) or _safe_date(y, mo, 28)
        return d, _cut(text, m)

    # "06.11.2026", "6.11", "6/11"
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        d = _safe_date(year, month, day)
        if d:
            if not m.group(3) and d < today:
                d = _safe_date(year + 1, month, day) or d
            return d, _cut(text, m)

    # "6 ноября", "6-го ноября 2027"
    m = re.search(r"\b(\d{1,2})(?:-?го|-?е)?\s+" + MONTH_RE + r"(?:\s+(\d{4}))?(?:\s*года?)?", low)
    if m:
        day, month = int(m.group(1)), _month_number(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        d = _safe_date(year, month, day)
        if d:
            if not m.group(3) and d < today:
                d = _safe_date(year + 1, month, day) or d
            return d, _cut(text, m)

    # "6 числа", "6-го числа" (+ необязательно "этого/следующего месяца")
    m = re.search(
        r"\b(\d{1,2})(?:-?го|-?е)?\s+числа(?:\s+(этого|текущего|следующего)\s+месяца)?", low
    )
    if m:
        day = int(m.group(1))
        which = m.group(2)
        if which == "следующего":
            y, mo = _add_month(today)
            d = _safe_date(y, mo, day)
        else:
            d = _safe_date(today.year, today.month, day)
            if d is None or (d < today and which is None):
                y, mo = _add_month(today)
                d = _safe_date(y, mo, day)
        if d:
            return d, _cut(text, m)

    # "в пятницу", "во вторник" — ближайший такой день впереди
    m = re.search(WEEKDAY_RE, low)
    if m:
        target = WEEKDAYS[m.group(1)]
        days_ahead = (target - today.weekday()) % 7 or 7
        return today + timedelta(days=days_ahead), _cut(text, m)

    return None, text


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
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"(^|\s)(у\s+меня|запланирована?|запланировано|запланированы)(?=\s|$)", " ", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-—")
    return text[:1].upper() + text[1:] if text else text


def parse_task(text: str, now: datetime) -> dict:
    """Главная функция: разбирает сообщение пользователя на части."""
    original = text.strip()
    rest = original

    due_time, rest = extract_time(rest)
    due_date, rest = extract_date(rest, now.date())
    place, rest = extract_place(rest)
    person, rest = extract_person(rest)

    # Если указано только время — считаем, что это сегодня (или завтра, если время уже прошло)
    if due_time and not due_date:
        due_date = now.date()
        if datetime.combine(due_date, due_time) <= now.replace(tzinfo=None):
            due_date += timedelta(days=1)

    title = _clean_title(rest) or original
    return {
        "title": title,
        "due_date": due_date,
        "due_time": due_time,
        "place": place,
        "person": person,
    }


def parse_when(text: str, now: datetime):
    """Разбирает только дату/время (используется при переносе задачи)."""
    due_time, rest = extract_time(text)
    due_date, rest = extract_date(rest, now.date())
    return due_date, due_time
