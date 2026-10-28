"""
Telegram-бот «Планировщик задач».

Запуск:  python bot.py   (или кнопка ▶ в VS Code)
Настройки (токен, часовой пояс) берутся из файла .env — см. README.md.
"""

import asyncio
import html
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
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
import reports
import weather
from date_parser import (REPEAT_KINDS, first_occurrence, make_repeat, next_date, next_occurrence,
                         parse_task, parse_when, repeat_label)

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
WAIT_REPORT_TIME = "wait_report_time"
WAIT_TAB_NAME = "wait_tab_name"
WAIT_TAB_RENAME = "wait_tab_rename"


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


def task_card(title, place, person, due_date, due_time, extra="", repeat=None, tab=None) -> str:
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
    if repeat:
        details.append(f"Повтор — {repeat_label(repeat)}")
    if tab:
        details.append(f"Вкладка — {e(tab)}")
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
                     task["due_date"], task["due_time"], extra, task["repeat"],
                     tab_name(task["category_id"], task["user_id"]))


def tab_name(category_id, user_id: int):
    """Название вкладки по её номеру (или None для общих задач)."""
    if not category_id:
        return None
    c = db.get_category(category_id, user_id)
    return c["name"] if c else None


def short_label(task, tab: str = None) -> str:
    """Короткая подпись задачи для кнопки в списке (tab — название вкладки, если нужно показать)."""
    parts = []
    if is_overdue(task):
        parts.append("!")
    if task["due_date"]:
        d = date.fromisoformat(task["due_date"])
        parts.append(d.strftime("%d.%m"))
    if task["due_time"]:
        parts.append(task["due_time"])
    if task["repeat"]:
        parts.append("↻")
    label = " ".join(parts)
    label = f"{label} — {task['title']}" if label else task["title"]
    if tab:
        label += f" · {tab}"
    return label if len(label) <= 60 else label[:57] + "…"


# ---------- Клавиатуры (кнопки) ----------

def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


