from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import time

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Message, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cursor_runner import CursorRunner
from chat_history_store import ChatHistoryStore
from feature_registry import FeatureRegistry
from git_policy import GitDiffPolicy, GitHubRepoClient
from identity_policy import IdentityPolicy
from local_backend_runner import LocalBackendRunner
from models import TaskRequest
from rate_limiter import RateLimiter
from s3_store import S3AuditStore
from security_guard import SecurityGuard
from spec_flow import SpecFlow


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
FEATURES_HISTORY_PATH = BASE_DIR / "data" / "features_history.json"
CHAT_HISTORY_DIR = BASE_DIR / "data" / "chat_history"
CHAT_TEXT_LOG_DIR = BASE_DIR / "data" / "chat_logs"
CHAT_TEXT_LOG_DIR.mkdir(parents=True, exist_ok=True)

RESET_BUTTON_TEXT = "Сброс контекста"
MAX_REPLY_CHARS = int(os.getenv("MAX_REPLY_CHARS", "3500"))
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "https://api.proxyapi.ru/openai/v1")
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "8"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
AUTO_APPROVE_REQUESTS = os.getenv("AUTO_APPROVE_REQUESTS", "true").lower() == "true"
REPO_QA_MODEL = os.getenv("REPO_QA_MODEL", os.getenv("CASUAL_MODEL_NAME", "gpt-5.5"))
CHAT_MODE_MODEL = os.getenv("CHAT_MODE_MODEL", os.getenv("CASUAL_MODEL_NAME", "gpt-5.5"))
CHAT_ASSISTANT_MODEL = os.getenv("CHAT_ASSISTANT_MODEL", "gpt-4o-mini")
DEVELOPMENT_TRIGGER = os.getenv("DEVELOPMENT_TRIGGER", "изменение").strip().lower()
MAX_REPLY_WORDS = int(os.getenv("MAX_REPLY_WORDS", "30"))


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
LOCAL_BACKEND_RUNNER = LocalBackendRunner(BASE_DIR)
S3_STORE = S3AuditStore()
DIFF_POLICY = GitDiffPolicy()
GITHUB = GitHubRepoClient()
FEATURES = FeatureRegistry(FEATURES_HISTORY_PATH)
HISTORY = ChatHistoryStore(CHAT_HISTORY_DIR, retention_days=7)
RATE_LIMITER = RateLimiter(
    max_requests=RATE_LIMIT_MAX_REQUESTS,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)
CHAT_ASSISTANT_ID: Optional[str] = None
CURSOR_PR_DETECTED_GRACE_SECONDS = int(os.getenv("CURSOR_PR_DETECTED_GRACE_SECONDS", "120"))
CURSOR_STALE_SECONDS = int(os.getenv("CURSOR_STALE_SECONDS", "120"))


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


def _truncate_words(text: str, max_words: int) -> str:
    words = text.strip().split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def _chat_log_path(chat_id: int) -> Path:
    return CHAT_TEXT_LOG_DIR / f"chat_{chat_id}.txt"


