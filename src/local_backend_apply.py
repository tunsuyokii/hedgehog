from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _build_prompt(task: dict) -> str:
    spec = task.get("spec") or {}
    criteria = spec.get("acceptance_criteria") or []
    criteria_text = "\n".join(f"- {item}" for item in criteria if str(item).strip())
    return (
        "Выполни локальные backend-изменения только в текущем проекте.\n"
        "Не трогай frontend/docs/public, не меняй .env и secrets.\n"
        "Сделай минимально достаточные правки по задаче.\n\n"
        f"Запрос: {task.get('original_text', '')}\n"
        f"Коротко: {spec.get('short_summary', '')}\n"
        f"Цель: {spec.get('implementation_goal', '')}\n"
        f"Критерии:\n{criteria_text}\n"
    )


def _git_status(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=30,
        check=False,
    )
    return (result.stdout or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True)
    args = parser.parse_args()

    task_file = Path(args.task_file).resolve()
    repo_root = Path(__file__).resolve().parent.parent
    if not task_file.exists():
        print(f"Task file not found: {task_file}")
        return 2

    task = json.loads(task_file.read_text(encoding="utf-8"))
    prompt = _build_prompt(task)

    before = _git_status(repo_root)
    timeout_s = int(os.getenv("LOCAL_BACKEND_TIMEOUT_SECONDS", "900"))

    # Cursor CLI is installed locally. We feed the task as stdin prompt.
    run = subprocess.run(
        ["cursor", "agent"],
        cwd=str(repo_root),
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout_s,
        check=False,
    )

    after = _git_status(repo_root)
    backend_changed = False
    if after and after != before:
        for line in after.splitlines():
            path = line[3:].strip() if len(line) > 3 else ""
            lowered = path.lower()
            if lowered.startswith(("frontend/", "docs/", "public/")):
                continue
            if lowered.endswith((".html", ".css")):
                continue
            backend_changed = True
            break

    if run.returncode == 0 and backend_changed:
        print("Backend changes applied locally.")
        return 0

    sys.stdout.write((run.stdout or "")[:1500])
    sys.stderr.write((run.stderr or "")[:1500])
    print("\nNo backend changes detected or Cursor agent failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
