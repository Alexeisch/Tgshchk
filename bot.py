"""
Telegram-бот «Планировщик задач».

Запуск:  python bot.py   (или зелёная кнопка ▶ в PyCharm)
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
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
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


def task_card(title, place, person, due_date, due_time, extra="") -> str:
    """Красивое описание задачи для сообщения."""
    e = html.escape
    lines = [f"📌 <b>{e(title)}</b>", f"📅 Дата: {fmt_date(due_date)}"]
    if due_time:
        lines.append(f"🕐 Время: {fmt_time(due_time)}")
    if place:
        lines.append(f"📍 Место: {e(place)}")
    if person:
        lines.append(f"👤 {e(person)}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def card_from_row(task) -> str:
    extra = ""
    if task["status"] == "done":
        extra = f"✅ Выполнено: {task['completed_at']}"
    elif is_overdue(task):
        extra = "⚠️ <b>Срок прошёл!</b>"
    return task_card(task["title"], task["place"], task["person"],
                     task["due_date"], task["due_time"], extra)


def short_label(task) -> str:
    """Короткая подпись задачи для кнопки в списке."""
    parts = []
    if is_overdue(task):
        parts.append("⚠️")
    if task["due_date"]:
        d = date.fromisoformat(task["due_date"])
        parts.append(d.strftime("%d.%m"))
    if task["due_time"]:
        parts.append(task["due_time"])
    label = " ".join(parts)
    label = f"{label} · {task['title']}" if label else task["title"]
    return label if len(label) <= 60 else label[:57] + "…"


# ---------- Клавиатуры (кнопки) ----------

def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )


MAIN_MENU = kb([
    [("➕ Создать новую задачу", "new")],
    [("📋 Текущие задачи", "list:all")],
    [("✅ Выполненные задачи", "done_list")],
])

BACK_TO_MENU = kb([[("🏠 Главное меню", "menu")]])


def draft_keyboard():
    return kb([
        [("💾 Сохранить", "draft_save")],
        [("📅 Изменить дату/время", "draft_when"), ("✏️ Заново", "new")],
        [("❌ Отмена", "menu")],
    ])


def task_keyboard(task):
    tid = task["id"]
    if task["status"] == "done":
        return kb([
            [("↩️ Вернуть в текущие", f"undone:{tid}")],
            [("🗑 Удалить", f"del:{tid}")],
            [("⬅️ К выполненным", "done_list"), ("🏠 Меню", "menu")],
        ])
    return kb([
        [("✅ Отметить выполненной", f"done:{tid}")],
        [("📅 Перенести", f"resched:{tid}"), ("✏️ Изменить", f"edit:{tid}")],
        [("🗑 Удалить", f"del:{tid}")],
        [("⬅️ К списку", "list:all"), ("🏠 Меню", "menu")],
    ])


# ---------- Вспомогательное ----------

def allowed(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED_USERS or (user and user.id in ALLOWED_USERS)


def greeting(update: Update) -> str:
    name = html.escape(update.effective_user.first_name or "друг")
    return (f"👋 Здравствуйте, <b>{name}</b>!\n\n"
            "Я ваш планировщик задач. Выберите действие:\n\n"
            "<i>Подсказка: можно просто написать мне задачу, например:\n"
            "«6 числа встреча по адресу ул. Ленина 5 с Иваном в 15:00»</i>")


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


# ---------- Главное меню ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Извините, это личный бот 🙂")
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
        "✍️ <b>Напишите задачу одним сообщением.</b>\n\n"
        "Можно указать дату, время, место и человека — я сам всё разберу.\n\n"
        "Примеры:\n"
        "• <code>6 числа встреча по адресу ул. Ленина 5 с Иваном Петровым в 15:00</code>\n"
        "• <code>Завтра в 10 утра позвонить маме</code>\n"
        "• <code>15.11 сдать отчёт</code>\n"
        "• <code>В пятницу в 19:00 ужин со Светланой</code>\n"
        "• <code>Купить продукты</code> (без срока)"
    )
    await reply_or_edit(update, text, kb([[("❌ Отмена", "menu")]]))


async def show_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["draft"]
    extra = ""
    if d["due_date"] and d["due_date"] < now().date():
        extra = "⚠️ Эта дата уже прошла — если это ошибка, нажмите «Изменить дату/время»."
    elif not d["due_date"]:
        extra = "ℹ️ Дата не указана — задача будет без срока и без напоминаний."
    text = "🔎 <b>Вот что я понял:</b>\n\n" + task_card(
        d["title"], d["place"], d["person"], d["due_date"], d["due_time"], extra
    ) + "\n\nВсё верно?"
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
        note = "\n\n🔔 Я напомню о ней за день и за пару часов до начала."
    await reply_or_edit(
        update,
        "✅ <b>Задача сохранена!</b>\n\n"
        + task_card(d["title"], d["place"], d["person"], d["due_date"], d["due_time"]) + note,
        kb([[("➕ Ещё задача", "new"), ("📋 Текущие", "list:all")], [("🏠 Главное меню", "menu")]]),
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

    filter_row = [((("• " + name + " •") if key == period else name), f"list:{key}")
                  for key, name in FILTERS.items()]
    rows = [filter_row]
    rows += [[(short_label(t), f"task:{t['id']}")] for t in tasks[:40]]
    rows.append([("➕ Новая задача", "new"), ("🏠 Меню", "menu")])

    if tasks:
        text = (f"📋 <b>Текущие задачи</b> ({FILTERS[period].lower()}): {len(tasks)}\n\n"
                "Нажмите на задачу, чтобы отметить её выполненной, перенести или изменить.")
        if len(tasks) > 40:
            text += "\n\nПоказаны первые 40."
    else:
        text = f"📋 <b>Текущие задачи</b> ({FILTERS[period].lower()})\n\nЗадач нет 🎉"
    await reply_or_edit(update, text, kb(rows))


async def show_done_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    tasks = db.get_done_tasks(update.effective_user.id)
    rows = [[("✅ " + short_label(t), f"task:{t['id']}")] for t in tasks]
    rows.append([("🏠 Главное меню", "menu")])
    if tasks:
        text = f"✅ <b>Выполненные задачи</b> (последние {len(tasks)})\n\nНажмите на задачу, чтобы открыть её."
    else:
        text = "✅ <b>Выполненные задачи</b>\n\nПока ничего не выполнено. Всё впереди! 💪"
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
    text = (card_from_row(task) + "\n\n📅 <b>На когда перенести?</b>\n\n"
            "Нажмите кнопку или напишите новую дату и/или время, например:\n"
            "<code>завтра в 18:00</code>, <code>12.11</code>, <code>в 16:30</code>, "
            "<code>в понедельник</code>")
    await reply_or_edit(update, text, kb([
        [("+1 час", f"shift:{task_id}:h1"), ("+1 день", f"shift:{task_id}:d1")],
        [("+1 неделя", f"shift:{task_id}:w1"), ("Без срока", f"shift:{task_id}:none")],
        [("⬅️ Назад", f"task:{task_id}")],
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
    await reply_or_edit(update, "📅 <b>Задача перенесена.</b>\n\n" + card_from_row(db.get_task(task_id, uid)),
                        task_keyboard(db.get_task(task_id, uid)))


async def ask_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await show_menu(update, context)
        return
    context.user_data.clear()
    context.user_data["state"] = WAIT_EDIT_TEXT
    context.user_data["task_id"] = task_id
    text = (card_from_row(task) + "\n\n✏️ <b>Напишите новый текст задачи.</b>\n\n"
            "Можно указать и новое место, человека, дату и время — я их обновлю. "
            "Если дату не укажете, она останется прежней.\n\n"
            f"Текущий текст: <code>{html.escape(task['title'])}</code>")
    await reply_or_edit(update, text, kb([
        [("📅 Изменить только дату/время", f"resched:{task_id}")],
        [("⬅️ Назад", f"task:{task_id}")],
    ]))


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    uid = update.effective_user.id
    db.set_done(task_id, uid, now())
    await update.callback_query.answer("Отлично! Задача выполнена ✅")
    task = db.get_task(task_id, uid)
    if task:
        await reply_or_edit(update, "🎉 <b>Задача выполнена!</b>\n\n" + card_from_row(task),
                            kb([[("📋 К текущим задачам", "list:all")], [("🏠 Главное меню", "menu")]]))


async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int):
    task = db.get_task(task_id, update.effective_user.id)
    if not task:
        await show_menu(update, context)
        return
    await reply_or_edit(update, card_from_row(task) + "\n\n🗑 <b>Точно удалить задачу?</b>",
                        kb([[("Да, удалить", f"delyes:{task_id}"), ("Нет", f"task:{task_id}")]]))


# ---------- Обработка нажатий на кнопки ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Это личный бот 🙂")
        return
    data = query.data
    action, _, arg = data.partition(":")

    if action == "done":
        await mark_done(update, context, int(arg))
        return  # mark_done сам вызывает query.answer
    await query.answer()

    if action == "menu":
        await show_menu(update, context)
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
        await reply_or_edit(update, "🗑 Задача удалена.",
                            kb([[("📋 К текущим задачам", "list:all")], [("🏠 Главное меню", "menu")]]))
    elif action == "draft_save":
        await save_draft(update, context)
    elif action == "draft_when":
        if "draft" not in context.user_data:
            await show_menu(update, context)
            return
        context.user_data["state"] = WAIT_DRAFT_WHEN
        await reply_or_edit(
            update,
            "📅 Напишите дату и/или время, например:\n"
            "<code>завтра в 18:00</code>, <code>6 ноября</code>, <code>12.11 в 9:30</code>",
            kb([[("Сегодня", "draft_day:0"), ("Завтра", "draft_day:1")],
                [("Без срока", "draft_day:none")],
                [("⬅️ Назад", "draft_back")]]),
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
        await update.message.reply_text("Извините, это личный бот 🙂")
        return
    db.save_user(update.effective_user.id, update.effective_user.first_name or "")
    text = update.message.text.strip()
    state = context.user_data.get("state")
    uid = update.effective_user.id
    n = now()

    if state == WAIT_DRAFT_WHEN:
        d_new, t_new = parse_when(text, n)
        if not d_new and not t_new:
            await update.message.reply_text(
                "🤔 Не смог распознать дату. Попробуйте так: «завтра в 18:00» или «12.11».")
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
                "🤔 Не смог распознать дату. Попробуйте так: «завтра в 18:00», «12.11» или «в 16:30».")
            return
        d = d_new or (date.fromisoformat(task["due_date"]) if task["due_date"] else n.date())
        t = t_new or (time.fromisoformat(task["due_time"]) if task["due_time"] else None)
        db.update_when(task_id, uid, d, t, n)
        context.user_data.clear()
        task = db.get_task(task_id, uid)
        await update.message.reply_text("📅 <b>Задача перенесена.</b>\n\n" + card_from_row(task),
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
        await update.message.reply_text("✏️ <b>Задача изменена.</b>\n\n" + card_from_row(task),
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
        head = "🔔 <b>Напоминание о задаче</b>" if num == 1 else "⏰ <b>Скоро начало!</b>"
        if num == 2 and not task["due_time"]:
            head = "📌 <b>Напоминание: задача на сегодня</b>"
        try:
            await context.bot.send_message(
                task["user_id"], head + "\n\n" + card_from_row(task),
                reply_markup=kb([[("✅ Выполнено", f"done:{task['id']}"),
                                  ("📅 Перенести", f"resched:{task['id']}")]]),
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
        return f"📊 <b>Сводка за неделю {period}</b>\n\nНа этой неделе задач не было."

    lines = [f"📊 <b>Сводка за неделю {period}</b>", ""]
    if planned:
        pct_done = round(len(done) * 100 / len(planned))
        bar = "🟩" * round(pct_done / 10) + "⬜" * (10 - round(pct_done / 10))
        lines += [
            f"Запланировано задач: <b>{len(planned)}</b>",
            f"✅ Выполнено: <b>{len(done)}</b> ({pct_done}%)",
            f"❌ Не выполнено: <b>{len(not_done)}</b> ({100 - pct_done}%)",
            "",
            bar,
        ]
        if pct_done == 100:
            lines.append("\n🏆 Великолепно! Все задачи выполнены!")
        elif pct_done >= 70:
            lines.append("\n💪 Отличный результат!")
        elif pct_done >= 40:
            lines.append("\n👍 Неплохо, но есть куда расти.")
        else:
            lines.append("\n🙂 Неделя была непростой. Следующая будет лучше!")
    if extra_done:
        lines.append(f"\nДополнительно выполнено задач без срока: <b>{len(extra_done)}</b>")
    if not_done:
        lines.append("\n<b>Не выполнены:</b>")
        for t in not_done[:15]:
            lines.append(f"• {html.escape(short_label(t))}")
    return "\n".join(lines)


async def weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    """Каждое воскресенье в 16:00 отправляет сводку всем пользователям."""
    today = now().date()
    for user in db.all_users():
        try:
            await context.bot.send_message(
                user["user_id"], build_summary(user["user_id"], today),
                reply_markup=kb([[("📋 Текущие задачи", "list:all")]]),
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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # Проверка напоминаний — раз в минуту
    app.job_queue.run_repeating(check_reminders, interval=60, first=5)
    # Недельная сводка — по воскресеньям в 16:00 (в библиотеке 0 = воскресенье)
    app.job_queue.run_daily(weekly_summary, time=time(16, 0, tzinfo=TZ), days=(0,))

    log.info("Бот запущен! Откройте его в Telegram и отправьте /start. Остановить: Ctrl+C или ■ в PyCharm.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
