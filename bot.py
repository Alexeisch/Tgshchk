"""
Telegram-бот «Планировщик задач».

Запуск:  python bot.py   (или кнопка ▶ в VS Code)
Настройки (токен, часовой пояс) берутся из файла .env — см. README.md.
"""

import html
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
import weather
from date_parser import parse_task, parse_when

# ---------- Настройки ----------

load_dotenv()  # читает файл .env
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip())
# Необязательно: ID пользователей через запятую, которым разрешено пользоваться ботом
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

WEEKDAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTH_NAMES = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря"]

# Состояния: чего бот ждёт от пользователя следующим сообщением
WAIT_NEW_TASK = "wait_new_task"
WAIT_DRAFT_WHEN = "wait_draft_when"
WAIT_RESCHEDULE = "wait_reschedule"
WAIT_EDIT_TEXT = "wait_edit_text"
WAIT_CITY = "wait_city"
WAIT_WEATHER_TIME = "wait_weather_time"


def now() -> datetime:
    return datetime.now(TZ)


# ---------- Форматирование ----------

def fmt_date(d) -> str:
    if not d:
        return "без срока"
    if isinstance(d, str):
        d = date.fromisoformat(d)
    today = now().date()
    if d == today:
        prefix = "сегодня, "
    elif d == today + timedelta(days=1):
        prefix = "завтра, "
    else:
        prefix = ""
    year = f" {d.year}" if d.year != today.year else ""
    return f"{prefix}{d.day} {MONTH_NAMES[d.month - 1]}{year} ({WEEKDAY_NAMES[d.weekday()]})"


def fmt_time(t) -> str:
    if not t:
        return ""
    return t if isinstance(t, str) else t.strftime("%H:%M")


def is_overdue(task) -> bool:
    if not task["due_date"] or task["status"] != "active":
        return False
    n = now()
    if task["due_time"]:
        due = datetime.fromisoformat(f"{task['due_date']} {task['due_time']}")
        return due < n.replace(tzinfo=None)
    return date.fromisoformat(task["due_date"]) < n.date()


def header(title: str, subtitle: str = "") -> str:
    """Заголовок сообщения: жирные заглавные буквы и, при желании, подзаголовок курсивом."""
    text = f"<b>{title.upper()}</b>"
    return text + f"\n<i>{subtitle}</i>" if subtitle else text


def task_card(title, place, person, due_date, due_time, extra="") -> str:
    """Описание задачи: название и детали в блоке-цитате (с серой полосой слева)."""
    e = html.escape
    when = fmt_date(due_date)
    if due_time:
        when += f" · {fmt_time(due_time)}"
    details = [f"Когда — {when}"]
    if place:
        details.append(f"Где — {e(place)}")
    if person:
        # "с Иваном Петровым" -> "С кем — Иваном Петровым"
        name = person.split(" ", 1)[1] if person.lower().startswith(("с ", "со ")) else person
        details.append(f"С кем — {e(name)}")
    text = f"<b>{e(title)}</b>\n<blockquote>" + "\n".join(details) + "</blockquote>"
    return text + "\n" + extra if extra else text


def card_from_row(task) -> str:
    extra = ""
    if task["status"] == "done":
        done_at = datetime.strptime(task["completed_at"], "%Y-%m-%d %H:%M")
        extra = f"<i>Выполнено {done_at.strftime('%d.%m.%Y в %H:%M')}</i>"
    elif is_overdue(task):
        extra = "<b>Срок истёк.</b>"
    return task_card(task["title"], task["place"], task["person"],
                     task["due_date"], task["due_time"], extra)


def short_label(task) -> str:
    """Короткая подпись задачи для кнопки в списке."""
    parts = []
    if is_overdue(task):
        parts.append("!")
    if task["due_date"]:
        d = date.fromisoformat(task["due_date"])
        parts.append(d.strftime("%d.%m"))
    if task["due_time"]:
        parts.append(task["due_time"])
    label = " ".join(parts)
    label = f"{label} — {task['title']}" if label else task["title"]
    return label if len(label) <= 60 else label[:57] + "…"


