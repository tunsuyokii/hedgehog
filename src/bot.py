from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cursor_runner import CursorRunner
from git_policy import GitDiffPolicy, GitHubRepoClient
from identity_policy import IdentityPolicy
from models import TaskRequest
from s3_store import S3AuditStore
from security_guard import SecurityGuard
from spec_flow import SpecFlow


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

RESET_BUTTON_TEXT = "Сброс контекста"
MAX_REPLY_CHARS = int(os.getenv("MAX_REPLY_CHARS", "3500"))
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "https://api.proxyapi.ru/openai/v1")


@dataclass
class ChatSession:
    uploaded_paths: List[Path] = field(default_factory=list)
    pending_task_id: Optional[str] = None


SESSIONS: Dict[int, ChatSession] = {}
TASKS: Dict[str, TaskRequest] = {}

OPENAI_CLIENT = OpenAI(api_key=os.getenv("PROXYAPI_TOKEN"), base_url=PROXY_BASE_URL)
IDENTITY = IdentityPolicy.from_env()
SECURITY_GUARD = SecurityGuard(OPENAI_CLIENT)
SPEC_FLOW = SpecFlow(OPENAI_CLIENT)
CURSOR_RUNNER = CursorRunner()
S3_STORE = S3AuditStore()
DIFF_POLICY = GitDiffPolicy()
GITHUB = GitHubRepoClient()


def get_session(chat_id: int) -> ChatSession:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = ChatSession()
    return SESSIONS[chat_id]


def reset_session(chat_id: int) -> None:
    session = get_session(chat_id)
    session.uploaded_paths.clear()
    session.pending_task_id = None


def _split_text_for_telegram(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current = text
    while len(current) > limit:
        split_at = current.rfind("\n", 0, limit)
        if split_at < int(limit * 0.6):
            split_at = current.rfind(" ", 0, limit)
        if split_at < int(limit * 0.6):
            split_at = limit
        chunk = current[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        current = current[split_at:].strip()
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:limit]]


async def safe_reply(update: Update, text: str, **kwargs) -> bool:
    if not update.message:
        return False
    chunks = _split_text_for_telegram(text, MAX_REPLY_CHARS)
    for idx, chunk in enumerate(chunks, start=1):
        payload = f"[{idx}/{len(chunks)}]\n{chunk}" if len(chunks) > 1 else chunk
        for attempt in range(3):
            try:
                await update.message.reply_text(payload, **kwargs)
                break
            except RetryAfter as exc:
                await asyncio.sleep(float(getattr(exc, "retry_after", 1.5)))
            except (TimedOut, NetworkError):
                await asyncio.sleep(1.0 + attempt)
            except Exception:
                logger.exception("Failed to send Telegram message")
                return False
        else:
            return False
    return True


def _is_confirmation(text: str) -> bool:
    normalized = text.strip().lower()
    positives = {"да", "yes", "ага", "ок", "окей", "подтверждаю", "go"}
    return normalized in positives or normalized.startswith("да ")


def _is_rejection(text: str) -> bool:
    normalized = text.strip().lower()
    negatives = {"нет", "no", "неа", "отмена", "cancel", "не подтверждаю"}
    return normalized in negatives or normalized.startswith("нет ")


def _read_file_excerpt(path: Path, max_chars: int = 1800) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return f"[{path.name}: бинарный или недоступный файл]"
    if not content:
        return f"[{path.name}: пустой файл]"
    return f"[{path.name}]\n{content[:max_chars]}"


def _augment_user_text(user_text: str, session: ChatSession) -> str:
    if not session.uploaded_paths:
        return user_text
    context_blocks = [_read_file_excerpt(path) for path in session.uploaded_paths[-3:]]
    joined = "\n\n".join(context_blocks)
    return f"{user_text}\n\nКонтекст из файлов:\n{joined}"


async def start_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    keyboard = ReplyKeyboardMarkup([[RESET_BUTTON_TEXT]], resize_keyboard=True)
    await safe_reply(
        update,
        "Готово. Режим: командный оркестратор Telegram -> Cursor.\n"
        "Сначала проверяю безопасность, затем формирую короткое ТЗ и прошу подтверждение.\n"
        "После `да` запускаю реализацию в Cursor Cloud и GitHub.\n"
        f"Для очистки состояния нажми '{RESET_BUTTON_TEXT}'.",
        reply_markup=keyboard,
    )


