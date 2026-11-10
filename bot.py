"""
Бот "Сплетница" для Telegram (aiogram 3).

Как работает:
  Человек присылает свою сплетню (текстом или фото) и сразу получает чужую от анонимного автора.
  Автор другим пользователям не виден.

Два раздела (переключаются кнопками внизу):
  💬 Обычные сплетни - открыт для всех.
  🔥 Грязные сплетни - пробный раздел, пока бесплатный, вход по одноразовому коду.
  Пулы сплетен полностью раздельные: обычная меняется на обычную, грязная на грязную.

Круговорот:
  Одна и та же сплетня может попасться разным людям, в том числе тем, кто уже видел её у другого.
  Одному и тому же человеку повторно она не приходит. Чаще показываются те сплетни,
  которые показывали реже всего.

Голосование:
  Под чужой сплетней кнопки «Согласен» / «Не согласен». Голосовать можно один раз, голос не меняется.
  Если «Не согласен» набирается 3 и больше, сплетня удаляется у всех.

Сплетня дня:
  Каждый день в 13:00 по Москве всем подписчикам приходит одна случайная сплетня из обычного раздела.
  Если новой сплетни нет, приходит текст «Увы, пока нет новой сплетни. Самое время загрузить свою!».
  В грязном разделе ежедневной рассылки нет, но есть кнопка «🎲 Случайная грязная сплетня»
  (команда /random): раз в час выдаёт случайную сплетню из грязного раздела.

Команды для всех:
  /start   - начало и кнопки разделов
  /daily   - включить/выключить ежедневную сплетню дня
  /id      - узнать свой Telegram ID

Команды админа (ADMIN_ID):
  (автоматически) каждая новая сплетня приходит админу: текст или фото, номер автора, /del и /ban
  /code [N]      - создать N одноразовых кодов для грязного раздела (по умолчанию 1, максимум 20)
  /codes         - неиспользованные коды и кто использовал последние
  /revoke <id>   - забрать у человека доступ к грязному разделу
  /broadcast <текст> - отправить сообщение всем пользователям
  /last [N]      - последние N сплетен (по умолчанию 10, максимум 30)
  /reported [N]  - сплетни с голосами «Не согласен» (по умолчанию 10, максимум 20)
  /del <номер>   - удалить сплетню у всех
  /ban <id>      - забанить автора и удалить его сплетни
  /unban <id>    - разбанить
  /stats         - статистика

Переменные окружения:
  BOT_TOKEN  - токен от @BotFather (обязательно)
  ADMIN_ID   - ваш Telegram ID (для модерации)
  DB_PATH    - путь к файлу базы (по умолчанию gossip.db)
"""

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "gossip.db")

MIN_LEN = 5            # минимальная длина текстовой сплетни
MAX_LEN = 4000         # максимальная длина текста (Телеграм не пропускает больше 4096)
MAX_CAPTION = 1000     # максимальная длина подписи к фото (у Телеграма лимит 1024)
DISLIKE_LIMIT = 3      # сколько «Не согласен» нужно, чтобы сплетня удалилась
DAILY_HOUR = 13        # во сколько по Москве приходит сплетня дня
MSK = timezone(timedelta(hours=3), "MSK")  # у Москвы нет перехода на летнее время

NORMAL = "normal"
DIRTY = "dirty"
BTN_NORMAL = "💬 Обычные сплетни"
BTN_DIRTY = "🔥 Грязные сплетни"
BTN_RANDOM = "🎲 Случайная грязная сплетня"
TITLES = {NORMAL: BTN_NORMAL, DIRTY: BTN_DIRTY}
RANDOM_COOLDOWN_MIN = 60  # как часто можно жать «Случайная грязная сплетня» (у админа без ограничений)

