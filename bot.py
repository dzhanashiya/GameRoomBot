"""
GameRoom Booking Bot (MVP) — Bobrovsky Bar x ИСКРА

Стек: Python 3.10+, python-telegram-bot v20, SQLite (sqlite3), python-dotenv
Функции MVP:
- /start: бронировать, мои брони, правила
- Бронирование: выбрать дату (7 ближайших дней), длительность (60/90/120), 
  время старта (с 13:00 до 02:00), допы (3-4 геймпада, 2 гарнитуры),
  расчёт цены (400 ₽/ч 13:00–18:00, 500 ₽/ч 18:00–02:00), буфер 10 минут
- Сбор имени и телефона (контакт или текст)
- Подтверждение и запись в SQLite, проверка пересечений
- Мои брони: список, отмена
- Админ: /admin — список на сегодня/завтра, создание тех. блока, отмена/подтв.

ENV:
BOT_TOKEN=xxxxxxxxx
ADMINS=12345678,87654321   # Telegram user IDs через запятую
TZ=Europe/Moscow
DB_PATH=./bookings.db

Запуск:
python -m pip install python-telegram-bot==20.7 python-dotenv==1.0.1 pytz==2024.1
python bot.py
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Optional, List, Tuple

import pytz
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = {int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x}
TZ = pytz.timezone(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "./bookings.db")

# ---- Константы бизнес-логики ----
OPEN_TIME = time(13, 0)   # 13:00
CLOSE_TIME = time(2, 0)   # 02:00 следующего дня
SLOT_STEP_MIN = 60        # шаг слота (мин)
BUFFER_MIN = 10           # буфер на уборку
DAY_RATE = 400            # 13:00–18:00 ₽/ч
EVE_RATE = 500            # 18:00–02:00 ₽/ч

# Состояния диалога
(CHOOSING_DATE, CHOOSING_DURATION, CHOOSING_TIME, CHOOSING_ADDONS,
 ENTERING_NAME, ENTERING_PHONE, CONFIRMING) = range(7)

@dataclass
class BookingDraft:
    date: Optional[datetime] = None
    duration_min: int = 60
    start_dt: Optional[datetime] = None
    gamepads_mode: str = "duo"  # duo|squad
    headsets: bool = False
    name: Optional[str] = None
    phone: Optional[str] = None

# ---- DB ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  username TEXT,
  name TEXT,
  phone TEXT,
  start_ts INTEGER NOT NULL,  -- epoch seconds (UTC)
  end_ts INTEGER NOT NULL,    -- epoch seconds (UTC)
  duration_min INTEGER NOT NULL,
  addons TEXT,                -- json-ish string: "squad;headsets"
  price_total INTEGER NOT NULL,
  status TEXT NOT NULL,       -- pending|confirmed|cancelled|blocked
  created_ts INTEGER NOT NULL
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)

# ---- Утилиты времени и цены ----

def local_today() -> datetime:
    return datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = TZ.localize(dt_local)
    return dt_local.astimezone(pytz.UTC)


def from_utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, pytz.UTC).astimezone(TZ)


def iter_open_slots(date_local: datetime, duration_min: int) -> List[Tuple[datetime, datetime]]:
    """Вернёт список (start, end) слотов на date_local с учётом закрытия в 02:00 следующего дня и буфера."""
    slots = []
    start_day = date_local.replace(hour=OPEN_TIME.hour, minute=0, second=0, microsecond=0)
    # Закрытие: 02:00 следующего дня
    close_dt = (date_local + timedelta(days=1)).replace(hour=CLOSE_TIME.hour, minute=0, second=0, microsecond=0)
    step = timedelta(minutes=SLOT_STEP_MIN)
    dur = timedelta(minutes=duration_min)
    buf = timedelta(minutes=BUFFER_MIN)

    cur = start_day
    while cur + dur + buf <= close_dt:
        slots.append((cur, cur + dur))
        cur += step
    return slots


def price_for_interval(start_local: datetime, end_local: datetime, gamepads_mode: str, headsets: bool) -> int:
    """Поминутно считаем ставку: днём 400/ч (13-18), вечером 500/ч (18-02). Допы: squad +100/ч, headsets +150 фикс."""
    total = 0
    cur = start_local
    while cur < end_local:
        next_min = min(end_local, cur + timedelta(minutes=1))
        rate = DAY_RATE if time(13,0) <= cur.time() < time(18,0) else EVE_RATE
        total += rate / 60
        cur = next_min
    # Доплаты
    if gamepads_mode == "squad":
        minutes = int((end_local - start_local).total_seconds() // 60)
        total += (100 / 60) * minutes
    if headsets:
        total += 150
    return int(round(total, 0))


def overlaps(conn: sqlite3.Connection, start_utc: datetime, end_utc: datetime) -> bool:
    s = int(start_utc.timestamp())
    e = int(end_utc.timestamp())
    q = """
    SELECT 1 FROM bookings
    WHERE status IN ('pending','confirmed','blocked')
      AND NOT (end_ts <= ? OR start_ts >= ?)
    LIMIT 1;
    """
    row = conn.execute(q, (s, e)).fetchone()
    return row is not None

# ---- Клавиатуры ----

def main_menu_kb():
    return ReplyKeyboardMarkup(
        [["🎮 Забронировать"], ["📅 Мои брони", "ℹ️ Правила"]], resize_keyboard=True
    )


def dates_kb():
    today = local_today()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        buttons.append([InlineKeyboardButton(d.strftime("%a, %d.%m"), callback_data=f"date:{d.strftime('%Y-%m-%d')}")])
    return InlineKeyboardMarkup(buttons)


def duration_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("60 мин", callback_data="dur:60"),
         InlineKeyboardButton("90 мин", callback_data="dur:90"),
         InlineKeyboardButton("120 мин", callback_data="dur:120")]
    ])


def time_kb(date_local: datetime, duration_min: int):
    slots = iter_open_slots(date_local, duration_min)
    buttons = []
    row = []
    now_local = datetime.now(TZ)
    for start, end in slots:
        if start < now_local and date_local.date() == now_local.date():
            continue
        row.append(InlineKeyboardButton(start.strftime("%H:%M"), callback_data=f"time:{start.strftime('%H:%M')}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if not buttons:
        buttons = [[InlineKeyboardButton("Нет доступных слотов", callback_data="noop")]]
    return InlineKeyboardMarkup(buttons)


def addons_kb(draft: BookingDraft):
    squad = "✅ 3–4 геймпада (+100/ч)" if draft.gamepads_mode == "squad" else "3–4 геймпада (+100/ч)"
    duo = "✅ 2 геймпада (вкл.)" if draft.gamepads_mode == "duo" else "2 геймпада (вкл.)"
    hs = "✅ 2 гарнитуры (+150)" if draft.headsets else "2 гарнитуры (+150)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(duo, callback_data="pads:duo")],
        [InlineKeyboardButton(squad, callback_data="pads:squad")],
        [InlineKeyboardButton(hs, callback_data="hs:toggle")],
        [InlineKeyboardButton("Продолжить", callback_data="next:confirm")]
    ])

# ---- Хендлеры ----

user_drafts: dict[int, BookingDraft] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Это геймрум Bobrovsky Bar. Здесь можно забронировать комнату с приставкой по часам.",
        reply_markup=main_menu_kb(),
    )

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text.startswith("🎮"):
        user_drafts[update.effective_user.id] = BookingDraft()
        await update.message.reply_text("Выбери дату (ближайшие 7 дней):", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("Дата:", reply_markup=dates_kb())
        return CHOOSING_DATE
    elif text.startswith("📅"):
        await list_bookings(update, context)
    elif text.startswith("ℹ️"):
        await update.message.reply_text(
            "Правила:\n• Возраст 14+ без сопровождения\n• Буфер между бронированиями 10 мин\n• Опоздание не продлевает слот\n• Аккуратно с обогревателем: дистанция 1 м\n• Запрещено ставить напитки на ТВ/приставку"
        )

async def date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("date:"):
        return CHOOSING_DATE
    _, ds = q.data.split(":", 1)
    d = datetime.strptime(ds, "%Y-%m-%d")
    d = TZ.localize(d)
    draft = user_drafts.get(q.from_user.id, BookingDraft())
    draft.date = d
    user_drafts[q.from_user.id] = draft
    await q.edit_message_text(f"Дата: {d.strftime('%a, %d.%m')}\nВыбери длительность:")
    await q.message.reply_text("Длительность:", reply_markup=duration_kb())
    return CHOOSING_DURATION

async def duration_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("dur:"):
        return CHOOSING_DURATION
    minutes = int(q.data.split(":")[1])
    draft = user_drafts.get(q.from_user.id)
    draft.duration_min = minutes
    user_drafts[q.from_user.id] = draft
    await q.edit_message_text(f"Длительность: {minutes} мин\nВыбери время старта:")
    await q.message.reply_text("Время:", reply_markup=time_kb(draft.date, draft.duration_min))
    return CHOOSING_TIME

async def time_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("time:"):
        return CHOOSING_TIME
    hhmm = q.data.split(":")[1]
    draft = user_drafts.get(q.from_user.id)
    start_local = draft.date.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))
    draft.start_dt = start_local
    user_drafts[q.from_user.id] = draft
    await q.edit_message_text(f"Старт: {start_local.strftime('%d.%m %H:%M')}\nДоп. опции:")
    await q.message.reply_text("Доп. опции:", reply_markup=addons_kb(draft))
    return CHOOSING_ADDONS

async def addons_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    draft = user_drafts.get(q.from_user.id)
    if q.data.startswith("pads:"):
        draft.gamepads_mode = q.data.split(":")[1]
    elif q.data == "hs:toggle":
        draft.headsets = not draft.headsets
    elif q.data == "next:confirm":
        # перейти к подтверждению
        start_local = draft.start_dt
        end_local = start_local + timedelta(minutes=draft.duration_min)
        price = price_for_interval(start_local, end_local, draft.gamepads_mode, draft.headsets)
        await q.edit_message_text(
            "Проверь бронь:\n"
            f"Дата: {start_local.strftime('%d.%m')}\n"
            f"Время: {start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}\n"
            f"Длительность: {draft.duration_min} мин\n"
            f"Опции: {'3–4 геймпада' if draft.gamepads_mode=='squad' else '2 геймпада'}, "
            f"{'2 гарнитуры' if draft.headsets else 'без гарнитур'}\n"
            f"Итого к оплате: {price} ₽\n\n"
            "Напиши, пожалуйста, как к тебе обращаться.")
        return ENTERING_NAME
    # Обновить инлайн клавиатуру
    user_drafts[q.from_user.id] = draft
    await q.edit_message_reply_markup(reply_markup=addons_kb(draft))
    return CHOOSING_ADDONS

async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    draft = user_drafts.get(update.effective_user.id)
    draft.name = name
    user_drafts[update.effective_user.id] = draft
    kb = ReplyKeyboardMarkup([[KeyboardButton("Отправить номер телефона", request_contact=True)]], resize_keyboard=True)
    await update.message.reply_text("Оставь телефон для связи (кнопкой ниже или напиши вручную).", reply_markup=kb)
    return ENTERING_PHONE

async def enter_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = user_drafts.get(update.effective_user.id)
    phone = None
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = (update.message.text or "").strip()
    draft.phone = phone
    user_drafts[update.effective_user.id] = draft

    # Проверка пересечений и запись
    start_local = draft.start_dt
    end_local = start_local + timedelta(minutes=draft.duration_min + BUFFER_MIN)
    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)

    with db() as conn:
        if overlaps(conn, start_utc, end_utc):
            await update.message.reply_text(
                "Увы, слот только что заняли. Выбери другое время:", reply_markup=time_kb(draft.date, draft.duration_min)
            )
            return CHOOSING_TIME
        price = price_for_interval(start_local, start_local + timedelta(minutes=draft.duration_min), draft.gamepads_mode, draft.headsets)
        addons = ";".join([x for x in [draft.gamepads_mode if draft.gamepads_mode=='squad' else None, 'headsets' if draft.headsets else None] if x])
        conn.execute(
            """
            INSERT INTO bookings (user_id, username, name, phone, start_ts, end_ts, duration_min, addons, price_total, status, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                update.effective_user.id,
                update.effective_user.username or "",
                draft.name,
                draft.phone,
                int(to_utc(start_local).timestamp()),
                int(to_utc(start_local + timedelta(minutes=draft.duration_min)).timestamp()),
                draft.duration_min,
                addons,
                price,
                int(datetime.utcnow().timestamp()),
            ),
        )

    admin_note = (
        f"Новая бронь (pending):\n"
        f"Пользователь: @{update.effective_user.username} ({draft.name})\n"
        f"Телефон: {draft.phone}\n"
        f"Когда: {start_local.strftime('%d.%m %H:%M')} — { (start_local + timedelta(minutes=draft.duration_min)).strftime('%H:%M')}\n"
        f"Опции: {draft.gamepads_mode}, {'headsets' if draft.headsets else 'no-hs'}\n"
        f"Цена: {price} ₽"
    )
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_note)
        except Exception:
            pass

    await update.message.reply_text(
        "Заявка принята! Статус: *pending*. Менеджер подтвердит и свяжется с тобой.\n\n"
        "Оплата: на месте/по ссылке от бармена. Если нужно оплатить заранее — напиши сюда, вышлем ссылку.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END

async def list_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND status != 'cancelled' ORDER BY start_ts DESC LIMIT 10",
            (uid,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("Броней пока нет.")
        return
    lines = []
    for r in rows:
        st = from_utc(r["start_ts"]).strftime('%d.%m %H:%M')
        en = from_utc(r["end_ts"]).strftime('%H:%M')
        lines.append(f"#{r['id']} — {st}–{en} • {r['status']} • {r['price_total']} ₽")
    await update.message.reply_text("Твои последние брони:\n" + "\n".join(lines))

# ---- Админ ----
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    today = local_today()
    tomorrow = today + timedelta(days=1)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bookings 
            WHERE start_ts BETWEEN ? AND ?
            ORDER BY start_ts ASC
            """,
            (int(to_utc(today).timestamp()), int(to_utc(tomorrow + timedelta(days=1)).timestamp())),
        ).fetchall()
    if not rows:
        await update.message.reply_text("Сегодня/завтра броней нет.")
        return
    lines = []
    for r in rows:
        st = from_utc(r["start_ts"]).strftime('%d.%m %H:%M')
        en = from_utc(r["end_ts"]).strftime('%H:%M')
        lines.append(f"#{r['id']} — {st}–{en} • {r['status']} • {r['price_total']} ₽ • {r['name']} / {r['phone']}")
    await update.message.reply_text("Сводка на сегодня/завтра:\n" + "\n".join(lines))

async def admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    try:
        _cmd, bid = update.message.text.split()  # формат: confirm 123
        bid = int(bid)
    except Exception:
        await update.message.reply_text("Формат: confirm <id>")
        return
    with db() as conn:
        conn.execute("UPDATE bookings SET status='confirmed' WHERE id=?", (bid,))
    await update.message.reply_text(f"Бронь #{bid} подтверждена.")

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    try:
        _cmd, bid = update.message.text.split()
        bid = int(bid)
    except Exception:
        await update.message.reply_text("Формат: cancel <id>")
        return
    with db() as conn:
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    await update.message.reply_text(f"Бронь #{bid} отменена.")

async def admin_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    # block 2025-11-15 18:00 120   — тех.блок на 120 мин
    try:
        _cmd, date_s, time_s, dur_s = update.message.text.split()
        d = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
        d = TZ.localize(d)
        dur = int(dur_s)
    except Exception:
        await update.message.reply_text("Формат: block YYYY-MM-DD HH:MM <мин>")
        return

    start_utc = to_utc(d)
    end_utc = to_utc(d + timedelta(minutes=dur))
    with db() as conn:
        if overlaps(conn, start_utc, end_utc):
            await update.message.reply_text("Нельзя: пересечение с существующей бронью.")
            return
        conn.execute(
            """
            INSERT INTO bookings (user_id, username, name, phone, start_ts, end_ts, duration_min, addons, price_total, status, created_ts)
            VALUES (0, '', 'BLOCK', '', ?, ?, ?, '', 0, 'blocked', ?)
            """,
            (int(start_utc.timestamp()), int(end_utc.timestamp()), dur, int(datetime.utcnow().timestamp())),
        )
    await update.message.reply_text("Тех. блок добавлен.")

# ---- Конфиг диалога ----

def build_app() -> Application:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
        states={
            CHOOSING_DATE: [CallbackQueryHandler(date_chosen)],
            CHOOSING_DURATION: [CallbackQueryHandler(duration_chosen)],
            CHOOSING_TIME: [CallbackQueryHandler(time_chosen)],
            CHOOSING_ADDONS: [CallbackQueryHandler(addons_toggle)],
            ENTERING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)],
            ENTERING_PHONE: [MessageHandler((filters.CONTACT | filters.TEXT) & ~filters.COMMAND, enter_phone)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    # Админ-команды
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.Regex(r"^confirm \\d+$"), admin_confirm))
    app.add_handler(MessageHandler(filters.Regex(r"^cancel \\d+$"), admin_cancel))
    app.add_handler(MessageHandler(filters.Regex(r"^block \\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2} \\d+$"), admin_block))

    return app


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set in .env")
    app = build_app()
    print("Booking bot is running…")
    app.run_polling()
