from __future__ import annotations

import json
import os
import re
from typing import Any, Dict

from openai import OpenAI

from models import SecurityVerdict


class SecurityGuard:
    def __init__(self, client: OpenAI) -> None:
        self.client = client
        self.model = os.getenv("SECURITY_MODEL_NAME", os.getenv("CASUAL_MODEL_NAME", "gpt-5.5"))
        self.profane_mode = os.getenv("SECURITY_PROFANE_MODE", "true").lower() == "true"
        self._hard_block_patterns = [
            r"кажд\w*\s+секунд\w*.*(чат|сообщен|telegram|телеграм)",
            r"(спам|флуд).*(чат|сообщен|telegram|телеграм)",
            r"заср\w*\s+чат",
            r"(бот|телеграм).*(спам|флуд)",
            r"саботаж.*(команд|чат|бот|релиз|деплой)",
        ]

    def _hard_block(self, user_text: str) -> SecurityVerdict | None:
        lowered = user_text.lower()
        for pattern in self._hard_block_patterns:
            if re.search(pattern, lowered):
                return SecurityVerdict(
                    is_safe=False,
                    category="team_harm_hard_block",
                    reason=f"Matched hard-block pattern: {pattern}",
                    confidence=0.99,
                )
        # Repeated-message abuse pattern
        if re.search(
            r"(отправляй|пиши|шли)\s+.*\s+(кажд\w*|раз в)\s+.*(чат|сообщен|telegram|телеграм)",
            lowered,
        ):
            return SecurityVerdict(
                is_safe=False,
                category="team_harm_hard_block",
                reason="Automated repetitive posting request to chat",
                confidence=0.98,
            )
        return None

    def inspect(self, user_text: str) -> SecurityVerdict:
        hard_block = self._hard_block(user_text)
        if hard_block:
            return hard_block

        prompt = (
            "Ты security-модератор для входящих задач в инженерного Telegram-бота.\n"
            "Твоя задача: оценить только ВРЕД ДЛЯ КОМАНДЫ/ЧАТА, а не общий вред.\n"
            "Блокировать нужно только если запрос:\n"
            "- запускает спам/флуд в Telegram,\n"
            "- предлагает саботаж командной работы или деградацию чата,\n"
            "- предлагает автоматизированный токсичный постинг в чат.\n"
            "Не блокируй нормальные продуктовые/технические задачи разработки.\n"
            "Верни только JSON с полями:\n"
            "is_safe (bool), category (string), reason (string), confidence (0..1).\n"
            "Если есть признаки опасности, is_safe=false."
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_text},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
            data: Dict[str, Any] = json.loads(raw)
            return SecurityVerdict(
                is_safe=bool(data.get("is_safe", False)),
                category=str(data.get("category", "unknown")),
                reason=str(data.get("reason", "No reason provided")),
                confidence=float(data.get("confidence", 0.0)),
            )
        except Exception:
            lowered = user_text.lower()
            indicators = (
                "каждую секунду",
                "спам",
                "флуд",
                "засри чат",
                "саботаж",
            )
            unsafe = any(token in lowered for token in indicators)
            return SecurityVerdict(
                is_safe=not unsafe,
                category=("team_harm_heuristic" if unsafe else "heuristic_fallback"),
                reason=("Possible team-harm request detected by heuristic" if unsafe else "Heuristic fallback moderation"),
                confidence=0.4,
            )

    def build_block_reply(self, verdict: SecurityVerdict) -> str:
        if self.profane_mode:
            if "team_harm" in verdict.category or "spam" in verdict.category:
                return (
                    "Ты сейчас предлагаешь хуйню, которая может навредить команде и засрать чат.\n"
                    "Такое блокирую жестко и без обсуждений.\n"
                    f"Причина: {verdict.category} — {verdict.reason}"
                )
            return (
                "Стоп, дружище, тут явная попытка инъекции/эксплуатации.\n"
                "Не прокатит: я такую хрень режу на входе, без вариантов.\n"
                f"Причина: {verdict.category} — {verdict.reason}"
            )
        return (
            "Запрос заблокирован системой безопасности: обнаружены признаки prompt injection/эксплуатации.\n"
            f"Причина: {verdict.category} — {verdict.reason}"
        )

