"""WebSocket wire protocol shared between the widget and the server.

Every message is a JSON object with a ``type`` discriminator. Client→server messages
are parsed via :func:`parse_client_message`; server→client messages are Pydantic models
that serialize with ``.model_dump()``.

A single connection multiplexes several **chats**: most messages carry a ``chat_id`` so
the server routes them to the right chat session and the widget renders them in the right
conversation. Connection-level messages (``list_agents``, ``list_chats``, ``start_session``,
``open_chat``, ``delete_chat``) have no ``chat_id`` (or, for ``open_chat``/``delete_chat``,
name the chat directly).

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
    #: Optional accent theming the widget applies when this agent is selected/active.
    #: Keys map to CSS variables: ``accent`` -> ``--ab-accent``, ``accentFg`` -> ``--ab-accent-fg``.
    #: Empty => the widget keeps its default accent.
    theme: dict[str, str] = Field(default_factory=dict)


class FileChange(BaseModel):
    path: str
    status: str  # porcelain code: M, A, D, R, ??, ...


class ChatMeta(BaseModel):
    id: str
    title: str
    agent: str
    updated_at: str
    message_count: int


# --------------------------------------------------------------------------- #
# Client -> server
# --------------------------------------------------------------------------- #


class ListAgents(BaseModel):
    type: Literal["list_agents"]


class ListChats(BaseModel):
    type: Literal["list_chats"]


class StartSession(BaseModel):
    """Create and open a brand-new chat with the given agent."""

    type: Literal["start_session"]
    agent: str
    title: str | None = None


class OpenChat(BaseModel):
    """Reopen an existing chat: replay its transcript and resume the agent."""

    type: Literal["open_chat"]
    chat_id: str


class DeleteChat(BaseModel):
    type: Literal["delete_chat"]
    chat_id: str


class UserMessage(BaseModel):
    type: Literal["user_message"]
    chat_id: str
    text: str
    # Optional browser context the widget collects (route, framework, components, a picked
    # element). Free-form; the server formats it into a preamble for the agent.
    context: dict | None = None
    # When True, the agent auto-approves routine file edits and shell commands for this turn
    # (risky shell commands still prompt). Reflects the widget's auto-approve toggle.
    auto_approve: bool = False
    # Workspace-relative paths of files the user attached to this turn (previously uploaded via
    # ``upload_file``). The server points the agent at them in the preamble.
    attachments: list[str] = Field(default_factory=list)


class UploadFile(BaseModel):
    """A file the user attaches to a chat. The server stores it under the workspace's gitignored
    ``.agentbridge/uploads/`` so the agent can read it by path without it polluting git or a PR."""

    type: Literal["upload_file"]
    chat_id: str
    # Client-generated id echoed back in ``FileUploaded`` so the widget can match the result to
    # the right pending attachment (uploads are handled concurrently, so order isn't guaranteed).
    upload_id: str
    name: str
    # Base64-encoded file bytes, sent inline over the WebSocket.
    data: str


class AgentResponse(BaseModel):
    type: Literal["agent_response"]
    chat_id: str
    request_id: str
    answer: str


class StopAgent(BaseModel):
    """Cancel the in-flight agent turn for a chat."""

    type: Literal["stop"]
    chat_id: str


class CreatePR(BaseModel):
    type: Literal["create_pr"]
    chat_id: str
    # Optional: when the user doesn't type one, the backend derives a meaningful title/body
    # from the agent's own summary of the changes it made.
    title: str | None = None
    body: str | None = None


class EndSession(BaseModel):
    """Free a chat's in-memory session (its persisted history is kept)."""

    type: Literal["end_session"]
    chat_id: str | None = None


ClientMessage = Annotated[
    Union[
        ListAgents,
        ListChats,
        StartSession,
        OpenChat,
        DeleteChat,
        UserMessage,
        UploadFile,
        AgentResponse,
        StopAgent,
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


class Chats(ServerMessage):
    type: Literal["chats"] = "chats"
    chats: list[ChatMeta]


class SessionStarted(ServerMessage):
    type: Literal["session_started"] = "session_started"
    chat_id: str
    agent: str
    title: str | None = None
    branch: str  # current HEAD — no new branch is created at session start


class ChatHistory(ServerMessage):
    """The persisted transcript + state of a reopened chat, for the widget to replay."""

    type: Literal["chat_history"] = "chat_history"
    chat_id: str
    entries: list[dict]
    files: list[FileChange] = Field(default_factory=list)
    branch: str | None = None
    target_branch: str | None = None


class ChatDeleted(ServerMessage):
    type: Literal["chat_deleted"] = "chat_deleted"
    chat_id: str


class AgentChunk(ServerMessage):
    type: Literal["agent_chunk"] = "agent_chunk"
    chat_id: str
    text: str
    stream: Literal["stdout", "stderr", "thinking"] = "stdout"


class AgentPrompt(ServerMessage):
    type: Literal["agent_prompt"] = "agent_prompt"
    chat_id: str
    request_id: str
    prompt: str
    # When present, the widget renders one button per option (e.g. ["Allow", "Deny"])
    # and sends the chosen label back as the answer. Absent => free-text reply.
    options: list[str] | None = None


class BranchCreated(ServerMessage):
    type: Literal["branch_created"] = "branch_created"
    chat_id: str
    branch: str
    # Absolute path of the worktree the branch was committed to (set at PR time). The user's
    # workspace path keeps its original branch untouched.
    worktree_path: str | None = None


class FileChanges(ServerMessage):
    type: Literal["file_changes"] = "file_changes"
    chat_id: str
    files: list[FileChange]


class PRCreated(ServerMessage):
    type: Literal["pr_created"] = "pr_created"
    chat_id: str
    url: str
    number: int | None = None


class FileUploaded(ServerMessage):
    """Result of an ``upload_file``. On success carries the workspace-relative ``path`` the agent
    can read; on failure carries ``error``. ``upload_id`` ties it to the widget's pending chip."""

    type: Literal["file_uploaded"] = "file_uploaded"
    chat_id: str
    upload_id: str
    ok: bool = True
    name: str | None = None
    path: str | None = None
    size: int | None = None
    error: str | None = None


class Status(ServerMessage):
    type: Literal["status"] = "status"
    chat_id: str
    state: str  # idle | thinking | working | done


class ErrorMessage(ServerMessage):
    type: Literal["error"] = "error"
    message: str
    chat_id: str | None = None
