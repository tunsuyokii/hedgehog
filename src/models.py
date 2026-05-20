from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SecurityVerdict:
    is_safe: bool
    category: str
    reason: str
    confidence: float = 0.0


@dataclass
class TaskSpec:
    short_summary: str
    implementation_goal: str
    acceptance_criteria: List[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str = ""


@dataclass
class TaskRequest:
    task_id: str
    chat_id: int
    user_id: int
    username: str
    original_text: str
    created_at: str
    phase: str
    spec: Optional[TaskSpec] = None
    run_id: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    status_message: str = ""

    @classmethod
    def create(cls, chat_id: int, user_id: int, username: str, text: str) -> "TaskRequest":
        return cls(
            task_id=str(uuid4()),
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            original_text=text,
            created_at=utc_now_iso(),
            phase="new",
        )

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        return result


@dataclass
class CursorRunState:
    run_id: str
    status: str
    message: str = ""
    agent_id: Optional[str] = None
    branch: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

