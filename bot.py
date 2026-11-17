"""
Бот "Сплетница" для Telegram (aiogram 3).

РАЗДЕЛ «СПЛЕТНИ» (по умолчанию, /main):
  Пришли свою сплетню (текстом или фото), получи чужую. К каждой сплетне бот в конце дописывает
  «от Коли» (случайное имя из списка), например: «я сегодня был пьяный. от Коли».
  Под чужой сплетней кнопки «Согласен» / «Не согласен». 3 и больше «Не согласен» = сплетня удаляется у всех.

РАЗДЕЛ «ВЫСКАЗАТЬСЯ» (/vent):
  Человек анонимно пишет мысль (только текст), получает чью-то чужую мысль. Под мыслью кнопка «Ответить»:
  ответ уходит автору анонимно, дальше они могут переписываться сколько угодно, оба остаются анонимными.
  Любой участник может закрыть диалог кнопкой «Закрыть».

Команды админа (ADMIN_ID):
  (автоматически) копия каждой новой сплетни и каждой новой мысли: содержимое, автор, /del и /ban.
  Личные диалоги в «Высказаться» админу НЕ пересылаются.
  /last [N]        - последние N сплетен/мыслей (по умолчанию 10, максимум 30)
  /reported [N]    - сплетни, у которых есть голоса «Не согласен»
  /del <номер>     - удалить сплетню/мысль у всех
  /ban <id>        - забанить автора, удалить его сплетни, закрыть его диалоги
  /unban <id>      - разбанить
  /names           - список имён для игры; /addname Имя; /delname Имя
  /stats           - статистика

Для всех: /vent, /main, /notify (вкл/выкл уведомления «появились новые сплетни»), /id.

Переменные окружения:
  BOT_TOKEN  - токен от @BotFather (обязательно)
  ADMIN_ID   - ваш Telegram ID (для модерации)
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "gossip.db")

BOT_VERSION = "v7: сплетни с подписью «от Имени» + раздел «Высказаться» (раздела 18+ нет, нижних кнопок нет)"

MIN_LEN = 5                  # минимальная длина сплетни
MIN_LEN_VENT = 3             # минимальная длина мысли в «Высказаться»
MAX_LEN = 4000               # максимальная длина текста (лимит Телеграма 4096, остаётся запас под заголовок и «от Имени»)
MAX_CAPTION = 950            # максимальная длина подписи к фото (лимит Телеграма 1024, нужен запас под «от Имени»)
DAILY_LIMIT = 10000          # лимит сплетен/мыслей в сутки на человека (10000 = практически без ограничений)
DISLIKE_LIMIT = 3            # сколько «Не согласен» нужно, чтобы сплетня удалилась
BROADCAST_EVERY = 5          # после скольких новых сплетен оповещать всех пользователей
BROADCAST_COOLDOWN_MIN = 30  # не чаще, чем раз в N минут (0 = без паузы, но это быстро надоест людям)

# Имена для игры. Это только начальный список: потом меняйте командами /addname и /delname.
DEFAULT_NAMES = ["Маша", "Дима", "Коля", "Никита", "Соня", "Дима Ш", "Арина"]

# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        banned INTEGER DEFAULT 0,
        created_at TEXT,
        notify INTEGER DEFAULT 1,
        section TEXT DEFAULT 'main',
        adult_ok INTEGER DEFAULT 0,
        active_thread INTEGER
    );
    CREATE TABLE IF NOT EXISTS gossips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id INTEGER,
        text TEXT,
        photo_id TEXT,
        created_at TEXT,
        hidden INTEGER DEFAULT 0,
        section TEXT DEFAULT 'main',
        name TEXT
    );
    CREATE TABLE IF NOT EXISTS seen (
        user_id INTEGER,
        gossip_id INTEGER,
        PRIMARY KEY (user_id, gossip_id)
    );
    CREATE TABLE IF NOT EXISTS votes (
        gossip_id INTEGER,
        user_id INTEGER,
        vote INTEGER,
        PRIMARY KEY (gossip_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS deliveries (
        gossip_id INTEGER,
        user_id INTEGER,
        message_id INTEGER,
        PRIMARY KEY (gossip_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS reports (
        gossip_id INTEGER,
        reporter_id INTEGER,
        PRIMARY KEY (gossip_id, reporter_id)
    );
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS pending (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT,
        section TEXT DEFAULT 'main'
    );
    CREATE TABLE IF NOT EXISTS names (
        name TEXT PRIMARY KEY,
        gen TEXT
    );
    CREATE TABLE IF NOT EXISTS threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gossip_id INTEGER,
        author_id INTEGER,
        responder_id INTEGER,
        created_at TEXT,
        closed INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0,
        UNIQUE (gossip_id, responder_id)
    );
    """
)
# Если база осталась от старой версии бота, добавляем новые столбцы
for _table, _col, _ddl in [
    ("users", "notify", "INTEGER DEFAULT 1"),
    ("users", "section", "TEXT DEFAULT 'main'"),
    ("users", "adult_ok", "INTEGER DEFAULT 0"),
    ("users", "active_thread", "INTEGER"),
    ("gossips", "photo_id", "TEXT"),
    ("gossips", "section", "TEXT DEFAULT 'main'"),
    ("gossips", "name", "TEXT"),
    ("pending", "section", "TEXT DEFAULT 'main'"),
    ("names", "gen", "TEXT"),
]:
    _cols = {r["name"] for r in db.execute(f"PRAGMA table_info({_table})")}
    if _col not in _cols:
        db.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")

