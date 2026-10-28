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


def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при запуске."""
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                first_name TEXT
            )
            """
        )
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
        # Новые поля добавляем отдельно, чтобы у тех, кто уже пользуется ботом,
        # база обновилась автоматически и задачи не пропали.
        _add_missing_columns(conn, "users", USER_SETTINGS_COLUMNS)


# Настройки пользователя (хранятся в таблице users)
USER_SETTINGS_COLUMNS = {
    "city_name": "TEXT",                       # город для погоды
    "city_lat": "REAL",
    "city_lon": "REAL",
    "weather_time": "TEXT DEFAULT '08:00'",    # во сколько присылать прогноз
    "weather_enabled": "INTEGER DEFAULT 0",    # 1 — присылать каждый день
    "weather_last_sent": "TEXT",               # дата последней отправки
}


def _add_missing_columns(conn, table: str, columns: dict):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


# ---------- Пользователи ----------

def save_user(user_id: int, first_name: str):
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, first_name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name = excluded.first_name",
            (user_id, first_name),
        )


def all_users():
    with connect() as conn:
        return conn.execute("SELECT * FROM users").fetchall()


def get_user(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def update_user(user_id: int, **fields):
    """Меняет настройки пользователя, например: update_user(1, city_name="Москва")."""
    for name in fields:
        if name not in USER_SETTINGS_COLUMNS:
            raise ValueError(f"Неизвестная настройка: {name}")
    sets = ", ".join(f"{name} = ?" for name in fields)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE user_id = ?", (*fields.values(), user_id))


def users_for_weather(hhmm: str, today: str):
    """Пользователи, которым пора отправить прогноз: время совпало, сегодня ещё не отправляли."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM users
            WHERE weather_enabled = 1 AND city_lat IS NOT NULL
              AND weather_time <= ? AND (weather_last_sent IS NULL OR weather_last_sent < ?)
            """,
            (hhmm, today),
        ).fetchall()


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


# ---------- Задачи ----------

def _d(value):
    return value.isoformat() if isinstance(value, date) else value


def _t(value):
    return value.strftime("%H:%M") if isinstance(value, time) else value


def add_task(user_id, title, place, person, due_date, due_time, now: datetime) -> int:
    r1, s1, r2, s2 = calc_reminders(due_date, due_time, now)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks (user_id, title, place, person, due_date, due_time, created_at,
                               remind1_at, remind1_sent, remind2_at, remind2_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, title, place, person, _d(due_date), _t(due_time), _fmt(now),
             r1, s1, r2, s2),
        )
        return cur.lastrowid


def get_task(task_id: int, user_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        ).fetchone()


def get_active_tasks(user_id: int):
    """Все невыполненные задачи: сначала с датой (по порядку), потом без даты."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM tasks WHERE user_id = ? AND status = 'active'
            ORDER BY due_date IS NULL, due_date, due_time IS NULL, due_time, id
            """,
            (user_id,),
        ).fetchall()


def get_done_tasks(user_id: int, limit: int = 30):
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM tasks WHERE user_id = ? AND status = 'done'
            ORDER BY completed_at DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def set_done(task_id: int, user_id: int, now: datetime):
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ? AND user_id = ?",
            (_fmt(now), task_id, user_id),
        )


def set_active(task_id: int, user_id: int):
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'active', completed_at = NULL WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


def delete_task(task_id: int, user_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))


def update_when(task_id: int, user_id: int, due_date, due_time, now: datetime):
    """Перенос задачи: новая дата/время и пересчёт напоминаний."""
    r1, s1, r2, s2 = calc_reminders(due_date, due_time, now)
    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks SET due_date = ?, due_time = ?,
                   remind1_at = ?, remind1_sent = ?, remind2_at = ?, remind2_sent = ?
            WHERE id = ? AND user_id = ?
            """,
            (_d(due_date), _t(due_time), r1, s1, r2, s2, task_id, user_id),
        )


def update_text(task_id: int, user_id: int, title, place, person):
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET title = ?, place = ?, person = ? WHERE id = ? AND user_id = ?",
            (title, place, person, task_id, user_id),
        )


def due_reminders(now: datetime):
    """Задачи, по которым пора отправить напоминание. Возвращает список (задача, номер)."""
    now_s = _fmt(now)
    result = []
    with connect() as conn:
        for num in (1, 2):
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE status = 'active' AND remind{num}_sent = 0 AND remind{num}_at <= ?
                """,
                (now_s,),
            ).fetchall()
            result += [(row, num) for row in rows]
    return result


def mark_reminded(task_id: int, num: int):
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET remind{num}_sent = 1 WHERE id = ?", (task_id,))


def tasks_for_period(user_id: int, start: date, end: date):
    """Все задачи (и выполненные, и нет) со сроком в промежутке [start, end]."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM tasks WHERE user_id = ? AND due_date BETWEEN ? AND ?
            ORDER BY due_date, due_time
            """,
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()


def done_without_date(user_id: int, start: date, end: date):
    """Задачи без срока, выполненные в промежутке [start, end]."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM tasks WHERE user_id = ? AND due_date IS NULL AND status = 'done'
              AND substr(completed_at, 1, 10) BETWEEN ? AND ?
            """,
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