def _append_chat_log(chat_id: int, who: str, text: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {who}: {text.strip()}\n"
    with _chat_log_path(chat_id).open("a", encoding="utf-8") as fh:
        fh.write(line)


def _read_chat_log_tail(chat_id: int, max_chars: int = 3000) -> str:
    path = _chat_log_path(chat_id)
    if not path.exists():
        return "(История в txt пока пустая)"
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return raw[-max_chars:]


def _get_or_create_chat_assistant_id() -> str:
    global CHAT_ASSISTANT_ID
    if CHAT_ASSISTANT_ID:
        return CHAT_ASSISTANT_ID

    storage = BASE_DIR / "data" / "chat_assistant_id.txt"
    if storage.exists():
        existing = storage.read_text(encoding="utf-8").strip()
        if existing:
            CHAT_ASSISTANT_ID = existing
            return CHAT_ASSISTANT_ID

    assistant = OPENAI_CLIENT.beta.assistants.create(
        name="Ежик",
        instructions=(
            "Ты Ёжик — инженер-архитектор по информационной безопасности и участник команды "
            "аналитиков ИБ. Отвечай на русском, дерзко и с подколами, можно маты. "
            "Не оскорбляй по защищенным признакам. Не перегибай в сторону ИБ: "
            "если вопрос общий технический/продуктовый, давай сбалансированный ответ, "
            "а ИБ-аспекты добавляй только где уместно. Учитывай контекст из текста лога. "
            "Максимум 30 слов в ответе."
        ),
        model=CHAT_ASSISTANT_MODEL,
    )
    CHAT_ASSISTANT_ID = assistant.id
    storage.write_text(CHAT_ASSISTANT_ID, encoding="utf-8")
    return CHAT_ASSISTANT_ID


async def safe_reply(update: Update, text: str, **kwargs) -> bool:
    if not update.message:
        return False
    text = _truncate_words(text, MAX_REPLY_WORDS)
    chunks = _split_text_for_telegram(text, MAX_REPLY_CHARS)
    for idx, chunk in enumerate(chunks, start=1):
        payload = f"[{idx}/{len(chunks)}]\n{chunk}" if len(chunks) > 1 else chunk
        for attempt in range(3):
            try:
                sent = await update.message.reply_text(payload, **kwargs)
                try:
                    _append_chat_log(update.effective_chat.id, "bot", payload)
                    HISTORY.append(
                        chat_id=update.effective_chat.id,
                        user_id=(sent.from_user.id if sent and sent.from_user else 0),
                        username=(sent.from_user.username if sent and sent.from_user else ""),
                        is_bot=True,
                        text=payload,
                    )
                except Exception:
                    logger.exception("Failed to persist bot message history")
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


def _augment_user_text(chat_id: int, user_text: str, session: ChatSession) -> str:
    weekly_context = HISTORY.render_context(chat_id=chat_id, limit=120, max_chars=7000)
    if not session.uploaded_paths:
        return f"{user_text}\n\nИстория переписки команды за неделю:\n{weekly_context}"
    context_blocks = [_read_file_excerpt(path) for path in session.uploaded_paths[-3:]]
    joined = "\n\n".join(context_blocks)
    return (
        f"{user_text}\n\n"
        f"Контекст из файлов:\n{joined}\n\n"
        f"История переписки команды за неделю:\n{weekly_context}"
    )


def _is_addressed_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.message
    if not chat or not message:
        return False
    if chat.type == "private":
        return True
    if chat.type not in {"group", "supergroup"}:
        return False

    reply = message.reply_to_message
    if reply and reply.from_user and context.bot and reply.from_user.id == context.bot.id:
        return True

    text = (message.text or message.caption or "").lower()
    bot_username = (context.bot.username or "").lower()
    return bool(bot_username and f"@{bot_username}" in text)


def _looks_like_repo_question(text: str) -> bool:
    lowered = text.strip().lower()
    if "?" in lowered:
        return True
    question_starts = (
        "какие",
        "что",
        "как",
        "где",
        "можно ли",
        "есть ли",
        "покажи",
        "расскажи",
    )
    action_starts = ("сделай", "добавь", "измени", "обнови", "реализуй", "исправь")
    return lowered.startswith(question_starts) and not lowered.startswith(action_starts)


def _repo_snapshot_text() -> str:
    candidate_paths = [
        BASE_DIR / "README.md",
        BASE_DIR / "requirements.txt",
        BASE_DIR / ".env.example",
        BASE_DIR / "src" / "bot.py",
        BASE_DIR / "src" / "cursor_runner.py",
        BASE_DIR / "src" / "git_policy.py",
    ]
    parts: List[str] = []
    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        parts.append(f"[{path.name}]\n{content[:2200]}")
    return "\n\n".join(parts) if parts else "Repo snapshot unavailable."


def _ask_assistant_with_log(chat_id: int, prompt: str) -> str:
    context_payload = (
        f"{prompt}\n\n"
        f"История фич:\n{FEATURES.summary_text(limit=20)}\n\n"
        f"Снимок репозитория:\n{_repo_snapshot_text()}\n\n"
        f"TXT лог (хвост):\n{_read_chat_log_tail(chat_id)}"
    )
    try:
        assistant_id = _get_or_create_chat_assistant_id()
        thread = OPENAI_CLIENT.beta.threads.create(
            messages=[{"role": "user", "content": context_payload}]
        )
        run = OPENAI_CLIENT.beta.threads.runs.create(thread_id=thread.id, assistant_id=assistant_id)
        while run.status in {"queued", "in_progress", "cancelling"}:
            time.sleep(0.8)
            run = OPENAI_CLIENT.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
        if run.status != "completed":
            raise RuntimeError(f"Assistant run status: {run.status}")

        messages = OPENAI_CLIENT.beta.threads.messages.list(thread_id=thread.id)
        for message in messages.data:
            if message.role != "assistant":
                continue
            chunks = []
            for item in message.content:
                if item.type == "text":
                    chunks.append(item.text.value)
            answer = "\n".join(chunks).strip()
            if answer:
                return _truncate_words(answer, MAX_REPLY_WORDS)
    except Exception:
        logger.exception("Assistant API failed, using fallback completions")

    fallback = OPENAI_CLIENT.chat.completions.create(
        model=CHAT_MODE_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты Ёжик — инженер-архитектор по ИБ и участник команды аналитиков ИБ. "
                    "Отвечай дерзко, с подколами и матом, но без дискриминации. "
                    "Не делай перекос только в ИБ: отвечай сбалансированно по задаче. "
                    "Максимум 30 слов."
                ),
            },
            {"role": "user", "content": context_payload},
        ],
    )
    text = (fallback.choices[0].message.content or "").strip()
    return _truncate_words(text or "Не смог ответить.", MAX_REPLY_WORDS)