NO_NEW_TEXT = "😔 Увы, пока нет новой сплетни. Самое время загрузить свою!"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих символов (0/O, 1/I)
CODE_LEN = 10
CODE_MAX_FAILS = 5     # сколько неверных кодов подряд можно ввести
CODE_LOCK_SEC = 600    # на сколько секунд блокируется ввод кодов после этого

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
        mode TEXT DEFAULT 'normal',
        dirty INTEGER DEFAULT 0,
        await_code INTEGER DEFAULT 0,
        daily INTEGER DEFAULT 1,
        last_random TEXT
    );
    CREATE TABLE IF NOT EXISTS gossips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        author_id INTEGER,
        text TEXT,
        photo_id TEXT,
        created_at TEXT,
        hidden INTEGER DEFAULT 0,
        section TEXT DEFAULT 'normal',
        shown INTEGER DEFAULT 0
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
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS waiting (
        user_id INTEGER,
        section TEXT,
        created_at TEXT,
        PRIMARY KEY (user_id, section)
    );
    CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,
        created_at TEXT,
        used_by INTEGER,
        used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS drops (
        gossip_id INTEGER PRIMARY KEY,
        dropped_at TEXT
    );
    """
)
# Если база осталась от старой версии бота, добавляем новые столбцы
for _table, _col, _ddl in [
    ("users", "notify", "INTEGER DEFAULT 1"),
    ("users", "mode", "TEXT DEFAULT 'normal'"),
    ("users", "dirty", "INTEGER DEFAULT 0"),
    ("users", "await_code", "INTEGER DEFAULT 0"),
    ("users", "daily", "INTEGER DEFAULT 1"),
    ("users", "last_random", "TEXT"),
    ("gossips", "photo_id", "TEXT"),
    ("gossips", "section", "TEXT DEFAULT 'normal'"),
    ("gossips", "shown", "INTEGER DEFAULT 0"),
]:
    _cols = {r["name"] for r in db.execute(f"PRAGMA table_info({_table})")}
    if _col not in _cols:
        db.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")
db.commit()


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


def ensure_user(user) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
        (user.id, user.username, datetime.now().isoformat()),
    )
    db.commit()


def is_banned(user_id: int) -> bool:
    row = db.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def has_dirty(user_id: int) -> bool:
    """Есть ли доступ к грязному разделу (админу можно всегда)."""
    if is_admin(user_id):
        return True
    row = db.execute("SELECT dirty FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["dirty"])


def get_mode(user_id: int) -> str:
    """В каком разделе сейчас человек. Без доступа к грязному всегда обычный."""
    row = db.execute("SELECT mode FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row and row["mode"] == DIRTY and has_dirty(user_id):
        return DIRTY
    return NORMAL


def set_user(user_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE users SET {cols} WHERE user_id = ?", (*fields.values(), user_id))
    db.commit()


def pick_gossip_for(user_id: int, section: str):
    """Чужая сплетня из того же раздела, которую человек ещё не видел.
    Чаще достаются те, что показывали реже всего, чтобы сплетни ходили по кругу."""
    return db.execute(
        """
        SELECT id, text, photo_id FROM gossips
        WHERE hidden = 0
          AND section = ?
          AND author_id != ?
          AND id NOT IN (SELECT gossip_id FROM seen WHERE user_id = ?)
        ORDER BY shown ASC, RANDOM() LIMIT 1
        """,
        (section, user_id, user_id),
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


def main_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    """Две кнопки разделов; тем, у кого есть доступ в грязный раздел, добавляется ещё «Случайная»."""
    rows = [[KeyboardButton(text=BTN_NORMAL), KeyboardButton(text=BTN_DIRTY)]]
    if user_id is not None and has_dirty(user_id):
        rows.append([KeyboardButton(text=BTN_RANDOM)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def meta_get(key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    db.commit()


# --------------------------------------------------------------------------
# Отправка и удаление сплетен
# --------------------------------------------------------------------------
async def send_text(bot: Bot, uid: int, text: str) -> bool:
    """Обычное сообщение с одной повторной попыткой при лимитах Telegram."""
    for _ in range(2):
        try:
            await bot.send_message(uid, text)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            # Человек заблокировал бота: сплетню дня ему больше не шлём
            set_user(uid, daily=0)
            return False
        except TelegramBadRequest:
            return False
    return False


async def send_gossip(
    bot: Bot,
    user_id: int,
    gossip_id: int,
    text: str,
    photo_id: str | None = None,
    section: str = NORMAL,
    title: str | None = None,
) -> bool:
    """Отправляет сплетню (текст или фото) с кнопками голосования.
    False, если не получилось (например, человек заблокировал бота)."""
    head = title or ("🔥 Чужая грязная сплетня" if section == DIRTY else "🤫 Чужая сплетня")
    kb = vote_keyboard(gossip_id)
    sent = None
    for _ in range(2):
        try:
            if photo_id:
                caption = head + (f":\n\n{text}" if text else "")
                sent = await bot.send_photo(user_id, photo_id, caption=caption, reply_markup=kb)
            else:
                sent = await bot.send_message(user_id, f"{head}:\n\n{text}", reply_markup=kb)
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            set_user(user_id, daily=0)
            return False
        except TelegramBadRequest:
            return False
    if sent is None:
        return False
    db.execute("INSERT OR IGNORE INTO seen (user_id, gossip_id) VALUES (?, ?)", (user_id, gossip_id))
    db.execute(
        "INSERT OR REPLACE INTO deliveries (gossip_id, user_id, message_id) VALUES (?, ?, ?)",
        (gossip_id, user_id, sent.message_id),
    )
    db.execute("UPDATE gossips SET shown = shown + 1 WHERE id = ?", (gossip_id,))
    db.commit()
    return True


async def deliver_to_waiting(
    bot: Bot, gossip_id: int, text: str, photo_id: str | None, author_id: int, section: str
) -> None:
    """Те, кто прислал сплетню, когда чужих не было, получают новую сплетню этого раздела."""
    rows = db.execute(
        "SELECT user_id FROM waiting WHERE section = ? AND user_id != ? ORDER BY created_at LIMIT 100",
        (section, author_id),
    ).fetchall()
    for row in rows:
        uid = row["user_id"]
        db.execute("DELETE FROM waiting WHERE user_id = ? AND section = ?", (uid, section))
        db.commit()
        if is_banned(uid):
            continue
        if section == DIRTY and not has_dirty(uid):
            continue
        await send_gossip(bot, uid, gossip_id, text, photo_id, section)
        await asyncio.sleep(0.05)


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
    """Админ узнаёт, что сплетня удалена по голосам."""
    if not ADMIN_ID:
        return
    row = db.execute("SELECT author_id, section FROM gossips WHERE id = ?", (gossip_id,)).fetchone()
    if not row:
        return
    agree, disagree = vote_counts(gossip_id)
    mark = "🔥 " if row["section"] == DIRTY else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🗑 {mark}Сплетня №{gossip_id} удалена по голосам (👍 {agree} / 👎 {disagree})\n"
            f"Автор: {row['author_id']}\n/ban {row['author_id']}",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


async def notify_admin_new_gossip(
    bot: Bot, gossip_id: int, user, text: str, photo_id: str | None, section: str
) -> None:
    """Копия каждой новой сплетни админу (текст или фото) с номером автора для модерации."""
    if not ADMIN_ID or user.id == ADMIN_ID:
        return
    mark = "🔥 [ГРЯЗНАЯ] " if section == DIRTY else ""
    try:
        if photo_id:
            caption = f"🤫 {mark}№{gossip_id}" + (f"\n\n{text}" if text else "")
            sent = await bot.send_photo(ADMIN_ID, photo_id, caption=caption)
        else:
            sent = await bot.send_message(ADMIN_ID, f"🤫 {mark}№{gossip_id}\n\n{text}")
        uname = f"@{user.username}" if user.username else "без юзернейма"
        await bot.send_message(
            ADMIN_ID,
            f"Автор: {user.id} ({uname})\n/del {gossip_id}   /ban {user.id}",
            reply_to_message_id=sent.message_id,
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


# --------------------------------------------------------------------------
# Сплетня дня (каждый день в 13:00 по Москве, только обычный раздел)
# --------------------------------------------------------------------------
async def send_daily_drop(bot: Bot) -> None:
    gossip = db.execute(
        """
        SELECT id, author_id, text, photo_id FROM gossips
        WHERE hidden = 0 AND section = ?
          AND id NOT IN (SELECT gossip_id FROM drops)
        ORDER BY RANDOM() LIMIT 1
        """,
        (NORMAL,),
    ).fetchone()
    users = db.execute("SELECT user_id FROM users WHERE banned = 0 AND daily = 1").fetchall()

    if gossip is None:
        for u in users:
            await send_text(bot, u["user_id"], NO_NEW_TEXT)
            await asyncio.sleep(0.05)  # не превышаем лимит Telegram (около 30 сообщений в секунду)
        if ADMIN_ID:
            await send_text(bot, ADMIN_ID, f"🕐 Сплетня дня: новых сплетен нет, разослано «увы» ({len(users)}).")
        return

    db.execute(
        "INSERT OR IGNORE INTO drops (gossip_id, dropped_at) VALUES (?, ?)",
        (gossip["id"], datetime.now().isoformat()),
    )
    db.commit()
    sent = 0
    for u in users:
        if u["user_id"] == gossip["author_id"]:
            continue  # автору его же сплетню не шлём
        ok = await send_gossip(
            bot, u["user_id"], gossip["id"], gossip["text"], gossip["photo_id"],
            NORMAL, title="🕐 Сплетня дня",
        )
        sent += int(ok)
        await asyncio.sleep(0.05)
    if ADMIN_ID:
        await send_text(bot, ADMIN_ID, f"🕐 Сплетня дня №{gossip['id']} отправлена {sent} людям.")


async def daily_loop(bot: Bot) -> None:
    """Раз в полминуты проверяет время; в 13:00 по Москве запускает рассылку (один раз в день)."""
    now = datetime.now(MSK)
    # Если бот впервые запущен уже после 13:00, не шлём сплетню дня «задним числом»
    if meta_get("daily_date") is None and now.hour >= DAILY_HOUR:
        meta_set("daily_date", now.strftime("%Y-%m-%d"))
    while True:
        try:
            now = datetime.now(MSK)
            today = now.strftime("%Y-%m-%d")
            if now.hour >= DAILY_HOUR and meta_get("daily_date") != today:
                meta_set("daily_date", today)  # сначала отмечаем, чтобы не отправить дважды
                await send_daily_drop(bot)
        except Exception:
            logging.exception("Ошибка ежедневной рассылки")
        await asyncio.sleep(30)


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------
def is_just_the_word_gossip(text: str) -> bool:
    """True, если сообщение состоит только из слова «сплетня» в любой форме
    (сплетня, сплетни, сплетню, сплетней, сплетница...), возможно со знаками и смайлами."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    words = cleaned.split()
    return bool(words) and all(w.startswith("сплетн") for w in words)


