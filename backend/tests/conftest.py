import pytest


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path_factory, monkeypatch):
    """Isolate git's global/system config for every test.

    GitService marks its workspace as a safe.directory via ``git config --global``; without
    this, running the suite would write throwaway temp paths into the developer's real
    ~/.gitconfig. Point --global at a per-test throwaway file and ignore system config.
    """
    cfg = tmp_path_factory.mktemp("gitcfg") / "config"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
