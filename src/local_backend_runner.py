from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from models import CursorRunState, TaskRequest


class LocalBackendRunner:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.command = os.getenv("LOCAL_BACKEND_APPLY_COMMAND", "").strip()
        self.timeout_seconds = int(os.getenv("LOCAL_BACKEND_TIMEOUT_SECONDS", "1800"))
        self.tasks_dir = self.base_dir / "data" / "local_backend_tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def run(self, task: TaskRequest) -> CursorRunState:
        run_id = f"local-{task.task_id}"
        task_file = self.tasks_dir / f"{task.task_id}.json"
        payload = {
            "task_id": task.task_id,
            "chat_id": task.chat_id,
            "user_id": task.user_id,
            "username": task.username,
            "original_text": task.original_text,
            "spec": (task.spec.__dict__ if task.spec else None),
        }
        task_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if not self.enabled:
            return CursorRunState(
                run_id=run_id,
                status="failed",
                message=(
                    "LOCAL_BACKEND_APPLY_COMMAND не задан. "
                    f"Задача сохранена в {task_file}."
                ),
                raw={"task_file": str(task_file)},
            )

        prepared = self.command.format(task_file=str(task_file))
        args = shlex.split(prepared, posix=False)
        if "{task_file}" not in self.command:
            args.append(str(task_file))

        process = subprocess.run(
            args,
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            encoding="utf-8",
            errors="ignore",
        )

        stdout = (process.stdout or "").strip()
        stderr = (process.stderr or "").strip()
        details = "\n".join(part for part in [stdout, stderr] if part).strip()
        if process.returncode == 0:
            return CursorRunState(
                run_id=run_id,
                status="finished",
                message=(details[:500] if details else "Локальный backend-run завершен."),
                raw={
                    "task_file": str(task_file),
                    "returncode": process.returncode,
                    "stdout": stdout[:2000],
                    "stderr": stderr[:2000],
                },
            )

        return CursorRunState(
            run_id=run_id,
            status="failed",
            message=(details[:500] if details else f"Команда завершилась с кодом {process.returncode}."),
            raw={
                "task_file": str(task_file),
                "returncode": process.returncode,
                "stdout": stdout[:2000],
                "stderr": stderr[:2000],
            },
        )