def make_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


_code_fails: dict[int, list[float]] = {}


def code_locked(user_id: int) -> bool:
    now = time.time()
    fails = [t for t in _code_fails.get(user_id, []) if now - t < CODE_LOCK_SEC]
    _code_fails[user_id] = fails
    return len(fails) >= CODE_MAX_FAILS


def code_failed(user_id: int) -> None:
    _code_fails.setdefault(user_id, []).append(time.time())


def section_hint(section: str) -> str:
    if section == DIRTY:
        return (
            "🔥 Ты в разделе «Грязные сплетни» (пробный раздел, пока бесплатно).\n"
            "Здесь всё анонимно. Пришли свою сплетню текстом или фото и получи чужую грязную. "
            "Ежедневной рассылки в этом разделе нет, зато есть кнопка «🎲 Случайная грязная сплетня»: "
            "раз в час она выдаёт случайную сплетню.\n"
            "Вернуться: кнопка «💬 Обычные сплетни»."
        )
    return (
        "💬 Ты в разделе «Обычные сплетни».\n"
        "Пришли свою сплетню текстом или фото и получи чужую. "
        "Каждый день в 13:00 по Москве приходит сплетня дня (отключить: /daily)."
    )


# --------------------------------------------------------------------------
# Хендлеры
# --------------------------------------------------------------------------
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    set_user(message.from_user.id, await_code=0)
    mode = get_mode(message.from_user.id)
    await message.answer(
        "🤫 Привет! Я Сплетница.\n\n"
        "Напиши мне свою сплетню (текстом или фото), и я сразу дам тебе чужую. "
        "Сначала ты, потом тебе.\n\n"
        "Всё анонимно: другие пользователи не узнают, кто автор.\n"
        "Под чужой сплетней можно нажать «Согласен» или «Не согласен» (голос один раз). "
        "Если трое не согласны, сплетня удаляется.\n\n"
        "Разделы переключаются кнопками внизу:\n"
        f"{BTN_NORMAL}\n"
        f"{BTN_DIRTY} (пробный раздел, пока бесплатно, вход по коду)\n\n"
        f"Сейчас ты в разделе: {TITLES[mode]}\n\n"
        "Просто отправь свою сплетню следующим сообщением 👇",
        reply_markup=main_keyboard(message.from_user.id),
    )


