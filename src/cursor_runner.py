from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import httpx

from models import CursorRunState, TaskRequest


class CursorRunner:
    def __init__(self) -> None:
        self.api_key = os.getenv("CURSOR_API_KEY", "")
        self.base_url = os.getenv("CURSOR_API_BASE", "https://api.cursor.com/v1")
        self.model = os.getenv("CURSOR_AGENT_MODEL", "gpt-5.5-medium")
        self.timeout_s = float(os.getenv("CURSOR_HTTP_TIMEOUT_SECONDS", "45"))
        self.poll_interval_s = float(os.getenv("CURSOR_POLL_INTERVAL_SECONDS", "8"))
        self.max_poll_minutes = int(os.getenv("CURSOR_MAX_POLL_MINUTES", "30"))
        self.github_repo = os.getenv("GITHUB_REPO", "tunsuyokii/hedgehog")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _guardrails_text(self) -> str:
        return (
            "Жесткие ограничения выполнения:\n"
            "1) Никаких изменений на текущем хосте/сервере.\n"
            "2) Любые изменения только через GitHub репозиторий.\n"
            "3) Работать только в рамках репозитория tunsuyokii/hedgehog.\n"
            "4) Если затронут фронт, деплой только через GitHub Pages workflow.\n"
            "5) Не выполнять операционные команды на прод-хосте.\n"
        )

    def _build_prompt(self, task: TaskRequest) -> str:
        spec = task.spec
        criteria = "\n".join(f"- {item}" for item in (spec.acceptance_criteria if spec else []))
        return (
            "Реализуй задачу через git workflow.\n\n"
            f"Запрос пользователя: {task.original_text}\n\n"
            f"Короткий scope: {spec.short_summary if spec else ''}\n"
            f"Цель: {spec.implementation_goal if spec else ''}\n"
            f"Критерии приемки:\n{criteria}\n\n"
            f"{self._guardrails_text()}"
        )

    def create_run(self, task: TaskRequest) -> CursorRunState:
        if not self.enabled:
            return CursorRunState(
                run_id="",
                status="failed",
                message="CURSOR_API_KEY is missing",
            )

        payload = {
            "model": self.model,
            "repo": self.github_repo,
            "task": self._build_prompt(task),
            "metadata": {
                "task_id": task.task_id,
                "chat_id": str(task.chat_id),
                "user_id": str(task.user_id),
                "username": task.username,
            },
        }
        url = f"{self.base_url}/agents/runs"
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        run_id = str(data.get("id", ""))
        return CursorRunState(
            run_id=run_id,
            status=str(data.get("status", "queued")),
            message=str(data.get("message", "")),
            raw=data,
        )

    def get_run(self, run_id: str) -> CursorRunState:
        url = f"{self.base_url}/agents/runs/{run_id}"
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.get(url, headers=self._headers())
            response.raise_for_status()
            data = response.json()

        return CursorRunState(
            run_id=run_id,
            status=str(data.get("status", "unknown")),
            message=str(data.get("message", "")),
            branch=data.get("branch"),
            pr_number=(int(data["pr_number"]) if data.get("pr_number") is not None else None),
            pr_url=data.get("pr_url"),
            raw=data,
        )

    def wait_until_finished(self, run_id: str) -> CursorRunState:
        deadline = time.time() + self.max_poll_minutes * 60
        last_state = CursorRunState(run_id=run_id, status="queued")
        while time.time() < deadline:
            state = self.get_run(run_id)
            last_state = state
            if state.status in {"completed", "failed", "cancelled"}:
                return state
            time.sleep(self.poll_interval_s)
        return CursorRunState(
            run_id=run_id,
            status="failed",
            message="Cursor run timed out",
            raw=last_state.raw,
        )

