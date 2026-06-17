"""WebSocket wire protocol shared between the widget and the server.

Every message is a JSON object with a ``type`` discriminator. Client→server messages
are parsed via :func:`parse_client_message`; server→client messages are Pydantic models
that serialize with ``.model_dump()``.

Keep this file in sync with ``widget/src/agentbridge-widget.js`` — it is the contract.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# --------------------------------------------------------------------------- #
# Shared value objects
# --------------------------------------------------------------------------- #


class AgentInfo(BaseModel):
    name: str
    label: str
    available: bool
    capabilities: dict[str, bool] = Field(default_factory=dict)


class FileChange(BaseModel):
    path: str
    status: str  # porcelain code: M, A, D, R, ??, ...


# --------------------------------------------------------------------------- #
# Client -> server
# --------------------------------------------------------------------------- #


class ListAgents(BaseModel):
    type: Literal["list_agents"]


class StartSession(BaseModel):
    type: Literal["start_session"]
    agent: str
    title: str | None = None


class UserMessage(BaseModel):
    type: Literal["user_message"]
    text: str


class AgentResponse(BaseModel):
    type: Literal["agent_response"]
    request_id: str
    answer: str


class CreateBranch(BaseModel):
    type: Literal["create_branch"]
    name: str | None = None
    base_branch: str | None = None


class CreatePR(BaseModel):
    type: Literal["create_pr"]
    title: str
    body: str | None = None


class EndSession(BaseModel):
    type: Literal["end_session"]


ClientMessage = Annotated[
    Union[
        ListAgents,
        StartSession,
        UserMessage,
        AgentResponse,
        CreateBranch,
        CreatePR,
        EndSession,
    ],
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(data: dict) -> ClientMessage:
    """Validate a decoded JSON object into a typed client message (raises on bad input)."""
    return _client_adapter.validate_python(data)


# --------------------------------------------------------------------------- #
# Server -> client
# --------------------------------------------------------------------------- #


class ServerMessage(BaseModel):
    """Base for all server→client messages. Subclasses set a literal ``type``."""


class Agents(ServerMessage):
    type: Literal["agents"] = "agents"
    agents: list[AgentInfo]


class SessionStarted(ServerMessage):
    type: Literal["session_started"] = "session_started"
    session_id: str
    agent: str
    branch: str  # current HEAD — no new branch is created at session start


class AgentChunk(ServerMessage):
    type: Literal["agent_chunk"] = "agent_chunk"
    text: str
    stream: Literal["stdout", "stderr", "thinking"] = "stdout"


class AgentPrompt(ServerMessage):
    type: Literal["agent_prompt"] = "agent_prompt"
    request_id: str
    prompt: str
    # When present, the widget renders one button per option (e.g. ["Allow", "Deny"])
    # and sends the chosen label back as the answer. Absent => free-text reply.
    options: list[str] | None = None


class BranchSuggested(ServerMessage):
    type: Literal["branch_suggested"] = "branch_suggested"
    suggested_name: str
    reason: str


class BranchCreated(ServerMessage):
    type: Literal["branch_created"] = "branch_created"
    branch: str


class FileChanges(ServerMessage):
    type: Literal["file_changes"] = "file_changes"
    files: list[FileChange]


class PRCreated(ServerMessage):
    type: Literal["pr_created"] = "pr_created"
    url: str
    number: int | None = None


class Status(ServerMessage):
    type: Literal["status"] = "status"
    state: str  # idle | thinking | working | done


class ErrorMessage(ServerMessage):
    type: Literal["error"] = "error"
    message: str
