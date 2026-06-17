from pathlib import Path

import pytest

from agentbridge.store import ChatStore, state_dir_for


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path / "state"))


def test_state_dir_namespaced_by_workspace(tmp_path):
    a = state_dir_for(tmp_path / "repo-a")
    b = state_dir_for(tmp_path / "repo-b")
    assert a != b  # different workspaces don't share history


def test_create_load_roundtrip(tmp_path):
    store = ChatStore(tmp_path)
    rec = store.create(agent="claude-code", title="Fix login")
    rec.transcript.append({"kind": "user", "text": "hello"})
    rec.resume_id = "sess-123"
    store.save(rec)

    loaded = store.load(rec.id)
    assert loaded is not None
    assert loaded.agent == "claude-code"
    assert loaded.resume_id == "sess-123"
    assert loaded.transcript[0]["text"] == "hello"


def test_list_meta_sorted_and_counts(tmp_path):
    store = ChatStore(tmp_path)
    first = store.create(agent="cursor", title="first")
    second = store.create(agent="cursor", title="second")
    second.transcript += [{"kind": "user", "text": "a"}, {"kind": "agent", "text": "b"}]
    store.save(second)  # bumps updated_at -> sorts ahead of `first`

    metas = store.list_meta()
    assert [m["id"] for m in metas][0] == second.id
    by_id = {m["id"]: m for m in metas}
    assert by_id[second.id]["message_count"] == 2
    assert by_id[first.id]["message_count"] == 0


def test_delete(tmp_path):
    store = ChatStore(tmp_path)
    rec = store.create(agent="cursor")
    assert store.delete(rec.id) is True
    assert store.load(rec.id) is None
    assert store.delete("nonexistent") is False
