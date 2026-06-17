import subprocess
from pathlib import Path

import pytest

from agentbridge.git_service import GitService, parse_github_remote


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
