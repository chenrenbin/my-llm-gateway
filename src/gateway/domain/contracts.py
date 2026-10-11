from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PromptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    description: str | None = None
    role: Literal["system", "developer"] = "system"
    activate: bool = True


class PromptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    name: str
    description: str | None
    role: str
    content: str
    is_active: bool
    created_at: str
