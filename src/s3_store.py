from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.client import BaseClient

from models import TaskRequest


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class S3AuditStore:
    def __init__(self) -> None:
        self.bucket = os.getenv("S3_BUCKET", "")
        self.prefix = os.getenv("S3_PREFIX", "telegram-cursor")
        endpoint = os.getenv("S3_ENDPOINT", "")
        access_key = os.getenv("S3_ACCESS_KEY", "")
        secret_key = os.getenv("S3_SECRET_KEY", "")

        self.enabled = bool(self.bucket and endpoint and access_key and secret_key)
        self._client: Optional[BaseClient] = None
        if self.enabled:
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )

    def _put_json(self, key: str, payload: Dict[str, Any]) -> None:
        if not self.enabled or not self._client:
            return
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

    def save_task(self, task: TaskRequest) -> None:
        key = f"{self.prefix}/tasks/{task.task_id}/task.json"
        self._put_json(key, task.to_dict())

    def append_event(self, task_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        key = f"{self.prefix}/tasks/{task_id}/events/{_utc_stamp()}_{event_type}.json"
        self._put_json(
            key,
            {
                "event_type": event_type,
                "payload": payload,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

