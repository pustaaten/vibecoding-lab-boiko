import asyncio
import json
import logging
import requests
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TimedOut, NetworkError, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)


# -----------------------------
# Configuration & constants
# -----------------------------

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "tasks.db")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# Conversation states for new task deadline prompt
ASK_DEADLINE = 1

# Telegram message hard limit
TELEGRAM_MSG_LIMIT = 4096
# Safe chunk size (leave margin for safety)
TELEGRAM_CHUNK_SIZE = 4000

# NewsAPI categories supported (per docs)
NEWS_CATEGORIES = [
    "business",
    "entertainment",
    "general",
    "health",
    "science",
    "sports",
    "technology",
]

# Russian names for categories for better UX
CATEGORY_NAMES = {
    "business": "Бизнес",
    "entertainment": "Развлечения",
    "general": "Общее",
    "health": "Здоровье",
    "science": "Наука",
    "sports": "Спорт",
    "technology": "Технологии",
}


# -----------------------------
# Utility functions
# -----------------------------

async def safe_send_message(update: Update, text: str, max_retries: int = 2) -> bool:
    """Safely send a message with automatic chunking and error handling.
    
    Returns True if successful, False otherwise.
    """
    if not text:
        return False
    
    # If message is too long, split into chunks
    if len(text) <= TELEGRAM_CHUNK_SIZE:
        for attempt in range(max_retries):
            try:
                await update.message.reply_text(text)
                return True
            except (TimedOut, NetworkError) as e:
                logger.warning(f"Timeout/Network error sending message (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)  # Wait before retry
                    continue
                return False
            except TelegramError as e:
                logger.error(f"Telegram error sending message: {e}")
                return False
            except Exception as e:
                logger.error(f"Unexpected error sending message: {e}")
                return False
        return False
    
    # Chunk the message
    lines = text.split('\n')
    chunks = []
    current_chunk = []
    current_size = 0
    
    for line in lines:
        line_size = len(line) + 1  # +1 for newline
        if current_size + line_size > TELEGRAM_CHUNK_SIZE and current_chunk:
            chunks.append('\n'.join(current_chunk))
            current_chunk = [line]
            current_size = line_size
        else:
            current_chunk.append(line)
            current_size += line_size
    
    if current_chunk:
        chunks.append('\n'.join(current_chunk))
    
    # Send chunks with error handling
    success = True
    for i, chunk in enumerate(chunks):
        chunk_sent = False
        for attempt in range(max_retries):
            try:
                if i == 0:
                    await update.message.reply_text(chunk)
                else:
                    # For subsequent chunks, add a plain text header (no markdown to avoid issues)
                    await update.message.reply_text(f"[продолжение {i+1}/{len(chunks)}]\n{chunk}")
                chunk_sent = True
                # Small delay between chunks to avoid rate limiting
                await asyncio.sleep(0.5)
                break
            except (TimedOut, NetworkError) as e:
                logger.warning(f"Timeout/Network error sending chunk {i+1} (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                success = False
            except TelegramError as e:
                logger.error(f"Telegram error sending chunk {i+1}: {e}")
                success = False
                break
            except Exception as e:
                logger.error(f"Unexpected error sending chunk {i+1}: {e}")
                success = False
                break
        
        if not chunk_sent:
            success = False
    
    return success


# -----------------------------
# News API helpers
# -----------------------------

def news_api_get(path: str, params: dict) -> Optional[dict]:
    """Call NewsAPI and return JSON or None on error. Adds apiKey automatically."""
    if not NEWSAPI_KEY:
        logger.warning("NewsAPI call attempted but NEWSAPI_KEY is not set")
        return None
    url = f"https://newsapi.org/v2/{path}"
    headers = {"User-Agent": "TaskNewsBot/1.0"}
    try:
        logger.info(f"Calling NewsAPI: {path} with params: {list(params.keys())}")
        response = requests.get(url, params={**params, "apiKey": NEWSAPI_KEY}, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"NewsAPI returned status {response.status_code} for {path}")
            return None
        data = response.json()
        if data.get("status") != "ok":
            logger.warning(f"NewsAPI returned status != 'ok': {data.get('status')}")
            return None
        logger.info(f"NewsAPI call successful: {path}")
        return data
    except requests.exceptions.Timeout:
        logger.error(f"NewsAPI request timeout for {path}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"NewsAPI request error for {path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error calling NewsAPI {path}: {e}")
        return None


def format_articles(articles: List[dict], limit: int = 5) -> str:
    lines: List[str] = []
    for art in articles[:limit]:
        title = art.get("title") or "(без заголовка)"
        url = art.get("url") or ""
        source = (art.get("source") or {}).get("name") or ""
        published = art.get("publishedAt") or ""
        line = f"• {title} ({source})\n{url}"
        if published:
            line += f"\n{published}"
        lines.append(line)
    if not lines:
        return "Новостей не найдено."
    text = "\n\n".join(lines)
    if len(text) > TELEGRAM_MSG_LIMIT:
        text = text[:TELEGRAM_MSG_LIMIT - 10] + "\n..."
    return text


# -----------------------------
# News command handlers
# -----------------------------

def build_category_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard with news categories as buttons.
    
    Creates a keyboard with 2 buttons per row for better layout.
    """
    keyboard = []
    # Create rows with 2 buttons each
    for i in range(0, len(NEWS_CATEGORIES), 2):
        row = []
        # Add first button in row
        cat = NEWS_CATEGORIES[i]
        row.append(InlineKeyboardButton(
            CATEGORY_NAMES.get(cat, cat.capitalize()),
            callback_data=f"news_category_{cat}"
        ))
        # Add second button if exists
        if i + 1 < len(NEWS_CATEGORIES):
            cat2 = NEWS_CATEGORIES[i + 1]
            row.append(InlineKeyboardButton(
                CATEGORY_NAMES.get(cat2, cat2.capitalize()),
                callback_data=f"news_category_{cat2}"
            ))
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


async def fetch_and_send_news(update: Update, context: CallbackContext, category: str, is_callback: bool = False) -> None:
    """Fetch news for a category and send to user.
    
    Args:
        update: Update object (from command or callback)
        context: CallbackContext
        category: News category name
        is_callback: True if called from callback query (button click), False if from command
    """
    if not NEWSAPI_KEY:
        error_msg = "NEWSAPI_KEY не задан. Укажите его в .env, чтобы пользоваться новостями."
        if is_callback and update.callback_query:
            try:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(error_msg)
            except Exception as e:
                logger.error(f"Error sending error message via callback: {e}")
        else:
            await safe_send_message(update, error_msg)
        return
    
    if category not in NEWS_CATEGORIES:
        error_msg = "Неизвестная категория. См. /categories"
        if is_callback and update.callback_query:
            try:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(error_msg)
            except Exception as e:
                logger.error(f"Error sending error message via callback: {e}")
        else:
            await safe_send_message(update, error_msg)
        return
    
    chat_id = update.effective_chat.id
    country = context.chat_data.get("news_country", "us")
    logger.info(f"Fetching news for category={category}, country={country}, chat={chat_id}")
    
    # Show loading message for callback queries
    if is_callback and update.callback_query:
        try:
            await update.callback_query.answer("Загрузка новостей...")
        except Exception:
            pass
    
    # Fetch news from API
    data = news_api_get("top-headlines", {"category": category, "country": country, "pageSize": 10})
    if not data:
        error_msg = "Не удалось получить новости. Попробуйте позже."
        if is_callback and update.callback_query:
            try:
                await update.callback_query.edit_message_text(error_msg, reply_markup=None)
            except Exception as e:
                logger.error(f"Error updating message via callback: {e}")
                # Fallback: send new message
                try:
                    await update.callback_query.message.reply_text(error_msg)
                except Exception:
                    pass
        else:
            await safe_send_message(update, error_msg)
        return
    
    text = format_articles(data.get("articles", []))
    
    # Send news articles
    if is_callback and update.callback_query:
        try:
            # Try to edit the message with keyboard
            full_text = f"📰 Новости: {CATEGORY_NAMES.get(category, category)}\n\n{text}"
            # Telegram message limit is 4096, but we need space for header
            if len(full_text) > 4000:
                # Split into parts
                await update.callback_query.edit_message_text(
                    f"📰 Новости: {CATEGORY_NAMES.get(category, category)}\n\n{text[:3900]}...",
                    reply_markup=None  # Remove keyboard after selection
                )
                # Send the rest
                remaining = text[3900:]
                await update.callback_query.message.reply_text(remaining)
            else:
                await update.callback_query.edit_message_text(
                    full_text,
                    reply_markup=None  # Remove keyboard after selection
                )
        except Exception as e:
            logger.error(f"Error editing message via callback: {e}")
            # Fallback: send new message
            try:
                await update.callback_query.message.reply_text(f"📰 Новости: {CATEGORY_NAMES.get(category, category)}\n\n{text}")
            except Exception as send_err:
                logger.error(f"Error sending message via callback: {send_err}")
    else:
        # Send via command handler
        await safe_send_message(update, text)


async def news_categories(update: Update, context: CallbackContext) -> None:
    """Show available news categories."""
    logger.info(f"User {update.effective_user.id} requested news categories")
    cats = ", ".join(NEWS_CATEGORIES)
    await safe_send_message(update, f"Доступные категории: {cats}")


async def set_country(update: Update, context: CallbackContext) -> None:
    logger.info(f"User {update.effective_user.id} setting country")
    parts = update.message.text.split()
    if len(parts) < 2:
        await safe_send_message(update, "Использование: /country {двухбуквенный код, напр. us, gb, de}")
        return
    code = parts[1].lower()
    if len(code) != 2:
        await safe_send_message(update, "Код страны должен быть из 2 букв, напр. us")
        return
    context.chat_data["news_country"] = code
    logger.info(f"Country set to {code} for user {update.effective_user.id}")
    await safe_send_message(update, f"Страна для новостей: {code}")


async def news_by_category(update: Update, context: CallbackContext) -> None:
    """Handle /news command - show category buttons or fetch news if category specified."""
    logger.info(f"User {update.effective_user.id} requested news by category")
    
    if not NEWSAPI_KEY:
        await safe_send_message(update, "NEWSAPI_KEY не задан. Укажите его в .env, чтобы пользоваться новостями.")
        return
    
    parts = update.message.text.split(maxsplit=1)
    
    # If no category specified, show inline keyboard with category buttons
    if len(parts) < 2:
        try:
            keyboard = build_category_keyboard()
            country = context.chat_data.get("news_country", "us")
            message_text = (
                "📰 Выберите категорию новостей:\n\n"
                f"Страна: {country.upper()}\n"
                "(Используйте /country {код} для изменения)"
            )
            await update.message.reply_text(message_text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error sending category keyboard: {e}")
            await safe_send_message(update, "Использование: /news {категория}. См. /categories")
        return
    
    # Category specified - fetch news directly
    category = parts[1].strip().lower()
    await fetch_and_send_news(update, context, category, is_callback=False)


async def news_category_callback(update: Update, context: CallbackContext) -> None:
    """Handle callback query when user clicks on a category button."""
    query = update.callback_query
    if not query:
        return
    
    # Extract category from callback_data (format: "news_category_{category}")
    callback_data = query.data
    if not callback_data.startswith("news_category_"):
        logger.warning(f"Unknown callback data: {callback_data}")
        try:
            await query.answer("Неизвестная команда")
        except Exception:
            pass
        return
    
    category = callback_data.replace("news_category_", "")
    logger.info(f"User {update.effective_user.id} selected category: {category}")
    
    # Fetch and send news for selected category
    await fetch_and_send_news(update, context, category, is_callback=True)


async def list_sources(update: Update, context: CallbackContext) -> None:
    logger.info(f"User {update.effective_user.id} requested news sources")
    if not NEWSAPI_KEY:
        await safe_send_message(update, "NEWSAPI_KEY не задан. Укажите его в .env, чтобы пользоваться новостями.")
        return
    # Popular sources: top-headlines/sources (optionally filter by language or country)
    data = news_api_get("top-headlines/sources", {"language": "en"})
    if not data:
        await safe_send_message(update, "Не удалось получить источники. Попробуйте позже.")
        return
    sources = data.get("sources", [])
    if not sources:
        await safe_send_message(update, "Источники не найдены.")
        return
    # Limit to first 30 sources to avoid extremely long messages and timeouts
    # Format: id - name (more compact)
    lines = [f"{s.get('id')} - {s.get('name', '')}" for s in sources[:30] if s.get('id')]
    if not lines:
        await safe_send_message(update, "Источники не найдены.")
        return
    text = "Популярные источники (первые 30):\n\n" + "\n".join(lines)
    logger.info(f"Sending {len(lines)} sources to user {update.effective_user.id}")
    # Use safe_send_message which handles chunking automatically
    await safe_send_message(update, text)


async def news_by_source(update: Update, context: CallbackContext) -> None:
    logger.info(f"User {update.effective_user.id} requested news by source")
    if not NEWSAPI_KEY:
        await safe_send_message(update, "NEWSAPI_KEY не задан. Укажите его в .env, чтобы пользоваться новостями.")
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await safe_send_message(update, "Использование: /source {id источника}. См. /sources")
        return
    source_id = parts[1].strip()
    if not source_id:
        await safe_send_message(update, "Укажите id источника. См. /sources")
        return
    logger.info(f"Fetching news for source={source_id}")
    data = news_api_get("top-headlines", {"sources": source_id, "pageSize": 10})
    if not data:
        await safe_send_message(update, "Не удалось получить новости этого источника. Попробуйте позже.")
        return
    text = format_articles(data.get("articles", []))
    await safe_send_message(update, text)


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
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO tasks (chat_id, text, deadline_utc_iso, completed) VALUES (?, ?, ?, 0)",
                (chat_id, text, deadline_utc.isoformat() if deadline_utc else None),
            )
            conn.commit()
            task_id = int(cursor.lastrowid)
            logger.info(f"Added task {task_id} for chat {chat_id}")
            return task_id
    except Exception as e:
        logger.error(f"Error adding task for chat {chat_id}: {e}")
        raise


def set_task_deadline(task_id: int, deadline_utc: Optional[datetime]) -> None:
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                "UPDATE tasks SET deadline_utc_iso = ? WHERE id = ?",
                (deadline_utc.isoformat() if deadline_utc else None, task_id),
            )
            conn.commit()
            logger.info(f"Set deadline for task {task_id}")
    except Exception as e:
        logger.error(f"Error setting deadline for task {task_id}: {e}")
        raise


def complete_task(chat_id: int, task_id: int) -> bool:
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tasks SET completed = 1 WHERE id = ? AND chat_id = ? AND completed = 0",
                (task_id, chat_id),
            )
            conn.commit()
            success = cursor.rowcount > 0
            if success:
                logger.info(f"Completed task {task_id} for chat {chat_id}")
            else:
                logger.warning(f"Task {task_id} not found or already completed for chat {chat_id}")
            return success
    except Exception as e:
        logger.error(f"Error completing task {task_id} for chat {chat_id}: {e}")
        return False


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
            logger.info(f"Reminder skipped: task {task_id} not found or already completed")
            return
        deadline_str = task.deadline_dt.strftime("%Y-%m-%d %H:%M") if task.deadline_dt else "не указан"
        reminder_text = (
            f"Напоминание: завтра дедлайн по задаче #{task.id}\n"
            f"Текст: {task.text}\n"
            f"Дедлайн (UTC): {deadline_str}"
        )
        try:
            await ctx.bot.send_message(chat_id=chat_id, text=reminder_text)
            logger.info(f"Reminder sent for task {task_id} to chat {chat_id}")
        except (TimedOut, NetworkError) as e:
            logger.warning(f"Failed to send reminder for task {task_id}: {e}")
            # Try once more after a delay
            try:
                await asyncio.sleep(2)
                await ctx.bot.send_message(chat_id=chat_id, text=reminder_text)
                logger.info(f"Reminder sent on retry for task {task_id}")
            except Exception as retry_err:
                logger.error(f"Reminder retry failed for task {task_id}: {retry_err}")
        except TelegramError as e:
            logger.error(f"Telegram error sending reminder for task {task_id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending reminder for task {task_id}: {e}")

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
    logger.info(f"User {update.effective_user.id} started the bot")
    await safe_send_message(
        update,
        "Привет! Я бот для управления задачами.\n\n"
        "Команды:\n"
        "/newtask {текст} — создать задачу (далее укажете дедлайн)\n"
        "/alltasks — показать задачи\n"
        "/completetask {id} — завершить задачу\n"
        "/todo {список id} — сформировать чеклист\n"
        "\nНовости:\n"
        "/categories — список доступных категорий\n"
        "/news — показать кнопки с категориями новостей\n"
        "/news {категория} — последние заголовки по категории (по стране)\n"
        "/country {код} — задать страну\n"
        "/sources — популярные источники\n"
        "/source {источник} — последние заголовки с источника\n"
        "\nДедлайн укажите форматом YYYY-MM-DD или YYYY-MM-DD HH:MM (локальное время)."
    )


async def newtask_entry(update: Update, context: CallbackContext) -> int:
    logger.info(f"User {update.effective_user.id} creating new task")
    args_text = update.message.text.partition(" ")[2].strip()
    if not args_text:
        await safe_send_message(update, "Пожалуйста, укажите текст задачи: /newtask {текст}")
        return ConversationHandler.END

    chat_id = update.message.chat_id
    task_id = add_task(chat_id, args_text, None)
    context.user_data["new_task_id"] = task_id
    logger.info(f"Created task {task_id} for user {update.effective_user.id}")

    await safe_send_message(
        update,
        "Задача создана. Укажите дедлайн в формате YYYY-MM-DD или YYYY-MM-DD HH:MM.\n"
        "Отправьте /cancel для отмены указания дедлайна."
    )
    return ASK_DEADLINE


async def ask_deadline(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if text.startswith("/"):
        # Any command cancels
        await safe_send_message(update, "Отмена указания дедлайна. Можно установить позже другой командой.")
        return ConversationHandler.END

    deadline_utc = parse_deadline_to_utc(text)
    if not deadline_utc:
        await safe_send_message(update, "Неверный формат. Пример: 2025-11-05 или 2025-11-05 18:30")
        return ASK_DEADLINE

    if deadline_utc <= datetime.utcnow():
        await safe_send_message(update, "Дедлайн должен быть в будущем. Попробуйте снова.")
        return ASK_DEADLINE

    task_id = context.user_data.get("new_task_id")
    if not task_id:
        await safe_send_message(update, "Не удалось найти задачу. Повторите /newtask.")
        return ConversationHandler.END

    set_task_deadline(task_id, deadline_utc)
    await schedule_reminder(context, update.message.chat_id, task_id, deadline_utc)
    logger.info(f"Deadline set for task {task_id} by user {update.effective_user.id}")

    await safe_send_message(update, "Дедлайн сохранён и напоминание запланировано (за 1 день).")
    context.user_data.pop("new_task_id", None)
    return ConversationHandler.END


async def alltasks(update: Update, context: CallbackContext) -> None:
    logger.info(f"User {update.effective_user.id} requested all tasks")
    tasks = get_tasks(update.message.chat_id, include_completed=False)
    if not tasks:
        await safe_send_message(update, "Активных задач нет.")
        return
    lines = ["ID | Приоритет | Дедлайн (UTC) | Текст"]
    for t in tasks:
        lines.append(format_task_for_list(t))
    text = "\n".join(lines)
    logger.info(f"Sending {len(tasks)} tasks to user {update.effective_user.id}")
    await safe_send_message(update, text)


async def completetask(update: Update, context: CallbackContext) -> None:
    logger.info(f"User {update.effective_user.id} completing task")
    parts = update.message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await safe_send_message(update, "Использование: /completetask {id}")
        return
    task_id = int(parts[1])
    ok = complete_task(update.message.chat_id, task_id)
    if ok:
        # If there was a scheduled job, remove it
        job_name = f"reminder_{update.message.chat_id}_{task_id}"
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        logger.info(f"Task {task_id} completed by user {update.effective_user.id}")
        await safe_send_message(update, "Задача завершена и убрана из списка.")
    else:
        logger.warning(f"Task {task_id} not found or already completed for user {update.effective_user.id}")
        await safe_send_message(update, "Задача не найдена или уже завершена.")


def build_checklist_lines(tasks: List[Task]) -> List[str]:
    return [f"[ ] {t.text}" for t in tasks]


async def todo(update: Update, context: CallbackContext) -> None:
    logger.info(f"User {update.effective_user.id} creating todo list")
    # Usage: /todo 1 2 3
    parts = update.message.text.split()
    if len(parts) < 2:
        await safe_send_message(update, "Использование: /todo {список id через пробел}")
        return

    ids: List[int] = []
    for p in parts[1:]:
        if p.isdigit():
            ids.append(int(p))

    if not ids:
        await safe_send_message(update, "Не указаны корректные id.")
        return

    selected: List[Task] = []
    for i in ids:
        t = get_task_by_id(update.message.chat_id, i)
        if t and not t.completed:
            selected.append(t)

    if not selected:
        await safe_send_message(update, "Задачи не найдены.")
        return

    lines = build_checklist_lines(selected)
    checklist = "\n".join(lines)

    # Trim to Telegram limit (safe_send_message will handle chunking)
    if len(checklist) > TELEGRAM_MSG_LIMIT:
        checklist = checklist[:TELEGRAM_MSG_LIMIT - 10] + "\n..."

    logger.info(f"Sending todo list with {len(selected)} tasks to user {update.effective_user.id}")
    await safe_send_message(update, checklist)


async def cancel(update: Update, context: CallbackContext) -> int:
    logger.info(f"User {update.effective_user.id} cancelled operation")
    await safe_send_message(update, "Отменено.")
    return ConversationHandler.END


async def help_(update: Update, context: CallbackContext) -> None:
    await start(update, context)


async def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a message to the user if possible."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    # Try to send error message to user if update is available
    if update and isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла ошибка при обработке запроса. Попробуйте позже."
            )
        except Exception:
            logger.error("Failed to send error message to user")


async def on_startup(app) -> None:
    logger.info("Bot starting up...")
    # Ensure DB exists
    ensure_db(DB_PATH)
    logger.info(f"Database initialized: {DB_PATH}")
    # Schedule pending reminders for all chats
    # Note: We do not have list of chats centrally; we schedule per task
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, chat_id, text, deadline_utc_iso, completed FROM tasks WHERE completed = 0"
        )
        rows = cursor.fetchall()
    reminder_count = 0
    for row in rows:
        t = Task(*row)
        await schedule_reminder(app.bot_data["context"], t.chat_id, t.id, t.deadline_dt)
        reminder_count += 1
    logger.info(f"Scheduled {reminder_count} reminders on startup")


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

    # --- News commands ---
    app.add_handler(CommandHandler("categories", news_categories))
    app.add_handler(CommandHandler("news", news_by_category))
    app.add_handler(CommandHandler("country", set_country))
    app.add_handler(CommandHandler("sources", list_sources))
    app.add_handler(CommandHandler("source", news_by_source))
    
    # --- Callback handlers for inline buttons ---
    # Handle category button clicks (callback_data starts with "news_category_")
    app.add_handler(CallbackQueryHandler(news_category_callback, pattern="^news_category_"))

    # Register error handler
    app.add_error_handler(error_handler)

    return app


def main() -> None:
    logger.info("Initializing bot...")
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан")
        raise RuntimeError("BOT_TOKEN не задан. Укажите его в .env или переменных окружения.")
    if not NEWSAPI_KEY:
        # Не фатально: позволяем запуск без новостей, но предупреждаем в консоли
        logger.warning("NEWSAPI_KEY не задан. Команды новостей будут недоступны.")
    logger.info("Building application...")
    app = build_application()
    logger.info("Bot is ready. Starting polling...")
    # Run polling (blocking call)
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Ошибка запуска: {exc}")


