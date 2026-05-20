from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class ChatMessage:
    ts: str
    chat_id: int
    user_id: int
    username: str
    is_bot: bool
    text: str


class ChatHistoryStore:
    def __init__(self, storage_dir: Path, retention_days: int = 7) -> None:
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days

    def _chat_path(self, chat_id: int) -> Path:
        return self.storage_dir / f"chat_{chat_id}.jsonl"

    def append(
        self,
        chat_id: int,
        user_id: int,
        username: str,
        is_bot: bool,
        text: str,
    ) -> None:
        item = ChatMessage(
            ts=_now().isoformat(),
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            is_bot=is_bot,
            text=text.strip(),
        )
        path = self._chat_path(chat_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
        self.prune(chat_id)

    def prune(self, chat_id: int) -> None:
        path = self._chat_path(chat_id)
        if not path.exists():
            return
        cutoff = _now() - timedelta(days=self.retention_days)
        kept: List[str] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    ts = _parse_ts(str(payload.get("ts", "")))
                    if ts >= cutoff:
                        kept.append(line)
                except Exception:
                    continue
        with path.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(kept))
            if kept:
                fh.write("\n")

    def recent(self, chat_id: int, limit: int = 120) -> List[ChatMessage]:
        path = self._chat_path(chat_id)
        if not path.exists():
            return []
        cutoff = _now() - timedelta(days=self.retention_days)
        items: List[ChatMessage] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    ts = _parse_ts(str(payload.get("ts", "")))
                    if ts < cutoff:
                        continue
                    items.append(
                        ChatMessage(
                            ts=str(payload.get("ts", "")),
                            chat_id=int(payload.get("chat_id", chat_id)),
                            user_id=int(payload.get("user_id", 0)),
                            username=str(payload.get("username", "")),
                            is_bot=bool(payload.get("is_bot", False)),
                            text=str(payload.get("text", "")),
                        )
                    )
                except Exception:
                    continue
        return items[-limit:]

    def render_context(self, chat_id: int, limit: int = 80, max_chars: int = 7000) -> str:
        rows = self.recent(chat_id, limit=limit)
        if not rows:
            return "История чата за неделю пока пустая."
        rendered = []
        for row in rows:
            role = "bot" if row.is_bot else "user"
            uname = f"@{row.username}" if row.username else "unknown"
            rendered.append(f"[{role}:{uname}] {row.text}")
        joined = "\n".join(rendered)
        return joined[-max_chars:]