# ----- команды админа ------------------------------------------------------
@dp.message(Command("code"))
async def cmd_code(message: Message):
    """Создать одноразовые коды для грязного раздела: /code или /code 5"""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    n = max(1, min(n, 20))
    codes: list[str] = []
    while len(codes) < n:
        code = make_code()
        cur = db.execute(
            "INSERT OR IGNORE INTO codes (code, created_at) VALUES (?, ?)",
            (code, datetime.now().isoformat()),
        )
        if cur.rowcount:
            codes.append(code)
    db.commit()
    body = "\n".join(f"<code>{c}</code>" for c in codes)
    await message.answer(
        f"Одноразовые коды для 🔥 Грязных сплетен ({n}):\n\n{body}\n\nКаждый код работает один раз.",
        parse_mode="HTML",
    )


@dp.message(Command("codes"))
async def cmd_codes(message: Message):
    """Неиспользованные коды и кто использовал последние."""
    if not is_admin(message.from_user.id):
        return
    unused = db.execute(
        "SELECT code FROM codes WHERE used_by IS NULL ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    used = db.execute(
        "SELECT code, used_by, used_at FROM codes WHERE used_by IS NOT NULL ORDER BY used_at DESC LIMIT 10"
    ).fetchall()
    total_used = db.execute("SELECT COUNT(*) FROM codes WHERE used_by IS NOT NULL").fetchone()[0]

    lines = [f"Свободных кодов: {len(unused)}"]
    lines += [f"<code>{r['code']}</code>" for r in unused]
    lines.append(f"\nИспользовано всего: {total_used}")
    for r in used:
        when = datetime.fromisoformat(r["used_at"]).strftime("%d.%m %H:%M")
        lines.append(f"{r['code']} → {r['used_by']} ({when})")
    if used:
        lines.append("\nЗабрать доступ: /revoke <id>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    """Забрать доступ к грязному разделу: /revoke 123456"""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /revoke <user_id>")
        return
    uid = int(parts[1])
    set_user(uid, dirty=0, mode=NORMAL, await_code=0)
    await message.answer(f"Доступ к грязному разделу у {uid} закрыт.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    """Отправить сообщение всем пользователям: /broadcast Текст сообщения"""
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Формат: /broadcast <текст сообщения>")
        return
    users = db.execute("SELECT user_id FROM users WHERE banned = 0").fetchall()
    await message.answer(f"Начинаю рассылку: {len(users)} человек...")
    ok = 0
    for u in users:
        if await send_text(bot, u["user_id"], text):
            ok += 1
        await asyncio.sleep(0.05)  # не превышаем лимит Telegram (около 30 сообщений в секунду)
    await message.answer(f"Рассылка завершена: доставлено {ok} из {len(users)}.")


@dp.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /ban <user_id>")
        return
    uid = int(parts[1])
    db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, datetime.now().isoformat()))
    db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM waiting WHERE user_id = ?", (uid,))
    db.commit()
    ids = [r["id"] for r in db.execute("SELECT id FROM gossips WHERE author_id = ? AND hidden = 0", (uid,))]
    for gid in ids:
        await remove_gossip(bot, gid)
    await message.answer(f"Пользователь {uid} забанен, его сплетни удалены ({len(ids)}).")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    n = max(1, min(n, 30))

    rows = db.execute(
        "SELECT id, author_id, text, photo_id, created_at, hidden, section "
        "FROM gossips ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        await message.answer("Сплетен пока нет.")
        return

    def meta_line(r) -> str:
        when = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
        agree, disagree = vote_counts(r["id"])
        sect = "🔥 " if r["section"] == DIRTY else ""
        flags = " [УДАЛЕНА]" if r["hidden"] else ""
        if agree or disagree:
            flags += f" [👍 {agree} / 👎 {disagree}]"
        return f"{sect}№{r['id']} · {when} · автор {r['author_id']}{flags}"

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
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    n = max(1, min(n, 20))

    rows = db.execute(
        """
        SELECT g.id, g.author_id, g.text, g.photo_id, g.hidden, g.section
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
        sect = "🔥 " if r["section"] == DIRTY else ""
        try:
            await send_content(bot, message.from_user.id, r)
        except (TelegramForbiddenError, TelegramBadRequest):
            await message.answer("(не удалось показать содержимое)")
        await message.answer(
            f"{sect}№{r['id']}{flag} · 👍 {agree} / 👎 {disagree} · автор {r['author_id']}\n"
            f"/del {r['id']}   /ban {r['author_id']}"
        )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    def count(sql: str, *args) -> int:
        return db.execute(sql, args).fetchone()[0]

    users = count("SELECT COUNT(*) FROM users")
    dirty_users = count("SELECT COUNT(*) FROM users WHERE dirty = 1")
    normal = count("SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND section = ?", NORMAL)
    dirty = count("SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND section = ?", DIRTY)
    photos = count("SELECT COUNT(*) FROM gossips WHERE hidden = 0 AND photo_id IS NOT NULL")
    hidden = count("SELECT COUNT(*) FROM gossips WHERE hidden = 1")
    free_codes = count("SELECT COUNT(*) FROM codes WHERE used_by IS NULL")
    await message.answer(
        f"Пользователей: {users} (с доступом в грязный раздел: {dirty_users})\n"
        f"Сплетен: обычных {normal}, грязных {dirty} (из них фото: {photos})\n"
        f"Удалено: {hidden}\n"
        f"Свободных кодов: {free_codes}"
    )


# ----- команды для всех ----------------------------------------------------
@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    """Включить или выключить ежедневную сплетню дня."""
    ensure_user(message.from_user)
    row = db.execute("SELECT daily FROM users WHERE user_id = ?", (message.from_user.id,)).fetchone()
    if row["daily"]:
        set_user(message.from_user.id, daily=0)
        await message.answer("🔕 Сплетня дня выключена. Включить: /daily")
    else:
        set_user(message.from_user.id, daily=1)
        await message.answer("🔔 Сплетня дня включена: каждый день в 13:00 по Москве.")


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


# ----- переключение разделов -----------------------------------------------
@dp.message(Command("normal"))
@dp.message(F.text == BTN_NORMAL)
async def on_btn_normal(message: Message):
    ensure_user(message.from_user)
    if is_banned(message.from_user.id):
        return
    set_user(message.from_user.id, mode=NORMAL, await_code=0)
    await message.answer(section_hint(NORMAL), reply_markup=main_keyboard(message.from_user.id))


@dp.message(Command("dirty"))
@dp.message(F.text == BTN_DIRTY)
async def on_btn_dirty(message: Message):
    ensure_user(message.from_user)
    uid = message.from_user.id
    if is_banned(uid):
        return
    if has_dirty(uid):
        set_user(uid, mode=DIRTY, await_code=0)
        await message.answer(section_hint(DIRTY), reply_markup=main_keyboard(message.from_user.id))
        return
    set_user(uid, await_code=1)
    await message.answer(
        "🔥 «Грязные сплетни» — закрытый раздел. Сейчас он работает в пробном режиме и пока бесплатный.\n\n"
        "Чтобы войти, пришли одноразовый код доступа одним сообщением.\n"
        "Передумал? Нажми «💬 Обычные сплетни».",
        reply_markup=main_keyboard(message.from_user.id),
    )


@dp.message(Command("random"))
@dp.message(F.text == BTN_RANDOM)
async def on_random(message: Message, bot: Bot):
    """Случайная грязная сплетня по кнопке: раз в час, только тем, у кого есть доступ в грязный раздел."""
    ensure_user(message.from_user)
    uid = message.from_user.id
    if is_banned(uid):
        return
    if not has_dirty(uid):
        await message.answer(
            "🔥 Эта кнопка работает в разделе «Грязные сплетни». "
            "Открой его кнопкой «🔥 Грязные сплетни» и введи код доступа."
        )
        return

    row = db.execute("SELECT last_random FROM users WHERE user_id = ?", (uid,)).fetchone()
    if not is_admin(uid) and row and row["last_random"]:
        left = timedelta(minutes=RANDOM_COOLDOWN_MIN) - (datetime.now() - datetime.fromisoformat(row["last_random"]))
        if left.total_seconds() > 0:
            mins = int(left.total_seconds() // 60) + 1
            await message.answer(f"⏳ Случайную грязную сплетню можно получать раз в час. Подожди ещё {mins} мин.")
            return

    g = db.execute(
        """
        SELECT id, text, photo_id FROM gossips
        WHERE hidden = 0 AND section = ? AND author_id != ?
          AND id NOT IN (SELECT gossip_id FROM seen WHERE user_id = ?)
        ORDER BY RANDOM() LIMIT 1
        """,
        (DIRTY, uid, uid),
    ).fetchone()
    if g is None:
        await message.answer("Новых грязных сплетен для тебя пока нет. Загляни позже или пришли свою 😏")
        return

    set_user(uid, last_random=datetime.now().isoformat())  # сначала отмечаем, чтобы двойное нажатие не сработало
    ok = await send_gossip(bot, uid, g["id"], g["text"], g["photo_id"], DIRTY, title="🎲 Случайная грязная сплетня")
    if not ok:
        set_user(uid, last_random=None)
        await message.answer("Не получилось отправить, попробуй ещё раз.")


async def try_redeem_code(message: Message) -> None:
    """Человек в режиме ввода кода прислал текст: проверяем, это рабочий одноразовый код."""
    uid = message.from_user.id
    if code_locked(uid):
        await message.answer("Слишком много неверных попыток. Попробуй позже.")
        return
    code = re.sub(r"[\s\-]", "", message.text or "").upper()
    cur = db.execute(
        "UPDATE codes SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
        (uid, datetime.now().isoformat(), code),
    )
    db.commit()
    if cur.rowcount != 1:
        code_failed(uid)
        await message.answer("Код не подошёл или уже использован. Проверь и пришли ещё раз.")
        return
    set_user(uid, dirty=1, mode=DIRTY, await_code=0)
    _code_fails.pop(uid, None)
    await message.answer("✅ Код принят! Добро пожаловать.\n\n" + section_hint(DIRTY), reply_markup=main_keyboard(message.from_user.id))


# ----- приём сплетен -------------------------------------------------------
async def process_gossip(message: Message, bot: Bot, text: str, photo_id: str | None = None) -> None:
    """Общая часть для текстовой и фото-сплетни: сохранить, отдать ждущим, выдать автору чужую."""
    user_id = message.from_user.id
    section = get_mode(user_id)

    cur = db.execute(
        "INSERT INTO gossips (author_id, text, photo_id, created_at, section) VALUES (?, ?, ?, ?, ?)",
        (user_id, text, photo_id, datetime.now().isoformat(), section),
    )
    gossip_id = cur.lastrowid
    db.commit()

    # Копия сплетни админу
    await notify_admin_new_gossip(bot, gossip_id, message.from_user, text, photo_id, section)

    # Новую сплетню сразу отдаём тем, кто ждёт в этом же разделе
    await deliver_to_waiting(bot, gossip_id, text, photo_id, user_id, section)

    # Взамен выдаём автору чужую из того же раздела
    row = pick_gossip_for(user_id, section)
    if row:
        await send_gossip(bot, user_id, row["id"], row["text"], row["photo_id"], section)
    else:
        db.execute(
            "INSERT OR IGNORE INTO waiting (user_id, section, created_at) VALUES (?, ?, ?)",
            (user_id, section, datetime.now().isoformat()),
        )
        db.commit()
        await message.answer(
            "Твоя сплетня принята ✅ Пока чужих сплетен в этом разделе нет, "
            "но как только появится новая, я пришлю её тебе."
        )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot):
    ensure_user(message.from_user)
    uid = message.from_user.id
    if is_banned(uid):
        return

    row = db.execute("SELECT await_code FROM users WHERE user_id = ?", (uid,)).fetchone()
    if row and row["await_code"]:
        if has_dirty(uid):
            set_user(uid, await_code=0)
        else:
            await try_redeem_code(message)
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
async def on_photo(message: Message, bot: Bot):
    """Фото (с подписью или без) равносильно сплетне."""
    ensure_user(message.from_user)
    uid = message.from_user.id
    if is_banned(uid):
        return
    row = db.execute("SELECT await_code FROM users WHERE user_id = ?", (uid,)).fetchone()
    if row and row["await_code"] and not has_dirty(uid):
        await message.answer("Сначала пришли код доступа текстом или нажми «💬 Обычные сплетни».")
        return
    caption = (message.caption or "").strip()
    if len(caption) > MAX_CAPTION:
        await message.answer(f"Подпись слишком длинная, уложись в {MAX_CAPTION} символов.")
        return
    photo_id = message.photo[-1].file_id  # самый большой размер
    await process_gossip(message, bot, caption, photo_id)


# ----- голосование ---------------------------------------------------------
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
    g = db.execute(
        "SELECT hidden, author_id, section FROM gossips WHERE id = ?", (gossip_id,)
    ).fetchone()
    if not g or g["hidden"]:
        await cb.answer("Эта сплетня уже удалена.")
        return
    if g["author_id"] == user_id:
        await cb.answer("За свою сплетню голосовать нельзя.")
        return
    if g["section"] == DIRTY and not has_dirty(user_id):
        await cb.answer("Нет доступа.")
        return

    # Один человек голосует за одну сплетню только один раз, голос потом не меняется
    cur = db.execute(
        "INSERT OR IGNORE INTO votes (gossip_id, user_id, vote) VALUES (?, ?, ?)",
        (gossip_id, user_id, vote),
    )
    db.commit()
    if cur.rowcount == 0:
        await cb.answer("Ты уже голосовал за эту сплетню.")
        return

    _, disagree = vote_counts(gossip_id)
    if disagree >= DISLIKE_LIMIT:
        await cb.answer("Голос учтён. Сплетня удалена.")
        await notify_admin_removed(bot, gossip_id)
        await remove_gossip(bot, gossip_id)
        return

    await cb.answer("Голос учтён")
    if cb.message:
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=cb.message.message_id,
                reply_markup=vote_keyboard(gossip_id),
            )
        except TelegramBadRequest:
            pass


