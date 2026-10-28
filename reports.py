"""
Отчёты за неделю и за месяц: подсчёт статистики и картинка с диаграммами.

Каждая задача со сроком в выбранном периоде попадает в одну из групп:
  - выполнено     — отмечена выполненной;
  - не выполнено  — срок уже прошёл, а задача не отмечена;
  - в процессе    — срок ещё не наступил (например, задача на вечер воскресенья).
"""

import html
from datetime import date, datetime, time, timedelta
from io import BytesIO

import database as db

# Чёрно-серая палитра: выполнено / не выполнено / в процессе
COLORS = ["#111111", "#8c8c8c", "#d9d9d9"]
GROUPS = ["Выполнено", "Не выполнено", "В процессе"]

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
          "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


# ---------- Периоды ----------

def week_period(today: date) -> dict:
    start = today - timedelta(days=today.weekday())  # понедельник
    end = start + timedelta(days=6)                  # воскресенье
    return {"kind": "week", "start": start, "end": end, "title": "Итоги недели",
            "subtitle": f"{start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}",
            "prev_name": "прошлой неделе"}


def month_period(today: date) -> dict:
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return {"kind": "month", "start": start, "end": end, "title": "Итоги месяца",
            "subtitle": f"{MONTHS[start.month - 1]} {start.year}",
            "prev_name": "прошлому месяцу"}


def previous_period(period: dict) -> dict:
    if period["kind"] == "week":
        return week_period(period["start"] - timedelta(days=7))
    return month_period(period["start"] - timedelta(days=1))


def is_last_day_of_month(d: date) -> bool:
    return (d + timedelta(days=1)).day == 1


# ---------- Подсчёт ----------

def _group(task, now: datetime) -> int:
    """0 — выполнено, 1 — не выполнено, 2 — в процессе."""
    if task["status"] == "done":
        return 0
    due = date.fromisoformat(task["due_date"])
    if task["due_time"]:
        deadline = datetime.combine(due, time.fromisoformat(task["due_time"]))
    else:
        deadline = datetime.combine(due + timedelta(days=1), time(0, 0))  # до конца дня
    return 1 if deadline <= now.replace(tzinfo=None) else 2


def _buckets(period: dict) -> list:
    """Столбики диаграммы: дни недели или отрезки месяца по 7 дней."""
    start, end = period["start"], period["end"]
    if period["kind"] == "week":
        return [(WEEKDAYS_SHORT[i], start + timedelta(days=i), start + timedelta(days=i)) for i in range(7)]
    result, d = [], start
    while d <= end:
        last = min(d + timedelta(days=6), end)
        result.append((f"{d.day}–{last.day}", d, last))
        d = last + timedelta(days=1)
    return result


def collect(user_id: int, period: dict, now: datetime, category_id: int = None) -> dict:
    """Статистика за период. category_id — только задачи этой вкладки (None — все задачи)."""
    tasks = db.tasks_for_period(user_id, period["start"], period["end"])
    extra = db.done_without_date(user_id, period["start"], period["end"])
    if category_id is not None:
        tasks = [t for t in tasks if t["category_id"] == category_id]
        extra = [t for t in extra if t["category_id"] == category_id]
    counts = [0, 0, 0]
    missed = []
    ranges = _buckets(period)
    buckets = [[label, 0, 0, 0] for label, _, _ in ranges]
    for t in tasks:
        g = _group(t, now)
        counts[g] += 1
        if g == 1:
            missed.append(t)
        due = date.fromisoformat(t["due_date"])
        i = next(i for i, (_, b_start, b_end) in enumerate(ranges) if b_start <= due <= b_end)
        buckets[i][g + 1] += 1
    total = sum(counts)
    return {
        "counts": counts,
        "total": total,
        "percents": [round(c * 100 / total) if total else 0 for c in counts],
        "missed": missed,
        "buckets": buckets,
        "extra_done": len(extra),
        "by_tab": _by_tab(user_id, tasks) if category_id is None else [],
    }


def _by_tab(user_id: int, tasks) -> list:
    """Разбивка по вкладкам: [(название, выполнено, всего), ...]. Пусто, если вкладок нет."""
    tabs = db.get_categories(user_id)
    if not tabs:
        return []
    names = {c["id"]: c["name"] for c in tabs}
    stat = {}
    for t in tasks:
        key = t["category_id"] if t["category_id"] in names else None
        done, total = stat.get(key, (0, 0))
        stat[key] = (done + (t["status"] == "done"), total + 1)
    rows = [(names[c["id"]], *stat[c["id"]]) for c in tabs if c["id"] in stat]
    if None in stat:
        rows.append(("Без вкладки", *stat[None]))
    return rows


# ---------- Картинка ----------

def prepare():
    """
    Загружает библиотеку диаграмм заранее. При самом первом запуске она составляет список
    шрифтов компьютера — на Mac это может занять 1–2 минуты, потом всё работает быстро.
    """
    import matplotlib.font_manager  # noqa: F401  (сама загрузка и строит список шрифтов)


