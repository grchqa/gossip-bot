"""
Бот "Сплетница" для Telegram (aiogram 3).

Правило: напиши свою сплетню, получи чужую. Автор другим пользователям не виден.

Переменные окружения:
  BOT_TOKEN  - токен от @BotFather (обязательно)
  ADMIN_ID   - ваш Telegram ID (чтобы получать жалобы и банить)
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "gossip.db")

MIN_LEN = 5          # минимальная длина сплетни
MAX_LEN = 4000       # максимальная длина (Телеграм не пропускает больше 4096 в одном сообщении)
DAILY_LIMIT = 10000  # лимит сплетен в сутки на человека (10000 = практически без ограничений)
BROADCAST_EVERY = 10        # после скольких новых сплетен оповещать всех пользователей
BROADCAST_COOLDOWN_MIN = 30  # не чаще, чем раз в N минут (0 = без паузы, но это быстро надоест людям)
HIDE_AFTER = 3       # после скольки жалоб сплетня скрывается

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
        notify INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS gossips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id INTEGER,
        text TEXT,
        created_at TEXT,
        hidden INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS seen (
        user_id INTEGER,
        gossip_id INTEGER,
        PRIMARY KEY (user_id, gossip_id)
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
        created_at TEXT
    );
    """
)
# Если база осталась от старой версии бота, добавляем новый столбец
_cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
if "notify" not in _cols:
    db.execute("ALTER TABLE users ADD COLUMN notify INTEGER DEFAULT 1")
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


def posts_today(user_id: int) -> int:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return db.execute(
        "SELECT COUNT(*) FROM gossips WHERE author_id = ? AND created_at >= ?",
        (user_id, start),
    ).fetchone()[0]


def pick_gossip_for(user_id: int):
    """Случайная чужая, ещё не виденная и не скрытая сплетня."""
    return db.execute(
        """
        SELECT id, text FROM gossips
        WHERE hidden = 0
          AND author_id != ?
          AND id NOT IN (SELECT gossip_id FROM seen WHERE user_id = ?)
        ORDER BY RANDOM() LIMIT 1
        """,
        (user_id, user_id),
    ).fetchone()


def report_keyboard(gossip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"rep:{gossip_id}")]]
    )


