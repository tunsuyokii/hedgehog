from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional

import httpx

from models import CursorRunState, TaskRequest


class CursorRunner:
    def __init__(self) -> None:
        self.api_key = os.getenv("CURSOR_API_KEY", "")
        self.base_url = os.getenv("CURSOR_API_BASE", "https://api.cursor.com/v1")
        self.model = os.getenv("CURSOR_AGENT_MODEL", "").strip()
        self.timeout_s = float(os.getenv("CURSOR_HTTP_TIMEOUT_SECONDS", "45"))
        self.poll_interval_s = float(os.getenv("CURSOR_POLL_INTERVAL_SECONDS", "8"))
        self.max_poll_minutes = int(os.getenv("CURSOR_MAX_POLL_MINUTES", "30"))
        self.github_repo = os.getenv("GITHUB_REPO", "tunsuyokii/hedgehog")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
        }

    def _auth(self) -> tuple[str, str]:
        # Cursor Cloud Agents API uses Basic auth: API_KEY as username, empty password.
        return (self.api_key, "")

    def _guardrails_text(self) -> str:
        return (
            "Жесткие ограничения выполнения:\n"
            "1) Никаких изменений на текущем хосте/сервере.\n"
            "2) Backend-контур работает локально; Git-поток использовать только для фронта.\n"
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

        payload: Dict[str, Any] = {
            "prompt": {"text": self._build_prompt(task)},
            "repos": [
                {
                    "url": f"https://github.com/{self.github_repo}",
                    "startingRef": "main",
                }
            ],
            "autoCreatePR": True,
        }
        # Cursor Cloud Agents expects model IDs from GET /v1/models.
        # If env contains an incompatible provider model slug, omit model and let Cursor default.
        if self.model and "/" not in self.model and not self.model.startswith("gpt-"):
            payload["model"] = {"id": self.model}

        url = f"{self.base_url}/agents"
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(url, headers=self._headers(), json=payload, auth=self._auth())
            if response.is_error:
                raise RuntimeError(
                    f"Cursor create_run failed: {response.status_code} {response.text[:500]}"
                )
            data = response.json()
        run = data.get("run", {}) if isinstance(data, dict) else {}
        agent = data.get("agent", {}) if isinstance(data, dict) else {}
        git = data.get("git", {}) if isinstance(data, dict) else {}
        branches = git.get("branches", []) if isinstance(git, dict) else []
        first_branch = branches[0] if isinstance(branches, list) and branches else {}
        run_id = str(run.get("id", ""))
        return CursorRunState(
            run_id=run_id,
            status=str(run.get("status", "CREATING")).lower(),
            message="",
            agent_id=str(agent.get("id", "")) or None,
            branch=(agent.get("branchName") or first_branch.get("branch")),
            raw=data,
        )

    def get_run(self, agent_id: str, run_id: str) -> CursorRunState:
        url = f"{self.base_url}/agents/{agent_id}/runs/{run_id}"
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.get(url, headers=self._headers(), auth=self._auth())
            response.raise_for_status()
            data = response.json()

        git = data.get("git", {}) if isinstance(data, dict) else {}
        branches = git.get("branches", []) if isinstance(git, dict) else []
        first_branch = branches[0] if isinstance(branches, list) and branches else {}
        pr_url = data.get("pr_url") or first_branch.get("prUrl") or first_branch.get("pr_url")
        pr_number_raw = data.get("pr_number")
        if pr_number_raw is None and isinstance(pr_url, str):
            match = re.search(r"/pull/(\d+)", pr_url)
            if match:
                pr_number_raw = int(match.group(1))
        return CursorRunState(
            run_id=run_id,
            status=str(data.get("status", "unknown")).lower(),
            message=str(data.get("message", "")),
            agent_id=agent_id,
            branch=first_branch.get("branch"),
            pr_number=(int(pr_number_raw) if pr_number_raw is not None else None),
            pr_url=pr_url,
            raw=data,
        )

    def wait_until_finished(self, agent_id: str, run_id: str) -> CursorRunState:
        deadline = time.time() + self.max_poll_minutes * 60
        last_state = CursorRunState(run_id=run_id, status="queued", agent_id=agent_id)
        while time.time() < deadline:
            state = self.get_run(agent_id, run_id)
            last_state = state
            if state.status in {"finished", "failed", "cancelled"}:
                return state
            time.sleep(self.poll_interval_s)
        return CursorRunState(
            run_id=run_id,
            status="failed",
            message="Cursor run timed out",
            agent_id=agent_id,
            raw=last_state.raw,
        )

