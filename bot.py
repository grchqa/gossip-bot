"""
Бот "Сплетница" для Telegram (aiogram 3).

Правило: пришли свою сплетню (текстом или фото) и получи чужую. Автор другим пользователям не виден.
Под чужой сплетней кнопки «Согласен» / «Не согласен» (видно, сколько людей проголосовало).
Если «Не согласны» набирается 3 и больше, сплетня автоматически удаляется у всех.

Команды админа (ADMIN_ID):
  (автоматически) каждая новая сплетня приходит админу сразу: текст или фото, номер автора, /del и /ban
  /last [N]      - последние N сплетен (по умолчанию 10, максимум 30)
  /reported [N]  - сплетни, у которых есть голоса «Не согласен» (последние N, по умолчанию 10)
  /del <номер>   - удалить сплетню у всех
  /ban <id>      - забанить автора и удалить его сплетни
  /unban <id>    - разбанить
  /stats         - статистика
Админ видит все сплетни: копия каждой приходит ему сразу, плюс уведомление, когда сплетня удалена по голосам.

Команды для всех: /notify (вкл/выкл уведомления «появились новые сплетни»).

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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "gossip.db")

MIN_LEN = 5                  # минимальная длина текстовой сплетни
MAX_LEN = 4000               # максимальная длина текста (Телеграм не пропускает больше 4096)
MAX_CAPTION = 1000           # максимальная длина подписи к фото (у Телеграма лимит 1024)
DAILY_LIMIT = 10000          # лимит сплетен в сутки на человека (10000 = практически без ограничений)
DISLIKE_LIMIT = 3            # сколько «Не согласен» нужно, чтобы сплетня удалилась
BROADCAST_EVERY = 5          # после скольких новых сплетен оповещать всех пользователей
BROADCAST_COOLDOWN_MIN = 30  # не чаще, чем раз в N минут (0 = без паузы, но это быстро надоест людям)

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
        photo_id TEXT,
        created_at TEXT,
        hidden INTEGER DEFAULT 0
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
        created_at TEXT
    );
    """
)
# Если база осталась от старой версии бота, добавляем новые столбцы
for _table, _col, _ddl in [
    ("users", "notify", "INTEGER DEFAULT 1"),
    ("gossips", "photo_id", "TEXT"),
]:
    _cols = {r["name"] for r in db.execute(f"PRAGMA table_info({_table})")}
    if _col not in _cols:
        db.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")
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
    """Случайная чужая, ещё не виденная и не удалённая сплетня."""
    return db.execute(
        """
        SELECT id, text, photo_id FROM gossips
        WHERE hidden = 0
          AND author_id != ?
          AND id NOT IN (SELECT gossip_id FROM seen WHERE user_id = ?)
        ORDER BY RANDOM() LIMIT 1
        """,
        (user_id, user_id),
    ).fetchone()


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


# --------------------------------------------------------------------------
# Отправка и удаление сплетен
# --------------------------------------------------------------------------
async def send_gossip(bot: Bot, user_id: int, gossip_id: int, text: str, photo_id: str | None = None) -> bool:
    """Отправляет сплетню (текст или фото) с кнопками голосования. False, если пользователь заблокировал бота."""
    kb = vote_keyboard(gossip_id)
    try:
        if photo_id:
            caption = "🤫 Чужая сплетня" + (f":\n\n{text}" if text else "")
            sent = await bot.send_photo(user_id, photo_id, caption=caption, reply_markup=kb)
        else:
            sent = await bot.send_message(user_id, f"🤫 Чужая сплетня:\n\n{text}", reply_markup=kb)
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    db.execute("INSERT OR IGNORE INTO seen (user_id, gossip_id) VALUES (?, ?)", (user_id, gossip_id))
    db.execute(
        "INSERT OR REPLACE INTO deliveries (gossip_id, user_id, message_id) VALUES (?, ?, ?)",
        (gossip_id, user_id, sent.message_id),
    )
    db.commit()
    return True