# Раздел 18+ убран: старые его сплетни скрываем, людей возвращаем в основной раздел
db.execute("UPDATE gossips SET hidden = 1 WHERE section = 'adult'")
db.execute("UPDATE users SET section = 'main' WHERE section = 'adult'")
db.execute("DELETE FROM pending WHERE section = 'adult'")

# Начальный список имён (один раз; дальше им управляет админ)
if not db.execute("SELECT 1 FROM meta WHERE key = 'names_seeded'").fetchone():
    db.executemany("INSERT OR IGNORE INTO names (name) VALUES (?)", [(n,) for n in DEFAULT_NAMES])
    db.execute("INSERT INTO meta (key, value) VALUES ('names_seeded', '1')")
db.commit()


def ensure_user(user) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
        (user.id, user.username, datetime.now().isoformat()),
    )
    db.commit()


def is_banned(user_id: int) -> bool:
    row = db.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def get_state(user_id: int) -> tuple[str, int | None]:
    """Возвращает (раздел, номер активного диалога или None)."""
    row = db.execute("SELECT section, active_thread FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return "main", None
    return (row["section"] or "main"), row["active_thread"]


def get_section(user_id: int) -> str:
    return get_state(user_id)[0]


def set_section(user_id: int, section: str) -> None:
    """Переключает раздел, выходит из диалога и убирает человека из очереди ожидания."""
    db.execute("UPDATE users SET section = ?, active_thread = NULL WHERE user_id = ?", (section, user_id))
    db.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
    db.commit()


def posts_today(user_id: int) -> int:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return db.execute(
        "SELECT COUNT(*) FROM gossips WHERE author_id = ? AND created_at >= ?",
        (user_id, start),
    ).fetchone()[0]


def pick_gossip_for(user_id: int, section: str = "main"):
    """Случайная чужая, ещё не виденная и не удалённая сплетня/мысль из того же раздела."""
    return db.execute(
        """
        SELECT id, text, photo_id FROM gossips
        WHERE hidden = 0
          AND section = ?
          AND author_id != ?
          AND id NOT IN (SELECT gossip_id FROM seen WHERE user_id = ?)
        ORDER BY RANDOM() LIMIT 1
        """,
        (section, user_id, user_id),
    ).fetchone()


# --------------------------------------------------------------------------
# Имена для игры
# --------------------------------------------------------------------------
def get_names() -> list[str]:
    return [r["name"] for r in db.execute("SELECT name FROM names ORDER BY rowid")]


def random_name() -> str | None:
    names = get_names()
    return random.choice(names) if names else None


def _decline_word(w: str) -> str:
    """Родительный падеж для одного имени: Коля -> Коли, Маша -> Маши, Дима -> Димы, Иван -> Ивана."""
    low = w.lower()
    if low.endswith("я"):
        return w[:-1] + "и"
    if low.endswith("а"):
        return w[:-1] + ("и" if len(w) > 1 and w[-2].lower() in "гжкхчшщ" else "ы")
    if low.endswith(("й", "ь")):
        return w[:-1] + "я"
    if low[-1] in "бвгджзклмнпрстфхцчшщ":
        return w + "а"
    return w  # несклоняемые имена оставляем как есть


def name_genitive(name: str) -> str:
    """Форма «от кого». Можно задать вручную: /addname Лёша=Лёши. Иначе склоняем первое слово имени."""
    row = db.execute("SELECT gen FROM names WHERE name = ?", (name,)).fetchone()
    if row and row["gen"]:
        return row["gen"]
    parts = name.split(" ")
    return " ".join([_decline_word(parts[0])] + parts[1:])


def with_name(text: str | None, name: str | None) -> str:
    """Дописывает в конец сплетни подпись: «я сегодня был пьяный. от Коли»."""
    text = (text or "").rstrip()
    if not name:
        return text
    sender = f"от {name_genitive(name)}"
    if not text:
        return sender
    if text[-1] not in ".!?…":
        text += "."
    return f"{text} {sender}"


# --------------------------------------------------------------------------
# Кнопки
# --------------------------------------------------------------------------
def vote_counts(gossip_id: int) -> tuple[int, int]:
    row = db.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) "
        "FROM votes WHERE gossip_id = ?",
        (gossip_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def vote_keyboard(gossip_id: int) -> InlineKeyboardMarkup:
    agree, disagree = vote_counts(gossip_id)
    a = f"👍 Согласен · {agree}" if agree else "👍 Согласен"
    d = f"👎 Не согласен · {disagree}" if disagree else "👎 Не согласен"
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=a, callback_data=f"v:{gossip_id}:1"),
            InlineKeyboardButton(text=d, callback_data=f"v:{gossip_id}:-1"),
        ]]
    )


