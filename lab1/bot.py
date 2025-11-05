import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)


# -----------------------------
# Configuration & constants
# -----------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "tasks.db")

# Conversation states for new task deadline prompt
ASK_DEADLINE = 1

# Telegram message hard limit
TELEGRAM_MSG_LIMIT = 4096


# -----------------------------
# Data models and utilities
# -----------------------------

@dataclass
class Task:
    id: int
    chat_id: int
    text: str
    deadline_utc_iso: Optional[str]  # ISO format in UTC, or None
    completed: int  # 0/1

    @property
    def deadline_dt(self) -> Optional[datetime]:
        if not self.deadline_utc_iso:
            return None
        try:
            return datetime.fromisoformat(self.deadline_utc_iso)
        except Exception:
            return None

    def priority(self) -> str:
        """Compute priority based on time remaining to deadline.

        - High: <= 2 days
        - Medium: <= 7 days
        - Low: > 7 days or no deadline
        """
        dl = self.deadline_dt
        if not dl:
            return "Low"
        remaining = dl - datetime.utcnow()
        if remaining <= timedelta(days=2):
            return "High"
        if remaining <= timedelta(days=7):
            return "Medium"
        return "Low"


def ensure_db(path: str) -> None:
    """Initialize the SQLite database if it doesn't exist."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                deadline_utc_iso TEXT,
                completed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def add_task(chat_id: int, text: str, deadline_utc: Optional[datetime]) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (chat_id, text, deadline_utc_iso, completed) VALUES (?, ?, ?, 0)",
            (chat_id, text, deadline_utc.isoformat() if deadline_utc else None),
        )
        conn.commit()
        return int(cursor.lastrowid)


def set_task_deadline(task_id: int, deadline_utc: Optional[datetime]) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE tasks SET deadline_utc_iso = ? WHERE id = ?",
            (deadline_utc.isoformat() if deadline_utc else None, task_id),
        )
        conn.commit()


def complete_task(chat_id: int, task_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET completed = 1 WHERE id = ? AND chat_id = ? AND completed = 0",
            (task_id, chat_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_tasks(chat_id: int, include_completed: bool = False) -> List[Task]:
    query = "SELECT id, chat_id, text, deadline_utc_iso, completed FROM tasks WHERE chat_id = ?"
    params: Tuple = (chat_id,)
    if not include_completed:
        query += " AND completed = 0"

    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
    tasks = [Task(*row) for row in rows]
    return tasks


def get_task_by_id(chat_id: int, task_id: int) -> Optional[Task]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, chat_id, text, deadline_utc_iso, completed FROM tasks WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        )
        row = cursor.fetchone()
    return Task(*row) if row else None


def parse_deadline_to_utc(text: str) -> Optional[datetime]:
    """Parse deadline input in formats:
    - YYYY-MM-DD
    - YYYY-MM-DD HH:MM

    Returns UTC naive datetime computed as (local now + offset).
    If only date provided, default time is 09:00 local.
    """
    text = text.strip()
    try:
        if len(text) == 10:
            # Date only
            local_dt = datetime.strptime(text, "%Y-%m-%d")
            local_dt = local_dt.replace(hour=9, minute=0)
        else:
            local_dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None

    # Convert local naive to UTC naive using current offset approximation
    # This avoids timezone libraries; good enough for reminders relative to now
    now_local = datetime.now()
    now_utc = datetime.utcnow()
    offset = now_local - now_utc
    utc_dt = local_dt - offset
    return utc_dt


async def schedule_reminder(context: CallbackContext, chat_id: int, task_id: int, deadline_utc: Optional[datetime]) -> None:
    if not deadline_utc:
        return
    reminder_time = deadline_utc - timedelta(days=1)
    delay = (reminder_time - datetime.utcnow()).total_seconds()
    if delay <= 0:
        return

    # Unique job name per chat/task for idempotency
    job_name = f"reminder_{chat_id}_{task_id}"

    async def _reminder_job(ctx: CallbackContext) -> None:
        task = get_task_by_id(chat_id, task_id)
        if not task or task.completed:
            return
        deadline_str = task.deadline_dt.strftime("%Y-%m-%d %H:%M") if task.deadline_dt else "не указан"
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Напоминание: завтра дедлайн по задаче #{task.id}\n"
                f"Текст: {task.text}\n"
                f"Дедлайн (UTC): {deadline_str}"
            ),
        )

    # Remove existing with same name then schedule
    existing = context.job_queue.get_jobs_by_name(job_name)
    for job in existing:
        job.schedule_removal()
    context.job_queue.run_once(_reminder_job, when=delay, name=job_name)


def format_task_for_list(t: Task) -> str:
    deadline_str = t.deadline_dt.strftime("%Y-%m-%d %H:%M") if t.deadline_dt else "-"
    return f"#{t.id} | {t.priority()} | {deadline_str} | {t.text}"


# -----------------------------
# Handlers
# -----------------------------

async def start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "Привет! Я бот для управления задачами.\n\n"
        "Команды:\n"
        "/newtask {текст} — создать задачу (далее укажете дедлайн)\n"
        "/alltasks — показать задачи\n"
        "/completetask {id} — завершить задачу\n"
        "/todo {список id} — сформировать чеклист\n"
        "\nДедлайн укажите форматом YYYY-MM-DD или YYYY-MM-DD HH:MM (локальное время)."
    )


async def newtask_entry(update: Update, context: CallbackContext) -> int:
    args_text = update.message.text.partition(" ")[2].strip()
    if not args_text:
        await update.message.reply_text("Пожалуйста, укажите текст задачи: /newtask {текст}")
        return ConversationHandler.END

    chat_id = update.message.chat_id
    task_id = add_task(chat_id, args_text, None)
    context.user_data["new_task_id"] = task_id

    await update.message.reply_text(
        "Задача создана. Укажите дедлайн в формате YYYY-MM-DD или YYYY-MM-DD HH:MM.\n"
        "Отправьте /cancel для отмены указания дедлайна."
    )
    return ASK_DEADLINE


async def ask_deadline(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if text.startswith("/"):
        # Any command cancels
        await update.message.reply_text("Отмена указания дедлайна. Можно установить позже другой командой.")
        return ConversationHandler.END

    deadline_utc = parse_deadline_to_utc(text)
    if not deadline_utc:
        await update.message.reply_text("Неверный формат. Пример: 2025-11-05 или 2025-11-05 18:30")
        return ASK_DEADLINE

    if deadline_utc <= datetime.utcnow():
        await update.message.reply_text("Дедлайн должен быть в будущем. Попробуйте снова.")
        return ASK_DEADLINE

    task_id = context.user_data.get("new_task_id")
    if not task_id:
        await update.message.reply_text("Не удалось найти задачу. Повторите /newtask.")
        return ConversationHandler.END

    set_task_deadline(task_id, deadline_utc)
    await schedule_reminder(context, update.message.chat_id, task_id, deadline_utc)

    await update.message.reply_text("Дедлайн сохранён и напоминание запланировано (за 1 день).")
    context.user_data.pop("new_task_id", None)
    return ConversationHandler.END


async def alltasks(update: Update, context: CallbackContext) -> None:
    tasks = get_tasks(update.message.chat_id, include_completed=False)
    if not tasks:
        await update.message.reply_text("Активных задач нет.")
        return
    lines = ["ID | Приоритет | Дедлайн (UTC) | Текст"]
    for t in tasks:
        lines.append(format_task_for_list(t))
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MSG_LIMIT:
        # Fallback to multiple chunks
        chunk = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > TELEGRAM_MSG_LIMIT:
                await update.message.reply_text("\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await update.message.reply_text("\n".join(chunk))
    else:
        await update.message.reply_text(text)


async def completetask(update: Update, context: CallbackContext) -> None:
    parts = update.message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Использование: /completetask {id}")
        return
    task_id = int(parts[1])
    ok = complete_task(update.message.chat_id, task_id)
    if ok:
        # If there was a scheduled job, remove it
        job_name = f"reminder_{update.message.chat_id}_{task_id}"
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        await update.message.reply_text("Задача завершена и убрана из списка.")
    else:
        await update.message.reply_text("Задача не найдена или уже завершена.")


def build_checklist_lines(tasks: List[Task]) -> List[str]:
    return [f"[ ] {t.text}" for t in tasks]


async def todo(update: Update, context: CallbackContext) -> None:
    # Usage: /todo 1 2 3
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Использование: /todo {список id через пробел}")
        return

    ids: List[int] = []
    for p in parts[1:]:
        if p.isdigit():
            ids.append(int(p))

    if not ids:
        await update.message.reply_text("Не указаны корректные id.")
        return

    selected: List[Task] = []
    for i in ids:
        t = get_task_by_id(update.message.chat_id, i)
        if t and not t.completed:
            selected.append(t)

    if not selected:
        await update.message.reply_text("Задачи не найдены.")
        return

    lines = build_checklist_lines(selected)
    checklist = "\n".join(lines)

    # Trim to Telegram limit
    if len(checklist) > TELEGRAM_MSG_LIMIT:
        checklist = checklist[:TELEGRAM_MSG_LIMIT - 10] + "\n..."

    await update.message.reply_text(checklist)


async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def help_(update: Update, context: CallbackContext) -> None:
    await start(update, context)


async def on_startup(app) -> None:
    # Ensure DB exists
    ensure_db(DB_PATH)
    # Schedule pending reminders for all chats
    # Note: We do not have list of chats centrally; we schedule per task
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, chat_id, text, deadline_utc_iso, completed FROM tasks WHERE completed = 0"
        )
        rows = cursor.fetchall()
    for row in rows:
        t = Task(*row)
        await schedule_reminder(app.bot_data["context"], t.chat_id, t.id, t.deadline_dt)


def build_application():
    # Register post_init to schedule reminders after initialization
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    # We store context reference to use in startup scheduling utility
    # PTB provides JobQueue in app.job_queue; use context wrapper for schedule API match
    # Create a lightweight context-like holder
    class _Ctx:
        def __init__(self, app):
            self.bot = app.bot
            self.job_queue = app.job_queue

    app.bot_data["context"] = _Ctx(app)

    conv = ConversationHandler(
        entry_points=[CommandHandler("newtask", newtask_entry)],
        states={
            ASK_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_deadline)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="newtask_conversation",
        persistent=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_))
    app.add_handler(conv)
    app.add_handler(CommandHandler("alltasks", alltasks))
    app.add_handler(CommandHandler("completetask", completetask))
    app.add_handler(CommandHandler("todo", todo))

    return app


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Укажите его в .env или переменных окружения.")
    app = build_application()
    # Run polling (blocking call)
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Ошибка запуска: {exc}")


