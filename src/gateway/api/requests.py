from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.contracts import PromptCreate

__all__ = [
    "ChatCompletionRequest",
    "ChatMessage",
    "PromptCreate",
    "PromptReference",
    "PromptRender",
    "ResponsesRequest",
]


class FlexibleModel(BaseModel):
    """Strict input: unknown fields are rejected (extra="forbid")."""

    model_config = ConfigDict(extra="forbid")


class ChatMessage(FlexibleModel):
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None


class PromptReference(FlexibleModel):
    id: str
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    position: Literal["prepend", "append"] = "prepend"


class ChatCompletionRequest(FlexibleModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    response_format: dict[str, Any] | None = None
    gateway_prompt: PromptReference | None = None


class ResponsesRequest(FlexibleModel):
    model: str
    input: Any
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    text: dict[str, Any] | None = None
    gateway_prompt: PromptReference | None = None


class PromptRender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
