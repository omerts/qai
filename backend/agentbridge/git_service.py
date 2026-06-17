"""Git operations for a session: branch / status / commit / push, plus GitHub PR creation.

The git layer is provider-agnostic (plain git via GitPython). Only PR creation is
provider-specific; v1 implements GitHub via its REST API. Parsing of the ``origin``
remote and PR creation are isolated in :meth:`GitService.create_pull_request` so adding
GitLab/Bitbucket later is a localized change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from git import GitCommandError, InvalidGitRepositoryError, Repo

from .protocol import FileChange


@dataclass
class PullRequest:
    url: str
    number: int | None = None


@dataclass
class GitHubRepo:
    owner: str
    name: str

    @property
    def api_path(self) -> str:
        return f"{self.owner}/{self.name}"


_GITHUB_REMOTE_RE = re.compile(
    r"""github\.com[:/]+(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"""
)


def parse_github_remote(url: str) -> GitHubRepo | None:
    """Extract owner/name from an https or ssh GitHub remote URL (None if not GitHub)."""
    match = _GITHUB_REMOTE_RE.search(url.strip())
    if not match:
        return None
    return GitHubRepo(owner=match.group("owner"), name=match.group("name"))


class GitError(RuntimeError):
    pass


class GitService:
    def __init__(self, workspace: Path) -> None:
        try:
            self.repo = Repo(workspace, search_parent_directories=True)
        except InvalidGitRepositoryError as exc:
            raise GitError(f"{workspace} is not inside a git repository") from exc

    # --------------------------------------------------------------------- #
    # Branch / status
    # --------------------------------------------------------------------- #

    def current_branch(self) -> str:
        if self.repo.head.is_detached:
            return self.repo.head.commit.hexsha[:8]
        return self.repo.active_branch.name

    def create_branch(self, name: str | None = None, base: str | None = None) -> str:
        """Create and check out a new branch. Returns the branch name actually used."""
        branch_name = self.sanitize_branch_name(name) if name else self.suggest_branch_name()
        # Avoid clobbering an existing branch by suffixing if needed.
        existing = {h.name for h in self.repo.heads}
        final = branch_name
        i = 2
        while final in existing:
            final = f"{branch_name}-{i}"
            i += 1

        try:
            if base:
                self.repo.git.checkout(base)
            new_branch = self.repo.create_head(final)
            new_branch.checkout()
        except GitCommandError as exc:
            raise GitError(f"Could not create branch '{final}': {exc}") from exc
        return final

    def status(self) -> list[FileChange]:
        """Working-tree changes as porcelain entries (staged + unstaged + untracked)."""
        raw = self.repo.git.status("--porcelain")
        changes: list[FileChange] = []
        for line in raw.splitlines():
            if not line:
                continue
            code = line[:2].strip() or line[:2]
            path = line[3:].strip()
            # Renames look like "old -> new"; keep the destination path.
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changes.append(FileChange(path=path, status=code))
        return changes

    # --------------------------------------------------------------------- #
    # Commit / push
    # --------------------------------------------------------------------- #

    def commit_all(self, message: str) -> str | None:
        """Stage everything and commit. Returns the commit sha, or None if nothing to commit."""
        self.repo.git.add("--all")
        # Anything staged relative to HEAD? (handles the initial-commit case too)
        staged = self.repo.index.diff("HEAD") if self.repo.head.is_valid() else self.repo.index.entries
        if not staged:
            return None
        try:
            commit = self.repo.index.commit(message)
        except Exception as exc:  # noqa: BLE001
            raise GitError(f"Commit failed: {exc}") from exc
        return commit.hexsha

    def has_uncommitted_changes(self) -> bool:
        return self.repo.is_dirty(untracked_files=True)

    def push(self, branch: str | None = None, remote: str = "origin") -> None:
        branch = branch or self.current_branch()
        try:
            self.repo.git.push("--set-upstream", remote, branch)
        except GitCommandError as exc:
            raise GitError(f"Push to {remote}/{branch} failed: {exc}") from exc

    # --------------------------------------------------------------------- #
    # PR creation (GitHub)
    # --------------------------------------------------------------------- #

    def github_repo(self, remote: str = "origin") -> GitHubRepo | None:
        try:
            url = self.repo.remote(remote).url
        except ValueError:
            return None
        return parse_github_remote(url)

    def default_base_branch(self) -> str:
        """Best-effort detection of the remote default branch (origin/HEAD), else 'main'."""
        try:
            ref = self.repo.git.symbolic_ref("refs/remotes/origin/HEAD")
            return ref.rsplit("/", 1)[-1]
        except GitCommandError:
            for candidate in ("main", "master"):
                if candidate in {h.name for h in self.repo.heads}:
                    return candidate
            return "main"

    def create_pull_request(
        self,
        *,
        title: str,
        head: str,
        base: str | None = None,
        body: str = "",
        token: str | None,
        remote: str = "origin",
    ) -> PullRequest:
        gh = self.github_repo(remote)
        if gh is None:
            raise GitError(
                f"Remote '{remote}' is not a GitHub repository; PR creation supports GitHub only in v1."
            )
        if not token:
            raise GitError(
                "No GitHub token available. Set GITHUB_TOKEN/GH_TOKEN or run `gh auth login`."
            )

        base = base or self.default_base_branch()
        resp = httpx.post(
            f"https://api.github.com/repos/{gh.api_path}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "head": head, "base": base, "body": body},
            timeout=30,
        )
        if resp.status_code >= 300:
            raise GitError(f"GitHub PR creation failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        return PullRequest(url=data["html_url"], number=data.get("number"))

    # --------------------------------------------------------------------- #
    # Helpers
    # --------------------------------------------------------------------- #

    @staticmethod
    def sanitize_branch_name(name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._/-]+", "-", name.strip().lower()).strip("-/")
        return slug or "agentbridge/session"

    def suggest_branch_name(self, title: str | None = None) -> str:
        base = self.sanitize_branch_name(title) if title else "agentbridge/session"
        if not base.startswith("agentbridge/"):
            base = f"agentbridge/{base}"
        suffix = self.repo.head.commit.hexsha[:4] if self.repo.head.is_valid() else "new"
        return f"{base}-{suffix}"
