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
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "gossip.db")

MIN_LEN = 5          # минимальная длина сплетни
MAX_LEN = 4000       # максимальная длина (Телеграм не пропускает больше 4096 в одном сообщении)
DAILY_LIMIT = 10     # сколько сплетен в сутки можно отправить (защита от спама)
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
        created_at TEXT
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
    CREATE TABLE IF NOT EXISTS pending (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT
    );
    """
)
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


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    gossips = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 0").fetchone()[0]
    hidden = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 1").fetchone()[0]
    await message.answer(f"Пользователей: {users}\nСплетен: {gossips}\nСкрыто: {hidden}")


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
