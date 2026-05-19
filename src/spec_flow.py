from __future__ import annotations

import json
import os
from typing import Dict, List

from openai import OpenAI

from models import TaskSpec


class SpecFlow:
    def __init__(self, client: OpenAI) -> None:
        self.client = client
        self.model = os.getenv("SPEC_MODEL_NAME", os.getenv("CASUAL_MODEL_NAME", "gpt-5.5"))

    def build_spec(self, user_text: str) -> TaskSpec:
        system_prompt = (
            "Ты продуктовый аналитик по ИБ-задачам. На основе входного текста собери короткое ТЗ.\n"
            "Верни строго JSON:\n"
            "{"
            '"short_summary": string, '
            '"implementation_goal": string, '
            '"acceptance_criteria": [string], '
            '"needs_clarification": bool, '
            '"clarification_question": string'
            "}\n"
            "short_summary: максимум 180 символов. "
            "acceptance_criteria: 2-5 пунктов, конкретно и проверяемо."
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
            data: Dict[str, object] = json.loads(raw)
            criteria = data.get("acceptance_criteria", [])
            if not isinstance(criteria, list):
                criteria = []
            return TaskSpec(
                short_summary=str(data.get("short_summary", "")).strip()[:180],
                implementation_goal=str(data.get("implementation_goal", "")).strip(),
                acceptance_criteria=[str(item).strip() for item in criteria if str(item).strip()],
                needs_clarification=bool(data.get("needs_clarification", False)),
                clarification_question=str(data.get("clarification_question", "")).strip(),
            )
        except Exception:
            return self._fallback_spec(user_text)

    @staticmethod
    def _fallback_spec(user_text: str) -> TaskSpec:
        summary = user_text.strip().replace("\n", " ")
        if len(summary) > 180:
            summary = summary[:177] + "..."
        return TaskSpec(
            short_summary=summary or "Сформулировать и реализовать пользовательский запрос",
            implementation_goal="Реализовать изменение в репозитории через безопасный Git workflow",
            acceptance_criteria=[
                "Изменения оформлены через GitHub PR",
                "Хост не затронут, только Git/S3/API операции",
                "Есть понятный итог для пользователя",
            ],
            needs_clarification=False,
            clarification_question="",
        )

    @staticmethod
    def render_confirmation(spec: TaskSpec) -> str:
        criteria = "\n".join(f"- {item}" for item in spec.acceptance_criteria[:5])
        return (
            f"Коротко: {spec.short_summary}\n\n"
            "Предлагаемая цель:\n"
            f"{spec.implementation_goal}\n\n"
            "Критерии приемки:\n"
            f"{criteria}\n\n"
            "Это то, что ты хотел? Ответь `да` или `нет`."
        )

    @staticmethod
    def is_positive_confirmation(text: str) -> bool:
        normalized = text.strip().lower()
        positives: List[str] = ["да", "yes", "ага", "ок", "окей", "подтверждаю", "go"]
        return normalized in positives or normalized.startswith("да ")