def thought_keyboard(gossip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Ответить", callback_data=f"t:{gossip_id}")]]
    )


def relay_keyboard(thread_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Ответить", callback_data=f"r:{thread_id}"),
            InlineKeyboardButton(text="🚫 Закрыть", callback_data=f"x:{thread_id}"),
        ]]
    )


# --------------------------------------------------------------------------
# Отправка и удаление сплетен
# --------------------------------------------------------------------------
async def send_gossip(bot: Bot, user_id: int, gossip_id: int, text: str, photo_id: str | None = None) -> bool:
    """Отправляет сплетню или мысль. False, если пользователь заблокировал бота."""
    srow = db.execute("SELECT section, name FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
    if srow and srow["section"] == "vent":
        kb, title, shown = thought_keyboard(gossip_id), "💭 Чья-то мысль", text
    else:
        kb, title = vote_keyboard(gossip_id), "🤫 Чужая сплетня"
        shown = with_name(text, srow["name"] if srow else None)
    try:
        if photo_id:
            caption = title + (f":\n\n{shown}" if shown else "")
            sent = await bot.send_photo(user_id, photo_id, caption=caption, reply_markup=kb)
        else:
            sent = await bot.send_message(user_id, f"{title}:\n\n{shown}", reply_markup=kb)
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    db.execute("INSERT OR IGNORE INTO seen (user_id, gossip_id) VALUES (?, ?)", (user_id, gossip_id))
    db.execute(
        "INSERT OR REPLACE INTO deliveries (gossip_id, user_id, message_id) VALUES (?, ?, ?)",
        (gossip_id, user_id, sent.message_id),
    )
    db.commit()
    return True


async def deliver_to_pending(bot: Bot, gossip_id: int, text: str, photo_id: str | None, author_id: int,
                             section: str = "main") -> None:
    """Если кто-то ждёт сплетню в этом разделе, отдаём новую первому в очереди."""
    rows = db.execute(
        "SELECT user_id FROM pending WHERE user_id != ? AND section = ? ORDER BY created_at LIMIT 5",
        (author_id, section),
    ).fetchall()
    for row in rows:
        uid = row["user_id"]
        ok = False if is_banned(uid) else await send_gossip(bot, uid, gossip_id, text, photo_id)
        db.execute("DELETE FROM pending WHERE user_id = ?", (uid,))
        db.commit()
        if ok:
            return


async def remove_gossip(bot: Bot, gossip_id: int) -> None:
    """Скрывает сплетню и удаляет её сообщения у всех, кому она была отправлена."""
    db.execute("UPDATE gossips SET hidden = 1 WHERE id = ?", (gossip_id,))
    rows = db.execute(
        "SELECT user_id, message_id FROM deliveries WHERE gossip_id = ?", (gossip_id,)
    ).fetchall()
    db.execute("DELETE FROM deliveries WHERE gossip_id = ?", (gossip_id,))
    db.commit()
    for r in rows:
        try:
            await bot.delete_message(r["user_id"], r["message_id"])
        except (TelegramBadRequest, TelegramForbiddenError):
            pass  # сообщение старше 48 часов или пользователь заблокировал бота
        await asyncio.sleep(0.03)


async def send_content(bot: Bot, chat_id: int, row) -> None:
    """Показывает содержимое сплетни (для модерации админом)."""
    if row["photo_id"]:
        await bot.send_photo(chat_id, row["photo_id"], caption=row["text"] or None)
    else:
        await bot.send_message(chat_id, row["text"][:4000])


async def notify_admin_removed(bot: Bot, gossip_id: int) -> None:
    """Админ узнаёт, что сплетня удалена по голосам (само содержимое он уже видел при публикации)."""
    if not ADMIN_ID:
        return
    row = db.execute("SELECT author_id FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
    if not row:
        return
    agree, disagree = vote_counts(gossip_id)
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🗑 Сплетня №{gossip_id} удалена по голосам (👍 {agree} / 👎 {disagree})\n"
            f"Автор: {row['author_id']}\n/ban {row['author_id']}",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


async def notify_admin_new_gossip(bot: Bot, gossip_id: int, user, text: str, photo_id: str | None,
                                  section: str = "main", name: str | None = None) -> None:
    """Копия каждой новой сплетни/мысли админу (текст или фото) с номером автора для модерации."""
    if not ADMIN_ID or user.id == ADMIN_ID:
        return
    mark = "💭" if section == "vent" else "🤫"
    shown = text if section == "vent" else with_name(text, name)
    try:
        if photo_id:
            caption = f"{mark} №{gossip_id}" + (f"\n\n{shown}" if shown else "")
            sent = await bot.send_photo(ADMIN_ID, photo_id, caption=caption)
        else:
            sent = await bot.send_message(ADMIN_ID, f"{mark} №{gossip_id}\n\n{shown}")
        uname = f"@{user.username}" if user.username else "без юзернейма"
        await bot.send_message(
            ADMIN_ID,
            f"Автор: {user.id} ({uname})\n/del {gossip_id}   /ban {user.id}",
            reply_to_message_id=sent.message_id,
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


# --------------------------------------------------------------------------
# Оповещение «появились новые сплетни»
# --------------------------------------------------------------------------
BROADCAST_TEXTS = [
    "🤫 В Сплетнице пополнились сплетни! Скорее отправляй свою, чтобы узнать всё 😏",
    "😏 Тут накопились свежие сплетни. Напиши свою и получи чужую!",
    "🤭 В Сплетнице новые истории. Отправляй свою сплетню и читай чужие 😏",
]
BROADCAST_SUFFIX = "\n\nОтключить такие уведомления: /notify"

_broadcast_running = False
_bg_tasks: set = set()


def meta_get(key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    db.commit()


async def _send_with_retry(bot: Bot, uid: int, text: str) -> None:
    for _attempt in range(2):
        try:
            await bot.send_message(uid, text)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            # Человек заблокировал бота: больше не беспокоим
            db.execute("UPDATE users SET notify = 0 WHERE user_id = ?", (uid,))
            db.commit()
            return
        except TelegramBadRequest:
            return


async def _do_broadcast(bot: Bot) -> None:
    global _broadcast_running
    try:
        rows = db.execute("SELECT user_id FROM users WHERE banned = 0 AND notify = 1").fetchall()
        for row in rows:
            await _send_with_retry(bot, row["user_id"], random.choice(BROADCAST_TEXTS) + BROADCAST_SUFFIX)
            await asyncio.sleep(0.05)  # не превышаем лимит Telegram (около 30 сообщений в секунду)
    except Exception:
        logging.exception("Ошибка рассылки")
    finally:
        _broadcast_running = False


async def maybe_broadcast(bot: Bot) -> None:
    """Если набралось BROADCAST_EVERY новых сплетен (основной раздел), оповещаем всех пользователей."""
    global _broadcast_running
    if _broadcast_running:
        return
    last_id = int(meta_get("bc_id", "0"))
    fresh = db.execute(
        "SELECT COUNT(*) FROM gossips WHERE id > ? AND hidden = 0 AND section = 'main'", (last_id,)
    ).fetchone()[0]
    if fresh < BROADCAST_EVERY:
        return
    last_at = meta_get("bc_at")
    if last_at and BROADCAST_COOLDOWN_MIN > 0:
        if datetime.now() - datetime.fromisoformat(last_at) < timedelta(minutes=BROADCAST_COOLDOWN_MIN):
            return
    newest = db.execute("SELECT MAX(id) FROM gossips WHERE section = 'main'").fetchone()[0]
    meta_set("bc_id", str(newest))
    meta_set("bc_at", datetime.now().isoformat())
    _broadcast_running = True
    task = asyncio.create_task(_do_broadcast(bot))
    _bg_tasks.add(task)  # держим ссылку, чтобы задачу не убрала сборка мусора
    task.add_done_callback(_bg_tasks.discard)


def is_just_the_word_gossip(text: str) -> bool:
    """True, если сообщение состоит только из слова «сплетня» в любой форме
    (сплетня, сплетни, сплетню, сплетней, сплетница...), возможно со знаками и смайлами."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    words = cleaned.split()
    return bool(words) and all(w.startswith("сплетн") for w in words)


# --------------------------------------------------------------------------
# Анонимные диалоги («Высказаться»)
# --------------------------------------------------------------------------
def get_thread(thread_id: int):
    return db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()


def is_participant(thread, user_id: int) -> bool:
    return user_id in (thread["author_id"], thread["responder_id"])


def thread_other(thread, user_id: int) -> int:
    return thread["responder_id"] if user_id == thread["author_id"] else thread["author_id"]


def open_thread(gossip_id: int, author_id: int, responder_id: int) -> int:
    db.execute(
        "INSERT OR IGNORE INTO threads (gossip_id, author_id, responder_id, created_at) VALUES (?, ?, ?, ?)",
        (gossip_id, author_id, responder_id, datetime.now().isoformat()),
    )
    db.commit()
    return db.execute(
        "SELECT id FROM threads WHERE gossip_id = ? AND responder_id = ?", (gossip_id, responder_id)
    ).fetchone()["id"]


def activate_thread(user_id: int, thread_id: int) -> None:
    db.execute("UPDATE users SET section = 'vent', active_thread = ? WHERE user_id = ?", (thread_id, user_id))
    db.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
    db.commit()


def close_thread(thread_id: int) -> None:
    db.execute("UPDATE threads SET closed = 1 WHERE id = ?", (thread_id,))
    db.execute("UPDATE users SET active_thread = NULL WHERE active_thread = ?", (thread_id,))
    db.commit()


async def relay_message(message: Message, bot: Bot, text: str, thread_id: int) -> None:
    """Пересылает сообщение собеседнику по диалогу, не раскрывая отправителя."""
    uid = message.from_user.id
    th = get_thread(thread_id)
    if not th or th["closed"] or not is_participant(th, uid):
        db.execute("UPDATE users SET active_thread = NULL WHERE user_id = ?", (uid,))
        db.commit()
        await message.answer(
            "Этот диалог уже закрыт. Теперь ваши сообщения снова публикуются как новые мысли.\n"
            "К сплетням: /main"
        )
        return
    if len(text) > MAX_LEN:
        await message.answer(f"Слишком длинно, уложитесь в {MAX_LEN} символов.")
        return
    other = thread_other(th, uid)
    if is_banned(other):
        close_thread(thread_id)
        await message.answer("Собеседник недоступен, диалог закрыт.")
        return

    header = f"💬 Диалог №{thread_id}"
    if th["msg_count"] == 0 and uid == th["responder_id"]:
        g = db.execute("SELECT text FROM gossips WHERE id = ?", (th["gossip_id"],)).fetchone()
        snippet = (g["text"] or "") if g else ""
        snippet = snippet[:120] + ("…" if len(snippet) > 120 else "")
        header += f"\nОтвет на вашу мысль: «{snippet}»"
    try:
        await bot.send_message(other, f"{header}\n\n{text}", reply_markup=relay_keyboard(thread_id))
    except (TelegramForbiddenError, TelegramBadRequest):
        close_thread(thread_id)
        await message.answer("Не удалось доставить: собеседник недоступен. Диалог закрыт.")
        return
    db.execute("UPDATE threads SET msg_count = msg_count + 1 WHERE id = ?", (thread_id,))
    db.commit()


# --------------------------------------------------------------------------
# Хендлеры
# --------------------------------------------------------------------------
dp = Dispatcher()

MENU_COMMANDS = [
    BotCommand(command="main", description="Сплетни"),
    BotCommand(command="vent", description="Высказаться"),
    BotCommand(command="notify", description="Уведомления вкл/выкл"),
]


@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    await message.answer(
        "🤫 Привет! Я Сплетница.\n\n"
        "Напиши мне сплетню (текстом или фото), и я сразу дам тебе чужую. Сначала ты, потом тебе.\n"
        "Всё анонимно. В конце сплетни я дописываю случайное имя из списка, "
        "оно выбрано наугад и ничего не значит.\n"
        "Под чужой сплетней можно нажать «Согласен» или «Не согласен»: "
        "если трое не согласны, сплетня удаляется.\n\n"
        "💭 Хочешь просто выговориться? Команда /vent: анонимно поделись мыслью, "
        "а другие смогут ответить, поддержать или поговорить с тобой.\n\n"
        "Просто отправь свою сплетню следующим сообщением 👇",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("vent"))
async def cmd_vent(message: Message):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    set_section(message.from_user.id, "vent")
    await message.answer(
        "💭 Высказаться\n\n"
        "Напишите свою мысль (только текст), всё анонимно. Я сразу пришлю вам чью-то чужую мысль, "
        "а на вашу смогут ответить другие: поддержать или продолжить разговор прямо в боте.\n\n"
        "К сплетням: /main",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("main"))
async def cmd_main(message: Message):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    set_section(message.from_user.id, "main")
    await message.answer(
        "🤫 Вы в разделе сплетен. Пришлите сплетню текстом или фото.\nВысказаться: /vent",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /ban <user_id>")
        return
    uid = int(parts[1])
    db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, datetime.now().isoformat()))
    db.execute("UPDATE users SET banned = 1, active_thread = NULL WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM pending WHERE user_id = ?", (uid,))
    db.execute("UPDATE threads SET closed = 1 WHERE author_id = ? OR responder_id = ?", (uid, uid))
    db.commit()
    ids = [r["id"] for r in db.execute("SELECT id FROM gossips WHERE author_id = ? AND hidden = 0", (uid,))]
    for gid in ids:
        await remove_gossip(bot, gid)
    await message.answer(f"Пользователь {uid} забанен, его сплетни удалены ({len(ids)}), диалоги закрыты.")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /unban <user_id>")
        return
    db.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (int(parts[1]),))
    db.commit()
    await message.answer(f"Пользователь {parts[1]} разбанен.")


@dp.message(Command("del"))
async def cmd_del(message: Message, bot: Bot):
    """Удалить сплетню или мысль у всех по номеру: /del 15"""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /del <номер_сплетни>")
        return
    await remove_gossip(bot, int(parts[1]))
    await message.answer(f"№{parts[1]} удалена.")


@dp.message(Command("names"))
async def cmd_names(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    names = get_names()
    listing = "\n".join(f"{n} → от {name_genitive(n)}" for n in names) if names else "(список пуст, имена не дописываются)"
    await message.answer(
        f"Имена к сплетням:\n{listing}\n\n"
        "Добавить: /addname Имя (форму «от кого» подберу сама)\n"
        "Если склонила неверно: /addname Лёша=Лёши\n"
        "Убрать: /delname Имя"
    )


@dp.message(Command("addname"))
async def cmd_addname(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    name, _, gen = raw.partition("=")
    name, gen = name.strip(), gen.strip()
    if not name or len(name) > 30 or len(gen) > 30:
        await message.answer("Формат: /addname Имя или /addname Имя=Форма (до 30 символов)")
        return
    if any(n.casefold() == name.casefold() for n in get_names()):
        await message.answer(f"«{name}» уже есть в списке.")
        return
    db.execute("INSERT INTO names (name, gen) VALUES (?, ?)", (name, gen or None))
    db.commit()
    await message.answer(f"Добавлено: {name} → от {name_genitive(name)}. Сейчас в списке: {len(get_names())}.")


@dp.message(Command("delname"))
async def cmd_delname(message: Message):
    """Убрать имя из игры. Из уже опубликованных сплетен оно тоже пропадёт (при следующей отправке)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    name = parts[1].strip() if len(parts) > 1 else ""
    found = next((n for n in get_names() if n.casefold() == name.casefold()), None) if name else None
    if not found:
        await message.answer("Такого имени нет. Список: /names")
        return
    db.execute("DELETE FROM names WHERE name = ?", (found,))
    db.execute("UPDATE gossips SET name = NULL WHERE name = ?", (found,))
    db.commit()
    await message.answer(f"Имя «{found}» убрано из игры и из уже опубликованных сплетен.")


@dp.message(Command("last"))
async def cmd_last(message: Message, bot: Bot):
    """Последние сплетни и мысли (текст и фото): /last (10 штук) или /last 25 (не больше 30)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    n = max(1, min(n, 30))

    rows = db.execute(
        "SELECT id, author_id, text, photo_id, created_at, hidden, section, name "
        "FROM gossips ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        await message.answer("Сплетен пока нет.")
        return

    def meta_line(r) -> str:
        when = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
        agree, disagree = vote_counts(r["id"])
        flags = " [УДАЛЕНА]" if r["hidden"] else ""
        if agree or disagree:
            flags += f" [👍 {agree} / 👎 {disagree}]"
        if r["name"]:
            flags += f" [🎲 {r['name']}]"
        mark = "💭 " if r["section"] == "vent" else ""
        return f"{mark}№{r['id']} · {when} · автор {r['author_id']}{flags}"

    buf = ""

    async def flush():
        nonlocal buf
        if buf:
            await message.answer(buf)
            buf = ""

    for r in reversed(rows):  # старые сверху, самые новые внизу
        tail = f"/del {r['id']}   /ban {r['author_id']}"
        if r["photo_id"]:
            await flush()
            caption = f"{meta_line(r)}\n{(r['text'] or '')[:700]}\n{tail}"
            try:
                await bot.send_photo(message.from_user.id, r["photo_id"], caption=caption)
            except (TelegramForbiddenError, TelegramBadRequest):
                await message.answer(f"{meta_line(r)}\n(фото недоступно)\n{tail}")
            continue
        text = r["text"] if len(r["text"]) <= 1500 else r["text"][:1500] + "… (обрезано)"
        block = f"{meta_line(r)}\n{text}\n{tail}"
        if buf and len(buf) + len(block) + 2 > 3800:
            await flush()
        buf = f"{buf}\n\n{block}" if buf else block
    await flush()


@dp.message(Command("reported"))
async def cmd_reported(message: Message, bot: Bot):
    """Сплетни, у которых есть голоса «Не согласен»: /reported (10 штук) или /reported 20 (не больше 20)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    n = max(1, min(n, 20))

    rows = db.execute(
        """
        SELECT g.id, g.author_id, g.text, g.photo_id, g.hidden
        FROM gossips g
        WHERE EXISTS (SELECT 1 FROM votes v WHERE v.gossip_id = g.id AND v.vote = -1)
        ORDER BY g.id DESC LIMIT ?
        """,
        (n,),
    ).fetchall()
    if not rows:
        await message.answer("Сплетен с голосами «Не согласен» пока нет.")
        return

    for r in reversed(rows):  # старые сверху, самые новые внизу
        agree, disagree = vote_counts(r["id"])
        flag = " [УДАЛЕНА]" if r["hidden"] else ""
        try:
            await send_content(bot, message.from_user.id, r)
        except (TelegramForbiddenError, TelegramBadRequest):
            await message.answer("(не удалось показать содержимое)")
        await message.answer(
            f"№{r['id']}{flag} · 👍 {agree} / 👎 {disagree} · автор {r['author_id']}\n"
            f"/del {r['id']}   /ban {r['author_id']}"
        )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    gossips = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND section = 'main'").fetchone()[0]
    photos = db.execute(
        "SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND section = 'main' AND photo_id IS NOT NULL"
    ).fetchone()[0]
    thoughts = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND section = 'vent'").fetchone()[0]
    hidden = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 1 AND section != 'adult'").fetchone()[0]
    threads = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    open_threads = db.execute("SELECT COUNT(*) FROM threads WHERE closed = 0").fetchone()[0]
    await message.answer(
        f"Пользователей: {users}\n"
        f"Сплетен: {gossips} (из них фото: {photos})\n"
        f"Мыслей в «Высказаться»: {thoughts}\n"
        f"Диалогов: {threads} (открытых: {open_threads})\n"
        f"Удалено: {hidden}"
    )


@dp.message(Command("notify"))
async def cmd_notify(message: Message):
    """Включить или выключить уведомления «в Сплетнице новые сплетни»."""
    ensure_user(message.from_user)
    row = db.execute("SELECT notify FROM users WHERE user_id = ?", (message.from_user.id,)).fetchone()
    if row["notify"]:
        db.execute("UPDATE users SET notify = 0 WHERE user_id = ?", (message.from_user.id,))
        db.commit()
        await message.answer("🔕 Уведомления о новых сплетнях выключены. Включить: /notify")
    else:
        db.execute("UPDATE users SET notify = 1 WHERE user_id = ?", (message.from_user.id,))
        db.commit()
        await message.answer("🔔 Уведомления о новых сплетнях включены.")


@dp.message(Command("version"))
async def cmd_version(message: Message):
    """Показывает, какая версия бота реально запущена на хостинге."""
    await message.answer(f"Версия бота: {BOT_VERSION}")


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


async def process_gossip(message: Message, bot: Bot, text: str, photo_id: str | None = None) -> None:
    """Общая часть для сплетни и мысли: сохранить, отдать ждущему, выдать автору чужую."""
    user_id = message.from_user.id
    section = get_section(user_id)
    if posts_today(user_id) >= DAILY_LIMIT:
        await message.answer("На сегодня лимит исчерпан, возвращайся завтра 🌙")
        return

    # Игра: к каждой сплетне в основном разделе бот подставляет случайное имя
    name = random_name() if section == "main" else None

    cur = db.execute(
        "INSERT INTO gossips (author_id, text, photo_id, created_at, section, name) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, text, photo_id, datetime.now().isoformat(), section, name),
    )
    gossip_id = cur.lastrowid
    db.commit()

    # Копия админу
    await notify_admin_new_gossip(bot, gossip_id, message.from_user, text, photo_id, section, name)

    # Новую сплетню сразу отдаём тому, кто ждёт
    await deliver_to_pending(bot, gossip_id, text, photo_id, user_id, section)

    # Взамен выдаём автору чужую
    row = pick_gossip_for(user_id, section)
    if row:
        await send_gossip(bot, user_id, row["id"], row["text"], row["photo_id"])
    else:
        db.execute(
            "INSERT OR REPLACE INTO pending (user_id, created_at, section) VALUES (?, ?, ?)",
            (user_id, datetime.now().isoformat(), section),
        )
        db.commit()
        what = "мысль" if section == "vent" else "сплетня"
        await message.answer(
            f"Твоя {what} принята ✅ Пока чужих нет, "
            "но как только появится новая, я пришлю её тебе."
        )

    await maybe_broadcast(bot)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text_gossip(message: Message, bot: Bot):
    ensure_user(message.from_user)
    uid = message.from_user.id
    if is_banned(uid):
        return

    text = message.text.strip()
    section, active_thread = get_state(uid)

    # В «Высказаться» с открытым диалогом сообщение уходит собеседнику
    if section == "vent" and active_thread:
        await relay_message(message, bot, text, active_thread)
        return

    if section == "main" and is_just_the_word_gossip(text):
        await message.answer("Напиши саму сплетню, а не слово «сплетня» 🙂")
        return
    min_len = MIN_LEN_VENT if section == "vent" else MIN_LEN
    if len(text) < min_len:
        await message.answer("Слишком коротко, расскажи подробнее 🙂")
        return
    if len(text) > MAX_LEN:
        await message.answer(f"Слишком длинно, уложись в {MAX_LEN} символов.")
        return
    await process_gossip(message, bot, text)


@dp.message(F.photo)
async def on_photo_gossip(message: Message, bot: Bot):
    """Фото (с подписью или без) равносильно сплетне. В «Высказаться» фото нельзя."""
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    if get_section(message.from_user.id) == "vent":
        await message.answer("В «Высказаться» только текст. К сплетням: /main")
        return
    caption = (message.caption or "").strip()
    if len(caption) > MAX_CAPTION:
        await message.answer(f"Подпись слишком длинная, уложись в {MAX_CAPTION} символов.")
        return
    photo_id = message.photo[-1].file_id  # самый большой размер
    await process_gossip(message, bot, caption, photo_id)


@dp.callback_query(F.data.startswith("v:"))
async def on_vote(cb: CallbackQuery, bot: Bot):
    try:
        _, gid_s, val_s = cb.data.split(":")
        gossip_id, vote = int(gid_s), (1 if val_s == "1" else -1)
    except ValueError:
        await cb.answer()
        return

    user_id = cb.from_user.id
    if is_banned(user_id):
        await cb.answer()
        return
    row = db.execute("SELECT hidden, author_id, section FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
    if not row or row["hidden"]:
        await cb.answer("Эта сплетня уже удалена.")
        return
    if row["section"] == "vent":
        await cb.answer()
        return
    if row["author_id"] == user_id:
        await cb.answer("За свою сплетню голосовать нельзя.")
        return

    prev = db.execute(
        "SELECT vote FROM votes WHERE gossip_id = ? AND user_id = ?", (gossip_id, user_id)
    ).fetchone()
    if prev and prev["vote"] == vote:
        await cb.answer("Вы уже так проголосовали.")
        return
    db.execute(
        "INSERT OR REPLACE INTO votes (gossip_id, user_id, vote) VALUES (?, ?, ?)",
        (gossip_id, user_id, vote),
    )
    db.commit()

    _, disagree = vote_counts(gossip_id)
    if disagree >= DISLIKE_LIMIT:
        await cb.answer("Голос учтён. Сплетня удалена.")
        await notify_admin_removed(bot, gossip_id)
        await remove_gossip(bot, gossip_id)
        return

    await cb.answer("Голос учтён")
    try:
        await cb.message.edit_reply_markup(reply_markup=vote_keyboard(gossip_id))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("t:"))
async def on_thought_reply(cb: CallbackQuery):
    """Кнопка «Ответить» под чужой мыслью: открываем анонимный диалог с автором."""
    try:
        gossip_id = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer()
        return
    uid = cb.from_user.id
    ensure_user(cb.from_user)
    if is_banned(uid):
        await cb.answer()
        return
    row = db.execute("SELECT author_id, hidden, section FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
    if not row or row["hidden"] or row["section"] != "vent":
        await cb.answer("Эта мысль уже удалена.")
        return
    if row["author_id"] == uid:
        await cb.answer("Это ваша собственная мысль.")
        return
    if is_banned(row["author_id"]):
        await cb.answer("Автор недоступен.")
        return
    tid = open_thread(gossip_id, row["author_id"], uid)
    th = get_thread(tid)
    if th["closed"]:
        await cb.answer("Этот диалог закрыт.")
        return
    activate_thread(uid, tid)
    await cb.answer()
    await cb.message.answer(
        f"💬 Диалог №{tid} открыт. Пишите, ваши сообщения уйдут автору анонимно, вы тоже останетесь анонимным.\n"
        "Выйти из диалога: /vent"
    )


@dp.callback_query(F.data.startswith("r:"))
async def on_relay_reply(cb: CallbackQuery):
    """Кнопка «Ответить» под сообщением собеседника: возвращаемся в диалог."""
    try:
        tid = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer()
        return
    uid = cb.from_user.id
    th = get_thread(tid)
    if is_banned(uid) or not th or not is_participant(th, uid):
        await cb.answer()
        return
    if th["closed"]:
        await cb.answer("Диалог закрыт.")
        return
    activate_thread(uid, tid)
    await cb.answer()
    await cb.message.answer(f"↩️ Вы в диалоге №{tid}. Пишите, сообщения уйдут анонимно.\nВыйти из диалога: /vent")


@dp.callback_query(F.data.startswith("x:"))
async def on_close_thread(cb: CallbackQuery, bot: Bot):
    """Кнопка «Закрыть»: любой участник может закрыть диалог."""
    try:
        tid = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer()
        return
    uid = cb.from_user.id
    th = get_thread(tid)
    if not th or not is_participant(th, uid):
        await cb.answer()
        return
    close_thread(tid)
    await cb.answer("Диалог закрыт")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    try:
        await bot.send_message(thread_other(th, uid), f"🚫 Собеседник закрыл диалог №{tid}.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@dp.callback_query(F.data.startswith("rep:") | F.data.startswith("age:"))
async def on_old_buttons(cb: CallbackQuery):
    """Кнопки из старых версий бота («Пожаловаться», подтверждение 18+): их больше нет."""
    await cb.answer("Эта кнопка устарела.")


@dp.message()
async def fallback(message: Message):
    if is_banned(message.from_user.id):
        return
    await message.answer("Присылай текст или фото 🙂")


# --------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Добавьте токен в переменные окружения.")
    logging.basicConfig(level=logging.INFO)
    logging.info("Запуск, версия бота: %s", BOT_VERSION)
    bot = Bot(BOT_TOKEN)
    try:
        await bot.set_my_commands(MENU_COMMANDS)
    except Exception:
        logging.exception("Не удалось задать меню команд")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
