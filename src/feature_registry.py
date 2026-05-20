from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FeatureRecord:
    task_id: str
    feature: str
    proposed_by_username: str
    proposed_by_user_id: int
    created_at: str
    status: str
    pr_url: str = ""
    note: str = ""
    updated_at: str = ""


class FeatureRegistry:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, FeatureRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            for item in raw:
                if not isinstance(item, dict):
                    continue
                rec = FeatureRecord(
                    task_id=str(item.get("task_id", "")),
                    feature=str(item.get("feature", "")),
                    proposed_by_username=str(item.get("proposed_by_username", "")),
                    proposed_by_user_id=int(item.get("proposed_by_user_id", 0)),
                    created_at=str(item.get("created_at", _now_iso())),
                    status=str(item.get("status", "new")),
                    pr_url=str(item.get("pr_url", "")),
                    note=str(item.get("note", "")),
                    updated_at=str(item.get("updated_at", "")),
                )
                if rec.task_id:
                    self._items[rec.task_id] = rec
        except Exception:
            return

    def _save(self) -> None:
        serialized = [asdict(item) for item in self._items.values()]
        self.storage_path.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create(
        self,
        task_id: str,
        feature: str,
        proposed_by_username: str,
        proposed_by_user_id: int,
    ) -> None:
        self._items[task_id] = FeatureRecord(
            task_id=task_id,
            feature=feature.strip(),
            proposed_by_username=proposed_by_username,
            proposed_by_user_id=proposed_by_user_id,
            created_at=_now_iso(),
            status="proposed",
            updated_at=_now_iso(),
        )
        self._save()

    def update(
        self,
        task_id: str,
        status: str,
        pr_url: str = "",
        note: str = "",
    ) -> None:
        item = self._items.get(task_id)
        if not item:
            return
        item.status = status
        if pr_url:
            item.pr_url = pr_url
        if note:
            item.note = note
        item.updated_at = _now_iso()
        self._save()

    def latest(self, limit: int = 20) -> List[FeatureRecord]:
        items = sorted(
            self._items.values(),
            key=lambda x: x.updated_at or x.created_at,
            reverse=True,
        )
        return items[:limit]

    def summary_text(self, limit: int = 15) -> str:
        items = self.latest(limit=limit)
        if not items:
            return "История фич пока пустая."
        lines = []
        for rec in items:
            pr = f" PR: {rec.pr_url}" if rec.pr_url else ""
            lines.append(
                f"- [{rec.status}] {rec.feature} | автор: @{rec.proposed_by_username}{pr}"
            )
        return "\n".join(lines)

