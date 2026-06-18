import subprocess
from pathlib import Path

import pytest
from git import GitCommandError

from agentbridge.git_service import GitError, GitService, _ensure_git_safe_directory, parse_github_remote


def _safe_dirs() -> list[str]:
    return subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        capture_output=True, text=True,
    ).stdout.splitlines()


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


def test_current_branch(repo: Path):
    svc = GitService(repo)
    assert svc.current_branch() == "main"


def test_ensure_safe_directory_is_idempotent(tmp_path: Path):
    target = tmp_path / "ws"
    target.mkdir()
    _ensure_git_safe_directory(target)
    _ensure_git_safe_directory(target)  # second call must not duplicate
    assert _safe_dirs().count(str(target)) == 1


def test_gitservice_marks_workspace_safe(repo: Path):
    # Constructing the service clears "dubious ownership" by trusting the workspace path.
    GitService(repo)
    assert str(repo) in _safe_dirs()


def test_create_branch_switches(repo: Path):
    svc = GitService(repo)
    branch = svc.create_branch("agentbridge/fix-thing")
    assert branch == "agentbridge/fix-thing"
    assert svc.current_branch() == branch


def test_create_branch_dedupes(repo: Path):
    svc = GitService(repo)
    a = svc.create_branch("agentbridge/x")
    svc.repo.git.checkout("main")
    b = svc.create_branch("agentbridge/x")
    assert a != b and b == "agentbridge/x-2"


def test_authed_push_url_for_github_remote(repo: Path):
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    svc = GitService(repo)
    assert svc._authed_push_url("origin", "tok123") == "https://x-access-token:tok123@github.com/acme/widgets.git"
    assert svc._authed_push_url("origin", None) is None  # no token -> use the configured remote


def test_authed_push_url_skips_non_github(repo: Path):
    subprocess.run(["git", "remote", "add", "origin", "https://gitlab.com/a/b.git"], cwd=repo, check=True)
    assert GitService(repo)._authed_push_url("origin", "tok") is None


def test_push_uses_token_https_url(repo: Path, monkeypatch):
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    svc = GitService(repo)
    seen = {}
    # Git uses __slots__ + __getattr__ dispatch, so patch the command at the class level.
    monkeypatch.setattr(type(svc.repo.git), "push", lambda self, *a, **k: seen.setdefault("args", a), raising=False)
    svc.push("agentbridge/feature", token="secret")
    # Pushed over HTTPS with the embedded token, not via ssh/--set-upstream.
    assert "https://x-access-token:secret@github.com/acme/widgets.git" in seen["args"]
    assert "refs/heads/agentbridge/feature:refs/heads/agentbridge/feature" in seen["args"]


def test_push_scrubs_token_from_errors(repo: Path, monkeypatch):
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    svc = GitService(repo)

    def boom(self, *a, **k):
        raise GitCommandError(
            "git push https://x-access-token:secret@github.com/acme/widgets.git", 128, b"denied"
        )

    monkeypatch.setattr(type(svc.repo.git), "push", boom, raising=False)
    with pytest.raises(GitError) as ei:
        svc.push("agentbridge/feature", token="secret")
    assert "secret" not in str(ei.value) and "***" in str(ei.value)


def test_push_without_token_hints_to_set_github_token(repo: Path, monkeypatch):
    # No token + GitHub remote -> ssh fallback; if it fails, the error should tell the user
    # to set GITHUB_TOKEN rather than leaving a cryptic ssh message.
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    svc = GitService(repo)

    def boom(self, *a, **k):
        raise GitCommandError("git push", 128, b"ssh: could not resolve hostname")

    monkeypatch.setattr(type(svc.repo.git), "push", boom, raising=False)
    with pytest.raises(GitError) as ei:
        svc.push("agentbridge/feature")  # no token
    assert "GITHUB_TOKEN" in str(ei.value)


def test_ensure_worktree_leaves_workspace_branch(repo: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(repo.parent / "wt"))
    svc = GitService(repo)
    path = svc.ensure_worktree("agentbridge/feature")
    # Workspace stays on main; the worktree is a separate dir checked out to the new branch.
    assert svc.current_branch() == "main"
    assert path.is_dir() and (path / ".git").exists()
    assert GitService(path).current_branch() == "agentbridge/feature"
    # Idempotent: asking again returns the same worktree.
    assert svc.ensure_worktree("agentbridge/feature") == path


def test_migrate_uncommitted_to_relocates_changes(repo: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(repo.parent / "wt"))
    svc = GitService(repo)
    # Simulate the agent having edited files in place (live in the workspace).
    (repo / "tracked_change.txt").write_text("edit")
    (repo / "untracked.txt").write_text("new file")

    path = svc.ensure_worktree("agentbridge/migrate")
    moved = svc.migrate_uncommitted_to(path)

    assert moved is True
    # Changes were relocated onto the branch worktree...
    assert (path / "tracked_change.txt").read_text() == "edit"
    assert (path / "untracked.txt").read_text() == "new file"
    # ...and the workspace is restored to clean (branch never switched).
    assert not svc.has_uncommitted_changes()
    assert svc.current_branch() == "main"


def test_status_reports_changes(repo: Path):
    svc = GitService(repo)
    (repo / "new.txt").write_text("data")
    statuses = {c.path: c.status for c in svc.status()}
    assert "new.txt" in statuses


def test_commit_all_returns_none_when_clean(repo: Path):
    svc = GitService(repo)
    assert svc.commit_all("nothing") is None


def test_commit_all_commits_changes(repo: Path):
    svc = GitService(repo)
    (repo / "f.txt").write_text("x")
    sha = svc.commit_all("add f")
    assert sha and not svc.has_uncommitted_changes()


@pytest.mark.parametrize(
    "url,owner,name",
    [
        ("git@github.com:acme/widgets.git", "acme", "widgets"),
        ("https://github.com/acme/widgets.git", "acme", "widgets"),
        ("https://github.com/acme/widgets", "acme", "widgets"),
    ],
)
def test_parse_github_remote(url, owner, name):
    gh = parse_github_remote(url)
    assert gh and gh.owner == owner and gh.name == name


def test_parse_github_remote_non_github():
    assert parse_github_remote("https://gitlab.com/a/b.git") is None
