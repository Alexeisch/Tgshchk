"""
Работа с базой данных (SQLite).

Все задачи хранятся в одном файле tasks.db рядом с ботом.
Даты и время хранятся текстом в формате "ГГГГ-ММ-ДД" и "ЧЧ:ММ",
время напоминаний — "ГГГГ-ММ-ДД ЧЧ:ММ" (в часовом поясе из настроек).
"""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

DB_FILE = "tasks.db"


@contextmanager
def connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # чтобы обращаться к полям по имени: task["title"]
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _all(sql: str, params=()):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def _one(sql: str, params=()):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def _run(sql: str, params=()) -> int:
    """Выполняет изменение и возвращает номер добавленной строки (для INSERT)."""
    with connect() as conn:
        return conn.execute(sql, params).lastrowid


def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при запуске."""
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                title        TEXT NOT NULL,
                place        TEXT,
                person       TEXT,
                due_date     TEXT,
                due_time     TEXT,
                status       TEXT NOT NULL DEFAULT 'active',  -- 'active' или 'done'
                created_at   TEXT NOT NULL,
                completed_at TEXT,
                remind1_at   TEXT,              -- напоминание "за день"
                remind1_sent INTEGER DEFAULT 0,
                remind2_at   TEXT,              -- напоминание "за пару часов"
                remind2_sent INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Новые поля добавляем отдельно, чтобы у тех, кто уже пользуется ботом,
        # база обновилась автоматически и задачи не пропали.
        for table, columns in (("users", USER_SETTINGS_COLUMNS), ("tasks", TASK_EXTRA_COLUMNS)):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


# Настройки пользователя (хранятся в таблице users)
USER_SETTINGS_COLUMNS = {
    "city_name": "TEXT",                       # город для погоды
    "city_lat": "REAL",
    "city_lon": "REAL",
    "weather_time": "TEXT DEFAULT '08:00'",    # во сколько присылать прогноз
    "weather_enabled": "INTEGER DEFAULT 0",    # 1 — присылать каждый день
    "weather_last_sent": "TEXT",               # дата последней отправки
    # Отчёты: еженедельный (по воскресеньям) и ежемесячный (в последний день месяца)
    "report_week_enabled": "INTEGER DEFAULT 1",
    "report_week_time": "TEXT DEFAULT '16:00'",
    "report_week_last_sent": "TEXT",
    "report_month_enabled": "INTEGER DEFAULT 1",
    "report_month_time": "TEXT DEFAULT '20:00'",
    "report_month_last_sent": "TEXT",
}

# Поля задач, добавленные в новых версиях бота
TASK_EXTRA_COLUMNS = {
    "repeat": "TEXT",          # правило повтора ("daily", "weekly:2", ...) или пусто
    "category_id": "INTEGER",  # вкладка (таблица categories) или пусто — общая задача
}

# Поля задачи, которые можно менять через update_task
TASK_EDITABLE = {"title", "place", "person", "repeat", "category_id", "status", "completed_at"}


# ---------- Пользователи ----------

def save_user(user_id: int, first_name: str):
    _run("INSERT INTO users (user_id, first_name) VALUES (?, ?) "
         "ON CONFLICT(user_id) DO UPDATE SET first_name = excluded.first_name", (user_id, first_name))


def get_user(user_id: int):
    return _one("SELECT * FROM users WHERE user_id = ?", (user_id,))


def update_user(user_id: int, **fields):
    """Меняет настройки пользователя, например: update_user(1, city_name="Москва")."""
    unknown = set(fields) - set(USER_SETTINGS_COLUMNS)
    if unknown:
        raise ValueError(f"Неизвестная настройка: {unknown}")
    sets = ", ".join(f"{name} = ?" for name in fields)
    _run(f"UPDATE users SET {sets} WHERE user_id = ?", (*fields.values(), user_id))


def users_due(prefix: str, hhmm: str, today: str, extra: str = ""):
    """
    Пользователи, которым пора что-то прислать: рассылка включена, её время наступило,
    а сегодня ещё не отправляли. prefix — "weather", "report_week" или "report_month".
    """
    assert prefix in ("weather", "report_week", "report_month")
    return _all(f"SELECT * FROM users WHERE {prefix}_enabled = 1 AND {prefix}_time <= ? "
                f"AND ({prefix}_last_sent IS NULL OR {prefix}_last_sent < ?) {extra}", (hhmm, today))


# ---------- Напоминания ----------

def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def calc_reminders(due_date, due_time, now: datetime):
    """
    Считает, когда напомнить о задаче.
      - Если время указано: за 24 часа и за 2 часа до начала.
      - Если указана только дата: накануне в 10:00 и в сам день в 09:00.
    Напоминание, время которого уже прошло, сразу помечается отправленным.
    Возвращает (remind1_at, remind1_sent, remind2_at, remind2_sent).
    """
    if due_date is None:
        return None, 1, None, 1
    if due_time is not None:
        start = datetime.combine(due_date, due_time)
        r1, r2 = start - timedelta(days=1), start - timedelta(hours=2)
    else:
        r1 = datetime.combine(due_date - timedelta(days=1), time(10, 0))
        r2 = datetime.combine(due_date, time(9, 0))
    now = now.replace(tzinfo=None)
    return _fmt(r1), int(r1 <= now), _fmt(r2), int(r2 <= now)


def due_reminders(now: datetime):
    """Задачи, по которым пора отправить напоминание. Возвращает список (задача, номер)."""
    return [(row, num) for num in (1, 2) for row in _all(
        f"SELECT * FROM tasks WHERE status = 'active' AND remind{num}_sent = 0 AND remind{num}_at <= ?",
        (_fmt(now),))]


def mark_reminded(task_id: int, num: int):
    _run(f"UPDATE tasks SET remind{num}_sent = 1 WHERE id = ?", (task_id,))


# ---------- Задачи ----------

def _d(value):
    return value.isoformat() if isinstance(value, date) else value


def _t(value):
    return value.strftime("%H:%M") if isinstance(value, time) else value


def add_task(user_id, title, place, person, due_date, due_time, now: datetime, repeat=None,
             category_id=None) -> int:
    return _run(
        """
        INSERT INTO tasks (user_id, title, place, person, due_date, due_time, created_at,
                           remind1_at, remind1_sent, remind2_at, remind2_sent, repeat, category_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, title, place, person, _d(due_date), _t(due_time), _fmt(now),
         *calc_reminders(due_date, due_time, now), repeat, category_id),
    )


