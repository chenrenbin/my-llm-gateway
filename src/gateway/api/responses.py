from pydantic import BaseModel, ConfigDict

from ..domain.contracts import PromptRecord

__all__ = ["ErrorDetail", "ErrorResponse", "ModelList", "ModelObject", "PromptRecord"]


class ModelObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    object: str = "model"
    created: int
    owned_by: str = "llm-gateway"


class ModelList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: str = "list"
    data: list[ModelObject]


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
