from dataclasses import dataclass
from typing import Any, Protocol

from .models import UsageEvent


@dataclass
class OpenStream:
    response: Any
    provider_name: str


class UpstreamClient(Protocol):
    async def request_json(
        self,
        provider_name: str,
        path: str,
        body: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]: ...

    async def open_stream(
        self,
        provider_name: str,
        path: str,
        body: dict[str, Any],
        request_id: str,
    ) -> OpenStream: ...

    async def close(self) -> None: ...


class UsageRepository(Protocol):
    async def initialize(self) -> None: ...

    async def record(self, event: UsageEvent) -> None: ...

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]: ...

    async def save_checkpoint(
        self,
        request_id: str,
        api: str,
        model: str,
        status: str,
        content: str,
    ) -> None: ...

    async def get_checkpoint(self, request_id: str) -> dict[str, Any] | None: ...