def _extract_development_payload(text: str) -> str:
    lowered = text.strip().lower()
    if not lowered.startswith(DEVELOPMENT_TRIGGER):
        return ""
    payload = text.strip()[len(DEVELOPMENT_TRIGGER) :]
    return payload.lstrip(" :.-\n\t")


def _is_frontend_task(task: TaskRequest) -> bool:
    spec = task.spec
    spec_text = ""
    if spec:
        spec_text = " ".join(
            [spec.short_summary, spec.implementation_goal, *spec.acceptance_criteria]
        )
    joined = f"{task.original_text}\n{spec_text}".lower()
    frontend_hints = (
        "frontend",
        "фронт",
        "ui",
        "ux",
        "сайт",
        "страниц",
        "лендинг",
        "html",
        "css",
        "react",
        "vue",
        "angular",
        "next.js",
        "vite",
        "кнопк",
        "интерфейс",
    )
    return any(hint in joined for hint in frontend_hints)


def _answer_standard_chat(chat_id: int, user_text: str) -> str:
    mode_hint = "Вопрос по репозиторию" if _looks_like_repo_question(user_text) else "Обычная рабочая дискуссия"
    context_payload = (
        f"Режим: {mode_hint}.\n"
        f"Вопрос пользователя: {user_text}\n\n"
        f"История фич:\n{FEATURES.summary_text(limit=20)}\n\n"
        f"Снимок репозитория:\n{_repo_snapshot_text()}\n\n"
        f"TXT лог (хвост):\n{_read_chat_log_tail(chat_id)}"
    )
    response = OPENAI_CLIENT.chat.completions.create(
        model=CHAT_MODE_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты Ёжик — инженер-архитектор по ИБ и участник команды аналитиков ИБ. "
                    "Отвечай дерзко, с подколами и матом, но без дискриминации. "
                    "Не делай перекос только в ИБ: отвечай сбалансированно по задаче. "
                    "Максимум 30 слов."
                ),
            },
            {"role": "user", "content": context_payload},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    return _truncate_words(text or "Не смог ответить.", MAX_REPLY_WORDS)


async def _start_work_placeholder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    with_message: bool,
) -> Optional[Message]:
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        logger.exception("Failed to send typing action")
    if not with_message:
        return None
    try:
        return await update.message.reply_text("Работаю...")
    except Exception:
        logger.exception("Failed to send placeholder message")
        return None


async def _stop_work_placeholder(placeholder: Optional[Message]) -> None:
    if not placeholder:
        return
    try:
        await placeholder.delete()
    except Exception:
        return


