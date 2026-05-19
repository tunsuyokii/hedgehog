from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import httpx


def _parse_patterns(raw: str, fallback: str) -> List[str]:
    source = raw or fallback
    return [item.strip() for item in source.split(",") if item.strip()]


@dataclass
class DiffPolicyResult:
    ok: bool
    violations: List[str] = field(default_factory=list)


class GitDiffPolicy:
    def __init__(self) -> None:
        self.allowed_patterns = _parse_patterns(
            os.getenv("GIT_ALLOWED_PATHS", ""),
            "src/**,.github/workflows/**,README.md,requirements.txt",
        )
        self.blocked_patterns = _parse_patterns(
            os.getenv("GIT_BLOCKED_PATHS", ""),
            ".env,**/.env,**/secrets/**,**/*.pem,**/*.key",
        )

    def validate_paths(self, files: List[str]) -> DiffPolicyResult:
        violations: List[str] = []
        for path in files:
            if any(fnmatch.fnmatch(path, pattern) for pattern in self.blocked_patterns):
                violations.append(f"Blocked path pattern match: {path}")
                continue
            if not any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed_patterns):
                violations.append(f"Path is outside allowlist: {path}")
        return DiffPolicyResult(ok=not violations, violations=violations)


class GitHubRepoClient:
    def __init__(self) -> None:
        self.repo = os.getenv("GITHUB_REPO", "tunsuyokii/hedgehog")
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.api_base = "https://api.github.com"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_pr_files(self, pr_number: int) -> List[str]:
        if not self.enabled:
            return []
        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/files"
        with httpx.Client(timeout=45) as client:
            response = client.get(url, headers=self._headers())
            response.raise_for_status()
            data = response.json()
        return [str(item.get("filename", "")) for item in data if item.get("filename")]

    def merge_pr(self, pr_number: int, commit_title: str) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "GITHUB_TOKEN is missing"
        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/merge"
        payload = {"merge_method": "squash", "commit_title": commit_title}
        with httpx.Client(timeout=45) as client:
            response = client.put(url, headers=self._headers(), json=payload)
        if response.status_code in {200, 201}:
            return True, "Merged"
        return False, f"Merge failed: {response.status_code} {response.text}"