async def reset_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    reset_session(chat_id)
    await safe_reply(update, "Контекст очищен. Текущая задача сброшена.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    session = get_session(update.effective_chat.id)
    document = update.message.document
    tg_file = await context.bot.get_file(document.file_id)
    safe_name = document.file_name or f"doc_{document.file_id}"
    local_path = UPLOADS_DIR / f"{update.effective_chat.id}_{safe_name}"
    await tg_file.download_to_drive(custom_path=str(local_path))
    session.uploaded_paths.append(local_path)
    await safe_reply(update, f"Файл `{safe_name}` добавлен в контекст.", parse_mode="Markdown")


async def _run_cursor_task(task: TaskRequest, update: Update) -> None:
    task.phase = "running"
    S3_STORE.save_task(task)
    S3_STORE.append_event(task.task_id, "cursor_run_start", {"task_id": task.task_id})

    await safe_reply(
        update,
        "Принято. Запускаю реализацию в Cursor Cloud.\n"
        "Хост не трогаю, работаю только через GitHub/S3/API.",
    )

    try:
        started = await asyncio.to_thread(CURSOR_RUNNER.create_run, task)
    except Exception as exc:
        logger.exception("Cursor run creation failed")
        task.phase = "failed"
        task.status_message = str(exc)
        S3_STORE.save_task(task)
        S3_STORE.append_event(task.task_id, "cursor_run_failed", {"error": str(exc)})
        await safe_reply(update, "Не удалось стартовать Cursor run. Проверь CURSOR_API_KEY и endpoint.")
        return

    task.run_id = started.run_id
    task.phase = started.status
    S3_STORE.save_task(task)
    S3_STORE.append_event(task.task_id, "cursor_run_created", started.raw)

    if not started.run_id:
        await safe_reply(update, "Cursor API не вернул run_id. Останавливаюсь.")
        task.phase = "failed"
        S3_STORE.save_task(task)
        return

    final_state = await asyncio.to_thread(CURSOR_RUNNER.wait_until_finished, started.run_id)
    task.phase = final_state.status
    task.pr_number = final_state.pr_number
    task.pr_url = final_state.pr_url
    task.status_message = final_state.message
    S3_STORE.save_task(task)
    S3_STORE.append_event(task.task_id, "cursor_run_finished", final_state.raw)

    if final_state.status != "completed":
        await safe_reply(update, f"Cursor run завершился со статусом `{final_state.status}`.", parse_mode="Markdown")
        return

    merge_note = "PR не найден в результате run."
    if final_state.pr_number:
        files = await asyncio.to_thread(GITHUB.get_pr_files, final_state.pr_number)
        diff_result = DIFF_POLICY.validate_paths(files)
        if not diff_result.ok:
            merge_note = "Автомерж заблокирован политикой diff:\n" + "\n".join(
                f"- {issue}" for issue in diff_result.violations
            )
            S3_STORE.append_event(
                task.task_id,
                "merge_blocked",
                {"violations": diff_result.violations, "files": files},
            )
        else:
            ok, message = await asyncio.to_thread(
                GITHUB.merge_pr,
                final_state.pr_number,
                f"Auto-merge task {task.task_id}",
            )
            merge_note = message if ok else f"Автомерж не выполнен: {message}"
            S3_STORE.append_event(
                task.task_id,
                "merge_result",
                {"ok": ok, "message": message, "pr_number": final_state.pr_number},
            )

    await safe_reply(
        update,
        "Готово.\n"
        f"- Run: `{final_state.run_id}`\n"
        f"- PR: {final_state.pr_url or 'n/a'}\n"
        f"- Merge: {merge_note}\n"
        "- Для фронта деплой должен отработать через GitHub Pages workflow.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    if user_text == RESET_BUTTON_TEXT:
        reset_session(chat_id)
        await safe_reply(update, "Сбросил состояние.")
        return

    actor_username = IDENTITY.actor_username(update)
    actor_user_id = IDENTITY.actor_user_id(update)
    if actor_user_id is None:
        return

    # Continue pending confirmation flow
    if session.pending_task_id and session.pending_task_id in TASKS:
        task = TASKS[session.pending_task_id]
        if not IDENTITY.can_continue_task(task, update):
            await safe_reply(update, "Эту задачу может подтвердить только автор исходного запроса.")
            return

        if task.phase == "awaiting_clarification":
            combined = f"{task.original_text}\n\nУточнение от пользователя:\n{user_text}"
            spec = await asyncio.to_thread(SPEC_FLOW.build_spec, combined)
            task.original_text = combined
            task.spec = spec
            task.phase = "awaiting_confirmation"
            S3_STORE.save_task(task)
            await safe_reply(update, SPEC_FLOW.render_confirmation(spec))
            return

        if task.phase == "awaiting_confirmation":
            if _is_rejection(user_text):
                task.phase = "cancelled"
                session.pending_task_id = None
                S3_STORE.save_task(task)
                await safe_reply(update, "Окей, отменил. Сформулируй запрос заново.")
                return
            if not _is_confirmation(user_text):
                await safe_reply(update, "Нужен явный ответ `да` или `нет`.")
                return

            session.pending_task_id = None
            await _run_cursor_task(task, update)
            return

    # New request flow
    enriched_text = _augment_user_text(user_text, session)
    verdict = await asyncio.to_thread(SECURITY_GUARD.inspect, enriched_text)
    if not verdict.is_safe:
        await safe_reply(update, SECURITY_GUARD.build_block_reply(verdict))
        return

    task = TaskRequest.create(
        chat_id=chat_id,
        user_id=actor_user_id,
        username=actor_username,
        text=enriched_text,
    )
    TASKS[task.task_id] = task
    S3_STORE.save_task(task)
    S3_STORE.append_event(task.task_id, "task_created", {"username": actor_username, "chat_id": chat_id})

    spec = await asyncio.to_thread(SPEC_FLOW.build_spec, enriched_text)
    task.spec = spec
    if spec.needs_clarification and spec.clarification_question:
        task.phase = "awaiting_clarification"
        session.pending_task_id = task.task_id
        S3_STORE.save_task(task)
        await safe_reply(
            update,
            "Нужно уточнение перед запуском:\n"
            f"{spec.clarification_question}",
        )
        return

    task.phase = "awaiting_confirmation"
    session.pending_task_id = task.task_id
    S3_STORE.save_task(task)
    await safe_reply(update, SPEC_FLOW.render_confirmation(spec))


def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    proxyapi_token = os.getenv("PROXYAPI_TOKEN")
    if not telegram_token or not proxyapi_token:
        raise RuntimeError("Set TELEGRAM_TOKEN and PROXYAPI_TOKEN in .env")

    app = Application.builder().token(telegram_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Telegram Cursor orchestrator bot is running...")
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()