@dp.message()
async def fallback(message: Message):
    if is_banned(message.from_user.id):
        return
    await message.answer("Присылай сплетню текстом или фото 🙂")


# --------------------------------------------------------------------------
async def setup_menu(bot: Bot) -> None:
    """Кнопка «Меню» слева от поля ввода: выбор раздела и остальные команды."""
    public = [
        BotCommand(command="normal", description=BTN_NORMAL),
        BotCommand(command="dirty", description=BTN_DIRTY),
        BotCommand(command="random", description=BTN_RANDOM),
        BotCommand(command="daily", description="🔔 Сплетня дня: вкл/выкл"),
        BotCommand(command="start", description="🏠 Начало"),
    ]
    try:
        await bot.set_my_commands(public)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logging.exception("Не удалось настроить меню")
    if ADMIN_ID:
        admin = public + [
            BotCommand(command="code", description="🔑 Создать одноразовый код"),
            BotCommand(command="codes", description="🔑 Список кодов"),
            BotCommand(command="last", description="📋 Последние сплетни"),
            BotCommand(command="reported", description="⚠️ Сплетни с «не согласен»"),
            BotCommand(command="stats", description="📊 Статистика"),
            BotCommand(command="broadcast", description="📣 Рассылка всем"),
        ]
        try:
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception:
            logging.warning("Админское меню не настроено (админ ещё не писал боту?)")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Добавьте токен в переменные окружения.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    await setup_menu(bot)
    daily_task = asyncio.create_task(daily_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        daily_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