# ---------- Клавиатуры (кнопки) ----------

def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


MAIN_MENU = kb([
    [("+  Новая задача", "new")],
    [("≡  Текущие задачи", "list:all")],
    [("✓  Выполненные", "done_list"), ("⋯  Настройки", "settings")],
])

BACK_TO_MENU = kb([[("⌂  Главное меню", "menu")]])


def draft_keyboard():
    return kb([
        [("✓  Сохранить", "draft_save")],
        [("Дата и время", "draft_when"), ("✎  Заново", "new")],
        [("✕  Отмена", "menu")],
    ])


def task_keyboard(task):
    tid = task["id"]
    if task["status"] == "done":
        return kb([
            [("↺  Вернуть в текущие", f"undone:{tid}")],
            [("✕  Удалить", f"del:{tid}")],
            [("←  Выполненные", "done_list"), ("⌂  Меню", "menu")],
        ])
    return kb([
        [("✓  Выполнено", f"done:{tid}")],
        [("→  Перенести", f"resched:{tid}"), ("✎  Изменить", f"edit:{tid}")],
        [("✕  Удалить", f"del:{tid}")],
        [("←  К списку", "list:all"), ("⌂  Меню", "menu")],
    ])


# ---------- Вспомогательное ----------

def allowed(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED_USERS or (user and user.id in ALLOWED_USERS)


def greeting(update: Update) -> str:
    name = html.escape(update.effective_user.first_name or "")
    return (f"<b>Здравствуйте, {name}.</b>\n\n"
            "Планировщик задач. Выберите раздел или просто напишите задачу сообщением.\n\n"
            "<blockquote>6 числа встреча по адресу ул. Ленина 5 с Иваном в 15:00</blockquote>")


async def reply_or_edit(update: Update, text: str, markup=None):
    """Если нажали кнопку — меняем текущее сообщение, иначе отправляем новое."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
            return
        except Exception:
            pass  # например, сообщение слишком старое — тогда просто отправим новое
    await update.effective_chat.send_message(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def answer_query(update: Update, text: str = None):
    """Отвечает на нажатие кнопки. Если нажатие старое (бот был выключен), Telegram
    его уже не принимает — просто пропускаем, чтобы не было ошибки в терминале."""
    try:
        await update.callback_query.answer(text)
    except BadRequest:
        pass


# ---------- Главное меню ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ к этому боту ограничен.")
        return
    db.save_user(update.effective_user.id, update.effective_user.first_name or "")
    context.user_data.clear()
    await reply_or_edit(update, greeting(update), MAIN_MENU)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply_or_edit(update, greeting(update), MAIN_MENU)


# ---------- Создание задачи ----------

async def ask_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = WAIT_NEW_TASK
    text = (
        header("Новая задача") + "\n\n"
        "Опишите задачу одним сообщением. Дату, время, место и человека бот распознает сам.\n\n"
        "<blockquote>6 числа встреча по адресу ул. Ленина 5 с Иваном Петровым в 15:00\n"
        "Завтра в 10 утра позвонить маме\n"
        "15.11 сдать отчёт\n"
        "В пятницу в 19:00 ужин со Светланой\n"
        "Купить продукты</blockquote>"
    )
    await reply_or_edit(update, text, kb([[("✕  Отмена", "menu")]]))


async def show_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["draft"]
    extra = ""
    if d["due_date"] and d["due_date"] < now().date():
        extra = "<i>Эта дата уже прошла. Если это ошибка — нажмите «Дата и время».</i>"
    elif not d["due_date"]:
        extra = "<i>Без срока — напоминаний не будет.</i>"
    text = header("Проверьте задачу") + "\n\n" + task_card(
        d["title"], d["place"], d["person"], d["due_date"], d["due_time"], extra
    )
    await reply_or_edit(update, text, draft_keyboard())


async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.get("draft")
    if not d:
        await show_menu(update, context)
        return
    db.add_task(update.effective_user.id, d["title"], d["place"], d["person"],
                d["due_date"], d["due_time"], now())
    context.user_data.clear()
    note = ""
    if d["due_date"]:
        note = "\n<i>Напоминания — за сутки и за два часа.</i>"
    await reply_or_edit(
        update,
        header("Задача сохранена") + "\n\n"
        + task_card(d["title"], d["place"], d["person"], d["due_date"], d["due_time"]) + note,
        kb([[("+  Ещё задача", "new"), ("≡  Текущие", "list:all")], [("⌂  Главное меню", "menu")]]),
    )


# ---------- Списки задач ----------

FILTERS = {
    "today": "Сегодня",
    "week": "7 дней",
    "month": "30 дней",
    "all": "Все",
}


def filter_tasks(tasks, period: str):
    if period == "all":
        return tasks
    today = now().date()
    end = {"today": today, "week": today + timedelta(days=6), "month": today + timedelta(days=29)}[period]
    result = []
    for t in tasks:
        if not t["due_date"]:
            continue
        d = date.fromisoformat(t["due_date"])
        if d <= end:  # просроченные тоже показываем, чтобы о них не забыть
            result.append(t)
    return result


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str = "all"):
    context.user_data.clear()
    tasks = filter_tasks(db.get_active_tasks(update.effective_user.id), period)

    filter_row = [((("● " + name) if key == period else name), f"list:{key}")
                  for key, name in FILTERS.items()]
    rows = [filter_row]
    rows += [[(short_label(t), f"task:{t['id']}")] for t in tasks[:40]]
    rows.append([("+  Новая задача", "new"), ("⌂  Меню", "menu")])

    text = header("Текущие задачи", f"{FILTERS[period]} · {len(tasks)}") + "\n\n"
    if tasks:
        text += "Выберите задачу, чтобы отметить, перенести или изменить её."
        if len(tasks) > 40:
            text += "\nПоказаны первые 40."
    else:
        text += "Задач нет."
    await reply_or_edit(update, text, kb(rows))


async def show_done_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    tasks = db.get_done_tasks(update.effective_user.id)
    rows = [[("✓ " + short_label(t), f"task:{t['id']}")] for t in tasks]
    rows.append([("⌂  Главное меню", "menu")])
    if tasks:
        text = header("Выполненные задачи", f"Показано: {len(tasks)}") + "\n\nВыберите задачу, чтобы открыть её."
    else:
        text = header("Выполненные задачи") + "\n\nПока пусто."
    await reply_or_edit(update, text, kb(rows))


async def show_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    context.user_data.clear()
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await reply_or_edit(update, "Задача не найдена (возможно, удалена).", BACK_TO_MENU)
        return
    await reply_or_edit(update, card_from_row(task), task_keyboard(task))


# ---------- Действия с задачей ----------

async def ask_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await show_menu(update, context)
        return
    context.user_data.clear()
    context.user_data["state"] = WAIT_RESCHEDULE
    context.user_data["task_id"] = task_id
    text = (header("Перенос задачи") + "\n\n" + card_from_row(task) + "\n\n"
            "Выберите вариант или напишите новую дату и время: "
            "<code>завтра в 18:00</code>, <code>12.11</code>, <code>в 16:30</code>.")
    await reply_or_edit(update, text, kb([
        [("+1 час", f"shift:{task_id}:h1"), ("+1 день", f"shift:{task_id}:d1")],
        [("+1 неделя", f"shift:{task_id}:w1"), ("Без срока", f"shift:{task_id}:none")],
        [("←  Назад", f"task:{task_id}")],
    ]))


async def shift_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int, how: str):
    uid = update.effective_user.id
    task = db.get_task(task_id, uid)
    if not task:
        await show_menu(update, context)
        return
    n = now()
    d = date.fromisoformat(task["due_date"]) if task["due_date"] else n.date()
    t = time.fromisoformat(task["due_time"]) if task["due_time"] else None
    # Если задача просрочена, переносим относительно сегодняшнего дня
    if d < n.date():
        d = n.date()
    if how == "none":
        d, t = None, None
    elif how == "h1":
        base = datetime.combine(d, t) if t else n.replace(tzinfo=None, second=0, microsecond=0)
        base += timedelta(hours=1)
        d, t = base.date(), base.time()
    elif how == "d1":
        d += timedelta(days=1)
    elif how == "w1":
        d += timedelta(weeks=1)
    db.update_when(task_id, uid, d, t, n)
    await reply_or_edit(update, header("Задача перенесена") + "\n\n" + card_from_row(db.get_task(task_id, uid)),
                        task_keyboard(db.get_task(task_id, uid)))


async def ask_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await show_menu(update, context)
        return
    context.user_data.clear()
    context.user_data["state"] = WAIT_EDIT_TEXT
    context.user_data["task_id"] = task_id
    text = (header("Изменение задачи") + "\n\n" + card_from_row(task) + "\n\n"
            "Отправьте новый текст задачи. Если в нём не будет даты, она останется прежней.\n\n"
            f"Текущий текст: <code>{html.escape(task['title'])}</code>")
    await reply_or_edit(update, text, kb([
        [("→  Изменить только дату и время", f"resched:{task_id}")],
        [("←  Назад", f"task:{task_id}")],
    ]))


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    uid = update.effective_user.id
    db.set_done(task_id, uid, now())
    await answer_query(update, "Задача выполнена")
    task = db.get_task(task_id, uid)
    if task:
        await reply_or_edit(update, header("Задача выполнена") + "\n\n" + card_from_row(task),
                            kb([[("≡  Текущие задачи", "list:all")], [("⌂  Главное меню", "menu")]]))


async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await show_menu(update, context)
        return
    await reply_or_edit(update, header("Удалить задачу?") + "\n\n" + card_from_row(task),
                        kb([[("✕  Удалить", f"delyes:{task_id}"), ("←  Отмена", f"task:{task_id}")]]))


# ---------- Настройки ----------

WEATHER_TIMES = ["06:00", "07:00", "07:30", "08:00", "09:00", "10:00"]


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    # Сюда будут добавляться новые разделы настроек
    await reply_or_edit(update, header("Настройки") + "\n\nВыберите раздел.", kb([
        [("Погода", "w_settings")],
        [("⌂  Главное меню", "menu")],
    ]))


async def show_weather_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str = ""):
    context.user_data.clear()
    user = db.get_user(update.effective_user.id)
    city = user["city_name"] if user else None
    w_time = (user["weather_time"] if user else None) or "08:00"
    enabled = bool(user and user["weather_enabled"])

    lines = [header("Погода", "Настройки"), ""]
    if note:
        lines += [note, ""]
    status = "включён" if enabled else "выключен"
    lines.append("<blockquote>"
                 f"Город — {html.escape(city) if city else 'не выбран'}\n"
                 f"Время — {w_time}\n"
                 f"Ежедневный прогноз — {status}"
                 "</blockquote>")
    if not city:
        lines.append("Чтобы получать прогноз, укажите город.")

    rows = [[("Изменить город" if city else "Указать город", "w_city")]]
    if city:
        rows.append([("Время прогноза", "w_time"),
                     ("○  Выключить" if enabled else "●  Включить", "w_toggle")])
        rows.append([("Погода сейчас", "w_now")])
    rows.append([("←  Настройки", "settings"), ("⌂  Меню", "menu")])
    await reply_or_edit(update, "\n".join(lines), kb(rows))


async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = WAIT_CITY
    await reply_or_edit(update, header("Город") + "\n\nНапишите название города, например <code>Москва</code>.",
                        kb([[("←  Назад", "w_settings")]]))


def mark_weather_sent_if_time_passed(user_id: int, w_time: str):
    """Если выбранное время сегодня уже прошло — сегодняшний прогноз не шлём, начнём с завтра."""
    n = now()
    if w_time <= n.strftime("%H:%M"):
        db.update_user(user_id, weather_last_sent=n.date().isoformat())
    else:
        db.update_user(user_id, weather_last_sent=None)


async def save_city(update: Update, context: ContextTypes.DEFAULT_TYPE, city: dict):
    uid = update.effective_user.id
    user = db.get_user(uid)
    first_time = not user["city_name"]
    db.update_user(uid, city_name=city["name"], city_lat=city["lat"], city_lon=city["lon"])
    note = f"<i>Город сохранён: {html.escape(city['label'])}.</i>"
    if first_time:
        # При первом выборе города сразу включаем ежедневный прогноз
        w_time = user["weather_time"] or "08:00"
        db.update_user(uid, weather_enabled=1, weather_time=w_time)
        mark_weather_sent_if_time_passed(uid, w_time)
        note = note[:-4] + f"\nПрогноз будет приходить ежедневно в {w_time}.</i>"
    await show_weather_settings(update, context, note)


async def ask_weather_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["state"] = WAIT_WEATHER_TIME
    buttons = [(t, f"w_settime:{t}") for t in WEATHER_TIMES]
    await reply_or_edit(
        update,
        header("Время прогноза") + "\n\n"
        "Выберите время или напишите своё, например <code>6:45</code>.",
        kb([buttons[:3], buttons[3:], [("←  Назад", "w_settings")]]),
    )


async def set_weather_time(update: Update, context: ContextTypes.DEFAULT_TYPE, w_time: str):
    uid = update.effective_user.id
    db.update_user(uid, weather_time=w_time, weather_enabled=1)
    mark_weather_sent_if_time_passed(uid, w_time)
    await show_weather_settings(update, context, f"<i>Прогноз будет приходить ежедневно в {w_time}.</i>")


async def toggle_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = db.get_user(uid)
    if user["weather_enabled"]:
        db.update_user(uid, weather_enabled=0)
        note = "<i>Ежедневный прогноз выключен.</i>"
    else:
        db.update_user(uid, weather_enabled=1)
        mark_weather_sent_if_time_passed(uid, user["weather_time"] or "08:00")
        note = "<i>Ежедневный прогноз включён.</i>"
    await show_weather_settings(update, context, note)


async def send_weather_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user or not user["city_name"]:
        await show_weather_settings(update, context, "Сначала укажите город.")
        return
    try:
        text = await weather.today_text(user["city_name"], user["city_lat"], user["city_lon"])
    except weather.WeatherError as exc:
        log.warning("Погода недоступна: %s", exc)
        text = "Не удалось получить прогноз. Попробуйте позже."
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=BACK_TO_MENU)


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /weather — погода на сегодня прямо сейчас."""
    if not allowed(update):
        return
    db.save_user(update.effective_user.id, update.effective_user.first_name or "")
    await send_weather_now(update, context)


# ---------- Обработка нажатий на кнопки ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not allowed(update):
        await answer_query(update, "Доступ ограничен")
        return
    db.save_user(update.effective_user.id, update.effective_user.first_name or "")
    data = query.data
    action, _, arg = data.partition(":")

    if action == "done":
        await mark_done(update, context, int(arg))
        return  # mark_done сам отвечает на нажатие
    await answer_query(update)

    if action == "menu":
        await show_menu(update, context)
    elif action == "settings":
        await show_settings(update, context)
    elif action == "w_settings":
        await show_weather_settings(update, context)
    elif action == "w_city":
        await ask_city(update, context)
    elif action == "w_citypick":
        cities = context.user_data.get("cities") or []
        if int(arg) < len(cities):
            await save_city(update, context, cities[int(arg)])
        else:
            await ask_city(update, context)
    elif action == "w_time":
        await ask_weather_time(update, context)
    elif action == "w_settime":
        await set_weather_time(update, context, arg)
    elif action == "w_toggle":
        await toggle_weather(update, context)
    elif action == "w_now":
        await send_weather_now(update, context)
    elif action == "new":
        await ask_new_task(update, context)
    elif action == "list":
        await show_list(update, context, arg or "all")
    elif action == "done_list":
        await show_done_list(update, context)
    elif action == "task":
        await show_task(update, context, int(arg))
    elif action == "undone":
        db.set_active(int(arg), update.effective_user.id)
        await show_task(update, context, int(arg))
    elif action == "resched":
        await ask_reschedule(update, context, int(arg))
    elif action == "shift":
        task_id, how = arg.split(":")
        await shift_task(update, context, int(task_id), how)
    elif action == "edit":
        await ask_edit(update, context, int(arg))
    elif action == "del":
        await ask_delete(update, context, int(arg))
    elif action == "delyes":
        db.delete_task(int(arg), update.effective_user.id)
        await reply_or_edit(update, header("Задача удалена"),
                            kb([[("≡  Текущие задачи", "list:all")], [("⌂  Главное меню", "menu")]]))
    elif action == "draft_save":
        await save_draft(update, context)
    elif action == "draft_when":
        if "draft" not in context.user_data:
            await show_menu(update, context)
            return
        context.user_data["state"] = WAIT_DRAFT_WHEN
        await reply_or_edit(
            update,
            header("Дата и время") + "\n\nНапишите дату и время, например:\n"
            "<code>завтра в 18:00</code>, <code>6 ноября</code>, <code>12.11 в 9:30</code>",
            kb([[("Сегодня", "draft_day:0"), ("Завтра", "draft_day:1")],
                [("Без срока", "draft_day:none")],
                [("←  Назад", "draft_back")]]),
        )
    elif action == "draft_day":
        d = context.user_data.get("draft")
        if not d:
            await show_menu(update, context)
            return
        if arg == "none":
            d["due_date"], d["due_time"] = None, None
        else:
            d["due_date"] = now().date() + timedelta(days=int(arg))
        context.user_data["state"] = None
        await show_draft(update, context)
    elif action == "draft_back":
        if "draft" in context.user_data:
            context.user_data["state"] = None
            await show_draft(update, context)
        else:
            await show_menu(update, context)


# ---------- Обработка текстовых сообщений ----------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ к этому боту ограничен.")
        return
    db.save_user(update.effective_user.id, update.effective_user.first_name or "")
    text = update.message.text.strip()
    state = context.user_data.get("state")
    uid = update.effective_user.id
    n = now()

    if state == WAIT_CITY:
        try:
            cities = await weather.find_cities(text)
        except weather.WeatherError as exc:
            log.warning("Поиск города не удался: %s", exc)
            await update.message.reply_text("Сервис погоды сейчас недоступен. Попробуйте позже.")
            return
        if not cities:
            await update.message.reply_text(
                "Город не найден. Проверьте написание и отправьте ещё раз.")
            return
        if len(cities) == 1:
            await save_city(update, context, cities[0])
            return
        # Несколько городов с таким названием — даём выбрать
        context.user_data["cities"] = cities
        rows = [[(c["label"][:60], f"w_citypick:{i}")] for i, c in enumerate(cities)]
        rows.append([("←  Назад", "w_settings")])
        await update.message.reply_text("Уточните город:", reply_markup=kb(rows))
        return

    if state == WAIT_WEATHER_TIME:
        try:
            t = time.fromisoformat(text.replace(".", ":").zfill(5))
        except ValueError:
            await update.message.reply_text("Не удалось распознать время. Пример: 7:30 или 08:00.")
            return
        await set_weather_time(update, context, t.strftime("%H:%M"))
        return

    if state == WAIT_DRAFT_WHEN:
        d_new, t_new = parse_when(text, n)
        if not d_new and not t_new:
            await update.message.reply_text(
                "Не удалось распознать дату. Пример: «завтра в 18:00» или «12.11».")
            return
        draft = context.user_data["draft"]
        if d_new:
            draft["due_date"] = d_new
        if t_new:
            draft["due_time"] = t_new
            if not draft["due_date"]:
                draft["due_date"] = n.date()
        context.user_data["state"] = None
        await show_draft(update, context)
        return

    if state == WAIT_RESCHEDULE:
        task_id = context.user_data["task_id"]
        task = db.get_task(task_id, uid)
        d_new, t_new = parse_when(text, n)
        if not task or (not d_new and not t_new):
            await update.message.reply_text(
                "Не удалось распознать дату. Пример: «завтра в 18:00», «12.11» или «в 16:30».")
            return
        d = d_new or (date.fromisoformat(task["due_date"]) if task["due_date"] else n.date())
        t = t_new or (time.fromisoformat(task["due_time"]) if task["due_time"] else None)
        db.update_when(task_id, uid, d, t, n)
        context.user_data.clear()
        task = db.get_task(task_id, uid)
        await update.message.reply_text(header("Задача перенесена") + "\n\n" + card_from_row(task),
                                        reply_markup=task_keyboard(task), parse_mode=ParseMode.HTML)
        return

    if state == WAIT_EDIT_TEXT:
        task_id = context.user_data["task_id"]
        task = db.get_task(task_id, uid)
        if not task:
            await show_menu(update, context)
            return
        p = parse_task(text, n)
        db.update_text(task_id, uid, p["title"], p["place"], p["person"])
        # parse_when не подставляет "сегодня", если указано только время, — дата останется прежней
        d_new, t_new = parse_when(text, n)
        if d_new or t_new:
            d = d_new or (date.fromisoformat(task["due_date"]) if task["due_date"] else n.date())
            t = t_new or (time.fromisoformat(task["due_time"]) if task["due_time"] else None)
            db.update_when(task_id, uid, d, t, n)
        context.user_data.clear()
        task = db.get_task(task_id, uid)
        await update.message.reply_text(header("Задача изменена") + "\n\n" + card_from_row(task),
                                        reply_markup=task_keyboard(task), parse_mode=ParseMode.HTML)
        return

    # Во всех остальных случаях считаем сообщение новой задачей
    context.user_data.clear()
    context.user_data["draft"] = parse_task(text, n)
    await show_draft(update, context)


# ---------- Напоминания и недельная сводка ----------

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Запускается каждую минуту и отправляет напоминания, время которых наступило."""
    n = now()
    for task, num in db.due_reminders(n):
        if task["due_time"]:
            start = datetime.fromisoformat(f"{task['due_date']} {task['due_time']}")
            # Если бот был выключен и напоминание сильно опоздало — не шлём устаревшее
            if start < n.replace(tzinfo=None):
                db.mark_reminded(task["id"], num)
                continue
        head = header("Напоминание") if num == 1 else header("Напоминание", "Скоро начало")
        if num == 2 and not task["due_time"]:
            head = header("Напоминание", "Задача на сегодня")
        try:
            await context.bot.send_message(
                task["user_id"], head + "\n\n" + card_from_row(task),
                reply_markup=kb([[("✓  Выполнено", f"done:{task['id']}"),
                                  ("→  Перенести", f"resched:{task['id']}")]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Не удалось отправить напоминание %s: %s", task["id"], exc)
        db.mark_reminded(task["id"], num)


def build_summary(user_id: int, today: date) -> str:
    week_start = today - timedelta(days=today.weekday())  # понедельник
    week_end = week_start + timedelta(days=6)              # воскресенье
    planned = db.tasks_for_period(user_id, week_start, week_end)
    done = [t for t in planned if t["status"] == "done"]
    not_done = [t for t in planned if t["status"] != "done"]
    extra_done = db.done_without_date(user_id, week_start, week_end)

    period = f"{week_start.strftime('%d.%m')} – {week_end.strftime('%d.%m')}"
    if not planned and not extra_done:
        return header("Итоги недели", period) + "\n\nНа этой неделе задач не было."

    lines = [header("Итоги недели", period), ""]
    if planned:
        pct_done = round(len(done) * 100 / len(planned))
        filled = round(pct_done / 10)
        # Моноширинный блок: столбцы ровные, полоса — из чёрно-белых квадратов
        lines.append(
            "<pre>"
            f"{'■' * filled}{'□' * (10 - filled)}  {pct_done}%\n\n"
            f"{'Запланировано':<15}{len(planned):>4}\n"
            f"{'Выполнено':<15}{len(done):>4}\n"
            f"{'Не выполнено':<15}{len(not_done):>4}"
            "</pre>"
        )
    if extra_done:
        lines.append(f"Также выполнено задач без срока: {len(extra_done)}")
    if not_done:
        lines.append("\n<b>Не выполнены</b>")
        lines.append("<blockquote>" + "\n".join(html.escape(short_label(t)) for t in not_done[:15])
                     + "</blockquote>")
    return "\n".join(lines)


async def weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    """Каждое воскресенье в 16:00 отправляет сводку всем пользователям."""
    today = now().date()
    for user in db.all_users():
        try:
            await context.bot.send_message(
                user["user_id"], build_summary(user["user_id"], today),
                reply_markup=kb([[("≡  Текущие задачи", "list:all")]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Не удалось отправить сводку %s: %s", user["user_id"], exc)


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /summary — посмотреть сводку за текущую неделю прямо сейчас."""
    if not allowed(update):
        return
    await update.message.reply_text(build_summary(update.effective_user.id, now().date()),
                                    reply_markup=BACK_TO_MENU, parse_mode=ParseMode.HTML)


async def check_weather(context: ContextTypes.DEFAULT_TYPE):
    """Запускается каждую минуту и отправляет ежедневный прогноз тем, у кого наступило время."""
    n = now()
    today = n.date().isoformat()
    for user in db.users_for_weather(n.strftime("%H:%M"), today):
        try:
            text = await weather.today_text(user["city_name"], user["city_lat"], user["city_lon"])
        except weather.WeatherError as exc:
            log.warning("Погода недоступна, повторю через минуту: %s", exc)
            return
        try:
            await context.bot.send_message(user["user_id"], text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            log.warning("Не удалось отправить погоду %s: %s", user["user_id"], exc)
        db.update_user(user["user_id"], weather_last_sent=today)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка при обработке сообщения", exc_info=context.error)


# ---------- Запуск ----------

def main():
    if not BOT_TOKEN or BOT_TOKEN == "сюда_вставьте_токен":
        raise SystemExit(
            "\n❗ Не найден токен бота.\n"
            "Создайте файл .env рядом с bot.py и впишите в него строку:\n"
            "BOT_TOKEN=ваш_токен_от_BotFather\n"
            "Подробнее — в README.md (шаг 4).\n"
        )

    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "menu"], start))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # Проверка напоминаний — раз в минуту
    app.job_queue.run_repeating(check_reminders, interval=60, first=5)
    # Ежедневный прогноз погоды — тоже проверяем раз в минуту
    app.job_queue.run_repeating(check_weather, interval=60, first=10)
    # Недельная сводка — по воскресеньям в 16:00 (в библиотеке 0 = воскресенье)
    app.job_queue.run_daily(weekly_summary, time=time(16, 0, tzinfo=TZ), days=(0,))

    log.info("Бот запущен! Откройте его в Telegram и отправьте /start. Остановить: Control+C в этом окне.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
