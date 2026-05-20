from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

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
        self.frontend_patterns = _parse_patterns(
            os.getenv("GIT_FRONTEND_PATHS", ""),
            "frontend/**,docs/**,public/**,*.html,*.css,*.js,*.ts,*.tsx",
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

    def is_frontend_only(self, files: List[str]) -> bool:
        if not files:
            return False
        for path in files:
            if not any(fnmatch.fnmatch(path, pattern) for pattern in self.frontend_patterns):
                return False
        return True


class GitHubRepoClient:
    def __init__(self) -> None:
        self.repo = os.getenv("GITHUB_REPO", "tunsuyokii/hedgehog")
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.api_base = "https://api.github.com"
        self.owner = self.repo.split("/")[0] if "/" in self.repo else ""

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

    def find_open_pr_by_branch(self, branch_name: str) -> Tuple[int | None, str | None]:
        if not self.enabled or not self.owner or not branch_name:
            return None, None
        head = f"{self.owner}:{branch_name}"
        url = f"{self.api_base}/repos/{self.repo}/pulls"
        params = {"state": "open", "head": head}
        with httpx.Client(timeout=45) as client:
            response = client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            items = response.json()
        if not items:
            return None, None
        pr = items[0]
        pr_number = int(pr["number"])
        pr_url = str(pr.get("html_url", ""))
        return pr_number, pr_url

    def merge_pr(self, pr_number: int, commit_title: str) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "GITHUB_TOKEN is missing"
        ready_ok, ready_message = self.ensure_pr_ready_for_merge(pr_number)
        if not ready_ok:
            return False, ready_message
        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/merge"
        payload = {"merge_method": "squash", "commit_title": commit_title}
        with httpx.Client(timeout=45) as client:
            response = client.put(url, headers=self._headers(), json=payload)
        if response.status_code in {200, 201}:
            return True, "Merged"
        return False, f"Merge failed: {response.status_code} {response.text}"

    def ensure_pr_ready_for_merge(self, pr_number: int) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "GITHUB_TOKEN is missing"
        url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}"
        with httpx.Client(timeout=45) as client:
            response = client.get(url, headers=self._headers())
            response.raise_for_status()
            pr_data = response.json()
        if not bool(pr_data.get("draft", False)):
            return True, "PR already ready"

        node_id = str(pr_data.get("node_id", "")).strip()
        if not node_id:
            return False, "PR is draft and has no node_id to mark ready"
        mutation = (
            "mutation($id: ID!) {"
            "  markPullRequestReadyForReview(input: { pullRequestId: $id }) {"
            "    pullRequest { isDraft }"
            "  }"
            "}"
        )
        payload: Dict[str, Any] = {
            "query": mutation,
            "variables": {"id": node_id},
        }
        graph_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=45) as client:
            graph_response = client.post(
                f"{self.api_base}/graphql",
                headers=graph_headers,
                json=payload,
            )
        if graph_response.is_error:
            return (
                False,
                f"Failed to mark PR ready: {graph_response.status_code} {graph_response.text}",
            )
        graph_data = graph_response.json()
        if graph_data.get("errors"):
            return False, f"Failed to mark PR ready: {graph_data['errors']}"
        return True, "PR moved from draft to ready"

