from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Set

from telegram import Update

from models import TaskRequest


def _parse_csv_set(raw: str) -> Set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


@dataclass
class IdentityPolicy:
    allowed_usernames: Set[str]
    allowed_chat_ids: Set[int]

    @classmethod
    def from_env(cls) -> "IdentityPolicy":
        usernames = {
            u.lower().lstrip("@")
            for u in _parse_csv_set(os.getenv("ALLOWED_USERNAMES", ""))
        }
        single_username = os.getenv("ALLOWED_USERNAME", "").strip().lower().lstrip("@")
        if single_username:
            usernames.add(single_username)

        chat_ids_raw = _parse_csv_set(os.getenv("ALLOWED_CHAT_IDS", ""))
        chat_ids = set()
        for item in chat_ids_raw:
            try:
                chat_ids.add(int(item))
            except ValueError:
                continue
        return cls(allowed_usernames=usernames, allowed_chat_ids=chat_ids)

    def is_user_allowed(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return False

        if self.allowed_chat_ids and chat.id not in self.allowed_chat_ids:
            return False

        if not self.allowed_usernames:
            return True

        username = (user.username or "").strip().lower().lstrip("@")
        return username in self.allowed_usernames

    @staticmethod
    def actor_username(update: Update) -> str:
        user = update.effective_user
        if not user:
            return ""
        return (user.username or "").strip().lower().lstrip("@")

    @staticmethod
    def actor_user_id(update: Update) -> Optional[int]:
        user = update.effective_user
        return user.id if user else None

    def can_continue_task(self, task: TaskRequest, update: Update) -> bool:
        user_id = self.actor_user_id(update)
        username = self.actor_username(update)
        if user_id is None:
            return False
        return task.user_id == user_id and task.username == username