# Постоянное меню внизу экрана (под полем ввода). Нажатие на кнопку отправляет её текст.
NAV_TODAY, NAV_TASKS, NAV_NEW = "◉ Сегодня", "≡ Задачи", "+ Задача"
NAV_TABS, NAV_REPORTS, NAV_SETTINGS = "▤ Вкладки", "◔ Отчёты", "⋯ Настройки"
REPLY_KB = ReplyKeyboardMarkup(
    [[NAV_TODAY, NAV_TASKS, NAV_NEW], [NAV_TABS, NAV_REPORTS, NAV_SETTINGS]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="Напишите задачу, например: завтра в 10 позвонить маме",
)

BACK_TO_MENU = kb([[("⌂  Сегодня", "menu")]])


def draft_keyboard(d: dict):
    rows = [[("✓  Сохранить", "draft_save")]]
    if d["due_date"]:
        rows.append([("Дата и время", "draft_when")])
    else:
        # Даты нет — сразу предлагаем самые частые варианты
        rows.append([("Сегодня", "draft_day:0"), ("Завтра", "draft_day:1"), ("Другая дата", "draft_when")])
    rows.append([("↻  Повтор", "repmenu:d"), ("▤  Вкладка", "dtab"), ("✕  Отмена", "menu")])
    return kb(rows)


def task_keyboard(task):
    tid = task["id"]
    if task["status"] == "done":
        return kb([
            [("↺  Вернуть в текущие", f"undone:{tid}"), ("✕  Удалить", f"del:{tid}")],
            [("←  Назад", "back")],
        ])
    first = [("✓  Выполнено", f"done:{tid}")]
    if task["repeat"]:
        first.append(("»  Пропустить", f"skip:{tid}"))
    return kb([
        first,
        [("+1 день", f"shift:{tid}:d1"), ("+1 неделя", f"shift:{tid}:w1"), ("Дата…", f"resched:{tid}")],
        [("✎  Текст", f"edit:{tid}"), ("↻  Повтор", f"repmenu:{tid}"), ("▤  Вкладка", f"move:{tid}")],
        [("✕  Удалить", f"del:{tid}"), ("←  Назад", "back")],
    ])


# ---------- Вспомогательное ----------

def allowed(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED_USERS or (user and user.id in ALLOWED_USERS)


def greeting(update: Update) -> str:
    name = html.escape(update.effective_user.first_name or "")
    return (f"<b>Здравствуйте, {name}.</b>\n\n"
            "Меню — внизу экрана. Задачу можно просто написать сообщением:\n"
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
    # Первое сообщение включает меню внизу экрана, второе — экран «Сегодня»
    await update.effective_chat.send_message(greeting(update), reply_markup=REPLY_KB, parse_mode=ParseMode.HTML)
    await show_today(update, context, new_message=True)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«Главная» — это экран «Сегодня»."""
    await show_today(update, context)


# ---------- Создание задачи ----------

async def ask_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: int = None):
    context.user_data.clear()
    context.user_data["state"] = WAIT_NEW_TASK
    context.user_data["new_cat"] = category_id  # задача сразу попадёт в эту вкладку
    tab = tab_name(category_id, update.effective_user.id)
    text = (
        header("Новая задача", f"Вкладка «{html.escape(tab)}»" if tab else "") + "\n\n"
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
        d["title"], d["place"], d["person"], d["due_date"], d["due_time"], extra, d.get("repeat"),
        tab_name(d.get("category_id"), update.effective_user.id)
    )
    await reply_or_edit(update, text, draft_keyboard(d))


async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.get("draft")
    if not d:
        await show_menu(update, context)
        return
    db.add_task(update.effective_user.id, d["title"], d["place"], d["person"],
                d["due_date"], d["due_time"], now(), d.get("repeat"), d.get("category_id"))
    context.user_data.clear()
    note = ""
    if d["due_date"]:
        note = "\n<i>Напоминания — за сутки и за два часа.</i>"
    await reply_or_edit(
        update,
        header("Задача сохранена") + "\n\n"
        + task_card(d["title"], d["place"], d["person"], d["due_date"], d["due_time"],
                    repeat=d.get("repeat"), tab=tab_name(d.get("category_id"), update.effective_user.id))
        + note,
        kb([[("+  Ещё задача", f"newin:{d.get('category_id') or 0}"),
             ("≡  К задачам", f"tl:{d.get('category_id') or 0}:0")],
            [("⌂  Сегодня", "menu")]]),
    )


# ---------- Экран «Сегодня» и списки задач ----------

PAGE_SIZE = 10
SECTIONS = ["Просрочено", "Сегодня", "Завтра", "На неделе", "Позже", "Без срока"]
WEEKDAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def section_of(task, today: date) -> int:
    """Номер раздела списка (см. SECTIONS) для задачи."""
    if not task["due_date"]:
        return 5
    d = date.fromisoformat(task["due_date"])
    if d < today:
        return 0
    if d == today:
        return 1
    if d == today + timedelta(days=1):
        return 2
    return 3 if d <= today + timedelta(days=7) else 4


def list_line(num: int, task, section: int, tab: str = None) -> str:
    """Строка задачи в списке: «3. пт 30.10 · 19:00 · Футбол ↻ · Учёба»."""
    when = []
    if task["due_date"] and section in (0, 3, 4):
        d = date.fromisoformat(task["due_date"])
        when.append(f"{WEEKDAY_NAMES[d.weekday()]} {d.strftime('%d.%m')}" if section == 3 else d.strftime("%d.%m"))
    if task["due_time"]:
        when.append(task["due_time"])
    line = f"{num}. " + (" · ".join(when) + " · " if when else "") + html.escape(task["title"])
    if task["repeat"]:
        line += " ↻"
    if tab:
        line += f" · <i>{html.escape(tab)}</i>"
    return line


def progress_bar(done: int, total: int) -> str:
    filled = round(done * 10 / total) if total else 0
    return "■" * filled + "□" * (10 - filled)


def today_label(task, today: date) -> str:
    """Подпись кнопки в чек-листе «Сегодня»: ○ — не сделано, ✓ — сделано, ! — просрочено."""
    if task["status"] == "done":
        mark = "✓"
    elif task["due_date"] < today.isoformat():
        mark = "!"
    else:
        mark = "○"
    when = task["due_time"] or ""
    if task["due_date"] < today.isoformat():
        when = date.fromisoformat(task["due_date"]).strftime("%d.%m")
    label = f"{mark}  " + (f"{when}  " if when else "") + task["title"]
    return label if len(label) <= 60 else label[:57] + "…"


async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "check",
                     new_message: bool = False):
    """
    Главный экран: прогресс за сегодня и чек-лист задач.
    mode="check" — нажатие отмечает задачу выполненной (и снимает отметку),
    mode="open"  — нажатие открывает карточку задачи.
    """
    uid = update.effective_user.id
    context.user_data.clear()
    context.chat_data["back"] = "td" if mode == "check" else "td:open"
    n = now()
    today = n.date()

    todays = db.tasks_for_period(uid, today, today)
    overdue = [t for t in db.get_active_tasks(uid) if t["due_date"] and t["due_date"] < today.isoformat()]
    done = sum(t["status"] == "done" for t in todays)
    tomorrow = len(db.tasks_for_period(uid, today + timedelta(days=1), today + timedelta(days=1)))

    text = header("Сегодня", f"{WEEKDAYS_FULL[today.weekday()]}, {today.day} {MONTH_NAMES[today.month - 1]}")
    text += "\n\n"
    if todays:
        text += f"<code>{progress_bar(done, len(todays))}</code>  {done} из {len(todays)} выполнено\n"
    else:
        text += "На сегодня задач нет.\n"
    extra = []
    if overdue:
        extra.append(f"просрочено: {len(overdue)}")
    if tomorrow:
        extra.append(f"на завтра: {tomorrow}")
    if extra:
        text += "<i>" + " · ".join(extra).capitalize() + "</i>\n"
    if todays or overdue:
        text += ("\nНажмите на задачу — она отметится выполненной. Ещё раз — отметка снимется."
                 if mode == "check" else "\nНажмите на задачу, чтобы открыть её.")

    rows = []
    for t in overdue + todays:
        cb = f"tq:{t['id']}" if mode == "check" else f"task:{t['id']}"
        rows.append([(today_label(t, today), cb)])
    switch = ("✎  Открыть задачу", "td:open") if mode == "check" else ("✓  Режим отметок", "td")
    rows.append([("+  Задача", "newin:0"), switch])
    rows.append([("≡  Все задачи", "tl:0:0"), ("▤  Вкладки", "tabs")])
    if new_message:
        await update.effective_chat.send_message(text, reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    else:
        await reply_or_edit(update, text, kb(rows))


async def toggle_today(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    """Чек-лист «Сегодня»: отметить задачу выполненной или снять отметку."""
    uid = update.effective_user.id
    task = db.get_task(task_id, uid)
    if not task:
        await answer_query(update)
    elif task["status"] == "active":
        next_id = complete_task(uid, task, now())
        context.chat_data.setdefault("undo", {})[task_id] = next_id
        await answer_query(update, "Выполнено")
    else:
        uncomplete_task(uid, task_id, context.chat_data.get("undo", {}).pop(task_id, None))
        await answer_query(update, "Отметка снята")
    await show_today(update, context)


async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: int = 0, page: int = 0):
    """Все текущие задачи (cat = 0) или задачи одной вкладки, по разделам и страницам."""
    uid = update.effective_user.id
    tab = db.get_category(cat, uid) if cat else None
    if cat and not tab:
        cat = 0  # вкладку удалили — показываем все задачи
    context.user_data.clear()
    # Если пользователь, находясь во вкладке, просто напишет задачу — она попадёт сюда
    context.user_data["new_cat"] = cat or None
    context.chat_data["back"] = f"tl:{cat}:{page}"

    today = now().date()
    tabs = db.get_categories(uid)
    names = {c["id"]: c["name"] for c in tabs}
    tasks = sorted(db.get_active_tasks(uid, cat or None), key=lambda t: section_of(t, today))
    pages = max(1, -(-len(tasks) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    shown = tasks[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text = header(tab["name"] if cat else "Все задачи",
                  f"{len(tasks)} {plural(len(tasks), 'задача', 'задачи', 'задач')}"
                  + (f" · стр. {page + 1} из {pages}" if pages > 1 else ""))
    last_section = None
    for i, t in enumerate(shown, start=page * PAGE_SIZE + 1):
        sec = section_of(t, today)
        if sec != last_section:
            text += f"\n\n<b>{SECTIONS[sec]}</b>"
            last_section = sec
        text += "\n" + list_line(i, t, sec, None if cat else names.get(t["category_id"]))
    if not tasks:
        text += "\n\nЗадач нет."
    else:
        text += "\n\n<i>Нажмите номер, чтобы открыть задачу.</i>"
    if cat:
        text += "\n<i>Новую задачу можно просто написать — она попадёт в эту вкладку.</i>"

    rows = []
    # Переключатель вкладок сверху: «Все» и вкладки пользователя
    if tabs:
        chips = [(("● " if not cat else "") + "Все", "tl:0:0")]
        chips += [(("● " if cat == c["id"] else "") + c["name"][:14], f"tl:{c['id']}:0") for c in tabs]
        rows += [chips[i:i + 3] for i in range(0, len(chips), 3)]
    numbers = [(str(i), f"task:{t['id']}") for i, t in enumerate(shown, start=page * PAGE_SIZE + 1)]
    rows += [numbers[i:i + 5] for i in range(0, len(numbers), 5)]
    if pages > 1:
        rows.append([("◀", f"tl:{cat}:{page - 1}" if page else "noop"),
                     (f"{page + 1} / {pages}", "noop"),
                     ("▶", f"tl:{cat}:{page + 1}" if page < pages - 1 else "noop")])
    bottom = [("+  Задача", f"newin:{cat}"), ("✓  Выполненные", "done_list")]
    if cat:
        bottom.append(("⋯  Вкладка", f"tabopt:{cat}"))
    rows.append(bottom)
    await reply_or_edit(update, text, kb(rows))


async def show_tabs(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str = ""):
    """Список вкладок с количеством задач и ближайшим сроком."""
    uid = update.effective_user.id
    context.user_data.clear()
    tabs = db.get_categories(uid)
    counts = db.count_active_by_category(uid)
    text = header("Вкладки") + "\n\n"
    if note:
        text += note + "\n\n"
    if tabs:
        lines = []
        for c in tabs:
            n_tasks = counts.get(c["id"], 0)
            dated = [t["due_date"] for t in db.get_active_tasks(uid, c["id"]) if t["due_date"]]
            nearest = f" · ближайшая {date.fromisoformat(min(dated)).strftime('%d.%m')}" if dated else ""
            lines.append(f"<b>{html.escape(c['name'])}</b> — {n_tasks} "
                         f"{plural(n_tasks, 'задача', 'задачи', 'задач')}{nearest}")
        general = counts.get(None, 0)
        if general:
            lines.append(f"<i>Без вкладки — {general} {plural(general, 'задача', 'задачи', 'задач')}</i>")
        text += "\n".join(lines)
    else:
        text += ("Вкладки — это разделы для задач, например «Учёба» или «Работа».\n"
                 "Создайте первую вкладку кнопкой ниже.")
    names = [(c["name"][:20], f"tl:{c['id']}:0") for c in tabs]
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    rows.append([("≡  Все задачи", "tl:0:0"), ("+  Новая вкладка", "tabnew")])
    await reply_or_edit(update, text, kb(rows))


async def show_done_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    tasks = db.get_done_tasks(update.effective_user.id)
    context.chat_data["back"] = "done_list"
    rows = [[("✓ " + short_label(t), f"task:{t['id']}")] for t in tasks]
    rows.append([("≡  Текущие задачи", "tl:0:0"), ("⌂  Сегодня", "menu")])
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


def complete_task(uid: int, task, n: datetime):
    """
    Отмечает задачу выполненной. У повторяющейся создаёт следующий раз (а у выполненной копии
    повтор убирает). Возвращает номер созданной задачи или None — он нужен, чтобы отменить отметку.
    """
    db.set_done(task["id"], uid, n)
    if not (task["repeat"] and task["due_date"]):
        return None
    nxt = next_run(task, n)
    due_time = time.fromisoformat(task["due_time"]) if task["due_time"] else None
    next_id = db.add_task(uid, task["title"], task["place"], task["person"], nxt, due_time, n,
                          task["repeat"], task["category_id"])
    db.set_repeat(task["id"], uid, None)
    return next_id


def uncomplete_task(uid: int, task_id: int, next_id=None):
    """Снимает отметку «выполнено». Если был создан следующий раз повтора — убирает его обратно."""
    db.set_active(task_id, uid)
    nxt = db.get_task(next_id, uid) if next_id else None
    if nxt and nxt["status"] == "active":
        db.set_repeat(task_id, uid, nxt["repeat"])
        db.delete_task(next_id, uid)


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    uid = update.effective_user.id
    task = db.get_task(task_id, uid)
    if not task or task["status"] == "done":
        await answer_query(update)
        return
    next_id = complete_task(uid, task, now())
    await answer_query(update, "Задача выполнена")
    note = ""
    if next_id:
        nxt = db.get_task(next_id, uid)
        when = fmt_date(nxt["due_date"]) + (f" · {nxt['due_time']}" if nxt["due_time"] else "")
        note = f"\n<i>Следующий раз — {when}</i>"
    await reply_or_edit(update, header("Задача выполнена") + "\n\n" + card_from_row(db.get_task(task_id, uid))
                        + note,
                        kb([[("↺  Отменить", f"undo:{task_id}:{next_id or 0}"), ("←  Назад", "back")]]))


def next_run(task, n: datetime):
    """Дата следующего раза повторяющейся задачи — всегда в будущем."""
    nxt = next_occurrence(task["repeat"], date.fromisoformat(task["due_date"]), n.date())
    if task["due_time"] and datetime.combine(nxt, time.fromisoformat(task["due_time"])) <= n.replace(tzinfo=None):
        nxt = next_date(task["repeat"], nxt)  # сегодняшнее время уже прошло
    return nxt


async def skip_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    """Пропустить этот раз повторяющейся задачи: переносим на следующую дату по правилу."""
    uid = update.effective_user.id
    task = db.get_task(task_id, uid)
    if not task or not task["repeat"] or not task["due_date"]:
        await show_menu(update, context)
        return
    n = now()
    nxt = next_run(task, n)
    due_time = time.fromisoformat(task["due_time"]) if task["due_time"] else None
    db.update_when(task_id, uid, nxt, due_time, n)
    task = db.get_task(task_id, uid)
    await reply_or_edit(update, header("Пропущено", "Задача перенесена на следующий раз") + "\n\n"
                        + card_from_row(task), task_keyboard(task))


async def show_repeat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    """Выбор повтора. target = "d" (создаваемая задача) или номер задачи."""
    if target == "d":
        d = context.user_data.get("draft")
        if not d:
            await show_menu(update, context)
            return
        current, back = d.get("repeat"), "draft_back"
        context.user_data["state"] = None
    else:
        task = db.get_task(int(target), update.effective_user.id)
        if not task:
            await show_menu(update, context)
            return
        current, back = task["repeat"], f"task:{target}"
    text = header("Повтор", f"Сейчас: {repeat_label(current) or 'не повторяется'}") + "\n\n" + \
        "Как часто повторять задачу? День недели, число месяца и дата в году берутся из даты задачи."
    opts = [(("● " if current and current.startswith(k) else "") + name, f"repset:{target}:{k}")
            for k, name in REPEAT_KINDS.items()]
    await reply_or_edit(update, text, kb([
        [opts[0], opts[1]],
        [opts[2], opts[3]],
        [opts[4], (("● " if not current else "") + "Не повторять", f"repset:{target}:none")],
        [("←  Назад", back)],
    ]))


def start_date_for(kind: str, current: date | None) -> date:
    """С какой даты начинать повтор: дата задачи, а если её нет — ближайший подходящий день."""
    today = now().date()
    if current is None:
        return first_occurrence(kind, today) if kind in ("daily", "weekdays") else today
    if kind == "weekdays" and current.weekday() >= 5:
        return first_occurrence("weekdays", current)  # суббота/воскресенье -> понедельник
    return current


async def set_repeat_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str, kind: str):
    if target == "d":
        d = context.user_data.get("draft")
        if not d:
            await show_menu(update, context)
            return
        if kind == "none":
            d["repeat"] = None
        else:
            d["due_date"] = start_date_for(kind, d["due_date"])
            d["repeat"] = make_repeat(kind, d["due_date"])
        await show_draft(update, context)
        return

    uid = update.effective_user.id
    task_id = int(target)
    task = db.get_task(task_id, uid)
    if not task:
        await show_menu(update, context)
        return
    if kind == "none":
        db.set_repeat(task_id, uid, None)
        title = "Повтор отключён"
    else:
        old = date.fromisoformat(task["due_date"]) if task["due_date"] else None
        start = start_date_for(kind, old)
        if start != old:
            due_time = time.fromisoformat(task["due_time"]) if task["due_time"] else None
            db.update_when(task_id, uid, start, due_time, now())
        db.set_repeat(task_id, uid, make_repeat(kind, start))
        title = "Повтор настроен"
    task = db.get_task(task_id, uid)
    await reply_or_edit(update, header(title) + "\n\n" + card_from_row(task), task_keyboard(task))


async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await show_menu(update, context)
        return
    note = "\n<i>Повтор тоже будет остановлен.</i>" if task["repeat"] else ""
    await reply_or_edit(update, header("Удалить задачу?") + "\n\n" + card_from_row(task) + note,
                        kb([[("✕  Удалить", f"delyes:{task_id}"), ("←  Отмена", f"task:{task_id}")]]))


# ---------- Вкладки ----------

async def ask_tab_name(update: Update, context: ContextTypes.DEFAULT_TYPE, rename_id: int = None):
    context.user_data.clear()
    if rename_id:
        context.user_data["state"] = WAIT_TAB_RENAME
        context.user_data["tab_id"] = rename_id
        title, back = "Переименовать вкладку", f"tabopt:{rename_id}"
    else:
        context.user_data["state"] = WAIT_TAB_NAME
        title, back = "Новая вкладка", "tabs"
    await reply_or_edit(update, header(title) + "\n\nНапишите название, например <code>Учёба</code> "
                        "или <code>Работа</code>.", kb([[("←  Назад", back)]]))


def plural(n: int, one: str, few: str, many: str) -> str:
    """Склонение по числу: 1 задача, 2 задачи, 5 задач."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def clean_tab_name(text: str):
    name = " ".join(text.split())[:30]
    return name or None


async def show_tab_options(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: int):
    context.user_data.clear()
    uid = update.effective_user.id
    tab = db.get_category(cat, uid)
    if not tab:
        await show_menu(update, context)
        return
    n = db.count_active_by_category(uid).get(cat, 0)
    await reply_or_edit(update, header(tab["name"], "Вкладка") + f"\n\nТекущих задач: {n}", kb([
        [("Отчёт: неделя", f"r_now:week:{cat}"), ("Отчёт: месяц", f"r_now:month:{cat}")],
        [("✎  Переименовать", f"tabren:{cat}"), ("✕  Удалить", f"tabdel:{cat}")],
        [("←  Назад", f"tl:{cat}:0")],
    ]))


async def ask_tab_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: int):
    uid = update.effective_user.id
    tab = db.get_category(cat, uid)
    if not tab:
        await show_menu(update, context)
        return
    n = db.count_active_by_category(uid).get(cat, 0)
    rows = []
    if n:
        rows.append([("Удалить, задачи сделать общими", f"tabdelyes:{cat}:keep")])
        rows.append([("Удалить вместе с задачами", f"tabdelyes:{cat}:all")])
    else:
        rows.append([("✕  Удалить", f"tabdelyes:{cat}:keep")])
    rows.append([("←  Отмена", f"tabopt:{cat}")])
    text = header("Удалить вкладку?", tab["name"]) + "\n\n"
    text += (f"Во вкладке {n} {plural(n, 'текущая задача', 'текущие задачи', 'текущих задач')}. "
             "Их можно оставить — они появятся в «Все задачи» "
             "без вкладки, — или удалить вместе с вкладкой." if n else "Текущих задач во вкладке нет.")
    await reply_or_edit(update, text, kb(rows))


async def delete_tab(update: Update, context: ContextTypes.DEFAULT_TYPE, cat: int, mode: str):
    db.delete_category(cat, update.effective_user.id, with_tasks=(mode == "all"))
    await show_tabs(update, context, "<i>Вкладка удалена.</i>")


async def show_tab_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    """Выбор вкладки для задачи. target = "d" (создаваемая задача) или номер задачи."""
    uid = update.effective_user.id
    if target == "d":
        d = context.user_data.get("draft")
        if not d:
            await show_menu(update, context)
            return
        current, back, prefix = d.get("category_id"), "draft_back", "dtabset:"
        context.user_data["state"] = None
    else:
        task = db.get_task(int(target), uid)
        if not task:
            await show_menu(update, context)
            return
        current, back, prefix = task["category_id"], f"task:{target}", f"moveto:{target}:"
    tabs = db.get_categories(uid)
    rows = [[(("● " if current == c["id"] else "") + c["name"], f"{prefix}{c['id']}")] for c in tabs]
    rows.append([(("● " if not current else "") + "Без вкладки", f"{prefix}0")])
    rows.append([("←  Назад", back)])
    text = header("Вкладка") + "\n\nВыберите, в какую вкладку положить задачу."
    if not tabs:
        text += "\n\n<i>Вкладок пока нет — создайте их в главном меню кнопкой «Новая вкладка».</i>"
    await reply_or_edit(update, text, kb(rows))


async def move_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int, cat: int):
    uid = update.effective_user.id
    if cat and not db.get_category(cat, uid):
        cat = 0
    db.set_category(task_id, uid, cat or None)
    task = db.get_task(task_id, uid)
    if not task:
        await show_menu(update, context)
        return
    await reply_or_edit(update, header("Вкладка изменена") + "\n\n" + card_from_row(task), task_keyboard(task))


# ---------- Настройки ----------

WEATHER_TIMES = ["06:00", "07:00", "07:30", "08:00", "09:00", "10:00"]


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    # Сюда будут добавляться новые разделы настроек
    await reply_or_edit(update, header("Настройки") + "\n\nВыберите раздел.", kb([
        [("Погода", "w_settings"), ("Отчёты", "r_settings")],
        [("⌂  Сегодня", "menu")],
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
    rows.append([("←  Настройки", "settings"), ("⌂  Сегодня", "menu")])
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


# ---------- Настройки: отчёты ----------

REPORT_TIMES = ["09:00", "12:00", "16:00", "18:00", "20:00", "21:00"]
REPORT_NAMES = {"week": "Еженедельный", "month": "Ежемесячный"}
REPORT_DAYS = {"week": "по воскресеньям", "month": "в последний день месяца"}


def is_report_day(kind: str, d: date) -> bool:
    return d.weekday() == 6 if kind == "week" else reports.is_last_day_of_month(d)


def mark_report_sent_if_time_passed(user_id: int, kind: str, r_time: str):
    """Если сегодня день отчёта, а выбранное время уже прошло, — сегодня отчёт не шлём."""
    n = now()
    passed = is_report_day(kind, n.date()) and r_time <= n.strftime("%H:%M")
    db.update_user(user_id, **{f"report_{kind}_last_sent": n.date().isoformat() if passed else None})


async def show_report_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str = ""):
    context.user_data.clear()
    user = db.get_user(update.effective_user.id)
    lines = [header("Отчёты"), ""]
    if note:
        lines += [note, ""]
    lines.append("Диаграмма и проценты: сколько задач выполнено, не выполнено и в процессе, "
                 "с разбивкой по вкладкам.")
    rows_text = []
    rows = [[("За неделю", "r_now:week"), ("За месяц", "r_now:month"), ("По вкладке", "r_tabs")]]
    for kind in ("week", "month"):
        enabled = user[f"report_{kind}_enabled"]
        r_time = user[f"report_{kind}_time"]
        status = f"{REPORT_DAYS[kind]} в {r_time}" if enabled else "выключен"
        rows_text.append(f"{REPORT_NAMES[kind]} — {status}")
        rows.append([(f"Время: {'неделя' if kind == 'week' else 'месяц'}", f"r_time:{kind}"),
                     ("○  Выключить" if enabled else "●  Включить", f"r_toggle:{kind}")])
    lines.append("\n<b>Автоматически</b>\n<blockquote>" + "\n".join(rows_text) + "</blockquote>")
    await reply_or_edit(update, "\n".join(lines), kb(rows))


async def ask_report_time(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    context.user_data.clear()
    context.user_data["state"] = WAIT_REPORT_TIME
    context.user_data["report_kind"] = kind
    buttons = [(t, f"r_settime:{kind}:{t}") for t in REPORT_TIMES]
    await reply_or_edit(
        update,
        header(f"{REPORT_NAMES[kind]} отчёт", f"Приходит {REPORT_DAYS[kind]}") + "\n\n"
        "Выберите время или напишите своё, например <code>17:30</code>.",
        kb([buttons[:3], buttons[3:], [("←  Назад", "r_settings")]]),
    )


async def set_report_time(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, r_time: str):
    uid = update.effective_user.id
    db.update_user(uid, **{f"report_{kind}_time": r_time, f"report_{kind}_enabled": 1})
    mark_report_sent_if_time_passed(uid, kind, r_time)
    await show_report_settings(update, context,
                               f"<i>{REPORT_NAMES[kind]} отчёт будет приходить {REPORT_DAYS[kind]} в {r_time}.</i>")


async def toggle_report(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    uid = update.effective_user.id
    user = db.get_user(uid)
    if user[f"report_{kind}_enabled"]:
        db.update_user(uid, **{f"report_{kind}_enabled": 0})
        note = f"<i>{REPORT_NAMES[kind]} отчёт выключен.</i>"
    else:
        db.update_user(uid, **{f"report_{kind}_enabled": 1})
        mark_report_sent_if_time_passed(uid, kind, user[f"report_{kind}_time"])
        note = f"<i>{REPORT_NAMES[kind]} отчёт включён.</i>"
    await show_report_settings(update, context, note)


async def send_report(bot, user_id: int, kind: str, category_id: int = None):
    """
    Готовит и отправляет отчёт: картинку с подписью (или только текст, если задач не было).
    category_id — отчёт только по одной вкладке.
    """
    n = now()
    period = reports.week_period(n.date()) if kind == "week" else reports.month_period(n.date())
    # Рисование картинки занимает около секунды — делаем это в фоне, чтобы бот не «замирал»
    text, image = await asyncio.to_thread(reports.build, user_id, period, n, category_id)
    markup = kb([[("≡  К задачам", f"list:all:{category_id or 0}"), ("⌂  Сегодня", "menu")]])
    if image:
        await bot.send_photo(user_id, photo=image, caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def report_now(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, category_id: int = None):
    uid = update.effective_user.id
    if category_id is not None and not db.get_category(category_id, uid):
        await show_menu(update, context)  # вкладку уже удалили
        return
    await send_report(context.bot, uid, kind, category_id)


async def show_report_tab_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отчёт по вкладке: для каждой вкладки — кнопки «неделя» и «месяц»."""
    tabs = db.get_categories(update.effective_user.id)
    rows = []
    for c in tabs:
        name = c["name"] if len(c["name"]) <= 16 else c["name"][:15] + "…"
        rows.append([(f"{name} · неделя", f"r_now:week:{c['id']}"),
                     (f"{name} · месяц", f"r_now:month:{c['id']}")])
    rows.append([("←  Назад", "r_settings")])
    text = header("Отчёт по вкладке") + "\n\n"
    text += ("Выберите вкладку и период — в отчёте будут только её задачи." if tabs else
             "Вкладок пока нет — создайте их в главном меню кнопкой «Новая вкладка».")
    await reply_or_edit(update, text, kb(rows))


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /summary — отчёт за текущую неделю прямо сейчас."""
    if allowed(update):
        db.save_user(update.effective_user.id, update.effective_user.first_name or "")
        await report_now(update, context, "week")


async def month_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /month — отчёт за текущий месяц прямо сейчас."""
    if allowed(update):
        db.save_user(update.effective_user.id, update.effective_user.first_name or "")
        await report_now(update, context, "month")


def parse_hhmm(text: str):
    """"7:30", "07.30", "17:00" -> "07:30" / "17:00"; если не время — None."""
    try:
        return time.fromisoformat(text.strip().replace(".", ":").zfill(5)).strftime("%H:%M")
    except ValueError:
        return None


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
    if action == "tq":
        await toggle_today(update, context, int(arg))
        return  # toggle_today сам отвечает на нажатие (всплывающая подсказка)
    await answer_query(update)

    if action in ("menu", "td"):
        await show_today(update, context, "open" if arg == "open" else "check")
    elif action == "tl":
        cat, _, page = arg.partition(":")
        await show_tasks(update, context, int(cat or 0), int(page or 0))
    elif action == "tabs":
        await show_tabs(update, context)
    elif action == "back":
        # «Назад» из карточки задачи — в тот экран, откуда её открыли
        back_action, _, back_arg = context.chat_data.get("back", "td").partition(":")
        if back_action == "tl":
            cat, _, page = back_arg.partition(":")
            await show_tasks(update, context, int(cat or 0), int(page or 0))
        elif back_action == "done_list":
            await show_done_list(update, context)
        else:
            await show_today(update, context, "open" if back_arg == "open" else "check")
    elif action == "undo":
        task_id, next_id = (int(x) for x in arg.split(":"))
        uncomplete_task(update.effective_user.id, task_id, next_id or None)
        await show_task(update, context, task_id)
    elif action == "noop":
        pass
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
    elif action == "r_settings":
        await show_report_settings(update, context)
    elif action == "r_time":
        await ask_report_time(update, context, arg)
    elif action == "r_settime":
        kind, r_time = arg.split(":", 1)
        await set_report_time(update, context, kind, r_time)
    elif action == "r_toggle":
        await toggle_report(update, context, arg)
    elif action == "r_now":
        kind, _, cat = arg.partition(":")
        await report_now(update, context, kind, int(cat) if cat else None)
    elif action == "r_tabs":
        await show_report_tab_picker(update, context)
    elif action == "new":
        await ask_new_task(update, context)
    elif action == "newin":
        await ask_new_task(update, context, int(arg) or None)
    elif action == "list":
        # Кнопки из старых сообщений (до переработки интерфейса)
        _, _, cat = arg.partition(":")
        await show_tasks(update, context, int(cat) if cat.isdigit() else 0)
    elif action == "tabnew":
        await ask_tab_name(update, context)
    elif action == "tabopt":
        await show_tab_options(update, context, int(arg))
    elif action == "tabren":
        await ask_tab_name(update, context, rename_id=int(arg))
    elif action == "tabdel":
        await ask_tab_delete(update, context, int(arg))
    elif action == "tabdelyes":
        cat, mode = arg.split(":")
        await delete_tab(update, context, int(cat), mode)
    elif action == "move":
        await show_tab_picker(update, context, arg)
    elif action == "moveto":
        task_id, cat = arg.split(":")
        await move_task(update, context, int(task_id), int(cat))
    elif action == "dtab":
        await show_tab_picker(update, context, "d")
    elif action == "dtabset":
        d = context.user_data.get("draft")
        if not d:
            await show_menu(update, context)
            return
        cat = int(arg)
        d["category_id"] = cat if cat and db.get_category(cat, update.effective_user.id) else None
        await show_draft(update, context)
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
                            kb([[("←  Назад", "back"), ("⌂  Сегодня", "menu")]]))
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
    elif action == "skip":
        await skip_task(update, context, int(arg))
    elif action == "repmenu":
        await show_repeat_menu(update, context, arg)
    elif action == "repset":
        target, kind = arg.split(":")
        await set_repeat_choice(update, context, target, kind)
    elif action == "draft_day":
        d = context.user_data.get("draft")
        if not d:
            await show_menu(update, context)
            return
        if arg == "none":
            d["due_date"], d["due_time"], d["repeat"] = None, None, None
        else:
            d["due_date"] = now().date() + timedelta(days=int(arg))
            refresh_draft_repeat(d)
        context.user_data["state"] = None
        await show_draft(update, context)
    elif action == "draft_back":
        if "draft" in context.user_data:
            context.user_data["state"] = None
            await show_draft(update, context)
        else:
            await show_menu(update, context)


def refresh_draft_repeat(draft: dict):
    """После смены даты пересчитываем правило повтора (например, "каждую среду" -> "каждую пятницу")."""
    if draft.get("repeat") and draft["due_date"]:
        draft["repeat"] = make_repeat(draft["repeat"].partition(":")[0], draft["due_date"])


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

    # Кнопки меню внизу экрана работают из любого места (и отменяют незаконченное действие)
    nav = {
        NAV_TODAY: lambda: show_today(update, context),
        NAV_TASKS: lambda: show_tasks(update, context),
        NAV_NEW: lambda: ask_new_task(update, context),
        NAV_TABS: lambda: show_tabs(update, context),
        NAV_REPORTS: lambda: show_report_settings(update, context),
        NAV_SETTINGS: lambda: show_settings(update, context),
    }
    if text in nav:
        await nav[text]()
        return

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

    if state in (WAIT_TAB_NAME, WAIT_TAB_RENAME):
        name = clean_tab_name(text)
        if not name:
            return
        existing = db.find_category(uid, name)
        if state == WAIT_TAB_RENAME:
            cat = context.user_data["tab_id"]
            if existing and existing["id"] != cat:
                await update.message.reply_text("Вкладка с таким названием уже есть. Напишите другое.")
                return
            db.rename_category(cat, uid, name)
        elif existing:
            cat = existing["id"]  # такая вкладка уже есть — просто открываем её
        else:
            cat = db.add_category(uid, name, n)
        await show_tasks(update, context, cat)
        return

    if state in (WAIT_WEATHER_TIME, WAIT_REPORT_TIME):
        hhmm = parse_hhmm(text)
        if not hhmm:
            await update.message.reply_text("Не удалось распознать время. Пример: 7:30 или 18:00.")
            return
        if state == WAIT_WEATHER_TIME:
            await set_weather_time(update, context, hhmm)
        else:
            await set_report_time(update, context, context.user_data["report_kind"], hhmm)
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
        refresh_draft_repeat(draft)
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
        if p["repeat"]:
            # В новом тексте есть "каждый день", "по пятницам" и т.п. — обновляем повтор вместе с датой
            db.update_when(task_id, uid, p["due_date"], p["due_time"] or (
                time.fromisoformat(task["due_time"]) if task["due_time"] else None), n)
            db.set_repeat(task_id, uid, p["repeat"])
        context.user_data.clear()
        task = db.get_task(task_id, uid)
        await update.message.reply_text(header("Задача изменена") + "\n\n" + card_from_row(task),
                                        reply_markup=task_keyboard(task), parse_mode=ParseMode.HTML)
        return

    # Во всех остальных случаях считаем сообщение новой задачей
    category_id = context.user_data.get("new_cat")  # открыта вкладка — задача попадёт в неё
    context.user_data.clear()
    context.user_data["draft"] = parse_task(text, n)
    context.user_data["draft"]["category_id"] = category_id
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


async def prepare_charts(context: ContextTypes.DEFAULT_TYPE):
    """Готовим библиотеку диаграмм в фоне, чтобы бот не «замирал» при запуске."""
    log.info("Готовлю диаграммы для отчётов. В первый раз это может занять 1–2 минуты — "
             "бот при этом уже работает, ничего не нажимайте.")
    await asyncio.to_thread(reports.prepare)
    log.info("Диаграммы готовы.")


async def check_reports(context: ContextTypes.DEFAULT_TYPE):
    """Каждую минуту: по воскресеньям — недельный отчёт, в последний день месяца — месячный."""
    n = now()
    today = n.date().isoformat()
    for kind in ("week", "month"):
        if not is_report_day(kind, n.date()):
            continue
        for user in db.users_for_report(kind, n.strftime("%H:%M"), today):
            try:
                await send_report(context.bot, user["user_id"], kind)
            except Exception as exc:
                log.warning("Не удалось отправить отчёт %s: %s", user["user_id"], exc)
            db.update_user(user["user_id"], **{f"report_{kind}_last_sent": today})


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


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await show_today(update, context)


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await show_tasks(update, context)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await ask_new_task(update, context)


async def set_commands(app: Application):
    """Список команд для кнопки «Меню» слева от поля ввода в Telegram."""
    await app.bot.set_my_commands([
        BotCommand("today", "Сегодня"),
        BotCommand("tasks", "Все задачи"),
        BotCommand("new", "Новая задача"),
        BotCommand("summary", "Отчёт за неделю"),
        BotCommand("month", "Отчёт за месяц"),
        BotCommand("weather", "Погода на сегодня"),
        BotCommand("start", "Показать меню внизу экрана"),
    ])


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
    app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

    app.add_handler(CommandHandler(["start", "menu"], start))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("month", month_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # Проверка напоминаний — раз в минуту
    app.job_queue.run_repeating(check_reminders, interval=60, first=5)
    # Ежедневный прогноз погоды — тоже проверяем раз в минуту
    app.job_queue.run_repeating(check_weather, interval=60, first=10)
    # Отчёты за неделю и месяц — тоже проверяем раз в минуту (время у каждого своё)
    app.job_queue.run_repeating(check_reports, interval=60, first=15)
    app.job_queue.run_once(prepare_charts, when=1)

    log.info("Бот запущен! Откройте его в Telegram и отправьте /start. Остановить: Control+C в этом окне.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