async def start_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    keyboard = ReplyKeyboardMarkup([[RESET_BUTTON_TEXT]], resize_keyboard=True)
    await safe_reply(
        update,
        "Готово. Режим: командный оркестратор Telegram.\n"
        "Сначала проверяю безопасность, затем формирую короткое ТЗ и прошу подтверждение.\n"
        "После `да`: backend — локально, frontend — через Git/PR с автомержем и деплоем.\n"
        f"Для очистки состояния нажми '{RESET_BUTTON_TEXT}'.",
        reply_markup=keyboard,
    )


async def reset_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    reset_session(chat_id)
    await safe_reply(update, "Контекст очищен. Текущая задача сброшена.")


async def features_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    await safe_reply(update, FEATURES.summary_text(limit=30))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return
    if not _is_addressed_to_bot(update, context):
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
    execution_mode = "frontend_git" if _is_frontend_task(task) else "backend_local"
    S3_STORE.append_event(
        task.task_id,
        "run_start",
        {"task_id": task.task_id, "mode": execution_mode},
    )

    status_message = None
    try:
        status_message = await update.message.reply_text("Взял в работу")
    except Exception:
        logger.exception("Failed to create status message")

    async def _edit_status(text: str) -> None:
        if not status_message:
            return
        try:
            await status_message.edit_text(text)
        except Exception:
            logger.exception("Failed to edit status message")

    if execution_mode == "backend_local":
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, LOCAL_BACKEND_RUNNER.run, task)
        last_edit_ts = 0.0
        while not future.done():
            now = time.time()
            if now - last_edit_ts >= 60:
                await _edit_status("Взял в работу\nПишу код локально")
                last_edit_ts = now
            await asyncio.sleep(5)

        final_state = await future
        task.run_id = final_state.run_id
        task.phase = final_state.status
        task.status_message = final_state.message
        S3_STORE.save_task(task)
        S3_STORE.append_event(task.task_id, "backend_local_finished", final_state.raw)

        if final_state.status == "finished":
            FEATURES.update(
                task.task_id,
                status="backend_local_done",
                note=(final_state.message[:250] if final_state.message else "Backend применен локально"),
            )
            await _edit_status(
                "Закончил\n"
                "Режим: backend локально\n"
                f"{(final_state.message or 'Изменения применены.')[:500]}"
            )
            return

        FEATURES.update(task.task_id, status="failed", note=final_state.message[:250])
        await _edit_status(
            "Закончил с ошибкой\n"
            "Режим: backend локально\n"
            f"{(final_state.message or 'Не удалось применить изменения.')[:500]}"
        )
        return

    try:
        started = await asyncio.to_thread(CURSOR_RUNNER.create_run, task)
    except Exception as exc:
        logger.exception("Cursor run creation failed")
        task.phase = "failed"
        task.status_message = str(exc)
        FEATURES.update(task.task_id, status="failed", note=str(exc)[:350])
        S3_STORE.save_task(task)
        S3_STORE.append_event(task.task_id, "cursor_run_failed", {"error": str(exc)})
        await _edit_status(f"Ошибка запуска: {str(exc)[:350]}")
        return

    task.run_id = started.run_id
    task.phase = started.status
    S3_STORE.save_task(task)
    S3_STORE.append_event(task.task_id, "cursor_run_created", started.raw)

    if not started.run_id:
        await _edit_status("Ошибка: Cursor API не вернул run_id")
        task.phase = "failed"
        FEATURES.update(task.task_id, status="failed", note="missing_run_id")
        S3_STORE.save_task(task)
        return

    if not started.agent_id:
        await _edit_status("Ошибка: Cursor API не вернул agent_id")
        task.phase = "failed"
        FEATURES.update(task.task_id, status="failed", note="missing_agent_id")
        S3_STORE.save_task(task)
        return

    async def _poll_with_status() -> object:
        last_label = ""
        last_edit_ts = 0.0
        started_at = time.time()
        last_pr_probe_ts = 0.0
        detected_pr_number: Optional[int] = None
        detected_pr_url: Optional[str] = None
        final_state_local = None
        while True:
            state = await asyncio.to_thread(CURSOR_RUNNER.get_run, started.agent_id, started.run_id)
            final_state_local = state
            normalized = state.status.lower()
            if (not state.pr_number or not state.pr_url) and detected_pr_number:
                state.pr_number = detected_pr_number
                state.pr_url = detected_pr_url
            if normalized in {"creating", "queued"}:
                label = "Готовлю среду"
            elif normalized in {"running", "in_progress"}:
                label = "Пишу код"
            elif normalized in {"finished"}:
                label = "Пушу изменения в Git"
            elif normalized in {"failed", "cancelled"}:
                label = f"Ошибка выполнения: {normalized}"
            else:
                label = f"Статус: {normalized}"

            now = time.time()
            if label != last_label or (now - last_edit_ts) >= 60:
                await _edit_status(f"Взял в работу\n{label}")
                last_label = label
                last_edit_ts = now

            if (
                normalized in {"running", "in_progress"}
                and started.branch
                and GITHUB.enabled
                and (now - last_pr_probe_ts) >= 20
            ):
                detected_pr_number, detected_pr_url = await asyncio.to_thread(
                    GITHUB.find_open_pr_by_branch,
                    started.branch,
                )
                last_pr_probe_ts = now
                if detected_pr_number:
                    state.pr_number = detected_pr_number
                    state.pr_url = detected_pr_url
                    if (now - started_at) >= CURSOR_PR_DETECTED_GRACE_SECONDS:
                        state.status = "finished"
                        state.message = "Cursor run still running, but PR detected; proceeding with merge flow."
                        return state

            updated_at_raw = ""
            if isinstance(state.raw, dict):
                updated_at_raw = str(
                    state.raw.get("updatedAt", state.raw.get("updated_at", ""))
                ).strip()
            if normalized in {"running", "in_progress"} and updated_at_raw:
                try:
                    updated_dt = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
                    stale_for = now - updated_dt.astimezone(timezone.utc).timestamp()
                except Exception:
                    stale_for = 0.0
                if stale_for >= CURSOR_STALE_SECONDS and (state.pr_url or state.pr_number):
                    state.status = "finished"
                    state.message = (
                        "Cursor run stale, but PR already exists; proceeding with merge flow."
                    )
                    return state

            if normalized in {"finished", "failed", "cancelled"}:
                return final_state_local
            await asyncio.sleep(8)

    final_state = await _poll_with_status()
    task.phase = final_state.status
    task.pr_number = final_state.pr_number
    task.pr_url = final_state.pr_url
    task.status_message = final_state.message
    S3_STORE.save_task(task)
    S3_STORE.append_event(task.task_id, "cursor_run_finished", final_state.raw)

    if final_state.status != "finished":
        FEATURES.update(task.task_id, status=final_state.status, note=final_state.message[:250])
        await _edit_status(f"Закончил с ошибкой: {final_state.status}")
        return

    merge_note = "PR не найден в результате run."
    pr_number = final_state.pr_number
    pr_url = final_state.pr_url
    if not pr_number and started.branch and GITHUB.enabled:
        detected_pr_number, detected_pr_url = await asyncio.to_thread(
            GITHUB.find_open_pr_by_branch,
            started.branch,
        )
        pr_number = detected_pr_number
        pr_url = detected_pr_url

    if pr_number:
        task.pr_number = pr_number
        task.pr_url = pr_url
        S3_STORE.save_task(task)
        files = await asyncio.to_thread(GITHUB.get_pr_files, pr_number)
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
            if DIFF_POLICY.is_frontend_only(files):
                ok, message = await asyncio.to_thread(
                    GITHUB.merge_pr,
                    pr_number,
                    f"Auto-merge task {task.task_id}",
                )
                merge_note = message if ok else f"Автомерж не выполнен: {message}"
                S3_STORE.append_event(
                    task.task_id,
                    "merge_result",
                    {"ok": ok, "message": message, "pr_number": pr_number},
                )
                FEATURES.update(
                    task.task_id,
                    status=("merged" if ok else "merge_failed"),
                    pr_url=(pr_url or ""),
                    note=message[:250],
                )
            else:
                merge_note = "Backend-изменения: Git-автомерж отключен (Git только для фронта)"
                FEATURES.update(
                    task.task_id,
                    status="backend_local_pending",
                    pr_url=(pr_url or ""),
                    note=merge_note,
                )
                S3_STORE.append_event(
                    task.task_id,
                    "merge_skipped_backend",
                    {"pr_number": pr_number, "files": files},
                )

    await _edit_status(
        "Закончил\n"
        f"PR: {pr_url or 'n/a'}\n"
        f"{merge_note}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not IDENTITY.is_user_allowed(update):
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    try:
        actor_user_id_for_log = update.effective_user.id if update.effective_user else 0
        actor_username_for_log = update.effective_user.username if update.effective_user else ""
        _append_chat_log(chat_id, (actor_username_for_log or "user"), user_text)
        HISTORY.append(
            chat_id=chat_id,
            user_id=actor_user_id_for_log,
            username=(actor_username_for_log or ""),
            is_bot=False,
            text=user_text,
        )
    except Exception:
        logger.exception("Failed to persist user message history")

    if not _is_addressed_to_bot(update, context):
        return

    if user_text == RESET_BUTTON_TEXT:
        reset_session(chat_id)
        await safe_reply(update, "Сбросил состояние.")
        return

    actor_username = IDENTITY.actor_username(update)
    actor_user_id = IDENTITY.actor_user_id(update)
    if actor_user_id is None:
        return

    development_payload = _extract_development_payload(user_text)
    placeholder = await _start_work_placeholder(update, context, with_message=False)
    try:
        # Default mode: chat participation (discussion and Q&A).
        if not development_payload and not session.pending_task_id:
            try:
                answer = await asyncio.to_thread(_answer_standard_chat, chat_id, user_text)
            except Exception:
                logger.exception("Standard chat answer failed")
                answer = "Не смог ответить из-за временной ошибки. Попробуй еще раз."
            ok = await safe_reply(update, answer)
            if not ok:
                await safe_reply(update, "Сорян, не смог отправить ответ с первого раза. Напиши еще раз.")
            return

        # Continue pending clarification / confirmation flow
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
                if AUTO_APPROVE_REQUESTS:
                    task.phase = "approved"
                    session.pending_task_id = None
                    S3_STORE.save_task(task)
                    await safe_reply(
                        update,
                        "Коротко понял так:\n"
                        f"{spec.short_summary}\n\n"
                        "Взял в работу.",
                    )
                    await _run_cursor_task(task, update)
                else:
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

        # New development request flow
        if not development_payload:
            await safe_reply(
                update,
                f"Чтобы запустить изменения, начни сообщение с `{DEVELOPMENT_TRIGGER}`.\n"
                f"Пример: `{DEVELOPMENT_TRIGGER} добавь раздел quickstart в README`.",
                parse_mode="Markdown",
            )
            return

        rate_key = f"{chat_id}:{actor_user_id}"
        if not RATE_LIMITER.allow(rate_key):
            await safe_reply(
                update,
                "Слишком часто кидаешь запросы, притормози немного.\n"
                f"Лимит: {RATE_LIMIT_MAX_REQUESTS} запросов за {RATE_LIMIT_WINDOW_SECONDS} сек.",
            )
            return

        enriched_text = _augment_user_text(chat_id, development_payload, session)
        # Harm-check only the explicit change request, not whole weekly history.
        verdict = await asyncio.to_thread(SECURITY_GUARD.inspect, development_payload)
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
        FEATURES.create(
            task_id=task.task_id,
            feature=(task.original_text[:220].replace("\n", " ").strip()),
            proposed_by_username=actor_username,
            proposed_by_user_id=actor_user_id,
        )
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
        if AUTO_APPROVE_REQUESTS:
            task.phase = "approved"
            S3_STORE.save_task(task)
            await safe_reply(
                update,
                "Коротко понял так:\n"
                f"{spec.short_summary}\n\n"
                "Взял в работу.",
            )
            await _run_cursor_task(task, update)
        else:
            session.pending_task_id = task.task_id
            S3_STORE.save_task(task)
            await safe_reply(update, SPEC_FLOW.render_confirmation(spec))
    finally:
        await _stop_work_placeholder(placeholder)


def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN")
    proxyapi_token = os.getenv("PROXYAPI_TOKEN")
    if not telegram_token or not proxyapi_token:
        raise RuntimeError("Set TELEGRAM_TOKEN and PROXYAPI_TOKEN in .env")

    app = Application.builder().token(telegram_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("features", features_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Telegram Cursor orchestrator bot is running...")
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()

