"""Git operations for a session: branch / status / commit / push, plus GitHub PR creation.

The git layer is provider-agnostic (plain git via GitPython). Only PR creation is
provider-specific; v1 implements GitHub via its REST API. Parsing of the ``origin``
remote and PR creation are isolated in :meth:`GitService.create_pull_request` so adding
GitLab/Bitbucket later is a localized change.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from git import GitCommandError, InvalidGitRepositoryError, Repo

from .protocol import FileChange


def _ensure_git_safe_directory(path: Path) -> None:
    """Mark ``path`` as a safe Git directory.

    Git 2.35.2+ refuses to operate on a repository owned by a different user than the running
    process ("fatal: detected dubious ownership"), which routinely happens with Docker bind
    mounts (host-owned files, container process is root). Adding the path to the global
    ``safe.directory`` list clears that. Idempotent and best-effort — never let this break
    the app.
    """
    target = str(path)
    try:
        existing = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
        if target in existing or "*" in existing:
            return
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", target],
            capture_output=True,
            check=False,
        )
    except Exception:  # noqa: BLE001 — safe-dir setup is best-effort
        pass


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


# Matches github.com as well as custom SSH host aliases that contain "github"
# (e.g. ``git@github-codevisionary:owner/repo.git`` from an ~/.ssh/config Host alias or an
# insteadOf rewrite). The host only needs to start with "github" — the canonical api/push
# host is always github.com, so we only extract owner/name here.
_GITHUB_REMOTE_RE = re.compile(
    r"""github[\w.-]*[:/]+(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"""
)


def parse_github_remote(url: str) -> GitHubRepo | None:
    """Extract owner/name from an https or ssh GitHub remote URL (None if not GitHub).

    Handles plain ``github.com`` remotes and custom host aliases like
    ``git@github-codevisionary:owner/repo.git`` so a tokenized HTTPS push can be built even
    when the configured remote points at an ssh alias.
    """
    match = _GITHUB_REMOTE_RE.search(url.strip())
    if not match:
        return None
    return GitHubRepo(owner=match.group("owner"), name=match.group("name"))


class GitError(RuntimeError):
    pass


class GitService:
    def __init__(self, workspace: Path) -> None:
        # Clear "dubious ownership" before any git command runs (Docker bind mounts, etc.).
        _ensure_git_safe_directory(Path(workspace))
        try:
            self.repo = Repo(workspace, search_parent_directories=True)
        except InvalidGitRepositoryError as exc:
            raise GitError(f"{workspace} is not inside a git repository") from exc
        # The actual repo root may be a parent of `workspace` — mark it too.
        _ensure_git_safe_directory(Path(self.repo.working_dir))

    # --------------------------------------------------------------------- #
    # Branch / status
    # --------------------------------------------------------------------- #

    def current_branch(self) -> str:
        if self.repo.head.is_detached:
            return self.repo.head.commit.hexsha[:8]
        return self.repo.active_branch.name

    def create_branch(self, name: str | None = None, base: str | None = None) -> str:
        """Create and check out a new branch. Returns the branch name actually used."""
        branch_name = (
            self.sanitize_branch_name(name) if name else self.suggest_branch_name()
        )
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

    # --------------------------------------------------------------------- #
    # Worktrees
    # --------------------------------------------------------------------- #

    def worktree_base_dir(self) -> Path:
        """Where agent worktrees live. Outside the workspace by default so the user's dev
        server (watching the workspace) never sees them, and out of ``.git``.

        Override with ``AGENTBRIDGE_WORKTREE_DIR``.
        """
        env = os.environ.get("AGENTBRIDGE_WORKTREE_DIR")
        if env:
            return Path(env).expanduser()
        root = Path(self.repo.working_dir)
        return root.parent / ".agentbridge-worktrees"

    def _stash_count(self) -> int:
        return len(
            [ln for ln in self.repo.git.stash("list").splitlines() if ln.strip()]
        )

    def worktree_dir_for(self, branch: str) -> Path:
        slug = branch.replace("/", "__")
        return self.worktree_base_dir() / f"{Path(self.repo.working_dir).name}__{slug}"

    def _registered_worktrees(self) -> set[str]:
        out = self.repo.git.worktree("list", "--porcelain")
        paths: set[str] = set()
        for line in out.splitlines():
            if line.startswith("worktree "):
                paths.add(str(Path(line[len("worktree ") :].strip())))
        return paths

    def ensure_worktree(self, branch: str, base: str | None = None) -> Path:
        """Return a worktree dir checked out to ``branch``, creating it if needed.

        The workspace's own HEAD is never switched — the branch is only ever checked out in
        this dedicated worktree, so the workspace (and the dev server running against it)
        keeps its current branch. Reuses an existing worktree for the branch if present.
        """
        path = self.worktree_dir_for(branch)
        if path.exists() and str(path) in self._registered_worktrees():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        heads = {h.name for h in self.repo.heads}
        base_ref = base or self.current_branch()
        try:
            if branch in heads:
                self.repo.git.worktree("add", str(path), branch)
            else:
                self.repo.git.worktree("add", "-b", branch, str(path), base_ref)
        except GitCommandError as exc:
            raise GitError(f"Could not create worktree for '{branch}': {exc}") from exc
        return path

    def top_level_entries(self) -> list[str]:
        """Tracked entries at the repo root (dirs suffixed with '/'), for a quick orientation
        map handed to the agent so it doesn't guess at paths. Empty on any error."""
        try:
            out = self.repo.git.ls_tree("HEAD")
        except GitCommandError:
            return []
        entries: list[str] = []
        for line in out.splitlines():
            meta, _, name = line.partition("\t")
            if not name:
                continue
            entries.append(name + ("/" if " tree " in meta else ""))
        return sorted(entries)

    def _tracked_files(self) -> list[str]:
        """All tracked files, repo-root-relative (forward slashes). Empty on any error."""
        try:
            out = self.repo.git.ls_files()
        except GitCommandError:
            return []
        return [ln for ln in out.splitlines() if ln]

    @staticmethod
    def _normalize_source_path(raw: str) -> str:
        """Strip the cruft a browser puts around a source path (URL schemes, query strings,
        bundler prefixes, leading ./ or /), leaving something close to a repo-relative path.
        Suffix matching in :meth:`resolve_tracked_path` tolerates whatever prefix remains."""
        p = raw.strip().split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
        for scheme in ("webpack-internal:///", "webpack://", "file://", "rsc://React/Server/", "rsc://React/Client/"):
            if p.startswith(scheme):
                p = p[len(scheme):]
        p = re.sub(r"^\(.*?\)/", "", p)  # webpack "(namespace)/..." wrappers
        p = p.lstrip("/")
        while p.startswith("./"):
            p = p[2:]
        return p

    def resolve_tracked_path(self, raw: str) -> str | None:
        """Map a browser-reported source path (often an absolute build-machine path that does
        not exist under the agent's workspace) to a real repo-relative tracked file, so the
        agent opens it directly instead of searching. Matches by longest unique path suffix;
        returns None if nothing matches or the match is ambiguous."""
        cand = self._normalize_source_path(raw or "")
        if not cand:
            return None
        files = self._tracked_files()
        if not files:
            return None
        file_set = set(files)
        if cand in file_set:
            return cand
        segs = [s for s in cand.split("/") if s]
        # Most specific first: try the full suffix, then drop leading segments.
        for start in range(len(segs)):
            suffix = "/".join(segs[start:])
            if suffix in file_set:
                return suffix
            matches = [f for f in files if f.endswith("/" + suffix)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None  # ambiguous at the most specific level that matched
        return None

    def is_path_dirty(self, path: str) -> bool:
        """Whether ``path`` (workspace-relative) has uncommitted changes (modified, staged,
        deleted, or untracked)."""
        try:
            out = self.repo.git.status("--porcelain", "--", path)
        except GitCommandError:
            return False
        return bool(out.strip())

    def migrate_uncommitted_to(
        self, worktree_path: Path, paths: list[str] | None = None
    ) -> bool:
        """Move uncommitted changes into ``worktree_path``.

        Used at commit/PR time: the agent edits in place (so hot reload shows changes), and
        only when the user opens a PR are those edits relocated onto the branch worktree.
        When ``paths`` is given, only those files are moved — the workspace keeps its other
        (pre-existing) changes — so we commit only what the agent actually touched. Returns
        True if anything moved.
        """
        if paths is not None and not paths:
            return False  # an explicit empty scope means "move nothing" (never sweep everything)
        if not self.has_uncommitted_changes():
            return False
        before = self._stash_count()
        args = ["push", "--include-untracked", "-m", "agentbridge:stage"]
        if paths:
            args += ["--", *paths]
        self.repo.git.stash(*args)
        if self._stash_count() <= before:
            return False  # nothing matched the pathspec
        wt = Repo(worktree_path)
        try:
            wt.git.stash("apply", "stash@{0}")
            self.repo.git.stash("drop", "stash@{0}")
        except GitCommandError as exc:
            raise GitError(f"Staging changes into the worktree failed: {exc}") from exc
        return True

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
        staged = (
            self.repo.index.diff("HEAD")
            if self.repo.head.is_valid()
            else self.repo.index.entries
        )
        if not staged:
            return None
        try:
            commit = self.repo.index.commit(message)
        except Exception as exc:  # noqa: BLE001
            raise GitError(f"Commit failed: {exc}") from exc
        return commit.hexsha

    def has_uncommitted_changes(self) -> bool:
        return self.repo.is_dirty(untracked_files=True)

    def _authed_push_url(self, remote: str, token: str | None) -> str | None:
        """An HTTPS push URL with the token embedded, for GitHub remotes.

        Lets us push without ssh (often absent in containers) or pre-configured credentials,
        reusing the same token used for PR creation. Returns None when there's no token or the
        remote isn't GitHub, in which case we fall back to the configured remote.
        """
        if not token:
            return None
        gh = self.github_repo(remote)
        if gh is None:
            return None
        return f"https://x-access-token:{token}@github.com/{gh.api_path}.git"

    def push(
        self,
        branch: str | None = None,
        remote: str = "origin",
        token: str | None = None,
    ) -> None:
        branch = branch or self.current_branch()
        url = self._authed_push_url(remote, token)
        try:
            if url:
                # Push straight to the tokenized HTTPS URL — no ssh / stored creds needed.
                self.repo.git.push(url, f"refs/heads/{branch}:refs/heads/{branch}")
            else:
                self.repo.git.push("--set-upstream", remote, branch)
        except GitCommandError as exc:
            msg = str(exc)
            if token:
                msg = msg.replace(token, "***")  # never leak the token in an error
            # The most common failure: a GitHub remote but no token, so we fell back to ssh
            # (often unavailable/unconfigured in containers, or rewritten by an insteadOf rule).
            if not url and self.github_repo(remote) is not None:
                msg += (
                    " — set GITHUB_TOKEN (or GH_TOKEN) so AgentBridge pushes to GitHub over "
                    "HTTPS instead of ssh"
                )
            raise GitError(f"Push to {remote}/{branch} failed: {msg}") from exc

    # --------------------------------------------------------------------- #
    # PR creation (GitHub)
    # --------------------------------------------------------------------- #

    def github_repo(self, remote: str = "origin") -> GitHubRepo | None:
        # Explicit override wins — lets the user point us at the GitHub repo directly when the
        # configured remote is a custom ssh host alias / insteadOf rewrite we can't resolve.
        # Same "owner/name" form GitHub Actions uses for GITHUB_REPOSITORY.
        env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if "/" in env_repo:
            owner, _, name = env_repo.partition("/")
            name = name.removesuffix(".git")
            if owner and name:
                return GitHubRepo(owner=owner, name=name)
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
            raise GitError(
                f"GitHub PR creation failed ({resp.status_code}): {resp.text}"
            )
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
        suffix = (
            self.repo.head.commit.hexsha[:4] if self.repo.head.is_valid() else "new"
        )
        return f"{base}-{suffix}"
