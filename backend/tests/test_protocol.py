import pytest
from pydantic import ValidationError

from agentbridge import protocol as P


def test_parse_start_session():
    msg = P.parse_client_message({"type": "start_session", "agent": "claude-code", "title": "fix"})
    assert isinstance(msg, P.StartSession)
    assert msg.agent == "claude-code"
    assert msg.title == "fix"


def test_parse_create_branch_optional_fields():
    msg = P.parse_client_message({"type": "create_branch", "chat_id": "c1"})
    assert isinstance(msg, P.CreateBranch)
    assert msg.chat_id == "c1" and msg.name is None and msg.base_branch is None


def test_parse_chat_management_messages():
    assert isinstance(P.parse_client_message({"type": "list_chats"}), P.ListChats)
    assert isinstance(P.parse_client_message({"type": "open_chat", "chat_id": "c1"}), P.OpenChat)
    assert isinstance(P.parse_client_message({"type": "delete_chat", "chat_id": "c1"}), P.DeleteChat)


def test_parse_user_message_with_context():
    msg = P.parse_client_message({
        "type": "user_message",
        "chat_id": "c1",
        "text": "fix this button",
        "context": {"page": {"route": "/orders/42", "framework": {"name": "React"}},
                    "element": {"label": "<button>", "component": "SaveButton"}},
    })
    assert isinstance(msg, P.UserMessage)
    assert msg.context["element"]["component"] == "SaveButton"


def test_user_message_context_optional():
    msg = P.parse_client_message({"type": "user_message", "chat_id": "c1", "text": "hi"})
    assert msg.context is None


def test_user_message_requires_chat_id():
    with pytest.raises(ValidationError):
        P.parse_client_message({"type": "user_message", "text": "hi"})


def test_parse_unknown_type_raises():
    with pytest.raises(ValidationError):
        P.parse_client_message({"type": "nope"})


def test_server_messages_carry_type_in_dump():
    assert P.SessionStarted(chat_id="c1", agent="cursor", branch="main").model_dump()["type"] == "session_started"
    assert P.BranchSuggested(chat_id="c1", suggested_name="x", reason="y").model_dump()["type"] == "branch_suggested"
    assert P.AgentChunk(chat_id="c1", text="hi").model_dump()["stream"] == "stdout"
    assert P.Chats(chats=[]).model_dump()["type"] == "chats"