async def deliver_to_pending(bot: Bot, gossip_id: int, text: str, photo_id: str | None, author_id: int) -> None:
    """Если кто-то ждёт сплетню, отдаём новую первому в очереди."""
    rows = db.execute(
        "SELECT user_id FROM pending WHERE user_id != ? ORDER BY created_at LIMIT 5", (author_id,)
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


async def notify_admin_new_gossip(bot: Bot, gossip_id: int, user, text: str, photo_id: str | None) -> None:
    """Копия каждой новой сплетни админу (текст или фото) с номером автора для модерации."""
    if not ADMIN_ID or user.id == ADMIN_ID:
        return
    try:
        if photo_id:
            caption = f"🤫 №{gossip_id}" + (f"\n\n{text}" if text else "")
            sent = await bot.send_photo(ADMIN_ID, photo_id, caption=caption)
        else:
            sent = await bot.send_message(ADMIN_ID, f"🤫 №{gossip_id}\n\n{text}")
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
    (сплетня, сплетни, сплетню, сплетней, сплетница...), возможно со знаками и смайлами."""
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
        "Напиши мне свою сплетню (текстом или фото), и я сразу дам тебе чужую. "
        "Сначала ты, потом тебе.\n\n"
        "Всё анонимно: другие пользователи не узнают, кто автор.\n"
        "Под чужой сплетней можно нажать «Согласен» или «Не согласен». "
        "Если трое не согласны, сплетня удаляется.\n\n"
        "Просто отправь свою сплетню следующим сообщением 👇"
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
    db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM pending WHERE user_id = ?", (uid,))
    db.commit()
    ids = [r["id"] for r in db.execute("SELECT id FROM gossips WHERE author_id = ? AND hidden = 0", (uid,))]
    for gid in ids:
        await remove_gossip(bot, gid)
    await message.answer(f"Пользователь {uid} забанен, его сплетни удалены ({len(ids)}).")


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
    """Удалить сплетню у всех по номеру: /del 15"""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /del <номер_сплетни>")
        return
    await remove_gossip(bot, int(parts[1]))
    await message.answer(f"Сплетня №{parts[1]} удалена.")


@dp.message(Command("last"))
async def cmd_last(message: Message, bot: Bot):
    """Последние сплетни (текст и фото): /last (10 штук) или /last 25 (не больше 30)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    n = max(1, min(n, 30))

    rows = db.execute(
        "SELECT id, author_id, text, photo_id, created_at, hidden FROM gossips ORDER BY id DESC LIMIT ?",
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
        return f"№{r['id']} · {when} · автор {r['author_id']}{flags}"

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
    gossips = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 0").fetchone()[0]
    photos = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND photo_id IS NOT NULL").fetchone()[0]
    hidden = db.execute("SELECT COUNT(*) FROM gossips WHERE hidden = 1").fetchone()[0]
    await message.answer(
        f"Пользователей: {users}\nСплетен: {gossips} (из них фото: {photos})\nУдалено: {hidden}"
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


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


async def process_gossip(message: Message, bot: Bot, text: str, photo_id: str | None = None) -> None:
    """Общая часть для текстовой и фото-сплетни: сохранить, отдать ждущему, выдать автору чужую."""
    user_id = message.from_user.id
    if posts_today(user_id) >= DAILY_LIMIT:
        await message.answer("На сегодня лимит сплетен исчерпан, возвращайся завтра 🌙")
        return

    cur = db.execute(
        "INSERT INTO gossips (author_id, text, photo_id, created_at) VALUES (?, ?, ?, ?)",
        (user_id, text, photo_id, datetime.now().isoformat()),
    )
    gossip_id = cur.lastrowid
    db.commit()

    # Копия сплетни админу
    await notify_admin_new_gossip(bot, gossip_id, message.from_user, text, photo_id)

    # Новую сплетню сразу отдаём тому, кто ждёт
    await deliver_to_pending(bot, gossip_id, text, photo_id, user_id)

    # Взамен выдаём автору чужую
    row = pick_gossip_for(user_id)
    if row:
        await send_gossip(bot, user_id, row["id"], row["text"], row["photo_id"])
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


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text_gossip(message: Message, bot: Bot):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
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
    await process_gossip(message, bot, text)


@dp.message(F.photo)
async def on_photo_gossip(message: Message, bot: Bot):
    """Фото (с подписью или без) равносильно сплетне."""
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
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
    row = db.execute("SELECT hidden, author_id FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
    if not row or row["hidden"]:
        await cb.answer("Эта сплетня уже удалена.")
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
        await notify_admin_removed(bot, gossip_id)  # пока содержимое ещё в базе
        await remove_gossip(bot, gossip_id)
        return

    await cb.answer("Голос учтён")
    try:
        await cb.message.edit_reply_markup(reply_markup=vote_keyboard(gossip_id))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("rep:"))
async def on_old_report_button(cb: CallbackQuery):
    """Кнопка «Пожаловаться» из старых сообщений: её больше нет."""
    await cb.answer("Эта кнопка устарела. Теперь есть «Согласен» / «Не согласен».")


@dp.message()
async def fallback(message: Message):
    if is_banned(message.from_user.id):
        return
    await message.answer("Присылай сплетню текстом или фото 🙂")


# --------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Добавьте токен в переменные окружения.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