def render_chart(period: dict, stats: dict) -> bytes:
    """Рисует картинку: кольцевая диаграмма слева и столбики по дням/неделям справа."""
    # Импорт здесь, а не в начале файла, чтобы бот запускался сразу, не дожидаясь библиотеки
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(9, 4.6), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)
    fig.text(0.04, 0.93, period["title"].upper(), fontsize=17, fontweight="bold", color="#111111")
    fig.text(0.04, 0.87, period["subtitle"], fontsize=11, color="#777777")

    # Кольцевая диаграмма
    ax = fig.add_axes([0.02, 0.17, 0.36, 0.64])
    counts = stats["counts"]
    ax.pie(counts if any(counts) else [1], colors=COLORS if any(counts) else ["#eeeeee"],
           startangle=90, counterclock=False,
           wedgeprops={"width": 0.28, "edgecolor": "white", "linewidth": 2})
    ax.text(0, 0.08, f"{stats['percents'][0]}%", ha="center", va="center",
            fontsize=26, fontweight="bold", color="#111111")
    ax.text(0, -0.22, "выполнено", ha="center", va="center", fontsize=10, color="#777777")
    ax.set_aspect("equal")

    # Подписи под кольцом
    for i, name in enumerate(GROUPS):
        y = 0.12 - i * 0.045
        fig.patches.append(_square(fig, 0.06, y, COLORS[i]))
        fig.text(0.085, y, name, fontsize=9.5, color="#333333", va="center")
        fig.text(0.34, y, f"{counts[i]}  ·  {stats['percents'][i]}%", fontsize=9.5,
                 color="#333333", va="center", ha="right")

    # Столбики
    bx = fig.add_axes([0.47, 0.17, 0.5, 0.62])
    labels = [b[0] for b in stats["buckets"]]
    bottom = [0] * len(labels)
    for g in range(3):
        values = [b[g + 1] for b in stats["buckets"]]
        bx.bar(labels, values, bottom=bottom, color=COLORS[g], width=0.62, edgecolor="white", linewidth=0.8)
        bottom = [a + b for a, b in zip(bottom, values)]
    bx.set_title("Задачи по дням" if period["kind"] == "week" else "Задачи по неделям",
                 loc="left", fontsize=10, color="#777777", pad=10)
    for side in ("top", "right", "left"):
        bx.spines[side].set_visible(False)
    bx.spines["bottom"].set_color("#bbbbbb")
    bx.tick_params(colors="#555555", labelsize=9, length=0)
    bx.yaxis.get_major_locator().set_params(integer=True)
    bx.grid(axis="y", color="#eeeeee", linewidth=0.8)
    bx.set_axisbelow(True)
    if max(bottom, default=0) == 0:
        bx.set_ylim(0, 1)

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    return buf.getvalue()


def _square(fig, x, y, color):
    from matplotlib.patches import Rectangle
    return Rectangle((x - 0.006, y - 0.012), 0.016, 0.024, transform=fig.transFigure,
                     facecolor=color, edgecolor="none")


# ---------- Текст ----------

def caption(period: dict, stats: dict, prev_stats: dict) -> str:
    """Подпись к картинке (не длиннее 1024 символов — ограничение Telegram)."""
    head = f"<b>{period['title'].upper()}</b>\n<i>{period['subtitle']}</i>\n\n"
    if not stats["total"] and not stats["extra_done"]:
        return head + "За этот период задач со сроком не было."

    rows = [f"{name:<14}{stats['counts'][i]:>4}{stats['percents'][i]:>6}%" for i, name in enumerate(GROUPS)]
    rows.append(f"{'Всего':<14}{stats['total']:>4}")
    text = head + "<pre>" + "\n".join(rows) + "</pre>"

    if stats["total"] and prev_stats["total"]:
        diff = stats["percents"][0] - prev_stats["percents"][0]
        sign = "+" if diff > 0 else ""
        text += f"\nВыполнено {stats['percents'][0]}% — {sign}{diff} п.п. к {period['prev_name']}."
    if stats["extra_done"]:
        text += f"\nТакже выполнено задач без срока: {stats['extra_done']}."
    if stats["by_tab"]:
        rows = []
        for name, done, total in stats["by_tab"]:
            short = name if len(name) <= 13 else name[:12] + "…"
            rows.append(f"{html.escape(short):<14}{done:>3}/{total:<3}{round(done * 100 / total):>4}%")
        text += "\n\n<b>По вкладкам</b>\n<pre>" + "\n".join(rows) + "</pre>"

    if stats["missed"]:
        lines = [html.escape(f"{date.fromisoformat(t['due_date']).strftime('%d.%m')} — {t['title']}"[:60])
                 for t in stats["missed"]]
        # Показываем до 8 задач, а если подпись не влезает в лимит — меньше
        for limit in range(min(8, len(lines)), 0, -1):
            shown = lines[:limit]
            if len(lines) > limit:
                shown.append(f"и ещё {len(lines) - limit}")
            block = "\n\n<b>Не выполнены</b>\n<blockquote>" + "\n".join(shown) + "</blockquote>"
            if len(text + block) <= 1024:
                text += block
                break
    return text


def build(user_id: int, period: dict, now: datetime, category_id: int = None):
    """
    Готовит отчёт. Возвращает (подпись, картинка или None, если задач не было).
    category_id — отчёт только по одной вкладке.
    """
    if category_id is not None:
        tab = db.get_category(category_id, user_id)
        period = {**period, "subtitle": f"{tab['name']} · {period['subtitle']}"}
    stats = collect(user_id, period, now, category_id)
    prev_stats = collect(user_id, previous_period(period), now, category_id)
    text = caption(period, stats, prev_stats)
    image = render_chart(period, stats) if stats["total"] else None
    return text, image