async def send_gossip(bot: Bot, user_id: int, gossip_id: int, text: str) -> bool:
    """Отправляет сплетню как обычный текст (без разметки). False, если пользователь заблокировал бота."""
    try:
        await bot.send_message(
            user_id, f"🤫 Чужая сплетня:\n\n{text}", reply_markup=report_keyboard(gossip_id)
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    db.execute("INSERT OR IGNORE INTO seen (user_id, gossip_id) VALUES (?, ?)", (user_id, gossip_id))
    db.commit()
    return True


async def deliver_to_pending(bot: Bot, gossip_id: int, text: str, author_id: int) -> None:
    """Если кто-то ждёт сплетню, отдаём новую первому в очереди."""
    rows = db.execute(
        "SELECT user_id FROM pending WHERE user_id != ? ORDER BY created_at LIMIT 5", (author_id,)
    ).fetchall()
    for row in rows:
        uid = row["user_id"]
        ok = False if is_banned(uid) else await send_gossip(bot, uid, gossip_id, text)
        db.execute("DELETE FROM pending WHERE user_id = ?", (uid,))
        db.commit()
        if ok:
            return


async def notify_admin_new_gossip(bot: Bot, gossip_id: int, user, text: str) -> None:
    """Каждая новая сплетня сразу приходит админу (без команд), с номером автора для модерации."""
    if not ADMIN_ID or user.id == ADMIN_ID:
        return
    try:
        sent = await bot.send_message(ADMIN_ID, f"🤫 №{gossip_id}\n\n{text}")
        uname = f"@{user.username}" if user.username else "без юзернейма"
        await bot.send_message(
            ADMIN_ID,
            f"Автор: {user.id} ({uname})\n/del {gossip_id}   /ban {user.id}",
            reply_to_message_id=sent.message_id,
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


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
    for attempt in range(2):
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
    """Если набралось BROADCAST_EVERY новых сплетен, оповещаем всех пользователей."""
    global _broadcast_running
    if _broadcast_running:
        return
    last_id = int(meta_get("bc_id", "0"))
    fresh = db.execute(
        "SELECT COUNT(*) FROM gossips WHERE id > ? AND hidden = 0", (last_id,)
    ).fetchone()[0]
    if fresh < BROADCAST_EVERY:
        return
    last_at = meta_get("bc_at")
    if last_at and BROADCAST_COOLDOWN_MIN > 0:
        if datetime.now() - datetime.fromisoformat(last_at) < timedelta(minutes=BROADCAST_COOLDOWN_MIN):
            return
    newest = db.execute("SELECT MAX(id) FROM gossips").fetchone()[0]
    meta_set("bc_id", str(newest))
    meta_set("bc_at", datetime.now().isoformat())
    _broadcast_running = True
    task = asyncio.create_task(_do_broadcast(bot))
    _bg_tasks.add(task)  # держим ссылку, чтобы задачу не убрала сборка мусора
    task.add_done_callback(_bg_tasks.discard)


def is_just_the_word_gossip(text: str) -> bool:
    """True, если сообщение состоит только из слова «сплетня» в любой форме
    (сплетня, сплетни, сплетню, сплетней, сплетница...), возможно с знаками и смайлами."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    words = cleaned.split()
    return bool(words) and all(w.startswith("сплетн") for w in words)


# --------------------------------------------------------------------------
# Хендлеры
# --------------------------------------------------------------------------
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    await message.answer(
        "🤫 Привет! Я Сплетница.\n\n"
        "Как это работает: напиши мне свою сплетню, и я сразу дам тебе чужую. "
        "Сначала ты, потом тебе.\n\n"
        "Всё анонимно: другие пользователи не узнают, кто автор.\n"
        "Если сплетня тебе не понравилась, под ней есть кнопка «Пожаловаться».\n\n"
        "Просто отправь свою сплетню следующим сообщением 👇"
    )


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /ban <user_id>")
        return
    uid = int(parts[1])
    db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, datetime.now().isoformat()))
    db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (uid,))
    db.execute("UPDATE gossips SET hidden = 1 WHERE author_id = ?", (uid,))
    db.execute("DELETE FROM pending WHERE user_id = ?", (uid,))
    db.commit()
    await message.answer(f"Пользователь {uid} забанен, его сплетни скрыты.")


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
async def cmd_del(message: Message):
    """Скрыть сплетню по номеру: /del 15"""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /del <номер_сплетни>")
        return
    db.execute("UPDATE gossips SET hidden = 1 WHERE id = ?", (int(parts[1]),))
    db.commit()
    await message.answer(f"Сплетня №{parts[1]} скрыта.")


@dp.message(Command("last"))
async def cmd_last(message: Message):
    """Показать последние сплетни: /last (10 штук) или /last 25 (не больше 30)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    n = max(1, min(n, 30))

    rows = db.execute(
        """
        SELECT g.id, g.author_id, g.text, g.created_at, g.hidden,
               (SELECT COUNT(*) FROM reports r WHERE r.gossip_id = g.id) AS rep
        FROM gossips g ORDER BY g.id DESC LIMIT ?
        """,
        (n,),
    ).fetchall()
    if not rows:
        await message.answer("Сплетен пока нет.")
        return

    blocks = []
    for r in reversed(rows):  # старые сверху, самые новые внизу
        when = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
        flags = ""
        if r["hidden"]:
            flags += " [СКРЫТА]"
        if r["rep"]:
            flags += f" [жалоб: {r['rep']}]"
        text = r["text"] if len(r["text"]) <= 1500 else r["text"][:1500] + "… (обрезано)"
        blocks.append(
            f"№{r['id']} · {when} · автор {r['author_id']}{flags}\n{text}\n"
            f"/del {r['id']}   /ban {r['author_id']}"
        )

    # Отправляем порциями, чтобы не упереться в лимит Телеграма
    buf = ""
    for block in blocks:
        if buf and len(buf) + len(block) + 2 > 3800:
            await message.answer(buf)
            buf = ""
        buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        await message.answer(buf)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    gossips = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 0").fetchone()[0]
    hidden = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 1").fetchone()[0]
    await message.answer(f"Пользователей: {users}\nСплетен: {gossips}\nСкрыто: {hidden}")


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


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(F.text & ~F.text.startswith("/"))
async def on_gossip(message: Message, bot: Bot):
    user_id = message.from_user.id
    ensure_user(message.from_user)
    if is_banned(user_id):
        return

    text = message.text.strip()
    if is_just_the_word_gossip(text):
        await message.answer("Напиши саму сплетню, а не слово «сплетня» 🙂")
        return
    if len(text) < MIN_LEN:
        await message.answer("Слишком коротко, расскажи подробнее 🙂")
        return
    if len(text) > MAX_LEN:
        await message.answer(f"Слишком длинно, уложись в {MAX_LEN} символов.")
        return
    if posts_today(user_id) >= DAILY_LIMIT:
        await message.answer("На сегодня лимит сплетен исчерпан, возвращайся завтра 🌙")
        return

    cur = db.execute(
        "INSERT INTO gossips (author_id, text, created_at) VALUES (?, ?, ?)",
        (user_id, text, datetime.now().isoformat()),
    )
    gossip_id = cur.lastrowid
    db.commit()

    # Копия сплетни админу
    await notify_admin_new_gossip(bot, gossip_id, message.from_user, text)

    # Новую сплетню сразу отдаём тому, кто ждёт
    await deliver_to_pending(bot, gossip_id, text, user_id)

    # Взамен выдаём автору чужую
    row = pick_gossip_for(user_id)
    if row:
        await send_gossip(bot, user_id, row["id"], row["text"])
    else:
        db.execute(
            "INSERT OR IGNORE INTO pending (user_id, created_at) VALUES (?, ?)",
            (user_id, datetime.now().isoformat()),
        )
        db.commit()
        await message.answer(
            "Твоя сплетня принята ✅ Пока чужих сплетен нет, "
            "но как только появится новая, я пришлю её тебе."
        )

    await maybe_broadcast(bot)


@dp.callback_query(F.data.startswith("rep:"))
async def on_report(cb: CallbackQuery, bot: Bot):
    gossip_id = int(cb.data.split(":")[1])
    cur = db.execute(
        "INSERT OR IGNORE INTO reports (gossip_id, reporter_id) VALUES (?, ?)",
        (gossip_id, cb.from_user.id),
    )
    db.commit()
    if cur.rowcount == 0:
        await cb.answer("Вы уже отправляли жалобу на эту сплетню.")
        return

    count = db.execute("SELECT COUNT(*) FROM reports WHERE gossip_id = ?", (gossip_id,)).fetchone()[0]
    if count >= HIDE_AFTER:
        db.execute("UPDATE gossips SET hidden = 1 WHERE id = ?", (gossip_id,))
        db.commit()

    await cb.answer("Жалоба отправлена, спасибо.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    if ADMIN_ID:
        row = db.execute("SELECT author_id, text FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
        if row:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🚩 Жалоба на сплетню №{gossip_id} (жалоб: {count})\n"
                    f"Автор: {row['author_id']}\n\n{row['text']}\n\n"
                    f"Забанить автора: /ban {row['author_id']}\n"
                    f"Скрыть сплетню: /del {gossip_id}",
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                pass


@dp.message()
async def fallback(message: Message):
    if is_banned(message.from_user.id):
        return
    await message.answer("Присылай сплетню текстом 🙂")


# --------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Добавьте токен в переменные окружения.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