def get_task(task_id: int, user_id: int):
    return _one("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))


def update_task(task_id: int, user_id: int, **fields):
    """Меняет поля задачи, например: update_task(5, 1, repeat=None, category_id=2)."""
    unknown = set(fields) - TASK_EDITABLE
    if unknown:
        raise ValueError(f"Неизвестное поле задачи: {unknown}")
    sets = ", ".join(f"{name} = ?" for name in fields)
    _run(f"UPDATE tasks SET {sets} WHERE id = ? AND user_id = ?", (*fields.values(), task_id, user_id))


def set_done(task_id: int, user_id: int, now: datetime):
    update_task(task_id, user_id, status="done", completed_at=_fmt(now))


def set_active(task_id: int, user_id: int):
    update_task(task_id, user_id, status="active", completed_at=None)


def update_when(task_id: int, user_id: int, due_date, due_time, now: datetime):
    """Перенос задачи: новая дата/время и пересчёт напоминаний. Без срока повтор снимается."""
    _run(
        """
        UPDATE tasks SET due_date = ?, due_time = ?,
               remind1_at = ?, remind1_sent = ?, remind2_at = ?, remind2_sent = ?,
               repeat = CASE WHEN ? IS NULL THEN NULL ELSE repeat END
        WHERE id = ? AND user_id = ?
        """,
        (_d(due_date), _t(due_time), *calc_reminders(due_date, due_time, now), _d(due_date), task_id, user_id),
    )


def delete_task(task_id: int, user_id: int):
    _run("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))


def get_active_tasks(user_id: int, category_id: int = None):
    """
    Невыполненные задачи: сначала с датой (по порядку), потом без даты.
    category_id=None — все задачи, иначе только задачи этой вкладки.
    """
    where = "" if category_id is None else "AND category_id = ?"
    params = (user_id,) if category_id is None else (user_id, category_id)
    return _all(f"SELECT * FROM tasks WHERE user_id = ? AND status = 'active' {where} "
                "ORDER BY due_date IS NULL, due_date, due_time IS NULL, due_time, id", params)


def get_done_tasks(user_id: int, limit: int = 30):
    return _all("SELECT * FROM tasks WHERE user_id = ? AND status = 'done' ORDER BY completed_at DESC LIMIT ?",
                (user_id, limit))


def tasks_for_period(user_id: int, start: date, end: date):
    """Все задачи (и выполненные, и нет) со сроком в промежутке [start, end]."""
    return _all("SELECT * FROM tasks WHERE user_id = ? AND due_date BETWEEN ? AND ? ORDER BY due_date, due_time",
                (user_id, start.isoformat(), end.isoformat()))


def done_without_date(user_id: int, start: date, end: date):
    """Задачи без срока, выполненные в промежутке [start, end]."""
    return _all("SELECT * FROM tasks WHERE user_id = ? AND due_date IS NULL AND status = 'done' "
                "AND substr(completed_at, 1, 10) BETWEEN ? AND ?", (user_id, start.isoformat(), end.isoformat()))


# ---------- Вкладки ----------

def get_categories(user_id: int):
    return _all("SELECT * FROM categories WHERE user_id = ? ORDER BY id", (user_id,))


def get_category(category_id: int, user_id: int):
    return _one("SELECT * FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))


def find_category(user_id: int, name: str):
    """Вкладка с таким названием без учёта регистра (сравниваем в Python:
    в SQLite lower() не понимает русские буквы) или None."""
    return next((c for c in get_categories(user_id) if c["name"].lower() == name.lower()), None)


def add_category(user_id: int, name: str, now: datetime) -> int:
    return _run("INSERT INTO categories (user_id, name, created_at) VALUES (?, ?, ?)", (user_id, name, _fmt(now)))


def rename_category(category_id: int, user_id: int, name: str):
    _run("UPDATE categories SET name = ? WHERE id = ? AND user_id = ?", (name, category_id, user_id))


def delete_category(category_id: int, user_id: int, with_tasks: bool):
    """Удаляет вкладку. Её задачи удаляются или становятся общими (with_tasks=False)."""
    with connect() as conn:
        if with_tasks:
            conn.execute("DELETE FROM tasks WHERE category_id = ? AND user_id = ?", (category_id, user_id))
        else:
            conn.execute("UPDATE tasks SET category_id = NULL WHERE category_id = ? AND user_id = ?",
                         (category_id, user_id))
        conn.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))


def count_active_by_category(user_id: int) -> dict:
    """{id вкладки: число текущих задач}; ключ None — общие задачи."""
    rows = _all("SELECT category_id, COUNT(*) AS n FROM tasks WHERE user_id = ? AND status = 'active' "
                "GROUP BY category_id", (user_id,))
    return {r["category_id"]: r["n"] for r in rows}
