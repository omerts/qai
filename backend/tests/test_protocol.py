import pytest
from pydantic import ValidationError

from agentbridge import protocol as P


def test_parse_start_session():
    msg = P.parse_client_message({"type": "start_session", "agent": "claude-code", "title": "fix"})
    assert isinstance(msg, P.StartSession)
    assert msg.agent == "claude-code"
    assert msg.title == "fix"


def test_parse_create_branch_optional_fields():
    msg = P.parse_client_message({"type": "create_branch"})
    assert isinstance(msg, P.CreateBranch)
    assert msg.name is None and msg.base_branch is None


def test_parse_unknown_type_raises():
    with pytest.raises(ValidationError):
        P.parse_client_message({"type": "nope"})


def test_server_messages_carry_type_in_dump():
    assert P.SessionStarted(session_id="s", agent="cursor", branch="main").model_dump()["type"] == "session_started"
    assert P.BranchSuggested(suggested_name="x", reason="y").model_dump()["type"] == "branch_suggested"
    assert P.AgentChunk(text="hi").model_dump()["stream"] == "stdout"
