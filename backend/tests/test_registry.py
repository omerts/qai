from pathlib import Path

from agentbridge.agents.registry import adapter_names, create_adapter, list_agent_info


def test_known_agents_registered():
    names = set(adapter_names())
    assert {"claude-code", "cursor", "aider", "copilot"} <= names


def test_list_agent_info_shape():
    infos = {i.name: i for i in list_agent_info()}
    assert infos["aider"].available is False  # stub
    assert infos["copilot"].available is False  # stub
    for info in infos.values():
        assert info.label
        assert set(info.capabilities) >= {"streaming", "edits_files"}


def test_create_adapter_returns_right_type():
    a = create_adapter("cursor", Path("."))
    assert a.name == "cursor"
